"""`slack-cli-agent init` — 설치 직후 프로필 뼈대와 상태 디렉터리 구조를 만든다."""

from __future__ import annotations

import io
import json
from pathlib import Path

from slack_cli_agent.cli import SlackCliAgent


def run_init(
    profile_dir: Path, state_dir: Path, name: str = "example", work_root: Path | None = None
) -> tuple[int, str]:
    out = io.StringIO()
    argv = [
        "init",
        "--name",
        name,
        "--profile-dir",
        str(profile_dir),
        "--state-dir",
        str(state_dir),
    ]
    if work_root is not None:
        argv += ["--work-root", str(work_root)]
    code = SlackCliAgent().run(argv, stdout=out)
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


class Test만든_프로필이_바로_기동_가능하다:
    """init 으로 만든 프로필이 게이트에 막히면 설치 직후 경로가 끊긴다.
    2026-09-17 에 실제로 그랬다 - 작업 자리 기본값이 홈 안이었다 (sca-xay).
    """

    def test_필수값을_채우면_preflight_가_통과한다(self, tmp_path: Path) -> None:
        profile_dir = tmp_path / "profiles"
        # conftest 가 HOME 을 tmp_path 로 바꾼다. 작업 자리는 홈 밖이어야 한다.
        work_root = tmp_path.parent / f"{tmp_path.name}-init-work"
        code, _ = run_init(profile_dir, tmp_path / "state", work_root=work_root)
        assert code == 0

        # 사람이 채우는 값. 뼈대가 비워 둔 것만 채우고 나머지는 그대로 쓴다 -
        # 작업 자리를 여기서 고치면 이 시험이 뼈대를 안 보게 된다.
        경로 = profile_dir / "example.json"
        data = json.loads(경로.read_text(encoding="utf-8"))
        data["primary_engine"]["model"] = "m"
        data["primary_engine"]["binary"] = "python3"
        data["owner_user_id"] = "U1"
        data["troubleshoot_channel"] = "C1"
        경로.write_text(json.dumps(data), encoding="utf-8")

        out = io.StringIO()
        결과 = SlackCliAgent().run(
            ["preflight", "--profile", "example", "--profile-dir", str(profile_dir)], stdout=out
        )
        assert 결과 == 0, out.getvalue()

    def test_작업_자리를_실제로_만든다(self, tmp_path: Path) -> None:
        """프로필에 경로만 적고 만들지 않으면 점검이 '작업 자리가 없다' 로 막는다."""
        work_root = tmp_path.parent / f"{tmp_path.name}-init-work2"
        run_init(tmp_path / "profiles", tmp_path / "state", work_root=work_root)
        assert work_root.is_dir()

    def test_기본_작업_자리는_홈_밖이다(self, tmp_path: Path) -> None:
        """인자를 안 주는 쪽이 기본 경로다. 그것이 홈 안이면 설치 직후 막힌다."""
        profile_dir = tmp_path / "profiles"
        run_init(profile_dir, tmp_path / "state")
        data = json.loads((profile_dir / "example.json").read_text(encoding="utf-8"))
        적힌곳 = Path(data["work_root"]).expanduser()
        assert not 적힌곳.is_relative_to(Path.home()), 적힌곳


class Test배포_견본:
    def test_견본의_작업_자리가_홈_밖이다(self) -> None:
        """견본을 복사해 쓰는 설치 경로도 같은 계약을 따라야 한다."""
        견본 = Path(__file__).resolve().parents[2] / "profiles" / "example.example.json"
        적힌곳 = Path(json.loads(견본.read_text(encoding="utf-8"))["work_root"]).expanduser()
        assert not 적힌곳.is_relative_to(Path.home()), 적힌곳
