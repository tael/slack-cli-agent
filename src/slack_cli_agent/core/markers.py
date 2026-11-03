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


__all__ = [
    "ASK_RE",
    "ASK_TAG",
    "ELAPSED_LINE",
    "ELAPSED_MODEL_LINE",
    "NOWATCH_RE",
    "NOWATCH_TAG",
    "Recipient",
    "RecipientResolution",
    "resolve_recipient",
    "strip_unknown_tags",
    "take_ask_tag",
    "take_nowatch_tag",
    "take_recipient_tag",
]
