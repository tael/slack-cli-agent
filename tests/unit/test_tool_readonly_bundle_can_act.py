"""조회 묶음이 쓰기 관문을 열지 않는다 (A10 리뷰).

ToolSelection.can_act 는 READ_ONLY_TOOLS 와 READ_ONLY_MCP_TOOLS 밖의 이름이
하나라도 있으면 참이다. 조회 래퍼와 metabase·notion 조회 도구는 그 밖에 있어,
묶음이 붙은 비소유자 턴의 can_act 가 참이 됐다. claude·gemini 엔진은 이 값으로
MCP 서버의 쓰기 관문 env(turn_env 의 can_act)를 연다. 마메치는
can_act = is_owner or (operator and not aside) 라 비소유자에게는 늘 거짓이다
(bot.py build_command). 이 파일은 그 경계를 파이프라인과 감시 확인 경로에서 본다.
"""

from __future__ import annotations

from typing import Any

from test_pipeline import build_pipeline, make_ctx, ok_response

from slack_cli_agent.auth.principal import Principal, TrustLevel
from slack_cli_agent.auth.tools import ToolPolicy
from slack_cli_agent.engine.codex import _codex_reads_only
from slack_cli_agent.engine.tool_selection import ToolSelection

_BUNDLE = (
    "mcp__snowflake__run_snowflake_query",
    "mcp__metabase__execute_query",
    "Bash(~/wrap/gh_read.sh:*)",
)


def _policy(**kw: Any) -> ToolPolicy:
    return ToolPolicy(base_tools=("Read", "Grep", "Glob"), readonly_tools=_BUNDLE, **kw)


def test_묶음을_받은_일반_사용자_턴은_쓰기_턴이_아니다() -> None:
    pipeline, parts = build_pipeline(responses=[ok_response()], tool_policy=_policy())

    pipeline.handle(make_ctx(user="U1"))

    tools = parts["runner"].calls[0].tools
    assert "Bash(~/wrap/gh_read.sh:*)" in tools.names
    assert tools.can_act is False


def test_묶음을_받은_봇_턴도_쓰기_턴이_아니다() -> None:
    pipeline, parts = build_pipeline(responses=[ok_response()], tool_policy=_policy())

    pipeline.handle(make_ctx(user="U1", from_bot=True))

    assert parts["runner"].calls[0].tools.can_act is False


def test_소유자_턴은_여전히_쓰기_턴이다() -> None:
    policy = _policy(owner_tools=("Bash",))
    pipeline, parts = build_pipeline(responses=[ok_response()], tool_policy=policy)

    pipeline.handle(make_ctx(user="UOWNER"))

    assert parts["runner"].calls[0].tools.can_act is True


def test_operator_턴은_마메치처럼_쓰기_턴이다() -> None:
    """operator 는 마메치에서 can_act 가 참이다. 받은 도구가 묶음과 겹쳐도
    조회로 셈하지 않는다."""
    policy = _policy(
        operator_level=lambda principal, is_bot=False: "operator",
        operator_tools=("mcp__metabase__execute_query",),
    )
    gen = Principal(user_id="U1", channel="C1", trust=TrustLevel.GENERAL, is_direct_message=False)
    selection = ToolSelection.from_names(policy.tool_list_for(gen), lookups=policy.lookups_for(gen))
    assert selection.can_act is True


def test_읽기전용_턴의_묶음은_codex_를_read_only_로_둔다() -> None:
    """소유자 감시 확인도 묶음을 받는다. 묶음 이름을 쓰기로 셈하면 codex 가
    read-only 대신 프로필 sandbox 로 돈다."""
    owner = Principal(user_id="U0", channel="C1", trust=TrustLevel.OWNER, is_direct_message=False)
    policy = _policy(owner_tools=("Bash",))
    selection = ToolSelection.from_names(
        policy.tool_list_for(owner, readonly=True), lookups=policy.lookups_for(owner, readonly=True),
    )
    assert "Bash(~/wrap/gh_read.sh:*)" in selection.names
    assert selection.can_act is False
    assert _codex_reads_only(selection) is True


def test_감시_확인_턴은_묶음을_받고_쓰기_턴이_아니다(tmp_path, monkeypatch) -> None:
    from test_application import FakeSlackClient, _프롬프트조립대역, write_profile

    from slack_cli_agent.core.application import Application
    from slack_cli_agent.engine.runner import EngineRunner
    from slack_cli_agent.reliability.watchjobs import WatchJob
    from slack_cli_agent.reliability.watchresult import WatchOutcome

    profile = write_profile(tmp_path, settings={"base_tools": ["Read"], "readonly_tools": list(_BUNDLE)})
    app = Application.from_profile(profile, client=FakeSlackClient())
    app._composer = lambda: _프롬프트조립대역()  # type: ignore[method-assign]
    sent: list[Any] = []
    monkeypatch.setattr(EngineRunner, "run", lambda self, engine, request: sent.append(request))

    app._watch_run_check(WatchJob(
        id=1, channel="C1", thread_ts="1.1", condition="배포 확인",
        created_at=0.0, last_run=None, trust=TrustLevel.GENERAL,
    ), WatchOutcome.UNKNOWN)

    assert "Bash(~/wrap/gh_read.sh:*)" in sent[0].tools.names
    assert sent[0].tools.can_act is False
