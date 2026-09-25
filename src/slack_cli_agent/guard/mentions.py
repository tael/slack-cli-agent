"""Addressee guards: turns plain-text mentions into real ones and strips
mentions of the wrong recipient.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable
from typing import ClassVar

from slack_cli_agent.guard.base import GuardContext, GuardResult, OutputGuard

ADDRESSED_AT_HEAD = re.compile(r"^\s*<@([A-Z0-9]+)>\s*(?:님|씨)?[,.!\s]*")

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
