"""지식 파일과 응답 아카이브에 쓰는 슬러그 산출 시험.

원본 `bot.py:131` 의 `channel_slug` 에 해당한다. 규칙 셋이 순서대로다 -
DM 은 한 이름을 공유하고, `knowledge` 가 있으면 그 채널의 지식을 함께 쓰며,
그 다음이 채널 이름이다. 이식본은 이 산출이 두 자리에 `config.name if config
else channel` 로 박혀 있어 앞의 둘이 없었다 (sca-l5sm, sca-pox7).
"""

from __future__ import annotations

from slack_cli_agent.config.channel import ChannelConfig, channel_slug


def _config(**data):
    return ChannelConfig.from_dict("C1", data)


class Test슬러그_산출:
    def test_DM_은_한_이름을_공유한다(self) -> None:
        assert channel_slug("D0AAA", None) == "dm"

    def test_DM_은_설정이_있어도_공유한다(self) -> None:
        """DM 을 등록부에 넣어도 개인 대화마다 지식 파일이 갈리면 안 된다."""
        assert channel_slug("D0AAA", _config(name="누구와의대화")) == "dm"

    def test_knowledge_가_이름보다_앞선다(self) -> None:
        assert channel_slug("C1", _config(name="개발-비공개", knowledge="개발")) == "개발"

    def test_knowledge_가_없으면_이름을_쓴다(self) -> None:
        assert channel_slug("C1", _config(name="개발")) == "개발"

    def test_설정이_없으면_채널_ID_를_쓴다(self) -> None:
        assert channel_slug("C1", None) == "C1"

    def test_이름도_없으면_채널_ID_를_쓴다(self) -> None:
        assert channel_slug("C1", _config(mode="default")) == "C1"


class Test_knowledge_는_별칭_하나다:
    def test_문자열을_그대로_읽는다(self) -> None:
        assert _config(knowledge="개발").knowledge == "개발"

    def test_목록으로_적혀_있으면_첫_항목을_쓴다(self) -> None:
        """옮기는 과정에서 tuple 로 파싱하게 돼 있었다. 원본은 슬러그
        하나를 가리키므로 되돌리되, 이미 적힌 목록을 오류로 만들지 않는다."""
        assert _config(knowledge=["개발", "무시됨"]).knowledge == "개발"

    def test_빈_목록은_없는_것과_같다(self) -> None:
        assert _config(knowledge=[]).knowledge == ""
