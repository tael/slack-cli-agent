"""Replay guard/pretool/plan_gate.py over recorded Claude Code sessions.

Usage:
    python tools/plan_gate_replay.py --since 2026-11-06T00:00+09:00 \
        --responses 3,4 --audit ~/.guchipachi-sca/audit.jsonl \
        '~/.claude/projects/-Users-Shared-guchipachi-work*/*.jsonl'

For every request (human input to the next human input) that starts inside
the window, each tool-calling response is judged at its first tool_use line
with the same ``responses_since_prompt()`` and ``judge()`` the live hook
uses: the transcript is cut right after that line, so the response's own
text written before it counts and later lines do not. The first deny of a
request is recorded and the rest of the request is not judged: once denied,
the live model writes a plan and the recorded tail no longer matches what
would have happened.

With ``--audit`` each request is matched to the audit ``request`` row of the
same session that finished first after the request began, and its ``tier``
is used to split the counts into turns the flow applies to (the same
``FLOW_TIERS`` as FlowSection) and the rest. Rows from before the audit
carried ``tier`` count as ``unknown``. Sessions with no request row at all
(nested finish-review verify runs, watch checks, reviews) are
``not_a_request``: the pipeline never sets PLAN_GATE there. Chat channels
are not separated.
"""

from __future__ import annotations

import argparse
import bisect
import datetime as dt
import glob
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from slack_cli_agent.guard.pretool.plan_gate import (
    PLAN_TAGS,
    is_request_start,
    judge,
    responses_since_prompt,
)
from slack_cli_agent.prompt.sections import FLOW_TIERS


def ts(raw: str) -> float:
    return dt.datetime.fromisoformat(raw).timestamp()


def load(path: str) -> list[dict[str, Any]]:
    records = []
    with open(path, encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                records.append(record)
    return records


def requests_of(records: list[dict[str, Any]]) -> list[tuple[int, int]]:
    """(start, end) record index ranges, one per human input."""
    starts = [i for i, record in enumerate(records) if is_request_start(record)]
    return [(s, starts[k + 1] if k + 1 < len(starts) else len(records)) for k, s in enumerate(starts)]


def first_uses(records: list[dict[str, Any]], start: int, end: int) -> list[tuple[int, str, Any]]:
    """(record index, tool_use id, message id) of each response's first tool_use."""
    seen: set[Any] = set()
    out = []
    for i in range(start, end):
        record = records[i]
        if record.get("type") != "assistant":
            continue
        message = record.get("message") or {}
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                mid = message.get("id")
                if mid not in seen:
                    seen.add(mid)
                    out.append((i, str(block.get("id", "")), mid))
                break
    return out


def worker_agents(records: list[dict[str, Any]], start: int, end: int) -> int:
    """Agent calls that are not the fable advisor (finish_review._is_advisor_call)."""
    count = 0
    for record in records[start:end]:
        if record.get("type") != "assistant":
            continue
        for block in (record.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") in ("Agent", "Task"):
                model = str((block.get("input") or {}).get("model") or "")
                if "fable" not in model:
                    count += 1
    return count


def load_tiers(path: str) -> dict[str, list[tuple[float, str]]]:
    rows: dict[str, list[tuple[float, str]]] = defaultdict(list)
    if not path:
        return rows
    with open(os.path.expanduser(path), encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("kind") != "request" or not row.get("session_id"):
                continue
            rows[str(row["session_id"])].append((float(row.get("at") or 0), str(row.get("tier") or "")))
    for value in rows.values():
        value.sort()
    return rows


def tier_for(rows: list[tuple[float, str]], started: float) -> str:
    index = bisect.bisect_left(rows, (started, ""))
    if index >= len(rows):
        return "unmatched"
    return rows[index][1] or "unknown"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("patterns", nargs="+")
    parser.add_argument("--since", default="2026-11-06T00:00+09:00")
    parser.add_argument("--until", default="")
    parser.add_argument("--responses", default="3")
    parser.add_argument("--audit", default="")
    args = parser.parse_args()
    since = ts(args.since)
    until = ts(args.until) if args.until else float("inf")
    thresholds = [int(x) for x in args.responses.split(",")]
    tiers = load_tiers(args.audit)

    files = sorted({f for p in args.patterns for f in glob.glob(os.path.expanduser(p))})
    total = 0
    by_group: dict[str, Counter[str]] = defaultdict(Counter)
    tool_responses: Counter[int] = Counter()
    deny_at: dict[int, Counter[int]] = {t: Counter() for t in thresholds}
    denied_by_group: dict[int, Counter[str]] = {t: Counter() for t in thresholds}
    denied_with_worker: dict[int, int] = dict.fromkeys(thresholds, 0)
    short_after_deny: dict[int, int] = dict.fromkeys(thresholds, 0)
    flow_short: dict[int, int] = dict.fromkeys(thresholds, 0)
    flow_worker: dict[int, int] = dict.fromkeys(thresholds, 0)
    flow_responses: Counter[int] = Counter()
    planned_already = 0
    for path in files:
        records = load(path)
        session = Path(path).stem
        for start, end in requests_of(records):
            raw = records[start].get("timestamp")
            if not raw:
                continue
            started = ts(raw)
            if not since <= started < until:
                continue
            total += 1
            uses = first_uses(records, start, end)
            tool_responses[len(uses)] += 1
            if tiers and session not in tiers:
                # Nested finish-review verify runs, watch checks and reviews:
                # no request row, and the pipeline never sets PLAN_GATE there.
                tier = "not_a_request"
            else:
                tier = tier_for(tiers.get(session, []), started) if tiers else "unknown"
            group = (
                "flow" if tier in FLOW_TIERS
                else tier if tier in ("unknown", "unmatched", "not_a_request")
                else "other"
            )
            by_group[group]["requests"] += 1
            if group == "flow":
                flow_responses[len(uses)] += 1
            texts = [
                str(b.get("text") or "")
                for r in records[start:end] if r.get("type") == "assistant"
                for b in (r.get("message") or {}).get("content") or []
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            if any(tag in t for t in texts for tag in PLAN_TAGS):
                planned_already += 1
            for threshold in thresholds:
                for index, use_id, _mid in uses:
                    responses = responses_since_prompt(records[: index + 1])
                    verdict, count = judge(responses, use_id, threshold)
                    if verdict == "deny":
                        deny_at[threshold][count] += 1
                        denied_by_group[threshold][group] += 1
                        worker = bool(worker_agents(records, start, end))
                        short = len(uses) <= threshold + 1
                        denied_with_worker[threshold] += worker
                        short_after_deny[threshold] += short
                        if group == "flow":
                            flow_worker[threshold] += worker
                            flow_short[threshold] += short
                        break
    print(f"files {len(files)}  requests {total}  with a plan tag already {planned_already}")
    print("tool-calling responses per request:", dict(sorted(tool_responses.items())))
    for group, counter in sorted(by_group.items()):
        print(f"  group {group}: {dict(counter)}")
    print("flow tiers, tool-calling responses per request:", dict(sorted(flow_responses.items())))
    flow_total = by_group["flow"]["requests"]
    for threshold in thresholds:
        flow_denied = denied_by_group[threshold]["flow"]
        print(
            f"PLAN_GATE={threshold} flow tiers: denied {flow_denied} of {flow_total} "
            f"({flow_denied / max(flow_total, 1):.1%}), already used a worker Agent {flow_worker[threshold]}, "
            f"ended within {threshold + 1} responses {flow_short[threshold]}"
        )
    for threshold in thresholds:
        denied = sum(deny_at[threshold].values())
        print(
            f"PLAN_GATE={threshold}: denied requests {denied} ({denied / max(total, 1):.1%}), "
            f"at response {dict(sorted(deny_at[threshold].items()))}, "
            f"by group {dict(denied_by_group[threshold])}, "
            f"already used a worker Agent {denied_with_worker[threshold]}, "
            f"ended within {threshold + 1} responses {short_after_deny[threshold]}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
