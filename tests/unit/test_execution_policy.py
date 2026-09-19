"""요청에 붙일 실행 보장 요구를 한 곳에서 정한다 (sca-98k).

기준은 신뢰 등급이나 요청 문구의 위험도가 아니라 '이 요청에 준 도구 권한을
엔진이 실제로 강제해야 하는가' 다. 제어면은 채널 설정과 작업 종류뿐이다.
"""

from __future__ import annotations

import pytest

from slack_cli_agent.auth.execution_policy import ExecutionPolicy
from slack_cli_agent.config.channel import (
    TOOL_ENFORCEMENT_AUDITED,
    TOOL_ENFORCEMENT_STRICT,
    ChannelConfig,
)
from slack_cli_agent.core.errors import ConfigError
from slack_cli_agent.engine.capability import ToolRestriction


def 채널(**kw: object) -> ChannelConfig:
    return ChannelConfig(channel_id="C1", **kw)  # type: ignore[arg-type]


class Test기본은_감사_완화다:
    def test_채널_설정이_없으면_완화를_허용한다(self) -> None:
        """기본이 STRICT 면 codex·gemini 봇의 모든 요청이 실행 전에 막힌다."""
        요구 = ExecutionPolicy().requirements_for(config=None, allowed_tools=("Read",))
        assert 요구.tool_restriction is ToolRestriction.EXACT_ALLOWLIST
        assert 요구.allow_audited_downgrade is True

    def test_audited_채널도_같다(self) -> None:
        요구 = ExecutionPolicy().requirements_for(
            config=채널(tool_enforcement=TOOL_ENFORCEMENT_AUDITED), allowed_tools=("Read",)
        )
        assert 요구.allow_audited_downgrade is True


class Test강제_채널:
    def test_strict_면_완화를_안_준다(self) -> None:
        요구 = ExecutionPolicy().requirements_for(
            config=채널(tool_enforcement=TOOL_ENFORCEMENT_STRICT), allowed_tools=("Read",)
        )
        assert 요구.tool_restriction is ToolRestriction.EXACT_ALLOWLIST
        assert 요구.allow_audited_downgrade is False


class Test도구_목록이_비면_요구가_없다:
    def test_빈_목록에는_강제할_경계가_없다(self) -> None:
        """--allowedTools 를 빈 값으로 넘긴 것은 제한을 건 것이 아니다.
        여기서 요구를 세우면 지킬 것이 없는데 엔진만 막힌다."""
        요구 = ExecutionPolicy().requirements_for(config=None, allowed_tools=())
        assert 요구.tool_restriction is None
        assert 요구.allow_audited_downgrade is False

    def test_strict_채널이어도_빈_목록은_요구가_없다(self) -> None:
        요구 = ExecutionPolicy().requirements_for(
            config=채널(tool_enforcement=TOOL_ENFORCEMENT_STRICT), allowed_tools=()
        )
        assert 요구.tool_restriction is None


class Test설정값_검증:
    def test_모르는_값은_적재에서_막는다(self) -> None:
        """조용히 기본값으로 떨어지면 strict 오타가 강제 해제가 된다."""
        with pytest.raises(ConfigError):
            ChannelConfig.from_dict("C1", {"tool_enforcement": "strinct"})

    def test_설정이_없으면_기본은_audited_다(self) -> None:
        assert ChannelConfig.from_dict("C1", {}).tool_enforcement == TOOL_ENFORCEMENT_AUDITED
