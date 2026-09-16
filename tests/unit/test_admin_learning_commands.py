"""학습 명령이 관리 명령으로 연결되는지 고정한다.

`LearningService` 를 만들어도 관리 명령에 연결하지 않으면 사용자는 그 기능을
부를 수 없다. 원본 `handle_admin` 의 학습 3종에 대응한다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from slack_cli_agent.admin.command import AdminContext
from slack_cli_agent.admin.learning_commands import (
    LearningApplyCommand,
    LearningRevertCommand,
    LearningShowCommand,
)
from slack_cli_agent.config.paths import StatePaths


class Test경로계약:
    def test_제안디렉터리와지식디렉터리가있다(self, tmp_path: Path) -> None:
        """원본 PROPOSAL_DIR·KNOWLEDGE_DIR 에 대응한다. 경로 조립을 한 곳에 모은다."""
        paths = StatePaths.for_bot("testbot", home=tmp_path)
        assert paths.proposals == paths.root / "proposals"
        assert paths.knowledge == paths.persona / "knowledge"

    def test_학습이_쌓는_자리는_사람이_쓰는_자리와_다르다(self, tmp_path: Path) -> None:
        """같은 파일에 두 출처가 섞이면 한쪽 갱신이 다른 쪽을 덮어쓴다(sca-jl4.5)."""
        paths = StatePaths.for_bot("testbot", home=tmp_path)
        assert paths.learned == paths.persona / "learned"
        assert paths.learned != paths.knowledge

    def test_관리_명령의_적용과_되돌리기가_학습_자리를_본다(self, tmp_path: Path) -> None:
        """적용은 학습 자리에 쓰는데 되돌리기가 사람 자리를 보면 되돌려지지 않는다."""
        from slack_cli_agent.admin.learning_commands import _service

        ctx = 맥락(tmp_path)
        service = _service(ctx)
        paths = StatePaths(ctx.profile.state_dir)
        assert service._applier._dir == paths.learned
        assert service._reverter._dir == paths.learned


def 맥락(tmp_path: Path, text: str = "") -> AdminContext:
    from slack_cli_agent.auth.principal import Principal, TrustLevel
    from slack_cli_agent.config.channel import ChannelRegistry
    from slack_cli_agent.config.profile import EngineSpec, Profile

    paths = StatePaths.for_bot("testbot", home=tmp_path)
    paths.root.mkdir(parents=True, exist_ok=True)
    profile = Profile(
        name="testbot",
        display_name="테스트봇",
        primary_engine=EngineSpec(type="claude", binary=Path("/bin/true"), model="opus"),
        fallback_engine=None,
        state_dir=paths.root,
        work_root=tmp_path / "work",
        data_dir=tmp_path / "data",
        attach_dir=tmp_path / "attach",
        launch_label="test",
        owner_user_id="U1",
        troubleshoot_channel="C9",
    )
    return AdminContext(
        principal=Principal(user_id="U1", channel="C1", trust=TrustLevel.OWNER, is_direct_message=True),
        channel="C1",
        thread_ts="1.0",
        channels=ChannelRegistry(paths.channels),
        profile=profile,
        text=text,
    )


class Test학습명령이붙는다:
    @pytest.mark.parametrize(
        "명령, 본문",
        [
            (LearningShowCommand, "학습 제안"),
            (LearningShowCommand, "학습제안"),
            (LearningShowCommand, "배운 거"),
            (LearningApplyCommand, "학습 반영"),
            (LearningApplyCommand, "학습반영"),
            (LearningApplyCommand, "배운 거 반영"),
            (LearningRevertCommand, "학습 되돌리기 2026-08-26"),
            (LearningRevertCommand, "학습되돌리기 2026-08-26"),
        ],
    )
    def test_원본별칭을받는다(self, 명령, 본문: str) -> None:
        assert 명령().matches(본문) is True

    def test_반영명령이제안명령을가로채지않는다(self) -> None:
        """"배운 거 반영" 은 "배운 거" 로도 시작한다. 앞의 것이 먼저 맞으면 안 된다."""
        assert LearningShowCommand().matches("배운 거 반영") is False


class Test실제동작:
    def test_제안이없으면그렇게답한다(self, tmp_path: Path) -> None:
        결과 = LearningShowCommand().execute(맥락(tmp_path))
        assert "없어요" in 결과.message

    def test_제안을읽어보여준다(self, tmp_path: Path) -> None:
        paths = StatePaths.for_bot("testbot", home=tmp_path)
        paths.proposals.mkdir(parents=True, exist_ok=True)
        (paths.proposals / "2026-08-26.json").write_text(
            json.dumps({"day": "2026-08-26", "channel_knowledge": {"일반": ["배포는 금요일에 안 한다"]}}),
            encoding="utf-8",
        )
        결과 = LearningShowCommand().execute(맥락(tmp_path))
        assert "배포는 금요일에 안 한다" in 결과.message

    def test_되돌리기는날짜를본문에서뽑는다(self, tmp_path: Path) -> None:
        """라우터가 본문을 맥락에 담아 넘긴다. 명령이 본문을 따로 받지 않는다."""
        결과 = LearningRevertCommand().execute(맥락(tmp_path, "학습 되돌리기 2026-08-26"))
        assert "2026-08-26" in 결과.message

    def test_날짜가없으면형식을알려준다(self, tmp_path: Path) -> None:
        결과 = LearningRevertCommand().execute(맥락(tmp_path, "학습 되돌리기"))
        assert "날짜" in 결과.message


class Test라우터가본문을맥락에담는다:
    def test_명령이본문을읽을수있다(self, tmp_path: Path) -> None:
        """맥락을 만드는 호출부가 본문을 안 넣어도 라우터가 채운다.

        본문을 안 채우면 되돌리기가 날짜를 못 읽어 항상 형식 안내만 낸다.
        """
        from slack_cli_agent.admin.router import AdminRouter

        라우터 = AdminRouter([LearningRevertCommand()])
        결과 = 라우터.dispatch("학습 되돌리기 2026-08-26", 맥락(tmp_path))
        assert 결과 is not None
        assert "2026-08-26" in 결과.message
