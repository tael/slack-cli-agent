# Correspondence between mametchi audit kinds and this core's (G7).
#
# mametchi wrote every `audit(kind=...)` row to ~/.mametchi/logs/audit.jsonl;
# a request row carried no kind at all. Reading both bots' logs side by side
# needs one table that says, for each mametchi kind, which kind this core
# writes instead -- or why it writes none. A mametchi kind in neither table
# is a gap, and the unit test fails on it.

from __future__ import annotations

from collections.abc import Mapping

from .audit import IncidentKind as K

#: mametchi kind -> the kind this core writes for the same event. "" is the
#: kindless mametchi request row.
MAMETCHI_KIND_MAP: Mapping[str, K] = {
    "": K.REQUEST,
    "awaiting_reply": K.AWAITING_REPLY,
    "blocks_rejected": K.BLOCKS_REJECTED,
    "channel_join": K.CHANNEL_JOIN,
    "control_tag_leak": K.CONTROL_TAG_LEAK,
    "debug_trace_format": K.REVIEW_FORMAT,
    # The delegated turn's request row carries tier="delegated" (A12).
    "delegation_accepted": K.REQUEST,
    "delegation_recorded": K.DELEGATION_RECORDED,
    "dm_probe": K.DM_PROBE,
    "dm_redirect": K.DM_REPLY,
    "dm_redirect_capped": K.DM_REPLY_CAPPED,
    "dm_redirect_failed": K.DM_REPLY_FAILED,
    "empty_after_tags": K.EMPTY_AFTER_TAGS,
    # The publisher drops a blank body through the same check (tags=[]).
    "empty_body": K.EMPTY_AFTER_TAGS,
    "figurative_fix": K.FIGURATIVE_FIX,
    "file_permalink_stripped": K.FILE_PERMALINK_STRIPPED,
    # The answer is written over the tool's file message instead.
    "file_post_duplicate": K.TOOL_POST_MERGED,
    "format_review_format": K.REVIEW_FORMAT,
    "gate_skipped": K.GATE_SKIPPED,
    "generated_image_autopost": K.GENERATED_IMAGE_AUTOPOST,
    "generated_image_autopost_failed": K.GENERATED_IMAGE_AUTOPOST_FAILED,
    "late_addendum": K.LATE_ADDENDUM,
    # RewriteLossGuard: the rewrite came back much shorter, both kept.
    "late_addendum_shrank": K.REWRITE_LOSS,
    "missing_recipient": K.MISSING_RECIPIENT,
    "missing_recipient_retry": K.MISSING_RECIPIENT_RETRY,
    "notion_link_fixed": K.NOTION_LINK_FIXED,
    "origin_deleted": K.ORIGIN_DELETED,
    "peer_skipped": K.PEER_SKIPPED,
    "peer_turn_limit": K.PEER_TURN_LIMIT,
    "plain_mention": K.PLAIN_MENTION,
    "post_failed": K.POST_FAILED,
    "postmortem_five_why": K.REVIEW_CHECK_FAILED,
    "postmortem_format": K.REVIEW_FORMAT,
    "promise_without_watch": K.PROMISE_WITHOUT_WATCH,
    "promise_without_watch_retry_failed": K.PROMISE_WITHOUT_WATCH_RETRY_FAILED,
    "render_without_post": K.RENDER_WITHOUT_POST,
    "shadow_answered": K.SHADOW_ANSWERED,
    "shadow_judged": K.SHADOW_JUDGED,
    # Only ```csv blocks become files here; a failed one is this kind.
    "snippet_failed": K.CSV_UPLOAD_FAILED,
    "split": K.SPLIT,
    "split_broken": K.SPLIT_BROKEN,
    "unregistered_invite": K.UNREGISTERED_INVITE,
    "unregistered_mention": K.UNREGISTERED_MENTION,
    # watch_finished with superseded=True.
    "watch_superseded": K.WATCH_FINISHED,
    "watch_verdict": K.WATCH_VERDICT,
    "welcome_failed": K.WELCOME_FAILED,
    "welcome_posted": K.WELCOME_POSTED,
    "wrong_addressee": K.WRONG_ADDRESSEE,
    # One row here: the strip is silent, so no separate notice row (B2).
    "wrong_addressee_stripped": K.WRONG_ADDRESSEE,
}

#: mametchi kinds this core writes nothing for, with the reason.
MAMETCHI_KIND_UNPORTED: Mapping[str, str] = {
    "board_closed": "스레드 상태판은 이관하지 않는다 (Z1, 사용자 결정)",
    "stopped": "마메치 현재 코드에 기록 위치가 없다. 2026-09 이전 행만 남아 있다",
    "snippet": "긴 코드블록을 파일로 떼는 기능은 이관하지 않았다. csv 블록만 떼고 실패만 기록한다",
    "watch_verdict_recovered": "판정 누락 시 판정만 재요청하는 기능은 이관하지 않았다. 누락은 watch_verdict verdict=missing 으로 남는다",
}
