"""캐치업의 봇 요청 복구, 본문 판정, 다른 봇 대화 끼어들기 방지 시험 (C3).

마메치 bot.py find_missed 가 하던 판정이다. 실시간 경로(listener)는 봇이
부른 말, 리치 본문 안의 멘션, 다른 봇을 부른 말과 다른 봇과 이어지던
대화를 가린다. 캐치업이 이 기준을 따르지 않으면 재기동 전에 들어온 봇
요청은 영구히 답을 못 받고, 실시간이 넘긴 말을 재기동 직후 되집는다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from identity_support import fake_identity

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.guard.mentions import PeerExchangeLimiter
from slack_cli_agent.observability.notices import NoticeCatalog
from slack_cli_agent.reliability.catchup import CatchupService
from slack_cli_agent.slack.gate import ResponseGate

BOTS = {"UPEERBOT", "UOTHERBOT"}


@dataclass
class _History:
    history: dict[str, list[Mapping[str, Any]] | None] = field(default_factory=dict)
    threads: dict[str, list[Mapping[str, Any]]] = field(default_factory=dict)

    def read_history(self, channel, oldest, limit):
        return self.history.get(channel)

    def read_thread(self, channel, thread_ts, limit):
        return self.threads.get(thread_ts, [])

    def read_full_thread(self, channel, thread_ts, page_size):
        return self.threads.get(thread_ts, [])


def _service(
    history: _History,
    *,
    bot_user_of=lambda _bot_id: "",
    peer_limiter: PeerExchangeLimiter | None = None,
) -> CatchupService:
    return CatchupService(
        history=history,
        gate=ResponseGate(),
        notices=NoticeCatalog(),
        settings=RuntimeSettings(),
        identity=fake_identity(user_id="U_BOT", bot_id="B_BOT"),
        now=lambda: 100_000.0,
        started_at=0.0,
        is_bot=lambda uid: uid in BOTS,
        bot_user_of=bot_user_of,
        peer_limiter=peer_limiter,
    )


def _rich(text: str, *elements: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [{"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": list(elements)}]}]


def test_재기동_전에_다른_봇이_부른_말을_되집는다() -> None:
    peer = {
        "ts": "99000.0",
        "bot_id": "B_PEER",
        "user": "UPEERBOT",
        "subtype": "bot_message",
        "text": "<@U_BOT> 이 표 검토해 주세요",
    }
    found = _service(_History(history={"C1": [peer]})).find_missed("C1", 3600).value()

    assert [c.ts for c in found] == ["99000.0"]
    assert found[0].user == "UPEERBOT"
    assert found[0].unaddressed is False


def test_리치_본문에만_멘션이_있는_봇_요청도_되집고_본문을_싣는다() -> None:
    peer = {
        "ts": "99000.0",
        "bot_id": "B_PEER",
        "user": "UPEERBOT",
        "text": "검토 요청",
        "blocks": _rich(
            "",
            {"type": "user", "user_id": "U_BOT"},
            {"type": "text", "text": " 아래 표의 합계를 확인해 주세요"},
        ),
    }
    found = _service(_History(history={"C1": [peer]})).find_missed("C1", 3600).value()

    assert [c.ts for c in found] == ["99000.0"]
    assert "합계를 확인해 주세요" in found[0].text


def test_사람의_리치_본문_멘션도_되집는다() -> None:
    human = {
        "ts": "99000.0",
        "user": "U1",
        "text": "알림 미리보기",
        "blocks": _rich(
            "",
            {"type": "user", "user_id": "U_BOT"},
            {"type": "text", "text": " 어제 적재 건수 알려줘"},
        ),
    }
    found = _service(_History(history={"C1": [human]})).find_missed("C1", 3600).value()

    assert [c.ts for c in found] == ["99000.0"]
    assert found[0].unaddressed is False


def test_부르지_않은_봇의_말은_되집지_않는다() -> None:
    """회귀 방지 시험이다. 수정 전 코드는 봇 말을 전부 버려서 이 시험을 통과한다.
    봇 요청을 후보로 연 뒤에도 부르지 않은 봇 말까지 열리지 않는지 지킨다."""
    thread = [
        {"ts": "98000.0", "user": "U1", "text": "<@U_BOT> 처음", "reactions": [{"name": "white_check_mark"}]},
        {"ts": "98500.0", "bot_id": "B_BOT", "user": "U_BOT", "text": "답했습니다"},
        {"ts": "99000.0", "bot_id": "B_PEER", "user": "UPEERBOT", "text": "제 의견은 이렇습니다?"},
    ]
    history = _History(
        history={"C1": [{**thread[0], "reply_count": 2}]}, threads={"98000.0": thread}
    )

    assert _service(history).find_missed("C1", 3600).value() == []


def test_이미_답한_봇_요청은_되집지_않는다() -> None:
    """회귀 방지 시험이다. 수정 전 코드도 통과한다. 봇 요청과 봇 답의 짝짓기를 지킨다."""
    thread = [
        {"ts": "98000.0", "bot_id": "B_PEER", "user": "UPEERBOT", "text": "<@U_BOT> 확인 부탁"},
        {"ts": "98500.0", "bot_id": "B_BOT", "user": "U_BOT", "text": "확인했습니다"},
    ]
    history = _History(
        history={"C1": [{**thread[0], "reply_count": 1}]}, threads={"98000.0": thread}
    )

    assert _service(history).find_missed("C1", 3600).value() == []


def test_문장_중간에서_다른_봇을_부른_말은_되집지_않는다() -> None:
    """실시간 경로의 calls_other_bot (C16) 과 같은 기준이다."""
    thread = [
        {"ts": "98000.0", "user": "U1", "text": "<@U_BOT> 처음", "reactions": [{"name": "white_check_mark"}]},
        {"ts": "98500.0", "bot_id": "B_BOT", "user": "U_BOT", "text": "답했습니다"},
        {"ts": "99000.0", "user": "U1", "text": "이건 <@UOTHERBOT> 가 확인해 줄래?"},
    ]
    history = _History(
        history={"C1": [{**thread[0], "reply_count": 2}]}, threads={"98000.0": thread}
    )

    assert _service(history).find_missed("C1", 3600).value() == []


def test_다른_봇과_이어지던_대화에_끼어들지_않는다() -> None:
    """사람이 마지막으로 부른 상대가 다른 봇이면 이름 없는 다음 말도 그쪽 몫이다 (C17)."""
    thread = [
        {"ts": "97000.0", "user": "U1", "text": "<@U_BOT> 처음", "reactions": [{"name": "white_check_mark"}]},
        {"ts": "97500.0", "bot_id": "B_BOT", "user": "U_BOT", "text": "답했습니다"},
        {"ts": "98000.0", "user": "U1", "text": "<@UOTHERBOT> 이 결과 어떻게 봐?"},
        {"ts": "98500.0", "bot_id": "B_PEER2", "user": "UOTHERBOT", "text": "이렇게 봅니다"},
        {"ts": "99000.0", "user": "U1", "text": "그럼 원인은 무엇인가요?"},
    ]
    history = _History(
        history={"C1": [{**thread[0], "reply_count": 4}]}, threads={"97000.0": thread}
    )

    assert _service(history).find_missed("C1", 3600).value() == []


def test_다른_봇_대화가_아니면_이름_없는_이어짐은_그대로_되집는다() -> None:
    """회귀 방지 시험이다. 수정 전 코드도 통과한다. C17 판정이 넓어져
    이어지는 사람 말까지 버리지 않는지 지킨다."""
    thread = [
        {"ts": "97000.0", "user": "U1", "text": "<@U_BOT> 처음", "reactions": [{"name": "white_check_mark"}]},
        {"ts": "97500.0", "bot_id": "B_BOT", "user": "U_BOT", "text": "답했습니다"},
        {"ts": "99000.0", "user": "U1", "text": "그럼 원인은 무엇인가요?"},
    ]
    history = _History(
        history={"C1": [{**thread[0], "reply_count": 2}]}, threads={"97000.0": thread}
    )

    found = _service(history).find_missed("C1", 3600).value()
    assert [c.ts for c in found] == ["99000.0"]


def test_앱의_캐치업에_봇_판정이_배선된다(tmp_path) -> None:
    """부품에 인자가 있어도 배선이 빠지면 운영에서는 아무도 봇이 아니다."""
    from test_application import FakeSlackClient, write_profile

    from slack_cli_agent.core.application import Application

    app = Application(write_profile(tmp_path), FakeSlackClient())
    assert app._catchup()._is_bot == app._bot_users.is_bot
    assert app._catchup()._bot_user_of == app._bot_users.user_of_bot
    # 실시간 경로와 같은 왕복 상한을 센다. 따로 두면 한쪽이 버린 멘션을 다른 쪽이 답한다.
    assert app._catchup()._peer_limiter is app._peer_exchange


def _nameless_peer(ts: str = "99000.0", thread_ts: str | None = None) -> dict[str, Any]:
    """chat:write.customize 로 이름을 바꿔 올린 봇 게시. user 필드가 없다."""
    msg: dict[str, Any] = {
        "ts": ts,
        "bot_id": "B_PEER",
        "subtype": "bot_message",
        "username": "구치파치",
        "text": "<@U_BOT> 이 표 검토해 주세요",
    }
    if thread_ts:
        msg["thread_ts"] = thread_ts
    return msg


def test_user_없는_봇_게시는_bot_id로_요청자를_찾는다() -> None:
    """실시간 경로 listener._context_from_event 의 bots_info 보정과 같다."""
    service = _service(
        _History(history={"C1": [_nameless_peer()]}),
        bot_user_of=lambda bot_id: "UPEERBOT" if bot_id == "B_PEER" else "",
    )

    found = service.find_missed("C1", 3600).value()

    assert [c.user for c in found] == ["UPEERBOT"]


def test_되살린_봇_요청에는_봇_표지가_남는다() -> None:
    found = _service(_History(history={"C1": [_nameless_peer()]})).find_missed("C1", 3600).value()

    assert found[0].extra.get("bot_id") == "B_PEER"


def test_사람_요청에는_봇_표지가_없다() -> None:
    human = {"ts": "99000.0", "user": "U1", "text": "<@U_BOT> 어제 건수"}
    found = _service(_History(history={"C1": [human]})).find_missed("C1", 3600).value()

    assert "bot_id" not in found[0].extra


def test_왕복_상한을_넘어_버린_봇_멘션은_캐치업이_되살리지_않는다() -> None:
    """실시간 경로가 상한(C19)으로 버린 멘션은 표식이 없어 미응답으로 남는다."""
    limiter = PeerExchangeLimiter(limit=1)
    assert limiter.allowed("99000.0")  # 실시간 경로가 이미 한 턴을 셌다
    assert not limiter.allowed("99000.0")  # 상한을 넘어 실시간이 버렸다
    service = _service(_History(history={"C1": [_nameless_peer()]}), peer_limiter=limiter)

    report = service.sweep(["C1"], 3600)

    assert report.missed == []


def test_캐치업이_되살린_봇_요청도_왕복_횟수에_센다() -> None:
    limiter = PeerExchangeLimiter(limit=1)
    service = _service(_History(history={"C1": [_nameless_peer()]}), peer_limiter=limiter)

    assert [c.ts for c in service.sweep(["C1"], 3600).missed] == ["99000.0"]
    # 같은 메시지를 다음 캐치업이 다시 잡아도 상한을 넘으면 답하지 않는다.
    assert service.sweep(["C1"], 3600).missed == []


def test_사람이_다시_부르면_상한을_새로_센다() -> None:
    """실시간 경로의 C18 초기화와 같다. 사람 호명 뒤 봇 요청은 다시 받는다."""
    limiter = PeerExchangeLimiter(limit=1)
    limiter.allowed("97000.0")
    limiter.allowed("97000.0")
    thread = [
        {"ts": "97000.0", "user": "U1", "text": "<@U_BOT> 처음", "reactions": [{"name": "white_check_mark"}]},
        {"ts": "98000.0", "user": "U1", "text": "<@U_BOT> 다시 봐 줘"},
        _nameless_peer("99000.0", thread_ts="97000.0"),
    ]
    history = _History(history={"C1": [{**thread[0], "reply_count": 2}]}, threads={"97000.0": thread})
    service = _service(history, peer_limiter=limiter)

    assert [c.ts for c in service.sweep(["C1"], 3600).missed] == ["99000.0"]


def test_앱_캐치업이_재기동_전_이름_바꾼_봇_호명을_접수한다(tmp_path) -> None:
    """advisor_plan C3 행의 통합 검증. 슬랙 대역부터 앱 조립까지 실제 배선으로 돈다."""
    import time

    from test_application import FakeSlackClient, write_profile

    from slack_cli_agent.core.application import Application

    asked = _nameless_peer(f"{time.time() - 300:.6f}")

    class Client(FakeSlackClient):
        def auth_test(self, **kwargs: Any) -> dict[str, Any]:
            return {"ok": True, "user_id": "U_BOT", "bot_id": "B_BOT"}

        def users_info(self, **kwargs: Any) -> dict[str, Any]:
            return {"user": {"id": kwargs.get("user"), "is_bot": kwargs.get("user") == "UPEERBOT"}}

        def bots_info(self, **kwargs: Any) -> dict[str, Any]:
            return {"bot": {"id": kwargs.get("bot"), "user_id": "UPEERBOT"}}

        def conversations_history(self, **kwargs: Any) -> dict[str, Any]:
            return {"ok": True, "messages": [asked]}

    app = Application(write_profile(tmp_path), Client())

    report = app._catchup().sweep(["C1"], 3600)

    assert [(c.ts, c.user) for c in report.missed] == [(asked["ts"], "UPEERBOT")]
    assert report.missed[0].extra.get("bot_id") == "B_PEER"


def test_상한에_걸린_스레드에서도_사람의_이름_없는_요청은_버리지_않는다() -> None:
    """실시간 from_message 는 스레드에 사람이 말하면 호명 여부와 관계없이
    상한을 새로 센다(C18). 상한 도달 → 사람의 이름 없는 질문 → 봇 호명 순서가
    캐치업으로 들어와도 실시간과 같이 답해야 한다. 수정 전에는 묶음 전체가
    버려져 사람 질문이 누가 다시 부를 때까지 미응답으로 남았다."""
    limiter = PeerExchangeLimiter(limit=1)
    limiter.allowed("97000.0")
    limiter.allowed("97000.0")
    thread = [
        {"ts": "97000.0", "user": "U1", "text": "<@U_BOT> 처음", "reactions": [{"name": "white_check_mark"}]},
        {"ts": "97500.0", "bot_id": "B_BOT", "user": "U_BOT", "text": "답했습니다"},
        {"ts": "98000.0", "user": "U1", "text": "그럼 원인은 무엇인가요?"},
        _nameless_peer("99000.0", thread_ts="97000.0"),
    ]
    history = _History(history={"C1": [{**thread[0], "reply_count": 3}]}, threads={"97000.0": thread})
    service = _service(history, peer_limiter=limiter)

    found = service.find_missed("C1", 3600).value()
    assert [c.ts for c in found] == ["98000.0", "99000.0"]
    assert found[0].unaddressed is True

    report = service.sweep(["C1"], 3600)

    # 대표의 답이 스레드 전체 맥락을 싣으므로 사람 질문도 함께 답을 받는다.
    assert [c.ts for c in report.missed] == ["99000.0"]
