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


def _service(history: _History) -> CatchupService:
    return CatchupService(
        history=history,
        gate=ResponseGate(),
        notices=NoticeCatalog(),
        settings=RuntimeSettings(),
        identity=fake_identity(user_id="U_BOT", bot_id="B_BOT"),
        now=lambda: 100_000.0,
        started_at=0.0,
        is_bot=lambda uid: uid in BOTS,
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
