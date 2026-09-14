"""되짚기(catch-up) — 소켓이 끊긴 동안 도착한 멘션을 되찾는다.

**되짚기만 유지한다.** 영속 작업 큐가 원본의 나머지 재기동 복구 로직(
`save_restart_dropped`, `save_inflight_dropped`, `replay_restart_dropped`,
`_busy_threads`, `_thread_queue`)을 대체하므로 옮기지 않는다. 소켓이 끊긴
동안 도착한 메시지는 이벤트 자체가 안 와서 큐에 안 들어가고, 그 구간은 슬랙
기록을 다시 읽어야만 회수된다 — 이것이 되짚기를 남기는 유일한 이유다.

`reactions_on`, `already_handled`, `unanswered` 는 원본 함수 본문을 그대로
옮겼다. `CatchupService` 는 이 순수 함수들과 `HistoryReader`(대역 가능),
`ResponseGate`, `NoticeCatalog` 를 조합해 원본 `find_missed`/`catch_up`/
`retry_catchup` 의 본체를 재구성한다.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..config.settings import RuntimeSettings
from ..core.context import RequestContext
from ..core.result import Outcome
from ..observability.notices import NoticeCatalog
from ..slack.gate import ResponseGate
from ..slack.identity import BotIdentity
from ..slack.message_kind import MessageKind
from .ports import HistoryReader

# 이 봇이 답을 내면 다는 표식, 답하지 않기로 하면 다는 표식. 원본 DONE_EMOJI.
SILENT_MARK_EMOJI = "zipper_mouth_face"
DONE_EMOJI: frozenset[str] = frozenset({"white_check_mark", SILENT_MARK_EMOJI})

# 재시도 사이 대기 시간(초). 채널 기록이 계속 빈 목록을 주는 문제는 대개
# 잠깐이라, 짧은 간격부터 시작해 점점 늘린다. 원본 CATCHUP_RETRY_BACKOFF.
CATCHUP_RETRY_BACKOFF: tuple[int, ...] = (30, 60, 120, 300, 600)
# 이보다 오래 못 보면 사람이 알아야 한다. 원본 CATCHUP_ALERT_AFTER_SEC.
CATCHUP_ALERT_AFTER_SEC: float = 1800
# 채널 하나에서 한 되짚기 회차에 살펴볼 스레드 상한. 원본 CATCHUP_MAX_PER_CHANNEL,
# CATCHUP_MAX_THREADS_PER_CHANNEL 중 큰 쪽을 그대로 쓴다.
CATCHUP_MAX_THREADS_PER_CHANNEL = 60


def reactions_on(msg: Mapping[str, Any]) -> set[str]:
    """그 메시지에 달린 이모지 이름들."""
    return {r.get("name") for r in (msg.get("reactions") or []) if r.get("name")}


def already_handled(msg: Mapping[str, Any]) -> bool:
    """이 메시지는 판단이 끝났는가.

    이 봇은 답을 내면 white_check_mark 를, 답하지 않기로 하면
    zipper_mouth_face 를 그 메시지에 단다. 그 표식이 있으면 다시 집지 않는다.

    eyes 나 hourglass 는 도중에 프로세스가 죽어도 그대로 남는다. 끝났다는
    뜻이 아니므로 되짚기 대상으로 남긴다. x 도 실패한 것이라 다시 시도할
    대상이다.

    사람이 직접 white_check_mark 를 달아도 끝난 것으로 본다. 손으로 넘길
    수단이 있어야 이 봇이 붙들고 있는 것을 놓게 할 수 있다.
    """
    return bool(reactions_on(msg) & DONE_EMOJI)


def unanswered(
    thread: list[Mapping[str, Any]],
    asked: list[Mapping[str, Any]],
    *,
    is_self: Callable[[Mapping[str, Any]], bool],
    is_notice: Callable[[str | None], bool],
) -> list[Mapping[str, Any]]:
    """답을 받지 못한 말만 고른다.

    "뒤에 봇 답글이 하나라도 있으면 처리됐다" 로 세면 앞 요청이 뒤 요청의
    답에 묻힌다. 13시에 물은 것과 13시 11분에 물은 것이 있고 13시 15분에
    한 번만 답했으면, 앞의 것은 답을 못 받았는데도 처리된 것으로 세어진다.

    이 봇은 스레드마다 한 번에 하나씩 처리하고 끝나면 다음 것을 꺼낸다.
    답 하나는 요청 하나에 대응한다. 그러니 들어온 순서대로 짝을 지어 센다.
    긴 답이 여러 조각으로 나뉘어 올라간 경우는 잇따른 봇 글을 하나로 본다.

    `is_self`, `is_notice` 는 원본에서 각각 봇 신원 판정(`is_self`)과 상태
    안내문 판정(`NoticeCatalog.is_notice`)이 하던 일이다. 이 계층에는 봇
    신원(BOT_ID)이 없어 호출부가 주입한다.

    thread : 시간순 메시지 목록
    asked  : 그 스레드에서 답을 기다리는 말 목록
    """
    waiting = {m.get("ts") for m in asked}
    pending: list[Any] = []
    answered: set[Any] = set()
    prev_bot = False
    for x in sorted(thread, key=lambda m: float(m.get("ts") or 0)):
        # 답으로 세는 것은 이 봇 자신의 말뿐이다. 다른 봇의 말은 답이 아니다
        if is_self(x):
            if is_notice(x.get("text")):
                # 안내문은 답이 아니다. 앞 요청을 지우지 않는다
                continue
            if not prev_bot and pending:
                answered.add(pending.pop(0))
            prev_bot = True
            continue
        if x.get("bot_id"):
            # 다른 봇의 말은 사람의 물음도 이 봇의 답도 아니다. 흐름을 끊지 않는다
            continue
        prev_bot = False
        if x.get("ts") in waiting:
            pending.append(x.get("ts"))
    return [m for m in asked if m.get("ts") not in answered]


@dataclass(frozen=True)
class CatchupReport:
    """`sweep` 한 회차의 결과."""

    missed: list[RequestContext]
    """대표 건. 큐에 넣을 대상이다. 원본은 이것을 handle_request 로 바로
    처리했지만, 여기서는 실행하지 않고 값만 돌려준다 — 큐에 실제로 넣는
    일은 이 서비스의 의존이 아닌 JobQueue 를 쥔 호출부(Worker) 몫이다."""
    skipped: list[RequestContext]
    """같은 스레드에서 대표건에 묻힌 나머지. 원본은 대표건과 같은 표식만
    남기고 따로 답하지 않았다 — 그 표식 동기화도 호출부 몫이다."""
    unchecked_channels: list[str]
    """조회 실패(판정 불가)로 이번 회차에 보지 못한 채널."""


@dataclass(frozen=True)
class RetryStatus:
    """`retry_pending` 이 채널 하나에 대해 돌려주는 상태."""

    channel: str
    stuck_sec: float
    alert: bool
    missed: tuple[RequestContext, ...] = ()


class CatchupService:
    def __init__(
        self,
        *,
        history: HistoryReader,
        gate: ResponseGate,
        notices: NoticeCatalog,
        settings: RuntimeSettings,
        identity: BotIdentity,
        message_text: Callable[[Mapping[str, Any]], str] = lambda m: m.get("text") or "",
        now: Callable[[], float] = time.time,
        started_at: float | None = None,
    ) -> None:
        self._history = history
        self._gate = gate
        self._notices = notices
        self._settings = settings
        # 이 봇의 신원. 자기 말 판정과 부름 판정의 근거가 하나다.
        self._identity = identity
        self._message_text = message_text
        self._now = now
        self._started_at = started_at if started_at is not None else now()
        # 원본 _catchup_pending. channel -> (최초 실패 시각, 시도 횟수)
        self._pending: dict[str, tuple[float, int]] = {}
        # 메시지를 무엇으로 볼지는 한 곳에서 판정한다. 여기 따로 적으면 기준이 갈린다.
        self._kind = MessageKind()

    def find_missed(self, channel: str, window: float) -> Outcome[list[RequestContext]]:
        """놓친 멘션을 찾는다.

        봇을 부른 메시지 중 그 뒤에 봇 답글이 없는 것을 고른다. 스레드
        답글로 부른 경우도 잡아야 한다.
        """
        if not self._identity.known:
            # 부름 판정의 근거가 없다. 여기서 빈 목록을 돌려주면 "부른 말이
            # 없다" 와 구분되지 않고, 그 회차가 재시도 목록에서도 빠져 신원이
            # 복구돼도 그 구간을 회수하지 못한다.
            return Outcome.unknown("봇 신원을 몰라 부름 판정 불가")

        now = self._now()
        oldest = now - window
        # 스레드는 부모가 오래됐어도 답글이 방금 달릴 수 있다. 찾는 범위를 넓게 잡는다.
        discover = now - max(window, self._settings.catchup_thread_lookback_sec)

        hist = self._history.read_history(channel, discover, CATCHUP_MAX_THREADS_PER_CHANNEL)
        if hist is None:
            # 빈 결과가 사실인지 슬랙 쪽 착오인지 가릴 수 없다.
            # 없다고 단정하지 않고 확인 실패로 올린다.
            return Outcome.unknown("기록을 여러 번 읽어도 비어 판정 불가")

        candidates: list[tuple[Mapping[str, Any], str]] = []  # (부른 메시지, 그 스레드)

        for msg in hist:
            ts = str(msg.get("ts") or "")
            thread_ts = str(msg.get("thread_ts") or ts)
            has_thread = bool(msg.get("thread_ts")) or msg.get("reply_count")

            if has_thread:
                # 마지막 답글까지 되짚기 창보다 이르면 펼쳐 볼 것이 없다.
                latest = float(msg.get("latest_reply") or ts or 0)
                if latest < oldest:
                    continue
                thread = self._history.read_thread(channel, thread_ts, 50)
                if not thread:
                    continue
            else:
                # 스레드가 없는 최상위 메시지는 자기 시각으로 판단한다
                if float(ts or 0) < oldest:
                    continue
                thread = [msg]

            asked: list[Mapping[str, Any]] = []
            bot_in_thread = any(self._identity.is_self(x) for x in thread)
            # 각 사람 말 직전에 이 봇이 되물었는지 훑어둔다
            asked_before: dict[Any, bool] = {}
            pending_ask = False
            for x in thread:
                if x.get("bot_id"):
                    if self._identity.is_self(x):
                        pending_ask = self._gate.asked_back(self._message_text(x))
                else:
                    asked_before[x.get("ts")] = pending_ask
            for m in thread:
                if not self._kind.is_human(m):
                    continue
                text = m.get("text") or ""
                # 이 봇을 부른 말이거나, 이 봇이 낀 스레드에서 답을 기다리는 말이다
                called = self._identity.is_mentioned(text)
                bot_asked = asked_before.get(m.get("ts"), False)
                if not called and not (
                    bot_in_thread and self._gate.worth_answering(text, bot_asked)
                ):
                    continue
                if float(m.get("ts", 0)) < oldest:
                    continue
                # 방금 들어온 말은 아직 처리 중일 수 있다. 손대지 않는다.
                # 이 프로세스가 뜨기 전에 올라온 말에는 유예를 적용하지 않는다.
                mts = float(m.get("ts", 0))
                if mts >= self._started_at and mts > now - self._settings.catchup_grace_sec:
                    continue
                # 이미 판단이 끝난 표식이 달려 있으면 다시 집지 않는다
                if already_handled(m):
                    continue
                asked.append(m)

            for m in unanswered(
                thread, asked, is_self=self._identity.is_self, is_notice=self._notices.is_notice
            ):
                candidates.append((m, thread_ts))

        missed: list[RequestContext] = []
        seen: set[tuple[str, Any]] = set()
        for m, thread_ts in candidates:
            key = (channel, m.get("ts"))
            if key in seen:
                continue
            seen.add(key)
            missed.append(
                RequestContext(
                    channel=channel,
                    user=m.get("user") or "",
                    ts=str(m.get("ts")),
                    thread_ts=str(thread_ts),
                    text=m.get("text") or "",
                )
            )
        missed.sort(key=lambda c: float(c.ts))
        return Outcome.found(missed)

    def sweep(self, channels: list[str], window: float) -> CatchupReport:
        """등록된 채널에서 놓친 요청을 찾는다.

        한 스레드에 놓친 게 여럿이면 가장 최근 것만 대표로 남긴다. 지난
        대화 복원이 나머지를 함께 담으므로 그 하나의 답이 전체를 종합한
        답이 된다. 2026-08-31 이전에는 건마다 따로 답해 10개가 밀리면 답이
        10개 올라갔다.
        """
        missed: list[RequestContext] = []
        skipped: list[RequestContext] = []
        unchecked: list[str] = []

        for channel in channels:
            outcome = self.find_missed(channel, window)
            if outcome.is_unknown:
                unchecked.append(channel)
                continue

            대표, 나머지 = self._pick_representatives(outcome.value())
            missed.extend(대표)
            skipped.extend(나머지)

        if unchecked:
            # 알리고 끝내지 않는다. 볼 때까지 계속 다시 본다.
            self._queue_retry(unchecked)
        else:
            self._pending.clear()

        return CatchupReport(missed=missed, skipped=skipped, unchecked_channels=unchecked)

    def _pick_representatives(
        self, found: list[RequestContext]
    ) -> tuple[list[RequestContext], list[RequestContext]]:
        """스레드마다 가장 최근 것 하나만 대표로 남기고 나머지를 돌려준다.

        지난 대화 복원이 나머지를 함께 담으므로 그 하나의 답이 전체를 종합한
        답이 된다. 건마다 따로 답하면 10개가 밀렸을 때 답이 10개 올라간다.

        답하겠다고 미리 말하지 않는다. 늦었다는 사실은 실제로 답할 때 그 답
        앞에 붙인다. 2026-08-25 에 약속만 두 번 올리고 두 번 다 침묵했다.
        """
        groups: dict[str, list[RequestContext]] = {}
        for ctx in found:
            groups.setdefault(ctx.thread_ts, []).append(ctx)

        대표: list[RequestContext] = []
        나머지: list[RequestContext] = []
        for items in groups.values():
            items.sort(key=lambda c: float(c.ts))
            대표.append(items[-1].marked_late())
            나머지.extend(items[:-1])
        return 대표, 나머지

    def retry_pending(self) -> list[RetryStatus]:
        """마치지 못한 되짚기를 다시 본다. 건강 점검이 돌 때마다 부른다.

        슬랙이 기록을 빈 목록으로 주는 것은 대개 잠깐이다. 한 번 실패했다고
        그 시간대의 요청을 영영 놓치면 안 된다.
        """
        now = self._now()
        due = [
            (ch, first, tries)
            for ch, (first, tries) in self._pending.items()
            if now - first >= CATCHUP_RETRY_BACKOFF[min(tries - 1, len(CATCHUP_RETRY_BACKOFF) - 1)]
        ]

        statuses: list[RetryStatus] = []
        for ch, first, _tries in due:
            outcome = self.find_missed(ch, self._settings.catchup_window_sec)
            if outcome.is_unknown:
                stuck = now - first
                if stuck >= CATCHUP_ALERT_AFTER_SEC:
                    # 오래 못 보면 그때는 사람이 알아야 한다. 알림 자체는
                    # 호출부 몫이라 여기서는 alert=True 로만 표시한다.
                    self._pending.pop(ch, None)
                else:
                    self._queue_retry([ch])
                statuses.append(
                    RetryStatus(channel=ch, stuck_sec=stuck, alert=stuck >= CATCHUP_ALERT_AFTER_SEC)
                )
                continue

            self._pending.pop(ch, None)
            # 정상 되짚기와 같은 묶음을 쓴다. 여기서만 전부 돌려주면 같은
            # 스레드의 여러 요청에 각각 답이 올라간다.
            대표, _나머지 = self._pick_representatives(outcome.value())
            if 대표:
                statuses.append(
                    RetryStatus(channel=ch, stuck_sec=0.0, alert=False, missed=tuple(대표))
                )

        return statuses

    def _queue_retry(self, channels: list[str]) -> None:
        now = self._now()
        for ch in channels:
            first, tries = self._pending.get(ch, (now, 0))
            self._pending[ch] = (first, tries + 1)
