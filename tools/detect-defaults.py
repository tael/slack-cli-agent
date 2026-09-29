#!/usr/bin/env python3
"""새 봇을 만들 때 넣을 값의 후보를 지금 환경에서 찾는다.

워크스페이스·소유자·문제 채널은 조직 고유값이라 스크립트가 기본값으로 가지면
안 된다. 대신 이미 있는 것에서 후보를 찾아 보여주고, 사람이 고른 값을 명령에
실어 넘긴다. 추론한 값을 그대로 쓰지 않고 한 번 보여주는 것이 요점이다.

    tools/detect-defaults.py [프로필디렉터리]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

CONFIG_DIR = Path.home() / ".slack-app-config"
EXAMPLE_SUFFIX = ".example.json"


def workspaces(config_dir: Path) -> list[str]:
    """설정 토큰이 있는 워크스페이스. 앱을 만들 수 있는 곳이 여기뿐이다."""
    if not config_dir.is_dir():
        return []
    return sorted(p.stem for p in config_dir.glob("*.access"))


def from_profiles(profiles_dir: Path) -> dict[str, dict[str, list[str]]]:
    """이미 있는 봇이 쓰는 소유자와 문제 채널을 값별로 모은다."""
    owners: dict[str, list[str]] = {}
    channels: dict[str, list[str]] = {}
    if profiles_dir.is_dir():
        for path in sorted(profiles_dir.glob("*.json")):
            if path.name.endswith(EXAMPLE_SUFFIX):
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            for key, table in (("owner_user_id", owners), ("troubleshoot_channel", channels)):
                value = data.get(key)
                if isinstance(value, str) and value:
                    table.setdefault(value, []).append(path.stem)
    return {"owner_user_id": owners, "troubleshoot_channel": channels}


def _print_table(title: str, table: dict[str, list[str]]) -> None:
    print(f"\n{title}")
    if not table:
        print("  없다. 사람에게 물어야 한다")
        return
    for value, bots in sorted(table.items()):
        print(f"  {value}   쓰는 봇: {', '.join(bots)}")


def main(argv: list[str]) -> int:
    repo = Path(__file__).resolve().parent.parent
    profiles_dir = Path(argv[1]) if len(argv) > 1 else repo / "profiles"

    found = from_profiles(profiles_dir)
    names = workspaces(CONFIG_DIR)

    print("워크스페이스 (설정 토큰이 있는 곳)")
    if names:
        for name in names:
            print(f"  {name}")
    else:
        print(f"  없다. {CONFIG_DIR} 에 <이름>.access 와 .refresh 가 있어야 한다")
    _print_table("소유자 슬랙 ID", found["owner_user_id"])
    _print_table("문제 알림 채널", found["troubleshoot_channel"])

    print("\n고른 값을 이렇게 넘긴다:")
    print("  BOT_WORKSPACE=<워크스페이스> BOT_OWNER_USER_ID=<U...> \\")
    print("  BOT_TROUBLESHOOT_CHANNEL=<C...> tools/new-bot.sh <이름> <표시이름> <엔진> <모델>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
