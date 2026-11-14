"""csv 코드블록 -> BOM 포함 파일 변환, `<<<SPLIT>>>` 다중 게시 분할 시험.

마메치 bot.py:6279-6296 ("csv 는 길이와 무관하게 항상 파일로 올린다", 2026-10-18
김태일 지시)과, bot.py:5975-6115 의 `SPLIT_MARKER` 분할을 `render.splitter` 로
옮긴다. 실제 슬랙 업로드(slack/publisher.py, B1 비소유)는 다루지 않는다 --
여기서는 순수 변환 결과(파일명/BOM 바이트/본문에 남는 안내문)만 검증한다.
"""

from __future__ import annotations

import pytest

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.render.blocks import SPLIT_MARKER, BlockBuilder
from slack_cli_agent.render.splitter import ContentSplitter, CsvFilePayload, extract_csv_blocks


@pytest.fixture
def settings() -> RuntimeSettings:
    return RuntimeSettings()


@pytest.fixture
def blocks() -> BlockBuilder:
    return BlockBuilder(bot_display_name="테스트봇")


@pytest.fixture
def splitter(settings: RuntimeSettings, blocks: BlockBuilder) -> ContentSplitter:
    return ContentSplitter(settings, blocks)


class TestExtractCsvBlocks:
    def test_csv_블록을_BOM_포함_파일로_바꾼다(self) -> None:
        text = "결과입니다.\n```csv\n이름,나이\n철수,10\n```"
        out, payloads = extract_csv_blocks(text)
        assert len(payloads) == 1
        payload = payloads[0]
        assert isinstance(payload, CsvFilePayload)
        assert payload.filename.endswith(".csv")
        assert payload.content.startswith(b"\xef\xbb\xbf")
        assert payload.content.decode("utf-8-sig") == "이름,나이\n철수,10\n"
        assert "```csv" not in out
        assert payload.filename in out

    def test_짧은_csv도_길이와_무관하게_변환한다(self) -> None:
        """짧은 코드블록은 그냥 두는 일반 스니펫 규칙과 달리, csv 는
        길이에 상관없이 항상 파일로 바뀐다 (bot.py:6279-6283)."""
        text = "```csv\na,b\n```"
        out, payloads = extract_csv_blocks(text)
        assert len(payloads) == 1
        assert "```" not in out

    def test_csv가_아닌_코드블록은_그대로_둔다(self) -> None:
        text = "쿼리입니다.\n```sql\nSELECT 1\n```"
        out, payloads = extract_csv_blocks(text)
        assert payloads == []
        assert out == text

    def test_여러_csv_블록은_각각_다른_파일명을_받는다(self) -> None:
        text = "```csv\na,b\n```\n그리고\n```csv\nc,d\n```"
        _out, payloads = extract_csv_blocks(text)
        assert len(payloads) == 2
        assert payloads[0].filename != payloads[1].filename
        assert len({p.filename for p in payloads}) == 2

    def test_csv와_다른_언어가_섞이면_csv만_바뀐다(self) -> None:
        text = "```python\nprint(1)\n```\n```csv\na,b\n1,2\n```"
        out, payloads = extract_csv_blocks(text)
        assert len(payloads) == 1
        assert "```python" in out
        assert "```csv" not in out

    def test_표식이_없으면_빈_목록을_돌려준다(self) -> None:
        out, payloads = extract_csv_blocks("그냥 답변입니다")
        assert out == "그냥 답변입니다"
        assert payloads == []


class TestSplitMarker:
    """<<<SPLIT>>> 는 한 응답을 여러 게시물로 나누는 경계다.

    양쪽 조각이 짧으면 merge_tiny 가 다시 합친다 -- 이미 locked 된 동작
    (test_render.py::test_마커로_나눈_자리를_존중해_분할한다). 여기서는 둘
    다 합치기 문턱(floor=500자)을 넘겨, 실제로 별도 게시물 여러 개로
    나뉘는 경로를 검증한다.
    """

    def test_split_마커로_여러_조각으로_나뉜다(self, splitter: ContentSplitter) -> None:
        first, second = "가" * 600, "나" * 600
        text = f"{first}{SPLIT_MARKER}{second}"
        chunks = splitter.split_for_blocks(text)
        assert len(chunks) == 2
        assert chunks[0] == first
        assert chunks[1] == second

    def test_나뉜_조각에_마커_문자열이_남지_않는다(self, splitter: ContentSplitter) -> None:
        first, second, third = "가" * 600, "나" * 600, "다" * 600
        text = f"{first}{SPLIT_MARKER}{second}{SPLIT_MARKER}{third}"
        chunks = splitter.split_for_blocks(text)
        assert len(chunks) == 3
        assert all(SPLIT_MARKER not in c for c in chunks)

    def test_마커가_없으면_한_조각이다(self, splitter: ContentSplitter) -> None:
        chunks = splitter.split_for_blocks("그냥 답변입니다")
        assert chunks == ["그냥 답변입니다"]
