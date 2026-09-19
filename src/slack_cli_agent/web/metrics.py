"""Web console metrics collection, backed by the new store (SQLite + StatePaths).

The original dashboard (mametchi-slack-bot/dashboard/metrics.py) read four
separate sources: audit.jsonl, sessions.db, postmortems.json, channels.json.
Here those collapse into state.db (audit/sessions/reviews tables) plus
ChannelRegistry, so this queries the DB directly instead of re-parsing files.

The new audit "request" payload doesn't carry the user id, question/answer
text, cost, turn count, or tool names that the original had. Metrics that
depend on those are left as None (or grouped under not_applicable) rather
than filled with 0 or an estimate.
"""

from __future__ import annotations

import collections
import contextlib
import json
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..auth.policy import DEFAULT_EFFORT
from ..config.channel import ChannelConfig, ChannelRegistry
from ..config.profile import Profile
from ..config.settings import RuntimeSettings
from ..core.channel_kind import is_direct_message_channel
from ..engine.registry import registry_for_profile
from ..jobs.ports import JobStatus
from ..observability.audit import (
    BASELINE_KIND_VALUES,
    REQUEST_KIND,
    IncidentKind,
    normalize_kind,
)
from ..storage.database import Database

KST = timezone(timedelta(hours=9))

# The snapshot is rewritten every health_interval_sec. A threshold shorter than
# that period marks every bot stale for part of each cycle, so the bar is
# derived from the profile and allows two missed writes.
SNAPSHOT_STALE_CYCLES = 2.5
SLOW_SEC = 300

CCUSAGE_BIN = "/opt/homebrew/bin/ccusage"
CCUSAGE_TIMEOUT_SEC = 20

_NO_USER_ID = "감사 기록에 사용자 식별자가 없다"

# usage.by_user / usage.turns_median were added to the audit record after it
# already had traffic — sca-qi5.2. Rows written before that carry neither
# field at all, so a 0 count in the window can mean "not instrumented yet"
# as easily as "really zero". _usage_field_first_seen() tells them apart the
# same way quality.tracked_since does for incident kinds; these two entries
# are removed from the dict dynamically once a field's first appearance is known.
NOT_APPLICABLE_REASONS: dict[str, str] = {
    "usage.by_user": _NO_USER_ID,
    # Unit price differs by model and changes over time — computing a total
    # here would bake in a stale rate with no way to tell when it applied.
    # Token counts (usage.tokens) are recorded instead; deriving cost from
    # those is left to a reporting layer that can date its price table.
    "usage.cost_total_usd": "감사 기록에 비용 필드가 없다(단가는 모델·시점마다 달라 토큰 수만 남긴다)",
    "usage.turns_median": "감사 기록에 턴 수가 없다",
    "usage.question_len_median": "감사 기록에 질문 원문이 없다",
    "usage.answer_len_median": "감사 기록에 답변 원문이 없다",
    "reliability.context_reset": "감사 기록에 컨텍스트 재설정 표시가 없다",
    "reliability.restarts": "신규 저장소에 재기동 이력이 없다",
    "followup.same_user_repeat": _NO_USER_ID,
    "tools": "감사 기록에 도구 호출 이름이 없다",
    "channels[].context_tokens": "세션 맥락 크기 계측은 이 수집기 범위 밖이다",
}
USAGE_BLOCK_NOT_APPLICABLE_REASON = "이 엔진의 사용량은 ccusage 가 세지 않는다"

# Quality/reliability incident kinds this collector counts. REQUEST is the
# baseline traffic kind, tallied separately by `_read_requests`.
_INCIDENT_KINDS: tuple[IncidentKind, ...] = (
    IncidentKind.LATE_ADDENDUM,
    IncidentKind.WRONG_ADDRESSEE,
    IncidentKind.SPLIT_BROKEN,
    IncidentKind.POST_FAILED,
    IncidentKind.REWRITE_LOSS,
    IncidentKind.SILENT,
    IncidentKind.PROGRESS_UNKNOWN_TOOL,
    IncidentKind.BLOCKS_RESPLIT,
)

_SLOW_LABELS = (
    "10초 이하", "10-30초", "30-60초", "1-2분", "2-5분", "5-10분", "10-15분", "15분 초과",
)
_SLOW_EDGES = (10, 30, 60, 120, 300, 600, 900)


def _kst(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, KST).isoformat(timespec="seconds")


def _pct(part: int, whole: int) -> float | None:
    return round(part / whole * 100, 1) if whole else None


def _quantile(sorted_values: Sequence[float], q: float) -> float | None:
    if not sorted_values:
        return None
    index = min(int(len(sorted_values) * q), len(sorted_values) - 1)
    value = sorted_values[index]
    return round(value, 2) if value < 10 else round(value)


def _epoch_from_iso(text: object) -> float | None:
    try:
        return datetime.fromisoformat(str(text)).timestamp()
    except ValueError:
        return None


def snapshot_stale_after(profile: Profile) -> float:
    settings = RuntimeSettings().override(profile.settings_override)
    return settings.health_interval_sec * SNAPSHOT_STALE_CYCLES


def read_snapshot(path: Path, now: float, stale_after: float) -> dict[str, Any]:
    """워커가 남긴 상태 스냅샷을 읽는다. 명부와 지표가 같이 쓴다."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"available": False, "reason": "스냅샷 파일 없음"}
    if not isinstance(data, dict):
        return {"available": False, "reason": "스냅샷 파일 없음"}
    written_at = data.get("written_at")
    if not isinstance(written_at, (int, float)):
        return {"available": False, "reason": "스냅샷 파일 없음"}
    age = now - written_at
    result: dict[str, Any] = dict(data)
    result["age_sec"] = round(age, 1)
    available = age <= stale_after
    result["available"] = available
    if not available:
        result["reason"] = f"스냅샷이 {int(age)}초 지났다"
    return result


def bot_row(profile: Profile, snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """봇 선택줄 한 줄. 프로필의 고정값과 스냅샷의 현재값을 합친다."""
    return {
        "name": profile.name,
        "display_name": profile.display_name,
        "engine": profile.primary_engine.type,
        "available": bool(snapshot.get("available")),
        "reason": snapshot.get("reason"),
        "pid": snapshot.get("pid"),
        "uptime_sec": snapshot.get("uptime_sec"),
        "inflight": snapshot.get("inflight"),
        "queued_total": snapshot.get("queued_total"),
        "shutting_down": bool(snapshot.get("shutting_down")),
    }


class MetricsCollector:
    def __init__(self, profile: Profile, *, now: Callable[[], float] = time.time) -> None:
        self._profile = profile
        self._now = now

    def collect(self, days: int) -> dict[str, Any]:
        """Closes the connection it opens. The console polls every few
        seconds, and a leaked handle per request eventually exhausts the
        process (127 open state.db handles in 5 hours, 2026-09-15)."""
        db = Database(self._profile.paths.database)
        try:
            return self._collect(db, days)
        finally:
            db.close()

    def _collect(self, db: Database, days: int) -> dict[str, Any]:
        now = self._now()
        paths = self._profile.paths
        db.migrate()
        cutoff = now - days * 86400

        snapshot = read_snapshot(paths.state_snapshot, now, snapshot_stale_after(self._profile))
        requests, incident_counts = self._read_requests(db, cutoff)
        first_seen = self._kind_first_seen_map(db)
        field_first_seen = self._usage_field_first_seen(db)
        reviews = self._read_reviews(db)
        sessions = self._read_sessions(db)
        queued_by_channel = self._read_queued_by_channel(db)
        channels = ChannelRegistry(paths.channels).all()

        latest = max((str(r.get("ts_kst") or "") for r in requests), default="")

        return {
            "generated_at": _kst(now),
            "window_days": days,
            "bot": self._bot_fields(field_first_seen),
            "snapshot": snapshot,
            "last_answer_kst": latest or None,
            "channels": self._channel_rows(requests, channels, sessions, queued_by_channel, now),
            "responsiveness": self._responsiveness(requests),
            "reliability": self._reliability(requests, incident_counts),
            "quality": self._quality(reviews, requests, incident_counts, first_seen),
            "usage": self._usage(requests, channels, field_first_seen),
            "followup": self._followup(requests),
            "tools": {"available": False, "reason": NOT_APPLICABLE_REASONS["tools"]},
            "queue_wait": self._queue_wait(requests),
            "usage_block": self._usage_block(),
            "session_total": len(sessions),
        }

    # --- 원천 읽기 -----------------------------------------------------

    def _read_requests(
        self, db: Database, cutoff: float
    ) -> tuple[list[dict[str, Any]], collections.Counter[str]]:
        """Reads every audit row in the window once and splits it by kind.

        Request-kind rows go through the same payload parsing as before;
        incident kinds are only tallied by name — the quality/reliability
        rollups need counts, not payload fields, and a bad payload there
        shouldn't hide that the incident happened at all. Baseline kinds
        other than REQUEST are neither parsed nor tallied.
        """
        rows = db.connect().execute(
            "SELECT at, kind, channel, thread_ts, payload FROM audit"
            " WHERE at >= ? ORDER BY id",
            (cutoff,),
        ).fetchall()
        out: list[dict[str, Any]] = []
        incidents: collections.Counter[str] = collections.Counter()
        for row in rows:
            kind = normalize_kind(row["kind"])
            if kind != REQUEST_KIND:
                if kind not in BASELINE_KIND_VALUES:
                    incidents[kind] += 1
                continue
            try:
                payload = json.loads(row["payload"])
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            record: dict[str, Any] = dict(payload)
            record.setdefault("channel", row["channel"])
            record.setdefault("thread_ts", row["thread_ts"])
            record["at"] = row["at"]
            record["ts_kst"] = _kst(row["at"])
            out.append(record)
        return out, incidents

    def _kind_first_seen_map(self, db: Database) -> dict[str, float]:
        """Earliest `at` ever recorded for each kind, unbounded by the
        requested window — used to tell "never happened" apart from "hasn't
        happened yet within this window because instrumentation is newer
        than the window start".
        """
        rows = db.connect().execute("SELECT kind, MIN(at) AS first_at FROM audit GROUP BY kind").fetchall()
        result: dict[str, float] = {}
        for row in rows:
            first_at = row["first_at"]
            if first_at is None:
                continue
            kind = normalize_kind(row["kind"])
            current = result.get(kind)
            value = float(first_at)
            if current is None or value < current:
                result[kind] = value
        return result

    def _usage_field_first_seen(self, db: Database) -> dict[str, float]:
        """Earliest `at` of a REQUEST row whose payload carries the `user` or
        `turns` key at all (regardless of value), unbounded by the window.

        These two fields were added to the audit record after request
        traffic already existed (sca-qi5.2), so a row can be missing the
        key entirely rather than carrying an empty/None value — presence of
        the key, not truthiness of the value, marks when instrumentation
        started. Without this, a window that predates the change would read
        as "0 사용자" instead of "collection hadn't started yet".
        """
        rows = db.connect().execute(
            "SELECT at, payload FROM audit WHERE kind = ? ORDER BY id", (REQUEST_KIND,)
        ).fetchall()
        result: dict[str, float] = {}
        for row in rows:
            if "by_user" in result and "turns_median" in result:
                break
            try:
                payload = json.loads(row["payload"])
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            at = float(row["at"])
            if "user" in payload and "by_user" not in result:
                result["by_user"] = at
            if "turns" in payload and "turns_median" not in result:
                result["turns_median"] = at
        return result

    def _read_reviews(self, db: Database) -> list[dict[str, Any]]:
        rows = db.connect().execute(
            "SELECT kind, channel, target_ts, at FROM reviews ORDER BY at"
        ).fetchall()
        return [dict(row) for row in rows]

    def _read_sessions(self, db: Database) -> list[dict[str, Any]]:
        rows = db.connect().execute(
            "SELECT scope, key, session_id, engine, created_at, last_seen_ts,"
            " updated_at, workdir, model FROM sessions"
        ).fetchall()
        return [dict(row) for row in rows]

    def _read_queued_by_channel(self, db: Database) -> dict[str, int]:
        rows = db.connect().execute(
            "SELECT channel, COUNT(*) AS n FROM jobs WHERE status = ? GROUP BY channel",
            (JobStatus.QUEUED.value,),
        ).fetchall()
        return {str(row["channel"]): int(row["n"]) for row in rows}

    def _count_corrections(self) -> int | None:
        """Counts both directories. New lines go to `learned`, but a bot that ran
        before the two sources were split still has its old ones under
        `knowledge`; reading only one would drop the count at that date
        (sca-jl4.5).
        """
        paths = self._profile.paths
        found = False
        total = 0
        for directory in (paths.knowledge, paths.learned):
            try:
                text = (directory / "_corrections.md").read_text(encoding="utf-8")
            except OSError:
                continue
            found = True
            total += sum(1 for line in text.splitlines() if line.startswith("- "))
        return total if found else None

    # --- 지표 계산 -------------------------------------------------------

    def _bot_fields(self, field_first_seen: Mapping[str, float]) -> dict[str, Any]:
        engine = self._profile.primary_engine
        not_applicable = dict(NOT_APPLICABLE_REASONS)
        if "by_user" in field_first_seen:
            not_applicable.pop("usage.by_user", None)
        if "turns_median" in field_first_seen:
            not_applicable.pop("usage.turns_median", None)
        if not self._ccusage_covers_engine():
            not_applicable["usage_block"] = USAGE_BLOCK_NOT_APPLICABLE_REASON
        return {
            "name": self._profile.name,
            "display_name": self._profile.display_name,
            "engine": engine.type,
            "default_model": engine.model,
            "owner_model": engine.model_for_owner(),
            "not_applicable": not_applicable,
        }


    def _responsiveness(self, requests: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        times = sorted(
            float(r["elapsed"]) for r in requests if isinstance(r.get("elapsed"), (int, float))
        )
        slow = [t for t in times if t > SLOW_SEC]
        buckets = [0] * len(_SLOW_LABELS)
        for value in times:
            for i, edge in enumerate(_SLOW_EDGES):
                if value <= edge:
                    buckets[i] += 1
                    break
            else:
                buckets[-1] += 1

        by_hour: collections.Counter[int] = collections.Counter()
        for r in requests:
            stamp = r.get("ts_kst")
            if isinstance(stamp, str) and stamp:
                with contextlib.suppress(ValueError):
                    by_hour[datetime.fromisoformat(stamp).hour] += 1

        return {
            "count": len(times),
            "median_sec": _quantile(times, 0.5),
            "p90_sec": _quantile(times, 0.9),
            "max_sec": round(times[-1]) if times else None,
            "slow_count": len(slow),
            "slow_pct": _pct(len(slow), len(times)),
            "slow_threshold_sec": SLOW_SEC,
            "histogram": [
                {"label": label, "count": count}
                for label, count in zip(_SLOW_LABELS, buckets, strict=True)
            ],
            "by_hour": [{"hour": h, "count": by_hour.get(h, 0)} for h in range(24)],
            "first_reaction": self._first_reaction(requests),
        }

    def _first_reaction(self, requests: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        values = sorted(
            float(r["first_reaction_sec"]) for r in requests
            if isinstance(r.get("first_reaction_sec"), (int, float))
        )
        return {
            "count": len(values),
            "sample_pct": _pct(len(values), len(requests)),
            "median_sec": _quantile(values, 0.5),
            "p90_sec": _quantile(values, 0.9),
            "max_sec": round(values[-1], 2) if values else None,
        }

    def _reliability(
        self, requests: Sequence[Mapping[str, Any]], incident_counts: collections.Counter[str]
    ) -> dict[str, Any]:
        ok = [r for r in requests if r.get("ok")]
        failed = [r for r in requests if not r.get("ok")]
        resumed = [r for r in requests if r.get("resumed")]
        recent_failures = [
            {
                "ts_kst": r.get("ts_kst"),
                "channel": r.get("channel"),
                "reason": r.get("failure") or "사유 없음",
            }
            for r in sorted(failed, key=lambda x: str(x.get("ts_kst") or ""), reverse=True)[:10]
        ]
        incidents = [{"kind": kind, "count": count} for kind, count in incident_counts.most_common()]
        return {
            "total": len(requests),
            "ok": len(ok),
            "failed": len(failed),
            "success_pct": _pct(len(ok), len(requests)),
            "resumed": len(resumed),
            "resumed_pct": _pct(len(resumed), len(requests)),
            "context_reset": None,
            "incidents": incidents,
            "restarts": None,
            "recent_failures": recent_failures,
        }

    def _quality(
        self,
        reviews: Sequence[Mapping[str, Any]],
        requests: Sequence[Mapping[str, Any]],
        incident_counts: collections.Counter[str],
        first_seen: Mapping[str, float],
    ) -> dict[str, Any]:
        kinds = collections.Counter(str(r["kind"]) for r in reviews)
        asked = collections.Counter(str(r.get("channel") or "") for r in requests)
        per_channel = collections.Counter(
            str(r["channel"]) for r in reviews if r["kind"] == "postmortem"
        )
        rows = []
        for channel, hits in per_channel.most_common():
            total = asked.get(channel)
            rows.append({
                "channel": channel,
                "postmortems": hits,
                "requests": total,
                "rate_pct": _pct(hits, total) if total else None,
            })
        silent = incident_counts.get(IncidentKind.SILENT.value, 0)
        tracked_since = {
            kind.value: (_kst(first_seen[kind.value]) if kind.value in first_seen else None)
            for kind in _INCIDENT_KINDS
        }
        return {
            "postmortems_total": kinds.get("postmortem", 0),
            "postmortems_by_channel": rows,
            "format_reviews": kinds.get("format_review", 0),
            "debug_traces": kinds.get("debug_trace", 0),
            "late_addendum": incident_counts.get(IncidentKind.LATE_ADDENDUM.value, 0),
            "wrong_addressee": incident_counts.get(IncidentKind.WRONG_ADDRESSEE.value, 0),
            "split_broken": incident_counts.get(IncidentKind.SPLIT_BROKEN.value, 0),
            "post_failed": incident_counts.get(IncidentKind.POST_FAILED.value, 0),
            "silent": silent,
            "silent_pct": _pct(silent, len(requests)),
            "corrections": self._count_corrections(),
            "rewrites": incident_counts.get(IncidentKind.REWRITE_LOSS.value, 0),
            "progress_unknown_tool": incident_counts.get(
                IncidentKind.PROGRESS_UNKNOWN_TOOL.value, 0
            ),
            "blocks_resplit": incident_counts.get(IncidentKind.BLOCKS_RESPLIT.value, 0),
            # Per sca-qi5.1 item 5: None means this kind has never fired in
            # this store, so a 0 count in the window can't be told apart
            # from "not instrumented yet" without this.
            "tracked_since": tracked_since,
        }

    def _usage(
        self,
        requests: Sequence[Mapping[str, Any]],
        channels: Mapping[str, ChannelConfig],
        field_first_seen: Mapping[str, float],
    ) -> dict[str, Any]:
        total = len(requests)
        by_channel: collections.Counter[str] = collections.Counter(
            str(r.get("channel") or "") for r in requests
        )
        by_model = collections.Counter(str(r["model"]) for r in requests if r.get("model"))
        by_effort = collections.Counter(str(r["effort"]) for r in requests if r.get("effort"))

        by_user_counter: collections.Counter[str] = collections.Counter(
            str(r["user"]) for r in requests if r.get("user")
        )
        by_user = [
            {"user": user, "count": count, "pct": _pct(count, total)}
            for user, count in by_user_counter.most_common()
        ]
        user_tracked_since = "by_user" in field_first_seen

        turns_values = sorted(int(r["turns"]) for r in requests if isinstance(r.get("turns"), int))
        turns_tracked_since = "turns_median" in field_first_seen

        tokens: collections.Counter[str] = collections.Counter()
        seen = 0
        for r in requests:
            usage = r.get("usage")
            if not isinstance(usage, dict):
                continue
            seen += 1
            tokens["input"] += int(usage.get("input_tokens") or 0)
            tokens["output"] += int(usage.get("output_tokens") or 0)
            tokens["cache_write"] += int(usage.get("cache_creation_tokens") or 0)
            tokens["cache_read"] += int(usage.get("cache_read_tokens") or 0)
        if seen:
            tokens["total"] = (
                tokens["input"] + tokens["output"] + tokens["cache_write"] + tokens["cache_read"]
            )

        def named(counter: collections.Counter[str]) -> list[dict[str, Any]]:
            return [
                {
                    "channel": channel,
                    "name": channels[channel].name if channel in channels else channel,
                    "count": count,
                    "pct": _pct(count, total),
                }
                for channel, count in counter.most_common()
            ]

        return {
            "by_channel": named(by_channel),
            "unlisted_channels": [
                c for c in by_channel if c not in channels and not is_direct_message_channel(c)
            ],
            "by_user": by_user if user_tracked_since else [],
            "user_count": len(by_user_counter) if user_tracked_since else None,
            "by_model": [{"model": m, "count": n} for m, n in by_model.most_common()],
            "by_effort": [{"effort": e, "count": n} for e, n in by_effort.most_common()],
            "cost_total_usd": None,
            "cost_median_usd": None,
            "cost_by_day": [],
            "cost_by_channel": [],
            "turns_median": _quantile(turns_values, 0.5) if turns_tracked_since else None,
            "question_len_median": None,
            "answer_len_median": None,
            "tokens": dict(tokens) if seen else None,
            "tokens_sample": seen,
            # None means this field has never been recorded in this store —
            # a 0 count above can't otherwise be told apart from "not
            # instrumented in this window yet". Per sca-qi5.2.
            "tracked_since": {
                "by_user": _kst(field_first_seen["by_user"]) if user_tracked_since else None,
                "turns_median": _kst(field_first_seen["turns_median"]) if turns_tracked_since else None,
            },
        }

    def _followup(self, requests: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        threads: collections.Counter[tuple[Any, Any]] = collections.Counter()
        for r in requests:
            key = (r.get("channel"), r.get("thread_ts"))
            if all(key):
                threads[key] += 1
        single = sum(1 for n in threads.values() if n == 1)
        multi = len(threads) - single
        return {
            "threads": len(threads),
            "single_turn": single,
            "multi_turn": multi,
            "same_user_repeat": None,
            "rate_pct": None,
            "rate_of_multi_pct": None,
        }

    def _queue_wait(self, requests: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        values = sorted(
            float(r["queue_wait_sec"]) for r in requests
            if isinstance(r.get("queue_wait_sec"), (int, float))
        )
        return {
            "approximate": False,
            "queued_events": len(values),
            "count": len(values),
            "matched_pct": 100.0,
            "median_sec": _quantile(values, 0.5),
            "p90_sec": _quantile(values, 0.9),
            "max_sec": round(values[-1]) if values else None,
        }

    def _channel_rows(
        self,
        requests: Sequence[Mapping[str, Any]],
        channels: Mapping[str, ChannelConfig],
        sessions: Sequence[Mapping[str, Any]],
        queued_by_channel: Mapping[str, int],
        now: float,
    ) -> list[dict[str, Any]]:
        settings = RuntimeSettings().override(self._profile.settings_override)
        thread_ttl = settings.session_ttl_hours * 3600
        channel_ttl = settings.channel_session_ttl_days * 86400

        thread_channel: dict[str, str] = {}
        last_seen: dict[str, str] = {}
        today = datetime.fromtimestamp(now, KST).strftime("%Y-%m-%d")
        today_count: collections.Counter[str] = collections.Counter()
        requests_window: collections.Counter[str] = collections.Counter()
        for r in requests:
            channel = r.get("channel")
            if not channel:
                continue
            channel = str(channel)
            requests_window[channel] += 1
            thread_ts = r.get("thread_ts")
            if thread_ts:
                thread_channel[str(thread_ts)] = channel
            stamp = str(r.get("ts_kst") or "")
            if stamp > last_seen.get(channel, ""):
                last_seen[channel] = stamp
            if stamp[:10] == today:
                today_count[channel] += 1

        live: dict[str, list[tuple[int, dict[str, Any]]]] = collections.defaultdict(list)
        for row in sessions:
            key = str(row["key"])
            session_channel: str | None
            if row["scope"] == "channel":
                session_channel = key
                ttl = channel_ttl
            else:
                session_channel = thread_channel.get(key)
                if session_channel is None and ":" in key:
                    session_channel = key.split(":", 1)[0]
                ttl = thread_ttl
            if not session_channel:
                continue
            age = now - float(row["updated_at"])
            if age > ttl:
                continue
            age_sec = round(age)
            live[session_channel].append((age_sec, {
                "session_id": str(row["session_id"])[:8],
                "model": row.get("model") or None,
                "age_sec": age_sec,
                "created_at": _kst(float(row["created_at"])) if row.get("created_at") else None,
                "last_used_at": _kst(float(row["updated_at"])),
                "workdir": row.get("workdir") or None,
                "context_tokens": None,
            }))

        seen = set(channels) | set(requests_window) | set(live) | set(queued_by_channel)
        rows: list[dict[str, Any]] = []
        for channel in seen:
            conf = channels.get(channel)
            entries = [entry for _, entry in sorted(live.get(channel, []), key=lambda x: x[0])]
            dm = is_direct_message_channel(channel)
            rows.append({
                "channel": channel,
                "name": conf.name if conf else channel,
                "is_dm": dm,
                "listed": conf is not None,
                "mode": conf.mode if conf else "default",
                "model": (conf.model if conf and conf.model else self._profile.primary_engine.model),
                "model_inherited": not (conf and conf.model),
                "effort": (conf.effort if conf and conf.effort else DEFAULT_EFFORT),
                "effort_inherited": not (conf and conf.effort),
                "answer_unaddressed": bool(conf and conf.answer_unaddressed),
                "rich": bool(conf and conf.rich),
                "light_context": bool(conf and conf.light_context),
                "session_scope": conf.session_scope if conf else "thread",
                "live_sessions": len(entries),
                "newest_session": entries[0] if entries else None,
                "context_total": None,
                "context_max": None,
                "context_unread": len(entries),
                "requests_today": today_count.get(channel, 0),
                "requests_window": requests_window.get(channel, 0),
                "last_seen": last_seen.get(channel) or None,
                "queued": queued_by_channel.get(channel, 0),
            })

        rows.sort(key=lambda r: str(r["last_seen"] or ""), reverse=True)
        return rows

    def _ccusage_covers_engine(self) -> bool:
        """ccusage reports Claude Code's own consumption. Asking the engine
        rather than naming it keeps a new engine from silently inheriting
        another engine's numbers (sca-cs0). An engine we don't know about is
        treated as not covered."""
        registry = registry_for_profile(self._profile)
        engine_class = registry.engine_class(self._profile.primary_engine.type)
        return bool(engine_class is not None and engine_class.ccusage_reports_consumption)

    def _usage_block(self) -> dict[str, Any]:
        if not self._ccusage_covers_engine():
            return {
                "available": False,
                "kind": "engine_not_covered",
                "reason": USAGE_BLOCK_NOT_APPLICABLE_REASON,
            }
        block = self._ccusage_block()
        block["kind"] = "ccusage_block"
        return block

    def _ccusage_block(self) -> dict[str, Any]:
        if not Path(CCUSAGE_BIN).exists():
            return {"available": False, "reason": "ccusage 없음"}
        try:
            out = subprocess.run(
                [CCUSAGE_BIN, "blocks", "--active", "-z", "Asia/Seoul", "--json"],
                capture_output=True, text=True, timeout=CCUSAGE_TIMEOUT_SEC, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return {"available": False, "reason": "ccusage 실행 실패"}
        if out.returncode != 0:
            return {"available": False, "reason": "ccusage 실행 실패"}
        try:
            parsed = json.loads(out.stdout)
        except json.JSONDecodeError:
            return {"available": False, "reason": "ccusage 응답을 읽지 못함"}
        blocks = (parsed or {}).get("blocks") or []
        block = next((b for b in blocks if b.get("isActive")), None)
        if not block:
            return {"available": False, "reason": "열려 있는 블록 없음"}

        start = _epoch_from_iso(block.get("startTime"))
        end = _epoch_from_iso(block.get("endTime"))
        now = time.time()
        counts = block.get("tokenCounts") or {}
        burn = block.get("burnRate") or {}
        projection = block.get("projection") or {}
        return {
            "available": True,
            "start_kst": _kst(start) if start else None,
            "end_kst": _kst(end) if end else None,
            "elapsed_sec": round(now - start) if start else None,
            "remaining_sec": (max(0, round(end - now)) if end else None),
            "cost_usd": round(block.get("costUSD") or 0, 2),
            "cost_per_hour_usd": round(burn.get("costPerHour") or 0, 2),
            "projected_cost_usd": round(projection.get("totalCost") or 0, 2),
            "total_tokens": block.get("totalTokens"),
            "cache_read_tokens": counts.get("cacheReadInputTokens"),
            "output_tokens": counts.get("outputTokens"),
            "entries": block.get("entries"),
            "models": block.get("models") or [],
            "scope": "이 노트북 기록만",
        }
