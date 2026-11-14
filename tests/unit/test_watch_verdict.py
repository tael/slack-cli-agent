"""게시한 답마다 watch 판정을 감사에 남긴다 (J9).

마메치 대조: `[[NOWATCH]]` 판정률 집계. 모델이 감시가 필요없다고 명시
(`[[NOWATCH]]`)했는지, 감시를 걸었는지, 둘 다 아닌지(missing)를 감사 로그에
남겨 판정률을 집계할 수 있게 한다. 침묵 응답은 게시가 없으므로 제외한다.
"""

from __future__ import annotations

from pathlib import Path

from test_pipeline import build_pipeline, make_ctx, ok_response

from slack_cli_agent.observability.audit import IncidentKind


def _verdicts(deps: dict) -> list[str]:
    return [
        inc["verdict"]
        for inc in deps["audit"].incidents
        if inc["kind"] == IncidentKind.WATCH_VERDICT.value
    ]


class TestWatch판정감사:
    def test_NOWATCH_표식이면_verdict_가_nowatch다(self, tmp_path: Path) -> None:
        pipeline, deps = build_pipeline(
            responses=[ok_response(body="끝났습니다.\n[[NOWATCH]]")], tmp_path=tmp_path
        )
        pipeline.handle(make_ctx())
        assert _verdicts(deps) == ["nowatch"]
        # 표식 자체는 사람 화면에 나가지 않는다.
        assert "[[NOWATCH]]" not in deps["publisher"].posted[0]["text"]

    def test_감시도_표식도_없으면_verdict_가_missing다(self, tmp_path: Path) -> None:
        pipeline, deps = build_pipeline(
            responses=[ok_response(body="그냥 평범한 답변입니다.")], tmp_path=tmp_path
        )
        pipeline.handle(make_ctx())
        assert _verdicts(deps) == ["missing"]

    def test_판정은_게시한_답마다_한_번만_남는다(self, tmp_path: Path) -> None:
        pipeline, deps = build_pipeline(
            responses=[ok_response(body="답변입니다")], tmp_path=tmp_path
        )
        pipeline.handle(make_ctx())
        assert len(_verdicts(deps)) == 1
