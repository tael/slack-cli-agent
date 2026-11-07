"""Regexes for trailer markers appended to message bodies before sending.

Lives in `core` (not `guard`) so the slack layer doesn't need to import guard.
`guard/watch.py` re-exports these to keep the old import path working.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

# Trailing marker, not part of the reply body — strip it before follow-up-question detection.
ELAPSED_LINE = re.compile(r"(?:\n\s*>\s*걸린 시간 : \s*\d+초\s*)+$")
# Models sometimes echo this line back from earlier turns; strip it too. Also matches the old "실행 엔진" label.
ELAPSED_MODEL_LINE = re.compile(r"\n*>\s*실행\s*(모델|엔진)\s*:.*$", re.MULTILINE)

# The model's own mark that this turn is waiting on a human decision
# (bot.py:1507, 2026-09-19). A short approval ("좋아", "네", "ㅇㅇ") reads the
# same as a reaction; the only thing that tells them apart is whether the
# bot's own last turn actually asked for one, and that fact has to survive a
# restart since the decision can land hours later (see session/awaiting.py).
ASK_TAG = "[[ASK]]"
# Only a line that is nothing but the tag counts. bot.py:1510-1512 (2026-11-01):
# matching the tag anywhere, including mid-sentence, ate real words next to it
# ("제가를 붙인", "내거나로") when a reply merely mentioned the convention.
ASK_RE = re.compile(r"^[ \t]*\[\[ASK\]\][ \t]*(?:\n|$)", re.MULTILINE)


def take_ask_tag(text: str) -> tuple[str, bool]:
    """Strips the `[[ASK]]` line and reports whether it was there.

    Matches bot.py:8273-8278's `take_ask_tag`.
    """
    if not text:
        return text, False
    stripped = ASK_RE.sub("", text)
    return stripped.rstrip(), stripped != text


# The model's own mark that this turn needs no background watch (bot.py:1538,
# 2026-09). The prompt asks the model to end with it so "no watch" is a
# deliberate signal rather than a forgotten `[[WATCH:]]` tag — but it is for the
# pipeline, never the reader, and must be stripped before sending. bot.py:1544:
# "모델이 규약대로 붙인 [[NOWATCH]] 가 사용자 화면에 그대로 나갔다" — the literal
# tag reached the channel until it was stripped.
NOWATCH_TAG = "[[NOWATCH]]"
# Only a line that is nothing but the tag counts, same reasoning as ASK_RE:
# matching mid-sentence would eat real words when a reply merely names the
# convention.
NOWATCH_RE = re.compile(r"^[ \t]*\[\[NOWATCH\]\][ \t]*(?:\n|$)", re.MULTILINE)


def take_nowatch_tag(text: str) -> tuple[str, bool]:
    """Strips the `[[NOWATCH]]` line(s) and reports whether any was there.

    Mirrors bot.py:1538's `NOWATCH_RE`. The model ends a reply that needs no
    background watch with this tag; it is a signal, never part of the visible
    body, so strip it in every path that reaches the channel.
    """
    if not text:
        return text, False
    stripped = NOWATCH_RE.sub("", text)
    return stripped.rstrip(), stripped != text


RecipientKind = Literal["owner", "asker", "bot"]


@dataclass(frozen=True)
class Recipient:
    """A resolved `[[RECIPIENT:...]]` value.

    `bot_id` is only set when `kind == "bot"`. Folds bot.py:5425-5471's
    `take_recipient_tag` two-value return (recipient, bot_id) into one
    object -- those two only ever make sense together.
    """

    kind: RecipientKind
    bot_id: str | None = None


# Matches bot.py:5373-5375's `RECIPIENT_TAG`. Anchored at the end of the
# text (via `trailing` + `\Z`) rather than per-line like ASK_RE/NOWATCH_RE,
# since bot.py:5370-5372 found the model putting [[NOWATCH]] on the line
# after [[RECIPIENT:...]] and a plain end-of-string anchor missed the tag
# entirely. The trailing group swallows those later control-tag lines so
# they're handed back untouched for ASK_RE/NOWATCH_RE/WATCH_RE to find.
RECIPIENT_TAG = re.compile(
    r"\s*\[\[RECIPIENT:(OWNER|ASKER|BOT:([A-Z0-9]+))\]\][ \t]*"
    r"(?P<trailing>(?:\n[ \t]*\[\[[^\]\n]*\]\][ \t]*)*)\s*\Z"
)


def take_recipient_tag(text: str) -> tuple[str, Recipient | None]:
    """Strips the `[[RECIPIENT:...]]` marker and classifies it.

    Matches bot.py:5425-5471's `take_recipient_tag`, minus the parts that
    need a live Slack lookup (ASKER-is-actually-a-bot reclassification,
    first-line mention enforcement) -- those belong to guard/mentions.py
    (B5), which can wrap this with its own `GuardContext`. This function
    only parses and strips.
    """
    if not text:
        return text, None
    m = RECIPIENT_TAG.search(text)
    if not m:
        return text, None
    kind_raw, bot_id = m.group(1), m.group(2)
    trailing = (m.group("trailing") or "").strip("\n")
    head = text[: m.start()].rstrip()
    clean = f"{head}\n{trailing}" if trailing else head
    recipient = (
        Recipient(kind="bot", bot_id=bot_id)
        if kind_raw.startswith("BOT:")
        else Recipient(kind=kind_raw.lower())  # type: ignore[arg-type]
    )
    return clean, recipient


@dataclass(frozen=True)
class RecipientResolution:
    """Outcome of resolving a `[[RECIPIENT:...]]` marker before publish."""

    text: str
    recipient: Recipient | None
    blocked: bool


def resolve_recipient(text: str, *, require_recipient: bool = False) -> RecipientResolution:
    """Parses and strips the trailing `[[RECIPIENT:...]]` marker, and
    decides whether the caller should block publish.

    `require_recipient` is the config flag from the parity acceptance
    criteria: bot.py:7904-7907 hard-blocks (posts `NOTICE_NO_RECIPIENT`
    instead of the body) whenever no tag resolved. This repo defaults the
    flag off so a deployment that hasn't configured owner_id/asker_id yet
    degrades to "publish anyway" instead of going silent; a caller that has
    those configured can pass `require_recipient=True` to get bot.py's
    behavior.

    Only classifies -- actually refusing to publish, mention enforcement,
    and the ASKER-is-a-bot reclassification are the caller's job (see
    `take_recipient_tag`'s docstring for the guard/mentions.py interface).
    """
    clean, recipient = take_recipient_tag(text)
    blocked = require_recipient and recipient is None
    return RecipientResolution(text=clean, recipient=recipient, blocked=blocked)


# Any recipient tag, in any position -- not anchored at the end like
# RECIPIENT_TAG. Used by pick_recipient_tag to count distinct choices in a
# reply that was asked for the tag alone (bot.py:1974 RECIPIENT_TAG_ANY).
RECIPIENT_TAG_ANY = re.compile(r"\[\[RECIPIENT:(?:OWNER|ASKER|BOT:[A-Z0-9]+)\]\]")


def pick_recipient_tag(text: str) -> str | None:
    """Returns the single `[[RECIPIENT:...]]` tag in `text`, or None when
    there are zero or two-or-more *distinct* ones (A54, bot.py:1978
    pick_recipient_tag).

    The missing-recipient retry re-asks the model for just the tag; only an
    unambiguous single choice is accepted, since a reply naming two different
    recipients is exactly the mistake the block is there to catch. Repeating
    the same tag is unambiguous and accepted.
    """
    tags: set[str] = set(RECIPIENT_TAG_ANY.findall(text or ""))
    if len(tags) != 1:
        return None
    return tags.pop()


# The reprompt handed back to the engine when a reply arrives without a
# recipient tag (A53, bot.py:1962 missing_recipient_prompt). Deliberately
# free of org-specific names/IDs: the three tag options are the protocol,
# and who "OWNER" or a given bot is comes from config, not this string.
_MISSING_RECIPIENT_PROMPT = (
    "방금 답변에 수신자 표식이 없어 게시되지 않았다. 사용자 화면에는 아직 "
    "아무것도 보이지 않는다. 본문은 그대로 두고, 이 답을 받을 쪽을 가리키는 "
    "수신자 표식 하나만 답변 마지막 줄에 붙여 다시 보내라. 본문을 새로 쓰지 마라.\n"
    "- 지금 말을 건 쪽에게 답하면 [[RECIPIENT:ASKER]]\n"
    "- 봇 소유자에게 보고하거나 확인을 구하면 [[RECIPIENT:OWNER]]\n"
    "- 특정 봇에게 답하면 [[RECIPIENT:BOT:그 봇의 사용자 ID]]\n"
    "수신자를 정하지 못했으면 표식 없이 침묵 표식만 남겨라. 두 수신자를 한 "
    "답변에 섞지 마라."
)


def missing_recipient_prompt() -> str:
    """The reprompt string for a reply that lacked a recipient tag (A53,
    bot.py:1962 missing_recipient_prompt). The body is already held by the
    caller; the model is asked only to choose a tag so a rerun can't change
    the answer text (re-running with the full task could repeat a side effect
    the first turn already did).
    """
    return _MISSING_RECIPIENT_PROMPT


# Markers with their own dedicated handling elsewhere (ASK_RE/NOWATCH_RE
# above, guard/watch.py's WATCH_RE, take_recipient_tag). A line matching one
# of these shapes is left untouched by strip_unknown_tags() -- stripping it
# twice would be harmless, but logging it as "unknown" would not be.
_KNOWN_TAG_LINE_RE = re.compile(
    r"\A[ \t]*\[\[(?:ASK|NOWATCH|침묵"
    r"|WATCH(?:@\d{4}-\d{2}-\d{2})?(?::.*)?"
    r"|RECIPIENT:(?:OWNER|ASKER|BOT:[A-Z0-9]+))\]\][ \t]*\Z",
    re.DOTALL,
)

# Any other double-bracket token alone on its own line. Matches bot.py:1561's
# `UNKNOWN_TAG_LINE_RE` -- a model that invents a marker outside the agreed
# set (2026-10-19: `[[침묵]]` itself slipped through unflagged before that
# patch, see bot.py:3406-3409) must not reach the channel, but silently
# eating it would hide the mistake just as badly, so it's logged instead.
_UNKNOWN_TAG_LINE_RE = re.compile(r"\A[ \t]*\[\[[^\[\]\n]{1,80}\]\][ \t]*\Z")


def strip_unknown_tags(text: str) -> tuple[str, list[str]]:
    """Drops undeclared `[[...]]` marker lines before publish and reports
    them for the audit log.

    Matches bot.py:1564-1592's `strip_control_tags`, narrowed to the
    "unknown" half of it -- the known markers here already have their own
    take_*_tag()/WATCH_RE functions upstream, so this only has to catch what
    they don't recognize. A fenced code block is left alone throughout: a
    reply that explains the marker convention by showing one as an example
    must not have it stripped out from under it.
    """
    if not text or "[[" not in text:
        return text, []
    dropped: list[str] = []
    out: list[str] = []
    in_fence = False
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            out.append(line)
            continue
        if not in_fence and _KNOWN_TAG_LINE_RE.match(line):
            out.append(line)
            continue
        if not in_fence and _UNKNOWN_TAG_LINE_RE.match(line):
            dropped.append("unknown:" + stripped)
            continue
        out.append(line)
    if not dropped:
        return text, []
    return "\n".join(out).rstrip(), dropped


# Every marker the engine-to-bot protocol defines, as a whole line. Matches
# bot.py:1573-1576's `CONTROL_TAG_LINE_RE`, plus the two this repo also uses
# (`[[침묵]]`, `[[WATCH_SUPERSEDED]]`). Unlike _KNOWN_TAG_LINE_RE, which exists
# to *skip* these lines, this one exists to drop them.
_CONTROL_TAG_LINE_RE = re.compile(
    r"\A[ \t]*\[\[(?:ASK|NOWATCH|침묵|WATCH_DONE|WATCH_STILL|WATCH_SUPERSEDED"
    r"|WATCH(?:@\d{4}-\d{2}-\d{2})?(?::.*)?"
    r"|RECIPIENT:(?:OWNER|ASKER|BOT:[A-Z0-9]+))\]\][ \t]*\Z",
    re.DOTALL,
)

# A trailing `[[WATCH: ...]]` may span several lines, so the line pass below
# can't see it. Same shape as guard/watch.py's WATCH_RE (kept here so core
# doesn't import guard), narrowed after PR #108 review: the tag has to start
# its own line and hold no other `[[`/`]]`. Unanchored, the lazy body ran from
# a mid-sentence mention all the way to a later marker and ate the lines in
# between. It is matched only after the line pass, so a trailing [[NOWATCH]]
# line is already gone and can't stretch the match.
_TRAILING_WATCH_RE = re.compile(
    r"(?:\A|\n)[ \t]*(\[\[WATCH(?:@\d{4}-\d{2}-\d{2})?:(?:(?!\[\[|\]\]).)+?\]\])\s*\Z",
    re.DOTALL,
)


def _inside_open_fence(prefix: str) -> bool:
    """True when `prefix` leaves a ``` fence open, i.e. what follows is code."""
    return sum(1 for ln in prefix.split("\n") if ln.strip().startswith("```")) % 2 == 1


def strip_control_tags(text: str) -> tuple[str, list[str]]:
    """Drops every internal marker line before publish and returns what it
    dropped, for the audit log (B4, bot.py:1586-1614 strip_control_tags).

    Known markers are reported as-is, undeclared ones with an `unknown:`
    prefix. Reaching this point means the marker's own handler didn't take
    it (a WATCH here was never registered), so the caller records it as a
    leak. Only whole-line markers outside fenced code count: a sentence that
    names a marker, or a code block showing one, is content.
    """
    if not text or "[[" not in text:
        return text, []
    dropped: list[str] = []
    out: list[str] = []
    in_fence = False
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            out.append(line)
            continue
        if not in_fence and _CONTROL_TAG_LINE_RE.match(line):
            dropped.append(stripped)
            continue
        if not in_fence and _UNKNOWN_TAG_LINE_RE.match(line):
            dropped.append("unknown:" + stripped)
            continue
        out.append(line)
    rest = "\n".join(out)
    m = _TRAILING_WATCH_RE.search(rest)
    if m and not _inside_open_fence(rest[: m.start()]):
        dropped.append(m.group(1))
        rest = rest[: m.start()]
    if not dropped:
        return text, []
    return rest.rstrip(), dropped


def split_leaked_tags(dropped: list[str]) -> tuple[list[str], list[str]]:
    """Splits strip_control_tags()'s report into (known, unknown) so the two
    land under their own audit kinds."""
    known = [t for t in dropped if not t.startswith("unknown:")]
    unknown = [t for t in dropped if t.startswith("unknown:")]
    return known, unknown


__all__ = [
    "ASK_RE",
    "ASK_TAG",
    "ELAPSED_LINE",
    "ELAPSED_MODEL_LINE",
    "NOWATCH_RE",
    "NOWATCH_TAG",
    "RECIPIENT_TAG_ANY",
    "Recipient",
    "RecipientResolution",
    "missing_recipient_prompt",
    "pick_recipient_tag",
    "resolve_recipient",
    "split_leaked_tags",
    "strip_control_tags",
    "strip_unknown_tags",
    "take_ask_tag",
    "take_nowatch_tag",
    "take_recipient_tag",
]
