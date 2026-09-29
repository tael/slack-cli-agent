"""요청에 붙일 실행 보장 요구를 한 곳에서 정한다 (sca-98k).

기준은 신뢰 등급이나 요청 문구의 위험도가 아니라 '이 요청에 준 도구 권한을
엔진이 실제로 강제해야 하는가' 다. 제어면은 채널 설정과 작업 종류뿐이다.
"""

from __future__ import annotations

import pytest

from slack_cli_agent.auth.execution_policy import NO_TOOLS_POLICY, ExecutionPolicy
from slack_cli_agent.config.channel import (
    TOOL_ENFORCEMENT_AUDITED,
    TOOL_ENFORCEMENT_STRICT,
    ChannelConfig,
)
from slack_cli_agent.core.errors import ConfigError
from slack_cli_agent.engine.capability import ToolRestriction
from slack_cli_agent.engine.tool_selection import ToolSelection


def 채널(**kw: object) -> ChannelConfig:
    return ChannelConfig(channel_id="C1", **kw)  # type: ignore[arg-type]


class Test기본은_감사_완화다:
    def test_채널_설정이_없으면_완화를_허용한다(self) -> None:
        """기본이 STRICT 면 codex·gemini 봇의 모든 요청이 실행 전에 막힌다."""
        요구 = ExecutionPolicy().requirements_for(config=None, tools=ToolSelection.allow(["Read"]))
        assert 요구.tool_restriction is ToolRestriction.EXACT_ALLOWLIST
        assert 요구.downgradable_axes == frozenset({"tool_restriction"})

    def test_audited_채널도_같다(self) -> None:
        요구 = ExecutionPolicy().requirements_for(
            config=채널(tool_enforcement=TOOL_ENFORCEMENT_AUDITED), tools=ToolSelection.allow(["Read"])
        )
        assert 요구.downgradable_axes == frozenset({"tool_restriction"})


class Test강제_채널:
    def test_strict_면_완화를_안_준다(self) -> None:
        요구 = ExecutionPolicy().requirements_for(
            config=채널(tool_enforcement=TOOL_ENFORCEMENT_STRICT), tools=ToolSelection.allow(["Read"])
        )
        assert 요구.tool_restriction is ToolRestriction.EXACT_ALLOWLIST
        assert 요구.downgradable_axes == frozenset()


class Test도구_목록이_비면_요구가_없다:
    def test_빈_목록에는_강제할_경계가_없다(self) -> None:
        """--allowedTools 를 빈 값으로 넘긴 것은 제한을 건 것이 아니다.
        여기서 요구를 세우면 지킬 것이 없는데 엔진만 막힌다."""
        요구 = ExecutionPolicy().requirements_for(config=None, tools=ToolSelection.unrestricted())
        assert 요구.tool_restriction is None
        assert 요구.downgradable_axes == frozenset()

    def test_strict_채널이어도_빈_목록은_요구가_없다(self) -> None:
        요구 = ExecutionPolicy().requirements_for(
            config=채널(tool_enforcement=TOOL_ENFORCEMENT_STRICT), tools=ToolSelection.unrestricted()
        )
        assert 요구.tool_restriction is None


class Test정책_이름을_남긴다:
    """감사 기록에서 '이 정책이 요구를 세웠다' 와 '아무도 안 세웠다' 가
    구분돼야 한다 (sca-98k 3/3)."""

    def test_감사_완화는_audited_로_남는다(self) -> None:
        요구 = ExecutionPolicy().requirements_for(config=None, tools=ToolSelection.allow(["Read"]))
        assert 요구.policy == TOOL_ENFORCEMENT_AUDITED

    def test_강제_채널은_strict_로_남는다(self) -> None:
        요구 = ExecutionPolicy().requirements_for(
            config=채널(tool_enforcement=TOOL_ENFORCEMENT_STRICT), tools=ToolSelection.allow(["Read"])
        )
        assert 요구.policy == TOOL_ENFORCEMENT_STRICT

    def test_도구가_없어_요구를_안_세운_것도_이름이_있다(self) -> None:
        요구 = ExecutionPolicy().requirements_for(config=None, tools=ToolSelection.unrestricted())
        assert 요구.policy == NO_TOOLS_POLICY

    def test_아무도_안_세운_요구는_이름이_비어_있다(self) -> None:
        from slack_cli_agent.engine.capability import ExecutionRequirements

        assert ExecutionRequirements().policy == ""


class Test설정값_검증:
    @pytest.mark.parametrize("값", ["strinct", "", None, False, 0, ["strict"]])
    def test_키가_있는데_값이_잘못되면_막는다(self, 값: object) -> None:
        """조용히 기본값으로 떨어지면 strict 채널을 잘못 편집했을 때 차단이
        아니라 완화 실행이 된다. 빈 문자열과 null 도 오편집이다."""
        with pytest.raises(ConfigError):
            ChannelConfig.from_dict("C1", {"tool_enforcement": 값})

    def test_설정이_없으면_기본은_audited_다(self) -> None:
        assert ChannelConfig.from_dict("C1", {}).tool_enforcement == TOOL_ENFORCEMENT_AUDITED
