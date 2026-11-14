"""Shadow evaluation (G4, bot.py shadow_loop).

A shadow channel names a source channel in its `shadow_source` key. Each new
top-level question in the source is posted to the shadow channel as a link,
answered there without looking at the source thread's real answer, and, with
`shadow_judge` on, judged against that answer the next day. Nothing is
written to the source channel.

The bot is not invited to the source: joining would show its entry to that
channel's people. The source is read with the owner's user session instead,
and with no user token configured the loop says so rather than skipping
quietly.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from ..core.markers import take_recipient_tag
from ..core.timezones import KST
from ..observability.audit import IncidentKind
from ..plugin.base import BotPlugin
from ..plugin.hooks import BackgroundJob, EngineSlot, SlotUnavailable
from ..slack.transcript import message_text
from .prompts import answer_prompt, judge_prompt
from .store import ShadowStore

log = logging.getLogger(__name__)

JUDGE_HOUR = 10  # judge from this hour KST on the day after the question
JUDGE_GIVEUP_DAYS = 3  # close as "판정 불가" when no answer came by then
MAX_ATTEMPTS = 3
FAILED_NOTICE = "답을 만들지 못했어요. 실행이 실패했어요."
_TOOL_CALL_TYPES = frozenset({"tool_use", "function_call", "custom_tool_call"})


@dataclass(frozen=True)
class TurnResult:
    ok: bool
    body: str
    # Every tool call of the turn as (name, input). None when the engine left
    # no readable record, so the blind check could not run at all.
    tool_calls: tuple[tuple[str, str], ...] | None


def is_question(msg: Mapping[str, Any]) -> bool:
    """A person's top-level post. Joins, leaves and bot posts are not."""
    if msg.get("bot_id") or msg.get("subtype") not in (None, "file_share"):
        return False
    if msg.get("thread_ts") and msg.get("thread_ts") != msg.get("ts"):
        return False
    return bool(str(msg.get("text") or "").strip() or msg.get("files"))


def _day(ts: str) -> str:
    return datetime.fromtimestamp(float(ts), KST).strftime("%Y-%m-%d")


def blind_violations(calls: Iterable[tuple[str, str]], source: str, ts: str) -> list[str]:
    """Tool calls that broke the blind rules. The prompt alone cannot stop
    them, so they are found afterwards and printed under the answer."""
    day, compact = _day(ts), ts.replace(".", "")
    bad: set[str] = set()
    for name, raw in calls:
        if source in raw or ts in raw or compact in raw:
            bad.add(f"{name} 가 원본 채널이나 원본 글을 열었다")
        elif "slack" in name.lower() and "search" in name.lower():
            dates = re.findall(r"before:(\d{4}-\d{2}-\d{2})", raw)
            if not dates or any(d > day for d in dates):
                bad.add("슬랙 검색에 질문일 이하 before: 가 없다")
    return sorted(bad)


def tool_calls_in(path: Path | None) -> tuple[tuple[str, str], ...] | None:
    """Tool calls in a Claude (tool_use) or Codex (function_call,
    custom_tool_call) jsonl transcript. None when there is no file to read."""
    if path is None or path.suffix != ".jsonl" or not path.is_file():
        return None
    calls: list[tuple[str, str]] = []

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            kind = node.get("type")
            if isinstance(kind, str) and kind in _TOOL_CALL_TYPES and node.get("name"):
                name = f"{node['namespace']}.{node['name']}" if node.get("namespace") else str(node["name"])
                raw = node.get("input", node.get("arguments"))
                calls.append((name, raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)))
                return
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            walk(json.loads(line))
        except ValueError:
            continue
    return tuple(calls)


class ShadowService:
    def __init__(
        self,
        *,
        store: ShadowStore,
        source_api: Callable[..., Mapping[str, Any]] | None,
        post: Callable[[str, str | None, str], str | None],
        run_turn: Callable[[str, str, str], TurnResult],
        pairs: Callable[[], Mapping[str, str]],
        judge_enabled: Callable[[str], bool],
        name_of: Callable[[str], str],
        audit: Callable[..., None],
        requests_path: Path,
        bot_name: str,
        is_self: Callable[[Mapping[str, Any]], bool] = lambda m: False,
        max_parallel: int = 4,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._api = source_api
        self._post = post
        self._run_turn = run_turn
        self._pairs = pairs
        self._judge_enabled = judge_enabled
        self._name_of = name_of
        self._audit = audit
        self._requests_path = requests_path
        self._bot = bot_name
        self._is_self = is_self
        self._max_parallel = max(1, max_parallel)
        self._now = now

    def tick(self, engine: EngineSlot) -> None:
        pairs = dict(self._pairs())
        if not pairs:
            return
        if self._api is None:
            log.error("그림자 채널 %d개가 있으나 사용자 세션 토큰이 없어 원본 채널을 읽지 못한다", len(pairs))
            return
        requests = self._take_requests()
        for req in requests:
            if req.get("target") in pairs:
                self._guarded(self._poll, req["target"], pairs[req["target"]], engine, req["oldest"])
        if requests:
            self._requests_path.unlink(missing_ok=True)
        self._retry_stuck(pairs, engine)
        for target, source in pairs.items():
            self._guarded(self._poll, target, source, engine, None)
        due = [
            it for it in self._store.items()
            if it.get("target") in pairs and self.is_due(it) and self._judge_enabled(it["target"])
        ]
        for item in due:
            self._guarded(self._judge, item, engine)

    @staticmethod
    def _guarded(fn: Callable[..., object], *args: Any) -> None:
        # One pair failing (a source the session cannot read) must not stop
        # the others in the same round.
        try:
            fn(*args)
        except SlotUnavailable:
            raise
        except Exception:
            log.exception("그림자 평가 실패 : %s", fn.__name__)

    def _poll(self, target: str, source: str, engine: EngineSlot, oldest: str | None) -> int:
        """New top-level posts, oldest first. A source seen for the first time
        starts from now; only a backfill request reaches into the past."""
        last = self._store.seen(source)
        if oldest is None and last is None:
            self._store.mark_seen(source, f"{self._now():.6f}")
            return 0
        msgs: list[Mapping[str, Any]] = []
        cursor = None
        while True:
            params: dict[str, Any] = {"channel": source, "oldest": oldest or last, "limit": 200}
            if cursor:
                params["cursor"] = cursor
            resp = self._api("conversations.history", **params)  # type: ignore[misc]
            msgs += resp.get("messages") or []
            cursor = (resp.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        questions = sorted((m for m in msgs if is_question(m)), key=lambda m: float(m["ts"]))
        # Links go up in question order first; the answers then run together,
        # each waiting for a shared slot (bot.py shadow_poll).
        jobs = [(m, item) for m in questions if (item := self._prepare(target, source, m)) is not None]
        if jobs:
            with ThreadPoolExecutor(max_workers=min(len(jobs), self._max_parallel)) as pool:
                list(pool.map(lambda job: self._guarded(self._answer, *job, engine), jobs))
        if oldest is None and questions:
            self._store.mark_seen(source, str(questions[-1]["ts"]))
        return len(jobs)

    def _prepare(self, target: str, source: str, msg: Mapping[str, Any]) -> dict[str, Any] | None:
        ts = str(msg["ts"])
        prev = self._store.item(source, ts)
        if prev.get("status") in ("answered", "judged"):
            return None
        if prev and int(prev.get("attempts", 0)) >= MAX_ATTEMPTS:
            return None
        # Counted here, before the link and the turn, so a link that cannot be
        # posted, a turn that raises or a process killed mid-turn still reaches
        # MAX_ATTEMPTS (bot.py shadow_prepare).
        item = self._store.update(source, ts, target=target, status="posting",
                                  attempts=int(prev.get("attempts", 0)) + 1,
                                  created_at=prev.get("created_at", self._now()))
        if not item.get("root_ts"):
            # A link already posted is reused on a retry, so no orphan link is left.
            try:
                link = str(self._api("chat.getPermalink", channel=source, message_ts=ts)["permalink"])  # type: ignore[misc]
            except Exception:  # noqa: BLE001 - any lookup failure falls back to a built link
                link = f"https://slack.com/archives/{source}/p{ts.replace('.', '')}"
            asked_at = datetime.fromtimestamp(float(ts), KST)
            root = self._post(target, None, f"[질문 원문 {asked_at:%m/%d %H:%M}]({link})")
            if not root:
                return None
            item = self._store.update(source, ts, root_ts=root)
        return item

    def _answer(self, msg: Mapping[str, Any], item: dict[str, Any], engine: EngineSlot) -> None:
        source, ts, target = item["source"], item["ts"], item["target"]
        try:
            source_name = str(self._api("conversations.info", channel=source)["channel"]["name"])  # type: ignore[misc]
        except Exception:  # noqa: BLE001 - the id stands in for the name on any failure
            source_name = source
        question = message_text(msg)
        prompt = answer_prompt(
            bot=self._bot, source_id=source, source_name=source_name, question=question,
            author=self._name_of(str(msg["user"])) if msg.get("user") else "확인 불가",
            asked_at=datetime.fromtimestamp(float(ts), KST), files=msg.get("files") or (),
        )
        attempts = int(item.get("attempts", 0))
        try:
            with engine():
                try:
                    result = self._run_turn(prompt, target, f"shadow-{source}-{ts}")
                except SlotUnavailable:
                    raise
                except Exception:  # a crashed turn counts as a failed attempt
                    log.exception("그림자 답 턴 실패 : %s %s", source, ts)
                    result = TurnResult(ok=False, body="", tool_calls=None)
        except SlotUnavailable:
            # No turn ran: give back the attempt _prepare counted, so a busy or
            # stopping worker cannot spend all three and leave the item stuck
            # at "posting" with no answer and no notice. The link stays.
            self._store.update(source, ts, attempts=max(attempts - 1, 0))
            raise
        answer, _ = take_recipient_tag(result.body or "")
        ok = result.ok and bool(answer.strip())
        violations = blind_violations(result.tool_calls or (), source, ts) if ok else []
        if ok:
            body = answer
            if result.tool_calls is None:
                body += "\n\n> 블라인드 위반 검사 : 실행 기록을 읽지 못해 검사하지 못했다"
            elif violations:
                body += "\n\n> 블라인드 위반 : " + "; ".join(violations)
            self._post(target, item["root_ts"], body)
        elif attempts >= MAX_ATTEMPTS:
            # Earlier failures stay quiet; the next round retries them.
            self._post(target, item["root_ts"], FAILED_NOTICE)
        self._store.update(
            source, ts, question=question[:2000], answer=answer[:4000], attempts=attempts,
            status="answered" if ok else "failed", blind_violations=violations,
            blind_checked=result.tool_calls is not None, answered_at=self._now(),
        )
        self._audit(IncidentKind.SHADOW_ANSWERED.value, channel=target, source=source, source_ts=ts,
                    ok=ok, blind_violations=len(violations))

    def _retry_stuck(self, pairs: Mapping[str, str], engine: EngineSlot) -> None:
        for item in self._store.items():
            if (item.get("target") not in pairs or item.get("status") not in ("posting", "failed")
                    or int(item.get("attempts", 0)) >= MAX_ATTEMPTS):
                continue
            # Per item: a source the session cannot read any more must not
            # stop the polls and verdicts after it in the same round.
            self._guarded(self._retry_one, item, engine)

    def _retry_one(self, item: Mapping[str, Any], engine: EngineSlot) -> None:
        resp = self._api("conversations.history", channel=item["source"],  # type: ignore[misc]
                         latest=item["ts"], inclusive="true", limit=1)
        msg = (resp.get("messages") or [None])[0]
        if msg and msg.get("ts") == item["ts"]:
            prepared = self._prepare(item["target"], item["source"], msg)
            if prepared is not None:
                self._guarded(self._answer, msg, prepared, engine)

    def is_due(self, item: Mapping[str, Any]) -> bool:
        now = datetime.fromtimestamp(self._now(), KST)
        if item.get("status") != "answered" or item.get("judge_tried_on") == now.strftime("%Y-%m-%d"):
            return False
        asked = datetime.fromtimestamp(float(item["ts"]), KST).date()
        return now.date() > asked and now.hour >= JUDGE_HOUR

    def _judge(self, item: dict[str, Any], engine: EngineSlot) -> None:
        source, ts, target = item["source"], item["ts"], item["target"]
        msgs = self._api("conversations.replies", channel=source, ts=ts, limit=100).get("messages") or []  # type: ignore[misc]
        replies = [m for m in msgs if m.get("ts") != ts and not self._is_self(m)]
        asker = next((m.get("user") for m in msgs if m.get("ts") == ts), None)
        final = (self._now() - float(ts)) / 86400 >= JUDGE_GIVEUP_DAYS
        today = datetime.fromtimestamp(self._now(), KST).strftime("%Y-%m-%d")
        # Only the asker's own follow-ups is not an answer yet; look again
        # tomorrow until the deadline.
        if not any(m.get("user") != asker for m in replies):
            if not final:
                self._store.update(source, ts, judge_tried_on=today)
                return
            replies = []
        if not replies:
            verdict = "판정 불가"
            self._post(target, item.get("root_ts"),
                       f"판정 : 판정 불가\n- 원본 스레드에 {JUDGE_GIVEUP_DAYS}일이 지나도 답이 없어요.")
        else:
            lines = [
                f"[{datetime.fromtimestamp(float(m['ts']), KST):%m/%d %H:%M} "
                f"{self._name_of(str(m['user'])) if m.get('user') else m.get('username') or '봇'}] "
                f"{message_text(m)}"
                for m in replies
            ]
            prompt = judge_prompt(bot=self._bot, question=str(item.get("question", "")),
                                  answer=str(item.get("answer", "")), replies="\n".join(lines)[:12000])
            with engine():
                result = self._run_turn(prompt, target, f"shadow-judge-{source}-{ts}")
            body, _ = take_recipient_tag(result.body or "")
            if not result.ok or not body.strip():
                log.warning("그림자 판정을 만들지 못했다 : %s %s", source, ts)
                return
            m = re.search(r"판정\s*:\s*([^\n]+)", body)
            verdict = m.group(1).strip() if m else "형식 불명"
            if verdict.startswith("판정 불가") and not final:
                self._store.update(source, ts, judge_tried_on=today)
                return
            self._post(target, item.get("root_ts"), body)
        self._store.update(source, ts, status="judged", verdict=verdict, judged_at=self._now())
        self._audit(IncidentKind.SHADOW_JUDGED.value, channel=target, source=source, source_ts=ts, verdict=verdict)

    def _take_requests(self) -> list[dict[str, Any]]:
        """Backfill requests, `[{"target", "days"}]` in a file. `oldest` is
        fixed on first read so a restart does not move the start; the file is
        removed only after the backfill ran."""
        if not self._requests_path.exists():
            return []
        try:
            reqs = json.loads(self._requests_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.exception("그림자 소급 요청 파일을 읽지 못했다")
            return []
        reqs = [r for r in reqs if isinstance(r, dict)] if isinstance(reqs, list) else []
        if any("oldest" not in r for r in reqs):
            for r in reqs:
                r.setdefault("oldest", f"{self._now() - float(r.get('days', 7)) * 86400:.6f}")
            self._requests_path.write_text(json.dumps(reqs), encoding="utf-8")
        return reqs


def shadow_pairs(channels: Mapping[str, Any]) -> dict[str, str]:
    """Shadow channel -> source channel, from each channel's `extra`."""
    pairs: dict[str, str] = {}
    for cid, config in channels.items():
        source = (getattr(config, "extra", None) or {}).get("shadow_source")
        if isinstance(source, str) and source:
            pairs[cid] = source
    return pairs


class ShadowPlugin(BotPlugin):
    """Runs the service as a worker background job, so its engine turns
    share the request slots (G1)."""

    name: ClassVar[str] = "shadow"

    def __init__(self, service: ShadowService, interval_sec: float = 300.0) -> None:
        self._service = service
        self._interval_sec = interval_sec

    def background_jobs(self) -> Sequence[BackgroundJob]:
        return (BackgroundJob("loop", self._service.tick, self._interval_sec, initial_delay_sec=60.0),)
