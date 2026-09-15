"""json.dumps 가 못 받는 값이 요청을 죽이지 않게 한다(sca-btw).

실측 — state.db 의 jobs.id=24 에 'Object of type frozenset is not JSON
serializable' 이 남아 있고, 그 요청은 감시 큐에 등록조차 안 됐다.

같은 가드가 observability/audit.py 에만 있었다. 두 자리에 따로 두면 한쪽만
고치게 되므로 한 곳에서 가져다 쓴다.
"""

from __future__ import annotations

import json
import traceback
from pathlib import Path

import pytest
from test_pipeline import build_pipeline, make_ctx

from slack_cli_agent.core.context import RequestContext
from slack_cli_agent.core.jsonsafe import dump_json, json_safe


def _문맥(**extra: object) -> RequestContext:
    return RequestContext(channel="C1", user="U1", ts="1.1", thread_ts="1.1", text="본문", extra=extra)


class Test요청_문맥_직렬화:
    def test_frozenset_이_들어와도_직렬화한다(self) -> None:
        되읽은것 = json.loads(_문맥(태그=frozenset({"가", "나"})).to_json())
        assert sorted(되읽은것["extra"]["태그"]) == ["가", "나"]

    def test_되읽으면_extra_까지_같은_문맥이_된다(self) -> None:
        """집합은 JSON 에 형식이 없어 list 로 돌아온다. 대기줄이 다시 꺼낼 때
        보는 것이 이 형태다 (코덱스 리뷰).
        """
        되읽은것 = RequestContext.from_json(_문맥(태그=frozenset({"가"})).to_json())
        assert 되읽은것 == _문맥(태그=["가"])

    def test_보통_값은_그대로_간다(self) -> None:
        되읽은것 = json.loads(_문맥(수=3, 말="값").to_json())
        assert 되읽은것["extra"] == {"수": 3, "말": "값"}

    def test_문맥의_기본_모습이_안_바뀐다(self) -> None:
        되읽은것 = json.loads(
            RequestContext(channel="C1", user="U1", ts="1.1", thread_ts="1.1", text="본문").to_json()
        )
        assert 되읽은것["files"] == []
        assert 되읽은것["extra"] == {}


class Test공용_가드:
    def test_집합을_정렬된_목록으로_바꾼다(self) -> None:
        assert json_safe({"a", "b"}) == ["a", "b"]

    def test_모르는_객체는_문자열로_바꾼다(self) -> None:
        class 무엇: ...
        assert isinstance(json_safe(무엇()), str)

    def test_순환_참조를_표시로_바꾼다(self) -> None:
        안쪽: dict[str, object] = {}
        안쪽["자기"] = 안쪽
        assert json_safe(안쪽) == {"자기": "<순환 참조>"}

    def test_문자열이_아닌_키도_받는다(self) -> None:
        assert json_safe({1: "값"}) == {"1": "값"}

    def test_dump_json_은_보통_값에_가드를_안_쓴다(self) -> None:
        assert dump_json({"가": 1}) == '{"가": 1}'

    def test_dump_json_이_못_받는_값도_낸다(self) -> None:
        assert json.loads(dump_json({"가": frozenset({"나"})})) == {"가": ["나"]}


class Test최상위_예외_기록:
    """사유 문자열만 남기면 다음 사고에서도 어디서 났는지 못 짚는다.

    sca-btw 의 frozenset 오류가 바로 그랬다. 다른 하위 except 절은
    log.exception 을 쓰는데 최상위만 str(exc) 를 남겼다.
    """

    def test_요청_처리가_죽으면_트레이스백을_남긴다(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        pipeline, _ = build_pipeline(responses=[], tmp_path=tmp_path)
        pipeline._mark_processing = _터지게()  # type: ignore[method-assign]
        with caplog.at_level("ERROR"):
            결과 = pipeline.handle(make_ctx())
        assert not 결과.ok
        기록 = [r for r in caplog.records if r.exc_info]
        assert 기록, "트레이스백이 붙은 기록이 없다"
        # 사유 문자열이 아니라 예외가 난 자리를 봐야 한다.
        찍힌것 = 기록[0]
        assert 찍힌것.exc_info is not None
        새긴것 = 찍힌것.exc_info[1]
        assert isinstance(새긴것, RuntimeError)
        assert "던진다" in "".join(traceback.format_exception(*찍힌것.exc_info))


def _터지게():
    def 던진다(_ctx: object) -> None:
        raise RuntimeError("터졌다")

    return 던진다
