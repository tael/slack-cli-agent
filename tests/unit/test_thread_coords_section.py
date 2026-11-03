"""대화 좌표 섹션 (A46).

파일이나 그림을 올리는 도구는 channel 과 thread_ts 를 받는다. 그 값이
프롬프트 어디에도 없으면 모델은 올리는 도구를 못 부르고 파일만 만드는
도구로 돌아섰다(마메치 thread_coords, bot.py:1819). 섹션이 좌표를 적는지,
그리고 파이프라인이 그 좌표를 프롬프트 조립에 넘기는지(배선) 본다.
"""

from __future__ import annotations

from test_pipeline import build_pipeline, make_ctx, ok_response

from slack_cli_agent.auth.principal import Principal, TrustLevel
from slack_cli_agent.prompt.sections import CompositionContext, ThreadCoordsSection

_P = Principal(user_id="U1", channel="C1", trust=TrustLevel.GENERAL, is_direct_message=False)


class Test좌표섹션:
    def test_좌표가_있으면_channel_thread_ts가_들어간다(self) -> None:
        section = ThreadCoordsSection()
        ctx = CompositionContext(principal=_P, channel="C9", thread_ts="1700000009.000900")
        assert section.applies_to(ctx)
        out = section.render(ctx)
        assert "channel=C9" in out
        assert "thread_ts=1700000009.000900" in out

    def test_좌표줄은_답변에_옮기지_말라고_이른다(self) -> None:
        section = ThreadCoordsSection()
        ctx = CompositionContext(principal=_P, channel="C9", thread_ts="1.1")
        assert "옮겨 적지 않는다" in section.render(ctx)

    def test_채널이_없으면_섹션은_빠진다(self) -> None:
        section = ThreadCoordsSection()
        assert not section.applies_to(CompositionContext(principal=_P))


class Test좌표배선:
    def test_파이프라인이_대화_좌표를_프롬프트_조립에_넘긴다(self) -> None:
        pipeline, deps = build_pipeline(responses=[ok_response()])
        pipeline.handle(make_ctx(channel="C9", thread_ts="1700000009.000900"))
        composed = deps["composer"].contexts[0]
        assert composed.channel == "C9"
        assert composed.thread_ts == "1700000009.000900"
