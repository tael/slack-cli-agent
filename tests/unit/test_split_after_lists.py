"""목록 뒤 문단을 별도 markdown 블록으로 떼는지 본다 (2026-11-10 private-taeil)."""

from slack_cli_agent.render.blocks import BlockBuilder

B = BlockBuilder("봇")


def test_빈줄_뒤_굵은_질문은_따로_뗀다():
    text = "## 질문\n- 상위\n  - 하위\n\n**진행할까요?** 뒤 문장."
    assert B.split_after_lists(text) == ["## 질문\n- 상위\n  - 하위", "**진행할까요?** 뒤 문장."]


def test_빈줄_없이_붙은_문단도_뗀다():
    assert B.split_after_lists("- a\n**질문**") == ["- a", "**질문**"]


def test_들여쓴_이어쓰기는_항목에_남긴다():
    text = "- a\n  이어지는 설명\n- b"
    assert B.split_after_lists(text) == [text]


def test_목록이_없으면_그대로():
    text = "첫 줄\n\n둘째 문단"
    assert B.split_after_lists(text) == [text]


def test_목록_뒤_헤딩_인용_표는_떼지_않는다():
    for tail in ("## 다음", "> 걸린 시간 : 1초", "| a | b |"):
        text = f"- a\n\n{tail}"
        assert B.split_after_lists(text) == [text]


def test_코드펜스_안은_자르지_않는다():
    text = "```\n- a\n본문\n```"
    assert B.split_after_lists(text) == [text]


def test_번호_목록도_같다():
    assert B.split_after_lists("1. a\n2. b\n\n결론") == ["1. a\n2. b", "결론"]


def test_원본_푸터까지_재현():
    text = "- q\n  - r\n\n**진행할까요?** 방식입니다.\n\n> 걸린 시간 : 72초\n> 실행 모델 : x"
    parts = B.split_after_lists(text)
    assert parts == ["- q\n  - r", "**진행할까요?** 방식입니다.\n\n> 걸린 시간 : 72초\n> 실행 모델 : x"]


def test_목록_뒤_조각에_빈줄_표식을_붙인다():
    """분리만으로는 슬랙이 위 불릿에 붙여 그린다. 두 번째 조각부터 ZWSP 줄을 앞에 둔다 (2026-11-10 재발)."""
    from slack_cli_agent.slack.publisher import MessagePublisher

    pub = MessagePublisher.__new__(MessagePublisher)
    pub._blocks = B
    payload = pub._rich_payload("- a\n  - b\n\n**진행할까요?** 뒤", split_note=False)
    texts = [b["text"] for b in payload.blocks]
    assert texts == ["- a\n  - b", "​\n**진행할까요?** 뒤"]
    assert payload.body.startswith("- a")


def test_목록이_없으면_표식도_없다():
    from slack_cli_agent.slack.publisher import MessagePublisher

    pub = MessagePublisher.__new__(MessagePublisher)
    pub._blocks = B
    payload = pub._rich_payload("첫 줄\n\n둘째", split_note=False)
    assert [b["text"] for b in payload.blocks] == ["첫 줄\n\n둘째"]
