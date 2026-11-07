"""Per-model token and cost rollup from audit rows (A14).

The Claude CLI reports a per-model usage table (``modelUsage``) with its own
``costUSD`` per model. The pipeline, review and watch rows carry it under
``model_usage``. The table is session-cumulative (engine/base.py ModelUsage),
so a row's own spend is that row minus the previous row of the same session.
This module takes those differences and sums them, so the advisor model a
turn consulted is counted apart from the executor that answered.

Pure functions over already-parsed rows; the metrics collector does the
reading.
"""

from __future__ import annotations

import collections
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..engine.base import MODEL_USAGE_KEY, MODEL_USAGE_SESSION_KEY, USAGE_FIELDS
from .audit import REQUEST_KIND

#: How far before the window the collector reads rows so a session that began
#: earlier still has its previous row to subtract. A session resumed after
#: longer than this has its first in-window row counted whole (over-count).
LOOKBACK_SEC = 30 * 86400


@dataclass(frozen=True)
class RowSpend:
    """One audit row's own spend, per model."""

    at: float
    kind: str
    channel: str
    #: model -> {token field: count, "cost_usd": float | None}
    by_model: Mapping[str, Mapping[str, Any]]

    @property
    def cost_usd(self) -> float | None:
        costs = [row.get("cost_usd") for row in self.by_model.values()]
        known = [c for c in costs if isinstance(c, (int, float))]
        return float(sum(known)) if known else None


def _table(record: Mapping[str, Any]) -> dict[str, dict[str, Any]] | None:
    raw = record.get(MODEL_USAGE_KEY)
    if not isinstance(raw, Mapping) or not raw:
        return None
    out: dict[str, dict[str, Any]] = {}
    for model, row in raw.items():
        if not isinstance(row, Mapping):
            continue
        entry: dict[str, Any] = {}
        for name in USAGE_FIELDS:
            value = row.get(name)
            entry[name] = int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0
        cost = row.get("cost_usd")
        entry["cost_usd"] = (
            float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None
        )
        out[str(model)] = entry
    return out or None


def _minus(cur: Mapping[str, Mapping[str, Any]], prev: Mapping[str, Mapping[str, Any]]):
    """cur - prev per model, or None when any count went down (the CLI
    started counting afresh, so cur is the row's own spend)."""
    delta: dict[str, dict[str, Any]] = {}
    for model, row in cur.items():
        before = prev.get(model)
        if before is None:
            delta[model] = dict(row)
            continue
        entry: dict[str, Any] = {}
        for name in USAGE_FIELDS:
            diff = int(row[name]) - int(before[name])
            if diff < 0:
                return None
            entry[name] = diff
        cost, cost_before = row["cost_usd"], before["cost_usd"]
        if cost is None:
            entry["cost_usd"] = None
        elif cost_before is None:
            # No earlier figure to subtract; the whole value would double count.
            entry["cost_usd"] = None
        else:
            if cost < cost_before - 1e-9:
                return None
            entry["cost_usd"] = max(cost - cost_before, 0.0)
        delta[model] = entry
    # A model the session had before but not now: the table was started anew.
    if any(model not in cur for model in prev):
        return None
    return delta


def _run_output(record: Mapping[str, Any]) -> int:
    usage = record.get("usage")
    if not isinstance(usage, Mapping):
        return 0
    unavailable = usage.get("unavailable")
    if isinstance(unavailable, list) and "output_tokens" in unavailable:
        return 0
    value = usage.get("output_tokens")
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def row_spends(records: Iterable[Mapping[str, Any]]) -> list[RowSpend]:
    """Each record's own spend, in the order given (oldest first).

    Records need ``at``, ``kind``, ``channel`` and may carry ``session_id``,
    ``usage`` and ``model_usage``. Records without a table are skipped. A
    record whose difference is smaller than the run's own output tokens was
    not cumulative over the previous row (the CLI did not restore the
    session's counters), so its table is taken whole.
    """
    last: dict[str, dict[str, dict[str, Any]]] = {}
    out: list[RowSpend] = []
    for record in records:
        table = _table(record)
        if table is None:
            continue
        session = str(record.get(MODEL_USAGE_SESSION_KEY) or record.get("session_id") or "")
        own = table
        if session and session in last:
            delta = _minus(table, last[session])
            if delta is not None:
                delta_output = sum(int(r["output_tokens"]) for r in delta.values())
                if delta_output >= _run_output(record):
                    own = delta
        if session:
            last[session] = table
        out.append(RowSpend(
            at=float(record.get("at") or 0.0),
            kind=str(record.get("kind") or ""),
            channel=str(record.get("channel") or ""),
            by_model=own,
        ))
    return out


def rollup(spends: Sequence[RowSpend], cutoff: float, day_of: Any) -> dict[str, Any]:
    """Sums row spends at or after `cutoff`.

    `day_of(epoch) -> "YYYY-MM-DD"` buckets by day. Cost totals are None when
    no row in the window carried a cost, so "unmeasured" never reads as $0.
    """
    in_window = [s for s in spends if s.at >= cutoff]
    by_model: dict[str, dict[str, Any]] = {}
    by_day: collections.defaultdict[str, float] = collections.defaultdict(float)
    by_channel: collections.defaultdict[str, float] = collections.defaultdict(float)
    by_kind: collections.defaultdict[str, float] = collections.defaultdict(float)
    request_costs: list[float] = []
    cost_seen = False
    for spend in in_window:
        for model, row in spend.by_model.items():
            agg = by_model.setdefault(model, {
                "model": model, **{name: 0 for name in USAGE_FIELDS},
                "cost_usd": None, "calls": 0,
            })
            spent = False
            for name in USAGE_FIELDS:
                agg[name] += int(row[name])
                spent = spent or int(row[name]) > 0
            if row.get("cost_usd") is not None:
                agg["cost_usd"] = (agg["cost_usd"] or 0.0) + float(row["cost_usd"])
                spent = spent or row["cost_usd"] > 0
            if spent:
                agg["calls"] += 1
        cost = spend.cost_usd
        if cost is None:
            continue
        cost_seen = True
        by_day[day_of(spend.at)] += cost
        by_channel[spend.channel] += cost
        by_kind[spend.kind] += cost
        if spend.kind == REQUEST_KIND:
            request_costs.append(cost)
    models = sorted(
        by_model.values(),
        key=lambda r: (-(r["cost_usd"] or 0.0), -r["output_tokens"], r["model"]),
    )
    for row in models:
        if row["cost_usd"] is not None:
            row["cost_usd"] = round(row["cost_usd"], 4)
    request_costs.sort()
    return {
        "sample": len(in_window),
        "by_model": models,
        "cost_total_usd": round(sum(by_kind.values()), 4) if cost_seen else None,
        "cost_median_usd": round(_median(request_costs), 4) if request_costs else None,
        "cost_by_day": [{"day": d, "usd": round(v, 4)} for d, v in sorted(by_day.items())],
        "cost_by_channel": sorted(
            ({"channel": c, "usd": round(v, 4)} for c, v in by_channel.items()),
            key=lambda r: -r["usd"],
        ),
        "cost_by_kind": {k: round(v, 4) for k, v in sorted(by_kind.items())},
    }


def _median(values: Sequence[float]) -> float:
    n = len(values)
    mid = n // 2
    return values[mid] if n % 2 else (values[mid - 1] + values[mid]) / 2
