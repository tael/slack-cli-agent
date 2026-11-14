"""상시 스킬 권한 (I83).

마메치는 metabase·askdata-* 같은 조회 스킬을 `Skill(<이름>)` 로 늘 허용한다
(bot.py:793). skills 가 꺼진 채널에서도 지정된 상시 스킬은 쓸 수 있어야 한다.
sca 는 넓은 `Skill` 도구를 skills 켜진 채널에서만 붙여, 상시 스킬 개념이
없었다. 메커니즘은 공통이고 어떤 스킬을 상시로 둘지는 조직 설정이다.
"""

from __future__ import annotations

from slack_cli_agent.auth.principal import Principal, TrustLevel
from slack_cli_agent.auth.tools import ToolPolicy

_GEN = Principal(user_id="U1", channel="C1", trust=TrustLevel.GENERAL, is_direct_message=False)


def test_상시스킬은_스킬_꺼진_채널에서도_부여된다() -> None:
    policy = ToolPolicy(
        base_tools=("Read",), always_skills=("metabase", "askdata-order-lookup")
    )
    tools = policy.tool_list_for(_GEN, skills_enabled=False)
    assert "Skill(metabase)" in tools
    assert "Skill(askdata-order-lookup)" in tools


def test_상시스킬_기본값은_없음() -> None:
    policy = ToolPolicy(base_tools=("Read",))
    assert policy.tool_list_for(_GEN, skills_enabled=False) == ("Read",)


def test_넓은_Skill이_켜지면_개별_grant는_생략된다() -> None:
    # 넓은 Skill 이 모든 이름을 포함하므로 중복 grant 를 붙이지 않는다.
    policy = ToolPolicy(base_tools=("Read",), always_skills=("metabase",))
    tools = policy.tool_list_for(_GEN, skills_enabled=True)
    assert "Skill" in tools
    assert "Skill(metabase)" not in tools


def test_읽기전용_턴에는_상시스킬도_빠진다() -> None:
    # 읽기 전용 턴(감시 점검 등)은 스킬을 일절 받지 않는다.
    policy = ToolPolicy(base_tools=("Read",), always_skills=("metabase",))
    tools = policy.tool_list_for(_GEN, readonly=True)
    assert all("Skill" not in t for t in tools)


def test_Application이_설정의_상시스킬을_정책에_연결한다(tmp_path) -> None:
    from test_application import FakeSlackClient, write_profile

    from slack_cli_agent.core.application import Application

    profile = write_profile(
        tmp_path, settings={"base_tools": ["Read"], "always_skills": ["metabase"]}
    )
    app = Application(profile, FakeSlackClient())
    general = Principal(
        user_id="U1", channel="C1", trust=TrustLevel.GENERAL, is_direct_message=False
    )
    tools = app.tool_policy().tool_list_for(general, skills_enabled=False)
    assert "Skill(metabase)" in tools
