#!/usr/bin/env python3
"""테스트 채널에 멘션을 넣고 그 봇이 어떻게 답했는지 읽는다.

    tools/test-channel-probe.py <봇이름> <본문>

세션이 사람 없이 멘션-응답 경로 전체를 재 볼 수단이다(sca-55x). 등록 당시
전제는 "사람 자격이 필요하다" 였으나 2026-09-19 실측으로 틀린 것이 드러났다 -
bot_id 를 거르는 것은 MessageKind.is_human 이고 그 경로는 from_message 다.
멘션은 from_app_mention 이 받고 그쪽은 안 거른다. 그래서 다른 봇의 토큰으로
멘션을 넣으면 대상 봇이 깨어난다. 데스크톱 토큰 추출이 필요 없다.

대상 채널은 프로필의 troubleshoot_channel 이다. 인자로 받지 않는 것이 이
도구의 안전장치다 - 채널을 골라 받으면 운영 채널로 잘못 나간다.
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from slack_cli_agent.guard.watch import WATCH_MARK_EMOJI  # noqa: E402
from slack_cli_agent.slack.app_registry import read_bot_token  # noqa: E402
from slack_cli_agent.slack.reactions import SILENT_MARK_EMOJI  # noqa: E402

API = "https://slack.com/api/"

DONE = "완료"
SILENT = "답할 것 없음"
FAILED = "실패"
WATCHING = "감시로 넘어감"
RUNNING = "처리 중"
WAITING = "차례 기다리는 중"
NO_REACTION = "반응 없음"

#: 표식에서 상태로. 앞선 것이 이긴다 - 최종 표식은 하나만 남는 것이 정상이라
#: 둘이 함께 보이면 이상 신호이므로 나쁜 쪽을 쓴다.
OUTCOMES: tuple[tuple[str, str], ...] = (
    ("x", FAILED),
    ("white_check_mark", DONE),
    (SILENT_MARK_EMOJI, SILENT),
    (WATCH_MARK_EMOJI, WATCHING),
    ("hourglass", WAITING),
    ("eyes", RUNNING),
)

#: 원인이 다르면 종료코드도 다르다. 전부 1로 내면 자동화가 봇 실패와
#: 멘션 미도달과 시간 초과를 구분하지 못한다.
EXIT_CODES = {DONE: 0, SILENT: 0, FAILED: 1, NO_REACTION: 3}
UNFINISHED_EXIT = 4

#: 이 상태에서는 더 기다리면 바뀐다. 나머지는 기다려도 그대로다.
PENDING = (NO_REACTION, RUNNING, WAITING)

NAME_PATTERN = re.compile(r"\A[a-z0-9_-]+\Z")
POLL_SEC = 5
DEFAULT_WAIT_SEC = 600


def check_name(name: str) -> str:
    """봇 이름이 그대로 프로필 경로에 붙는다. 검증이 없으면 프로필 밖 JSON 을
    읽혀 임의 채널로 보낼 수 있고, 채널을 인자로 안 받는 안전장치가 무의미해진다."""
    if not NAME_PATTERN.match(name):
        raise SystemExit(f"봇 이름이 아니다 : {name!r}")
    return name


def target_channel(profile: Mapping[str, Any]) -> str:
    channel = str(profile.get("troubleshoot_channel") or "")
    if not channel:
        raise SystemExit("프로필에 troubleshoot_channel 이 없다. 어디로 보낼지 정할 수 없다")
    return channel


def mention_text(bot_user_id: str, text: str) -> str:
    return f"<@{bot_user_id}> {text}"


def outcome_of(reactions: list[str]) -> str:
    for name, state in OUTCOMES:
        if name in reactions:
            return state
    return NO_REACTION


def exit_code(outcome: str) -> int:
    return EXIT_CODES.get(outcome, UNFINISHED_EXIT)


def _post(method: str, payload: dict[str, Any], token: str) -> dict[str, Any]:
    body = urllib.parse.urlencode({k: str(v) for k, v in payload.items()}).encode()
    req = urllib.request.Request(API + method, data=body)
    req.add_header("Content-type", "application/x-www-form-urlencoded; charset=utf-8")
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as res:
        result: dict[str, Any] = json.loads(res.read().decode())
    if not result.get("ok"):
        raise SystemExit(f"{method} 실패 : {result.get('error')}")
    return result


def _get(url: str, token: str, cookie: str = "") -> dict[str, Any]:
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    if cookie:
        req.add_header("Cookie", f"d={cookie}")
    with urllib.request.urlopen(req, timeout=30) as res:
        data: dict[str, Any] = json.loads(res.read().decode())
    return data


def thread_state(channel: str, ts: str, token: str, cookie: str = "") -> tuple[str, list[str]]:
    """멘션에 붙은 표식과 봇이 단 답. 조회가 실패하면 멈춘다 - 권한이나 rate
    limit 오류를 표식 없음으로 읽으면 원인이 안 남은 채 시간 초과까지 기다린다."""
    query = urllib.parse.urlencode({"channel": channel, "ts": ts, "limit": 50})
    data = _get(f"{API}conversations.replies?{query}", token, cookie)
    if not data.get("ok"):
        raise SystemExit(f"conversations.replies 실패 : {data.get('error')}")
    messages = data.get("messages") or [{}]
    reactions = [str(r.get("name")) for r in messages[0].get("reactions") or []]
    replies = [str(m.get("text") or "") for m in messages[1:] if m.get("bot_id")]
    return outcome_of(reactions), replies


def _token_of(name: str) -> str:
    token = read_bot_token(Path.home() / f".{name}")
    if not token:
        raise SystemExit(f"{name} 의 봇 토큰을 찾지 못했다")
    return token


def _poster_token(target: str) -> str:
    """대상이 아닌 다른 봇의 토큰. 자기 자신을 멘션하면 자기 대화가 된다."""
    for name in ("shinji", "rei", "asuka"):
        if name == target:
            continue
        try:
            return _token_of(name)
        except SystemExit:
            continue
    raise SystemExit(f"{target} 말고 멘션을 넣어 줄 봇이 없다")


def main(argv: list[str]) -> None:
    if len(argv) < 3:
        print(__doc__)
        raise SystemExit(2)
    name, text = check_name(argv[1]), argv[2]
    profile = json.loads((REPO / "profiles" / f"{name}.json").read_text(encoding="utf-8"))
    channel = target_channel(profile)

    bot_user_id = str(_post("auth.test", {}, _token_of(name))["user_id"])
    poster = _poster_token(name)
    ts = str(_post("chat.postMessage", {"channel": channel, "text": mention_text(bot_user_id, text)}, poster)["ts"])
    print(f"게시 : {channel} {ts}")

    deadline = time.monotonic() + DEFAULT_WAIT_SEC
    outcome, replies = thread_state(channel, ts, poster)
    while outcome in PENDING and time.monotonic() < deadline:
        time.sleep(POLL_SEC)
        outcome, replies = thread_state(channel, ts, poster)

    print(f"결과 : {outcome}")
    for reply in replies:
        print(f"--- {reply}")
    raise SystemExit(exit_code(outcome))


if __name__ == "__main__":
    main(sys.argv)
