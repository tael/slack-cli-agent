"""Addressee guards: turns plain-text mentions into real ones and strips
mentions of the wrong recipient.
"""

from __future__ import annotations

import re
import secrets
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

from slack_cli_agent.guard.base import GuardContext, GuardResult, OutputGuard
from slack_cli_agent.slack.message_kind import HUMAN_SUBTYPES

ADDRESSED_AT_HEAD = re.compile(r"^\s*<@([A-Z0-9]+)>\s*(?:님|씨)?[,.!\s]*")

#: An English salutation the model sometimes opens a peer-bot reply with
#: ("Dear 마메치님,"). Once the leading mention is forced it only repeats the
#: addressee in a foreign register, so it is dropped (2026-11-06).
LEADING_SALUTATION = re.compile(r"^\s*(?:Dear|Hi|Hello|Hey)\s+[^,.\n]{1,30}[,.]\s*", re.IGNORECASE)

ANY_MENTION = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]*)?>")

#: Collapses the gap a removed mention leaves, without touching line
#: indentation or blank-line structure.
INNER_RUN_OF_SPACES = re.compile(r"(?<=\S)[ \t]{2,}(?=\S)")

#: A code span is a run of backticks closed by a run of the same length.
#: Single backticks keep the original's no-newline rule; two or more are
#: matched by length so a `` `` example isn't rewritten from the inside.
#: The run has to be the whole run on both sides -- without that the
#: opening length backtracks and pairs with a shorter closing run, hiding
#: real body text from every guard (sca-9qv).
CODE_SPANS = re.compile(r"(?<!`)(`{2,})(?!`)[\s\S]*?\1(?!`)|`[^`\n]*`")


class CodeSpanMask:
    """Hides code spans while a guard rewrites prose, then puts them back.

    An example is not a call: substituting inside one produces a mention
    that really fires. The placeholder carries a random tag because a fixed
    one gets misread as a placeholder when the body already contains that
    shape -- restoring then raises IndexError or swaps in the wrong span.
    """

    def __init__(self, body: str) -> None:
        self._spans: list[str] = []
        tag = secrets.token_hex(4)
        while f"\x00{tag}" in body:
            tag = secrets.token_hex(4)
        self._hole = re.compile(rf"\x00{tag}:(\d+)\x00")
        self._tag = tag
        self.masked = CODE_SPANS.sub(self._stash, body)

    def _stash(self, m: re.Match[str]) -> str:
        self._spans.append(m.group(0))
        return f"\x00{self._tag}:{len(self._spans) - 1}\x00"

    def restore(self, text: str) -> str:
        return self._hole.sub(lambda m: self._spans[int(m.group(1))], text)


class PlainMentionGuard(OutputGuard):
    """Rewrites plain-text @name mentions into real Slack mentions.

    Plain "@name" doesn't link or notify in Slack. This has bitten us before
    (the bot wrote a plain-text mention thinking it was pinging someone, and
    the person never got notified), so it's enforced in code rather than
    left to the prompt.
    """

    name: ClassVar[str] = "plain_mention"

    @staticmethod
    def _fix_plain_mentions(
        body: str, table: dict[str, str]
    ) -> tuple[str, list[str]]:
        """Longest names first, so a short name doesn't get substituted inside
        a longer one that contains it, leaving a dangling remainder.
        """
        if not body or not table:
            return body, []

        mask = CodeSpanMask(body)
        out = mask.masked

        changed: list[str] = []
        for name in sorted(table, key=len, reverse=True):
            uid = table[name]
            pat = re.compile(r"@" + re.escape(name) + r"(?:\s*(?:님|씨))?")
            if pat.search(out):
                out = pat.sub(f"<@{uid}>", out)
                changed.append(name)

        return mask.restore(out), changed

    def apply(self, body: str, ctx: GuardContext) -> GuardResult:
        out, changed = self._fix_plain_mentions(body, dict(ctx.mention_names))
        if not changed:
            return GuardResult(body=body, changed=False)
        return GuardResult(body=out, changed=True, detail={"names": changed})


class AddresseeGuard(OutputGuard):
    """Blocks the reply from addressing someone other than whoever asked.

    Has misfired before: replying to the owner's aside with a mention meant
    for the previous turn's participant. Speaker labels and prompting reduce
    this but both depend on the model complying, so this checks it in code.

    Only the leading mention is checked — a mention mid-body could be a
    draft or a quote, so it's left alone. When it strips a mention, it
    leaves a note in the body rather than silently editing.
    """

    name: ClassVar[str] = "wrong_addressee"

    @staticmethod
    def _guard_wrong_addressee(
        body: str, is_owner: bool, asker_id: str, owner_user_id: str
    ) -> tuple[str, str | None]:
        m = ADDRESSED_AT_HEAD.match(body or "")
        if not m:
            return body, None
        target = m.group(1)
        if target == (asker_id or "") or (is_owner and target == owner_user_id):
            return body, None
        fixed = body[m.end() :].lstrip()
        return fixed, target

    def apply(self, body: str, ctx: GuardContext) -> GuardResult:
        fixed, wrong = self._guard_wrong_addressee(
            body, ctx.is_owner, ctx.asker_id, ctx.owner_user_id
        )
        if wrong is None:
            return GuardResult(body=body, changed=False)
        fixed = fixed + "\n\n(다른 분을 부르는 첫머리를 지웠습니다.)"
        return GuardResult(body=fixed, changed=True, detail={"wrong_target": wrong})


class BotMentionGuard(OutputGuard):
    """Replaces mentions of other bots with their plain display name.

    app_mention carries no bot_id filter (sca-3ee), so a mention another bot
    posts wakes this one. That is deliberate — it is how the test probe drives
    the whole mention-to-answer path. The loop it allows is stopped here
    instead: if this bot's answer names another bot, that bot answers, and if
    its answer names this one back they keep waking each other.

    Human mentions are left alone. Calling a person is a feature of this bot,
    and PlainMentionGuard exists to create exactly those mentions.

    A channel with `bot_mentions` on keeps the mentions of every bot except
    the one that asked. Handing work to another bot needs a real mention --
    a plain name neither links nor wakes it -- and the loop this guard exists
    to stop needs the answer to name the asker back, so only that leg is cut
    (sca-c4m).
    """

    name: ClassVar[str] = "bot_mention"

    def __init__(
        self,
        is_bot: Callable[[str], bool],
        display_name: Callable[[str], str],
        allowed_in: Callable[[str], bool] | None = None,
    ) -> None:
        self._is_bot = is_bot
        self._display_name = display_name
        self._allowed_in = allowed_in

    def _allowed(self, channel: str) -> bool:
        if self._allowed_in is None:
            return False
        try:
            return bool(self._allowed_in(channel))
        except Exception:  # noqa: BLE001 - an unreadable setting falls back to stripping
            return False

    def apply(self, body: str, ctx: GuardContext) -> GuardResult:
        if not body or "<@" not in body:
            return GuardResult(body=body, changed=False)

        # Only the asker's own mention closes the loop; the rest are calls
        # this channel is configured to allow.
        keep_others = self._allowed(ctx.channel)
        asker = ctx.asker_id or ""

        mask = CodeSpanMask(body)
        out = mask.masked

        targets: list[str] = []
        verdicts: dict[str, bool] = {}

        def swap(m: re.Match[str]) -> str:
            user_id = m.group(1)
            if user_id not in verdicts:
                verdicts[user_id] = self._is_bot(user_id)
            if not verdicts[user_id]:
                return m.group(0)
            if keep_others and user_id != asker:
                return m.group(0)
            if user_id not in targets:
                targets.append(user_id)
            return self._display_name(user_id)

        out = mask.restore(ANY_MENTION.sub(swap, out))

        if not targets:
            return GuardResult(body=body, changed=False)
        return GuardResult(
            body=INNER_RUN_OF_SPACES.sub(" ", out), changed=True, detail={"targets": targets}
        )


def calls_other_bot(is_bot: Callable[[str], bool], self_id: str, text: str) -> bool:
    """Whether a message names only other bots, not this one (C16).

    bot.py:4129-4146 `calls_other_bot`. More than one bot can sit in the
    same thread and answer the same prompt; a message that names one of
    them by mention is that bot's to answer, not every bot that happens to
    read it. A message naming nobody, or naming this bot among others,
    isn't filtered here.
    """
    called = ANY_MENTION.findall(text or "")
    if self_id and self_id in called:
        return False
    return any(is_bot(uid) for uid in called)


def talking_to_peer(
    messages: Iterable[Mapping[str, Any]],
    *,
    is_bot: Callable[[str], bool],
    self_id: str,
    upto_ts: str | None = None,
) -> bool:
    """Whether the thread's last human turn (before `upto_ts`) named a
    different bot, not this one (C17).

    bot.py:4148-4175 `talking_to_peer`. A person doesn't re-name a bot on
    every follow-up once a conversation with it has started -- they name it
    once and then just talk. An unaddressed reply right after that belongs
    to whichever bot was last named, not to whichever bot happens to be
    scanning the thread. Scans backward from the newest message so the
    *last* address wins over an older one further back; bot-authored
    messages (this bot's own included) are skipped while searching, same as
    the original -- only a human turn settles who was being addressed.
    """
    for m in reversed(list(messages)):
        ts = m.get("ts")
        if upto_ts and ts and float(ts) >= float(upto_ts):
            continue
        subtype = m.get("subtype")
        if m.get("bot_id") or (subtype and subtype not in HUMAN_SUBTYPES):
            continue
        called = ANY_MENTION.findall(m.get("text") or "")
        if self_id and self_id in called:
            return False
        if any(is_bot(uid) for uid in called):
            return True
    return False


class PeerExchangeLimiter:
    """Caps a bot-to-bot back-and-forth in one thread, reset the moment a
    human speaks (C19 cap, with the C18 reset it has to ship alongside).

    bot.py:4195-4200 (`peer_turn_allowed`/`reset_peer_turns`,
    `PEER_TURN_LIMIT = 6`). The cap alone is a trap: once a thread's count
    ever reaches the limit, every later message in it -- including one from
    a human who just walked back in -- would go unanswered forever with
    nothing to bring the count back down. That is why the reset has to ship
    with the cap, not after it (ownership.md's "B5 에 C18 포함" note).
    """

    def __init__(self, limit: int = 6) -> None:
        self._limit = limit
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def allowed(self, thread_ts: str) -> bool:
        """One more bot turn arrived in this thread; say whether it may run.

        Counts this call before comparing (bot.py increments first), so the
        call that pushes the count past the limit is itself the one denied.
        """
        with self._lock:
            count = self._counts.get(thread_ts, 0) + 1
            self._counts[thread_ts] = count
            return count <= self._limit

    def reset(self, thread_ts: str) -> None:
        """A human spoke in this thread; its bot-exchange count starts over."""
        with self._lock:
            self._counts.pop(thread_ts, None)
            if len(self._counts) > 200:
                # Matches bot.py's safety valve: don't let counts for
                # long-dead threads accumulate forever.
                self._counts.clear()


@dataclass(frozen=True)
class Recipient:
    """Who a reply is actually going to, resolved from the trailing
    `[[RECIPIENT:...]]` marker (D3)."""

    kind: str  # "owner" | "asker" | "bot"
    bot_id: str = ""


#: bot.py:5373-5375's RECIPIENT_TAG (trailing-marker form). Carried here
#: rather than imported from `core/markers.py` because bundle B1 -- the
#: owner of that module -- has not landed a RECIPIENT parser there yet
#: (checked 2026-11-03 on both `main` and `feat/parity-b1`). RecipientResolver
#: below is deliberately self-contained so that once B1 adds one, swapping
#: this regex for an import is a one-line change; the public shape (strip
#: the tag, hand back a `Recipient`) is what the rest of this bundle wires
#: against and won't need to move.
RECIPIENT_TAG = re.compile(
    r"\s*\[\[RECIPIENT:(OWNER|ASKER|BOT:([A-Z0-9]+))\]\][ \t]*"
    r"(?P<trailing>(?:\n[ \t]*\[\[[^\]\n]*\]\][ \t]*)*)\s*\Z"
)


def _mentions(body: str | None, user_id: str) -> bool:
    return any(m.group(1) == user_id for m in ANY_MENTION.finditer(body or ""))


class RecipientResolver:
    """Parses the trailing `[[RECIPIENT:...]]` marker and forces a leading
    mention when the recipient turns out to be a bot (D3).

    bot.py:5425-5471 `take_recipient_tag`. An `ASKER` tag folds into `BOT`
    when the asker turns out to be a bot -- the model always writes ASKER
    for "whoever just spoke to me", and a bot asker still needs the leading
    mention to actually get notified (Slack mentions, unlike plain names,
    are what wakes a bot up). A `BOT:` tag naming an ID that isn't actually
    a bot is treated as unresolved (`None`) rather than posted with a stray
    mention -- the caller's job is to block posting on `None`, not this
    class's.
    """

    def __init__(self, is_bot: Callable[[str], bool]) -> None:
        self._is_bot = is_bot

    def resolve(self, body: str, asker_id: str = "") -> tuple[str, Recipient | None]:
        m = RECIPIENT_TAG.search(body or "")
        if not m:
            # A bot asker still has to be woken up when the model forgot the
            # tag. Instructions alone did not hold: a turn resumed from an
            # owner-opened session keeps the owner's "no mention" note and
            # never sees the bot-asker note, so the reply to the peer bot went
            # out unmentioned and the peer never learned the work was handed
            # back (2026-11-06, 구치파치 -> 마메치 in rnd-pe팀-private).
            # Enforced here, at publish time, where the session cannot drift.
            if asker_id and self._is_bot(asker_id) and not _mentions(body, asker_id):
                head = ADDRESSED_AT_HEAD.match(body or "")
                rest = (body or "")[head.end() :].lstrip() if head else (body or "").lstrip()
                rest = LEADING_SALUTATION.sub("", rest, count=1)
                return f"<@{asker_id}> {rest}".rstrip(), Recipient("bot", asker_id)
            if asker_id and self._is_bot(asker_id):
                return body, Recipient("bot", asker_id)
            return body, None
        kind, bot_id = m.group(1), m.group(2)
        # Whatever followed the tag (another trailer, like [[NOWATCH]]) is
        # kept; only the RECIPIENT tag itself and the whitespace around it
        # are removed.
        clean = (body or "")[: m.start()].rstrip() + (m.group("trailing") or "").rstrip()
        if kind == "ASKER" and asker_id and self._is_bot(asker_id):
            kind, bot_id = "BOT", asker_id
        if kind in ("OWNER", "ASKER"):
            return clean, Recipient(kind.lower())
        if not bot_id or not self._is_bot(bot_id):
            return clean, None
        head = ADDRESSED_AT_HEAD.match(clean)
        if head:
            clean = clean[head.end() :].lstrip()
        clean = LEADING_SALUTATION.sub("", clean, count=1)
        return f"<@{bot_id}> {clean}".rstrip(), Recipient("bot", bot_id)
