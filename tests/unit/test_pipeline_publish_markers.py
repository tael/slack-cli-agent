"""게시 전 표식 정리 배선 — core/pipeline.py 가 strip_unknown_tags 와
resolve_recipient 를 출력 경로에 적용하는지 본다 (B1 배선, gaps J18/D3).

마커 함수 자체의 동작은 test_markers.py·test_markers_recipient.py 가 검증한다.
여기서는 파이프라인이 게시 직전에 그 함수를 실제로 부르는지(배선)만 본다.
"""

from __future__ import annotations

from pathlib import Path

from test_pipeline import build_pipeline, make_ctx, ok_response


class Test게시전_표식정리:
    def test_미선언_표식_단독줄은_사람화면에서_사라진다(self, tmp_path: Path) -> None:
        pipeline, deps = build_pipeline(
            responses=[ok_response(body="처리했습니다.\n[[깜짝표식]]")],
            tmp_path=tmp_path,
        )
        pipeline.handle(make_ctx())
        posted = deps["publisher"].posted[0]["text"]
        assert "[[깜짝표식]]" not in posted
        assert posted == "처리했습니다."

    def test_미선언_표식은_감사에_기록된다(self, tmp_path: Path) -> None:
        pipeline, deps = build_pipeline(
            responses=[ok_response(body="처리했습니다.\n[[깜짝표식]]")],
            tmp_path=tmp_path,
        )
        pipeline.handle(make_ctx())
        kinds = [i["kind"] for i in deps["audit"].incidents]
        assert "unknown_tag" in kinds

    def test_RECIPIENT_표식은_게시본문에_새지_않는다(self, tmp_path: Path) -> None:
        pipeline, deps = build_pipeline(
            responses=[ok_response(body="핵심 한 줄입니다.\n[[RECIPIENT:OWNER]]")],
            tmp_path=tmp_path,
        )
        pipeline.handle(make_ctx())
        posted = deps["publisher"].posted[0]["text"]
        assert "RECIPIENT" not in posted
        assert posted == "핵심 한 줄입니다."

    def test_코드블록_안의_표식은_보존된다(self, tmp_path: Path) -> None:
        body = "표식 예시입니다:\n```\n[[WATCH: 예시]]\n```"
        pipeline, deps = build_pipeline(
            responses=[ok_response(body=body)], tmp_path=tmp_path,
        )
        pipeline.handle(make_ctx())
        posted = deps["publisher"].posted[0]["text"]
        assert "[[WATCH: 예시]]" in posted

    def test_표식_없는_본문은_그대로다(self, tmp_path: Path) -> None:
        pipeline, deps = build_pipeline(
            responses=[ok_response(body="그냥 평범한 답변입니다.")],
            tmp_path=tmp_path,
        )
        pipeline.handle(make_ctx())
        assert deps["publisher"].posted[0]["text"] == "그냥 평범한 답변입니다."
        kinds = [i["kind"] for i in deps["audit"].incidents]
        assert "unknown_tag" not in kinds
