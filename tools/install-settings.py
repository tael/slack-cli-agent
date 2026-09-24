#!/usr/bin/env python3
"""봇 상태 디렉터리에 엔진 settings 본보기를 깐다.

    tools/install-settings.py <봇이름> [--state-dir 경로] [--engine claude]

settings 는 운영물이라 저장소에 없다. 없으면 엔진이 빈 조각으로 돌아
permissions.deny 가 하나도 안 걸린다. 그래서 설치물 쪽에 본보기를 두고
(src/slack_cli_agent/assets/engine/) 이 명령이 상태 디렉터리로 옮긴다.

이미 있는 파일은 건드리지 않는다. 설치물 갱신이 운영물을 덮으면 안 된다.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ASSET_DIR = REPO / "src" / "slack_cli_agent" / "assets" / "engine"
PLACEHOLDER = "__STATE__"

sys.path.insert(0, str(REPO / "src"))

from slack_cli_agent.engine.claude_settings import load_settings_file  # noqa: E402

#: 본보기 파일 이름. 공통 파일과 general 덧씌움 둘뿐이다. owner·trusted 는
#: 공통 파일 내용이 그대로 걸리므로 덧씌움을 두지 않는다.
ASSET_NAMES = ("settings-claude.json", "settings-claude.general.json")


def permission_path(state_dir: Path, home: Path) -> str:
    """claude 권한 규칙이 쓰는 경로 표기. 홈 아래면 ~, 밖이면 // 절대경로."""
    try:
        relative = state_dir.relative_to(home)
    except ValueError:
        return "//" + str(state_dir).lstrip("/")
    return "~/" + relative.as_posix()


def render(source: Path, state_dir: Path, home: Path) -> str:
    return source.read_text(encoding="utf-8").replace(
        PLACEHOLDER, permission_path(state_dir, home)
    )


def install(
    state_dir: Path,
    engine: str = "claude",
    home: Path | None = None,
    asset_dir: Path | None = None,
) -> tuple[list[Path], list[Path]]:
    """만든 파일과 이미 있어 건너뛴 파일."""
    if engine != "claude":
        raise SystemExit(f"{engine} 엔진에는 settings 본보기가 없다")
    home = home or Path.home()
    asset_dir = asset_dir or ASSET_DIR
    target_dir = state_dir / "engine"
    target_dir.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    skipped: list[Path] = []
    for name in ASSET_NAMES:
        target = target_dir / name
        if target.exists():
            skipped.append(target)
            continue
        target.write_text(render(asset_dir / name, state_dir, home), encoding="utf-8")
        # Shape check on what was actually written, not on the asset: a bad
        # substitution would otherwise only show up as a dead deny list.
        load_settings_file(target)
        created.append(target)
    return created, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="엔진 settings 본보기를 상태 디렉터리에 깐다")
    parser.add_argument("name", help="봇 이름")
    parser.add_argument("--state-dir", default=None, help="상태 디렉터리. 기본은 ~/.<봇이름>")
    parser.add_argument("--engine", default="claude", help="엔진 종류. 지금은 claude 뿐")
    args = parser.parse_args(argv)
    state_dir = (
        Path(args.state_dir).expanduser() if args.state_dir else Path.home() / f".{args.name}"
    )
    created, skipped = install(state_dir, args.engine)
    for path in created:
        print(f"만들었다 : {path}")
    for path in skipped:
        print(f"이미 있어 건너뛴다 : {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
