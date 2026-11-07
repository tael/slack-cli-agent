"""렌더만 하고 슬랙에 올리지 않은 Mermaid 도식을 회수하는가 (D1).

마메치 bot.py rendered_without_posting / unposted_render_prompt 이관이다.
렌더 도구는 로컬 PNG 만 만들어 모델은 다 됐다고 답하는데 사람 화면에는
아무것도 없다. 2026-09-22 구치파치가 두 번 렌더하고 한 번도 올리지 않았다.
"""

from __future__ import annotations

from typing import Any

from test_pipeline import build_pipeline, make_ctx, ok_response

from slack_cli_agent.core.unposted_render import (
    NOTICE_UNPOSTED_RENDER,
    rendered_and_posted,
    rendered_without_posting,
    unposted_render_prompt,
)
from slack_cli_agent.engine.base import EngineResponse
from slack_cli_agent.observability.audit import IncidentKind


def tooled(body: str, tools: list[str], session_id: str = "sess-1") -> EngineResponse:
    base = ok_response(body, session_id)
    return EngineResponse(
        ok=base.ok, body=base.body, session_id=base.session_id,
        model_actual=base.model_actual, elapsed=base.elapsed, turns=base.turns,
        usage=base.usage, raw={"tools": tools},
    )


class Test판정:
    def test_codex_표기로_렌더만_했으면_참(self) -> None:
        assert rendered_without_posting(tooled("x", ["mermaid.mermaid_render"]))

    def test_claude_표기도_끝_이름으로_읽는다(self) -> None:
        assert rendered_without_posting(tooled("x", ["mcp__mermaid__mermaid_render"]))

    def test_올리는_도구도_불렀으면_거짓(self) -> None:
        resp = tooled("x", ["mermaid.mermaid_render", "mermaid.mermaid_render_and_post"])
        assert not rendered_without_posting(resp)
        assert rendered_and_posted(resp)

    def test_도구_기록이_없으면_거짓(self) -> None:
        assert not rendered_without_posting(ok_response("x"))
        assert not rendered_and_posted(ok_response("x"))

    def test_도구_기록이_목록이_아니면_거짓(self) -> None:
        resp = tooled("x", [])
        resp = EngineResponse(**{**resp.__dict__, "raw": {"tools": "mermaid_render"}})
        assert not rendered_without_posting(resp)

    def test_지시문에_좌표와_올리는_도구가_있다(self) -> None:
        text = unposted_render_prompt("C9", "1700.1")
        assert "mermaid_render_and_post" in text
        assert "channel=C9" in text and "thread_ts=1700.1" in text


def _incidents(parts: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        i for i in parts["audit"].incidents
        if i["kind"] == IncidentKind.RENDER_WITHOUT_POST.value
    ]


class Test파이프라인:
    def test_렌더만_하면_같은_세션으로_한_번_다시_돌려_올리게_한다(self) -> None:
        pipeline, parts = build_pipeline(responses=[
            tooled("도식을 그렸습니다", ["mermaid.mermaid_render"]),
            tooled("올렸습니다", ["mermaid.mermaid_render_and_post"]),
        ])
        pipeline.handle(make_ctx(channel="C1", thread_ts="1700000001.000100"))
        calls = parts["runner"].calls
        assert len(calls) == 2
        assert calls[1].resume is True
        assert calls[1].session_id == "sess-1"
        assert "mermaid_render_and_post" in calls[1].prompt
        assert "channel=C1" in calls[1].prompt
        # 같은 도구 범위로 다시 돈다. 비소유자 턴이 권한을 더 얻지 않는다.
        assert calls[1].tools == calls[0].tools
        posted = parts["publisher"].posted[-1]["text"]
        assert posted == "도식을 그렸습니다"
        assert _incidents(parts)[0]["recovered"] is True

    def test_다시_돌려도_못_올리면_본문에_사실을_덧붙인다(self) -> None:
        pipeline, parts = build_pipeline(responses=[
            tooled("도식을 그렸습니다", ["mermaid.mermaid_render"]),
            tooled("다시 그렸습니다", ["mermaid.mermaid_render"]),
        ])
        pipeline.handle(make_ctx())
        assert len(parts["runner"].calls) == 2
        posted = parts["publisher"].posted[-1]["text"]
        assert posted.startswith("도식을 그렸습니다")
        assert posted.endswith(NOTICE_UNPOSTED_RENDER)
        assert _incidents(parts)[0]["recovered"] is False

    def test_재실행이_실패해도_사실을_덧붙인다(self) -> None:
        from test_pipeline import fail_response

        pipeline, parts = build_pipeline(responses=[
            tooled("도식을 그렸습니다", ["mermaid.mermaid_render"]),
            fail_response(),
        ])
        pipeline.handle(make_ctx())
        assert parts["publisher"].posted[-1]["text"].endswith(NOTICE_UNPOSTED_RENDER)
        assert _incidents(parts)[0]["recovered"] is False

    def test_올렸으면_다시_돌리지_않는다(self) -> None:
        pipeline, parts = build_pipeline(responses=[
            tooled("올렸습니다", ["mermaid.mermaid_render", "mermaid.mermaid_render_and_post"]),
        ])
        pipeline.handle(make_ctx())
        assert len(parts["runner"].calls) == 1
        assert not _incidents(parts)

    def test_도구_기록이_없는_엔진은_건드리지_않는다(self) -> None:
        pipeline, parts = build_pipeline(responses=[ok_response("답")])
        pipeline.handle(make_ctx())
        assert len(parts["runner"].calls) == 1
        assert parts["publisher"].posted[-1]["text"] == "답"
