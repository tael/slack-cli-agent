"""채널 설정을 바꾸는 관리 명령 — 말수 3종, 모드 전환, 채널 해제."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from slack_cli_agent.admin.channel_commands import (
    ChannelUnregisterCommand,
    ChatActiveCommand,
    ChatNormalCommand,
    ChatQuietCommand,
    CoachModeCommand,
    DefaultModeCommand,
    UnaddressedOffCommand,
    UnaddressedOnCommand,
)
from slack_cli_agent.admin.command import AdminContext
from slack_cli_agent.auth.principal import Principal, TrustLevel
from slack_cli_agent.config.channel import ChannelRegistry
from slack_cli_agent.config.profile import Profile
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


def make_context(tmp_path: Path, *, channel: str = "C1", channels_path: Path | None = None) -> AdminContext:
    profile = make_profile(tmp_path)
    registry = ChannelRegistry(channels_path or (tmp_path / "channels.json"))
    principal = Principal(user_id="U_OWNER", channel=channel, trust=TrustLevel.OWNER, is_direct_message=False)
    return AdminContext(principal=principal, channel=channel, thread_ts="1.0", channels=registry, profile=profile)


def read_channel(tmp_path: Path, channel: str = "C1") -> dict:
    path = tmp_path / "channels.json"
    return json.loads(path.read_text(encoding="utf-8"))[channel]


# ---------------------------------------------------------------------------
# 말수 3종


class TestChatActiveCommand:
    @pytest.mark.parametrize("text", ["말수 많게", "적극 모드", "말 많이", "수다 모드"])
    def test_원본_별칭을_전부_받는다(self, text: str) -> None:
        cmd = ChatActiveCommand(NoticeCatalog())
        assert cmd.matches(text) is True

    def test_실행하면_chat이_active로_저장된다(self, tmp_path: Path) -> None:
        ctx = make_context(tmp_path)
        cmd = ChatActiveCommand(NoticeCatalog())
        result = cmd.execute(ctx)
        assert read_channel(tmp_path)["chat"] == "active"
        assert result.handled is True
        assert result.message


class TestChatNormalCommand:
    @pytest.mark.parametrize("text", ["말수 보통", "기본 말수"])
    def test_원본_별칭을_전부_받는다(self, text: str) -> None:
        cmd = ChatNormalCommand(NoticeCatalog())
        assert cmd.matches(text) is True

    def test_실행하면_chat이_normal로_저장된다(self, tmp_path: Path) -> None:
        ctx = make_context(tmp_path)
        cmd = ChatNormalCommand(NoticeCatalog())
        cmd.execute(ctx)
        assert read_channel(tmp_path)["chat"] == "normal"


class TestChatQuietCommand:
    @pytest.mark.parametrize("text", ["말수 적게", "조용 모드", "조용히"])
    def test_원본_별칭을_전부_받는다(self, text: str) -> None:
        cmd = ChatQuietCommand(NoticeCatalog())
        assert cmd.matches(text) is True

    def test_실행하면_chat이_quiet로_저장된다(self, tmp_path: Path) -> None:
        ctx = make_context(tmp_path)
        cmd = ChatQuietCommand(NoticeCatalog())
        cmd.execute(ctx)
        assert read_channel(tmp_path)["chat"] == "quiet"


class TestChatCommandsAreMutuallyExclusive:
    def test_많게는_보통_적게_문구를_안_받는다(self) -> None:
        cmd = ChatActiveCommand(NoticeCatalog())
        assert cmd.matches("말수 보통") is False
        assert cmd.matches("말수 적게") is False


# ---------------------------------------------------------------------------
# 호명 정책


class TestUnaddressedOnCommand:
    @pytest.mark.parametrize("text", ["끼어들기 허용", "끼어들기허용", "호명 없이 응답", "멘션 없이 응답"])
    def test_별칭을_받는다(self, text: str) -> None:
        assert UnaddressedOnCommand(NoticeCatalog()).matches(text) is True

    def test_실행하면_answer_unaddressed가_켜진다(self, tmp_path: Path) -> None:
        ctx = make_context(tmp_path)
        result = UnaddressedOnCommand(NoticeCatalog()).execute(ctx)
        assert read_channel(tmp_path)["answer_unaddressed"] is True
        assert result.handled is True
        assert result.message


class TestUnaddressedOffCommand:
    @pytest.mark.parametrize("text", ["멘션 전용", "멘션전용", "호명 전용", "불러야 답해"])
    def test_별칭을_받는다(self, text: str) -> None:
        assert UnaddressedOffCommand(NoticeCatalog()).matches(text) is True

    def test_이미_켜진_채널도_꺼진다(self, tmp_path: Path) -> None:
        """값을 지우지 않고 False 로 명시해야 한다. 키를 지우면 기본값 판정에 기댄다."""
        channels_path = tmp_path / "channels.json"
        channels_path.write_text(
            json.dumps({"C1": {"answer_unaddressed": True}}), encoding="utf-8"
        )
        ctx = make_context(tmp_path, channels_path=channels_path)
        UnaddressedOffCommand(NoticeCatalog()).execute(ctx)
        assert json.loads(channels_path.read_text(encoding="utf-8"))["C1"]["answer_unaddressed"] is False


class TestUnaddressedCommandsAreMutuallyExclusive:
    def test_허용_명령은_전용_문구를_안_받는다(self) -> None:
        assert UnaddressedOnCommand(NoticeCatalog()).matches("멘션 전용") is False
        assert UnaddressedOffCommand(NoticeCatalog()).matches("끼어들기 허용") is False


# ---------------------------------------------------------------------------
# 코치 모드


class TestCoachModeCommand:
    @pytest.mark.parametrize("text", ["코치 모드", "코치모드"])
    def test_별칭을_받는다(self, text: str) -> None:
        cmd = CoachModeCommand(NoticeCatalog())
        assert cmd.matches(text) is True

    def test_실행하면_mode와_mention_only에_해당하는_설정이_함께_켜진다(self, tmp_path: Path) -> None:
        ctx = make_context(tmp_path)
        cmd = CoachModeCommand(NoticeCatalog())
        result = cmd.execute(ctx)
        saved = read_channel(tmp_path)
        assert saved["mode"] == "agent_coach"
        assert saved["answer_unaddressed"] is False
        assert saved["light_context"] is True
        assert result.handled is True


# ---------------------------------------------------------------------------
# 기본 모드


class TestDefaultModeCommand:
    @pytest.mark.parametrize("text", ["기본 모드", "기본모드"])
    def test_별칭을_받는다(self, text: str) -> None:
        cmd = DefaultModeCommand(NoticeCatalog())
        assert cmd.matches(text) is True

    def test_실행하면_mode가_private로_저장된다(self, tmp_path: Path) -> None:
        ctx = make_context(tmp_path)
        cmd = DefaultModeCommand(NoticeCatalog())
        cmd.execute(ctx)
        assert read_channel(tmp_path)["mode"] == "private"


# ---------------------------------------------------------------------------
# 채널 해제


class TestChannelUnregisterCommand:
    @pytest.mark.parametrize("text", ["채널 해제", "채널해제", "여기서 나가", "응답 중지"])
    def test_별칭을_받는다(self, text: str) -> None:
        cmd = ChannelUnregisterCommand(NoticeCatalog())
        assert cmd.matches(text) is True

    def test_등록_안_된_채널이면_그_사실을_답한다(self, tmp_path: Path) -> None:
        ctx = make_context(tmp_path)
        cmd = ChannelUnregisterCommand(NoticeCatalog())
        result = cmd.execute(ctx)
        assert "목록에 없습니다" in result.message

    def test_등록된_채널이면_목록에서_뺀다(self, tmp_path: Path) -> None:
        channels_path = tmp_path / "channels.json"
        channels_path.write_text(json.dumps({"C1": {"name": "테스트채널"}}), encoding="utf-8")
        ctx = make_context(tmp_path, channels_path=channels_path)
        cmd = ChannelUnregisterCommand(NoticeCatalog())
        result = cmd.execute(ctx)
        assert ctx.channels.is_registered("C1") is False
        assert "테스트채널" in result.message
