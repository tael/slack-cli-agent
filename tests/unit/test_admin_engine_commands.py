"""엔진 승인/거부 관리 명령."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from slack_cli_agent.admin.command import AdminContext
from slack_cli_agent.admin.engine_commands import EngineApproveCommand, EngineDenyCommand
from slack_cli_agent.auth.principal import Principal, TrustLevel
from slack_cli_agent.config.channel import ChannelRegistry
from slack_cli_agent.config.profile import Profile


def make_profile(tmp_path: Path, *, with_fallback: bool = True) -> Profile:
    data = {
        "name": "example",
        "display_name": "예시봇",
        "primary_engine": {"type": "claude", "binary": "claude", "model": "m"},
        "owner_user_id": "U_OWNER",
        "troubleshoot_channel": "C1",
        "state_dir": str(tmp_path / "state"),
    }
    if with_fallback:
        data["fallback_engine"] = {"type": "codex", "binary": "codex", "model": "m2"}
    return Profile.from_dict(data)


def make_context(tmp_path: Path, *, with_fallback: bool = True) -> AdminContext:
    profile = make_profile(tmp_path, with_fallback=with_fallback)
    registry = ChannelRegistry(tmp_path / "channels.json")
    principal = Principal(user_id="U_OWNER", channel="C1", trust=TrustLevel.OWNER, is_direct_message=False)
    return AdminContext(principal=principal, channel="C1", thread_ts="1.0", channels=registry, profile=profile)


def write_switch_state(ctx: AdminContext, **extra) -> None:
    path = ctx.profile.paths.engine_state
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {"engine": "codex", "approval": "pending", "detail": "한도 소진"}
    state.update(extra)
    path.write_text(json.dumps(state), encoding="utf-8")


# 엔진 승인


class TestEngineApproveCommand:
    @pytest.mark.parametrize("text", ["엔진 승인", "엔진승인", "엔진 허용"])
    def test_별칭을_받는다(self, text: str) -> None:
        assert EngineApproveCommand().matches(text) is True

    def test_전환_기록이_없으면_승인할_전환이_없다고_답한다(self, tmp_path: Path) -> None:
        ctx = make_context(tmp_path)
        result = EngineApproveCommand().execute(ctx)
        assert "승인할 전환이 없습니다" in result.message
        assert "claude" in result.message

    def test_전환_기록이_있으면_승인하고_상태를_바꾼다(self, tmp_path: Path) -> None:
        ctx = make_context(tmp_path)
        write_switch_state(ctx)
        result = EngineApproveCommand().execute(ctx)
        saved = json.loads(ctx.profile.paths.engine_state.read_text(encoding="utf-8"))
        assert saved["approval"] == "approved"
        assert "codex" in result.message


# 엔진 거부


class TestEngineDenyCommand:
    @pytest.mark.parametrize("text", ["엔진 거부", "엔진거부", "엔진 취소"])
    def test_별칭을_받는다(self, text: str) -> None:
        assert EngineDenyCommand().matches(text) is True

    def test_전환_기록이_없으면_거부할_전환이_없다고_답한다(self, tmp_path: Path) -> None:
        ctx = make_context(tmp_path)
        result = EngineDenyCommand().execute(ctx)
        assert "거부할 전환이 없습니다" in result.message

    def test_전환_기록이_있으면_거부하고_상태를_바꾼다(self, tmp_path: Path) -> None:
        ctx = make_context(tmp_path)
        write_switch_state(ctx)
        result = EngineDenyCommand().execute(ctx)
        saved = json.loads(ctx.profile.paths.engine_state.read_text(encoding="utf-8"))
        assert saved["approval"] == "denied"
        assert "쓰지 않겠습니다" in result.message
