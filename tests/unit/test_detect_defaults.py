"""tools/detect-defaults.py — 새 봇을 만들 때 넣을 값을 환경에서 추론한다.

워크스페이스·소유자·문제 채널은 조직 고유값이라 스크립트가 기본값으로
가지면 안 된다(2026-09-15 사용자 지시). 대신 지금 도는 환경에서 후보를
찾아 사람에게 보여주고, 고른 값을 명령에 실어 넘긴다.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "detect_defaults", Path(__file__).resolve().parents[2] / "tools" / "detect-defaults.py"
)
assert _SPEC and _SPEC.loader
detect_defaults = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(detect_defaults)


def write_profile(base: Path, name: str, owner: str, channel: str) -> None:
    base.mkdir(parents=True, exist_ok=True)
    (base / f"{name}.json").write_text(
        json.dumps(
            {
                "name": name,
                "display_name": name,
                "primary_engine": {"type": "claude", "binary": "claude", "model": "m"},
                "owner_user_id": owner,
                "troubleshoot_channel": channel,
                "state_dir": str(base / name),
            }
        ),
        encoding="utf-8",
    )


class Test워크스페이스:
    def test_설정_토큰이_있는_워크스페이스를_찾는다(self, tmp_path: Path) -> None:
        cfg = tmp_path / "slack-app-config"
        cfg.mkdir()
        (cfg / "example.access").write_text("x", encoding="utf-8")
        (cfg / "example.refresh").write_text("x", encoding="utf-8")
        (cfg / "other.access").write_text("x", encoding="utf-8")

        assert detect_defaults.workspaces(cfg) == ["example", "other"]

    def test_디렉터리가_없으면_빈_목록(self, tmp_path: Path) -> None:
        assert detect_defaults.workspaces(tmp_path / "없음") == []


class Test소유자와_채널:
    def test_이미_있는_봇에서_값을_모은다(self, tmp_path: Path) -> None:
        profiles = tmp_path / "profiles"
        write_profile(profiles, "asuka", "U1", "C1")
        write_profile(profiles, "rei", "U1", "C2")

        found = detect_defaults.from_profiles(profiles)

        assert found["owner_user_id"] == {"U1": ["asuka", "rei"]}
        assert found["troubleshoot_channel"] == {"C1": ["asuka"], "C2": ["rei"]}

    def test_예시_프로필은_안_센다(self, tmp_path: Path) -> None:
        profiles = tmp_path / "profiles"
        write_profile(profiles, "asuka", "U1", "C1")
        (profiles / "example.example.json").write_text(
            json.dumps({"name": "example", "owner_user_id": "U0", "troubleshoot_channel": "C0"}),
            encoding="utf-8",
        )

        found = detect_defaults.from_profiles(profiles)

        assert "U0" not in found["owner_user_id"]

    def test_깨진_프로필은_건너뛴다(self, tmp_path: Path) -> None:
        profiles = tmp_path / "profiles"
        write_profile(profiles, "asuka", "U1", "C1")
        (profiles / "broken.json").write_text("{ 깨짐", encoding="utf-8")

        found = detect_defaults.from_profiles(profiles)

        assert found["owner_user_id"] == {"U1": ["asuka"]}

    def test_봇이_하나도_없으면_빈_값(self, tmp_path: Path) -> None:
        found = detect_defaults.from_profiles(tmp_path / "없음")
        assert found == {"owner_user_id": {}, "troubleshoot_channel": {}}
