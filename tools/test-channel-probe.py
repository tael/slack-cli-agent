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
import urllib.error
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

#: 슬랙에 못 닿은 것은 봇 실패가 아니다. 2026-09-19 20:00 점검에서 HTTP 500
#: 하나가 종료코드 1 로 나가 봇이 답을 못 낸 것과 같아 보였다 (sca-oaty).
TRANSPORT_EXIT = 5

#: 다시 해 보면 달라지는 것만 고른다. 서버 오류와 rate limit, 그리고 연결
#: 자체가 안 된 것이다. 4xx 는 다시 해도 같아 원인만 늦게 드러난다.
RETRY_STATUS = (429, 500, 502, 503, 504)
RETRY_ATTEMPTS = 3
RETRY_DELAY_SEC = 3

#: 슬랙은 internal_error 에서 일부 작업이 이미 성공했을 수 있다고 적는다.
#: 게시를 다시 하면 같은 멘션이 두 번 나가고 봇이 둘 다 처리한다. 한 번만
#: 보내고 못 닿았으면 그대로 끝낸다 - 다시 재는 것은 사람이 하면 된다.
WRITE_METHODS = ("chat.postMessage", "chat.update", "chat.delete", "reactions.add")

#: 이 상태에서는 더 기다리면 바뀐다. 나머지는 기다려도 그대로다.
PENDING = (NO_REACTION, RUNNING, WAITING)

NAME_PATTERN = re.compile(r"\A[a-z0-9_-]+\Z")
POLL_SEC = 5
DEFAULT_WAIT_SEC = 600


class TransportFailed(Exception):
    """슬랙에 못 닿았다. 봇이 답을 못 낸 것과 고칠 자리가 다르다."""


def is_transient(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in RETRY_STATUS
    return isinstance(exc, urllib.error.URLError)


def wait_sec(exc: Exception, default: float) -> float:
    """429 는 슬랙이 얼마나 기다리라고 알려 준다. 그 값을 무시하면 429 가
    계속 돌아온다."""
    headers = getattr(exc, "headers", None)
    raw = headers.get("Retry-After") if headers else None
    try:
        return float(raw) if raw else default
    except (TypeError, ValueError):
        return default


def call_with_retry(
    call: Any,
    attempts: int = RETRY_ATTEMPTS,
    delay: float = RETRY_DELAY_SEC,
    sleep: Any = time.sleep,
) -> Any:
    """일시 오류면 다시 해 보고, 한도를 넘기면 마지막 오류를 담아 올린다."""
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except Exception as exc:
            if not is_transient(exc):
                raise
            last = exc
            print(f"슬랙 호출 실패 {attempt}/{attempts} : {exc}", file=sys.stderr)
            if attempt < attempts:
                sleep(wait_sec(exc, delay))
    raise TransportFailed(f"슬랙에 {attempts}번 못 닿았다 : {last}")


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

    def once() -> dict[str, Any]:
        with urllib.request.urlopen(req, timeout=30) as res:
            loaded: dict[str, Any] = json.loads(res.read().decode())
        return loaded

    # 쓰기는 한 번만. 다시 하면 같은 멘션이 두 번 나갈 수 있다.
    attempts = 1 if method in WRITE_METHODS else RETRY_ATTEMPTS
    result: dict[str, Any] = call_with_retry(once, attempts=attempts)
    if not result.get("ok"):
        raise SystemExit(f"{method} 실패 : {result.get('error')}")
    return result


def _get(url: str, token: str, cookie: str = "") -> dict[str, Any]:
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    if cookie:
        req.add_header("Cookie", f"d={cookie}")

    def once() -> dict[str, Any]:
        with urllib.request.urlopen(req, timeout=30) as res:
            loaded: dict[str, Any] = json.loads(res.read().decode())
        return loaded

    data: dict[str, Any] = call_with_retry(once)
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
    replies = [
        (str(m.get("ts") or ""), str(m.get("text") or ""))
        for m in messages[1:] if m.get("bot_id")
    ]
    return outcome_of(reactions), replies


#: 지우고 끝내도 되는 결과. 그 밖은 사람이 원인을 봐야 하므로 남긴다.
CLEARABLE = (DONE, SILENT)


def clear_probe(
    channel: str, ts: str, reply_ts: list[str], outcome: str, poster: str, target: str,
) -> None:
    """점검이 남긴 멘션과 답을 지운다.

    점검이 잦아 테스트 채널이 같은 질문으로 찼다(사용자 지시 2026-09-20).
    멘션-응답 경로를 실제로 재는 것은 그대로 두고 그 자리에 쌓이는 것만 없앤다.
    봇 토큰은 자기가 올린 글만 지울 수 있어 답과 멘션의 토큰이 다르다.
    답을 먼저 지운다 - 멘션이 먼저 사라지면 스레드가 끊겨 답이 채널에 남는다.
    """
    if outcome not in CLEARABLE:
        return
    for reply in reply_ts:
        _clear_one(channel, reply, target)
    _clear_one(channel, ts, poster)


def _clear_one(channel: str, ts: str, token: str) -> None:
    """정리는 곁다리다. 못 지운 것이 점검 판정을 뒤집으면 안 된다."""
    try:
        _post("chat.delete", {"channel": channel, "ts": ts}, token)
    # _post 는 슬랙 오류를 SystemExit 로 낸다. Exception 만 잡으면 그것이
    # 그대로 올라가 점검 종료코드를 덮는다.
    except (SystemExit, Exception) as exc:  # noqa: BLE001 - 점검은 이미 끝났다
        print(f"점검 흔적을 못 지웠다 : {channel} {ts} : {exc}", file=sys.stderr)


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
    for _reply_ts, text in replies:
        print(f"--- {text}")
    clear_probe(channel, ts, [t for t, _ in replies], outcome, poster, _token_of(name))
    raise SystemExit(exit_code(outcome))


def run(argv: list[str]) -> None:
    """못 닿은 것을 봇 실패와 가르는 자리. main 을 직접 부르면 TransportFailed
    가 그대로 올라가 종료코드 1 이 된다."""
    try:
        main(argv)
    except TransportFailed as exc:
        print(f"점검 도구가 슬랙에 못 닿았다 : {exc}", file=sys.stderr)
        raise SystemExit(TRANSPORT_EXIT) from exc


if __name__ == "__main__":
    run(sys.argv)
