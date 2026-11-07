"""Answers a non-owner's DM with an isolated read-only turn (E1-E5).

mametchi refused every non-owner DM in the main path and answered it in a
separate `claude -p` with only lookup tools and no session (bot.py:7220
_dm_guide_reply, 7472 notify_dm_redirect). This keeps that boundary: the
turn here never joins the request queue, never resumes a session, and its
tools come from the read-only selection the caller builds (A10's lookup
bundle, no browser action, no bare write tool).

Per DM, in order: the daily cap (E2), the turn, the fixed notice when the
turn fails or gives nothing (E3), the post, an audit row (E7), the owner's
full copy (E5), then the probe count and warning (E4).
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from ..core.timezones import KST
from ..observability.audit import IncidentKind
from ..observability.notices import NoticeCatalog, NoticeKey
from .limits import DailyLimit
from .probe import DailyCounter, is_probe

log = logging.getLogger(__name__)

#: Longest DM sent to the turn, as in mametchi (text[:4000]).
MAX_QUESTION_CHARS = 4000
#: Longest text kept per audit field, as in mametchi (question[:2000]).
MAX_AUDIT_CHARS = 2000
#: Lines an organisation's instructions append to every answer (mametchi
#: dropped its Vooster line the same way, bot.py:7253).
_NOISE_MARKS: tuple[str, ...] = ("Vooster",)
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


class TurnResult(Protocol):
    # Read-only members, so a frozen EngineResponse matches.
    @property
    def ok(self) -> bool: ...
    @property
    def body(self) -> str: ...
    @property
    def failure_reason(self) -> str | None: ...


@dataclass(frozen=True)
class DmAnswer:
    reply: str | None
    kind: str | None = None
    summary: str | None = None
    restriction: str | None = None


def parse_answer(result: TurnResult | None, error: str | None = None) -> DmAnswer:
    """The model's JSON, or its plain text as the reply when it gave none.
    A failed or empty turn has no reply and says why in `restriction`."""
    if result is None:
        return DmAnswer(reply=None, restriction=error or "DM 답 생성 실패")
    if not result.ok:
        return DmAnswer(reply=None, restriction=f"DM 답 생성 실패 : {result.failure_reason or '알 수 없음'}")
    text = "\n".join(
        line for line in (result.body or "").splitlines()
        if not any(mark in line for mark in _NOISE_MARKS)
    ).strip()
    if not text:
        return DmAnswer(reply=None, restriction="빈 답")
    match = _JSON_RE.search(text)
    if match:
        try:
            data = json.loads(match.group(0))
        except ValueError:
            data = None
        if isinstance(data, dict) and str(data.get("reply") or "").strip():
            restriction = str(data.get("restriction") or "").strip() or None
            if data.get("restricted") and not restriction:
                restriction = "제약이 있었다고 표시됐으나 사유가 비어 있다"
            return DmAnswer(
                reply=str(data["reply"]).strip(),
                kind=str(data.get("kind") or "").strip() or None,
                summary=str(data.get("summary") or "").strip() or None,
                restriction=restriction,
            )
    return DmAnswer(reply=text)


def _quote(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return "> (빈 본문)"
    return "\n".join("> " + line if line.strip() else ">" for line in text.splitlines())


class DmReplyService:
    def __init__(
        self,
        *,
        # (prompt) -> the turn's result. Runs the engine with read-only tools.
        run_turn: Callable[[str], TurnResult],
        # (channel, text): posts to the DM itself.
        post: Callable[[str, str], object],
        audit: Callable[..., None],
        notices: NoticeCatalog,
        daily_limit: int,
        # The owner's DM. None sends no digest or warning.
        notify_owner: Callable[[str], object] | None = None,
        # Wraps the engine call in the shared concurrency slot (A16).
        slot: Callable[[], AbstractContextManager[None]] = nullcontext,
        # (user) -> the <recent_channels> block for that person, or "".
        context: Callable[[str], str] = lambda user: "",
        probe_warn_at: Sequence[int] = (2, 5),
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._run_turn = run_turn
        self._post = post
        self._audit = audit
        self._notices = notices
        self._limit = DailyLimit(daily_limit)
        self._notify_owner = notify_owner
        self._slot = slot
        self._context = context
        self._probe_warn_at = frozenset(probe_warn_at)
        self._probes = DailyCounter()
        self._clock = clock

    def handle(self, channel: str, user: str, text: str) -> None:
        day = datetime.fromtimestamp(self._clock(), KST).strftime("%Y-%m-%d")
        question = (text or "")[:MAX_AUDIT_CHARS]
        probe = is_probe(text)
        verdict = self._limit.take(user, day)
        if not verdict.allowed:
            log.info("DM 답 하루 상한을 넘었다 : %s (%d번째)", user, verdict.count)
            self._record(IncidentKind.DM_REPLY_CAPPED, channel, user=user, question=question,
                         count_today=verdict.count, notified=verdict.notify)
            if verdict.notify:
                self._safe_post(channel, user, self._notices.render(NoticeKey.DM_CAPPED), question)
        else:
            answer = self._answer(user, text or "")
            fallback = answer.reply is None
            reply = answer.reply or self._notices.render(NoticeKey.DM_FALLBACK)
            if self._safe_post(channel, user, reply, question):
                self._record(
                    IncidentKind.DM_REPLY, channel, user=user, dm_kind=answer.kind,
                    fallback=fallback, probe=probe, question=question,
                    reply=reply[:MAX_AUDIT_CHARS], restriction=answer.restriction,
                    count_today=verdict.count,
                )
                self._digest(user, answer, text or "", reply, fallback)
        if probe:
            self._record_probe(channel, user, text or "", day)

    def _answer(self, user: str, text: str) -> DmAnswer:
        prompt = self._context(user) + "<dm>\n" + text[:MAX_QUESTION_CHARS] + "\n</dm>"
        try:
            with self._slot():
                result = self._run_turn(prompt)
        except Exception as exc:  # noqa: BLE001 - any failure becomes the fixed notice (E3)
            log.warning("DM 답을 만들지 못했다 : %s : %s", user, exc)
            return parse_answer(None, f"DM 답 생성 실패 : {type(exc).__name__} {str(exc)[:120]}")
        return parse_answer(result)

    def _safe_post(self, channel: str, user: str, text: str, question: str) -> bool:
        try:
            self._post(channel, text)
        except Exception as exc:  # noqa: BLE001 - recorded; the next DM still runs
            log.warning("DM 답을 보내지 못했다 : %s : %s", channel, exc)
            self._record(IncidentKind.DM_REPLY_FAILED, channel, user=user, question=question,
                         error=str(exc)[:300])
            return False
        return True

    def _digest(self, user: str, answer: DmAnswer, question: str, reply: str, fallback: bool) -> None:
        """The full text both ways, not a summary (mametchi 2026-11-03: a
        summary rewrote the original and hid what was actually said)."""
        if self._notify_owner is None:
            return
        body = self._notices.render(
            NoticeKey.DM_OWNER_DIGEST,
            kind=answer.kind or "미분류",
            user=user,
            restriction=f"\n- 제약 : {answer.restriction}" if answer.restriction else "",
            question=_quote(question),
            fallback=" (고정 안내문)" if fallback else "",
            reply=_quote(reply),
        )
        self._notify(body)

    def _record_probe(self, channel: str, user: str, text: str, day: str) -> None:
        count = self._probes.add(user, day)
        self._record(IncidentKind.DM_PROBE, channel, user=user, count_today=count,
                     text=text[:1000])
        if count in self._probe_warn_at and self._notify_owner is not None:
            self._notify(self._notices.render(
                NoticeKey.DM_PROBE_WARN, user=user, count=count, text=_quote(text[:300]),
            ))

    def _notify(self, text: str) -> None:
        assert self._notify_owner is not None
        try:
            self._notify_owner(text)
        except Exception as exc:  # noqa: BLE001 - the DM reply already went out
            log.warning("소유자에게 DM 알림을 보내지 못했다 : %s", exc)

    def _record(self, kind: IncidentKind, channel: str, **fields: Any) -> None:
        try:
            self._audit(kind, channel=channel, **fields)
        except Exception as exc:  # noqa: BLE001 - a lost audit row must not lose the reply
            log.warning("DM 감사 기록을 남기지 못했다 : %s : %s", kind, exc)
