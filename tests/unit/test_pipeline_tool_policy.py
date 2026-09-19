"""도구 허용 목록이 요청까지 전달되는지 본다.

ToolPolicy 는 만들어져 있고 단위 시험도 있었지만 RequestPipeline 이 그것을
부르지 않아 EngineRequest.allowed_tools 가 늘 비어 있었다. claude 엔진은 그
목록을 `--allowedTools` 로 그대로 넘기므로 비면 도구를 하나도 못 쓴다.
"""

from __future__ import annotations

from test_pipeline import build_pipeline, make_ctx, ok_response

from slack_cli_agent.auth.tools import ToolPolicy
from slack_cli_agent.config.channel import ChannelConfig


def test_일반_사용자는_기본_도구만_받는다() -> None:
    policy = ToolPolicy(base_tools=("Read", "Grep"), owner_tools=("Write", "Edit"))
    pipeline, parts = build_pipeline(responses=[ok_response()], tool_policy=policy)

    pipeline.handle(make_ctx(user="U1"))

    assert parts["runner"].calls[0].tools.names == ("Read", "Grep")


def test_소유자는_소유자_도구까지_받는다() -> None:
    policy = ToolPolicy(base_tools=("Read", "Grep"), owner_tools=("Write", "Edit"))
    pipeline, parts = build_pipeline(responses=[ok_response()], tool_policy=policy)

    pipeline.handle(make_ctx(user="UOWNER"))

    assert parts["runner"].calls[0].tools.names == ("Read", "Grep", "Write", "Edit")


def test_채널에서_스킬을_켜면_Skill_이_붙는다() -> None:
    policy = ToolPolicy(base_tools=("Read",))
    pipeline, parts = build_pipeline(
        responses=[ok_response()],
        channels={"C1": ChannelConfig(channel_id="C1", name="테스트", skills=True)},
        tool_policy=policy,
    )

    pipeline.handle(make_ctx(user="U1"))

    assert parts["runner"].calls[0].tools.names == ("Read", "Skill")


def test_정책이_없으면_도구_목록이_비어_있다() -> None:
    pipeline, parts = build_pipeline(responses=[ok_response()])

    pipeline.handle(make_ctx(user="U1"))

    assert parts["runner"].calls[0].tools.names == ()


def test_Application_이_설정의_도구를_정책으로_조립한다(tmp_path) -> None:
    """부품을 만든 것과 조립에 연결한 것은 다르다. ToolPolicy 는 예전에도
    있었지만 Application 이 안 불러 도구가 하나도 안 실렸다."""
    from test_application import FakeSlackClient, write_profile

    from slack_cli_agent.auth.principal import Principal, TrustLevel
    from slack_cli_agent.core.application import Application

    profile = write_profile(
        tmp_path,
        settings={"base_tools": ["Read", "Grep"], "owner_tools": ["Write", "Edit"]},
    )
    app = Application(profile, FakeSlackClient())

    policy = app.tool_policy()
    owner = Principal(user_id="UOWNER", channel="C1", trust=TrustLevel.OWNER, is_direct_message=False)
    general = Principal(user_id="U1", channel="C1", trust=TrustLevel.GENERAL, is_direct_message=False)

    assert policy.tool_list_for(general) == ("Read", "Grep")
    assert policy.tool_list_for(owner) == ("Read", "Grep", "Write", "Edit")
    # 정책을 만든 것과 파이프라인에 넘긴 것은 다르다
    assert app.pipeline().tool_policy is policy


def test_Application_이_채널별_사용자_도구를_정책에_연결한다(tmp_path) -> None:
    """표를 읽는 코드가 있어도 조립이 안 붙으면 채널 설정이 아무 효과가 없다."""
    import json

    from test_application import FakeSlackClient, write_profile

    from slack_cli_agent.auth.principal import Principal, TrustLevel
    from slack_cli_agent.core.application import Application

    profile = write_profile(tmp_path, settings={"base_tools": ["Read"], "owner_tools": []})
    profile.paths.channels.parent.mkdir(parents=True, exist_ok=True)
    profile.paths.channels.write_text(
        json.dumps({"C1": {"user_tools": {"U1": ["Bash(ops_ctl.sh:*)"]}}}, ensure_ascii=False),
        encoding="utf-8",
    )
    app = Application(profile, FakeSlackClient())

    general = Principal(
        user_id="U1", channel="C1", trust=TrustLevel.GENERAL, is_direct_message=False
    )

    assert app.tool_policy().tool_list_for(general) == ("Read", "Bash(ops_ctl.sh:*)")
