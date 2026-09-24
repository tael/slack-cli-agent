"""IngressService — 슬랙 이벤트 수신부터 작업 큐 등록까지.

엔진을 부르지 않는다. 실제 처리는 워커(별도 프로세스)가 큐를 소비해 한다.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from identity_support import fake_identity

from slack_cli_agent.admin.admission import AdminAdmission
from slack_cli_agent.admin.command import AdminCommand, AdminContext, AdminResult
from slack_cli_agent.admin.router import AdminRouter
from slack_cli_agent.auth.policy import AccessPolicy
from slack_cli_agent.auth.principal import Principal, TrustLevel
from slack_cli_agent.config.channel import ChannelRegistry
from slack_cli_agent.config.profile import Profile
from slack_cli_agent.core.access import RequestAccess
from slack_cli_agent.core.context import RequestContext
from slack_cli_agent.core.ingress import IngressService
from slack_cli_agent.core.spawn import InlineTaskSpawner, TaskSpawner
from slack_cli_agent.jobs.ports import Job, JobQueue, ReclaimResult
from slack_cli_agent.observability.notices import NoticeCatalog, NoticeKey
from slack_cli_agent.reliability.dedup import DeduplicationTracker
from slack_cli_agent.slack.attachments import AttachmentStore, DownloadResult, SavedAttachment
from slack_cli_agent.slack.gate import ResponseGate
from slack_cli_agent.slack.gateway import SlackGateway
from slack_cli_agent.slack.listener import EventListener
from slack_cli_agent.slack.mentions import SelfMentionStripper
from slack_cli_agent.slack.reactions import ReactionMarker

# 대역들


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

    def __init__(
        self,
        *,
        enqueue_result: bool = True,
        raise_on_enqueue: bool = False,
        fail_times: int = 0,
        blocked: bool = False,
    ) -> None:
        self.enqueued: list[RequestContext] = []
        self._enqueue_result = enqueue_result
        self._raise_on_enqueue = raise_on_enqueue
        # 앞의 몇 번만 실패하는 저장소. 재전달이 살아나는지 보는 데 쓴다.
        self._fail_times = fail_times
        # 같은 스레드에 먼저 들어온 미완료 작업이 있는 상태를 흉내낸다.
        self._blocked = blocked
        # 등록 때 함께 넘어온 재시도 상한. 접수가 이 값을 안 넘기면
        # 실패로 끝난 건이 재전달돼도 되살아나지 않는다.
        self.enqueue_limits: list[int] = []

    def enqueue(self, ctx: RequestContext, max_attempts: int = 0) -> bool:
        if self._fail_times > 0:
            self._fail_times -= 1
            raise RuntimeError("큐 저장소 장애")
        if self._raise_on_enqueue:
            raise RuntimeError("큐 저장소 장애")
        self.enqueued.append(ctx)
        self.enqueue_limits.append(max_attempts)
        return self._enqueue_result

    def claim_next(self, worker_id: str) -> Job | None:
        raise NotImplementedError

    def heartbeat(self, job_id: int, lease: str) -> None:
        raise NotImplementedError

    def complete(self, job_id: int, ok: bool, failure: str = "", *, lease: str) -> bool:
        raise NotImplementedError

    def requeue(self, job_id: int, *, lease: str) -> bool:
        raise NotImplementedError

    def reclaim_stale(self, deadline: float, max_attempts: int) -> ReclaimResult:
        raise NotImplementedError

    def blocked_on_thread(self, thread_ts: str, message_ts: str) -> bool:
        return self._blocked

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
    return EventListener(FakeWebClient(), registry, gate, identity=fake_identity())


@pytest.fixture
def admin_router() -> AdminRouter:
    return AdminRouter([RecordingAdminCommand()])


#: 등록 채널이 없는 상태. 같은 뜻의 빈 사전을 시험마다 새로 쓰면 무엇을 뜻하는지
#: 안 보인다.
NO_CHANNELS: Mapping[str, Any] = {}


def make_channels(tmp_path: Path, registered: Mapping[str, Any] | None = None) -> ChannelRegistry:
    """채널 설정 파일을 실제로 만들어 준다. 자동 등록은 이 파일에 쓰므로
    읽기만 하는 대역으로는 그 경로를 못 본다."""
    path = tmp_path / "channels.json"
    entries = {"C1": {"name": "테스트"}} if registered is None else registered
    path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    return ChannelRegistry(path)


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
    enqueue_attempts: int = 1,
    spawn: TaskSpawner | None = None,
    assistant: Any = None,
    lock_budget: Any = None,
    channels: ChannelRegistry | None = None,
) -> IngressService:
    profile = make_profile(tmp_path)
    channels = channels if channels is not None else make_channels(tmp_path)
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
        access=RequestAccess(
            policy=AccessPolicy(profile, channels),
            channels=channels,
            channel_name=lambda channel: f"이름-{channel}",
            notices=NoticeCatalog(),
            reply=reply,
        ),
        strip_self_mention=SelfMentionStripper(fake_identity()).remove_self,
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
        admin=AdminAdmission(
            router=admin_router,
            context_builder=admin_context_builder,
            reply=reply,
        ),
        reply=reply,
        allowed_reactions=frozenset({"dango"}),
        on_reaction=on_reaction,
        spawn=spawn if spawn is not None else InlineTaskSpawner(),
        assistant=assistant,
        job_max_attempts=job_max_attempts,
        enqueue_attempts=enqueue_attempts,
        sleep=lambda _초: None,
        lock_budget=lock_budget,
    )


def mention_event(
    ts: str = "1.0",
    text: str = "<@U_BOT> 안녕",
    files: list[dict] | None = None,
    user: str = "U1",
    channel: str = "C1",
) -> dict:
    event: dict[str, Any] = {"channel": channel, "user": user, "ts": ts, "text": text}
    if files is not None:
        event["files"] = files
    return event


def dm_event(ts: str = "1.0", text: str = "안녕", user: str = "U1", channel: str = "D1") -> dict:
    return {
        "channel": channel, "channel_type": "im", "user": user, "ts": ts, "text": text,
    }


# 시험


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
    def test_큐에_새로_들어가면_접수_표식이_달린다(self, listener, admin_router, tmp_path) -> None:
        queue = FakeJobQueue()
        client = FakeWebClient()
        reactions = ReactionMarker(client)
        ingress = make_ingress(
            listener=listener, queue=queue, reactions=reactions,
            admin_router=admin_router, tmp_path=tmp_path,
        )

        ingress.handle_app_mention(mention_event())

        assert client.reaction_add_calls == [("C1", "1.0", "eyes")]

    def test_앞선_요청이_없으면_대기_표식을_안_단다(self, listener, admin_router, tmp_path) -> None:
        """모래시계가 무조건 달리면 눈과 항상 같이 떠서 대기를 뜻하지 못한다."""
        queue = FakeJobQueue(blocked=False)
        client = FakeWebClient()
        reactions = ReactionMarker(client)
        ingress = make_ingress(
            listener=listener, queue=queue, reactions=reactions,
            admin_router=admin_router, tmp_path=tmp_path,
        )

        ingress.handle_app_mention(mention_event())

        assert ("C1", "1.0", "hourglass") not in client.reaction_add_calls

    def test_같은_스레드에_앞선_요청이_있으면_대기_표식이_달린다(
        self, listener, admin_router, tmp_path
    ) -> None:
        queue = FakeJobQueue(blocked=True)
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


class Test접근_판정:
    """원본 bot.py:3695 `is_allowed` 를 접수 경로에 되살린 것이다.

    소유자는 어디서든 통과하고, 그 밖의 DM 은 거절하고, 채널은 등록된 것만
    통과한다. 거절은 조용하다 - 안내를 올리면 모르는 사람에게 봇이 있다는 것을
    알리게 되고 원본과도 달라진다.
    """

    def test_소유자는_미등록_채널에서도_통과한다(self, listener, admin_router, tmp_path) -> None:
        queue = FakeJobQueue()
        ingress = make_ingress(
            listener=listener, queue=queue, admin_router=admin_router, tmp_path=tmp_path,
            channels=make_channels(tmp_path, NO_CHANNELS),
        )

        ingress.handle_app_mention(mention_event(channel="C9", user="U_OWNER"))

        assert len(queue.enqueued) == 1

    def test_미등록_채널의_제삼자는_무시된다(self, listener, admin_router, tmp_path) -> None:
        queue = FakeJobQueue()
        replies: list[tuple[str, str, str]] = []
        client = FakeWebClient()
        ingress = make_ingress(
            listener=listener, queue=queue, admin_router=admin_router, tmp_path=tmp_path,
            replies=replies, reactions=ReactionMarker(client),
            channels=make_channels(tmp_path, NO_CHANNELS),
        )

        ingress.handle_app_mention(mention_event(channel="C9", user="U1"))

        assert queue.enqueued == []
        assert replies == [], "거절을 알리면 모르는 사람에게 봇을 알리게 된다"
        assert client.reaction_add_calls == []

    def test_등록된_채널의_제삼자는_통과한다(self, listener, admin_router, tmp_path) -> None:
        queue = FakeJobQueue()
        ingress = make_ingress(
            listener=listener, queue=queue, admin_router=admin_router, tmp_path=tmp_path,
            channels=make_channels(tmp_path, {"C1": {"name": "테스트"}}),
        )

        ingress.handle_app_mention(mention_event(channel="C1", user="U1"))

        assert len(queue.enqueued) == 1

    def test_소유자가_아닌_DM은_무시된다(self, listener, admin_router, tmp_path) -> None:
        queue = FakeJobQueue()
        replies: list[tuple[str, str, str]] = []
        ingress = make_ingress(
            listener=listener, queue=queue, admin_router=admin_router, tmp_path=tmp_path,
            replies=replies,
        )

        ingress.handle_message(dm_event(user="U1"))

        assert queue.enqueued == []
        assert replies == []

    def test_등록된_DM이라도_소유자가_아니면_무시된다(
        self, listener, admin_router, tmp_path
    ) -> None:
        """DM 은 등록 여부를 보기 전에 거른다. 관리 명령은 DM 에서도 채널 설정을
        쓰므로 채널 파일에 D 로 시작하는 항목이 남을 수 있다."""
        queue = FakeJobQueue()
        ingress = make_ingress(
            listener=listener, queue=queue, admin_router=admin_router, tmp_path=tmp_path,
            channels=make_channels(tmp_path, {"D1": {"name": "쪽지"}}),
        )

        ingress.handle_message(dm_event(user="U1", channel="D1"))

        assert queue.enqueued == []

    def test_소유자_DM은_통과한다(self, listener, admin_router, tmp_path) -> None:
        queue = FakeJobQueue()
        ingress = make_ingress(
            listener=listener, queue=queue, admin_router=admin_router, tmp_path=tmp_path,
        )

        ingress.handle_message(dm_event(user="U_OWNER"))

        assert len(queue.enqueued) == 1

    def test_미등록_채널의_제삼자는_관리_명령도_못_쓴다(
        self, listener, admin_router, tmp_path
    ) -> None:
        """판정이 관리 명령 처리보다 앞이다. 뒤에 두면 명령만 통과한다."""
        replies: list[tuple[str, str, str]] = []
        ingress = make_ingress(
            listener=listener, queue=FakeJobQueue(), admin_router=admin_router,
            tmp_path=tmp_path, replies=replies,
            channels=make_channels(tmp_path, NO_CHANNELS),
        )

        ingress.handle_app_mention(mention_event(channel="C9", user="U1", text="!ping"))

        assert replies == []


class Test소유자_호출로_채널이_등록된다:
    """원본 bot.py:4996. 접근 판정만 넣고 이것을 빼면 미등록 채널이 통째로
    조용해진다."""

    def test_소유자가_부르면_등록되고_그때만_안내가_나간다(
        self, listener, admin_router, tmp_path
    ) -> None:
        channels = make_channels(tmp_path, NO_CHANNELS)
        replies: list[tuple[str, str, str]] = []
        ingress = make_ingress(
            listener=listener, queue=FakeJobQueue(), admin_router=admin_router,
            tmp_path=tmp_path, replies=replies, channels=channels,
        )

        ingress.handle_app_mention(mention_event(channel="C9", user="U_OWNER"))

        등록 = channels.get("C9")
        assert 등록 is not None
        assert 등록.name == "이름-C9"
        assert replies == [("C9", "1.0", NoticeCatalog().render(NoticeKey.JOINED))]

    def test_이미_등록된_채널에서는_안내가_안_나간다(
        self, listener, admin_router, tmp_path
    ) -> None:
        channels = make_channels(tmp_path, {"C1": {"name": "테스트"}})
        replies: list[tuple[str, str, str]] = []
        ingress = make_ingress(
            listener=listener, queue=FakeJobQueue(), admin_router=admin_router,
            tmp_path=tmp_path, replies=replies, channels=channels,
        )

        ingress.handle_app_mention(mention_event(channel="C1", user="U_OWNER"))

        assert replies == []
        남은것 = channels.get("C1")
        assert 남은것 is not None and 남은것.name == "테스트", "기존 설정을 덮어쓰면 안 된다"

    def test_소유자의_DM은_등록하지_않는다(self, listener, admin_router, tmp_path) -> None:
        channels = make_channels(tmp_path, NO_CHANNELS)
        replies: list[tuple[str, str, str]] = []
        ingress = make_ingress(
            listener=listener, queue=FakeJobQueue(), admin_router=admin_router,
            tmp_path=tmp_path, replies=replies, channels=channels,
        )

        ingress.handle_message(dm_event(user="U_OWNER"))

        assert channels.channel_ids() == []
        assert replies == []

    def test_관리_명령은_채널을_등록하지_않는다(self, listener, admin_router, tmp_path) -> None:
        """원본은 관리 명령 처리가 등록보다 앞이라 명령만으로는 안 등록된다."""
        channels = make_channels(tmp_path, NO_CHANNELS)
        ingress = make_ingress(
            listener=listener, queue=FakeJobQueue(), admin_router=admin_router,
            tmp_path=tmp_path, channels=channels,
        )

        ingress.handle_app_mention(mention_event(channel="C9", user="U_OWNER", text="!ping"))

        assert channels.channel_ids() == []


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

    def test_점검은_이벤트_처리기_밖에서_돈다(
        self, listener, admin_router, tmp_path
    ) -> None:
        """점검은 엔진 호출이라 몇 분이 걸린다. 그 자리에서 돌리면 그동안
        소켓 이벤트를 하나도 못 받는다."""
        spawned: list[tuple[str, Callable[[], None]]] = []

        class RecordingSpawner(TaskSpawner):
            def spawn(self, name, work):
                spawned.append((name, work))

        reaction_seen: list[tuple[str, str, str, str]] = []
        ingress = make_ingress(
            listener=listener, queue=FakeJobQueue(), admin_router=admin_router,
            tmp_path=tmp_path, reactions_seen=reaction_seen, spawn=RecordingSpawner(),
        )
        event = {
            "reaction": "dango",
            "item": {"type": "message", "channel": "C1", "ts": "1.0"},
            "item_user": "U_BOT",
            "user": "U1",
        }

        ingress.handle_reaction(event)

        assert reaction_seen == []
        assert [name for name, _ in spawned] == ["점검:dango"]
        spawned[0][1]()
        assert reaction_seen == [("dango", "C1", "1.0", "U1")]

    def test_미등록_채널의_리액션도_그대로_넘어간다(
        self, listener, admin_router, tmp_path
    ) -> None:
        """원본 bot.py:6143 의 리액션 처리에는 `is_allowed` 가 없다. 이 봇이
        자기 답변을 올린 자리에만 붙는 것이라 이미 응답한 대화다."""
        reaction_seen: list[tuple[str, str, str, str]] = []
        ingress = make_ingress(
            listener=listener, queue=FakeJobQueue(), admin_router=admin_router,
            tmp_path=tmp_path, reactions_seen=reaction_seen,
            channels=make_channels(tmp_path, NO_CHANNELS),
        )
        event = {
            "reaction": "dango",
            "item": {"type": "message", "channel": "C9", "ts": "1.0"},
            "item_user": "U_BOT",
            "user": "U1",
        }

        ingress.handle_reaction(event)

        assert reaction_seen == [("dango", "C9", "1.0", "U1")]

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


class Test에이전트패널:
    """상단바 에이전트 패널에서 스레드를 열면 오는 이벤트다(sca-kos.7).

    구독만 하고 분배에 안 걸면 패널이 빈 채로 열린다.
    """

    def test_register_뒤_에이전트_스레드_시작이_패널로_간다(
        self, listener, admin_router, tmp_path
    ) -> None:
        본것: list[dict] = []

        class Fake패널:
            def thread_started(self, event):
                본것.append(dict(event))

        ingress = make_ingress(
            listener=listener, queue=FakeJobQueue(), admin_router=admin_router,
            tmp_path=tmp_path, assistant=Fake패널(),
        )
        gateway = SlackGateway(client=FakeWebClient())
        ingress.register(gateway)

        열림 = {"assistant_thread": {"channel_id": "D1", "user_id": "U_OWNER"}}
        gateway.dispatch("assistant_thread_started", 열림)

        assert 본것 == [열림]

    def test_소유자가_아닌_사람의_패널에는_인사하지_않는다(
        self, listener, admin_router, tmp_path
    ) -> None:
        """패널은 DM 이다. 인사를 올리면 뒤이어 보내는 말은 전부 무시되는데
        봇이 있다는 것만 알리게 된다."""
        본것: list[dict] = []

        class Fake패널:
            def thread_started(self, event):
                본것.append(dict(event))

        ingress = make_ingress(
            listener=listener, queue=FakeJobQueue(), admin_router=admin_router,
            tmp_path=tmp_path, assistant=Fake패널(),
        )

        ingress.handle_assistant_thread_started(
            {"assistant_thread": {"channel_id": "D1", "user_id": "U1"}}
        )

        assert 본것 == []

    def test_패널이_없으면_아무_일도_하지_않는다(self, listener, admin_router, tmp_path) -> None:
        ingress = make_ingress(
            listener=listener, queue=FakeJobQueue(), admin_router=admin_router, tmp_path=tmp_path,
        )
        gateway = SlackGateway(client=FakeWebClient())
        ingress.register(gateway)
        gateway.dispatch(
            "assistant_thread_started",
            {"assistant_thread": {"channel_id": "D1", "user_id": "U_OWNER"}},
        )


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


class Test적재가_실패한_요청:
    """슬랙 Socket Mode 는 이벤트를 먼저 ACK 한다. 적재가 실패하면 그 요청을
    되살릴 곳은 슬랙의 재전달뿐인데, 중복 표식이 먼저 남으면 그것도 막힌다."""

    def test_적재가_실패하면_같은_이벤트를_다시_받아_처리한다(
        self, listener, admin_router, tmp_path
    ) -> None:
        queue = FakeJobQueue(fail_times=1)
        ingress = make_ingress(
            listener=listener, queue=queue, admin_router=admin_router, tmp_path=tmp_path
        )

        ingress.handle_app_mention(mention_event(ts="1.0"))
        assert queue.enqueued == []

        ingress.handle_app_mention(mention_event(ts="1.0"))
        assert len(queue.enqueued) == 1

    def test_적재가_한_번_실패해도_그_자리에서_다시_시도한다(
        self, listener, admin_router, tmp_path
    ) -> None:
        """슬랙은 ACK 한 이벤트를 다시 보내지 않는다. 되살릴 기회는 이 자리뿐이다."""
        queue = FakeJobQueue(fail_times=1)
        ingress = make_ingress(
            listener=listener, queue=queue, admin_router=admin_router, tmp_path=tmp_path,
            enqueue_attempts=3,
        )

        ingress.handle_app_mention(mention_event(ts="1.0"))

        assert len(queue.enqueued) == 1

    def test_끝까지_실패하면_스레드에_알린다(
        self, listener, admin_router, tmp_path
    ) -> None:
        """조용히 사라지면 물은 사람은 답을 기다리기만 한다."""
        replies: list[tuple[str, str, str]] = []
        queue = FakeJobQueue(raise_on_enqueue=True)
        ingress = make_ingress(
            listener=listener, queue=queue, admin_router=admin_router, tmp_path=tmp_path,
            replies=replies, enqueue_attempts=2,
        )

        ingress.handle_app_mention(mention_event(ts="1.0"))

        assert replies, "접수 실패를 알리지 않았다"
        assert "접수" in replies[-1][2]

    def test_관리_명령은_되돌리지_않는다(
        self, listener, admin_router, tmp_path
    ) -> None:
        """관리 명령은 그 자리에서 효과를 낸다. 되돌려 다시 받으면 두 번 돈다."""
        replies: list[tuple[str, str, str]] = []
        queue = FakeJobQueue()
        ingress = make_ingress(
            listener=listener, queue=queue, admin_router=admin_router, tmp_path=tmp_path,
            replies=replies,
        )

        ingress.handle_app_mention(mention_event(ts="1.0", text="!ping"))
        ingress.handle_app_mention(mention_event(ts="1.0", text="!ping"))

        assert [r[2] for r in replies] == ["pong"]
        assert queue.enqueued == []

    def test_첨부_저장이_실패해도_접수_실패로_다룬다(
        self, listener, admin_router, tmp_path
    ) -> None:
        """첨부 저장이 적재 앞에 있어, 거기서 터지면 표식도 안내도 없이 사라졌다."""
        replies: list[tuple[str, str, str]] = []

        class 터지는첨부(AttachmentStore):
            def __init__(self) -> None:
                super().__init__(
                    attach_dir=tmp_path / "attach",
                    token_provider=lambda: "",
                    downloader=lambda url, token: DownloadResult("image/png", b""),
                )

            def save(self, event: Mapping[str, Any]) -> list[SavedAttachment]:
                raise OSError("디스크 없음")

        queue = FakeJobQueue()
        ingress = make_ingress(
            listener=listener, queue=queue, admin_router=admin_router, tmp_path=tmp_path,
            attachments=터지는첨부(), replies=replies,
        )

        ingress.handle_app_mention(mention_event(ts="1.0", files=[{"url_private": "u", "name": "a.png"}]))

        assert replies, "첨부 저장 실패를 알리지 않았다"

    def test_재접수에_성공하면_실패_표식을_지운다(
        self, listener, admin_router, tmp_path
    ) -> None:
        client = FakeWebClient()
        queue = FakeJobQueue(fail_times=1)
        ingress = make_ingress(
            listener=listener, queue=queue, admin_router=admin_router, tmp_path=tmp_path,
            reactions=ReactionMarker(client), enqueue_attempts=1,
        )

        ingress.handle_app_mention(mention_event(ts="1.0"))
        assert ("C1", "1.0", "x") in client.reaction_add_calls

        client.reaction_remove_calls.clear()
        ingress.handle_app_mention(mention_event(ts="1.0"))

        assert ("C1", "1.0", "x") in client.reaction_remove_calls

    def test_실패한_적_없으면_표식을_지우러_가지_않는다(
        self, listener, admin_router, tmp_path
    ) -> None:
        """정상 요청마다 슬랙 호출을 하나 더 쓰지 않는다."""
        client = FakeWebClient()
        ingress = make_ingress(
            listener=listener, queue=FakeJobQueue(), admin_router=admin_router,
            tmp_path=tmp_path, reactions=ReactionMarker(client),
        )

        ingress.handle_app_mention(mention_event(ts="1.0"))

        assert all(name != "x" for _c, _t, name in client.reaction_remove_calls)

    def test_적재에_성공하면_재전달을_계속_막는다(
        self, listener, admin_router, tmp_path
    ) -> None:
        queue = FakeJobQueue()
        ingress = make_ingress(
            listener=listener, queue=queue, admin_router=admin_router, tmp_path=tmp_path
        )

        ingress.handle_app_mention(mention_event(ts="1.0"))
        ingress.handle_app_mention(mention_event(ts="1.0"))

        assert len(queue.enqueued) == 1

    def test_이미_큐에_있어_False가_와도_재전달을_막는다(
        self, listener, admin_router, tmp_path
    ) -> None:
        """False 는 장애가 아니라 이미 들어가 있다는 뜻이다. 되살릴 것이 없다."""
        queue = FakeJobQueue(enqueue_result=False)
        ingress = make_ingress(
            listener=listener, queue=queue, admin_router=admin_router, tmp_path=tmp_path
        )

        ingress.handle_app_mention(mention_event(ts="1.0"))
        ingress.handle_app_mention(mention_event(ts="1.0"))

        assert len(queue.enqueued) == 1


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

    def test_남을_부른_멘션은_큐에_남는다(self, listener, admin_router, tmp_path) -> None:
        """전부 지우면 누구를 불렀는지가 모델에게 안 간다. 이름으로 바꾸는
        것은 프롬프트를 만드는 자리가 한다 (sca-za2a)."""
        queue = FakeJobQueue()
        ingress = make_ingress(
            listener=listener, queue=queue, admin_router=admin_router, tmp_path=tmp_path
        )

        ingress.handle_app_mention(mention_event(text="<@U_BOT> <@U9> 에게 물어봐"))

        assert queue.enqueued[0].text == "<@U9> 에게 물어봐"


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


class Test봇이_넣은_멘션도_받는다:
    """app_mention 은 bot_id 를 안 거른다. 이것은 이식 결함이 아니라 계약이다.

    원본 bot.py:5410 on_mention 도 봇 필터 없이 처리한다. 그리고 이 성질이
    tools/test-channel-probe.py 가 사람 없이 멘션-응답 경로 전체를 재는
    근거다 - 다른 봇의 토큰으로 멘션을 넣어 대상 봇을 깨운다. 필터를 넣으면
    원본 동등성과 그 도구가 함께 죽으므로 여기서 고정한다(sca-3ee).

    루프 위험은 다른 자리에서 막는다 - 발신 본문의 다른 봇 멘션을 지우는
    것이다(sca-c4m). 수신을 막는 것이 아니다.
    """

    def test_bot_id_가_붙은_멘션도_큐에_들어간다(
        self, listener: EventListener, admin_router: AdminRouter, tmp_path: Path
    ) -> None:
        queue = FakeJobQueue()
        ingress = make_ingress(
            listener=listener, queue=queue, admin_router=admin_router, tmp_path=tmp_path
        )

        event = mention_event() | {"bot_id": "B_OTHER"}
        ingress.handle_app_mention(event)

        assert len(queue.enqueued) == 1

    def test_사람이_넣은_멘션과_같게_다룬다(
        self, listener: EventListener, admin_router: AdminRouter, tmp_path: Path
    ) -> None:
        """bot_id 가 큐 항목의 내용을 바꾸지 않는다. 받되 다르게 다루면
        도구로 잰 것이 실제 동작과 달라진다."""
        사람큐, 봇큐 = FakeJobQueue(), FakeJobQueue()
        make_ingress(
            listener=listener, queue=사람큐, admin_router=admin_router, tmp_path=tmp_path
        ).handle_app_mention(mention_event())
        make_ingress(
            listener=listener, queue=봇큐, admin_router=admin_router, tmp_path=tmp_path
        ).handle_app_mention(mention_event() | {"bot_id": "B_OTHER"})

        사람, 봇 = 사람큐.enqueued[0], 봇큐.enqueued[0]
        assert (봇.channel, 봇.ts, 봇.text) == (사람.channel, 사람.ts, 사람.text)


class 기록하는예산:
    """적재가 잠금 예산 안에서 도는지를 본다."""

    def __init__(self) -> None:
        self.깊이 = 0
        self.들어간_횟수 = 0

    @contextmanager
    def __call__(self) -> Iterator[None]:
        self.깊이 += 1
        self.들어간_횟수 += 1
        try:
            yield
        finally:
            self.깊이 -= 1


class Test접수경로의_잠금예산:
    """소켓 처리 스레드가 잠금 대기로 묶이면 뒤 이벤트가 밀린다 (sca-9l1)."""

    def test_적재를_예산_안에서_한다(self, tmp_path: Path, listener: EventListener) -> None:
        예산 = 기록하는예산()

        class 예산확인큐(FakeJobQueue):
            def enqueue(self, ctx: RequestContext, max_attempts: int = 0) -> bool:
                assert 예산.깊이 == 1, "적재가 예산 밖에서 돌았다"
                return super().enqueue(ctx, max_attempts=max_attempts)

        queue = 예산확인큐()
        ingress = make_ingress(
            listener=listener, queue=queue, admin_router=AdminRouter([]),
            tmp_path=tmp_path, lock_budget=예산,
        )
        ingress.handle_app_mention(mention_event())
        assert len(queue.enqueued) == 1
        assert 예산.들어간_횟수 >= 1

    def test_대기_여부_조회도_예산_안에서_한다(self, tmp_path: Path, listener: EventListener) -> None:
        """같은 연결·같은 잠금을 쓰므로 여기서 30초를 쓰면 의미가 없다."""
        예산 = 기록하는예산()
        본_깊이: list[int] = []

        class 예산확인큐(FakeJobQueue):
            def blocked_on_thread(self, thread_ts: str, message_ts: str) -> bool:
                본_깊이.append(예산.깊이)
                return False

        queue = 예산확인큐()
        ingress = make_ingress(
            listener=listener, queue=queue, admin_router=AdminRouter([]),
            tmp_path=tmp_path, lock_budget=예산,
        )
        ingress.handle_app_mention(mention_event())
        assert 본_깊이 == [1]

    def test_관리_명령_판정도_예산_안에서_한다(self, tmp_path: Path, listener: EventListener) -> None:
        """판정이 점유 원장에 쓴다. 예산 밖이면 소켓 처리 스레드가 그 잠금에
        30초까지 묶인다 (sca-8m5p)."""
        예산 = 기록하는예산()
        본_깊이: list[int] = []

        class 깊이보는판정:
            def handled(self, ctx: RequestContext) -> bool:
                본_깊이.append(예산.깊이)
                return False

        ingress = make_ingress(
            listener=listener, queue=FakeJobQueue(), admin_router=AdminRouter([]),
            tmp_path=tmp_path, lock_budget=예산,
        )
        ingress._admin = 깊이보는판정()  # type: ignore[assignment]
        ingress.handle_app_mention(mention_event())
        assert 본_깊이 == [1]

    def test_예산을_안_주면_그냥_돈다(self, tmp_path: Path, listener: EventListener) -> None:
        queue = FakeJobQueue()
        ingress = make_ingress(
            listener=listener, queue=queue, admin_router=AdminRouter([]),
            tmp_path=tmp_path,
        )
        ingress.handle_app_mention(mention_event())
        assert len(queue.enqueued) == 1

    def test_적재가_오래_걸리면_경고를_남긴다(self, tmp_path: Path, listener: EventListener, caplog) -> None:
        """오류 0건은 '30초가 만료된 적 없다' 까지만 말한다. 잠금 대기 분포를
        보려면 성공한 적재의 소요도 남아야 한다."""
        시각 = [100.0]

        class 느린큐(FakeJobQueue):
            def enqueue(self, ctx: RequestContext, max_attempts: int = 0) -> bool:
                시각[0] += 2.0
                return super().enqueue(ctx, max_attempts=max_attempts)

        queue = 느린큐()
        ingress = make_ingress(
            listener=listener, queue=queue, admin_router=AdminRouter([]),
            tmp_path=tmp_path,
        )
        ingress._clock = lambda: 시각[0]  # type: ignore[assignment]
        with caplog.at_level(logging.WARNING):
            ingress.handle_app_mention(mention_event())
        assert any("적재가 오래" in r.message for r in caplog.records), caplog.text

    def test_빠른_적재는_경고를_안_남긴다(self, tmp_path: Path, listener: EventListener, caplog) -> None:
        queue = FakeJobQueue()
        ingress = make_ingress(
            listener=listener, queue=queue, admin_router=AdminRouter([]),
            tmp_path=tmp_path,
        )
        with caplog.at_level(logging.WARNING):
            ingress.handle_app_mention(mention_event())
        assert not any("적재가 오래" in r.message for r in caplog.records)


class Test적재_뒤의_표식_실패:
    """적재는 됐는데 표식 조회가 실패하면, 작업은 큐에 있는데 로그에는
    접수 실패로 남았다 (sca-9l1 리뷰)."""

    def test_표식이_실패해도_접수_실패로_적지_않는다(
        self, tmp_path: Path, listener: EventListener, caplog
    ) -> None:
        class 조회가_막힌큐(FakeJobQueue):
            def blocked_on_thread(self, thread_ts: str, message_ts: str) -> bool:
                raise RuntimeError("잠겨 있다")

        queue = 조회가_막힌큐()
        ingress = make_ingress(
            listener=listener, queue=queue, admin_router=AdminRouter([]),
            tmp_path=tmp_path,
        )
        with caplog.at_level(logging.WARNING):
            ingress.handle_app_mention(mention_event())
        assert len(queue.enqueued) == 1
        assert not any("요청 접수 실패" in r.message for r in caplog.records), caplog.text
        assert any("표식" in r.message for r in caplog.records), caplog.text

    def test_표식이_실패해도_실패_안내를_보내지_않는다(
        self, tmp_path: Path, listener: EventListener
    ) -> None:
        class 조회가_막힌큐(FakeJobQueue):
            def blocked_on_thread(self, thread_ts: str, message_ts: str) -> bool:
                raise RuntimeError("잠겨 있다")

        replies: list[tuple[str, str, str]] = []
        ingress = make_ingress(
            listener=listener, queue=조회가_막힌큐(), admin_router=AdminRouter([]),
            tmp_path=tmp_path, replies=replies,
        )
        ingress.handle_app_mention(mention_event())
        assert replies == []


class Test관리_명령_점유를_못_했을_때:
    """점유 원장에 못 쓰면 이 프로세스는 명령을 안 돌린다. 그런데 중복 방지
    기록은 이미 남아 슬랙 재전달도 막히고, 캐치업은 창 안에서만 회수한다.
    그대로 두면 그 명령이 영영 사라진다 (코덱스 리뷰, sca-8m5p).
    """

    class 점유불가판정:
        def handled(self, ctx: RequestContext) -> bool:
            from slack_cli_agent.admin.admission import ClaimUnavailable

            raise ClaimUnavailable("DB 오류")

    def test_중복_방지_기록을_지운다(self, tmp_path: Path, listener: EventListener) -> None:
        dedup = DeduplicationTracker()
        ingress = make_ingress(
            listener=listener, queue=FakeJobQueue(), admin_router=AdminRouter([]),
            tmp_path=tmp_path, dedup=dedup,
        )
        ingress._admin = self.점유불가판정()  # type: ignore[assignment]
        event = mention_event()
        ingress.handle_app_mention(event)
        assert dedup.already_seen_event(event["channel"], event["ts"]) is False

    def test_다시_불러_달라고_알린다(self, tmp_path: Path, listener: EventListener) -> None:
        보냄: list[tuple[str, str, str]] = []
        ingress = make_ingress(
            listener=listener, queue=FakeJobQueue(), admin_router=AdminRouter([]),
            tmp_path=tmp_path, replies=보냄,
        )
        ingress._admin = self.점유불가판정()  # type: ignore[assignment]
        ingress.handle_app_mention(mention_event())
        assert 보냄 and "다시" in 보냄[0][2]

    def test_큐에_넣지_않는다(self, tmp_path: Path, listener: EventListener) -> None:
        """모델로 보내면 권한 판정을 안 거친 명령이 모델 요청으로 돈다."""
        queue = FakeJobQueue()
        ingress = make_ingress(
            listener=listener, queue=queue, admin_router=AdminRouter([]), tmp_path=tmp_path,
        )
        ingress._admin = self.점유불가판정()  # type: ignore[assignment]
        ingress.handle_app_mention(mention_event())
        assert queue.enqueued == []
