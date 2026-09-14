"""IngressService — 슬랙 이벤트 수신부터 작업 큐 등록까지.

엔진을 부르지 않는다. 실제 처리는 워커(별도 프로세스)가 큐를 소비해 한다.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from slack_cli_agent.admin.command import AdminCommand, AdminContext, AdminResult
from slack_cli_agent.admin.router import AdminRouter
from slack_cli_agent.auth.principal import Principal, TrustLevel
from slack_cli_agent.config.channel import ChannelRegistry
from slack_cli_agent.config.profile import Profile
from slack_cli_agent.core.context import RequestContext
from slack_cli_agent.core.ingress import IngressService
from slack_cli_agent.jobs.ports import Job, JobQueue, ReclaimResult
from slack_cli_agent.reliability.dedup import DeduplicationTracker
from slack_cli_agent.slack.attachments import AttachmentStore, DownloadResult
from slack_cli_agent.slack.gate import ResponseGate
from slack_cli_agent.slack.gateway import SlackGateway
from slack_cli_agent.slack.listener import EventListener
from slack_cli_agent.slack.reactions import ReactionMarker


# ---------------------------------------------------------------------------
# 대역들
# ---------------------------------------------------------------------------


class FakeWebClient:
    """리액션과 스레드 조회만 흉내낸다. IngressService 시험에는 이 정도면 된다."""

    def __init__(self) -> None:
        self.reaction_add_calls: list[tuple[str, str, str]] = []
        self.reaction_remove_calls: list[tuple[str, str, str]] = []
        self._replies_response: dict = {"messages": []}

    def reactions_add(self, *, channel, timestamp, name):
        self.reaction_add_calls.append((channel, timestamp, name))

    def reactions_remove(self, *, channel, timestamp, name):
        self.reaction_remove_calls.append((channel, timestamp, name))

    def conversations_replies(self, **kwargs):
        return self._replies_response


class FakeJobQueue:
    """JobQueue 계약 중 enqueue 만 이 시험에 필요하다."""

    def __init__(self, *, enqueue_result: bool = True, raise_on_enqueue: bool = False) -> None:
        self.enqueued: list[RequestContext] = []
        self._enqueue_result = enqueue_result
        self._raise_on_enqueue = raise_on_enqueue
        # 등록 때 함께 넘어온 재시도 상한. 접수가 이 값을 안 넘기면
        # 실패로 끝난 건이 재전달돼도 되살아나지 않는다.
        self.enqueue_limits: list[int] = []

    def enqueue(self, ctx: RequestContext, max_attempts: int = 0) -> bool:
        if self._raise_on_enqueue:
            raise RuntimeError("큐 저장소 장애")
        self.enqueued.append(ctx)
        self.enqueue_limits.append(max_attempts)
        return self._enqueue_result

    def claim_next(self, worker_id: str) -> Job | None:
        raise NotImplementedError

    def heartbeat(self, job_id: int) -> None:
        raise NotImplementedError

    def complete(self, job_id: int, ok: bool, failure: str = "") -> None:
        raise NotImplementedError

    def requeue(self, job_id: int) -> None:
        raise NotImplementedError

    def reclaim_stale(self, deadline: float, max_attempts: int) -> ReclaimResult:
        raise NotImplementedError

    def pending(self, limit: int = 50) -> list[Job]:
        raise NotImplementedError

    def counts(self) -> dict[str, int]:
        raise NotImplementedError

    def purge_finished(self, before: float) -> int:
        raise NotImplementedError


class RecordingAdminCommand(AdminCommand):
    """본문이 "!ping" 이면 반응하는 관리 명령 대역."""

    name = "ping"
    required_trust = TrustLevel.GENERAL

    def matches(self, text: str) -> bool:
        return text.strip() == "!ping"

    def execute(self, ctx: AdminContext) -> AdminResult:
        return AdminResult(message="pong")


def make_profile(tmp_path: Path) -> Profile:
    return Profile.from_dict(
        {
            "name": "example",
            "display_name": "예시봇",
            "primary_engine": {"type": "claude", "binary": "claude", "model": "m"},
            "owner_user_id": "U_OWNER",
            "troubleshoot_channel": "C1",
            "state_dir": str(tmp_path / "state"),
        }
    )


@pytest.fixture
def gate() -> ResponseGate:
    return ResponseGate()


@pytest.fixture
def registry() -> ChannelRegistry:
    return ChannelRegistry(Path("/nonexistent.json"))


@pytest.fixture
def listener(registry: ChannelRegistry, gate: ResponseGate) -> EventListener:
    return EventListener(FakeWebClient(), registry, gate, bot_user_id="U_BOT")


@pytest.fixture
def admin_router() -> AdminRouter:
    return AdminRouter([RecordingAdminCommand()])


def make_ingress(
    *,
    listener: EventListener,
    queue: JobQueue,
    dedup: DeduplicationTracker | None = None,
    reactions: ReactionMarker | None = None,
    attachments: AttachmentStore | None = None,
    admin_router: AdminRouter,
    tmp_path: Path,
    replies: list[tuple[str, str, str]] | None = None,
    reactions_seen: list[tuple[str, str, str, str]] | None = None,
    job_max_attempts: int = 0,
) -> IngressService:
    profile = make_profile(tmp_path)
    channels = ChannelRegistry(Path("/nonexistent.json"))
    reply_log = replies if replies is not None else []
    reaction_log = reactions_seen if reactions_seen is not None else []

    def admin_context_builder(ctx: RequestContext) -> AdminContext:
        principal = Principal(
            user_id=ctx.user, channel=ctx.channel, trust=TrustLevel.GENERAL,
            is_direct_message=ctx.is_direct_message,
        )
        return AdminContext(
            principal=principal, channel=ctx.channel, thread_ts=ctx.thread_ts,
            channels=channels, profile=profile,
        )

    def reply(channel: str, thread_ts: str, text: str) -> None:
        reply_log.append((channel, thread_ts, text))

    def on_reaction(emoji: str, channel: str, ts: str, by_user: str) -> None:
        reaction_log.append((emoji, channel, ts, by_user))

    return IngressService(
        listener=listener,
        dedup=dedup if dedup is not None else DeduplicationTracker(),
        queue=queue,
        reactions=reactions if reactions is not None else ReactionMarker(FakeWebClient()),
        attachments=attachments
        if attachments is not None
        else AttachmentStore(
            attach_dir=tmp_path / "attach",
            token_provider=lambda: "",
            downloader=lambda url, token: DownloadResult("image/png", b""),
        ),
        admin_router=admin_router,
        admin_context_builder=admin_context_builder,
        reply=reply,
        allowed_reactions=frozenset({"dango"}),
        on_reaction=on_reaction,
        job_max_attempts=job_max_attempts,
    )


def mention_event(ts: str = "1.0", text: str = "<@U_BOT> 안녕") -> dict:
    return {"channel": "C1", "user": "U1", "ts": ts, "text": text}


# ---------------------------------------------------------------------------
# 시험
# ---------------------------------------------------------------------------


class TestQueueing:
    def test_멘션_이벤트가_큐에_들어간다(self, listener, admin_router, tmp_path) -> None:
        queue = FakeJobQueue()
        ingress = make_ingress(listener=listener, queue=queue, admin_router=admin_router, tmp_path=tmp_path)

        ingress.handle_app_mention(mention_event())

        assert len(queue.enqueued) == 1
        assert queue.enqueued[0].channel == "C1"
        assert queue.enqueued[0].ts == "1.0"

    def test_같은_이벤트가_두_번_오면_큐에_한_번만_들어간다(
        self, listener, admin_router, tmp_path
    ) -> None:
        queue = FakeJobQueue()
        ingress = make_ingress(listener=listener, queue=queue, admin_router=admin_router, tmp_path=tmp_path)

        ingress.handle_app_mention(mention_event())
        ingress.handle_app_mention(mention_event())

        assert len(queue.enqueued) == 1

    def test_EventListener가_None을_돌려준_이벤트는_큐에_안_들어간다(
        self, listener, admin_router, tmp_path
    ) -> None:
        queue = FakeJobQueue()
        ingress = make_ingress(listener=listener, queue=queue, admin_router=admin_router, tmp_path=tmp_path)

        # 스레드 답글이 아니고 멘션도 없는 채널 메시지는 from_message 가 None 을 돌려준다.
        ingress.handle_message({"channel": "C1", "user": "U1", "ts": "1.0", "text": "그냥 잡담"})

        assert queue.enqueued == []


class TestReactionMark:
    def test_큐에_새로_들어가면_대기_표식이_달린다(self, listener, admin_router, tmp_path) -> None:
        queue = FakeJobQueue()
        client = FakeWebClient()
        reactions = ReactionMarker(client)
        ingress = make_ingress(
            listener=listener, queue=queue, reactions=reactions,
            admin_router=admin_router, tmp_path=tmp_path,
        )

        ingress.handle_app_mention(mention_event())

        assert client.reaction_add_calls == [("C1", "1.0", "hourglass")]

    def test_enqueue가_False를_돌려주면_리액션을_안_단다(
        self, listener, admin_router, tmp_path
    ) -> None:
        """이 경로가 빠지면 같은 요청에 대기 표식이 두 번 달린다."""
        queue = FakeJobQueue(enqueue_result=False)
        client = FakeWebClient()
        reactions = ReactionMarker(client)
        ingress = make_ingress(
            listener=listener, queue=queue, reactions=reactions,
            admin_router=admin_router, tmp_path=tmp_path,
        )

        ingress.handle_app_mention(mention_event())

        assert queue.enqueued  # 등록 시도는 했다
        assert client.reaction_add_calls == []  # 표식은 안 달았다


class TestAdminCommand:
    def test_관리_명령은_큐에_안_들어가고_그_자리에서_답한다(
        self, listener, admin_router, tmp_path
    ) -> None:
        queue = FakeJobQueue()
        replies: list[tuple[str, str, str]] = []
        ingress = make_ingress(
            listener=listener, queue=queue, admin_router=admin_router,
            tmp_path=tmp_path, replies=replies,
        )

        ingress.handle_app_mention(mention_event(text="!ping"))

        assert queue.enqueued == []
        assert replies == [("C1", "1.0", "pong")]


class TestReactionEvent:
    def test_리액션_이벤트는_큐에_안_들어가고_콜백으로_넘어간다(
        self, listener, admin_router, tmp_path
    ) -> None:
        queue = FakeJobQueue()
        reaction_seen: list[tuple[str, str, str, str]] = []
        ingress = make_ingress(
            listener=listener, queue=queue, admin_router=admin_router,
            tmp_path=tmp_path, reactions_seen=reaction_seen,
        )
        event = {
            "reaction": "dango",
            "item": {"type": "message", "channel": "C1", "ts": "1.0"},
            "item_user": "U_BOT",
            "user": "U1",
        }

        ingress.handle_reaction(event)

        assert queue.enqueued == []
        assert reaction_seen == [("dango", "C1", "1.0", "U1")]

    def test_대상이_아닌_이모지는_콜백이_안_불린다(
        self, listener, admin_router, tmp_path
    ) -> None:
        queue = FakeJobQueue()
        reaction_seen: list[tuple[str, str, str, str]] = []
        ingress = make_ingress(
            listener=listener, queue=queue, admin_router=admin_router,
            tmp_path=tmp_path, reactions_seen=reaction_seen,
        )
        event = {
            "reaction": "thumbsup",
            "item": {"type": "message", "channel": "C1", "ts": "1.0"},
            "item_user": "U_BOT",
            "user": "U1",
        }

        ingress.handle_reaction(event)

        assert reaction_seen == []


class TestRegister:
    def test_register_뒤_dispatch로_실제_분배_경로가_동작한다(
        self, listener, admin_router, tmp_path
    ) -> None:
        queue = FakeJobQueue()
        ingress = make_ingress(listener=listener, queue=queue, admin_router=admin_router, tmp_path=tmp_path)
        gateway = SlackGateway(client=FakeWebClient())

        ingress.register(gateway)
        gateway.dispatch("app_mention", mention_event())

        assert len(queue.enqueued) == 1


class TestResilience:
    def test_큐_등록이_예외를_내도_이벤트_수신_루프가_죽지_않는다(
        self, listener, admin_router, tmp_path
    ) -> None:
        queue = FakeJobQueue(raise_on_enqueue=True)
        ingress = make_ingress(listener=listener, queue=queue, admin_router=admin_router, tmp_path=tmp_path)
        gateway = SlackGateway(client=FakeWebClient())
        ingress.register(gateway)

        # 예외가 밖으로 새면 이 호출 자체가 실패한다.
        gateway.dispatch("app_mention", mention_event(ts="1.0"))

        # 뒤이은 이벤트는 영향 없이 처리된다(다른 큐로 확인).
        queue2 = FakeJobQueue()
        ingress2 = make_ingress(listener=listener, queue=queue2, admin_router=admin_router, tmp_path=tmp_path)
        ingress2.handle_app_mention(mention_event(ts="2.0"))
        assert len(queue2.enqueued) == 1


class TestAttachments:
    def test_첨부가_있으면_저장하고_경로를_컨텍스트에_반영한다(
        self, listener, admin_router, tmp_path
    ) -> None:
        queue = FakeJobQueue()

        def downloader(url: str, token: str) -> DownloadResult:
            return DownloadResult(content_type="image/png", data=b"pngdata")

        attachments = AttachmentStore(
            attach_dir=tmp_path / "attach",
            token_provider=lambda: "xoxb-token",
            downloader=downloader,
        )
        ingress = make_ingress(
            listener=listener, queue=queue, attachments=attachments,
            admin_router=admin_router, tmp_path=tmp_path,
        )
        event = mention_event()
        event["files"] = [
            {"name": "photo.png", "url_private_download": "https://slack/x", "size": 10}
        ]

        ingress.handle_app_mention(event)

        assert len(queue.enqueued) == 1
        ctx = queue.enqueued[0]
        assert len(ctx.files) == 1
        assert ctx.files[0]["name"] == "photo.png"
        assert "local_path" in ctx.files[0]
        assert Path(ctx.files[0]["local_path"]).read_bytes() == b"pngdata"


class Test멘션이_붙은_관리_명령:
    def test_이름을_부르며_준_관리_명령도_동작한다(
        self, listener, admin_router, tmp_path
    ) -> None:
        """슬랙 멘션 이벤트의 본문은 `<@U_BOT> 도움말` 형태로 온다.

        멘션 표기를 지우지 않고 판정하면 어느 관리 명령도 맞지 않아 전부
        모델에게 넘어간다. 원본은 본문을 받자마자 멘션을 지운 뒤 판정했다.
        """
        queue = FakeJobQueue()
        replies: list[tuple[str, str, str]] = []
        ingress = make_ingress(
            listener=listener, queue=queue, admin_router=admin_router,
            tmp_path=tmp_path, replies=replies,
        )

        ingress.handle_app_mention(mention_event(text="<@U_BOT> !ping"))

        assert replies == [("C1", "1.0", "pong")]
        assert queue.enqueued == []

    def test_멘션을_지운_본문이_큐에_들어간다(self, listener, admin_router, tmp_path) -> None:
        """관리 명령이 아닌 요청도 같다. 엔진에 멘션 표기를 그대로 넘기지 않는다."""
        queue = FakeJobQueue()
        ingress = make_ingress(
            listener=listener, queue=queue, admin_router=admin_router, tmp_path=tmp_path
        )

        ingress.handle_app_mention(mention_event(text="<@U_BOT> 배포 상태 알려줘"))

        assert queue.enqueued[0].text == "배포 상태 알려줘"


class Test삼킨_예외를_기록한다:
    def test_큐_등록_실패가_로그에_남는다(
        self, listener, admin_router, tmp_path, caplog
    ) -> None:
        """예외를 그냥 삼키면 요청이 사라진 것과 아무 일도 없던 것이 같은 모습이 된다.

        수신 루프를 멈추지 않는 것과 실패를 안 남기는 것은 다르다.
        """
        queue = FakeJobQueue(raise_on_enqueue=True)
        ingress = make_ingress(
            listener=listener, queue=queue, admin_router=admin_router, tmp_path=tmp_path
        )

        with caplog.at_level("ERROR", logger="slack_cli_agent.core.ingress"):
            ingress.handle_app_mention(mention_event())

        assert any("큐 저장소 장애" in r.message or r.exc_info for r in caplog.records)


class Test접수가_재시도상한을_넘긴다:
    """실패한 건을 다시 등록할 때 상한을 함께 넘기는가.

    상한 없이 되살리면 계속 실패하는 요청이 슬랙 재전달마다 되살아나 끝나지
    않는다. 반대로 안 넘기면 실패한 건이 답 없이 남는다.
    """

    def test_등록에_상한이_함께_넘어간다(
        self, tmp_path: Path, listener: EventListener, admin_router: AdminRouter
    ) -> None:
        queue = FakeJobQueue()
        service = make_ingress(
            listener=listener, queue=queue, admin_router=admin_router,
            tmp_path=tmp_path, job_max_attempts=3,
        )
        service.handle_app_mention(mention_event())
        assert queue.enqueue_limits == [3]

    def test_상한을_안_주면_제한없음으로_넘어간다(
        self, tmp_path: Path, listener: EventListener, admin_router: AdminRouter
    ) -> None:
        queue = FakeJobQueue()
        service = make_ingress(
            listener=listener, queue=queue, admin_router=admin_router, tmp_path=tmp_path,
        )
        service.handle_app_mention(mention_event())
        assert queue.enqueue_limits == [0]
