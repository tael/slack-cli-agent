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
from slack_cli_agent.engine.tool_selection import ToolSelection

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


def test_소유자_턴은_조회_묶음과_무관하다_회귀() -> None:
    """수정 전 코드에서도 통과하는 회귀 시험이다. 결함 재현이 아니다."""
    policy = _policy(owner_tools=("Bash",))
    assert ToolSelection.from_names(
        policy.tool_list_for(_OWNER), lookups=policy.lookups_for(_OWNER),
    ).can_act is True


def test_읽기전용_턴도_마메치처럼_조회_묶음을_받는다() -> None:
    """마메치는 부검·디버그·no_act 턴에도 ALLOWED_TOOLS + READ_SHELL_TOOLS 를
    준다(bot.py base_tools). 읽기 전용 턴에서 래퍼와 조회 MCP 를 빼면 감시
    확인이 마메치보다 적은 수단으로 돈다."""
    for who in (_GEN, _OWNER):
        tools = _policy().tool_list_for(who, readonly=True)
        for name in _BUNDLE:
            assert name in tools
        assert not any(t.rsplit("__", 1)[-1] in ("browser_click", "browser_type") for t in tools)


def test_범위_지정_Bash_는_경로를_적은_래퍼만_남긴다() -> None:
    """Bash(curl:*) 같은 허가가 남으면 claude 엔진이 맨 Bash deny 를 풀어
    비소유자 턴에서 그 명령이 돈다. 맨 이름만 거르면 다른 표기가 통과한다."""
    policy = ToolPolicy(
        base_tools=("Read",),
        readonly_tools=(
            "Bash(curl:*)", "Bash(python3:*)", "Bash(sh:*)",
            "Bash(/bin/sh:*)", "Bash(/usr/bin/python3 -c:*)", "Bash(/usr/bin/env python3:*)",
            "Bash(*)", "Bash(/opt/*:*)", "Bash(~/w/../x/run.sh:*)",
            "Edit(/tmp/**)", "Write(/tmp/x)", "NotebookEdit(/tmp/x)",
            "Bash(/opt/wrap/git_read.sh:*)", "Bash(~/w/mbs.sh sql:*)",
        ),
    )
    assert policy.tool_list_for(_GEN) == (
        "Read", "Bash(/opt/wrap/git_read.sh:*)", "Bash(~/w/mbs.sh sql:*)",
    )


def test_Application이_설정의_조회_묶음을_정책에_연결한다(tmp_path) -> None:
    from test_application import FakeSlackClient, write_profile

    from slack_cli_agent.core.application import Application

    profile = write_profile(
        tmp_path,
        settings={"base_tools": ["Read"], "readonly_tools": ["mcp__snowflake__run_snowflake_query"]},
    )
    app = Application(profile, FakeSlackClient())
    assert "mcp__snowflake__run_snowflake_query" in app.tool_policy().tool_list_for(_GEN)


def test_배포_견본의_조회_묶음은_거르는_항목_없이_비소유자에게_붙는다() -> None:
    """운영 프로필은 .gitignore 대상이라 묶음 값을 검토할 곳이 견본뿐이다.
    견본 값이 필터에 걸리면 견본을 따라 쓴 프로필이 조용히 도구를 잃는다."""
    import json
    from pathlib import Path

    견본 = Path(__file__).resolve().parents[2] / "profiles" / "example.example.json"
    names = tuple(json.loads(견본.read_text(encoding="utf-8"))["settings"]["readonly_tools"])
    policy = ToolPolicy(base_tools=("Read", "Grep", "Glob"), readonly_tools=names)
    tools = policy.tool_list_for(_GEN)
    assert all(name in tools for name in names)
    assert ToolSelection.from_names(tools, lookups=policy.lookups_for(_GEN)).can_act is False
