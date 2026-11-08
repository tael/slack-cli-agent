"""Replay guard/pretool/repeat_guard.py over recorded Claude Code sessions.

Usage:
    python tools/repeat_replay.py --since 2026-11-06T00:00+09:00 \
        --until 2026-11-09T00:00+09:00 --runs 2,3 --similarity 0.9,1.0 \
        --samples /tmp/repeat_samples.md \
        '~/.claude/projects/-Users-Shared-guchipachi-work*/*.jsonl'

Every tool call of every tool-calling response in the window is judged with
the same ``judge()`` the live hook uses, against the responses before it
(transcript split into requests by serial_read_replay.requests). A denied
call is marked denied for the rest of the request, as the hook would see its
tool_result. The replay cannot know what the model would have done instead,
so later responses are the recorded ones.

Each denied response is classified for the table:

- ``exact``: the denied call's normalized input equals the previous
  matching call's -- the same command again.
- ``fuzzy``: near-identical only (ratio >= similarity) -- a variation.
- ``fail``: of those, every earlier matching call in the run ended in a
  tool error -- the same failing call retried.
- ``poll``: a Bash command with sleep, tail, or a background output read,
  or a TaskOutput/BashOutput call -- waiting turn by turn.

``--samples`` writes every deny with the run's inputs and result heads so
they can be read by hand.
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from serial_read_replay import requests

from slack_cli_agent.guard.pretool.repeat_guard import judge, normalize, similar
from slack_cli_agent.guard.pretool.serial_read_guard import Response, Use

POLL = re.compile(r"\bsleep\b|\btail\b|\.output\b")
POLL_TOOLS = {"TaskOutput", "BashOutput"}


def replay(
    reqs: list[dict[str, Any]], run_needed: int, threshold: float, samples: list[str] | None = None,
) -> Counter[str]:
    c: Counter[str] = Counter()
    for req in reqs:
        c["requests"] += 1
        seen: list[Response] = []
        req_denied = False
        for mid, uses in req["responses"]:
            c["responses"] += 1
            c["calls"] += len(uses)
            response = Response(mid, [Use(i, n, inp) for i, n, inp, _ in uses])
            denied_here: list[tuple[Use, int]] = []
            for use in response.uses:
                verdict, run = judge([*seen, response], use.tool_use_id, use.name, use.tool_input,
                                     run_needed, threshold)
                if verdict == "deny":
                    denied_here.append((use, run))
            if denied_here:
                c["deny_responses"] += 1
                c["deny_calls"] += len(denied_here)
                req_denied = True
                use, run = denied_here[0]
                matched = [
                    next(u for u in r.uses if similar(u.name, u.tool_input, use.name, use.tool_input, threshold))
                    for r in seen[-run:]
                ]
                if normalize(matched[-1].name, matched[-1].tool_input) == normalize(use.name, use.tool_input):
                    c["exact"] += 1
                else:
                    c["fuzzy"] += 1
                if all(m.tool_use_id in req["errors"] for m in matched):
                    c["fail"] += 1
                command = str(use.tool_input.get("command", "")) if use.name == "Bash" else ""
                if use.name in POLL_TOOLS or POLL.search(command):
                    c["poll"] += 1
                if samples is not None:
                    lines = [f"### {len(samples) + 1}. {os.path.basename(req['path'])} run={run} tool={use.name}",
                             f"- 요청: {req['prompt'][:120]!r}"]
                    for m in [*matched, use]:
                        tag = "거부" if m is use else ("오류" if m.tool_use_id in req["errors"] else "결과")
                        res = req["results"].get(m.tool_use_id, "")[:160]
                        lines.append(f"- 입력: {normalize(m.name, m.tool_input)[:220]!r}")
                        lines.append(f"  - {tag}: {res!r}")
                    samples.append("\n".join(lines))
                for u, _ in denied_here:
                    u.denied = True
            seen.append(response)
        if req_denied:
            c["deny_requests"] += 1
    return c


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pattern")
    ap.add_argument("--since", required=True)
    ap.add_argument("--until", required=True)
    ap.add_argument("--runs", default="3")
    ap.add_argument("--similarity", default="0.9")
    ap.add_argument("--samples", help="write the denies of the first run/similarity pair here")
    args = ap.parse_args()
    since = dt.datetime.fromisoformat(args.since).timestamp()
    until = dt.datetime.fromisoformat(args.until).timestamp()
    files = [f for f in glob.glob(os.path.expanduser(args.pattern)) if os.path.getmtime(f) >= since]
    reqs = [r for f in files for r in requests(f, since, until)]
    print(f"files={len(files)} requests={len(reqs)}")
    print("| N | 유사도 | 요청 | 도구 응답 | 호출 | deny 응답 | deny 요청 "
          "| 정확히 같음 | 변형 | 같은 실패 | 대기 폴링 |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    first = True
    for n in [int(x) for x in args.runs.split(",")]:
        for sim in [float(x) for x in args.similarity.split(",")]:
            samples: list[str] | None = [] if (first and args.samples) else None
            c = replay(reqs, n, sim, samples)
            print(f"| {n} | {sim:g} | {c['requests']} | {c['responses']} | {c['calls']} | {c['deny_responses']} | "
                  f"{c['deny_requests']} | {c['exact']} | {c['fuzzy']} | {c['fail']} | {c['poll']} |")
            if samples is not None:
                Path(args.samples).write_text(
                    f"# repeat_guard 재생 거부 표본 (N={n}, 유사도={sim:g})\n\n" + "\n\n".join(samples) + "\n",
                    encoding="utf-8",
                )
            first = False
    return 0


if __name__ == "__main__":
    sys.exit(main())
