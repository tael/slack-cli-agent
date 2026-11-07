"""비소유자 조회 도구 묶음 (A10).

마메치는 비소유자 턴에도 조회 MCP 묶음과 읽기 래퍼를 공통 도구로 준다
(bot.py:486 ALLOWED_TOOLS, 788 READ_SHELL_TOOLS). sca 는 비소유자 턴에
base_tools 3개(Read, Grep, Glob)만 줘서, 비소유자가 Snowflake 집계를 물어도
조회할 수단이 없었다. 묶음의 도구 이름은 조직 설정이라 프로필이 준다.
브라우저 조작 도구, 서버 와일드카드, 맨 쓰기 도구는 프로필이 적어도 붙이지 않는다.
"""

from __future__ import annotations

from slack_cli_agent.auth.principal import Principal, TrustLevel
from slack_cli_agent.auth.tools import ToolPolicy

_GEN = Principal(user_id="U1", channel="C1", trust=TrustLevel.GENERAL, is_direct_message=False)
_OWNER = Principal(user_id="U0", channel="C1", trust=TrustLevel.OWNER, is_direct_message=False)

_BUNDLE = (
    "mcp__snowflake__run_snowflake_query",
    "mcp__slack__slack_search",
    "mcp__browser__browser_navigate",
    "mcp__browser__browser_snapshot",
    "Bash(/opt/wrap/git_read.sh:*)",
)


def _policy(**kw) -> ToolPolicy:
    return ToolPolicy(base_tools=("Read", "Grep", "Glob"), readonly_tools=_BUNDLE, **kw)


def test_비소유자_턴에_조회_묶음이_붙는다() -> None:
    tools = _policy().tool_list_for(_GEN)
    for name in _BUNDLE:
        assert name in tools
    assert tools[:3] == ("Read", "Grep", "Glob")


def test_조회_묶음_기본값은_비어_있다() -> None:
    assert ToolPolicy(base_tools=("Read", "Grep", "Glob")).tool_list_for(_GEN) == (
        "Read", "Grep", "Glob",
    )


def test_aside_턴에도_조회_묶음이_붙는다() -> None:
    tools = _policy().tool_list_for(_GEN, aside=True)
    assert "mcp__snowflake__run_snowflake_query" in tools


def test_브라우저_조작과_와일드카드와_맨_쓰기도구는_빠진다() -> None:
    policy = ToolPolicy(
        base_tools=("Read",),
        readonly_tools=(
            "mcp__snowflake__run_snowflake_query",
            "mcp__browser__browser_click",
            "mcp__browser__browser_type",
            "mcp__browser__browser_evaluate",
            "mcp__gmail__*",
            "kubernetes",
            "mcp__browser",
            "Bash", "Edit", "Write", "NotebookEdit",
            "Bash(/opt/wrap/git_read.sh:*)",
        ),
    )
    tools = policy.tool_list_for(_GEN)
    assert tools == (
        "Read", "mcp__snowflake__run_snowflake_query", "Bash(/opt/wrap/git_read.sh:*)",
    )


def test_소유자_턴은_조회_묶음과_무관하다() -> None:
    policy = _policy(owner_tools=("Bash", "mcp__snowflake__*"))
    assert policy.tool_list_for(_OWNER) == ("Read", "Grep", "Glob", "Bash", "mcp__snowflake__*")


def test_읽기전용_턴은_묶음의_조회_MCP만_남긴다() -> None:
    tools = _policy().tool_list_for(_GEN, readonly=True)
    assert "mcp__snowflake__run_snowflake_query" in tools
    assert "mcp__slack__slack_search" in tools
    assert not any(t.startswith("Bash") or "browser" in t for t in tools)


def test_Application이_설정의_조회_묶음을_정책에_연결한다(tmp_path) -> None:
    from test_application import FakeSlackClient, write_profile

    from slack_cli_agent.core.application import Application

    profile = write_profile(
        tmp_path,
        settings={"base_tools": ["Read"], "readonly_tools": ["mcp__snowflake__run_snowflake_query"]},
    )
    app = Application(profile, FakeSlackClient())
    assert "mcp__snowflake__run_snowflake_query" in app.tool_policy().tool_list_for(_GEN)
