"""B5 — 봇간 상호작용·수신자 가드.

마메치 bot.py 상당 기능의 이식 동등성을 고정한다:
- C16: 다른 봇만 지목해 부른 말인지 판별 (calls_other_bot)
- C17: 직전까지 다른 봇과 대화 중이었는지(이어말) 판별 (talking_to_peer)
- C18+C19: 봇간 왕복 한도+사람 리셋 (PeerExchangeLimiter)
- C25: 캐시만으로 봇 여부 조회 (BotUserResolver.known)
- C26: 다른 봇이 나를 지목해 불렀는지 판별 (from_app_mention)
- E6: 스레드 참여·대기·타봇대화·봇개설 4튜플 (EventListener._thread_state)
- E7: 멘션없는 말에 첨부파일 있으면 무조건 응답 (ResponseGate.worth_answering)
- B1/B2: 화자가 봇일 때의 AskerSection 3분기
- D3: [[RECIPIENT:...]] 해석+첫머리 멘션 강제 (RecipientResolver)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from identity_support import fake_identity

from slack_cli_agent.auth.principal import Principal, TrustLevel
from slack_cli_agent.config.channel import ChannelRegistry
from slack_cli_agent.guard.mentions import (
    PeerExchangeLimiter,
    Recipient,
    RecipientResolver,
    calls_other_bot,
    talking_to_peer,
)
from slack_cli_agent.prompt.library import PromptLibrary
from slack_cli_agent.prompt.sections import AskerSection, CompositionContext
from slack_cli_agent.slack.gate import ResponseGate
from slack_cli_agent.slack.listener import EventListener
from slack_cli_agent.slack.names import BotUserResolver


def _is_bot(ids: set[str]):
    return lambda uid: uid in ids


# C16 ---------------------------------------------------------------------


class Test다른_봇만_지목:
    def test_다른_봇_둘만_불렀으면_침묵한다(self) -> None:
        assert calls_other_bot(
            _is_bot({"U0OTHER1", "U0OTHER2"}), "U_BOT", "<@U0OTHER1> <@U0OTHER2> 검토해줘"
        ) is True

    def test_자기도_함께_불렸으면_침묵하지_않는다(self) -> None:
        assert calls_other_bot(
            _is_bot({"U0OTHER1"}), "U0BOT", "<@U0BOT> <@U0OTHER1> 같이 봐줘"
        ) is False

    def test_사람만_불렀으면_침묵하지_않는다(self) -> None:
        assert calls_other_bot(_is_bot(set()), "U_BOT", "<@U1> 확인해줘") is False

    def test_아무도_안_불렀으면_침묵하지_않는다(self) -> None:
        assert calls_other_bot(_is_bot({"U0OTHER1"}), "U_BOT", "그냥 잡담") is False


# C17 ---------------------------------------------------------------------


class Test이어말_판별:
    def test_직전_사람_말이_다른_봇을_불렀으면_이어말이다(self) -> None:
        messages = [
            {"ts": "1.0", "user": "U1", "text": "<@U0OTHER> 검토해줘"},
        ]
        assert talking_to_peer(
            messages, is_bot=_is_bot({"U0OTHER"}), self_id="U_BOT"
        ) is True

    def test_직전_사람_말이_자기를_불렀으면_이어말이_아니다(self) -> None:
        messages = [
            {"ts": "1.0", "user": "U1", "text": "<@U0BOT> 검토해줘"},
        ]
        assert talking_to_peer(
            messages, is_bot=_is_bot({"U0OTHER"}), self_id="U0BOT"
        ) is False

    def test_봇_메시지는_건너뛰고_그_전_사람_말을_본다(self) -> None:
        messages = [
            {"ts": "1.0", "user": "U1", "text": "<@U0OTHER> 검토해줘"},
            {"ts": "1.1", "bot_id": "B_OTHER", "text": "확인할게요"},
        ]
        assert talking_to_peer(
            messages, is_bot=_is_bot({"U0OTHER"}), self_id="U_BOT"
        ) is True

    def test_아무도_안_불렀으면_이어말이_아니다(self) -> None:
        messages = [{"ts": "1.0", "user": "U1", "text": "그냥 잡담"}]
        assert talking_to_peer(
            messages, is_bot=_is_bot({"U0OTHER"}), self_id="U_BOT"
        ) is False

    def test_upto_ts_이후_메시지는_보지_않는다(self) -> None:
        messages = [
            {"ts": "1.0", "user": "U1", "text": "<@U0OTHER> 검토해줘"},
            {"ts": "2.0", "user": "U1", "text": "그 뒤 잡담"},
        ]
        # upto_ts=1.0 이면 1.0 자체도 보지 않는다(그 메시지가 지금 판정 대상).
        assert talking_to_peer(
            messages, is_bot=_is_bot({"U0OTHER"}), self_id="U_BOT", upto_ts="1.0"
        ) is False


# C18 + C19 -----------------------------------------------------------------


class TestPeerExchangeLimiter:
    def test_한도_안에서는_허용한다(self) -> None:
        limiter = PeerExchangeLimiter(limit=3)
        assert limiter.allowed("T1") is True
        assert limiter.allowed("T1") is True
        assert limiter.allowed("T1") is True

    def test_한도를_넘으면_거부한다(self) -> None:
        limiter = PeerExchangeLimiter(limit=2)
        assert limiter.allowed("T1") is True
        assert limiter.allowed("T1") is True
        assert limiter.allowed("T1") is False

    def test_다른_스레드는_서로_간섭하지_않는다(self) -> None:
        limiter = PeerExchangeLimiter(limit=1)
        assert limiter.allowed("T1") is True
        assert limiter.allowed("T2") is True

    def test_사람이_말하면_리셋된다(self) -> None:
        """C18 — 리셋이 없으면 한도 도달 뒤 영구 침묵한다."""
        limiter = PeerExchangeLimiter(limit=1)
        assert limiter.allowed("T1") is True
        assert limiter.allowed("T1") is False  # 한도 도달

        limiter.reset("T1")  # 사람이 한 마디 남겼다

        assert limiter.allowed("T1") is True  # 다시 봇 왕복을 받을 수 있다

    def test_리셋한_적_없는_스레드를_리셋해도_안전하다(self) -> None:
        limiter = PeerExchangeLimiter(limit=1)
        limiter.reset("T_NEVER_SEEN")  # 예외 없이 지나간다


# C25 -----------------------------------------------------------------------


class _FakeSlackClient:
    def __init__(self, profiles: dict[str, dict[str, Any]] | None = None) -> None:
        self._profiles = profiles or {}
        self.call_count = 0

    def users_info(self, user: str) -> dict[str, Any]:
        self.call_count += 1
        if user not in self._profiles:
            raise RuntimeError("조회 실패")
        return {"user": self._profiles[user]}


class TestBotUserResolver_캐시전용_조회:
    def test_캐시_미스는_호출_없이_거짓이다(self) -> None:
        client = _FakeSlackClient()
        resolver = BotUserResolver(client)

        assert resolver.known("U_UNKNOWN") is False
        assert client.call_count == 0

    def test_빈_user_id도_호출_없이_거짓이다(self) -> None:
        client = _FakeSlackClient()
        resolver = BotUserResolver(client)

        assert resolver.known("") is False
        assert client.call_count == 0

    def test_is_bot으로_채운_캐시를_그대로_읽는다(self) -> None:
        client = _FakeSlackClient({"U0REI": {"id": "U0REI", "is_bot": True, "profile": {}, "name": ""}})
        resolver = BotUserResolver(client)

        resolver.is_bot("U0REI")  # 조회가 일어나 캐시를 채운다
        client.call_count = 0  # 이후 known() 은 조회하지 않는지 확인

        assert resolver.known("U0REI") is True
        assert client.call_count == 0

    def test_사람으로_확인된_캐시도_그대로_읽는다(self) -> None:
        client = _FakeSlackClient({"U0TAEL": {"id": "U0TAEL", "is_bot": False, "profile": {}, "name": ""}})
        resolver = BotUserResolver(client)

        resolver.is_bot("U0TAEL")
        client.call_count = 0

        assert resolver.known("U0TAEL") is False
        assert client.call_count == 0


# C26 + E6 + E7 — EventListener 배선 -----------------------------------------


class _ThreadClient:
    def __init__(self, messages: list[dict[str, Any]] | None = None) -> None:
        self._messages = messages or []

    def conversations_replies(self, **kwargs: Any) -> dict[str, Any]:
        return {"messages": self._messages}


def _registry(tmp_path: Path, entries: dict[str, Any]) -> ChannelRegistry:
    import json

    path = tmp_path / "channels.json"
    path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    return ChannelRegistry(path)


class TestC26_다른_봇이_나를_불렀는지:
    def test_나를_불렀으면_받는다(self) -> None:
        listener = EventListener(
            _ThreadClient(), ChannelRegistry(Path("/nonexistent.json")),
            ResponseGate(), identity=fake_identity(),
        )
        event = {"channel": "C1", "bot_id": "B_OTHER", "ts": "1.0", "text": "<@U_BOT> 검토해줘"}
        assert listener.from_app_mention(event) is not None

    def test_나를_안_불렀으면_안_받는다(self) -> None:
        listener = EventListener(
            _ThreadClient(), ChannelRegistry(Path("/nonexistent.json")),
            ResponseGate(), identity=fake_identity(),
        )
        event = {"channel": "C1", "bot_id": "B_OTHER", "ts": "1.0", "text": "다른 봇들끼리 하는 말"}
        assert listener.from_app_mention(event) is None

    def test_사람이_부른_말은_그대로_받는다(self) -> None:
        """기존 계약 — bot_id 가 없는 사람 멘션은 영향 없다."""
        listener = EventListener(
            _ThreadClient(), ChannelRegistry(Path("/nonexistent.json")),
            ResponseGate(), identity=fake_identity(),
        )
        event = {"channel": "C1", "user": "U1", "ts": "1.0", "text": "아무 말"}
        assert listener.from_app_mention(event) is not None


class TestE6_네튜플:
    def test_넷_다_돌려준다(self, tmp_path: Path) -> None:
        client = _ThreadClient(
            [
                {"ts": "0.9", "bot_id": "B_BOT", "text": "질문입니다"},
            ]
        )
        listener = EventListener(
            client, ChannelRegistry(Path("/nonexistent.json")),
            ResponseGate(), identity=fake_identity(),
        )
        joined, bot_asked, peer_talk, root_self = listener._thread_state("C1", "0.9")
        assert (joined, bot_asked, peer_talk, root_self) == (True, False, False, True)

    def test_조회_실패는_넷_다_거짓이다(self) -> None:
        class _터지는클라이언트:
            def conversations_replies(self, **kwargs: Any) -> dict[str, Any]:
                raise RuntimeError("레이트리밋")

        listener = EventListener(
            _터지는클라이언트(), ChannelRegistry(Path("/nonexistent.json")),
            ResponseGate(), identity=fake_identity(),
        )
        assert listener._thread_state("C1", "1.0") == (False, False, False, False)


class TestC17_배선_이름생략_이어말:
    def test_직전_사람_말이_다른_봇을_불렀으면_끼어들지_않는다(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path, {"C1": {"answer_unaddressed": True}})
        client = _ThreadClient(
            [
                {"ts": "1.0", "user": "U1", "text": "<@U0OTHERBOT> 검토해줘"},
                {"ts": "1.1", "bot_id": "B_BOT", "text": "확인할게요"},
            ]
        )
        listener = EventListener(
            client, registry, ResponseGate(), identity=fake_identity(),
            is_bot=_is_bot({"U0OTHERBOT"}),
        )
        event = {
            "channel": "C1", "user": "U1", "ts": "2.0", "thread_ts": "1.0",
            "text": "그거 어떻게 됐어?",
        }
        assert listener.from_message(event) is None

    def test_봇이_되물은_뒤에는_이어말이어도_받는다(self, tmp_path: Path) -> None:
        """bot_asked 가 참이면 C17 보다 그 되물음이 앞선다."""
        registry = _registry(tmp_path, {"C1": {"answer_unaddressed": True}})
        client = _ThreadClient(
            [
                {"ts": "1.0", "user": "U1", "text": "<@U0OTHERBOT> 검토해줘"},
                {"ts": "1.1", "bot_id": "B_BOT", "text": "지금 반영할까요?"},
            ]
        )
        listener = EventListener(
            client, registry, ResponseGate(), identity=fake_identity(),
            is_bot=_is_bot({"U0OTHERBOT"}),
        )
        event = {
            "channel": "C1", "user": "U1", "ts": "2.0", "thread_ts": "1.0",
            "text": "네",
        }
        ctx = listener.from_message(event)
        assert ctx is not None
        assert ctx.unaddressed is True


class TestE7_배선_첨부있으면_무조건_응답:
    def test_첨부만_있고_본문이_비어도_받는다(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path, {"C1": {"answer_unaddressed": True}})
        client = _ThreadClient([{"ts": "0.9", "bot_id": "B_BOT", "text": "안녕"}])
        listener = EventListener(client, registry, ResponseGate(), identity=fake_identity())
        event = {
            "channel": "C1", "user": "U1", "ts": "1.0", "thread_ts": "0.9",
            "text": "", "files": [{"name": "스샷.png"}],
        }
        ctx = listener.from_message(event)
        assert ctx is not None
        assert ctx.unaddressed is True

    def test_첨부가_있어도_다른_참가자를_부른_말은_안_받는다(self, tmp_path: Path) -> None:
        """E7 이 addresses_someone_else 보다 앞서지 않는다."""
        registry = _registry(tmp_path, {"C1": {"answer_unaddressed": True}})
        client = _ThreadClient([{"ts": "0.9", "bot_id": "B_BOT", "text": "안녕"}])
        listener = EventListener(client, registry, ResponseGate(), identity=fake_identity())
        event = {
            "channel": "C1", "user": "U1", "ts": "1.0", "thread_ts": "0.9",
            "text": "<@U0OTHERPERSON> 봐주세요", "files": [{"name": "스샷.png"}],
        }
        assert listener.from_message(event) is None

    def test_참여하지_않은_스레드는_첨부가_있어도_안_받는다(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path, {"C1": {"answer_unaddressed": True}})
        client = _ThreadClient([{"ts": "0.9", "user": "U1", "text": "그냥 잡담"}])
        listener = EventListener(client, registry, ResponseGate(), identity=fake_identity())
        event = {
            "channel": "C1", "user": "U1", "ts": "1.0", "thread_ts": "0.9",
            "text": "", "files": [{"name": "스샷.png"}],
        }
        assert listener.from_message(event) is None

    def test_감탄사만_있어도_첨부가_있으면_받는다(self) -> None:
        gate = ResponseGate()
        assert gate.worth_answering("감사합니다", False, has_files=True) is True

    def test_첨부없는_감탄사는_여전히_거른다(self) -> None:
        gate = ResponseGate()
        assert gate.worth_answering("감사합니다", False, has_files=False) is False


# B1 / B2 — AskerSection 3분기 ------------------------------------------------


def _ctx(**overrides: object) -> CompositionContext:
    base: dict[str, object] = {
        "principal": Principal(
            user_id="U1", channel="C1", trust=TrustLevel.GENERAL, is_direct_message=False,
        ),
        "library": PromptLibrary(Path("/does/not/exist")),
    }
    base.update(overrides)
    return CompositionContext(**base)  # type: ignore[arg-type]


class TestAskerSection_봇_3분기:
    def test_소유자는_기존과_같다(self) -> None:
        ctx = _ctx(principal=Principal(
            user_id="U_OWNER", channel="C1", trust=TrustLevel.OWNER, is_direct_message=False,
        ))
        note = AskerSection().render(ctx)
        assert "소유자 본인" in note
        # 소유자 갈래도 짝 봇 안내 줄에 "다른 봇" 이 들어간다(#71). 봇 화자 갈래 문구가 섞이지 않는지만 본다
        assert "사람이 아니라 소유자가 함께 돌리는" not in note

    def test_봇이_물으면_사람이_아니라고_알린다(self) -> None:
        ctx = _ctx(asker_is_bot=True, asker_name="구치파치", asker_id="U0PEER")
        note = AskerSection().render(ctx)
        assert "구치파치" in note
        assert "사람이 아니라" in note
        assert "<@U0PEER>" in note
        assert "상한" in note  # 왕복 상한이 있다는 안내

    def test_사람이_물으면_기존과_같다(self) -> None:
        ctx = _ctx(asker_is_bot=False, asker_name="홍길동", asker_id="U1")
        note = AskerSection().render(ctx)
        assert "홍길동" in note
        assert "사람이 아니라" not in note

    def test_asker_is_bot_기본값은_거짓이다(self) -> None:
        ctx = _ctx(asker_name="홍길동", asker_id="U1")
        assert ctx.asker_is_bot is False


# D3 — 수신자 표식 ------------------------------------------------------------


class TestRecipientResolver:
    def _resolver(self, bots: set[str]) -> RecipientResolver:
        return RecipientResolver(_is_bot(bots))

    def test_OWNER_표식을_읽는다(self) -> None:
        body, recipient = self._resolver(set()).resolve(
            "보고 드립니다.\n\n[[RECIPIENT:OWNER]]"
        )
        assert recipient == Recipient("owner")
        assert "[[RECIPIENT" not in body
        assert body == "보고 드립니다."

    def test_ASKER_표식을_읽는다(self) -> None:
        body, recipient = self._resolver(set()).resolve(
            "확인했습니다.\n\n[[RECIPIENT:ASKER]]", asker_id="U1"
        )
        assert recipient == Recipient("asker")
        assert body == "확인했습니다."

    def test_ASKER가_봇이면_BOT으로_접어_첫머리_멘션을_강제한다(self) -> None:
        body, recipient = self._resolver({"U0PEER"}).resolve(
            "동의합니다.\n\n[[RECIPIENT:ASKER]]", asker_id="U0PEER"
        )
        assert recipient == Recipient("bot", "U0PEER")
        assert body == "<@U0PEER> 동의합니다."

    def test_BOT_표식은_첫머리_멘션을_강제한다(self) -> None:
        body, recipient = self._resolver({"U0PEER"}).resolve(
            "검토 부탁드립니다.\n\n[[RECIPIENT:BOT:U0PEER]]"
        )
        assert recipient == Recipient("bot", "U0PEER")
        assert body == "<@U0PEER> 검토 부탁드립니다."

    def test_이미_그_봇을_부른_본문은_중복_멘션을_안_만든다(self) -> None:
        body, recipient = self._resolver({"U0PEER"}).resolve(
            "<@U0PEER> 검토 부탁드립니다.\n\n[[RECIPIENT:BOT:U0PEER]]"
        )
        assert recipient == Recipient("bot", "U0PEER")
        assert body == "<@U0PEER> 검토 부탁드립니다."

    def test_BOT_표식이_실제_봇이_아니면_미해결이다(self) -> None:
        """사람 ID 를 BOT 으로 적으면 엉뚱한 사람에게 멘션이 강제되는 대신
        수신자 미확정으로 처리된다 — 호출자가 게시를 막는다."""
        _body, recipient = self._resolver(set()).resolve(
            "검토 부탁드립니다.\n\n[[RECIPIENT:BOT:U0HUMAN]]"
        )
        assert recipient is None

    def test_표식이_없으면_미해결이다(self) -> None:
        body, recipient = self._resolver(set()).resolve("그냥 답변입니다")
        assert recipient is None
        assert body == "그냥 답변입니다"

    def test_뒤따르는_다른_표식은_보존한다(self) -> None:
        body, recipient = self._resolver(set()).resolve(
            "확인했습니다.\n[[RECIPIENT:OWNER]]\n[[NOWATCH]]"
        )
        assert recipient == Recipient("owner")
        assert body == "확인했습니다.\n[[NOWATCH]]"


# C18 + C19 배선 — EventListener 가 봇 왕복을 실제로 막는지 -----------------


class TestC19_배선_봇왕복_한도:
    """PeerExchangeLimiter 가 리스너 경로에 실제로 걸려 봇-봇 무한왕복을
    끊는지 고정한다. 유닛 primitive 시험(TestPeerExchangeLimiter)과 달리
    from_app_mention/from_message 배선 자체를 검증한다 (C19 cap, C18 reset).
    """

    def _listener(self, limiter: PeerExchangeLimiter) -> EventListener:
        return EventListener(
            _ThreadClient(),
            ChannelRegistry(Path("/nonexistent.json")),
            ResponseGate(),
            identity=fake_identity(),
            peer_limiter=limiter,
        )

    def test_봇_멘션이_한도를_넘으면_침묵한다(self) -> None:
        limiter = PeerExchangeLimiter(limit=1)
        listener = self._listener(limiter)
        event = {
            "channel": "C1", "bot_id": "B_OTHER", "ts": "1.0", "thread_ts": "1.0",
            "text": "<@U_BOT> 다시 봐줘",
        }
        assert listener.from_app_mention(event) is not None  # 1회차 허용
        assert listener.from_app_mention(event) is None  # 한도 초과 → 침묵

    def test_사람이_말하면_봇왕복_한도가_리셋된다(self) -> None:
        """C18 — 리셋이 배선 안 되면 한도 도달 뒤 영구 침묵한다."""
        limiter = PeerExchangeLimiter(limit=1)
        listener = self._listener(limiter)
        bot_event = {
            "channel": "C1", "bot_id": "B_OTHER", "ts": "1.0", "thread_ts": "1.0",
            "text": "<@U_BOT> 다시",
        }
        assert listener.from_app_mention(bot_event) is not None
        assert listener.from_app_mention(bot_event) is None  # 한도 도달

        # 사람이 스레드에 한 마디 → 리셋되어야 한다.
        human = {
            "channel": "C1", "user": "U1", "ts": "2.0", "thread_ts": "1.0",
            "text": "<@U_BOT> 이어서",
        }
        assert listener.from_app_mention(human) is not None
        assert listener.from_app_mention(bot_event) is not None  # 다시 왕복 허용

    def test_from_message_사람말도_한도를_리셋한다(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path, {"C1": {"answer_unaddressed": True}})
        client = _ThreadClient([{"ts": "1.0", "bot_id": "B_BOT", "text": "지금 반영할까요?"}])
        limiter = PeerExchangeLimiter(limit=1)
        listener = EventListener(
            client, registry, ResponseGate(), identity=fake_identity(),
            peer_limiter=limiter,
        )
        assert limiter.allowed("1.0") is True
        assert limiter.allowed("1.0") is False  # 한도 도달

        human = {"channel": "C1", "user": "U1", "ts": "2.0", "thread_ts": "1.0", "text": "네"}
        listener.from_message(human)  # 사람이 말했으니 리셋되어야 한다

        assert limiter.allowed("1.0") is True

    def test_limiter_없으면_제한이_없다(self) -> None:
        """기존 계약 — peer_limiter 미전달 시 봇 멘션을 몇 번이든 받는다."""
        listener = EventListener(
            _ThreadClient(), ChannelRegistry(Path("/nonexistent.json")),
            ResponseGate(), identity=fake_identity(),
        )
        event = {
            "channel": "C1", "bot_id": "B_OTHER", "ts": "1.0", "thread_ts": "1.0",
            "text": "<@U_BOT> 다시",
        }
        for _ in range(10):
            assert listener.from_app_mention(event) is not None
