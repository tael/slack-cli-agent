"""Baseline metrics of how bot sessions orchestrate tool calls and worker agents.

Usage:
    python tools/orchestration_replay.py \
        --glob '~/.claude/projects/-Users-Shared-guchipachi-work*/*.jsonl' \
        --since 2026-11-06

Reads Claude Code transcripts offline (stdlib only, nothing is written). One
transcript file is one session. Parsing rules:

- One assistant response is one ``message.id``. Claude Code writes every
  content block of a response as its own line, so tool_use blocks are grouped
  by ``message.id``.
- Tool input booleans can arrive as strings. ``run_in_background`` counts as
  true only for ``True`` or the string ``"true"``.
- An Agent/Task call whose ``input.model`` contains ``fable`` is an advisor
  call; every other Agent/Task call is a worker call.
- A session is heavy when it has at least ``HEAVY`` tool-calling responses.

Heavy sessions are labelled by a heuristic (first human prompt plus the
distinct data sources the session touched):

- ``갈래형``: two or more independent branches (parallel-able lookups)
- ``탐색형``: exploratory, the next lookup depends on the previous result
- ``단답형``: one or two lookups are enough

The label comes with the evidence it was based on, so a person can overrule it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re
import shlex
import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

HEAVY = 30
KST = dt.timezone(dt.timedelta(hours=9))
DENY_MARK = "[연속 단건 조회]"
HISTORY_END = "여기까지가 지난 대화다 ====="
PERSON_TURN = re.compile(r"\[\d\d:\d\d:\d\d 김태일")
PLAN_MARK = "[계획]"
PLAN_UPDATE_MARK = "[계획 갱신]"
AGENT_TOOLS = {"Agent", "Task"}

#: Prompt cues for several independent targets in one request.
BRANCH_CUE = re.compile(
    r"각각|각 |모두|전부|동시에|나란히|비교|대조|교차|양쪽|둘 ?다|세 가지|세 곳|세 개|여러|별\b|"
    r"\d(?:\s*,\s*\d)+|또\.|그리고|"
    r"\b[1-9][).]\s|①|②|첫째|둘째|\b\d+\s*(?:개|곳|건)\b"
)
#: Prompt cues for "something is wrong, find out what".
EXPLORE_CUE = re.compile(
    r"왜|원인|이유|안 ?[돼되됨]|않[는은아]|실패|오류|에러|장애|멈|느리|이상|문제|조사|추적|파악|"
    r"어디|무엇|뭐가|알아봐|찾아"
)


@dataclass
class Session:
    path: str
    session_id: str
    started: float = 0.0
    prompt: str = ""
    responses: list[list[tuple[str, dict[str, Any]]]] = field(default_factory=list)
    plan_marks: int = 0
    plan_update_marks: int = 0
    denials: int = 0

    @property
    def calls(self) -> int:
        return sum(len(r) for r in self.responses)

    @property
    def multi(self) -> int:
        return sum(1 for r in self.responses if len(r) >= 2)

    @property
    def heavy(self) -> bool:
        return len(self.responses) >= HEAVY

    def agents(self) -> list[dict[str, Any]]:
        return [i for r in self.responses for n, i in r if n in AGENT_TOOLS]

    @property
    def advisors(self) -> int:
        return sum(1 for i in self.agents() if is_advisor(i))

    @property
    def workers(self) -> int:
        return sum(1 for i in self.agents() if not is_advisor(i))

    @property
    def background_workers(self) -> int:
        return sum(1 for i in self.agents() if not is_advisor(i) and is_true(i.get("run_in_background")))

    @property
    def multi_worker_responses(self) -> int:
        return sum(1 for r in self.responses if sum(1 for n, i in r if n in AGENT_TOOLS and not is_advisor(i)) >= 2)


def is_advisor(tool_input: dict[str, Any]) -> bool:
    return "fable" in str(tool_input.get("model", "")).lower()


def is_true(value: Any) -> bool:
    """Booleans in tool input may be strings; only ``True`` and ``"true"`` count."""
    return value is True or value == "true"


def _is_human_turn(content: object) -> bool:
    if isinstance(content, str):
        return True
    return isinstance(content, list) and not any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    )


def _text_of(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _result_text(block: dict[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(b.get("text", "")) for b in content if isinstance(b, dict))
    return ""


def parse_session(path: str) -> Session | None:
    """Parse one transcript. Returns None when it has no timestamped lines."""
    session = Session(path=path, session_id=os.path.basename(path).removesuffix(".jsonl"))
    last_id: str | None = None
    seen_text: set[tuple[str, str]] = set()
    with open(path, encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict) or rec.get("isSidechain"):
                continue
            kind = rec.get("type")
            if kind not in ("user", "assistant"):
                continue
            raw_ts = rec.get("timestamp")
            if raw_ts and not session.started:
                session.started = dt.datetime.fromisoformat(raw_ts).timestamp()
            msg = rec.get("message") or {}
            content = msg.get("content") if isinstance(msg, dict) else None
            if kind == "user":
                if _is_human_turn(content):
                    if not session.prompt:
                        session.prompt = _text_of(content)
                    continue
                for block in content or []:
                    if (isinstance(block, dict) and block.get("type") == "tool_result"
                            and DENY_MARK in _result_text(block)):
                        session.denials += 1
                continue
            if not isinstance(content, list):
                continue
            mid = str(msg.get("id") or rec.get("uuid"))
            uses = [(str(b.get("name", "")), b.get("input") or {}) for b in content
                    if isinstance(b, dict) and b.get("type") == "tool_use"]
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = str(block.get("text", ""))
                    if (mid, text) in seen_text:
                        continue
                    seen_text.add((mid, text))
                    session.plan_marks += text.count(PLAN_MARK)
                    session.plan_update_marks += text.count(PLAN_UPDATE_MARK)
            if not uses:
                continue
            if session.responses and mid == last_id:
                session.responses[-1].extend(uses)
            else:
                session.responses.append(uses)
            last_id = mid
    return session if session.started else None


def human_request(prompt: str) -> str:
    """The person's request inside a bot wrapper: the text after the replayed
    conversation, minus bracketed control lines."""
    prompt = prompt.rsplit(HISTORY_END, 1)[-1]
    asked = list(PERSON_TURN.finditer(prompt))
    if asked:
        prompt = prompt[asked[-1].start():]
    kept = [ln for ln in prompt.splitlines() if ln.strip() and not ln.lstrip().startswith("[이 ")
            and not ln.lstrip().startswith("[위임받은") and "RECIPIENT" not in ln]
    return " ".join(kept)[:600]


def bash_family(command: str) -> str:
    """First real command word of a Bash call: ``cd x && gh pr`` -> ``gh``."""
    for part in re.split(r"&&|\|\||;|\|", command):
        try:
            words = shlex.split(part)
        except ValueError:
            words = part.split()
        words = [w for w in words if "=" not in w.split("/")[0] or w.startswith("-")]
        if words and words[0] not in ("cd", "export", "set", "echo", "true"):
            return os.path.basename(words[0])
    return "bash"


def sources(session: Session) -> dict[str, Counter[str]]:
    """Distinct data sources touched: MCP servers, file roots, Bash families."""
    found: dict[str, Counter[str]] = {"mcp": Counter(), "file": Counter(), "bash": Counter()}
    for response in session.responses:
        for name, tool_input in response:
            if name.startswith("mcp__"):
                found["mcp"][name.split("__")[1]] += 1
            elif name == "Bash":
                found["bash"][bash_family(str(tool_input.get("command", "")))] += 1
            elif name in ("Read", "Grep", "Glob", "Edit", "Write"):
                p = str(tool_input.get("file_path") or tool_input.get("path") or "")
                found["file"]["/".join(p.split("/")[:5])] += 1
    return found


def classify(session: Session) -> tuple[str, str]:
    """Label a session and say why. Heuristic, meant to be hand-checked."""
    request = human_request(session.prompt)
    src = sources(session)
    mcp, files, bash = len(src["mcp"]), len(src["file"]), len(src["bash"])
    branch = bool(BRANCH_CUE.search(request))
    explore = bool(EXPLORE_CUE.search(request))
    evidence = f"mcp={mcp} file_roots={files} bash_families={bash} branch_cue={int(branch)} explore_cue={int(explore)}"
    if len(session.responses) < 4:
        return "단답형", evidence
    spread = mcp + bash + (1 if files >= 2 else 0)
    if branch and spread >= 3 and not explore:
        return "갈래형", evidence
    if branch and spread >= 4:
        return "갈래형", evidence
    return "탐색형", evidence


def load(pattern: str, since: float) -> list[Session]:
    out: list[Session] = []
    for path in sorted(glob.glob(os.path.expanduser(pattern))):
        session = parse_session(path)
        if session and session.started >= since:
            out.append(session)
    return out


def aggregate(sessions: list[Session]) -> dict[str, Any]:
    tool_responses = sum(len(s.responses) for s in sessions)
    heavy = [s for s in sessions if s.heavy]
    return {
        "sessions": len(sessions),
        "tool_responses": tool_responses,
        "tool_calls": sum(s.calls for s in sessions),
        "multi_call_responses": sum(s.multi for s in sessions),
        "multi_share": sum(s.multi for s in sessions) / tool_responses if tool_responses else 0.0,
        "calls_per_response": sum(s.calls for s in sessions) / tool_responses if tool_responses else 0.0,
        "workers": sum(s.workers for s in sessions),
        "multi_worker_responses": sum(s.multi_worker_responses for s in sessions),
        "background_workers": sum(s.background_workers for s in sessions),
        "advisors": sum(s.advisors for s in sessions),
        "heavy_sessions": len(heavy),
        "heavy_with_worker": sum(1 for s in heavy if s.workers),
        "plan_marks": sum(s.plan_marks for s in sessions),
        "plan_update_marks": sum(s.plan_update_marks for s in sessions),
        "denials": sum(s.denials for s in sessions),
    }


def row(s: Session) -> dict[str, Any]:
    label, evidence = classify(s)
    return {
        "session": s.session_id, "started": dt.datetime.fromtimestamp(s.started, KST).strftime("%m-%d %H:%M"),
        "responses": len(s.responses), "calls": s.calls, "multi": s.multi, "workers": s.workers,
        "multi_worker_responses": s.multi_worker_responses, "advisors": s.advisors,
        "heavy": s.heavy, "plan": s.plan_marks, "plan_update": s.plan_update_marks, "denials": s.denials,
        "label": label, "evidence": evidence, "prompt": human_request(s.prompt)[:120],
    }


def hand_label(manual: dict[str, str], session: str) -> str:
    return next((v for k, v in manual.items() if session.startswith(k)), "")


def table(rows: list[dict[str, Any]], manual: dict[str, str]) -> list[str]:
    out = ["| 세션 | 시작(KST) | 첫 요청 | 응답 | 호출 | 작업자 | 수동 판정 | 휴리스틱 | 일치 |",
           "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        hand = hand_label(manual, r["session"])
        agree = "" if not hand else ("O" if hand == r["label"] else "X")
        prompt = r["prompt"].replace("|", "/")[:60]
        out.append(f"| {r['session'][:8]} | {r['started']} | {prompt} | {r['responses']} | {r['calls']} | "
                   f"{r['workers']} | {hand} | {r['label']} | {agree} |")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--glob", default="~/.claude/projects/-Users-Shared-guchipachi-work*/*.jsonl")
    ap.add_argument("--since", default="2026-11-06", help="KST date or ISO time; sessions started before are skipped")
    ap.add_argument("--manual", help="JSON file {session id prefix: 갈래형|탐색형|단답형} from a hand check")
    ap.add_argument("--json", action="store_true", help="print machine-readable output")
    args = ap.parse_args()
    since_dt = dt.datetime.fromisoformat(args.since)
    if since_dt.tzinfo is None:
        since_dt = since_dt.replace(tzinfo=KST)
    manual: dict[str, str] = {}
    if args.manual:
        with open(os.path.expanduser(args.manual), encoding="utf-8") as handle:
            manual = json.load(handle)
    sessions = load(args.glob, since_dt.timestamp())
    total = aggregate(sessions)
    heavy_rows = sorted((row(s) for s in sessions if s.heavy), key=lambda r: r["started"])
    if args.json:
        json.dump({"aggregate": total, "heavy": heavy_rows}, sys.stdout, ensure_ascii=False, indent=1)
        print()
        return 0
    print(f"glob={args.glob} since={since_dt.isoformat()}")
    for key, value in total.items():
        print(f"{key}: {value:.3f}" if isinstance(value, float) else f"{key}: {value}")
    if manual:
        checked = [(r["label"], hand_label(manual, r["session"])) for r in heavy_rows]
        checked = [(a, b) for a, b in checked if b]
        branch = [r for r in heavy_rows if hand_label(manual, r["session"]) == "갈래형"]
        print(f"manual_checked: {len(checked)} agreed: {sum(1 for a, b in checked if a == b)}")
        print(f"manual_branch_sessions: {len(branch)} with_worker: {sum(1 for r in branch if r['workers'])}")
    print()
    print("\n".join(table(heavy_rows, manual)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
