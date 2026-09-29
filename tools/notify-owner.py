#!/usr/bin/env python3
"""그 봇의 토큰으로 소유자 DM 에 한 줄 보낸다. 봇 프로세스 밖에서 쓴다.

    tools/notify-owner.py <봇이름> <본문>

봇 본체의 소유자 알림 경로와 달리 재시도도 보류 저장도 없다. 발송 실패는
종료코드로만 알린다 - 이것을 부르는 쪽이 로그를 남긴다.
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from slack_cli_agent.slack.app_registry import read_bot_token

API = "https://slack.com/api/"


def call(method: str, data: dict[str, str], token: str) -> dict:
    req = urllib.request.Request(API + method, data=urllib.parse.urlencode(data).encode())
    req.add_header("Content-type", "application/x-www-form-urlencoded")
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as res:
        return json.loads(res.read().decode())


def main(argv: list[str]) -> None:
    if len(argv) < 3:
        print(__doc__)
        raise SystemExit(2)
    name, text = argv[1], argv[2]
    repo = Path(__file__).resolve().parents[1]
    profile = json.loads((repo / "profiles" / f"{name}.json").read_text(encoding="utf-8"))
    owner = profile.get("owner_user_id")
    if not owner:
        raise SystemExit(f"{name} 프로필에 owner_user_id 가 없다")
    token = read_bot_token(Path.home() / f".{name}")
    if not token:
        raise SystemExit(f"{name} 의 봇 토큰을 찾지 못했다")
    opened = call("conversations.open", {"users": owner}, token)
    if not opened.get("ok"):
        raise SystemExit(f"DM 방 열기 실패: {opened.get('error')}")
    channel = str((opened.get("channel") or {}).get("id", ""))
    sent = call("chat.postMessage", {"channel": channel, "text": text}, token)
    if not sent.get("ok"):
        raise SystemExit(f"발송 실패: {sent.get('error')}")
    print("보냈다")


if __name__ == "__main__":
    main(sys.argv)
