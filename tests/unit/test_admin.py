"""관리 명령. AdminCommand 계약, AdminRouter 권한 대조, 기본 명령 3종."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from slack_cli_agent.admin.command import AdminCommand, AdminContext, AdminResult
from slack_cli_agent.admin.commands import ChannelListCommand, EngineStatusCommand, HelpCommand
from slack_cli_agent.admin.defaults import default_admin_commands
from slack_cli_agent.admin.router import AdminRouter
from slack_cli_agent.auth.principal import Principal, TrustLevel
from slack_cli_agent.config.channel import ChannelRegistry
from slack_cli_agent.config.profile import Profile
from slack_cli_agent.core.errors import ConfigError
from slack_cli_agent.observability.notices import NoticeCatalog


def make_profile(tmp_path: Path) -> Profile:
    return Profile.from_dict(
        {
            "name": "example",
            "display_name": "예시봇",
            "primary_engine": {"type": "claude", "binary": "claude", "model": "m"},
            "owner_user_id": "U_OWNER",
            "troubleshoot_channel": "C1",
            "state_dir": str(tmp_path / "state"),
        }
    )


def make_context(
    tmp_path: Path,
    *,
    trust: TrustLevel = TrustLevel.OWNER,
    channel: str = "C1",
    channels_path: Path | None = None,
) -> AdminContext:
    profile = make_profile(tmp_path)
    registry = ChannelRegistry(channels_path or (tmp_path / "channels.json"))
    principal = Principal(user_id="U_OWNER", channel=channel, trust=trust, is_direct_message=False)
    return AdminContext(principal=principal, channel=channel, thread_ts="1.0", channels=registry, profile=profile)


# AdminCommand 계약


class TestAdminCommandContract:
    def test_ABC라_직접_인스턴스화하지_못한다(self) -> None:
        with pytest.raises(TypeError):
            AdminCommand()  # type: ignore[abstract]

    def test_기본_요구_신뢰등급은_OWNER다(self) -> None:
        class _Cmd(AdminCommand):
            name = "x"

            def matches(self, text: str) -> bool:
                return False

            def execute(self, ctx: AdminContext) -> AdminResult:
                return AdminResult(message="")

        assert _Cmd.required_trust is TrustLevel.OWNER


class TestAdminResult:
    def test_기본은_handled_True다(self) -> None:
        assert AdminResult(message="x").handled is True


# AdminRouter — 매칭과 권한 대조


class _EchoCommand(AdminCommand):
    name = "echo"
    usage = "에코"
    description = "말한 것을 그대로 돌려준다"
    required_trust = TrustLevel.TRUSTED

    def matches(self, text: str) -> bool:
        return text.strip() == "에코"

    def execute(self, ctx: AdminContext) -> AdminResult:
        return AdminResult(message="에코 응답")


class TestAdminRouter:
    def test_맞는_명령이_없으면_None이다(self, tmp_path: Path) -> None:
        router = AdminRouter([_EchoCommand()])
        result = router.dispatch("아무 말", make_context(tmp_path))
        assert result is None

    def test_권한이_충분하면_실행한다(self, tmp_path: Path) -> None:
        router = AdminRouter([_EchoCommand()])
        ctx = make_context(tmp_path, trust=TrustLevel.TRUSTED)
        result = router.dispatch("에코", ctx)
        assert result is not None
        assert result.handled is True
        assert result.message == "에코 응답"

    def test_권한이_모자라면_실행하지_않고_거절한다(self, tmp_path: Path) -> None:
        router = AdminRouter([_EchoCommand()])
        ctx = make_context(tmp_path, trust=TrustLevel.GENERAL)
        result = router.dispatch("에코", ctx)
        assert result is not None
        assert result.handled is False

    def test_소유자는_TRUSTED_요구_명령도_쓴다(self, tmp_path: Path) -> None:
        router = AdminRouter([_EchoCommand()])
        ctx = make_context(tmp_path, trust=TrustLevel.OWNER)
        result = router.dispatch("에코", ctx)
        assert result is not None
        assert result.handled is True


# HelpCommand


class TestHelpCommand:
    @pytest.mark.parametrize("text", ["도움말", "help", "명령어"])
    def test_이형태를_전부_받는다(self, text: str) -> None:
        assert HelpCommand().matches(text) is True

    def test_봇_표시이름이_들어간다(self, tmp_path: Path) -> None:
        result = HelpCommand().execute(make_context(tmp_path))
        assert "예시봇" in result.message

    def test_등록된_명령을_전부_안내한다(self, tmp_path: Path) -> None:
        """안내가 고정 문구면 명령을 추가해도 안 보인다. 실제로 등록된 것을 낸다."""
        router = AdminRouter([HelpCommand(), _EchoCommand()])
        result = router.dispatch("도움말", make_context(tmp_path))
        assert result is not None
        assert "에코" in result.message
        assert "말한 것을 그대로 돌려준다" in result.message


class TestApplicationHelpCoverage:
    def test_안내에_실제_명령이_전부_있다(self, tmp_path: Path) -> None:
        """관리 명령을 새로 넣고 안내에 안 넣으면 사용자는 그것을 모른다."""
        commands = default_admin_commands(NoticeCatalog())
        result = AdminRouter(commands).dispatch("도움말", make_context(tmp_path))
        assert result is not None
        for command in commands:
            assert command.usage in result.message, command.name


# ChannelListCommand


class TestChannelListCommand:
    def test_등록된_채널이_없으면_그렇게_말한다(self, tmp_path: Path) -> None:
        ctx = make_context(tmp_path)
        result = ChannelListCommand().execute(ctx)
        assert "등록된 채널이 없습니다" in result.message

    def test_등록된_채널을_나열한다(self, tmp_path: Path) -> None:
        channels_path = tmp_path / "channels.json"
        channels_path.write_text(
            json.dumps({"C1": {"mode": "agent_coach", "chat": "active"}}),
            encoding="utf-8",
        )
        ctx = make_context(tmp_path, channels_path=channels_path)
        result = ChannelListCommand().execute(ctx)
        assert "C1" in result.message
        assert "코치" in result.message
        assert "많음" in result.message

    def test_호명_정책과_세션_단위를_같이_보여준다(self, tmp_path: Path) -> None:
        """어느 채널이 멘션 없이 답하는지 슬랙에서 확인할 방법이 없었다."""
        channels_path = tmp_path / "channels.json"
        channels_path.write_text(
            json.dumps({
                "C1": {"answer_unaddressed": True, "session_scope": "channel"},
                "C2": {},
            }),
            encoding="utf-8",
        )
        ctx = make_context(tmp_path, channels_path=channels_path)
        result = ChannelListCommand().execute(ctx)
        assert "호명 : 없어도 답함, 세션 : 채널" in result.message
        assert "호명 : 불러야 답함, 세션 : 스레드" in result.message


# EngineStatusCommand


class TestEngineStatusCommand:
    def test_전환_기록이_없으면_전환_없음이다(self, tmp_path: Path) -> None:
        ctx = make_context(tmp_path)
        result = EngineStatusCommand().execute(ctx)
        assert "전환 : 없음" in result.message
        assert "claude" in result.message

    def test_전환_기록이_있으면_승인_상태를_보여준다(self, tmp_path: Path) -> None:
        ctx = make_context(tmp_path)
        ctx.profile.paths.root.mkdir(parents=True)
        ctx.profile.paths.engine_state.write_text(
            json.dumps({"engine": "codex", "approval": "approved", "detail": "한도 소진"}),
            encoding="utf-8",
        )
        result = EngineStatusCommand().execute(ctx)
        assert "codex" in result.message
        assert "승인됨" in result.message
        assert "한도 소진" in result.message


class Test채널목록은이름을보여준다:
    """원본은 저장된 채널 이름을 보여주고 없을 때만 채널 ID 로 대신한다.

    채널 ID 만 나오면 어느 채널인지 사람이 알아볼 수 없다. `name` 이 정식
    필드로 올라간 뒤에도 이 표시가 채널 ID 그대로였다.
    """

    def test_이름이있으면이름을쓴다(self, tmp_path: Path) -> None:
        channels_path = tmp_path / "channels.json"
        channels_path.write_text(
            json.dumps({"C1": {"name": "일반", "mode": "private"}}), encoding="utf-8"
        )
        ctx = make_context(tmp_path, channels_path=channels_path)
        result = ChannelListCommand().execute(ctx)
        assert "- 일반" in result.message
        assert "- C1" not in result.message

    def test_이름이없으면채널ID로대신한다(self, tmp_path: Path) -> None:
        channels_path = tmp_path / "channels.json"
        channels_path.write_text(json.dumps({"C1": {"mode": "private"}}), encoding="utf-8")
        ctx = make_context(tmp_path, channels_path=channels_path)
        result = ChannelListCommand().execute(ctx)
        assert "- C1" in result.message


class Test설정오류를사용자에게알린다:
    """설정 파일을 못 읽어 명령이 멈추면 접수기가 예외를 삼키고 로그만 남긴다.
    사용자 쪽에서는 아무 응답이 없어 명령이 먹은 것과 구분되지 않는다 (sca-zvk).
    """

    class _터지는명령(AdminCommand):
        name = "터짐"
        usage = "터짐"
        description = "설정 오류를 낸다"
        required_trust = TrustLevel.TRUSTED

        def matches(self, text: str) -> bool:
            return text.strip() == "터짐"

        def execute(self, ctx: AdminContext) -> AdminResult:
            raise ConfigError("채널 설정을 읽지 못해 쓰기를 멈춘다 : /경로")

    def test_설정_오류는_사유를_담아_돌려준다(self, tmp_path: Path) -> None:
        router = AdminRouter([self._터지는명령()])
        ctx = make_context(tmp_path, trust=TrustLevel.TRUSTED)

        result = router.dispatch("터짐", ctx)

        assert result is not None
        assert result.handled is False
        assert "채널 설정을 읽지 못해" in result.message
