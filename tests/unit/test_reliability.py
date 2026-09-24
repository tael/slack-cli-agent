"""신뢰성 계층 — 캐치업, 연결 감시, 지켜보기 큐, 중복 처리 방어.

기대 출력은 원본 `bot.py` 의 `reactions_on`, `already_handled`, `unanswered`
본문을 그대로 옮긴 하네스로 실제 실행해 얻었다(손으로 짐작하지 않았다). 하네스
스크립트는 `/tmp/harness/orig_funcs.py` 에 있고, 아래 각 케이스 옆 주석이 그
실행 결과다. 원본 `bot.py` 자체는 읽기만 했고 수정하지 않았다.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest
from identity_support import fake_identity

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.core.result import OutcomeKind
from slack_cli_agent.observability.notices import NoticeCatalog
from slack_cli_agent.slack.gate import ResponseGate
from slack_cli_agent.slack.identity import SlackBotIdentity

# dedup.py — DeduplicationTracker


class TestDeduplicationTracker:
    def test_처음_보는_조합은_False_다(self) -> None:
        from slack_cli_agent.reliability.dedup import DeduplicationTracker

        tracker = DeduplicationTracker()
        assert tracker.already_seen_event("C1", "1.1") is False

    def test_같은_조합을_다시_보면_True_다(self) -> None:
        from slack_cli_agent.reliability.dedup import DeduplicationTracker

        tracker = DeduplicationTracker()
        tracker.already_seen_event("C1", "1.1")
        assert tracker.already_seen_event("C1", "1.1") is True

    def test_채널이_다르면_같은_ts_도_별개다(self) -> None:
        from slack_cli_agent.reliability.dedup import DeduplicationTracker

        tracker = DeduplicationTracker()
        tracker.already_seen_event("C1", "1.1")
        assert tracker.already_seen_event("C2", "1.1") is False

    def test_상한을_넘기면_통째로_비운다(self) -> None:
        # 원본 _SEEN_EVENTS_MAX = 2000 과 같은 동작. 상한에 닿으면 비우고
        # 새 항목을 넣으므로, 상한 직후 다시 보는 항목은 새로 등록된 것으로 본다.
        from slack_cli_agent.reliability.dedup import DeduplicationTracker

        tracker = DeduplicationTracker(max_entries=2)
        assert tracker.already_seen_event("C", "1") is False
        assert tracker.already_seen_event("C", "2") is False
        # 상한(2)에 닿아 있던 상태에서 세 번째가 들어오며 비워지고 새로 등록된다
        assert tracker.already_seen_event("C", "3") is False
        # 방금 비워졌으므로 앞서 등록됐던 것은 잊혔다
        assert tracker.already_seen_event("C", "1") is False


# catchup.py — 순수 판정 함수 (원본 실행 결과로 기대값을 얻었다)


class TestReactionsOn:
    def test_리액션_이름_집합을_돌려준다(self) -> None:
        from slack_cli_agent.reliability.catchup import reactions_on

        msg = {"reactions": [{"name": "white_check_mark"}, {"name": "eyes"}]}
        # 실행 결과: {'eyes', 'white_check_mark'}
        assert reactions_on(msg) == {"eyes", "white_check_mark"}

    def test_리액션이_없으면_빈_집합이다(self) -> None:
        from slack_cli_agent.reliability.catchup import reactions_on

        assert reactions_on({}) == set()


class TestAlreadyHandled:
    @pytest.mark.parametrize(
        "reactions,expected",
        [
            # 실행 결과: already_handled 는 white_check_mark 가 있으면 True
            ([{"name": "white_check_mark"}, {"name": "eyes"}], True),
            # zipper_mouth_face(침묵 표식)도 끝난 것으로 본다
            ([{"name": "zipper_mouth_face"}], True),
            # eyes 만 있으면 아직 처리 중이다. 끝난 것이 아니다
            ([{"name": "eyes"}], False),
            ([], False),
        ],
    )
    def test_판정_결과(self, reactions: list[dict], expected: bool) -> None:
        from slack_cli_agent.reliability.catchup import already_handled

        assert already_handled({"reactions": reactions}) is expected


class TestUnanswered:
    """원본 `unanswered(thread, asked)` 를 그대로 실행해 얻은 결과.

    is_self 판정은 CatchupService 생성자로 주입하는 콜백이 대신한다.
    """

    @staticmethod
    def _is_self(msg: Mapping[str, Any]) -> bool:
        return msg.get("bot_id") == "B123"

    def test_뒤_요청의_답이_앞_요청을_묻지_않는다(self) -> None:
        from slack_cli_agent.reliability.catchup import unanswered

        thread = [
            {"ts": "100.0", "user": "U1", "text": "<@U_BOT> 질문1"},
            {"ts": "110.0", "user": "U1", "text": "<@U_BOT> 질문2"},
            {"ts": "120.0", "bot_id": "B123", "text": "답변", "user": "U_BOT"},
        ]
        asked = [thread[0], thread[1]]
        # 실행 결과: [{'ts': '110.0', ...}] — 13시와 13시11분 예시와 같은 형태
        result = unanswered(thread, asked, is_self=self._is_self, is_notice=lambda t: False)
        assert [m["ts"] for m in result] == ["110.0"]

    def test_안내문은_답으로_세지_않는다(self) -> None:
        from slack_cli_agent.reliability.catchup import unanswered

        thread = [
            {"ts": "200.0", "user": "U1", "text": "<@U_BOT> 질문"},
            {
                "ts": "210.0",
                "bot_id": "B123",
                "text": "지금은 재시작 중이에요. 잠시 후 다시 불러주세요.",
                "user": "U_BOT",
            },
        ]
        asked = [thread[0]]
        notices = NoticeCatalog()
        # 실행 결과: [{'ts': '200.0', ...}] — 안내문이 답으로 세어지지 않는다
        result = unanswered(thread, asked, is_self=self._is_self, is_notice=notices.is_notice)
        assert [m["ts"] for m in result] == ["200.0"]

    def test_연속된_봇_글은_하나로_본다(self) -> None:
        from slack_cli_agent.reliability.catchup import unanswered

        thread = [
            {"ts": "300.0", "user": "U1", "text": "<@U_BOT> 질문A"},
            {"ts": "310.0", "user": "U1", "text": "<@U_BOT> 질문B"},
            {"ts": "320.0", "bot_id": "B123", "text": "답변 파트1", "user": "U_BOT"},
            {"ts": "321.0", "bot_id": "B123", "text": "답변 파트2", "user": "U_BOT"},
        ]
        asked = [thread[0], thread[1]]
        # 실행 결과: [{'ts': '310.0', ...}] — 조각난 긴 답이 하나로 묶인다
        result = unanswered(thread, asked, is_self=self._is_self, is_notice=lambda t: False)
        assert [m["ts"] for m in result] == ["310.0"]

    def test_다른_봇의_말은_흐름을_끊지_않는다(self) -> None:
        from slack_cli_agent.reliability.catchup import unanswered

        thread = [
            {"ts": "400.0", "user": "U1", "text": "<@U_BOT> 질문"},
            {"ts": "410.0", "bot_id": "B_OTHER", "text": "다른봇 말", "user": "U_OTHER"},
        ]
        asked = [thread[0]]
        # 실행 결과: [{'ts': '400.0', ...}] — 다른 봇 말은 답도 흐름 차단도 아니다
        result = unanswered(thread, asked, is_self=self._is_self, is_notice=lambda t: False)
        assert [m["ts"] for m in result] == ["400.0"]


# catchup.py — CatchupService


@dataclass
class FakeHistoryReader:
    """`HistoryReader` 대역. 채널별 응답을 미리 등록해 둔다."""

    history: dict[str, list[Mapping[str, Any]] | None] = field(default_factory=dict)
    threads: dict[str, list[Mapping[str, Any]]] = field(default_factory=dict)
    history_calls: list[tuple[str, float, int]] = field(default_factory=list)

    def read_history(self, channel, oldest, limit):
        self.history_calls.append((channel, oldest, limit))
        return self.history.get(channel)

    def read_thread(self, channel, thread_ts, limit):
        return self.threads.get(thread_ts, [])


def make_service(
    history: FakeHistoryReader,
    *,
    bot_user_id: str = "U_BOT",
    identity=None,
    settings: RuntimeSettings | None = None,
    now: float = 100_000.0,
):
    from slack_cli_agent.reliability.catchup import CatchupService

    return CatchupService(
        history=history,
        gate=ResponseGate(),
        notices=NoticeCatalog(),
        settings=settings or RuntimeSettings(),
        identity=identity or fake_identity(user_id=bot_user_id, bot_id="B123"),
        now=lambda: now,
        started_at=0.0,  # 이 프로세스가 아주 예전에 떴다고 가정 — 유예 창을 검사 대상에서 뺀다
    )


class TestFindMissed:
    def test_기록을_못_읽으면_판정_불가다(self) -> None:
        history = FakeHistoryReader(history={"C1": None})
        service = make_service(history)

        outcome = service.find_missed("C1", window=3600)

        assert outcome.kind is OutcomeKind.UNKNOWN

    def test_멘션된_말_중_답_없는_것을_찾는다(self) -> None:
        history = FakeHistoryReader(
            history={
                "C1": [
                    {"ts": "99000.0", "user": "U1", "text": "<@U_BOT> 질문"},
                ]
            }
        )
        service = make_service(history)

        outcome = service.find_missed("C1", window=3600)

        assert outcome.is_found
        missed = outcome.value()
        assert [m.ts for m in missed] == ["99000.0"]
        assert missed[0].channel == "C1"
        assert missed[0].thread_ts == "99000.0"

    def test_파일을_붙여_부른_말도_찾는다(self) -> None:
        """슬랙은 파일을 붙이면 subtype 을 붙인다.

        소켓 이벤트를 받는 쪽은 그것을 사람 말로 받는다. 캐치업이 거르면 그
        요청은 재기동 중에 들어왔을 때만 유실되고, 평소에는 멀쩡해 보인다.
        """
        history = FakeHistoryReader(
            history={
                "C1": [
                    {"ts": "99000.0", "user": "U1", "subtype": "file_share", "text": "<@U_BOT> 이거 봐줘"},
                ]
            }
        )
        service = make_service(history)

        outcome = service.find_missed("C1", window=3600)

        assert [m.ts for m in outcome.value()] == ["99000.0"]

    def test_붙은_파일을_그대로_싣는다(self) -> None:
        """접수 경로는 event["files"] 를 RequestContext 에 담는다. 캐치업이 그것을
        안 담으면 재기동 중에 들어온 요청만 첨부 없이 엔진에 간다."""
        history = FakeHistoryReader(
            history={
                "C1": [
                    {
                        "ts": "99000.0",
                        "user": "U1",
                        "subtype": "file_share",
                        "text": "<@U_BOT> 이거 봐줘",
                        "files": [{"id": "F1", "name": "a.png", "url_private": "https://x/a.png"}],
                    },
                ]
            }
        )
        service = make_service(history)

        missed = service.find_missed("C1", window=3600).value()

        assert [f.get("id") for f in missed[0].files] == ["F1"]

    def test_본문_없는_부름은_안_찾는다(self) -> None:
        """접수 경로는 빈 본문을 되묻고 큐에 안 넣는다(ingress). 캐치업이 같은
        판정을 안 하면 그 부름이 캐치업으로만 엔진 한 턴을 쓴다."""
        history = FakeHistoryReader(
            history={
                "C1": [
                    {"ts": "99000.0", "user": "U1", "text": "<@U_BOT>"},
                ]
            }
        )
        service = make_service(history)

        assert service.find_missed("C1", window=3600).value() == []

    def test_채널_참여_알림은_안_찾는다(self) -> None:
        history = FakeHistoryReader(
            history={
                "C1": [
                    {"ts": "99000.0", "user": "U1", "subtype": "channel_join", "text": "<@U_BOT> 들어옴"},
                ]
            }
        )
        service = make_service(history)

        assert service.find_missed("C1", window=3600).value() == []

    def test_표시_이름이_붙은_멘션도_찾는다(self) -> None:
        """슬랙은 `<@U123|이름>` 형태로도 보낸다. 문자열 포함으로 보면 못 잡는다."""
        history = FakeHistoryReader(
            history={
                "C1": [
                    {"ts": "99000.0", "user": "U1", "text": "<@U_BOT|마메치> 질문"},
                ]
            }
        )
        service = make_service(history)

        assert [m.ts for m in service.find_missed("C1", window=3600).value()] == ["99000.0"]

    def test_다른_봇을_부른_것은_안_찾는다(self) -> None:
        """`<@U_BOT2>` 안에 `U_BOT` 이 들어 있다. 문자열 포함으로 보면 걸린다."""
        history = FakeHistoryReader(
            history={
                "C1": [
                    {"ts": "99000.0", "user": "U1", "text": "<@U_BOT2> 질문"},
                ]
            }
        )
        service = make_service(history)

        assert service.find_missed("C1", window=3600).value() == []

    def test_봇이_낀_스레드여도_다른_참가자를_부른_말은_안_찾는다(self) -> None:
        """실시간 경로가 거른 것을 복구가 다시 집어 들면 안 된다.

        2026-09-19 18:44 에 아스카만 부른 요청을 이 봇이 받았다. 실시간
        판정만 고치면 같은 메시지가 다음 캐치업에서 되살아난다.
        """
        history = FakeHistoryReader(
            history={
                "C1": [
                    {"ts": "98000.0", "user": "U1", "text": "<@U_BOT> 처음 질문", "reply_count": 2},
                ]
            },
            threads={
                "98000.0": [
                    {
                        "ts": "98000.0",
                        "user": "U1",
                        "text": "<@U_BOT> 처음 질문",
                        "reactions": [{"name": "white_check_mark"}],
                    },
                    {"ts": "98500.0", "bot_id": "B123", "text": "답했습니다"},
                    {"ts": "99000.0", "user": "U1", "text": "<@U_BOT2> 리뷰해주세요"},
                ]
            },
        )
        service = make_service(history)

        assert service.find_missed("C1", window=3600).value() == []

    def test_신원을_모르면_판정_불가로_올린다(self) -> None:
        """부름 판정의 근거가 없으면 "부른 말이 없다" 와 구분되지 않는다.

        조회에 실패한 그 회차를 "놓친 요청 없음" 으로 끝내면 재시도 목록에서도
        빠져, 신원이 복구돼도 그 구간을 회수하지 못한다. 원본은 봇 사용자 ID 가
        없으면 캐치업 자체를 건너뛴다.
        """

        class 조회실패:
            def auth_test(self, **kwargs):
                raise RuntimeError("슬랙에 못 닿는다")

        history = FakeHistoryReader(
            history={
                "C1": [
                    {"ts": "99000.0", "user": "U1", "text": "<@U_BOT> 질문"},
                ]
            }
        )
        service = make_service(history, identity=SlackBotIdentity(조회실패()))

        assert service.find_missed("C1", window=3600).kind is OutcomeKind.UNKNOWN

    def test_판정_불가인_채널은_다시_볼_목록에_남는다(self) -> None:
        class 조회실패:
            def auth_test(self, **kwargs):
                raise RuntimeError("슬랙에 못 닿는다")

        history = FakeHistoryReader(history={"C1": []})
        service = make_service(history, identity=SlackBotIdentity(조회실패()))

        report = service.sweep(["C1"], window=3600)

        assert report.unchecked_channels == ["C1"]

    def test_이미_답변_표식이_있으면_빠진다(self) -> None:
        history = FakeHistoryReader(
            history={
                "C1": [
                    {
                        "ts": "99000.0",
                        "user": "U1",
                        "text": "<@U_BOT> 질문",
                        "reactions": [{"name": "white_check_mark"}],
                    },
                ]
            }
        )
        service = make_service(history)

        outcome = service.find_missed("C1", window=3600)

        assert outcome.value() == []

    def test_유예_시간_안의_방금_온_말은_아직_대상이_아니다(self) -> None:
        settings = RuntimeSettings(catchup_grace_sec=120)
        now = 100_000.0
        history = FakeHistoryReader(
            history={
                "C1": [
                    {"ts": f"{now - 10:.6f}", "user": "U1", "text": "<@U_BOT> 방금 질문"},
                ]
            }
        )
        # started_at 을 now 보다 이전으로 잡아 "이 프로세스가 뜬 뒤" 조건을 만족시킨다
        from slack_cli_agent.reliability.catchup import CatchupService

        service = CatchupService(
            history=history,
            gate=ResponseGate(),
            notices=NoticeCatalog(),
            settings=settings,
            identity=fake_identity(user_id="U_BOT", bot_id="B123"),
            now=lambda: now,
            started_at=now - 3600,
        )

        outcome = service.find_missed("C1", window=3600)

        assert outcome.value() == []

    def test_스레드_답글도_찾는다(self) -> None:
        history = FakeHistoryReader(
            history={
                "C1": [
                    {
                        "ts": "99000.0",
                        "thread_ts": "99000.0",
                        "reply_count": 1,
                        "latest_reply": "99005.0",
                        "user": "U1",
                        "text": "부모 글",
                    },
                ]
            },
            threads={
                "99000.0": [
                    {"ts": "99000.0", "user": "U1", "text": "부모 글"},
                    {"ts": "99005.0", "user": "U1", "text": "<@U_BOT> 스레드 답글 질문"},
                ]
            },
        )
        service = make_service(history)

        outcome = service.find_missed("C1", window=3600)

        assert [m.ts for m in outcome.value()] == ["99005.0"]


class TestSweep:
    def test_한_스레드에_여럿이면_최근_것만_대표로_남는다(self) -> None:
        history = FakeHistoryReader(
            history={
                "C1": [
                    {
                        "ts": "99000.0",
                        "thread_ts": "99000.0",
                        "reply_count": 1,
                        "latest_reply": "99010.0",
                        "user": "U1",
                        "text": "<@U_BOT> 1",
                    },
                ]
            },
            threads={
                "99000.0": [
                    {"ts": "99000.0", "user": "U1", "text": "<@U_BOT> 1"},
                    {"ts": "99010.0", "user": "U1", "text": "<@U_BOT> 2"},
                ]
            },
        )
        service = make_service(history)

        report = service.sweep(["C1"], window=3600)

        assert [c.ts for c in report.missed] == ["99010.0"]
        assert [c.ts for c in report.skipped] == ["99000.0"]
        assert report.missed[0].late is True

    def test_대표건은_늦었다는_표식이_붙는다(self) -> None:
        history = FakeHistoryReader(
            history={"C1": [{"ts": "99000.0", "user": "U1", "text": "<@U_BOT> 질문"}]}
        )
        service = make_service(history)

        report = service.sweep(["C1"], window=3600)

        assert report.missed[0].late is True

    def test_조회_실패한_채널은_다시_볼_목록에_남는다(self) -> None:
        history = FakeHistoryReader(history={"C1": None})
        service = make_service(history)

        report = service.sweep(["C1"], window=3600)

        assert report.unchecked_channels == ["C1"]
        assert report.missed == []


class TestRetryPending:
    def test_아직_유예_안의_채널은_다시_보지_않는다(self) -> None:
        history = FakeHistoryReader(history={"C1": None})
        now = 1000.0
        service = make_service(history, now=now)
        service.sweep(["C1"], window=3600)

        # 재시도 최초 대기(30초)가 지나지 않았다
        statuses = service.retry_pending()

        assert statuses == []

    def test_유예가_지나면_다시_보고_찾으면_돌려준다(self) -> None:
        history = FakeHistoryReader(history={"C1": None})
        now = 1000.0
        service = make_service(history, now=now)
        service.sweep(["C1"], window=3600)

        # 유예(catchup_grace_sec 기본 120초)보다 확실히 이전 시각으로 둔다.
        # 방금 온 말은 아직 처리 중일 수 있어 유예 안에서는 대상이 아니다.
        history.history["C1"] = [
            {"ts": "800.0", "user": "U1", "text": "<@U_BOT> 질문"}
        ]
        # 시계를 30초 뒤로 돌린다(첫 backoff 30초).
        service._now = lambda: now + 31  # type: ignore[attr-defined]

        statuses = service.retry_pending()

        assert len(statuses) == 1
        assert statuses[0].channel == "C1"
        assert [m.ts for m in statuses[0].missed] == ["800.0"]


# health.py — SocketErrorWatch, HealthMonitor


class TestSocketErrorWatch:
    def test_오류_문구를_세어둔다(self) -> None:
        from slack_cli_agent.reliability.health import SocketErrorWatch

        watch = SocketErrorWatch(now=lambda: 100.0)
        record = logging.LogRecord(
            "slack_bolt", logging.ERROR, __file__, 1, "on_error invoked", None, None
        )
        watch.emit(record)
        assert watch.recent_errors(window=10) == 1

    def test_재연결_문구를_따로_센다(self) -> None:
        from slack_cli_agent.reliability.health import SocketErrorWatch

        watch = SocketErrorWatch(now=lambda: 100.0)
        record = logging.LogRecord(
            "slack_sdk.socket_mode",
            logging.INFO,
            __file__,
            1,
            "A new session has been established",
            None,
            None,
        )
        watch.emit(record)
        assert watch.recent_reconnects(window=10) == 1
        assert watch.recent_errors(window=10) == 0

    def test_무관한_문구는_세지_않는다(self) -> None:
        from slack_cli_agent.reliability.health import SocketErrorWatch

        watch = SocketErrorWatch(now=lambda: 100.0)
        record = logging.LogRecord(
            "slack_bolt", logging.INFO, __file__, 1, "그냥 정보 로그", None, None
        )
        watch.emit(record)
        assert watch.recent_errors(window=10) == 0
        assert watch.recent_reconnects(window=10) == 0

    def test_창_밖의_기록은_안_센다(self) -> None:
        from slack_cli_agent.reliability.health import SocketErrorWatch

        clock = {"t": 0.0}
        watch = SocketErrorWatch(now=lambda: clock["t"])
        record = logging.LogRecord(
            "slack_bolt", logging.ERROR, __file__, 1, "Failed to connect", None, None
        )
        watch.emit(record)
        clock["t"] = 1000.0
        assert watch.recent_errors(window=180) == 0

    def test_clear_로_비운다(self) -> None:
        from slack_cli_agent.reliability.health import SocketErrorWatch

        watch = SocketErrorWatch(now=lambda: 100.0)
        watch.emit(
            logging.LogRecord(
                "x", logging.ERROR, __file__, 1, "on_error invoked", None, None
            )
        )
        watch.clear()
        assert watch.recent_errors(window=10) == 0


@dataclass
class RestartSpy:
    reasons: list[str] = field(default_factory=list)

    def __call__(self, reason: str) -> None:
        self.reasons.append(reason)


class TestHealthMonitor:
    def test_안닿으면_DOWN을_돌려준다(self) -> None:
        from slack_cli_agent.reliability.health import (
            HealthEventKind,
            HealthMonitor,
            SocketErrorWatch,
        )

        monitor = HealthMonitor(
            watch=SocketErrorWatch(),
            reachable=lambda: False,
            restart=RestartSpy(),
            settings=RuntimeSettings(),
        )
        event = monitor.check()
        assert event.kind is HealthEventKind.DOWN

    def test_끊겼다_돌아오면_RECOVERED_를_돌려주고_끊긴_시간을_잰다(self) -> None:
        from slack_cli_agent.reliability.health import (
            HealthEventKind,
            HealthMonitor,
            SocketErrorWatch,
        )

        clock = {"t": 0.0}
        reachable = {"ok": False}
        monitor = HealthMonitor(
            watch=SocketErrorWatch(now=lambda: clock["t"]),
            reachable=lambda: reachable["ok"],
            restart=RestartSpy(),
            settings=RuntimeSettings(),
            now=lambda: clock["t"],
        )
        monitor.check()  # DOWN, down_since = 0
        clock["t"] = 90.0
        reachable["ok"] = True

        event = monitor.check()

        assert event.kind is HealthEventKind.RECOVERED
        assert event.outage_sec == 90.0

    def test_재연결_상한을_넘으면_재기동한다(self) -> None:
        # 실측 근거: 정상 4시간35분 0회 vs 장애 22분 128회. 상한 4.
        from slack_cli_agent.reliability.health import (
            HealthEventKind,
            HealthMonitor,
            SocketErrorWatch,
        )

        clock = {"t": 0.0}
        watch = SocketErrorWatch(now=lambda: clock["t"])
        for _ in range(4):
            watch.emit(
                logging.LogRecord(
                    "x", logging.INFO, __file__, 1,
                    "A new session has been established", None, None,
                )
            )
        spy = RestartSpy()
        monitor = HealthMonitor(
            watch=watch,
            reachable=lambda: True,
            restart=spy,
            settings=RuntimeSettings(socket_reconnect_limit=4),
            now=lambda: clock["t"],
        )

        event = monitor.check()

        assert event.kind is HealthEventKind.RESTARTED
        assert len(spy.reasons) == 1
        # 실제 프로세스를 죽이지 않는다 — 주입한 대역이 사유 문자열만 받는다
        assert "4" in spy.reasons[0]

    def test_재연결이_상한_미만이면_그대로_돈다(self) -> None:
        from slack_cli_agent.reliability.health import (
            HealthEventKind,
            HealthMonitor,
            SocketErrorWatch,
        )

        monitor = HealthMonitor(
            watch=SocketErrorWatch(),
            reachable=lambda: True,
            restart=RestartSpy(),
            settings=RuntimeSettings(),
        )
        event = monitor.check()
        assert event.kind is HealthEventKind.OK

    def test_오류_상한을_넘어도_재기동한다(self) -> None:
        from slack_cli_agent.reliability.health import (
            HealthEventKind,
            HealthMonitor,
            SocketErrorWatch,
        )

        clock = {"t": 0.0}
        watch = SocketErrorWatch(now=lambda: clock["t"])
        for _ in range(8):
            watch.emit(
                logging.LogRecord(
                    "x", logging.ERROR, __file__, 1, "Failed to send", None, None
                )
            )
        spy = RestartSpy()
        monitor = HealthMonitor(
            watch=watch,
            reachable=lambda: True,
            restart=spy,
            settings=RuntimeSettings(socket_error_limit=8),
            now=lambda: clock["t"],
        )

        event = monitor.check()

        assert event.kind is HealthEventKind.RESTARTED


# watchjobs.py — WatchJobQueue


@dataclass
class Clock:
    t: float = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def watch_queue(database, clock: Clock):
    from slack_cli_agent.reliability.watchjobs import WatchJobQueue

    return WatchJobQueue(database, now=clock)


class TestWatchJobQueue:
    def test_등록하면_id를_돌려준다(self, watch_queue) -> None:
        job_id = watch_queue.enqueue("C1", "T1", "배포 결과 확인")
        assert isinstance(job_id, int)

    def test_방금_등록한_것은_유예_전에는_대상이_아니다(self, watch_queue) -> None:
        watch_queue.enqueue("C1", "T1", "확인")
        due = watch_queue.due(now=1000.0, min_gap=300)
        assert due == []

    def test_유예가_지나면_확인_대상이다(self, watch_queue) -> None:
        watch_queue.enqueue("C1", "T1", "확인")
        due = watch_queue.due(now=1000.0 + 301, min_gap=300)
        assert len(due) == 1
        assert due[0].condition == "확인"

    def test_확인_뒤_last_run을_갱신하면_다음_유예까지_다시_안_나온다(
        self, watch_queue
    ) -> None:
        job_id = watch_queue.enqueue("C1", "T1", "확인")
        watch_queue.mark_checked(job_id, at=1000.0)
        assert watch_queue.due(now=1200.0, min_gap=300) == []
        assert len(watch_queue.due(now=1301.0, min_gap=300)) == 1

    def test_완료로_표시하면_대상에서_빠진다(self, watch_queue) -> None:
        job_id = watch_queue.enqueue("C1", "T1", "확인")
        watch_queue.mark_done(job_id)
        assert watch_queue.due(now=2000.0, min_gap=300) == []

    def test_재기동해도_등록된_건이_남는다(self, database, clock: Clock) -> None:
        from slack_cli_agent.reliability.watchjobs import WatchJobQueue

        WatchJobQueue(database, now=clock).enqueue("C1", "T1", "확인")
        # 같은 DB 를 다시 여는 것이 프로세스 재기동에 대응한다
        again = WatchJobQueue(database, now=clock)
        assert len(again.due(now=2000.0, min_gap=300)) == 1

    def test_24시간을_넘긴_건을_가려낸다(self, watch_queue) -> None:
        job_id = watch_queue.enqueue("C1", "T1", "확인")
        expired = watch_queue.expired(now=1000.0 + 24 * 3600 + 1, max_age=24 * 3600)
        assert [j.id for j in expired] == [job_id]

    def test_완료된_건은_만료_목록에도_없다(self, watch_queue) -> None:
        job_id = watch_queue.enqueue("C1", "T1", "확인")
        watch_queue.mark_done(job_id)
        expired = watch_queue.expired(now=1000.0 + 24 * 3600 + 1, max_age=24 * 3600)
        assert expired == []


class TestSocketErrorWatch이력조회:
    """상태 기록이 쓸 수 있게 시각 목록 자체를 내주는가.

    최근 N초 건수만 내면 기록하는 쪽이 다른 창으로 셀 수 없다.
    """

    def test_오류_시각_목록을_준다(self) -> None:
        from slack_cli_agent.reliability.health import SocketErrorWatch

        clock = {"now": 100.0}
        watch = SocketErrorWatch(now=lambda: clock["now"])
        watch.emit(logging.LogRecord("x", logging.ERROR, "f", 1, "on_error invoked", None, None))
        assert list(watch.error_timestamps()) == [100.0]

    def test_재접속_시각_목록을_준다(self) -> None:
        from slack_cli_agent.reliability.health import SocketErrorWatch

        clock = {"now": 100.0}
        watch = SocketErrorWatch(now=lambda: clock["now"])
        watch.emit(logging.LogRecord("x", logging.INFO, "f", 1, "A new session has been established", None, None))
        assert list(watch.reconnect_timestamps()) == [100.0]


class TestWatchJobQueue열린건수:
    """상태 기록에 적을 감시 작업 수를 셀 수 있는가.

    끝난 것까지 세면 상태 파일만 보는 쪽이 밀린 일이 있다고 읽는다.
    """

    def test_끝나지_않은_것만_센다(self, watch_queue) -> None:
        first = watch_queue.enqueue("C1", "T1", "조건1")
        watch_queue.enqueue("C1", "T2", "조건2")
        watch_queue.mark_done(first)
        assert watch_queue.open_count() == 1


class Test스스로재기동:
    """소켓이 죽은 채로 살아 있으면 아무 이벤트도 안 들어온다. 스스로 나가고
    감독 프로세스가 다시 띄우게 한다. 원본 `bot.py` 의 `self_restart()`.
    """

    def test_0이_아닌_코드로_나간다(self) -> None:
        """0 으로 나가면 감독 프로세스가 정상 종료로 보고 다시 안 띄운다."""
        from slack_cli_agent.reliability.health import SelfRestarter

        나간코드: list[int] = []
        SelfRestarter(exit_process=나간코드.append)("소켓 오류 9건")
        assert 나간코드 == [1]

    def test_나가기전에_사유를_알린다(self) -> None:
        from slack_cli_agent.reliability.health import SelfRestarter

        알린것: list[str] = []
        나간코드: list[int] = []
        SelfRestarter(notify=알린것.append, exit_process=나간코드.append)("소켓 오류 9건")
        assert 알린것 and "소켓 오류 9건" in 알린것[0]
        assert 나간코드 == [1]

    def test_알림이_실패해도_나간다(self) -> None:
        """알릴 곳이 죽어 있다고 끊긴 프로세스가 그대로 살아 있으면 안 된다."""
        from slack_cli_agent.reliability.health import SelfRestarter

        def boom(text: str) -> None:
            raise RuntimeError("발송 실패")

        나간코드: list[int] = []
        SelfRestarter(notify=boom, exit_process=나간코드.append)("소켓 오류 9건")
        assert 나간코드 == [1]

    def test_처리중인_요청이_끝나기를_기다린다(self) -> None:
        """처리 중인 답을 버리고 나가면 그 요청은 답 없이 사라진다."""
        from slack_cli_agent.reliability.health import SelfRestarter

        남은건수 = [2, 1, 0]
        기다린시간: list[float] = []
        나간코드: list[int] = []
        SelfRestarter(
            inflight_count=lambda: 남은건수.pop(0) if 남은건수 else 0,
            sleep=기다린시간.append,
            exit_process=나간코드.append,
        )("소켓 오류 9건")
        assert 기다린시간 and 나간코드 == [1]

    def test_끝나기를_무한정_기다리지는_않는다(self) -> None:
        from slack_cli_agent.reliability.health import SelfRestarter

        기다린시간: list[float] = []
        나간코드: list[int] = []
        SelfRestarter(
            inflight_count=lambda: 1,
            sleep=기다린시간.append,
            grace_sec=2.0,
            exit_process=나간코드.append,
        )("소켓 오류 9건")
        assert sum(기다린시간) <= 2.0
        assert 나간코드 == [1]


class Test재시도도대표건만남긴다:
    """한 스레드에 놓친 것이 여럿이면 가장 최근 것 하나만 처리한다.

    정상 캐치업은 그렇게 묶는데 재시도 경로만 전부 그대로 돌려줬다. 그러면
    기록 조회가 한 번 실패했다가 복구된 뒤 같은 스레드의 여러 요청에 각각
    답이 올라간다. 2026-08-31 이전에 10개가 밀리면 답이 10개 올라간 것과
    같은 형태다.
    """

    @staticmethod
    def 한_스레드에_여럿인_기록() -> FakeHistoryReader:
        return FakeHistoryReader(
            history={
                "C1": [
                    {"ts": "99000.0", "thread_ts": "99000.0", "user": "U1", "text": "<@U_BOT> 첫 물음"},
                    {"ts": "99001.0", "thread_ts": "99000.0", "user": "U1", "text": "<@U_BOT> 두 번째"},
                    {"ts": "99002.0", "thread_ts": "99000.0", "user": "U1", "text": "<@U_BOT> 세 번째"},
                ]
            },
            threads={
                "99000.0": [
                    {"ts": "99000.0", "user": "U1", "text": "<@U_BOT> 첫 물음"},
                    {"ts": "99001.0", "user": "U1", "text": "<@U_BOT> 두 번째"},
                    {"ts": "99002.0", "user": "U1", "text": "<@U_BOT> 세 번째"},
                ]
            },  # 기본 now 는 100_000.0 — 유예 120초 밖이다
        )

    def test_정상_캐치업은_대표_하나만_남긴다(self) -> None:
        service = make_service(self.한_스레드에_여럿인_기록())
        report = service.sweep(["C1"], window=3600)
        assert [m.ts for m in report.missed] == ["99002.0"]

    def test_재시도도_대표_하나만_남긴다(self) -> None:
        history = FakeHistoryReader(history={"C1": None})
        service = make_service(history)
        # 한 번 조회에 실패해 재시도 목록에 남는다
        assert service.sweep(["C1"], window=3600).unchecked_channels == ["C1"]

        # 그 뒤 조회가 복구된다
        복구된것 = self.한_스레드에_여럿인_기록()
        service._history = 복구된것  # type: ignore[attr-defined]
        # 첫 재시도 대기(30초)를 넘긴다
        service._now = lambda: 100_030.0 + 1  # type: ignore[attr-defined]

        statuses = service.retry_pending()

        assert [s.channel for s in statuses] == ["C1"]
        assert [m.ts for m in statuses[0].missed] == ["99002.0"]

    def test_재시도가_찾은_것도_늦었다는_표식이_붙는다(self) -> None:
        """정상 경로는 붙이는데 재시도만 안 붙이면 늦은 답이 늦었다고 안 밝힌다."""
        history = FakeHistoryReader(history={"C1": None})
        service = make_service(history)
        service.sweep(["C1"], window=3600)
        service._history = self.한_스레드에_여럿인_기록()  # type: ignore[attr-defined]
        service._now = lambda: 100_030.0 + 1  # type: ignore[attr-defined]

        statuses = service.retry_pending()

        assert statuses[0].missed[0].late


class Test캐치업_본문도_실시간과_같게_만든다:
    """캐치업은 ingress 를 안 거쳐 본문을 그대로 큐에 넣었다. 실시간으로 받은
    같은 말과 본문이 달라진다 (코덱스 리뷰 지적, sca-za2a)."""

    def _본문(self, text: str) -> str:
        history = FakeHistoryReader(history={"C1": [{"ts": "99000.0", "user": "U1", "text": text}]})
        outcome = make_service(history).find_missed("C1", window=3600)
        return outcome.value()[0].text

    def test_봇_자신의_멘션을_지운다(self) -> None:
        assert self._본문("<@U_BOT> 질문") == "질문"

    def test_남을_부른_멘션은_남긴다(self) -> None:
        assert self._본문("<@U_BOT> <@U9> 에게 물어봐") == "<@U9> 에게 물어봐"
