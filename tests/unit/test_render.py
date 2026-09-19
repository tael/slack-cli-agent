"""render/ 패키지 특성화 테스트.

원본 bot.py 의 to_mrkdwn, tables_to_bullets, chunk, md_chunks,
fit_chunk, split_for_blocks, merge_tiny, verify_chunks, safe_fallback,
separate_tables, blocks_rejected, clean_markers, preview, split_context 를 옮긴
MarkdownConverter, ContentSplitter, SplitVerifier, BlockBuilder 를 검증한다.

기대값은 원본 함수를 실제로 실행해서 얻었다 (2026-09-14, /tmp/oracle_funcs.py 로
AST 추출 후 실행). 손으로 짐작한 값이 아니다.
"""

from __future__ import annotations

import pytest

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.render.blocks import BlockBuilder
from slack_cli_agent.render.markdown import MarkdownConverter
from slack_cli_agent.render.splitter import ContentSplitter
from slack_cli_agent.render.verifier import SplitVerifier


@pytest.fixture
def settings() -> RuntimeSettings:
    return RuntimeSettings()


@pytest.fixture
def converter() -> MarkdownConverter:
    return MarkdownConverter()


@pytest.fixture
def blocks() -> BlockBuilder:
    return BlockBuilder(bot_display_name="테스트봇")


@pytest.fixture
def splitter(settings: RuntimeSettings, blocks: BlockBuilder) -> ContentSplitter:
    return ContentSplitter(settings, blocks)


@pytest.fixture
def verifier(settings: RuntimeSettings) -> SplitVerifier:
    return SplitVerifier(settings)


class TestMarkdownConverter:
    def test_이중별표_헤딩_링크_변환하고_코드블록안은_보존한다(self, converter: MarkdownConverter) -> None:
        text = '**굵게** 와 *기울임아님* 그리고 __밑줄__ 과 ***굵은기울임***\n# 헤딩1\n## 헤딩2\n• 점\n---\n[링크](https://example.com/a)\n```\n**코드안에서는안바뀜**\n```'
        assert converter.to_mrkdwn(text) == '*굵게* 와 *기울임아님* 그리고 _밑줄_ 과 *굵은기울임*\n*헤딩1*\n*헤딩2*\n- 점\n\n<https://example.com/a|링크>\n```\n**코드안에서는안바뀜**\n```'

    def test_표를_불릿으로_편다(self, converter: MarkdownConverter) -> None:
        text = '머리말\n| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n꼬리말'
        assert converter.tables_to_bullets(text) == '머리말\n- 1 2\n- 3 4\n꼬리말'


class TestContentSplitter:
    def test_상한_이내면_그대로_한_조각이다(self, splitter: ContentSplitter) -> None:
        assert splitter.chunk('short text') == ['short text']

    def test_긴_텍스트는_줄_경계로_자른다(self, splitter: ContentSplitter) -> None:
        text = 'line0-xxxxxxxxxxxxxxxxxxxx\nline1-xxxxxxxxxxxxxxxxxxxx\nline2-xxxxxxxxxxxxxxxxxxxx\nline3-xxxxxxxxxxxxxxxxxxxx\nline4-xxxxxxxxxxxxxxxxxxxx\nline5-xxxxxxxxxxxxxxxxxxxx\nline6-xxxxxxxxxxxxxxxxxxxx\nline7-xxxxxxxxxxxxxxxxxxxx\nline8-xxxxxxxxxxxxxxxxxxxx\nline9-xxxxxxxxxxxxxxxxxxxx'
        assert splitter.chunk(text, 60) == ['line0-xxxxxxxxxxxxxxxxxxxx\nline1-xxxxxxxxxxxxxxxxxxxx', 'line2-xxxxxxxxxxxxxxxxxxxx\nline3-xxxxxxxxxxxxxxxxxxxx', 'line4-xxxxxxxxxxxxxxxxxxxx\nline5-xxxxxxxxxxxxxxxxxxxx', 'line6-xxxxxxxxxxxxxxxxxxxx\nline7-xxxxxxxxxxxxxxxxxxxx', 'line8-xxxxxxxxxxxxxxxxxxxx\nline9-xxxxxxxxxxxxxxxxxxxx']

    def test_표_코드블록_인용_목록_경계에서만_덩어리를_끊는다(self, splitter: ContentSplitter) -> None:
        text = '문단1\n문단2\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n```python\nprint(1)\n```\n\n> 인용1\n> 인용2\n\n- 목록1\n- 목록2\n\n그냥문단'
        assert splitter.md_chunks(text) == ['문단1\n문단2\n\n| a | b |\n|---|---|\n| 1 | 2 |\n', '```python\nprint(1)\n```', '\n> 인용1\n> 인용2\n', '- 목록1\n- 목록2\n', '그냥문단']

    def test_표_조각마다_열_이름_행을_다시_단다(self, splitter: ContentSplitter) -> None:
        chunk_text = '| a | b |\n|---|---|\n| 0 | 0 |\n| 1 | 2 |\n| 2 | 4 |\n| 3 | 6 |\n| 4 | 8 |\n| 5 | 10 |\n| 6 | 12 |\n| 7 | 14 |\n| 8 | 16 |\n| 9 | 18 |\n| 10 | 20 |\n| 11 | 22 |\n| 12 | 24 |\n| 13 | 26 |\n| 14 | 28 |\n| 15 | 30 |\n| 16 | 32 |\n| 17 | 34 |\n| 18 | 36 |\n| 19 | 38 |'
        assert splitter.fit_chunk(chunk_text, 60) == ['| a | b |\n|---|---|\n| 0 | 0 |\n| 1 | 2 |\n| 2 | 4 |\n| 3 | 6 |', '| a | b |\n|---|---|\n| 4 | 8 |\n| 5 | 10 |\n| 6 | 12 |', '| a | b |\n|---|---|\n| 7 | 14 |\n| 8 | 16 |\n| 9 | 18 |', '| a | b |\n|---|---|\n| 10 | 20 |\n| 11 | 22 |\n| 12 | 24 |', '| a | b |\n|---|---|\n| 13 | 26 |\n| 14 | 28 |\n| 15 | 30 |', '| a | b |\n|---|---|\n| 16 | 32 |\n| 17 | 34 |\n| 18 | 36 |', '| a | b |\n|---|---|\n| 19 | 38 |']

    def test_코드블록_조각마다_펜스를_닫고_다시_연다(self, splitter: ContentSplitter) -> None:
        chunk_text = '```python\nx = 0\nx = 1\nx = 2\nx = 3\nx = 4\nx = 5\nx = 6\nx = 7\nx = 8\nx = 9\nx = 10\nx = 11\nx = 12\nx = 13\nx = 14\nx = 15\nx = 16\nx = 17\nx = 18\nx = 19\n```'
        assert splitter.fit_chunk(chunk_text, 60) == ['```python\nx = 0\nx = 1\nx = 2\nx = 3\nx = 4\nx = 5\nx = 6\n```', '```python\nx = 7\nx = 8\nx = 9\nx = 10\nx = 11\nx = 12\nx = 13\n```', '```python\nx = 14\nx = 15\nx = 16\nx = 17\nx = 18\nx = 19\n```']

    def test_인용_조각은_줄_경계로_나눈다(self, splitter: ContentSplitter) -> None:
        chunk_text = '> line 0\n> line 1\n> line 2\n> line 3\n> line 4\n> line 5\n> line 6\n> line 7\n> line 8\n> line 9\n> line 10\n> line 11\n> line 12\n> line 13\n> line 14\n> line 15\n> line 16\n> line 17\n> line 18\n> line 19'
        assert splitter.fit_chunk(chunk_text, 60) == ['> line 0\n> line 1\n> line 2\n> line 3\n> line 4\n> line 5', '> line 6\n> line 7\n> line 8\n> line 9\n> line 10\n> line 11', '> line 12\n> line 13\n> line 14\n> line 15\n> line 16\n> line 17', '> line 18\n> line 19']

    def test_줄바꿈_없는_단일_긴_줄은_글자수로_자른다(self, splitter: ContentSplitter) -> None:
        chunk_text = 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
        assert splitter.fit_chunk(chunk_text, 100) == ['xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx', 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx', 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx']

    def test_마커로_나눈_자리를_존중해_분할한다(self, splitter: ContentSplitter) -> None:
        text = '첫 부분입니다.\n<<<SPLIT>>>\n둘째 부분입니다.'
        assert splitter.split_for_blocks(text, 12000) == ['첫 부분입니다.\n둘째 부분입니다.']

    def test_상한을_넘으면_기계적으로_쪼갠다(self, splitter: ContentSplitter) -> None:
        text = '이것은 0번째 줄\n이것은 1번째 줄\n이것은 2번째 줄\n이것은 3번째 줄\n이것은 4번째 줄\n이것은 5번째 줄\n이것은 6번째 줄\n이것은 7번째 줄\n이것은 8번째 줄\n이것은 9번째 줄\n이것은 10번째 줄\n이것은 11번째 줄\n이것은 12번째 줄\n이것은 13번째 줄\n이것은 14번째 줄\n이것은 15번째 줄\n이것은 16번째 줄\n이것은 17번째 줄\n이것은 18번째 줄\n이것은 19번째 줄\n이것은 20번째 줄\n이것은 21번째 줄\n이것은 22번째 줄\n이것은 23번째 줄\n이것은 24번째 줄\n이것은 25번째 줄\n이것은 26번째 줄\n이것은 27번째 줄\n이것은 28번째 줄\n이것은 29번째 줄\n이것은 30번째 줄\n이것은 31번째 줄\n이것은 32번째 줄\n이것은 33번째 줄\n이것은 34번째 줄\n이것은 35번째 줄\n이것은 36번째 줄\n이것은 37번째 줄\n이것은 38번째 줄\n이것은 39번째 줄\n이것은 40번째 줄\n이것은 41번째 줄\n이것은 42번째 줄\n이것은 43번째 줄\n이것은 44번째 줄\n이것은 45번째 줄\n이것은 46번째 줄\n이것은 47번째 줄\n이것은 48번째 줄\n이것은 49번째 줄\n이것은 50번째 줄\n이것은 51번째 줄\n이것은 52번째 줄\n이것은 53번째 줄\n이것은 54번째 줄\n이것은 55번째 줄\n이것은 56번째 줄\n이것은 57번째 줄\n이것은 58번째 줄\n이것은 59번째 줄\n이것은 60번째 줄\n이것은 61번째 줄\n이것은 62번째 줄\n이것은 63번째 줄\n이것은 64번째 줄\n이것은 65번째 줄\n이것은 66번째 줄\n이것은 67번째 줄\n이것은 68번째 줄\n이것은 69번째 줄\n이것은 70번째 줄\n이것은 71번째 줄\n이것은 72번째 줄\n이것은 73번째 줄\n이것은 74번째 줄\n이것은 75번째 줄\n이것은 76번째 줄\n이것은 77번째 줄\n이것은 78번째 줄\n이것은 79번째 줄\n이것은 80번째 줄\n이것은 81번째 줄\n이것은 82번째 줄\n이것은 83번째 줄\n이것은 84번째 줄\n이것은 85번째 줄\n이것은 86번째 줄\n이것은 87번째 줄\n이것은 88번째 줄\n이것은 89번째 줄\n이것은 90번째 줄\n이것은 91번째 줄\n이것은 92번째 줄\n이것은 93번째 줄\n이것은 94번째 줄\n이것은 95번째 줄\n이것은 96번째 줄\n이것은 97번째 줄\n이것은 98번째 줄\n이것은 99번째 줄\n이것은 100번째 줄\n이것은 101번째 줄\n이것은 102번째 줄\n이것은 103번째 줄\n이것은 104번째 줄\n이것은 105번째 줄\n이것은 106번째 줄\n이것은 107번째 줄\n이것은 108번째 줄\n이것은 109번째 줄\n이것은 110번째 줄\n이것은 111번째 줄\n이것은 112번째 줄\n이것은 113번째 줄\n이것은 114번째 줄\n이것은 115번째 줄\n이것은 116번째 줄\n이것은 117번째 줄\n이것은 118번째 줄\n이것은 119번째 줄\n이것은 120번째 줄\n이것은 121번째 줄\n이것은 122번째 줄\n이것은 123번째 줄\n이것은 124번째 줄\n이것은 125번째 줄\n이것은 126번째 줄\n이것은 127번째 줄\n이것은 128번째 줄\n이것은 129번째 줄\n이것은 130번째 줄\n이것은 131번째 줄\n이것은 132번째 줄\n이것은 133번째 줄\n이것은 134번째 줄\n이것은 135번째 줄\n이것은 136번째 줄\n이것은 137번째 줄\n이것은 138번째 줄\n이것은 139번째 줄\n이것은 140번째 줄\n이것은 141번째 줄\n이것은 142번째 줄\n이것은 143번째 줄\n이것은 144번째 줄\n이것은 145번째 줄\n이것은 146번째 줄\n이것은 147번째 줄\n이것은 148번째 줄\n이것은 149번째 줄\n이것은 150번째 줄\n이것은 151번째 줄\n이것은 152번째 줄\n이것은 153번째 줄\n이것은 154번째 줄\n이것은 155번째 줄\n이것은 156번째 줄\n이것은 157번째 줄\n이것은 158번째 줄\n이것은 159번째 줄\n이것은 160번째 줄\n이것은 161번째 줄\n이것은 162번째 줄\n이것은 163번째 줄\n이것은 164번째 줄\n이것은 165번째 줄\n이것은 166번째 줄\n이것은 167번째 줄\n이것은 168번째 줄\n이것은 169번째 줄\n이것은 170번째 줄\n이것은 171번째 줄\n이것은 172번째 줄\n이것은 173번째 줄\n이것은 174번째 줄\n이것은 175번째 줄\n이것은 176번째 줄\n이것은 177번째 줄\n이것은 178번째 줄\n이것은 179번째 줄\n이것은 180번째 줄\n이것은 181번째 줄\n이것은 182번째 줄\n이것은 183번째 줄\n이것은 184번째 줄\n이것은 185번째 줄\n이것은 186번째 줄\n이것은 187번째 줄\n이것은 188번째 줄\n이것은 189번째 줄\n이것은 190번째 줄\n이것은 191번째 줄\n이것은 192번째 줄\n이것은 193번째 줄\n이것은 194번째 줄\n이것은 195번째 줄\n이것은 196번째 줄\n이것은 197번째 줄\n이것은 198번째 줄\n이것은 199번째 줄\n이것은 200번째 줄\n이것은 201번째 줄\n이것은 202번째 줄\n이것은 203번째 줄\n이것은 204번째 줄\n이것은 205번째 줄\n이것은 206번째 줄\n이것은 207번째 줄\n이것은 208번째 줄\n이것은 209번째 줄\n이것은 210번째 줄\n이것은 211번째 줄\n이것은 212번째 줄\n이것은 213번째 줄\n이것은 214번째 줄\n이것은 215번째 줄\n이것은 216번째 줄\n이것은 217번째 줄\n이것은 218번째 줄\n이것은 219번째 줄\n이것은 220번째 줄\n이것은 221번째 줄\n이것은 222번째 줄\n이것은 223번째 줄\n이것은 224번째 줄\n이것은 225번째 줄\n이것은 226번째 줄\n이것은 227번째 줄\n이것은 228번째 줄\n이것은 229번째 줄\n이것은 230번째 줄\n이것은 231번째 줄\n이것은 232번째 줄\n이것은 233번째 줄\n이것은 234번째 줄\n이것은 235번째 줄\n이것은 236번째 줄\n이것은 237번째 줄\n이것은 238번째 줄\n이것은 239번째 줄\n이것은 240번째 줄\n이것은 241번째 줄\n이것은 242번째 줄\n이것은 243번째 줄\n이것은 244번째 줄\n이것은 245번째 줄\n이것은 246번째 줄\n이것은 247번째 줄\n이것은 248번째 줄\n이것은 249번째 줄\n이것은 250번째 줄\n이것은 251번째 줄\n이것은 252번째 줄\n이것은 253번째 줄\n이것은 254번째 줄\n이것은 255번째 줄\n이것은 256번째 줄\n이것은 257번째 줄\n이것은 258번째 줄\n이것은 259번째 줄\n이것은 260번째 줄\n이것은 261번째 줄\n이것은 262번째 줄\n이것은 263번째 줄\n이것은 264번째 줄\n이것은 265번째 줄\n이것은 266번째 줄\n이것은 267번째 줄\n이것은 268번째 줄\n이것은 269번째 줄\n이것은 270번째 줄\n이것은 271번째 줄\n이것은 272번째 줄\n이것은 273번째 줄\n이것은 274번째 줄\n이것은 275번째 줄\n이것은 276번째 줄\n이것은 277번째 줄\n이것은 278번째 줄\n이것은 279번째 줄\n이것은 280번째 줄\n이것은 281번째 줄\n이것은 282번째 줄\n이것은 283번째 줄\n이것은 284번째 줄\n이것은 285번째 줄\n이것은 286번째 줄\n이것은 287번째 줄\n이것은 288번째 줄\n이것은 289번째 줄\n이것은 290번째 줄\n이것은 291번째 줄\n이것은 292번째 줄\n이것은 293번째 줄\n이것은 294번째 줄\n이것은 295번째 줄\n이것은 296번째 줄\n이것은 297번째 줄\n이것은 298번째 줄\n이것은 299번째 줄\n이것은 300번째 줄\n이것은 301번째 줄\n이것은 302번째 줄\n이것은 303번째 줄\n이것은 304번째 줄\n이것은 305번째 줄\n이것은 306번째 줄\n이것은 307번째 줄\n이것은 308번째 줄\n이것은 309번째 줄\n이것은 310번째 줄\n이것은 311번째 줄\n이것은 312번째 줄\n이것은 313번째 줄\n이것은 314번째 줄\n이것은 315번째 줄\n이것은 316번째 줄\n이것은 317번째 줄\n이것은 318번째 줄\n이것은 319번째 줄\n이것은 320번째 줄\n이것은 321번째 줄\n이것은 322번째 줄\n이것은 323번째 줄\n이것은 324번째 줄\n이것은 325번째 줄\n이것은 326번째 줄\n이것은 327번째 줄\n이것은 328번째 줄\n이것은 329번째 줄\n이것은 330번째 줄\n이것은 331번째 줄\n이것은 332번째 줄\n이것은 333번째 줄\n이것은 334번째 줄\n이것은 335번째 줄\n이것은 336번째 줄\n이것은 337번째 줄\n이것은 338번째 줄\n이것은 339번째 줄\n이것은 340번째 줄\n이것은 341번째 줄\n이것은 342번째 줄\n이것은 343번째 줄\n이것은 344번째 줄\n이것은 345번째 줄\n이것은 346번째 줄\n이것은 347번째 줄\n이것은 348번째 줄\n이것은 349번째 줄\n이것은 350번째 줄\n이것은 351번째 줄\n이것은 352번째 줄\n이것은 353번째 줄\n이것은 354번째 줄\n이것은 355번째 줄\n이것은 356번째 줄\n이것은 357번째 줄\n이것은 358번째 줄\n이것은 359번째 줄\n이것은 360번째 줄\n이것은 361번째 줄\n이것은 362번째 줄\n이것은 363번째 줄\n이것은 364번째 줄\n이것은 365번째 줄\n이것은 366번째 줄\n이것은 367번째 줄\n이것은 368번째 줄\n이것은 369번째 줄\n이것은 370번째 줄\n이것은 371번째 줄\n이것은 372번째 줄\n이것은 373번째 줄\n이것은 374번째 줄\n이것은 375번째 줄\n이것은 376번째 줄\n이것은 377번째 줄\n이것은 378번째 줄\n이것은 379번째 줄\n이것은 380번째 줄\n이것은 381번째 줄\n이것은 382번째 줄\n이것은 383번째 줄\n이것은 384번째 줄\n이것은 385번째 줄\n이것은 386번째 줄\n이것은 387번째 줄\n이것은 388번째 줄\n이것은 389번째 줄\n이것은 390번째 줄\n이것은 391번째 줄\n이것은 392번째 줄\n이것은 393번째 줄\n이것은 394번째 줄\n이것은 395번째 줄\n이것은 396번째 줄\n이것은 397번째 줄\n이것은 398번째 줄\n이것은 399번째 줄'
        assert splitter.split_for_blocks(text, 500) == ['이것은 0번째 줄\n이것은 1번째 줄\n이것은 2번째 줄\n이것은 3번째 줄\n이것은 4번째 줄\n이것은 5번째 줄\n이것은 6번째 줄\n이것은 7번째 줄\n이것은 8번째 줄\n이것은 9번째 줄\n이것은 10번째 줄\n이것은 11번째 줄\n이것은 12번째 줄\n이것은 13번째 줄\n이것은 14번째 줄\n이것은 15번째 줄\n이것은 16번째 줄\n이것은 17번째 줄\n이것은 18번째 줄\n이것은 19번째 줄\n이것은 20번째 줄\n이것은 21번째 줄\n이것은 22번째 줄\n이것은 23번째 줄\n이것은 24번째 줄\n이것은 25번째 줄\n이것은 26번째 줄\n이것은 27번째 줄\n이것은 28번째 줄\n이것은 29번째 줄\n이것은 30번째 줄\n이것은 31번째 줄\n이것은 32번째 줄\n이것은 33번째 줄\n이것은 34번째 줄\n이것은 35번째 줄\n이것은 36번째 줄\n이것은 37번째 줄\n이것은 38번째 줄\n이것은 39번째 줄\n이것은 40번째 줄\n이것은 41번째 줄', '이것은 42번째 줄\n이것은 43번째 줄\n이것은 44번째 줄\n이것은 45번째 줄\n이것은 46번째 줄\n이것은 47번째 줄\n이것은 48번째 줄\n이것은 49번째 줄\n이것은 50번째 줄\n이것은 51번째 줄\n이것은 52번째 줄\n이것은 53번째 줄\n이것은 54번째 줄\n이것은 55번째 줄\n이것은 56번째 줄\n이것은 57번째 줄\n이것은 58번째 줄\n이것은 59번째 줄\n이것은 60번째 줄\n이것은 61번째 줄\n이것은 62번째 줄\n이것은 63번째 줄\n이것은 64번째 줄\n이것은 65번째 줄\n이것은 66번째 줄\n이것은 67번째 줄\n이것은 68번째 줄\n이것은 69번째 줄\n이것은 70번째 줄\n이것은 71번째 줄\n이것은 72번째 줄\n이것은 73번째 줄\n이것은 74번째 줄\n이것은 75번째 줄\n이것은 76번째 줄\n이것은 77번째 줄\n이것은 78번째 줄\n이것은 79번째 줄\n이것은 80번째 줄\n이것은 81번째 줄\n이것은 82번째 줄', '이것은 83번째 줄\n이것은 84번째 줄\n이것은 85번째 줄\n이것은 86번째 줄\n이것은 87번째 줄\n이것은 88번째 줄\n이것은 89번째 줄\n이것은 90번째 줄\n이것은 91번째 줄\n이것은 92번째 줄\n이것은 93번째 줄\n이것은 94번째 줄\n이것은 95번째 줄\n이것은 96번째 줄\n이것은 97번째 줄\n이것은 98번째 줄\n이것은 99번째 줄\n이것은 100번째 줄\n이것은 101번째 줄\n이것은 102번째 줄\n이것은 103번째 줄\n이것은 104번째 줄\n이것은 105번째 줄\n이것은 106번째 줄\n이것은 107번째 줄\n이것은 108번째 줄\n이것은 109번째 줄\n이것은 110번째 줄\n이것은 111번째 줄\n이것은 112번째 줄\n이것은 113번째 줄\n이것은 114번째 줄\n이것은 115번째 줄\n이것은 116번째 줄\n이것은 117번째 줄\n이것은 118번째 줄\n이것은 119번째 줄\n이것은 120번째 줄\n이것은 121번째 줄', '이것은 122번째 줄\n이것은 123번째 줄\n이것은 124번째 줄\n이것은 125번째 줄\n이것은 126번째 줄\n이것은 127번째 줄\n이것은 128번째 줄\n이것은 129번째 줄\n이것은 130번째 줄\n이것은 131번째 줄\n이것은 132번째 줄\n이것은 133번째 줄\n이것은 134번째 줄\n이것은 135번째 줄\n이것은 136번째 줄\n이것은 137번째 줄\n이것은 138번째 줄\n이것은 139번째 줄\n이것은 140번째 줄\n이것은 141번째 줄\n이것은 142번째 줄\n이것은 143번째 줄\n이것은 144번째 줄\n이것은 145번째 줄\n이것은 146번째 줄\n이것은 147번째 줄\n이것은 148번째 줄\n이것은 149번째 줄\n이것은 150번째 줄\n이것은 151번째 줄\n이것은 152번째 줄\n이것은 153번째 줄\n이것은 154번째 줄\n이것은 155번째 줄\n이것은 156번째 줄\n이것은 157번째 줄\n이것은 158번째 줄\n이것은 159번째 줄', '이것은 160번째 줄\n이것은 161번째 줄\n이것은 162번째 줄\n이것은 163번째 줄\n이것은 164번째 줄\n이것은 165번째 줄\n이것은 166번째 줄\n이것은 167번째 줄\n이것은 168번째 줄\n이것은 169번째 줄\n이것은 170번째 줄\n이것은 171번째 줄\n이것은 172번째 줄\n이것은 173번째 줄\n이것은 174번째 줄\n이것은 175번째 줄\n이것은 176번째 줄\n이것은 177번째 줄\n이것은 178번째 줄\n이것은 179번째 줄\n이것은 180번째 줄\n이것은 181번째 줄\n이것은 182번째 줄\n이것은 183번째 줄\n이것은 184번째 줄\n이것은 185번째 줄\n이것은 186번째 줄\n이것은 187번째 줄\n이것은 188번째 줄\n이것은 189번째 줄\n이것은 190번째 줄\n이것은 191번째 줄\n이것은 192번째 줄\n이것은 193번째 줄\n이것은 194번째 줄\n이것은 195번째 줄\n이것은 196번째 줄\n이것은 197번째 줄', '이것은 198번째 줄\n이것은 199번째 줄\n이것은 200번째 줄\n이것은 201번째 줄\n이것은 202번째 줄\n이것은 203번째 줄\n이것은 204번째 줄\n이것은 205번째 줄\n이것은 206번째 줄\n이것은 207번째 줄\n이것은 208번째 줄\n이것은 209번째 줄\n이것은 210번째 줄\n이것은 211번째 줄\n이것은 212번째 줄\n이것은 213번째 줄\n이것은 214번째 줄\n이것은 215번째 줄\n이것은 216번째 줄\n이것은 217번째 줄\n이것은 218번째 줄\n이것은 219번째 줄\n이것은 220번째 줄\n이것은 221번째 줄\n이것은 222번째 줄\n이것은 223번째 줄\n이것은 224번째 줄\n이것은 225번째 줄\n이것은 226번째 줄\n이것은 227번째 줄\n이것은 228번째 줄\n이것은 229번째 줄\n이것은 230번째 줄\n이것은 231번째 줄\n이것은 232번째 줄\n이것은 233번째 줄\n이것은 234번째 줄\n이것은 235번째 줄', '이것은 236번째 줄\n이것은 237번째 줄\n이것은 238번째 줄\n이것은 239번째 줄\n이것은 240번째 줄\n이것은 241번째 줄\n이것은 242번째 줄\n이것은 243번째 줄\n이것은 244번째 줄\n이것은 245번째 줄\n이것은 246번째 줄\n이것은 247번째 줄\n이것은 248번째 줄\n이것은 249번째 줄\n이것은 250번째 줄\n이것은 251번째 줄\n이것은 252번째 줄\n이것은 253번째 줄\n이것은 254번째 줄\n이것은 255번째 줄\n이것은 256번째 줄\n이것은 257번째 줄\n이것은 258번째 줄\n이것은 259번째 줄\n이것은 260번째 줄\n이것은 261번째 줄\n이것은 262번째 줄\n이것은 263번째 줄\n이것은 264번째 줄\n이것은 265번째 줄\n이것은 266번째 줄\n이것은 267번째 줄\n이것은 268번째 줄\n이것은 269번째 줄\n이것은 270번째 줄\n이것은 271번째 줄\n이것은 272번째 줄\n이것은 273번째 줄', '이것은 274번째 줄\n이것은 275번째 줄\n이것은 276번째 줄\n이것은 277번째 줄\n이것은 278번째 줄\n이것은 279번째 줄\n이것은 280번째 줄\n이것은 281번째 줄\n이것은 282번째 줄\n이것은 283번째 줄\n이것은 284번째 줄\n이것은 285번째 줄\n이것은 286번째 줄\n이것은 287번째 줄\n이것은 288번째 줄\n이것은 289번째 줄\n이것은 290번째 줄\n이것은 291번째 줄\n이것은 292번째 줄\n이것은 293번째 줄\n이것은 294번째 줄\n이것은 295번째 줄\n이것은 296번째 줄\n이것은 297번째 줄\n이것은 298번째 줄\n이것은 299번째 줄\n이것은 300번째 줄\n이것은 301번째 줄\n이것은 302번째 줄\n이것은 303번째 줄\n이것은 304번째 줄\n이것은 305번째 줄\n이것은 306번째 줄\n이것은 307번째 줄\n이것은 308번째 줄\n이것은 309번째 줄\n이것은 310번째 줄\n이것은 311번째 줄', '이것은 312번째 줄\n이것은 313번째 줄\n이것은 314번째 줄\n이것은 315번째 줄\n이것은 316번째 줄\n이것은 317번째 줄\n이것은 318번째 줄\n이것은 319번째 줄\n이것은 320번째 줄\n이것은 321번째 줄\n이것은 322번째 줄\n이것은 323번째 줄\n이것은 324번째 줄\n이것은 325번째 줄\n이것은 326번째 줄\n이것은 327번째 줄\n이것은 328번째 줄\n이것은 329번째 줄\n이것은 330번째 줄\n이것은 331번째 줄\n이것은 332번째 줄\n이것은 333번째 줄\n이것은 334번째 줄\n이것은 335번째 줄\n이것은 336번째 줄\n이것은 337번째 줄\n이것은 338번째 줄\n이것은 339번째 줄\n이것은 340번째 줄\n이것은 341번째 줄\n이것은 342번째 줄\n이것은 343번째 줄\n이것은 344번째 줄\n이것은 345번째 줄\n이것은 346번째 줄\n이것은 347번째 줄\n이것은 348번째 줄\n이것은 349번째 줄', '이것은 350번째 줄\n이것은 351번째 줄\n이것은 352번째 줄\n이것은 353번째 줄\n이것은 354번째 줄\n이것은 355번째 줄\n이것은 356번째 줄\n이것은 357번째 줄\n이것은 358번째 줄\n이것은 359번째 줄\n이것은 360번째 줄\n이것은 361번째 줄\n이것은 362번째 줄\n이것은 363번째 줄\n이것은 364번째 줄\n이것은 365번째 줄\n이것은 366번째 줄\n이것은 367번째 줄\n이것은 368번째 줄\n이것은 369번째 줄\n이것은 370번째 줄\n이것은 371번째 줄\n이것은 372번째 줄\n이것은 373번째 줄\n이것은 374번째 줄\n이것은 375번째 줄\n이것은 376번째 줄\n이것은 377번째 줄\n이것은 378번째 줄\n이것은 379번째 줄\n이것은 380번째 줄\n이것은 381번째 줄\n이것은 382번째 줄\n이것은 383번째 줄\n이것은 384번째 줄\n이것은 385번째 줄\n이것은 386번째 줄\n이것은 387번째 줄', '이것은 388번째 줄\n이것은 389번째 줄\n이것은 390번째 줄\n이것은 391번째 줄\n이것은 392번째 줄\n이것은 393번째 줄\n이것은 394번째 줄\n이것은 395번째 줄\n이것은 396번째 줄\n이것은 397번째 줄\n이것은 398번째 줄\n이것은 399번째 줄']

    def test_짧은_조각을_앞_조각에_붙인다(self, splitter: ContentSplitter) -> None:
        parts = ['앞부분', '짧', '충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문']
        forced = [False, False, False]
        assert splitter.merge_tiny(parts, forced, 12000, 500) == ['앞부분\n짧\n충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문충분히긴본문']

    def test_마커로_일부러_끊은_짧은_조각은_안_붙인다(self, splitter: ContentSplitter) -> None:
        parts = ['첫부분', '마커로갈린짧은둘째']
        forced = [False, True]
        assert splitter.merge_tiny(parts, forced, 12000, 500) == ['첫부분', '마커로갈린짧은둘째']


class TestSplitVerifier:
    def test_정상_분할은_문제가_없다(self, verifier: SplitVerifier) -> None:
        assert verifier.verify_chunks('본문내용입니다', ['본문내용입니다']) == []

    def test_표에_열_이름_행이_없으면_문제로_잡는다(self, verifier: SplitVerifier) -> None:
        본문 = '| a | b |\n|---|---|\n| 1 | 2 |'
        (문제,) = verifier.verify_chunks(본문, ['| a | b |\n|---|---|', '| 1 | 2 |'])
        assert 문제.reason == '1번 조각 표 열 이름 행 없음'

    @pytest.mark.parametrize("구분행", [
        "|---|---|",
        "| --- | --- |",
        "|:---|---:|",
        "| :--- | :---: |",
        "| - | - |",
        "|-|-|",
    ])
    def test_쓰이는_구분행_형태를_모두_통과시킨다(self, verifier: SplitVerifier, 구분행: str) -> None:
        """모델이 실제로 쓰는 것은 '| --- | --- |' 다. 파이프 뒤 공백을
        안 받아 주면 표가 든 응답이 매번 안전 낙하로 떨어져 서식을 잃는다
        (2026-09-18 실측 5건, sca-3pr)."""
        본문 = f"| a | b |\n{구분행}\n| 1 | 2 |"
        assert verifier.verify_chunks(본문, [본문]) == []

    def test_구분행이_아닌_줄이_오면_여전히_잡는다(self, verifier: SplitVerifier) -> None:
        본문 = "| a | b |\n|---|---|\n| 1 | 2 |\n꼬리말"
        (문제,) = verifier.verify_chunks(본문, ["| a | b |\n|---|---|", "| 1 | 2 |\n꼬리말"])
        assert 문제.reason == "1번 조각 표 열 이름 행 없음"

    def test_원문에_이미_있던_열_이름_없는_표는_분할_탓이_아니다(self, verifier: SplitVerifier) -> None:
        """분할 점검은 분할이 만든 손상만 본다. 원문이 원래 그런 표를 담고
        있으면 safe_fallback 으로 보내도 표가 살아나지 않고 서식만 잃는다.
        2026-09-19 실측 split_broken 2건이 전부 이것이었다 (sca-iyq)."""
        본문 = "| 대상 | 파일 |\n| 진행 표시 | progress.py |"
        assert verifier.verify_chunks(본문, [본문]) == []

    def test_분할이_열_이름_행을_떼어_내면_잡는다(self, verifier: SplitVerifier) -> None:
        본문 = "| a | b |\n|---|---|\n| 1 | 2 |"
        문제 = verifier.verify_chunks(본문, ["| a | b |\n|---|---|", "| 1 | 2 |"])
        assert [p.reason for p in 문제] == ["1번 조각 표 열 이름 행 없음"]

    def test_코드블록_펜스_짝이_안_맞으면_문제로_잡는다(self, verifier: SplitVerifier) -> None:
        (문제,) = verifier.verify_chunks('```python\nprint(1)', ['```python\nprint(1)'])
        assert 문제.reason == '0번 조각 코드블록 펜스 짝 안 맞음'

    def test_내용이_크게_유실되면_문제로_잡는다(self, verifier: SplitVerifier) -> None:
        text = '가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가'
        chunks_out = ['가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가']
        (문제,) = verifier.verify_chunks(text, chunks_out)
        assert 문제.reason == '내용 유실 의심 1000 -> 100'

    def test_표_문제에_걸린_줄과_번호를_담는다(self, verifier: SplitVerifier) -> None:
        """사유 이름만 남으면 새 사유가 나와도 재현이 안 된다. 2026-09-19 에
        실제로 원인 미규명으로 끝났다 (sca-9uj)."""
        본문 = "머리말\n| a | b |\n|---|---|\n| 1 | 2 |\n꼬리말"
        (문제,) = verifier.verify_chunks(본문, ["머리말\n| 1 | 2 |\n꼬리말"])
        assert 문제.reason == "0번 조각 표 열 이름 행 없음"
        assert 문제.line_no == 2
        assert "| 1 | 2 |" in 문제.excerpt
        # 앞뒤 한 줄씩. 그 줄만 있으면 왜 그렇게 읽혔는지가 안 보인다
        assert "머리말" in 문제.excerpt and "꼬리말" in 문제.excerpt

    def test_펜스_문제에_여는_줄을_담는다(self, verifier: SplitVerifier) -> None:
        본문 = "설명\n```python\nprint(1)"
        (문제,) = verifier.verify_chunks(본문, [본문])
        assert 문제.reason == "0번 조각 코드블록 펜스 짝 안 맞음"
        assert 문제.line_no == 2
        assert "```python" in 문제.excerpt

    def test_줄에_매이지_않는_사유는_번호가_없다(self, verifier: SplitVerifier) -> None:
        """내용 유실은 조각 전체의 문제다. 억지로 줄을 붙이면 엉뚱한 줄을
        원인으로 읽게 된다."""
        본문 = "가" * 1000
        (문제,) = verifier.verify_chunks(본문, ["가" * 100])
        assert 문제.line_no is None
        assert 문제.excerpt == ""

    def test_점검_실패시_안전_낙하는_줄_경계로만_자른다(self, verifier: SplitVerifier) -> None:
        text = '줄0\n줄1\n줄2\n줄3\n줄4\n줄5\n줄6\n줄7\n줄8\n줄9\n줄10\n줄11\n줄12\n줄13\n줄14\n줄15\n줄16\n줄17\n줄18\n줄19\n줄20\n줄21\n줄22\n줄23\n줄24\n줄25\n줄26\n줄27\n줄28\n줄29\n줄30\n줄31\n줄32\n줄33\n줄34\n줄35\n줄36\n줄37\n줄38\n줄39\n줄40\n줄41\n줄42\n줄43\n줄44\n줄45\n줄46\n줄47\n줄48\n줄49'
        assert verifier.safe_fallback(text, 40) == ['줄0\n줄1\n줄2\n줄3\n줄4\n줄5\n줄6\n줄7\n줄8\n줄9\n줄10\n줄11', '줄12\n줄13\n줄14\n줄15\n줄16\n줄17\n줄18\n줄19\n줄20\n줄21', '줄22\n줄23\n줄24\n줄25\n줄26\n줄27\n줄28\n줄29\n줄30\n줄31', '줄32\n줄33\n줄34\n줄35\n줄36\n줄37\n줄38\n줄39\n줄40\n줄41', '줄42\n줄43\n줄44\n줄45\n줄46\n줄47\n줄48\n줄49']

    def test_표_앞뒤에_빈_줄을_넣는다(self, verifier: SplitVerifier) -> None:
        text = '문단\n| a | b |\n| 1 | 2 |\n다음문단'
        assert verifier.separate_tables(text) == '문단\n\n| a | b |\n| 1 | 2 |\n\n다음문단'

    def test_코드_울타리_안의_표는_건드리지_않는다(self, verifier: SplitVerifier) -> None:
        text = '```\n| a | b |\n| 1 | 2 |\n```'
        assert verifier.separate_tables(text) == '```\n| a | b |\n| 1 | 2 |\n```'

    def test_invalid_blocks_사유면_거절로_판정한다(self, verifier: SplitVerifier) -> None:
        class FakeExc(Exception):
            def __init__(self, error: str) -> None:
                self.response = {"error": error}

        assert verifier.blocks_rejected(FakeExc("invalid_blocks")) is True

    def test_다른_사유면_거절로_판정하지_않는다(self, verifier: SplitVerifier) -> None:
        class FakeExc(Exception):
            def __init__(self, error: str) -> None:
                self.response = {"error": error}

        assert verifier.blocks_rejected(FakeExc("something_else")) is False

    def test_블록_수_한도를_다른_형식_오류와_가른다(self, verifier: SplitVerifier) -> None:
        """sca-2k7 — 한도 초과는 더 잘게 나누면 풀리고 나머지는 안 풀린다."""

        class FakeExc(Exception):
            def __init__(self, error: str) -> None:
                self.response: dict = {"error": error}

        한도 = FakeExc("invalid_blocks")
        한도.response["errors"] = ["no more than 50 items allowed [json-pointer:/blocks]"]
        assert verifier.block_limit_exceeded(한도) is True

        형식 = FakeExc("invalid_blocks")
        형식.response["errors"] = ["must be less than 3001 characters [json-pointer:/blocks/0/text]"]
        assert verifier.block_limit_exceeded(형식) is False
        assert verifier.block_limit_exceeded(FakeExc("something_else")) is False

    def test_response_속성이_없어도_예외를_내지_않는다(self, verifier: SplitVerifier) -> None:
        assert verifier.blocks_rejected(Exception("invalid_blocks in str")) is False


class TestBlockBuilder:
    def test_표와_코드블록_한가운데_마커는_지우고_일반_문단의_마커는_남긴다(self, blocks: BlockBuilder) -> None:
        text = '| a | b |\n<<<SPLIT>>>\n| c | d |\n\n일반문단\n<<<SPLIT>>>\n다음문단\n```\n코드1\n<<<SPLIT>>>\n코드2\n```'
        assert blocks.clean_markers(text) == '| a | b |\n| c | d |\n\n일반문단\n<<<SPLIT>>>\n다음문단\n```\n코드1\n코드2\n```'

    def test_미리보기는_헤딩과_표시문자를_건너뛰고_첫_문장을_뽑는다(self, blocks: BlockBuilder) -> None:
        text = '# 헤딩\n\n**굵은** 본문 첫줄입니다 [링크](http://a.com)'
        assert blocks.preview(text) == '굵은 본문 첫줄입니다 링크'

    def test_본문이_전부_없으면_봇_이름으로_대체한다(self, blocks: BlockBuilder) -> None:
        assert blocks.preview("") == "테스트봇 답변"

    def test_제목만_있는_답은_제목을_미리보기로_쓴다(self, blocks: BlockBuilder) -> None:
        """미리보기는 알림에 뜨는 유일한 문구다. 봇 이름으로 떨어지면 무슨
        답이 왔는지 열어 보기 전에는 알 수 없다 (2026-09-19 실측)."""
        assert blocks.preview("## 배포 결과\n\n### 요약") == "배포 결과"

    def test_표만_있는_답은_첫_행의_칸을_쓴다(self, blocks: BlockBuilder) -> None:
        assert blocks.preview("| 대상 | 파일 |\n| 진행 표시 | progress.py |") == "대상 · 파일"

    def test_구분행은_미리보기로_쓰지_않는다(self, blocks: BlockBuilder) -> None:
        """'--- · ---' 이 알림에 뜨면 아무 뜻이 없다. 표 한가운데서 잘린
        조각은 구분행으로 시작한다."""
        assert blocks.preview("| --- | --- |\n| 1 | 2 |") == "1 · 2"

    @pytest.mark.parametrize("구분행", ["| :---: | ---: |", "|:--|--:|", "| --- | --- |"])
    def test_정렬_표기가_붙은_구분행도_거른다(self, blocks: BlockBuilder, 구분행: str) -> None:
        """콜론이 붙으면 앞에서 대시만 떼는 정리로는 안 걸러진다."""
        assert blocks.preview(f"{구분행}\n| 1 | 2 |") == "1 · 2"

    def test_평문_줄이_있으면_그것이_먼저다(self, blocks: BlockBuilder) -> None:
        """제목보다 본문 첫 줄이 무슨 답인지 더 잘 말한다."""
        assert blocks.preview("# 제목\n\n본문 첫 줄") == "본문 첫 줄"

    def test_끝의_짧은_인용_줄을_보조_줄로_떼어낸다(self, blocks: BlockBuilder) -> None:
        text = '본문입니다\n두번째줄\n> 걸린시간: 3초\n> 기준: 2026-09-14'
        assert blocks.split_context(text) == ('본문입니다\n두번째줄', '걸린시간: 3초\n기준: 2026-09-14')

    def test_인용이_없으면_보조_줄이_빈다(self, blocks: BlockBuilder) -> None:
        text = '본문만있고인용없음'
        assert blocks.split_context(text) == ('본문만있고인용없음', '')

    def test_인용이_상한보다_길면_떼어내지_않는다(self, blocks: BlockBuilder) -> None:
        text = '본문\n> xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
        assert blocks.split_context(text) == ('본문\n> xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx', '')


class Test분할_전후_이중_separate_tables:
    """2026-08-27 사고 재현. md_chunks 가 표와 문단을 각각 끊고
    split_for_blocks 가 "\n".join 으로 다시 붙이며 빈 줄이 사라진다.
    separate_tables 를 분할 전후로 두 번 불러야 표가 안 깨진다."""

    def test_분할_후_다시_separate_tables를_불러야_표와_문단_사이_빈_줄이_유지된다(
        self, splitter: ContentSplitter, verifier: SplitVerifier
    ) -> None:
        text = '문단설명\n| a | b |\n|---|---|\n| 1 | 2 |\n다음문단'
        once = verifier.separate_tables(text)
        parts = splitter.split_for_blocks(once, 12000)
        twice = [verifier.separate_tables(p) for p in parts]
        assert twice == ['문단설명\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n다음문단']
        # 표와 그 앞 문단 사이에 빈 줄이 있어야 슬랙이 문단을 표 행으로 삼지 않는다
        assert "| a | b |\n\n" not in "".join(twice) or "\n\n| a | b |" in "".join(twice)


class Test상한_기본값:
    def test_슬랙_한_덩어리_상한은_3500자다(self, settings: RuntimeSettings) -> None:
        assert settings.slack_chunk == 3500

    def test_마크다운_블록_상한은_12000자다(self, settings: RuntimeSettings) -> None:
        assert settings.markdown_block_limit == 12000



class Test블록_개수_상한:
    """슬랙은 markdown 블록을 서버에서 펼친다. 제목 한 줄과 표 하나가 각각
    블록 1개가 되고 50개를 넘으면 게시 전체가 invalid_blocks 로 거부된다
    (2026-09-18 asuka 실측, sca-mb1). 문단·코드·인용·목록은 몇 개든 합쳐진다."""

    def _제목과_표(self, n: int) -> str:
        return "\n\n".join(f"## 제목 {i}\n\n| a | b |\n| --- | --- |\n| 1 | 2 |" for i in range(n))

    def test_제목과_표만_블록으로_센다(self) -> None:
        from slack_cli_agent.render.splitter import block_cost

        assert block_cost("## 제목\n\n문단입니다.") == 2
        assert block_cost("\n\n".join(f"문단 {i}." for i in range(50))) == 1
        assert block_cost("| a | b |\n| --- | --- |\n| 1 | 2 |") == 1

    def test_상한을_넘는_본문은_여러_조각으로_나뉜다(self, splitter: ContentSplitter) -> None:
        from slack_cli_agent.render.splitter import MAX_BLOCKS, block_cost

        chunks = splitter.split_for_blocks(self._제목과_표(60))
        assert len(chunks) > 1
        assert all(block_cost(c) <= MAX_BLOCKS for c in chunks)

    def test_짧은_조각을_붙일_때도_상한을_지킨다(self, splitter: ContentSplitter) -> None:
        from slack_cli_agent.render.splitter import MAX_BLOCKS, block_cost

        # 둘 다 500자 미만이라 merge_tiny 의 병합 대상이지만 합치면 상한을 넘는다.
        parts = ["\n".join(f"## 제목 {i}" for i in range(40)), "\n".join(f"## 끝 {i}" for i in range(10))]
        merged = splitter.merge_tiny(parts, [False, False], 12000)
        assert all(block_cost(c) <= MAX_BLOCKS for c in merged)

    def test_안전_낙하도_상한을_지킨다(self, verifier: SplitVerifier) -> None:
        from slack_cli_agent.render.splitter import MAX_BLOCKS, block_cost

        chunks = verifier.safe_fallback(self._제목과_표(60))
        assert all(block_cost(c) <= MAX_BLOCKS for c in chunks)


class Test검증기는_코드_문맥을_가린다:
    """운영에서 오판정 2건이 났다 (2026-09-18 23:54, 09-19 00:13). 둘 다
    safe_fallback 으로 떨어져 서식을 버린 채 게시됐다 (sca-a3b)."""

    def test_코드블록_안의_표_예시는_표로_보지_않는다(self, verifier: SplitVerifier) -> None:
        본문 = (
            "아래 형식으로 바꿉니다.\n\n"
            "```markdown\n"
            "## 고칠 대상\n"
            "| 대상 | 파일 | 바꿀 내용 |\n"
            "```\n\n"
            "제목을 고정하면 비교가 됩니다."
        )
        assert verifier.verify_chunks(본문, [본문]) == []

    def test_문장_중간의_인라인_코드는_펜스로_세지_않는다(self, verifier: SplitVerifier) -> None:
        본문 = "응답 안에 ` ```slack-blocks ` 펜스로 JSON 을 넣으면 그것을 꺼냅니다."
        assert verifier.verify_chunks(본문, [본문]) == []

    def test_진짜로_펜스가_안_닫히면_여전히_잡는다(self, verifier: SplitVerifier) -> None:
        본문 = "설명\n\n```python\nprint(1)\n"
        assert "0번 조각 코드블록 펜스 짝 안 맞음" in [p.reason for p in verifier.verify_chunks(본문, [본문])]

    def test_코드블록_밖의_열_이름_없는_표는_여전히_잡는다(self, verifier: SplitVerifier) -> None:
        본문 = "설명\n\n| 대상 | 파일 |\n|---|---|\n| 가 | 나 |"
        조각 = ["설명\n\n| 대상 | 파일 |\n|---|---|", "| 가 | 나 |"]
        assert "1번 조각 표 열 이름 행 없음" in [p.reason for p in verifier.verify_chunks(본문, 조각)]
