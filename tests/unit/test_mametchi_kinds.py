"""G7: every mametchi audit kind maps onto a kind this core writes, or is
listed as not ported with a reason."""

from __future__ import annotations

from pathlib import Path

from slack_cli_agent.observability.audit import IncidentKind
from slack_cli_agent.observability.mametchi_kinds import (
    MAMETCHI_KIND_MAP,
    MAMETCHI_KIND_UNPORTED,
)

# Collected 2026-11-07 from mametchi-slack-bot *.py `audit(kind=...)` calls
# and the distinct kinds in ~/.mametchi/logs/audit.jsonl. "" is the kindless
# request row.
MAMETCHI_KINDS = frozenset({
    "", "awaiting_reply", "blocks_rejected", "board_closed", "channel_join",
    "control_tag_leak", "debug_trace_format", "delegation_accepted",
    "delegation_recorded", "dm_probe", "dm_redirect", "dm_redirect_capped",
    "dm_redirect_failed", "empty_after_tags", "empty_body", "figurative_fix",
    "file_permalink_stripped", "file_post_duplicate", "format_review_format",
    "gate_skipped", "generated_image_autopost", "generated_image_autopost_failed",
    "late_addendum", "late_addendum_shrank", "missing_recipient",
    "missing_recipient_retry", "notion_link_fixed", "origin_deleted",
    "peer_skipped", "peer_turn_limit", "plain_mention", "post_failed",
    "postmortem_five_why", "postmortem_format", "promise_without_watch",
    "promise_without_watch_retry_failed", "render_without_post",
    "shadow_answered", "shadow_judged", "snippet", "snippet_failed", "split",
    "split_broken", "stopped", "unregistered_invite", "unregistered_mention",
    "watch_superseded", "watch_verdict", "watch_verdict_recovered",
    "welcome_failed", "welcome_posted", "wrong_addressee",
    "wrong_addressee_stripped",
})

SRC = Path(__file__).resolve().parents[2] / "src" / "slack_cli_agent"


def test_no_mametchi_kind_is_unaccounted_for() -> None:
    covered = set(MAMETCHI_KIND_MAP) | set(MAMETCHI_KIND_UNPORTED)
    assert sorted(MAMETCHI_KINDS - covered) == []


def test_a_kind_is_either_mapped_or_unported_not_both() -> None:
    assert set(MAMETCHI_KIND_MAP) & set(MAMETCHI_KIND_UNPORTED) == set()
    assert all(reason.strip() for reason in MAMETCHI_KIND_UNPORTED.values())


def test_every_mapped_kind_has_a_writer_in_the_code() -> None:
    """A mapped kind nothing records would pass the coverage test on paper."""
    skip = {SRC / "observability" / "audit.py", SRC / "observability" / "mametchi_kinds.py"}
    text = "\n".join(
        p.read_text(encoding="utf-8") for p in SRC.rglob("*.py") if p not in skip
    )
    unwritten = sorted(
        k.name for k in set(MAMETCHI_KIND_MAP.values())
        if f"IncidentKind.{k.name}" not in text and f"{k.name}_KIND" not in text
    )
    assert unwritten == []


def test_mapped_values_are_incident_kinds() -> None:
    assert all(isinstance(v, IncidentKind) for v in MAMETCHI_KIND_MAP.values())
