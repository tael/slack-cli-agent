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
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
ASSET_DIR = REPO / "src" / "slack_cli_agent" / "assets" / "engine"
PLACEHOLDER = "__STATE__"

sys.path.insert(0, str(REPO / "src"))

from slack_cli_agent.engine.claude_settings import load_settings_file  # noqa: E402

#: 본보기 파일 이름. 공통 파일, 신뢰 수준별 덧씌움(general·trusted·owner),
#: operator_full 채널 전용 덧씌움까지 전부 깐다. owner·trusted·operator_full
#: 셋 다 지금은 빈 조각(`{}`)이거나 자격증명류만 더 막아 공통 파일의 Bash/
#: Edit/Write 비차단을 바꾸지 않는다 — 도구 전면 차단은 general 에만 있다.
#: operator_full 은 engine/claude.py 의 _settings_value 가 GENERAL 요청인데
#: 도구 이름에 Bash/Edit/Write/NotebookEdit 가 이미 있을 때만 general 대신
#: 골라 쓴다(그 이름은 auth/policy.py 의 channel_tools_for 가 채널의
#: operator_full 플래그를 봤을 때만 내준다 — 두 축이 한 플래그로 묶여 있다).
ASSET_NAMES = (
    "settings-claude.json",
    "settings-claude.general.json",
    "settings-claude.trusted.json",
    "settings-claude.owner.json",
    "settings-claude.operator_full.json",
)


def permission_path(state_dir: Path, home: Path) -> str:
    """claude 권한 규칙이 쓰는 경로 표기. 홈 아래면 ~, 밖이면 // 절대경로.

    Both sides get resolved first: a relative state dir would otherwise never
    match home and get written as a bogus //relative/path rule.
    """
    resolved = state_dir.expanduser().resolve()
    home = home.expanduser().resolve()
    try:
        relative = resolved.relative_to(home)
    except ValueError:
        return "//" + str(resolved).lstrip("/")
    return "~/" + relative.as_posix()


def substitute(node: Any, value: str) -> Any:
    if isinstance(node, str):
        return node.replace(PLACEHOLDER, value)
    if isinstance(node, list):
        return [substitute(item, value) for item in node]
    if isinstance(node, dict):
        return {substitute(key, value): substitute(item, value) for key, item in node.items()}
    return node


def render(source: Path, state_dir: Path, home: Path) -> str:
    """Substitution happens on parsed values, not on the raw text: a state
    path holding a quote, a backslash or a newline breaks the JSON otherwise."""
    document = json.loads(source.read_text(encoding="utf-8"))
    rendered = substitute(document, permission_path(state_dir, home))
    return json.dumps(rendered, ensure_ascii=False, indent=2) + "\n"


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


#: 본보기에서 뺀 deny 항목. 이미 깔린 운영물은 install() 이 건너뛰므로 본보기를
#: 고쳐도 도는 봇에는 반영되지 않는다. 운영물에만 있는 항목은 조직이 더한 것과
#: 구분이 안 되므로, 뺀 항목을 여기 적어 두고 check() 가 운영물에 남은 것을 보고한다.
#: 본보기에서 deny 를 뺄 때 이 표에 함께 적는다.
RETIRED: dict[str, dict[str, str]] = {
    "settings-claude.json": {
        # 2026-11-18 신지 운영물에 이 줄이 남아 소유자 턴이 자기 memory 와
        # 세션 기록을 못 읽었다. 본보기 수정 9일 뒤에 발견했다.
        "Read(~/.claude/**)": "2026-11-09 c6f8355f 소유자 밖 수준 덧씌움으로 옮김",
    },
}


def _covered(entry: str, installed: list[str]) -> bool:
    """`Read(~/.config/gh/**)` 는 운영물의 `Read(~/.config/**)` 가 이미 막는다."""
    if entry in installed:
        return True
    if "(" not in entry or not entry.endswith(")"):
        return False
    tool, _, pattern = entry[:-1].partition("(")
    for other in installed:
        if not other.startswith(tool + "(") or not other.endswith("/**)"):
            continue
        prefix = other[len(tool) + 1:-len("**)")]
        if pattern.startswith(prefix):
            return True
    return False


def check(
    state_dir: Path,
    home: Path | None = None,
    asset_dir: Path | None = None,
    retired: dict[str, dict[str, str]] | None = None,
) -> list[str]:
    """깔린 settings 와 지금 본보기의 deny 차이. 빈 목록이면 어긋남 없음.

    보고 대상은 본보기에 있는데 운영물에 없는 파일·deny 항목과, 본보기에서
    뺐는데 운영물에 남은 항목이다. 운영물에만 있는 그 밖의 항목은 조직이 더한
    것이라 보고하지 않는다.
    """
    home = home or Path.home()
    asset_dir = asset_dir or ASSET_DIR
    retired = RETIRED if retired is None else retired
    problems: list[str] = []
    for name in ASSET_NAMES:
        target = state_dir / "engine" / name
        if not target.exists():
            problems.append(f"{target} : 파일 없음")
            continue
        expected = json.loads(render(asset_dir / name, state_dir, home))
        installed = (load_settings_file(target).get("permissions") or {}).get("deny") or []
        for entry in (expected.get("permissions") or {}).get("deny") or []:
            if not _covered(entry, installed):
                problems.append(f"{target} : 본보기에 있는데 없음 {entry}")
        for entry, reason in retired.get(name, {}).items():
            if entry in installed:
                problems.append(f"{target} : 본보기에서 뺀 항목이 남음 {entry} ({reason})")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="엔진 settings 본보기를 상태 디렉터리에 깐다")
    parser.add_argument("name", help="봇 이름")
    parser.add_argument("--state-dir", default=None, help="상태 디렉터리. 기본은 ~/.<봇이름>")
    parser.add_argument("--engine", default="claude", help="엔진 종류. 지금은 claude 뿐")
    parser.add_argument(
        "--check", action="store_true",
        help="깔지 않고 본보기와 운영물의 deny 차이만 출력한다. 차이가 있으면 종료코드 1",
    )
    args = parser.parse_args(argv)
    state_dir = (
        Path(args.state_dir).expanduser() if args.state_dir else Path.home() / f".{args.name}"
    )
    if args.check:
        problems = check(state_dir)
        for line in problems:
            print(line)
        if not problems:
            print(f"어긋남 없음 : {state_dir / 'engine'}")
        return 1 if problems else 0
    created, skipped = install(state_dir, args.engine)
    for path in created:
        print(f"만들었다 : {path}")
    for path in skipped:
        print(f"이미 있어 건너뛴다 : {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
