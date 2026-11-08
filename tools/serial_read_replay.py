"""Replay guard/pretool/serial_read_guard.py over recorded Claude Code sessions.

Usage:
    python tools/serial_read_replay.py --since 2026-11-08T00:00+09:00 \
        --until 2026-11-08T21:05+09:00 --runs 4,5,6 \
        '~/.claude/projects/-Users-Shared-guchipachi-work*/*.jsonl'

For every tool-calling response inside the window, the first tool_use of the
response is judged with the same ``judge()`` the live hook uses. What the hook
would see is rebuilt from line timestamps: a sibling tool_use written within
``--wait`` seconds of the first one is visible (the hook's sibling wait), a
later one is not. A denied response is marked denied for the rest of the
request, as the hook would see its tool_result.

Each deny is then classified:

- ``batch``: the response really had more calls -- a false deny caused by
  siblings written after the wait.
- ``dep``: a single call whose input carries a token (4+ chars) that first
  appeared in the previous response's tool result -- it needed that result,
  so the deny cost a round trip without saving one (estimated false positive).
- ``cont``: a single Read of the same file as the previous call (paging); also
  counted as a false positive.
- ``retry``: the same tool as the previous single call with a near-identical
  input (difflib ratio >= 0.6) -- a fix-up after seeing that result; counted
  as a false positive.
- ``indep``: none of the above -- could have gone in an earlier response.
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import glob
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from slack_cli_agent.guard.pretool.serial_read_guard import (
    Response,
    Use,
    _is_human_turn,
    judge,
)

#: Words, file names and path parts (4+ chars, no slash or colon so a path
#: and a ``path:line:`` grep hit share their parts), and numbers of 3+ digits
#: (line numbers, ids).
TOKEN = re.compile(r"[A-Za-z_가-힣][0-9A-Za-z_.\-가-힣]{3,}|\d{3,}")


def ts(raw: str) -> float:
    return dt.datetime.fromisoformat(raw).timestamp()


def tokens(value: Any) -> set[str]:
    """TOKEN matches plus their 4+ char parts split on ``._-`` (so the id in
    ``b5qaiyw27.output`` matches a bare ``b5qaiyw27``)."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    found = set(TOKEN.findall(text))
    for token in list(found):
        found.update(p for p in re.split(r"[._\-]", token) if len(p) >= 4)
    return found


def novel(call: set[str], prev: set[str], known_words: set[str], known_inputs: set[str]) -> set[str]:
    """Tokens of this call first seen in the previous result. Numbers are only
    discounted when an earlier input or the prompt had them -- big earlier
    results contain most 3-digit numbers by chance."""
    return {
        t for t in call & prev
        if t not in known_inputs and (t.isdigit() or t not in known_words)
    }


def result_text(block: dict[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(b.get("text", "")) for b in content if isinstance(b, dict))
    return ""


def requests(path: str, since: float, until: float) -> list[dict[str, Any]]:
    """Split one transcript into requests. Each request keeps its prompt text
    and its responses as (message_id, [(id, name, input, t)]) plus results."""
    out: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    with open(path, encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict) or "timestamp" not in rec:
                continue
            t = ts(rec["timestamp"])
            kind = rec.get("type")
            msg = rec.get("message") or {}
            content = msg.get("content") if isinstance(msg, dict) else None
            if kind == "user":
                if _is_human_turn(content):
                    cur = {"prompt": content if isinstance(content, str) else json.dumps(content, ensure_ascii=False),
                           "t": t, "responses": [], "results": {}, "errors": set(),
                           "in_window": since <= t < until, "path": path}
                    out.append(cur)
                elif cur is not None:
                    for b in content or []:
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            cur["results"][str(b.get("tool_use_id"))] = result_text(b)
                            if b.get("is_error"):
                                cur["errors"].add(str(b.get("tool_use_id")))
                continue
            if kind != "assistant" or cur is None or not isinstance(content, list):
                continue
            uses = [(str(b.get("id")), str(b.get("name", "")), b.get("input") or {}, t)
                    for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
            if not uses:
                continue
            mid = msg.get("id")
            if cur["responses"] and cur["responses"][-1][0] == mid:
                cur["responses"][-1][1].extend(uses)
            else:
                cur["responses"].append((mid, uses))
    return [r for r in out if r["in_window"]]


def replay(reqs: list[dict[str, Any]], run_needed: int, wait: float) -> Counter[str]:
    c: Counter[str] = Counter()
    for req in reqs:
        seen: list[Response] = []
        known = tokens(req["prompt"])
        known_inputs = set(known)
        prev_tokens: set[str] = set()
        prev_single: tuple[str, dict[str, Any]] | None = None
        for mid, uses in req["responses"]:
            c["responses"] += 1
            first_id, name, tool_input, t0 = uses[0]
            visible = [Use(i, n, inp) for i, n, inp, t in uses if i == first_id or t - t0 <= wait]
            verdict, _ = judge([*seen, Response(mid, visible)], first_id, name, tool_input, run_needed)
            response = Response(mid, [Use(i, n, inp) for i, n, inp, _ in uses])
            if verdict == "deny":
                c["deny"] += 1
                if len(uses) > 1:
                    c["batch"] += 1
                elif (name == "Read" and prev_single and prev_single[0] == "Read"
                      and prev_single[1].get("file_path") == tool_input.get("file_path")):
                    c["cont"] += 1
                elif prev_single and prev_single[0] == name and difflib.SequenceMatcher(
                    None, json.dumps(prev_single[1], ensure_ascii=False), json.dumps(tool_input, ensure_ascii=False),
                ).ratio() >= 0.6:
                    c["retry"] += 1
                elif novel(tokens(tool_input), prev_tokens, known, known_inputs):
                    c["dep"] += 1
                else:
                    c["indep"] += 1
                for u in response.uses:
                    u.denied = True
            seen.append(response)
            # what the next call could have known before this response's results
            known |= prev_tokens
            for _, _, inp, _ in uses:
                known |= tokens(inp)
                known_inputs |= tokens(inp)
            prev_tokens = set()
            for i, _, _, _ in uses:
                prev_tokens |= tokens(req["results"].get(i, ""))
            prev_single = (name, tool_input) if len(uses) == 1 else None
        c["requests"] += 1
    return c


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pattern")
    ap.add_argument("--since", required=True)
    ap.add_argument("--until", required=True)
    ap.add_argument("--runs", default="4")
    ap.add_argument("--wait", type=float, default=1.5)
    args = ap.parse_args()
    since, until = dt.datetime.fromisoformat(args.since).timestamp(), dt.datetime.fromisoformat(args.until).timestamp()
    files = [f for f in glob.glob(os.path.expanduser(args.pattern)) if os.path.getmtime(f) >= since]
    reqs = [r for f in files for r in requests(f, since, until)]
    sessions = len({f for f in files if requests(f, since, until)})
    print(f"files={len(files)} sessions_with_requests={sessions} requests={len(reqs)}")
    print(
        "| N | 대기(초) | 요청 | 도구 응답 | deny | 묶음 오거부 | 의존 | 이어읽기 "
        "| 고쳐 재호출 | 독립 | 오탐 추정/deny |"
    )
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for n in [int(x) for x in args.runs.split(",")]:
        for wait in sorted({0.0, args.wait}):
            c = replay(reqs, n, wait)
            fp = c["batch"] + c["dep"] + c["cont"] + c["retry"]
            rate = f"{fp / c['deny'] * 100:.1f}%" if c["deny"] else "-"
            print(f"| {n} | {wait:g} | {c['requests']} | {c['responses']} | {c['deny']} | {c['batch']} | "
                  f"{c['dep']} | {c['cont']} | {c['retry']} | {c['indep']} | {fp}/{c['deny']} = {rate} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
