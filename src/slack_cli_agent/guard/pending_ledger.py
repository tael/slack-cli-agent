"""미완 항목 장부. 답이 남긴 일을 세션별로 적어 두고 다음 턴에 되돌려 준다.

2026-11-26 shinji: an owner turn ended on "남은 일 3건임" or "정해 주면 진행함" and
every follow-up push moved it exactly one step. Rules in the persona did not
change that, so the leftover is carried in state instead: the Stop hook writes
what the allowed answer left open, the UserPromptSubmit hook (plan_start) puts
it in front of the next turn, and finish_review.ledger_unworked blocks a next
turn that ends without touching it.

Keyed by the engine session id, which a resumed thread keeps. Standard
library only: plan_start imports this on every prompt.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import time
from collections.abc import Mapping
from pathlib import Path

TTL_SEC = 24 * 3600
MAX_ITEMS = 8
MAX_ITEM_CHARS = 160

_QUOTED = re.compile(r'"[^"\n]*"|“[^”\n]*”|\'[^\'\n]*\'|`[^`\n]*`')
_ASK_LINE_RE = re.compile(r"^[ \t]*\[\[ASK\]\][ \t]*$", re.MULTILINE)
_HANDED_RE = re.compile(
    r"(?:정해|결정해|골라|선택해|허용해|승인해|알려)\s?(?:주면|주시면|준다면)[^\n.]{0,30}?"
    r"(?:진행|처리|실행|이어|하겠|할게|함\b|함\.)"
)
_LEFT_RE = re.compile(r"남은\s?(?:일|작업|항목|단계)")
#: A "남은 일" that is reported as done or absent is not left open.
_LEFT_DONE_RE = re.compile(r"없|처리했|처리함|마쳤|끝냈|끝났|완료|해결했|0\s?[건개]")
_NOT_STARTED_RE = re.compile(
    r"아직\s?(?:시작하지\s?않|안\s?한|하지\s?않은|안\s?돌|못\s?한|손대지\s?않)"
)
_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")


def left_hit(text: str) -> str | None:
    """The phrase, if `text` leaves work open or hands a reversible decision back."""
    body = _QUOTED.sub("", text or "")
    match = _HANDED_RE.search(body) or _NOT_STARTED_RE.search(body)
    if match:
        return match.group(0)
    for match in _LEFT_RE.finditer(body):
        end = len(body)
        for stop in (body.find("\n", match.end()), body.find(". ", match.end())):
            if stop != -1:
                end = min(end, stop)
        window = body[match.start():min(end, match.end() + 60)]
        if not _LEFT_DONE_RE.search(window):
            return window.strip()
    return None


def has_ask(text: str) -> bool:
    return bool(_ASK_LINE_RE.search(text or ""))


def extract(answer: str) -> tuple[str, ...]:
    """The lines that left work open, each followed by the bullets under it."""
    lines = (answer or "").splitlines()
    items: list[str] = []
    i = 0
    while i < len(lines) and len(items) < MAX_ITEMS:
        line = lines[i].strip()
        if line and left_hit(line):
            items.append(line[:MAX_ITEM_CHARS])
            j = i + 1
            while j < len(lines) and len(items) < MAX_ITEMS:
                nxt = lines[j]
                if not nxt.strip():
                    if j + 1 < len(lines) and _BULLET_RE.match(lines[j + 1]):
                        j += 1
                        continue
                    break
                if not _BULLET_RE.match(nxt):
                    break
                items.append(_BULLET_RE.sub("", nxt).strip()[:MAX_ITEM_CHARS])
                j += 1
            i = j
            continue
        i += 1
    return tuple(items)


def state_dir(payload: Mapping[str, object], env: Mapping[str, str] | None = None) -> Path | None:
    """Same place finish_hook keeps its per-turn counters."""
    env = os.environ if env is None else env
    if env.get("FINISH_HOOK_STATE_DIR"):
        return Path(env["FINISH_HOOK_STATE_DIR"])
    transcript = payload.get("transcript_path")
    if not transcript:
        return None
    try:
        return Path(str(transcript)).resolve().parent
    except OSError:
        return None


def ledger_path(directory: Path, session_id: object) -> Path:
    sid = re.sub(r"[^A-Za-z0-9_-]", "_", str(session_id or "nosession"))
    return directory / f"pending-ledger-{sid}.json"


def read(directory: Path | None, session_id: object, *, now: float | None = None) -> tuple[str, ...]:
    if directory is None:
        return ()
    try:
        data = json.loads(ledger_path(directory, session_id).read_text(encoding="utf-8"))
        written = float(data["t"])
        items = data["items"]
    except (OSError, ValueError, KeyError, TypeError):
        return ()
    if (time.time() if now is None else now) - written > TTL_SEC or not isinstance(items, list):
        return ()
    return tuple(str(x) for x in items)


def write(directory: Path | None, session_id: object, items: tuple[str, ...], *, now: float | None = None) -> None:
    if directory is None:
        return
    path = ledger_path(directory, session_id)
    with contextlib.suppress(OSError):
        if not items:
            path.unlink(missing_ok=True)
            return
        path.write_text(
            json.dumps({"t": time.time() if now is None else now, "items": list(items)}, ensure_ascii=False),
            encoding="utf-8",
        )


def context_text(items: tuple[str, ...]) -> str:
    if not items:
        return ""
    body = "\n".join(f"- {item}" for item in items)
    return (
        f"[미완 항목 {len(items)}건] 직전 답에 남긴 일이다. 소유자가 다른 지시를 하지 않았으면 이번 턴에 "
        "먼저 처리하고 결과로 답한다. 삭제, 외부 발송, 비용, 운영 반영처럼 승인이 필요한 것만 이유와 함께 "
        "남기고 [[ASK]] 를 붙인다. 소유자가 중단이나 보류를 지시했으면 그 사실을 한 줄로 적는다.\n" + body
    )
