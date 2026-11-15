"""Audit-only check for "handed it to another bot" claims nothing backs.

2026-11-15 휴가 위임 거짓 보고: the final answer said the leave request was
handed to 마메치, but the turn never posted to Slack, so nobody received it.
The delegation ledger (delegations.json) expires after 30 minutes and cannot
serve as evidence, so the check reads the same turn's session transcript.

Only logs (audit kind ``handoff_claim_unbacked``); it never blocks a post.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

#: A bot name followed within 20 characters by a handoff verb.
HANDOFF_CLAIM = re.compile(r"(마메치|데이전트|구치파치|봇)[^\n]{0,20}(넘겼|맡겼|위임했|전달했)")
#: "넘기지 않았", "않았음", "못했", "안 넘겼" ... the sentence denies the handoff.
_NEGATION = re.compile(r"않|못\s?했|안\s?(넘|맡|위임|전달)")
_SENTENCE_SPLIT = re.compile(r"[.!?。\n]+")
SLACK_PERMALINK = "slack.com/archives"
POST_TOOL_MARK = "post_message"


def find_handoff_claim(body: str) -> str | None:
    """The first non-negated sentence claiming a handoff, or None."""
    for sentence in _SENTENCE_SPLIT.split(body or ""):
        if HANDOFF_CLAIM.search(sentence) and not _NEGATION.search(sentence):
            return sentence.strip()
    return None


def handoff_claim_unbacked(body: str, tool_names: Iterable[str] | None) -> str | None:
    """The claiming sentence when the turn shows no Slack post, else None.

    ``tool_names`` is every tool the turn called; None means the transcript
    could not be read, which is not evidence either way, so nothing is flagged.
    """
    claim = find_handoff_claim(body)
    if claim is None or tool_names is None:
        return None
    if SLACK_PERMALINK in (body or ""):
        return None
    if any(POST_TOOL_MARK in name for name in tool_names):
        return None
    return claim
