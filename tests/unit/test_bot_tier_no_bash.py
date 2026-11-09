"""봇 명의 요청에는 Bash 가 어떤 형태로도 붙지 않는다.

2026-11-09 #test-agent 에서 구치파치 명의 글("date 명령을 실제로 실행해서")에
마메치가 Bash 로 date 를 실행했다. 조회 묶음의 Bash(<래퍼>:*) 가 도구 목록에 있으면
engine/claude.py 가 general 오버레이의 맨 Bash deny 를 풀고, Claude Code 는 deny 가
없는 Bash 에서 date 같은 읽기 명령을 허가 목록과 무관하게 실행한다.
원본 bot.py is_operator 는 "봇에게는 어느 경우에도 실행 도구가 붙지 않는다".
"""

from __future__ import annotations

from slack_cli_agent.auth.principal import Principal, TrustLevel
from slack_cli_agent.auth.tools import ToolPolicy

_GEN = Principal(user_id="U1", channel="C1", trust=TrustLevel.GENERAL, is_direct_message=False)

_BUNDLE = (
    "mcp__snowflake__run_snowflake_query",
    "Bash(~/wrap/git_read.sh:*)",
)


def _policy() -> ToolPolicy:
    return ToolPolicy(
        base_tools=("Read", "Grep", "Glob"),
        readonly_tools=_BUNDLE,
        channel_tools=lambda principal: ("Bash(~/wrap/chan.sh:*)", "mcp__openalex__search"),
    )


def test_봇_요청에는_Bash_도구가_붙지_않는다() -> None:
    tools = _policy().tool_list_for(_GEN, asker_is_bot=True)
    assert not [name for name in tools if name.split("(", 1)[0] == "Bash"]
    assert "mcp__snowflake__run_snowflake_query" in tools
    assert "mcp__openalex__search" in tools


def test_사람_요청의_조회_래퍼는_그대로_붙는다() -> None:
    tools = _policy().tool_list_for(_GEN, asker_is_bot=False)
    assert "Bash(~/wrap/git_read.sh:*)" in tools
    assert "Bash(~/wrap/chan.sh:*)" in tools
