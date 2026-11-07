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


def failing(body: str, tools: list[str], errors: list[str]) -> EngineResponse:
    resp = tooled(body, tools)
    return EngineResponse(**{**resp.__dict__, "raw": {"tools": tools, "tool_errors": errors}})


class Test실패한_호출:
    """codex 는 status=failed 인 MCP 호출도 tools 에 넣고 tool_errors 에 다시 적는다."""

    def test_게시가_거부됐으면_올린_것으로_보지_않는다(self) -> None:
        resp = failing(
            "x", ["mermaid.mermaid_render", "mermaid.mermaid_render_and_post"],
            ["mermaid.mermaid_render_and_post : POST_CHANNEL 밖이다"],
        )
        assert not rendered_and_posted(resp)
        assert rendered_without_posting(resp)

    def test_같은_이름_두_번_중_한_번만_실패하면_올린_것이다(self) -> None:
        resp = failing(
            "x", ["mermaid.mermaid_render_and_post", "mermaid.mermaid_render_and_post"],
            ["mermaid.mermaid_render_and_post : 실패"],
        )
        assert rendered_and_posted(resp)

    def test_렌더가_실패했으면_그린_것이_아니다(self) -> None:
        resp = failing("x", ["mermaid.mermaid_render"], ["mermaid.mermaid_render : 문법 오류"])
        assert not rendered_without_posting(resp)

    def test_첫_턴_게시_실패면_다시_돌리고_재실행도_실패하면_알린다(self) -> None:
        pipeline, parts = build_pipeline(responses=[
            failing(
                "도식을 올렸습니다",
                ["mermaid.mermaid_render", "mermaid.mermaid_render_and_post"],
                ["mermaid.mermaid_render_and_post : 거부"],
            ),
            failing(
                "올렸습니다", ["mermaid.mermaid_render_and_post"],
                ["mermaid.mermaid_render_and_post : 거부"],
            ),
        ])
        pipeline.handle(make_ctx())
        assert len(parts["runner"].calls) == 2
        assert parts["publisher"].posted[-1]["text"].endswith(NOTICE_UNPOSTED_RENDER)
        assert _incidents(parts)[0]["recovered"] is False


class Test재실행과_함께_판정:
    """늦은 추가말 재실행이나 가드 재작성이 돈 턴은 그 호출의 도구까지 함께 본다."""

    def test_추가말_재실행이_올렸으면_다시_돌리지_않는다(self) -> None:
        from test_pipeline import Fake늦은추가말

        pipeline, parts = build_pipeline(
            responses=[
                tooled("도식을 그렸습니다", ["mermaid.mermaid_render"]),
                tooled("추가 말 반영해 올렸습니다", ["mermaid.mermaid_render_and_post"]),
                tooled("또 올렸습니다", ["mermaid.mermaid_render_and_post"]),
            ],
            late_addendum=Fake늦은추가말("이것도 그려줘", "1700000009.000000"),
        )
        pipeline.handle(make_ctx())
        assert len(parts["runner"].calls) == 2
        assert not _incidents(parts)

    def test_추가말_재실행이_렌더만_했으면_회수한다(self) -> None:
        from test_pipeline import Fake늦은추가말

        pipeline, parts = build_pipeline(
            responses=[
                ok_response("답"),
                tooled("그렸습니다", ["mermaid.mermaid_render"]),
                tooled("올렸습니다", ["mermaid.mermaid_render_and_post"]),
            ],
            late_addendum=Fake늦은추가말("그려줘", "1700000009.000000"),
        )
        pipeline.handle(make_ctx())
        assert len(parts["runner"].calls) == 3
        assert _incidents(parts)[0]["recovered"] is True


class Test대체엔진_턴:
    def test_세션을_이을_수_없으면_다시_돌리지_않고_알린다(self) -> None:
        from dataclasses import replace
        from types import SimpleNamespace

        pipeline, parts = build_pipeline(responses=[
            replace(tooled("도식을 그렸습니다", ["mermaid.mermaid_render"]), engine="codex"),
            tooled("올렸습니다", ["mermaid.mermaid_render_and_post"]),
        ])
        pipeline._engine.secondary = SimpleNamespace(name="codex")  # type: ignore[attr-defined]
        pipeline.handle(make_ctx())
        assert len(parts["runner"].calls) == 1
        assert parts["publisher"].posted[-1]["text"].endswith(NOTICE_UNPOSTED_RENDER)
        incident = _incidents(parts)[0]
        assert incident["recovered"] is False and incident["rerun"] is False

    def test_1차_엔진_턴은_다시_돌린다(self) -> None:
        from dataclasses import replace
        from types import SimpleNamespace

        pipeline, parts = build_pipeline(responses=[
            replace(tooled("도식을 그렸습니다", ["mermaid.mermaid_render"]), engine="claude"),
            tooled("올렸습니다", ["mermaid.mermaid_render_and_post"]),
        ])
        pipeline._engine.secondary = SimpleNamespace(name="codex")  # type: ignore[attr-defined]
        pipeline.handle(make_ctx())
        assert len(parts["runner"].calls) == 2


class Test물음삭제:
    def test_물음이_지워졌으면_다시_돌려_올리지_않는다(self) -> None:
        from test_pipeline import Fake물음생존

        pipeline, parts = build_pipeline(
            responses=[
                tooled("도식을 그렸습니다", ["mermaid.mermaid_render"]),
                tooled("올렸습니다", ["mermaid.mermaid_render_and_post"]),
            ],
            origin_alive=Fake물음생존(False),
        )
        pipeline.handle(make_ctx())
        assert len(parts["runner"].calls) == 1
        assert not parts["publisher"].posted
