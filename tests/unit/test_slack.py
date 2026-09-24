"""slack/ 패키지 시험.

이식 대상(HistoryReader.slack_ts/read_history/wait_history_slot, 빈 응답
재시도, Block Kit 거절 시 평문 낮춤)의 기대값은 원본 bot.py
의 slack_ts, read_history, wait_history_slot, post 를 실제로 읽고 그 로직을
그대로 따라 만들었다. 손으로 짐작하지 않았다.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from identity_support import fake_identity

from slack_cli_agent.config.channel import ChannelRegistry
from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.core.context import RequestContext
from slack_cli_agent.core.errors import ConfigError, HistoryUnavailable, SlackError
from slack_cli_agent.guard.watch import WATCH_MARK_EMOJI
from slack_cli_agent.observability.notices import NoticeCatalog
from slack_cli_agent.render.blocks import BlockBuilder
from slack_cli_agent.render.markdown import MarkdownConverter
from slack_cli_agent.render.splitter import ContentSplitter
from slack_cli_agent.render.verifier import SplitVerifier
from slack_cli_agent.review.base import ReactionPort
from slack_cli_agent.slack.attachments import AttachmentStore, DownloadResult
from slack_cli_agent.slack.gate import ResponseGate
from slack_cli_agent.slack.gateway import SlackGateway
from slack_cli_agent.slack.history import HistoryReader
from slack_cli_agent.slack.listener import EventListener
from slack_cli_agent.slack.publisher import MessagePublisher
from slack_cli_agent.slack.reactions import (
    FORMAT_REVIEW_EMOJI,
    POSTMORTEM_EMOJI,
    SILENT_MARK_EMOJI,
    STALE_ON_SETTLE,
    UNFINISHED_EMOJI,
    ReactionMarker,
)
from slack_cli_agent.slack.transcript import CurrentMessage, TranscriptBuilder

# 대역 슬랙 클라이언트


class FakeWebClient:
    """실제 슬랙 응답 형태를 흉내내는 대역.

    각 메서드 호출을 기록하고, 미리 채워 둔 응답 큐에서 하나씩 꺼내 돌려준다.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._history_responses: list[dict] = []
        self._post_responses: list[Any] = []
        self._update_responses: list[Any] = []
        self.reaction_add_calls: list[tuple[str, str, str]] = []
        self.reaction_remove_calls: list[tuple[str, str, str]] = []
        self._raise_on_reaction_add: set[str] = set()
        self._replies_response: dict = {"messages": []}
        self.auth_test_response: dict = {"team": "", "user": ""}

    def auth_test(self) -> dict:
        return self.auth_test_response

    def queue_history(self, response: dict) -> None:
        self._history_responses.append(response)

    def conversations_history(self, **kwargs):
        self.calls.append(("conversations_history", kwargs))
        return self._history_responses.pop(0)

    def conversations_replies(self, **kwargs):
        self.calls.append(("conversations_replies", kwargs))
        return self._replies_response

    def set_replies_response(self, response: dict) -> None:
        self._replies_response = response

    def queue_post(self, response: Any) -> None:
        self._post_responses.append(response)

    def chat_postMessage(self, **kwargs):
        self.calls.append(("chat_postMessage", kwargs))
        result = self._post_responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def queue_update(self, response: Any) -> None:
        self._update_responses.append(response)

    def chat_update(self, **kwargs):
        self.calls.append(("chat_update", kwargs))
        result = self._update_responses.pop(0) if self._update_responses else {"ok": True}
        if isinstance(result, Exception):
            raise result
        return result

    def reactions_add(self, *, channel, timestamp, name):
        if name in self._raise_on_reaction_add:
            raise RuntimeError("boom")
        self.reaction_add_calls.append((channel, timestamp, name))

    def reactions_remove(self, *, channel, timestamp, name):
        self.reaction_remove_calls.append((channel, timestamp, name))


class SlackApiError(Exception):
    def __init__(self, error: str, errors: list[str] | None = None) -> None:
        super().__init__(error)
        # The real payload carries the reason in errors[], which is how the
        # block-count limit is told apart from other invalid_blocks causes.
        self.response: dict[str, Any] = {"ok": False, "error": error}
        if errors is not None:
            self.response["errors"] = errors
            self.response["response_metadata"] = {"messages": [f"[ERROR] {e}" for e in errors]}


BLOCK_LIMIT_ERRORS = ["no more than 50 items allowed [json-pointer:/blocks]"]


# HistoryReader


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, sec: float) -> None:
        self.sleeps.append(sec)
        self.now += sec


@pytest.fixture
def settings() -> RuntimeSettings:
    return RuntimeSettings()


class TestHistoryReader:
    def test_slack_ts_는_소수_6자리로_고정한다(self, settings: RuntimeSettings) -> None:
        """원본 slack_ts: f"{float(value):.6f}" — 7자리 입력도 6자리로 잘린다."""
        client = FakeWebClient()
        reader = HistoryReader(client, settings)
        assert reader.slack_ts(1788253544.4080493) == "1788253544.408049"
        assert reader.slack_ts("1788253544.408049") == "1788253544.408049"

    def test_연속_호출_간격이_최소간격보다_짧으면_그만큼_잔다(
        self, settings: RuntimeSettings
    ) -> None:
        client = FakeWebClient()
        clock = FakeClock(start=100.0)
        reader = HistoryReader(client, settings, clock=clock.time, sleep=clock.sleep)
        reader.wait_history_slot()
        assert clock.sleeps == []
        clock.now = 100.5  # 0.5초 뒤 재호출 — 최소간격 2.0초에 못 미친다
        reader.wait_history_slot()
        assert clock.sleeps == [pytest.approx(1.5)]

    def test_빈_응답이면_재시도하고_그중_한번이라도_차있으면_그것을_돌려준다(
        self, settings: RuntimeSettings
    ) -> None:
        client = FakeWebClient()
        client.queue_history({"ok": True, "messages": []})
        client.queue_history({"ok": True, "messages": [{"ts": "1", "text": "hi"}]})
        clock = FakeClock()
        reader = HistoryReader(client, settings, clock=clock.time, sleep=clock.sleep)
        msgs = reader.read_history("C1", "0.000000", 40)
        assert msgs == [{"ts": "1", "text": "hi"}]
        # 재시도 사이에 history_read_pause_sec 만큼 쉰다
        쉰횟수 = sum(1 for 초 in clock.sleeps if 초 == pytest.approx(settings.history_read_pause_sec))
        assert 쉰횟수 >= 1

    def test_기록조회_상한만큼_다_비면_판정불가_예외를_낸다(
        self, settings: RuntimeSettings
    ) -> None:
        """원본은 None 을 돌려준다. 이 포팅은 core.errors.HistoryUnavailable 로
        판정 불가를 명시적으로 알린다 — core.errors 는 이미 이 목적으로 있는
        타입이다."""
        client = FakeWebClient()
        for _ in range(settings.history_read_tries):
            client.queue_history({"ok": True, "messages": []})
        clock = FakeClock()
        reader = HistoryReader(client, settings, clock=clock.time, sleep=clock.sleep)
        with pytest.raises(HistoryUnavailable):
            reader.read_history("C1", "0.000000", 40)
        assert len(client.calls) == settings.history_read_tries


# ReactionMarker


class TestReactionMarker:
    def test_add_실패해도_조용히_넘긴다(self) -> None:
        client = FakeWebClient()
        client._raise_on_reaction_add.add("eyes")
        marker = ReactionMarker(client)
        marker.mark_processing("C1", "1.0")  # 예외를 내지 않아야 한다
        assert client.reaction_add_calls == []

    def test_ReactionPort_를_만족한다(self) -> None:
        """점검이 이 객체로 clear_processing 을 부른다. 없으면 실행 중에 AttributeError 가 난다."""
        assert isinstance(ReactionMarker(FakeWebClient()), ReactionPort)

    def test_clear_processing은_눈_표식을_뗀다(self) -> None:
        client = FakeWebClient()
        marker = ReactionMarker(client)
        marker.clear_processing("C1", "1.0")
        assert client.reaction_remove_calls == [("C1", "1.0", "eyes")]

    def test_clear_waiting은_모래시계_표식을_뗀다(self) -> None:
        client = FakeWebClient()
        marker = ReactionMarker(client)
        marker.clear_waiting("C1", "1.0")
        assert client.reaction_remove_calls == [("C1", "1.0", "hourglass")]

    def test_mark_done은_미완료_표식을_떼고_체크를_단다(self) -> None:
        client = FakeWebClient()
        marker = ReactionMarker(client)
        marker.mark_done("C1", "1.0")
        removed = {name for _, _, name in client.reaction_remove_calls}
        assert removed == set(STALE_ON_SETTLE)
        assert client.reaction_add_calls == [("C1", "1.0", "white_check_mark")]

    def test_mark_silent은_눈을_떼고_zipper를_단다(self) -> None:
        client = FakeWebClient()
        marker = ReactionMarker(client)
        marker.mark_silent("C1", "1.0")
        assert client.reaction_add_calls == [("C1", "1.0", SILENT_MARK_EMOJI)]

    def test_mark_resolved_like는_미완료_표식을_전부_떼고_새_표식을_단다(self) -> None:
        client = FakeWebClient()
        marker = ReactionMarker(client)
        marker.mark_resolved_like("C1", "1.0", "white_check_mark")
        removed = {name for _, _, name in client.reaction_remove_calls}
        assert removed == set(STALE_ON_SETTLE)
        assert client.reaction_add_calls == [("C1", "1.0", "white_check_mark")]

    def test_already_handled은_완료표식이_있을때만_참이다(self) -> None:
        marker = ReactionMarker(FakeWebClient())
        done_msg = {"reactions": [{"name": "white_check_mark"}]}
        unfinished_msg = {"reactions": [{"name": "eyes"}]}
        assert marker.already_handled(done_msg) is True
        assert marker.already_handled(unfinished_msg) is False
        assert marker.already_handled({}) is False


# MessagePublisher


@pytest.fixture
def block_builder() -> BlockBuilder:
    return BlockBuilder(bot_display_name="테스트봇")


@pytest.fixture
def splitter(settings: RuntimeSettings, block_builder: BlockBuilder) -> ContentSplitter:
    return ContentSplitter(settings, block_builder)


@pytest.fixture
def verifier(settings: RuntimeSettings) -> SplitVerifier:
    return SplitVerifier(settings)


@pytest.fixture
def markdown() -> MarkdownConverter:
    return MarkdownConverter()


def make_publisher(
    client: FakeWebClient,
    settings: RuntimeSettings,
    markdown: MarkdownConverter,
    splitter: ContentSplitter,
    verifier: SplitVerifier,
    block_builder: BlockBuilder,
    audit: list | None = None,
) -> MessagePublisher:
    sink = None
    if audit is not None:
        def sink(**fields):
            audit.append(fields)
    return MessagePublisher(
        client=client,
        settings=settings,
        markdown=markdown,
        splitter=splitter,
        verifier=verifier,
        blocks=block_builder,
        bot_display_name="테스트봇",
        audit=sink,
    )


class 펜스를_가르는_분할기(ContentSplitter):
    """여는 펜스까지만 첫 조각에 담아 분할 손상을 만든다."""

    def split_for_blocks(self, text: str, limit: int | None = None) -> list[str]:
        head, _, tail = text.partition("\n```\n")
        return [head, tail] if tail else [text.rsplit("\n```", 1)[0]]


class TestMessagePublisher:
    def test_평문_채널은_mrkdwn으로_낮춰_스레드에_올린다(
        self, settings, markdown, splitter, verifier, block_builder
    ) -> None:
        client = FakeWebClient()
        client.queue_post({"ts": "111.000000"})
        pub = make_publisher(client, settings, markdown, splitter, verifier, block_builder)
        parent_ts = pub.post("C1", "100.0", "**굵게**", rich=False)
        # 채널 게시는 스레드 부모 ts 를 그대로 돌려준다. res["ts"] 로 바뀌는
        # 것은 parent_ts 가 None(DM)이었을 때뿐이다.
        assert parent_ts == "100.0"
        kind, kwargs = client.calls[0]
        assert kind == "chat_postMessage"
        assert kwargs["text"] == "*굵게*"
        assert kwargs["thread_ts"] == "100.0"
        assert "blocks" not in kwargs

    def test_스레드_없이_채널에_올리면_첫_조각의_ts_를_돌려준다(
        self, settings, markdown, splitter, verifier, block_builder
    ) -> None:
        """점검 보고는 스레드 없이 올린다. 빈 문자열을 그대로 두면 돌려줄 ts 가
        안 정해지고, 뒤 조각도 스레드로 안 접혀 최상위에 따로 올라간다."""
        client = FakeWebClient()
        client.queue_post({"ts": "111.000000"})
        client.queue_post({"ts": "222.000000"})
        pub = make_publisher(client, settings, markdown, splitter, verifier, block_builder)
        parent_ts = pub.post("C1", "", "본문", rich=False)
        assert parent_ts == "111.000000"
        assert "thread_ts" not in client.calls[0][1]

    def test_DM은_스레드로_접지_않는다(
        self, settings, markdown, splitter, verifier, block_builder
    ) -> None:
        client = FakeWebClient()
        client.queue_post({"ts": "111.000000"})
        pub = make_publisher(client, settings, markdown, splitter, verifier, block_builder)
        pub.post("D1", "100.0", "안녕", rich=False)
        _, kwargs = client.calls[0]
        assert "thread_ts" not in kwargs

    def test_리치_채널은_markdown_블록으로_넘긴다(
        self, settings, markdown, splitter, verifier, block_builder
    ) -> None:
        client = FakeWebClient()
        client.queue_post({"ts": "111.000000"})
        pub = make_publisher(client, settings, markdown, splitter, verifier, block_builder)
        pub.post("C1", "100.0", "본문입니다", rich=True)
        _, kwargs = client.calls[0]
        assert kwargs["blocks"][0] == {"type": "markdown", "text": "본문입니다"}

    def test_리치_표기가_거절되면_평문으로_낮춰_재발신한다(
        self, settings, markdown, splitter, verifier, block_builder
    ) -> None:
        """원본 post(): invalid_blocks 이면 평문으로 낮춰 다시 보낸다."""
        client = FakeWebClient()
        client.queue_post(SlackApiError("invalid_blocks"))
        client.queue_post({"ts": "222.000000"})
        audit: list = []
        pub = make_publisher(
            client, settings, markdown, splitter, verifier, block_builder, audit=audit
        )
        parent_ts = pub.post("C1", "100.0", "본문입니다", rich=True)
        assert parent_ts == "100.0"
        assert len(client.calls) == 2
        second_kwargs = client.calls[1][1]
        assert "blocks" not in second_kwargs
        assert second_kwargs["text"] == "본문입니다"
        assert any(a.get("kind") == "blocks_rejected" for a in audit)

    def test_블록_수_한도면_더_잘게_나눠_리치로_다시_보낸다(
        self, settings, markdown, splitter, verifier, block_builder
    ) -> None:
        """sca-2k7 — 블록 수 초과는 길이 문제이지 형식 문제가 아니다. 평문으로
        낮추면 표·제목이 사라지고 본문도 3900자에서 잘린다."""
        client = FakeWebClient()
        client.queue_post(SlackApiError("invalid_blocks", BLOCK_LIMIT_ERRORS))
        for i in range(20):
            client.queue_post({"ts": f"{200 + i}.000000"})
        audit: list = []
        pub = make_publisher(
            client, settings, markdown, splitter, verifier, block_builder, audit=audit
        )
        body = "\n\n".join(f"## 제목{i}\n\n문단{i}" for i in range(40))
        pub.post("C1", "100.0", body, rich=True)
        재발신 = client.calls[1:]
        assert len(재발신) >= 2
        assert all("blocks" in kwargs for _, kwargs in 재발신)
        보낸_본문 = "".join(kwargs["blocks"][0]["text"] for _, kwargs in 재발신)
        assert "제목0" in 보낸_본문 and "제목39" in 보낸_본문
        assert any(a.get("kind") == "blocks_resplit" for a in audit)

    def test_더_나눌_수_없으면_평문으로_낮춘다(
        self, settings, markdown, splitter, verifier, block_builder
    ) -> None:
        """한 줄짜리 본문은 나눠도 그대로다. 그 자리에서 다시 시도하면
        같은 거부가 끝없이 돈다."""
        client = FakeWebClient()
        client.queue_post(SlackApiError("invalid_blocks", BLOCK_LIMIT_ERRORS))
        client.queue_post({"ts": "222.000000"})
        pub = make_publisher(client, settings, markdown, splitter, verifier, block_builder)
        pub.post("C1", "100.0", "한 줄뿐인 본문", rich=True)
        assert "blocks" not in client.calls[-1][1]

    def test_분할_점검_실패를_걸린_줄과_함께_남긴다(
        self, settings, markdown, splitter, verifier, block_builder
    ) -> None:
        """사유 이름만 남기면 원본 마크다운이 어디에도 안 남아 재현이 안 된다.
        응답 전체를 남기지 않고 걸린 자리만 남긴다 (sca-9uj)."""
        client = FakeWebClient()
        for i in range(5):
            client.queue_post({"ts": f"{100 + i}.000000"})
        audit: list = []
        pub = make_publisher(
            client, settings, markdown, splitter, verifier, block_builder, audit=audit
        )
        # 실제 분할기는 표 헤더도 코드 펜스도 뒤 조각에 다시 붙여 주므로
        # 게시 경로로는 손상을 못 만든다(2026-09-19 실측). 원장에 무엇이
        # 남는지를 보는 시험이라 분할만 대역으로 바꾼다
        pub._splitter = 펜스를_가르는_분할기(settings, block_builder)
        pub.post("C1", "100.0", "설명\n```python\nprint(1)\n```", rich=True)
        (기록,) = [a for a in audit if a.get("kind") == "split_broken"]
        assert 기록["problems"] == ["0번 조각 코드블록 펜스 짝 안 맞음"]
        (근거,) = 기록["evidence"]
        assert 근거["line_no"] == 2
        assert "```python" in 근거["excerpt"]

    def test_조각_전송_중_실패하면_부분전달_안내를_붙이고_예외를_낸다(
        self, settings, markdown, splitter, verifier, block_builder
    ) -> None:
        client = FakeWebClient()
        long_text = "\n".join(f"줄{i}" for i in range(2000))
        # 첫 조각은 성공, 둘째 조각은 알 수 없는 이유로 실패
        client.queue_post({"ts": "1.000000"})
        client.queue_post(SlackApiError("some_other_error"))
        client.queue_post({"ts": "2.000000"})  # 부분전달 안내 발신
        pub = make_publisher(client, settings, markdown, splitter, verifier, block_builder)
        with pytest.raises(SlackError):
            pub.post("C1", "100.0", long_text, rich=False)
        notice_kwargs = client.calls[-1][1]
        assert "까지만 전달됐습니다" in notice_kwargs["text"]

    def test_실행모델_표기는_rich_채널에서만_붙는다(
        self, settings, markdown, splitter, verifier, block_builder
    ) -> None:
        client = FakeWebClient()
        pub = make_publisher(client, settings, markdown, splitter, verifier, block_builder)
        assert pub.apply_elapsed_model_line("답변", "claude-opus-5", rich=False) == "답변"
        out = pub.apply_elapsed_model_line("답변", "claude-opus-5", rich=True)
        assert out == "답변\n> 실행 모델 : claude-opus-5"

    def test_실행모델_표기는_기존_줄을_지우고_다시_붙인다(
        self, settings, markdown, splitter, verifier, block_builder
    ) -> None:
        client = FakeWebClient()
        pub = make_publisher(client, settings, markdown, splitter, verifier, block_builder)
        before = "답변\n> 실행 모델 : old-model"
        out = pub.apply_elapsed_model_line(before, "new-model", rich=True)
        assert out == "답변\n> 실행 모델 : new-model"


# SlackGateway


class Test이모지실패_흔적:
    """이모지 조작 실패가 흔적을 남기는가.

    전부 삼키면 "실패했다" 와 "아예 안 불렀다" 가 구분되지 않는다. 감시 완료 후
    mag 가 안 지워진 원인을 못 갈라낸 이유다(sca-aj3).
    """

    def _marker(self, failing: str):
        class 실패하는클라이언트:
            def reactions_add(self, **kwargs):
                if failing == "add":
                    raise RuntimeError("no_reaction")

            def reactions_remove(self, **kwargs):
                if failing == "remove":
                    raise RuntimeError("no_reaction")

        return ReactionMarker(실패하는클라이언트())

    @pytest.mark.parametrize("동작", ["add", "remove"])
    def test_실패_사유가_기록된다(self, 동작: str, caplog) -> None:
        marker = self._marker(동작)
        with caplog.at_level(logging.DEBUG, logger="slack_cli_agent.slack.reactions"):
            getattr(marker, 동작)("C1", "100.0", "mag")
        assert any("no_reaction" in r.getMessage() for r in caplog.records)

    def test_성공하면_아무것도_안_남긴다(self, caplog) -> None:
        marker = self._marker("add")
        with caplog.at_level(logging.DEBUG, logger="slack_cli_agent.slack.reactions"):
            marker.remove("C1", "100.0", "mag")
        assert caplog.records == []


class Test빈본문게시:
    """보낼 내용이 없을 때 발행기가 그 사실을 남기는가.

    2026-09-16 실측 — rich 채널에서 빈 본문을 주면 split_for_blocks 가 빈 목록을
    돌려주고 발송 루프가 한 번도 안 돈다. 예외도 감사 기록도 없어서 성공과
    구분되지 않았고, 감시 완료 보고가 조용히 사라졌다.
    """

    def test_리치_채널에서_빈_본문이면_경고를_남기고_안_보낸다(
        self, settings, markdown, splitter, verifier, block_builder, caplog
    ) -> None:
        client = FakeWebClient()
        pub = make_publisher(client, settings, markdown, splitter, verifier, block_builder)
        with caplog.at_level(logging.WARNING):
            assert pub.post("C1", "100.0", "", rich=True) is None
        assert client.calls == []
        assert any("빈 본문" in r.getMessage() for r in caplog.records)

    def test_보낼_내용이_있으면_경고가_안_남는다(
        self, settings, markdown, splitter, verifier, block_builder, caplog
    ) -> None:
        client = FakeWebClient()
        client.queue_post({"ts": "111.000000"})
        pub = make_publisher(client, settings, markdown, splitter, verifier, block_builder)
        with caplog.at_level(logging.WARNING):
            pub.post("C1", "100.0", "본문", rich=True)
        assert caplog.records == []


class Test메시지갱신:
    """서식 점검이 지적한 메시지를 교정본으로 바꿔 쓰는 경로.

    이것이 없던 동안 서식 점검은 위반을 찾아도 원 메시지를 고칠 수단이
    없었다(2026-09-18 부검). 갱신은 ts 하나만 건드리므로, 한 메시지에 안
    들어가는 교정본은 앞 조각만 보내는 대신 거부한다.
    """

    def test_리치_채널은_markdown_블록으로_갱신한다(
        self, settings, markdown, splitter, verifier, block_builder
    ) -> None:
        client = FakeWebClient()
        pub = make_publisher(client, settings, markdown, splitter, verifier, block_builder)
        종류 = pub.update("C1", "100.0", "## 제목\n\n본문", rich=True)
        assert 종류 == ["markdown"]
        kind, kwargs = client.calls[0]
        assert kind == "chat_update"
        assert kwargs["ts"] == "100.0"
        assert kwargs["blocks"][0]["text"] == "## 제목\n\n본문"

    def test_말미_인용줄은_context_블록으로_내린다(
        self, settings, markdown, splitter, verifier, block_builder
    ) -> None:
        """게시 때와 같은 렌더를 써야 교정 전후로 표시가 달라지지 않는다."""
        client = FakeWebClient()
        pub = make_publisher(client, settings, markdown, splitter, verifier, block_builder)
        종류 = pub.update("C1", "100.0", "본문\n> 실행 모델 : m", rich=True)
        assert 종류 == ["markdown", "context"]

    def test_평문_채널은_mrkdwn으로_낮춘다(
        self, settings, markdown, splitter, verifier, block_builder
    ) -> None:
        client = FakeWebClient()
        pub = make_publisher(client, settings, markdown, splitter, verifier, block_builder)
        assert pub.update("C1", "100.0", "**굵게**", rich=False) == []
        _, kwargs = client.calls[0]
        assert kwargs["text"] == "*굵게*"
        assert "blocks" not in kwargs

    def test_한_메시지에_안_들어가면_갱신하지_않는다(
        self, settings, markdown, splitter, verifier, block_builder
    ) -> None:
        client = FakeWebClient()
        pub = make_publisher(client, settings, markdown, splitter, verifier, block_builder)
        긴본문 = "\n\n".join(["가" * 3000] * 6)
        with pytest.raises(SlackError) as 오류:
            pub.update("C1", "100.0", 긴본문, rich=True)
        assert "조각" in str(오류.value)
        assert client.calls == []

    def test_빈_교정본은_갱신하지_않는다(
        self, settings, markdown, splitter, verifier, block_builder
    ) -> None:
        client = FakeWebClient()
        pub = make_publisher(client, settings, markdown, splitter, verifier, block_builder)
        with pytest.raises(SlackError):
            pub.update("C1", "100.0", "   ", rich=True)
        assert client.calls == []

    def test_블록_거부는_평문으로_낮춰_다시_갱신한다(
        self, settings, markdown, splitter, verifier, block_builder
    ) -> None:
        """교정본이 통째로 사라지는 것보다 서식 없이라도 올라가는 것이 낫다.
        게시 경로에는 이 낮추기가 있었고 갱신 경로에만 없었다(sca-a23)."""
        client = FakeWebClient()
        client.queue_update(SlackApiError("invalid_blocks"))
        기록: list = []
        pub = make_publisher(
            client, settings, markdown, splitter, verifier, block_builder, audit=기록
        )
        assert pub.update("C1", "100.0", "**굵게**", rich=True) == []
        kinds = [kind for kind, _ in client.calls]
        assert kinds == ["chat_update", "chat_update"]
        _, retry = client.calls[1]
        assert "blocks" not in retry
        assert retry["text"] == "*굵게*"
        assert [a["kind"] for a in 기록] == ["blocks_rejected"]

    def test_평문_갱신도_실패하면_감사에_남기고_예외를_낸다(
        self, settings, markdown, splitter, verifier, block_builder
    ) -> None:
        client = FakeWebClient()
        client.queue_update(SlackApiError("invalid_blocks"))
        client.queue_update(RuntimeError("cant_update_message"))
        기록: list = []
        pub = make_publisher(
            client, settings, markdown, splitter, verifier, block_builder, audit=기록
        )
        with pytest.raises(SlackError):
            pub.update("C1", "100.0", "본문", rich=True)
        assert [a["kind"] for a in 기록] == ["blocks_rejected", "post_failed"]
        assert 기록[-1]["error"] == "cant_update_message"

    def test_갱신_실패는_감사에_남는다(
        self, settings, markdown, splitter, verifier, block_builder
    ) -> None:
        """교정본이 안 올라간 것을 조용히 넘기면 점검 보고의 교정했습니다 절이
        실제와 어긋난다."""
        client = FakeWebClient()
        client.queue_update(RuntimeError("cant_update_message"))
        기록: list = []
        pub = make_publisher(
            client, settings, markdown, splitter, verifier, block_builder, audit=기록
        )
        with pytest.raises(SlackError):
            pub.update("C1", "100.0", "본문", rich=True)
        assert 기록 and 기록[0]["error"] == "cant_update_message"


class TestSlackGateway:
    def test_등록한_핸들러_전부에게_이벤트를_분배한다(self) -> None:
        gateway = SlackGateway(client=FakeWebClient())
        received: list[tuple[str, Mapping[str, Any]]] = []
        gateway.on("app_mention", lambda e: received.append(("a", e)))
        gateway.on("app_mention", lambda e: received.append(("b", e)))
        event = {"channel": "C1"}
        gateway.dispatch("app_mention", event)
        assert received == [("a", event), ("b", event)]

    def test_등록되지_않은_이벤트타입은_아무_일도_하지_않는다(self) -> None:
        """다른 타입의 핸들러가 관찰 지점이다. 부작용의 부재만 보면 아무것도
        안 부르는 구현과 전부 부르는 구현이 같아 보인다."""
        gateway = SlackGateway(client=FakeWebClient())
        받은것: list[Mapping[str, Any]] = []
        gateway.on("app_mention", 받은것.append)
        gateway.dispatch("reaction_added", {"reaction": "dango"})
        assert 받은것 == []

    def test_핸들러_개수를_센다(self) -> None:
        gateway = SlackGateway(client=FakeWebClient())
        assert gateway.handler_count("message") == 0
        gateway.on("message", lambda e: None)
        assert gateway.handler_count("message") == 1


class TestSlackGatewayConnection:
    """Socket Mode 로 들어온 것을 분배하고 연결을 맺는 부분."""

    def test_events_api_payload_를_이벤트_타입으로_분배한다(self) -> None:
        gateway = SlackGateway(client=FakeWebClient())
        received: list[Mapping[str, Any]] = []
        gateway.on("app_mention", received.append)
        gateway.handle_events_api({"event": {"type": "app_mention", "channel": "C1"}})
        assert received == [{"type": "app_mention", "channel": "C1"}]

    def test_event_가_없는_payload_는_아무_일도_하지_않는다(self) -> None:
        """타입이 빈 문자열인 핸들러까지 걸어 둔다. 빈 event 를 타입 "" 로
        분배하는 구현이면 여기서 걸린다."""
        gateway = SlackGateway(client=FakeWebClient())
        받은것: list[Mapping[str, Any]] = []
        gateway.on("app_mention", 받은것.append)
        gateway.on("", 받은것.append)
        gateway.handle_events_api({})
        gateway.handle_events_api({"event": {}})
        assert 받은것 == []

    def test_핸들러_예외가_밖으로_나가지_않고_기록된다(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """한 이벤트의 실패로 소켓 연결이 끊기면 그 뒤 요청이 전부 사라진다."""

        def boom(event: Mapping[str, Any]) -> None:
            raise RuntimeError("핸들러 실패")

        gateway = SlackGateway(client=FakeWebClient())
        gateway.on("message", boom)
        with caplog.at_level(logging.ERROR):
            gateway.handle_events_api({"event": {"type": "message"}})
        assert caplog.records

    def test_토큰이_비어_있으면_연결하지_않는다(self) -> None:
        gateway = SlackGateway(client=FakeWebClient())
        with pytest.raises(ConfigError):
            gateway.start("")

    def test_주입한_연결기를_게이트웨이와_토큰으로_부른다(self) -> None:
        seen: list[tuple[object, str]] = []
        gateway = SlackGateway(
            client=FakeWebClient(), connector=lambda gw, token: seen.append((gw, token))
        )
        gateway.start("xapp-1-token")
        assert seen == [(gateway, "xapp-1-token")]


class TestSlackGateway연결성공로그:
    """sca-0nr: 실패만 기록되고 성공은 조용해서, 연결됐는지 죽었는지 로그만
    보고는 구분할 수 없었다. 소켓이 실제로 연결된 시점에 한 줄을 남긴다."""

    def test_연결되면_워크스페이스와_봇과_프로필이_담긴_로그를_남긴다(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = FakeWebClient()
        client.auth_test_response = {"team": "테스트팀", "user": "봇계정"}
        gateway = SlackGateway(client=client, profile_name="example")
        with caplog.at_level(logging.INFO):
            gateway.log_connected()
        assert any(
            "테스트팀" in r.message and "봇계정" in r.message and "example" in r.message
            for r in caplog.records
        )

    def test_auth_test가_실패해도_예외_없이_확인_안_됨으로_남는다(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        class BrokenClient:
            def auth_test(self) -> dict:
                raise RuntimeError("네트워크 장애")

        gateway = SlackGateway(client=BrokenClient(), profile_name="example")
        with caplog.at_level(logging.INFO):
            gateway.log_connected()
        assert any("확인 안 됨" in r.message for r in caplog.records)

    def test_재연결_로그는_최초_연결과_문구가_다르다(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = FakeWebClient()
        client.auth_test_response = {"team": "테스트팀", "user": "봇계정"}
        gateway = SlackGateway(client=client)
        with caplog.at_level(logging.INFO):
            gateway.log_connected()
            gateway.log_connected(reconnect=True)
        messages = [r.message for r in caplog.records]
        assert not any("재연결" in m for m in messages[:1])
        assert any("재연결" in m for m in messages[1:])

    def test_토큰이_로그에_안_남는다(self, caplog: pytest.LogCaptureFixture) -> None:
        client = FakeWebClient()
        client.auth_test_response = {"team": "테스트팀", "user": "봇계정"}
        gateway = SlackGateway(client=client)
        with caplog.at_level(logging.INFO):
            gateway.log_connected()
        assert not any("xoxb-" in r.message or "xapp-" in r.message for r in caplog.records)


class Test연결_상태_전환_감지:
    """ConnectionEdgeDetector — is_connected() 를 주기적으로 찍은 값들에서
    "방금 연결됐다" 시점만 골라낸다. 매 주기 로그를 남기면 조용한 정상
    상태와 소켓이 실제로 붙는 순간을 구분할 수 없다."""

    def test_최초_연결에서_initial을_낸다(self) -> None:
        from slack_cli_agent.slack.gateway import ConnectionEdgeDetector

        detector = ConnectionEdgeDetector()
        assert detector.on_poll(True) == "initial"

    def test_연결된_채로_반복_확인해도_다시_안_낸다(self) -> None:
        from slack_cli_agent.slack.gateway import ConnectionEdgeDetector

        detector = ConnectionEdgeDetector()
        assert detector.on_poll(True) == "initial"
        assert detector.on_poll(True) is None
        assert detector.on_poll(True) is None

    def test_끊긴_뒤_다시_붙으면_reconnect를_낸다(self) -> None:
        from slack_cli_agent.slack.gateway import ConnectionEdgeDetector

        detector = ConnectionEdgeDetector()
        assert detector.on_poll(True) == "initial"
        assert detector.on_poll(False) is None
        assert detector.on_poll(False) is None
        assert detector.on_poll(True) == "reconnect"

    def test_연결_전에는_아무것도_안_낸다(self) -> None:
        from slack_cli_agent.slack.gateway import ConnectionEdgeDetector

        detector = ConnectionEdgeDetector()
        assert detector.on_poll(False) is None


# EventListener


@pytest.fixture
def gate() -> ResponseGate:
    return ResponseGate()


class TestEventListener:
    def test_app_mention은_항상_컨텍스트를_만든다(self, gate: ResponseGate) -> None:
        registry = ChannelRegistry(Path("/nonexistent.json"))
        listener = EventListener(FakeWebClient(), registry, gate, identity=fake_identity())
        event = {"channel": "C1", "user": "U1", "ts": "1.0", "text": "<@U_BOT> 안녕"}
        ctx = listener.from_app_mention(event)
        assert isinstance(ctx, RequestContext)
        assert ctx.unaddressed is False
        assert ctx.thread_ts == "1.0"

    def test_DM_메시지는_바로_컨텍스트가_된다(self, gate: ResponseGate) -> None:
        registry = ChannelRegistry(Path("/nonexistent.json"))
        listener = EventListener(FakeWebClient(), registry, gate, identity=fake_identity())
        event = {"channel": "D1", "user": "U1", "ts": "1.0", "text": "안녕",
                 "channel_type": "im"}
        ctx = listener.from_message(event)
        assert ctx is not None
        assert ctx.is_direct_message is True

    def test_봇_자신의_메시지는_거른다(self, gate: ResponseGate) -> None:
        registry = ChannelRegistry(Path("/nonexistent.json"))
        listener = EventListener(FakeWebClient(), registry, gate, identity=fake_identity())
        event = {"channel": "D1", "user": "U_BOT", "ts": "1.0", "text": "안녕",
                 "channel_type": "im", "bot_id": "B1"}
        assert listener.from_message(event) is None

    def test_멘션이_있는_채널_메시지는_app_mention이_이미_받으므로_거른다(
        self, gate: ResponseGate
    ) -> None:
        registry = ChannelRegistry(Path("/nonexistent.json"))
        listener = EventListener(FakeWebClient(), registry, gate, identity=fake_identity())
        event = {"channel": "C1", "user": "U1", "ts": "2.0", "thread_ts": "1.0",
                 "text": "<@U_BOT> 다시 봐줘"}
        assert listener.from_message(event) is None

    def test_등록안된_채널의_스레드_답글은_이름을_불러야만_받는다(
        self, gate: ResponseGate
    ) -> None:
        registry = ChannelRegistry(Path("/nonexistent.json"))  # 빈 레지스트리
        listener = EventListener(FakeWebClient(), registry, gate, identity=fake_identity())
        event = {"channel": "C1", "user": "U1", "ts": "2.0", "thread_ts": "1.0",
                 "text": "고마워"}
        assert listener.from_message(event) is None

    def test_answer_unaddressed가_켜진_채널에서_봇이_낀_스레드의_되물음답은_받는다(
        self, gate: ResponseGate, tmp_path: Path
    ) -> None:
        channels_file = tmp_path / "channels.json"
        channels_file.write_text(
            '{"C1": {"answer_unaddressed": true}}', encoding="utf-8"
        )
        registry = ChannelRegistry(channels_file)
        client = FakeWebClient()
        client.set_replies_response({
            "messages": [{"user": "U_BOT", "text": "지금 반영할까요?"}]
        })
        listener = EventListener(client, registry, gate, identity=fake_identity())
        event = {"channel": "C1", "user": "U1", "ts": "2.0", "thread_ts": "1.0",
                 "text": "네"}
        ctx = listener.from_message(event)
        assert ctx is not None
        assert ctx.unaddressed is True

    def test_봇이_안_낀_스레드는_거른다(self, gate: ResponseGate, tmp_path: Path) -> None:
        channels_file = tmp_path / "channels.json"
        channels_file.write_text(
            '{"C1": {"answer_unaddressed": true}}', encoding="utf-8"
        )
        registry = ChannelRegistry(channels_file)
        client = FakeWebClient()
        client.set_replies_response({"messages": [{"user": "U1", "text": "아무 말"}]})
        listener = EventListener(client, registry, gate, identity=fake_identity())
        event = {"channel": "C1", "user": "U1", "ts": "2.0", "thread_ts": "1.0",
                 "text": "고마워"}
        assert listener.from_message(event) is None

    def test_다른_봇만_말한_스레드는_이봇이_낀_것이_아니다(
        self, gate: ResponseGate, tmp_path: Path
    ) -> None:
        """`bot_id` 가 있다는 것만으로 이 봇으로 보면 다른 봇이 답한 스레드에 끼어든다."""
        channels_file = tmp_path / "channels.json"
        channels_file.write_text('{"C1": {"answer_unaddressed": true}}', encoding="utf-8")
        registry = ChannelRegistry(channels_file)
        client = FakeWebClient()
        client.set_replies_response({
            "messages": [{"bot_id": "B_OTHER", "text": "지금 반영할까요?"}]
        })
        listener = EventListener(client, registry, gate, identity=fake_identity())
        event = {"channel": "C1", "user": "U1", "ts": "2.0", "thread_ts": "1.0",
                 "text": "네"}
        assert listener.from_message(event) is None

    def test_이봇의_bot_id_로_말한_스레드는_낀_것이다(
        self, gate: ResponseGate, tmp_path: Path
    ) -> None:
        """봇이 올린 메시지에는 `user` 대신 `bot_id` 가 담긴다. 그 경로도 판정돼야 한다."""
        channels_file = tmp_path / "channels.json"
        channels_file.write_text('{"C1": {"answer_unaddressed": true}}', encoding="utf-8")
        registry = ChannelRegistry(channels_file)
        client = FakeWebClient()
        client.set_replies_response({
            "messages": [{"bot_id": "B_BOT", "text": "지금 반영할까요?"}]
        })
        listener = EventListener(client, registry, gate, identity=fake_identity())
        event = {"channel": "C1", "user": "U1", "ts": "2.0", "thread_ts": "1.0",
                 "text": "네"}
        ctx = listener.from_message(event)
        assert ctx is not None
        assert ctx.unaddressed is True

    def test_리액션은_대상_이모지와_봇_자신의_답변일_때만_받는다(
        self, gate: ResponseGate
    ) -> None:
        registry = ChannelRegistry(Path("/nonexistent.json"))
        listener = EventListener(FakeWebClient(), registry, gate, identity=fake_identity())
        allowed = frozenset({POSTMORTEM_EMOJI, FORMAT_REVIEW_EMOJI})
        good = {"reaction": POSTMORTEM_EMOJI, "item": {"type": "message", "channel": "C1", "ts": "1.0"},
                "item_user": "U_BOT", "user": "U1"}
        assert listener.from_reaction(good, allowed) == (POSTMORTEM_EMOJI, "C1", "1.0", "U1")

        not_bot_msg = {**good, "item_user": "U1"}
        assert listener.from_reaction(not_bot_msg, allowed) is None

        wrong_emoji = {**good, "reaction": "thumbsup"}
        assert listener.from_reaction(wrong_emoji, allowed) is None

        self_reaction = {**good, "user": "U_BOT"}
        assert listener.from_reaction(self_reaction, allowed) is None

    def test_item_user_가_없으면_받지_않는다(self, gate: ResponseGate) -> None:
        """실제 reaction_added 에는 item_user 가 담긴다. 없는 이벤트를 받아들이면
        아무 메시지에나 점검이 발동한다."""
        registry = ChannelRegistry(Path("/nonexistent.json"))
        client = FakeWebClient()
        listener = EventListener(client, registry, gate, identity=fake_identity())
        event = {"reaction": POSTMORTEM_EMOJI,
                 "item": {"type": "message", "channel": "C1", "ts": "1.0"},
                 "user": "U1"}
        assert listener.from_reaction(event, frozenset({POSTMORTEM_EMOJI})) is None
        assert client.calls == []

    def test_신원을_모르면_리액션을_받지_않는다(self, gate: ResponseGate) -> None:
        """신원 조회가 실패한 구간에서는 item_user 대조를 못 한다. 그대로 받으면
        소유자나 신뢰 사용자가 남의 메시지에 이모지를 달아도 점검이 돈다."""
        registry = ChannelRegistry(Path("/nonexistent.json"))
        listener = EventListener(
            FakeWebClient(), registry, gate, identity=fake_identity(user_id="", bot_id="")
        )
        event = {"reaction": POSTMORTEM_EMOJI,
                 "item": {"type": "message", "channel": "C1", "ts": "1.0"},
                 "item_user": "U1", "user": "U2"}
        assert listener.from_reaction(event, frozenset({POSTMORTEM_EMOJI})) is None


# TranscriptBuilder


@pytest.fixture
def notices() -> NoticeCatalog:
    return NoticeCatalog()


class TestTranscriptBuilder:
    def test_스레드_기록을_화자표시가_붙은_기록으로_만든다(
        self, settings: RuntimeSettings, notices: NoticeCatalog
    ) -> None:
        client = FakeWebClient()
        client.set_replies_response({
            "messages": [
                {"ts": "1700000000.000001", "user": "U1", "text": "안녕하세요"},
                {"ts": "1700000001.000001", "user": "U_BOT", "text": "네 안녕하세요"},
            ]
        })
        builder = TranscriptBuilder(
            client, settings, notices,
            name_resolver=lambda uid: {"U1": "김철수"}.get(uid, ""),
            identity=fake_identity(), bot_display_name="테스트봇",
        )
        body = builder.thread_transcript("C1", "1700000000.000001", before_ts=None)
        assert "김철수" in body
        assert "테스트봇" in body
        assert "안녕하세요" in body

    def test_파일을_붙여_보낸_말도_기록에_넣는다(
        self, settings: RuntimeSettings, notices: NoticeCatalog
    ) -> None:
        """기록에서 빠지면 모델이 그 말을 못 보고 답한다."""
        client = FakeWebClient()
        client.set_replies_response({
            "messages": [
                {"ts": "1700000000.000001", "user": "U1", "subtype": "file_share", "text": "이 그림 봐줘"},
            ]
        })
        builder = TranscriptBuilder(
            client, settings, notices,
            name_resolver=lambda uid: {"U1": "김철수"}.get(uid, ""),
            identity=fake_identity(), bot_display_name="테스트봇",
        )
        body = builder.thread_transcript("C1", "1700000000.000001", before_ts=None)
        assert "이 그림 봐줘" in body

    def test_채널_참여_알림은_기록에_안_넣는다(
        self, settings: RuntimeSettings, notices: NoticeCatalog
    ) -> None:
        client = FakeWebClient()
        client.set_replies_response({
            "messages": [
                {"ts": "1700000000.000001", "user": "U1", "subtype": "channel_join", "text": "들어왔습니다"},
            ]
        })
        builder = TranscriptBuilder(
            client, settings, notices,
            name_resolver=lambda uid: {"U1": "김철수"}.get(uid, ""),
            identity=fake_identity(), bot_display_name="테스트봇",
        )
        assert "들어왔습니다" not in builder.thread_transcript("C1", "1700000000.000001", before_ts=None)

    def test_다른_봇의_말은_이봇의_이름으로_적히지_않는다(
        self, settings: RuntimeSettings, notices: NoticeCatalog
    ) -> None:
        """화자 표시만 어긋나는 것이 아니다. 캐치업이 같은 판정을 쓴다."""
        client = FakeWebClient()
        client.set_replies_response({
            "messages": [
                {"ts": "1700000000.000001", "bot_id": "B_OTHER",
                 "bot_profile": {"name": "잠만보"}, "text": "다른 봇의 답"},
            ]
        })
        builder = TranscriptBuilder(
            client, settings, notices, name_resolver=lambda uid: "",
            identity=fake_identity(), bot_display_name="테스트봇",
        )
        body = builder.thread_transcript("C1", "1700000000.000001", before_ts=None)
        assert "다른 봇의 답" in body
        assert "테스트봇" not in body
        assert "잠만보" in body

    def test_공지문은_기록에서_제외한다(
        self, settings: RuntimeSettings, notices: NoticeCatalog
    ) -> None:
        client = FakeWebClient()
        client.set_replies_response({
            "messages": [
                {"ts": "1700000000.000001", "user": "U1", "text": notices.render("ask_what")},
            ]
        })
        builder = TranscriptBuilder(
            client, settings, notices, name_resolver=lambda uid: "김철수",
            identity=fake_identity(),
        )
        body = builder.thread_transcript("C1", "1700000000.000001", before_ts=None)
        assert body == ""

    def _builder(
        self, settings: RuntimeSettings, notices: NoticeCatalog
    ) -> TranscriptBuilder:
        return TranscriptBuilder(
            FakeWebClient(), settings, notices, name_resolver=lambda uid: "김철수",
            identity=fake_identity(),
        )

    def test_with_history는_지난_대화가_없어도_화자_머리를_붙인다(
        self, settings: RuntimeSettings, notices: NoticeCatalog
    ) -> None:
        out = self._builder(settings, notices).with_history(
            "", CurrentMessage(ts="1700000000.000001", user="U1", raw_text="지금 말", body="지금 말")
        )
        assert out.startswith("[")
        assert "김철수" in out.splitlines()[0]
        assert out.endswith("지금 말")

    def test_with_history는_지난_대화를_앞에_붙이고_다시_답하지_말라고_못박는다(
        self, settings: RuntimeSettings, notices: NoticeCatalog
    ) -> None:
        out = self._builder(settings, notices).with_history(
            "[10:00 김철수]\n안녕",
            CurrentMessage(ts="1700000000.000001", user="U1", raw_text="지금 말", body="지금 말"),
        )
        assert "지난 대화" in out
        assert "안녕" in out
        assert out.endswith("지금 말")
        assert out.splitlines()[-2].startswith("[")

    def test_현재_발언_머리는_대화록_줄과_같은_형식이다(
        self, settings: RuntimeSettings, notices: NoticeCatalog
    ) -> None:
        builder = self._builder(settings, notices)
        head = builder.tag_current(
            CurrentMessage(ts="1700000000.000001", user="U1", raw_text="<@U9> 봐라", body="<@U9> 봐라")
        ).splitlines()[0]
        assert re.match(r"^\[\d\d:\d\d:\d\d .+ -> .+\]$", head), head

    def test_with_history는_머리없는_문자열을_거부한다(
        self, settings: RuntimeSettings, notices: NoticeCatalog
    ) -> None:
        # The regression itself: a bare body used to pass straight through.
        with pytest.raises(TypeError):
            self._builder(settings, notices).with_history("", "지금 말")  # type: ignore[arg-type]


# AttachmentStore


class TestAttachmentStore:
    def test_첨부를_내려받아_저장한다(self, tmp_path: Path) -> None:
        attach_dir = tmp_path / "attach"

        def downloader(url: str, token: str) -> DownloadResult:
            return DownloadResult(content_type="image/png", data=b"pngdata")

        store = AttachmentStore(
            attach_dir=attach_dir,
            token_provider=lambda: "xoxb-token",
            downloader=downloader,
        )
        event = {"ts": "1700000000.0", "files": [
            {"name": "photo.png", "url_private_download": "https://slack/x", "size": 10}
        ]}
        saved = store.save(event)
        assert len(saved) == 1
        assert Path(saved[0].path).read_bytes() == b"pngdata"
        assert saved[0].name == "photo.png"

    def test_토큰이_없으면_건너뛴다(self, tmp_path: Path) -> None:
        store = AttachmentStore(
            attach_dir=tmp_path / "attach",
            token_provider=lambda: "",
            downloader=lambda url, token: DownloadResult("image/png", b"x"),
        )
        saved = store.save({"ts": "1", "files": [{"name": "a.png", "url_private_download": "u"}]})
        assert saved == []

    def test_권한없는_로그인화면이_돌아오면_건너뛴다(self, tmp_path: Path) -> None:
        """2026-08-25 원본 사고 : 권한 없는 다운로드가 200과 HTML 로그인 화면을 준다."""
        def downloader(url: str, token: str) -> DownloadResult:
            return DownloadResult(content_type="text/html", data=b"<html>login</html>")

        store = AttachmentStore(
            attach_dir=tmp_path / "attach",
            token_provider=lambda: "xoxb-token",
            downloader=downloader,
        )
        saved = store.save({"ts": "1", "files": [{"name": "a.png", "url_private_download": "u", "size": 5}]})
        assert saved == []

    def test_상한을_넘는_첨부는_건너뛴다(self, tmp_path: Path) -> None:
        store = AttachmentStore(
            attach_dir=tmp_path / "attach",
            token_provider=lambda: "xoxb-token",
            downloader=lambda url, token: DownloadResult("image/png", b"x"),
            max_bytes=100,
        )
        saved = store.save({"ts": "1", "files": [
            {"name": "a.png", "url_private_download": "u", "size": 1000}
        ]})
        assert saved == []

    def test_cleanup은_보관기한이_지난_파일을_지운다(self, tmp_path: Path) -> None:
        attach_dir = tmp_path / "attach"
        sub = attach_dir / "1700000000.0"
        sub.mkdir(parents=True)
        old_file = sub / "old.png"
        old_file.write_bytes(b"x")
        import os
        old_time = 0
        os.utime(old_file, (old_time, old_time))

        store = AttachmentStore(
            attach_dir=attach_dir,
            token_provider=lambda: "x",
            downloader=lambda url, token: DownloadResult("image/png", b"x"),
            keep_hours=1,
        )
        지운수 = store.cleanup(now=old_time + 3600 * 2)
        assert not old_file.exists()
        assert not sub.exists()
        assert 지운수 == 1

    def test_cleanup은_지운_건수를_돌려준다(self, tmp_path: Path) -> None:
        """건수를 안 돌려주면 0건과 안 돈 것이 로그에서 같아 보인다(sca-mf6)."""
        attach_dir = tmp_path / "attach"
        attach_dir.mkdir(parents=True)
        store = AttachmentStore(
            attach_dir=attach_dir,
            token_provider=lambda: "x",
            downloader=lambda url, token: DownloadResult("image/png", b"x"),
            keep_hours=1,
        )
        assert store.cleanup(now=1_700_000_000.0) == 0

    def test_cleanup은_실패를_삼키지_않고_기록한다(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """조용히 pass 하면 권한 문제로 한 번도 못 지운 상태가 정상과 같아 보인다."""
        import os

        attach_dir = tmp_path / "attach"
        sub = attach_dir / "1700000000.0"
        sub.mkdir(parents=True)
        오래된파일 = sub / "old.png"
        오래된파일.write_bytes(b"x")
        os.utime(오래된파일, (0, 0))
        # 부모 디렉터리에 쓰기 권한이 없으면 unlink 가 실패한다.
        sub.chmod(0o500)
        store = AttachmentStore(
            attach_dir=attach_dir,
            token_provider=lambda: "x",
            downloader=lambda url, token: DownloadResult("image/png", b"x"),
            keep_hours=1,
        )
        try:
            with caplog.at_level(logging.WARNING):
                assert store.cleanup(now=3600 * 2) == 0
        finally:
            sub.chmod(0o700)
        assert [r for r in caplog.records if "첨부" in r.getMessage()]


class Test완료_표식은_미완료_표식을_전부_지운다:
    """접수와 처리가 다른 프로세스로 갈리면서 미완료 표식이 둘이 됐다.

    접수 프로세스가 모래시계를, 처리 프로세스가 눈을 단다. 완료 표식이 눈만
    지우면 끝난 요청에 모래시계가 그대로 남아, 사람 눈에는 아직 대기 중으로
    보인다.
    """

    def _removed(self, method: str) -> set[str]:
        client = FakeWebClient()
        getattr(ReactionMarker(client), method)("C1", "1.0")
        return {name for _, _, name in client.reaction_remove_calls}

    def test_mark_done이_모래시계도_지운다(self) -> None:
        assert "hourglass" in self._removed("mark_done")

    def test_mark_silent이_모래시계도_지운다(self) -> None:
        assert "hourglass" in self._removed("mark_silent")

    def test_mark_watch가_모래시계도_지운다(self) -> None:
        assert "hourglass" in self._removed("mark_watch")

    def test_실패했던_요청이_성공하면_실패_표식도_지운다(self) -> None:
        """재시도로 성공했는데 x 가 남으면 실패한 것으로 읽힌다."""
        assert "x" in self._removed("mark_done")

    def test_mark_failed는_자기가_달_표식을_지우지_않는다(self) -> None:
        """x 는 미완료 표식이면서 실패 표식이다. 지웠다 다시 달지 않는다."""
        assert self._removed("mark_failed") == {"eyes", "hourglass", WATCH_MARK_EMOJI}


class Test감시표식도_정리_대상이다:
    """감시 표식은 미완료 표식이다. 최종 표식을 달 때 함께 떼지 않으면 mag 와
    x 가 한 메시지에 같이 남는다. 떼는 자리를 한 곳으로 모아, 호출부가 따로
    remove 를 부르다 조용히 실패하는 경로를 없앤다 (sca-3p6, 코덱스 검토).
    """

    def test_완료표식이_감시표식을_뗀다(self) -> None:
        client = FakeWebClient()
        ReactionMarker(client).mark_done("C1", "1.0")
        removed = {name for _, _, name in client.reaction_remove_calls}
        assert WATCH_MARK_EMOJI in removed

    def test_실패표식이_감시표식을_뗀다(self) -> None:
        client = FakeWebClient()
        ReactionMarker(client).mark_failed("C1", "1.0")
        removed = {name for _, _, name in client.reaction_remove_calls}
        assert WATCH_MARK_EMOJI in removed

    def test_감시표식을_달_때는_자기를_안_뗀다(self) -> None:
        """떼고 다시 다는 것은 슬랙 호출만 한 번 더 쓴다."""
        client = FakeWebClient()
        ReactionMarker(client).mark_watch("C1", "1.0")
        removed = {name for _, _, name in client.reaction_remove_calls}
        assert WATCH_MARK_EMOJI not in removed
        assert client.reaction_add_calls == [("C1", "1.0", WATCH_MARK_EMOJI)]

    def test_미완료_판정_집합은_원본_그대로다(self) -> None:
        """복구 스캔이 보는 집합과 최종 표식이 지우는 집합은 다르다. 전자를
        넓히면 감시 위임 건이 복구 대상으로 다시 실행된다."""
        assert WATCH_MARK_EMOJI not in UNFINISHED_EMOJI
        assert UNFINISHED_EMOJI | {WATCH_MARK_EMOJI} == STALE_ON_SETTLE


class Test감시표식_제거_실패는_운영_로그에_남는다:
    """실제 ReactionMarker.remove 는 슬랙 예외를 삼킨다. 감시 표식 제거만
    실패하면 mag 와 최종 표식이 함께 남는데, 로그가 debug 라 운영 수준(INFO)
    에서는 아무 흔적이 없다. 실패를 못 보면 재시도 설계도 못 한다 (sca-3p6).

    슬랙은 원래 그 이모지가 없을 때도 no_reaction 오류를 낸다. 그것은 지우려던
    상태에 이미 도달한 것이므로 실패로 세지 않는다.
    """

    class 일부실패클라이언트:
        def __init__(self, 실패이름: str, 사유: str = "ratelimited") -> None:
            self.실패이름 = 실패이름
            self.사유 = 사유
            self.removed: list[str] = []
            self.added: list[str] = []

        def reactions_remove(self, *, channel: str, timestamp: str, name: str) -> None:
            if name == self.실패이름:
                raise RuntimeError(self.사유)
            self.removed.append(name)

        def reactions_add(self, *, channel: str, timestamp: str, name: str) -> None:
            self.added.append(name)

    def test_감시표식만_제거_실패하면_경고를_남긴다(self, caplog) -> None:
        client = self.일부실패클라이언트(WATCH_MARK_EMOJI)
        with caplog.at_level(logging.WARNING, logger="slack_cli_agent.slack.reactions"):
            ReactionMarker(client).mark_done("C1", "1.0")

        assert client.added == ["white_check_mark"]
        경고 = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert 경고, "감시 표식이 남았는데 운영 로그에 아무 흔적이 없다"
        assert any(WATCH_MARK_EMOJI in r.getMessage() for r in 경고)

    def test_없어서_난_오류는_경고가_아니다(self, caplog) -> None:
        """no_reaction 은 그 이모지가 애초에 없었다는 뜻이다. 목표는 달성됐다."""
        client = self.일부실패클라이언트(WATCH_MARK_EMOJI, 사유="no_reaction")
        with caplog.at_level(logging.WARNING, logger="slack_cli_agent.slack.reactions"):
            ReactionMarker(client).mark_done("C1", "1.0")

        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    def test_다_지워지면_경고가_없다(self, caplog) -> None:
        client = self.일부실패클라이언트("없는이름")
        with caplog.at_level(logging.WARNING, logger="slack_cli_agent.slack.reactions"):
            ReactionMarker(client).mark_failed("C1", "1.0")

        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    def test_remove가_제거_여부를_돌려준다(self) -> None:
        """호출부가 실패를 알 방법이 없으면 재시도도 경고도 못 만든다."""
        client = self.일부실패클라이언트(WATCH_MARK_EMOJI)
        marker = ReactionMarker(client)
        assert marker.remove("C1", "1.0", "eyes") is True
        assert marker.remove("C1", "1.0", WATCH_MARK_EMOJI) is False

    def test_no_reaction이면_제거된_것으로_본다(self) -> None:
        client = self.일부실패클라이언트(WATCH_MARK_EMOJI, 사유="no_reaction")
        assert ReactionMarker(client).remove("C1", "1.0", WATCH_MARK_EMOJI) is True
