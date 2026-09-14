"""slack/ 패키지 시험.

이식 대상(HistoryReader.slack_ts/read_history/wait_history_slot, 빈 응답
재시도, Block Kit 거절 시 평문 낮춤)의 기대값은 원본 bot.py
의 slack_ts, read_history, wait_history_slot, post 를 실제로 읽고 그 로직을
그대로 따라 만들었다. 손으로 짐작하지 않았다.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from slack_cli_agent.config.channel import ChannelRegistry
from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.core.context import RequestContext
from slack_cli_agent.core.errors import ConfigError, HistoryUnavailable, SlackError
from slack_cli_agent.observability.notices import NoticeCatalog
from slack_cli_agent.render.blocks import BlockBuilder
from slack_cli_agent.render.markdown import MarkdownConverter
from slack_cli_agent.render.splitter import ContentSplitter
from slack_cli_agent.render.verifier import SplitVerifier
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
    UNFINISHED_EMOJI,
    ReactionMarker,
)
from slack_cli_agent.slack.transcript import TranscriptBuilder

# ---------------------------------------------------------------------------
# 대역 슬랙 클라이언트
# ---------------------------------------------------------------------------


class FakeWebClient:
    """실제 슬랙 응답 형태를 흉내내는 대역.

    각 메서드 호출을 기록하고, 미리 채워 둔 응답 큐에서 하나씩 꺼내 돌려준다.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._history_responses: list[dict] = []
        self._post_responses: list[Any] = []
        self.reaction_add_calls: list[tuple[str, str, str]] = []
        self.reaction_remove_calls: list[tuple[str, str, str]] = []
        self._raise_on_reaction_add: set[str] = set()
        self._replies_response: dict = {"messages": []}

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

    def reactions_add(self, *, channel, timestamp, name):
        if name in self._raise_on_reaction_add:
            raise RuntimeError("boom")
        self.reaction_add_calls.append((channel, timestamp, name))

    def reactions_remove(self, *, channel, timestamp, name):
        self.reaction_remove_calls.append((channel, timestamp, name))


class SlackApiError(Exception):
    def __init__(self, error: str) -> None:
        super().__init__(error)
        self.response = {"error": error}


# ---------------------------------------------------------------------------
# HistoryReader
# ---------------------------------------------------------------------------


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
        assert clock.sleeps.count(pytest.approx(settings.history_read_pause_sec)) >= 1

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


# ---------------------------------------------------------------------------
# ReactionMarker
# ---------------------------------------------------------------------------


class TestReactionMarker:
    def test_add_실패해도_조용히_넘긴다(self) -> None:
        client = FakeWebClient()
        client._raise_on_reaction_add.add("eyes")
        marker = ReactionMarker(client)
        marker.mark_processing("C1", "1.0")  # 예외를 내지 않아야 한다
        assert client.reaction_add_calls == []

    def test_mark_done은_미완료_표식을_떼고_체크를_단다(self) -> None:
        client = FakeWebClient()
        marker = ReactionMarker(client)
        marker.mark_done("C1", "1.0")
        removed = {name for _, _, name in client.reaction_remove_calls}
        assert removed == set(UNFINISHED_EMOJI)
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
        assert removed == set(UNFINISHED_EMOJI)
        assert client.reaction_add_calls == [("C1", "1.0", "white_check_mark")]

    def test_already_handled은_완료표식이_있을때만_참이다(self) -> None:
        marker = ReactionMarker(FakeWebClient())
        done_msg = {"reactions": [{"name": "white_check_mark"}]}
        unfinished_msg = {"reactions": [{"name": "eyes"}]}
        assert marker.already_handled(done_msg) is True
        assert marker.already_handled(unfinished_msg) is False
        assert marker.already_handled({}) is False


# ---------------------------------------------------------------------------
# MessagePublisher
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# SlackGateway
# ---------------------------------------------------------------------------


class TestSlackGateway:
    def test_등록한_핸들러_전부에게_이벤트를_분배한다(self) -> None:
        gateway = SlackGateway(client=FakeWebClient())
        received: list[dict] = []
        gateway.on("app_mention", lambda e: received.append(("a", e)))
        gateway.on("app_mention", lambda e: received.append(("b", e)))
        event = {"channel": "C1"}
        gateway.dispatch("app_mention", event)
        assert received == [("a", event), ("b", event)]

    def test_등록되지_않은_이벤트타입은_아무_일도_하지_않는다(self) -> None:
        gateway = SlackGateway(client=FakeWebClient())
        gateway.dispatch("reaction_added", {"reaction": "dango"})  # 예외 없음

    def test_핸들러_개수를_센다(self) -> None:
        gateway = SlackGateway(client=FakeWebClient())
        assert gateway.handler_count("message") == 0
        gateway.on("message", lambda e: None)
        assert gateway.handler_count("message") == 1


class TestSlackGatewayConnection:
    """Socket Mode 로 들어온 것을 분배하고 연결을 맺는 부분."""

    def test_events_api_payload_를_이벤트_타입으로_분배한다(self) -> None:
        gateway = SlackGateway(client=FakeWebClient())
        received: list[dict] = []
        gateway.on("app_mention", received.append)
        gateway.handle_events_api({"event": {"type": "app_mention", "channel": "C1"}})
        assert received == [{"type": "app_mention", "channel": "C1"}]

    def test_event_가_없는_payload_는_아무_일도_하지_않는다(self) -> None:
        gateway = SlackGateway(client=FakeWebClient())
        gateway.handle_events_api({})
        gateway.handle_events_api({"event": {}})

    def test_핸들러_예외가_밖으로_나가지_않고_기록된다(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """한 이벤트의 실패로 소켓 연결이 끊기면 그 뒤 요청이 전부 사라진다."""

        def boom(event: dict) -> None:
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


# ---------------------------------------------------------------------------
# EventListener
# ---------------------------------------------------------------------------


@pytest.fixture
def gate() -> ResponseGate:
    return ResponseGate()


class TestEventListener:
    def test_app_mention은_항상_컨텍스트를_만든다(self, gate: ResponseGate) -> None:
        registry = ChannelRegistry(Path("/nonexistent.json"))
        listener = EventListener(FakeWebClient(), registry, gate, bot_user_id="U_BOT")
        event = {"channel": "C1", "user": "U1", "ts": "1.0", "text": "<@U_BOT> 안녕"}
        ctx = listener.from_app_mention(event)
        assert isinstance(ctx, RequestContext)
        assert ctx.unaddressed is False
        assert ctx.thread_ts == "1.0"

    def test_DM_메시지는_바로_컨텍스트가_된다(self, gate: ResponseGate) -> None:
        registry = ChannelRegistry(Path("/nonexistent.json"))
        listener = EventListener(FakeWebClient(), registry, gate, bot_user_id="U_BOT")
        event = {"channel": "D1", "user": "U1", "ts": "1.0", "text": "안녕",
                 "channel_type": "im"}
        ctx = listener.from_message(event)
        assert ctx is not None
        assert ctx.is_direct_message is True

    def test_봇_자신의_메시지는_거른다(self, gate: ResponseGate) -> None:
        registry = ChannelRegistry(Path("/nonexistent.json"))
        listener = EventListener(FakeWebClient(), registry, gate, bot_user_id="U_BOT")
        event = {"channel": "D1", "user": "U_BOT", "ts": "1.0", "text": "안녕",
                 "channel_type": "im", "bot_id": "B1"}
        assert listener.from_message(event) is None

    def test_멘션이_있는_채널_메시지는_app_mention이_이미_받으므로_거른다(
        self, gate: ResponseGate
    ) -> None:
        registry = ChannelRegistry(Path("/nonexistent.json"))
        listener = EventListener(FakeWebClient(), registry, gate, bot_user_id="U_BOT")
        event = {"channel": "C1", "user": "U1", "ts": "2.0", "thread_ts": "1.0",
                 "text": "<@U_BOT> 다시 봐줘"}
        assert listener.from_message(event) is None

    def test_등록안된_채널의_스레드_답글은_이름을_불러야만_받는다(
        self, gate: ResponseGate
    ) -> None:
        registry = ChannelRegistry(Path("/nonexistent.json"))  # 빈 레지스트리
        listener = EventListener(FakeWebClient(), registry, gate, bot_user_id="U_BOT")
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
        listener = EventListener(client, registry, gate, bot_user_id="U_BOT")
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
        listener = EventListener(client, registry, gate, bot_user_id="U_BOT")
        event = {"channel": "C1", "user": "U1", "ts": "2.0", "thread_ts": "1.0",
                 "text": "고마워"}
        assert listener.from_message(event) is None

    def test_리액션은_대상_이모지와_봇_자신의_답변일_때만_받는다(
        self, gate: ResponseGate
    ) -> None:
        registry = ChannelRegistry(Path("/nonexistent.json"))
        listener = EventListener(FakeWebClient(), registry, gate, bot_user_id="U_BOT")
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


# ---------------------------------------------------------------------------
# TranscriptBuilder
# ---------------------------------------------------------------------------


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
            bot_user_id="U_BOT", bot_display_name="테스트봇",
        )
        body = builder.thread_transcript("C1", "1700000000.000001", before_ts=None)
        assert "김철수" in body
        assert "테스트봇" in body
        assert "안녕하세요" in body

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
            bot_user_id="U_BOT",
        )
        body = builder.thread_transcript("C1", "1700000000.000001", before_ts=None)
        assert body == ""

    def test_with_history는_지난_대화가_없으면_그대로_돌려준다(
        self, settings: RuntimeSettings, notices: NoticeCatalog
    ) -> None:
        builder = TranscriptBuilder(
            FakeWebClient(), settings, notices, name_resolver=lambda uid: ""
        )
        assert builder.with_history("", "지금 말") == "지금 말"

    def test_with_history는_지난_대화를_앞에_붙이고_다시_답하지_말라고_못박는다(
        self, settings: RuntimeSettings, notices: NoticeCatalog
    ) -> None:
        builder = TranscriptBuilder(
            FakeWebClient(), settings, notices, name_resolver=lambda uid: ""
        )
        out = builder.with_history("[10:00 김철수]\n안녕", "지금 말")
        assert "지난 대화" in out
        assert "안녕" in out
        assert out.endswith("지금 말")


# ---------------------------------------------------------------------------
# AttachmentStore
# ---------------------------------------------------------------------------


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
        store.cleanup(now=old_time + 3600 * 2)
        assert not old_file.exists()
        assert not sub.exists()


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
        assert self._removed("mark_failed") == {"eyes", "hourglass"}
