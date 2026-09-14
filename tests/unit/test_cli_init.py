"""`slack-cli-agent init` — 설치 직후 프로필 뼈대와 상태 디렉터리 구조를 만든다."""

from __future__ import annotations

import io
import json
from pathlib import Path

from slack_cli_agent.cli import SlackCliAgent


def run_init(profile_dir: Path, state_dir: Path, name: str = "example") -> tuple[int, str]:
    out = io.StringIO()
    code = SlackCliAgent().run(
        [
            "init",
            "--name",
            name,
            "--profile-dir",
            str(profile_dir),
            "--state-dir",
            str(state_dir),
        ],
        stdout=out,
    )
    return code, out.getvalue()


class TestInitCommand:
    def test_프로필_파일을_만든다(self, tmp_path: Path) -> None:
        profile_dir = tmp_path / "profiles"
        state_dir = tmp_path / "state"
        code, _ = run_init(profile_dir, state_dir)
        assert code == 0
        profile_path = profile_dir / "example.json"
        assert profile_path.is_file()
        data = json.loads(profile_path.read_text(encoding="utf-8"))
        assert data["name"] == "example"
        assert "primary_engine" in data

    def test_상태_디렉터리_뼈대를_만든다(self, tmp_path: Path) -> None:
        profile_dir = tmp_path / "profiles"
        state_dir = tmp_path / "state"
        run_init(profile_dir, state_dir)
        assert (state_dir / "prompts").is_dir()
        assert (state_dir / "persona").is_dir()
        assert (state_dir / "persona" / "knowledge").is_dir()
        assert (state_dir / "engine").is_dir()

    def test_이미_있는_프로필_파일은_덮지_않는다(self, tmp_path: Path) -> None:
        profile_dir = tmp_path / "profiles"
        state_dir = tmp_path / "state"
        profile_dir.mkdir(parents=True)
        profile_path = profile_dir / "example.json"
        original = '{"name": "example", "owner_user_id": "U_기존값"}'
        profile_path.write_text(original, encoding="utf-8")

        code, out = run_init(profile_dir, state_dir)

        assert code == 0
        assert profile_path.read_text(encoding="utf-8") == original
        assert "건너뛰" in out

    def test_이미_있는_상태_디렉터리_안의_파일은_그대로다(self, tmp_path: Path) -> None:
        profile_dir = tmp_path / "profiles"
        state_dir = tmp_path / "state"
        prompts_dir = state_dir / "prompts"
        prompts_dir.mkdir(parents=True)
        existing_prompt = prompts_dir / "owner_note.md"
        existing_prompt.write_text("사람이 직접 쓴 내용", encoding="utf-8")

        run_init(profile_dir, state_dir)

        assert existing_prompt.read_text(encoding="utf-8") == "사람이 직접 쓴 내용"

    def test_두_번_실행하면_전부_건너뛴다고_출력한다(self, tmp_path: Path) -> None:
        profile_dir = tmp_path / "profiles"
        state_dir = tmp_path / "state"
        run_init(profile_dir, state_dir)
        code, out = run_init(profile_dir, state_dir)
        assert code == 0
        assert "건너뛰" in out

    def test_출력은_한국어이고_다음에_채울_값을_안내한다(self, tmp_path: Path) -> None:
        profile_dir = tmp_path / "profiles"
        state_dir = tmp_path / "state"
        _, out = run_init(profile_dir, state_dir)
        assert "SLACK_BOT_TOKEN" in out
        assert "owner_user_id" in out
