"""Addressee guards: turns plain-text mentions into real ones and strips
mentions of the wrong recipient.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import ClassVar

from slack_cli_agent.guard.base import GuardContext, GuardResult, OutputGuard

ADDRESSED_AT_HEAD = re.compile(r"^\s*<@([A-Z0-9]+)>\s*(?:님|씨)?[,.!\s]*")

ANY_MENTION = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]*)?>")

#: Collapses the gap a removed mention leaves, without touching line
#: indentation or blank-line structure.
INNER_RUN_OF_SPACES = re.compile(r"(?<=\S)[ \t]{2,}(?=\S)")

CODE_SPANS = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)


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

        # Code spans are protected — substituting inside an example would
        # produce a bogus mention.
        holes: list[str] = []

        def stash(m: re.Match[str]) -> str:
            holes.append(m.group(0))
            return f"\x00{len(holes) - 1}\x00"

        out = CODE_SPANS.sub(stash, body)

        changed: list[str] = []
        for name in sorted(table, key=len, reverse=True):
            uid = table[name]
            pat = re.compile(r"@" + re.escape(name) + r"(?:\s*(?:님|씨))?")
            if pat.search(out):
                out = pat.sub(f"<@{uid}>", out)
                changed.append(name)

        out = re.sub(r"\x00(\d+)\x00", lambda m: holes[int(m.group(1))], out)
        return out, changed

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
    """

    name: ClassVar[str] = "bot_mention"

    def __init__(
        self, is_bot: Callable[[str], bool], display_name: Callable[[str], str]
    ) -> None:
        self._is_bot = is_bot
        self._display_name = display_name

    def apply(self, body: str, ctx: GuardContext) -> GuardResult:
        if not body or "<@" not in body:
            return GuardResult(body=body, changed=False)

        # Code spans are left as written; an example is not a call.
        holes: list[str] = []

        def stash(m: re.Match[str]) -> str:
            holes.append(m.group(0))
            return f"\x00{len(holes) - 1}\x00"

        out = CODE_SPANS.sub(stash, body)

        targets: list[str] = []
        verdicts: dict[str, bool] = {}

        def swap(m: re.Match[str]) -> str:
            user_id = m.group(1)
            if user_id not in verdicts:
                verdicts[user_id] = self._is_bot(user_id)
            if not verdicts[user_id]:
                return m.group(0)
            if user_id not in targets:
                targets.append(user_id)
            return self._display_name(user_id)

        out = ANY_MENTION.sub(swap, out)
        out = re.sub(r"\x00(\d+)\x00", lambda m: holes[int(m.group(1))], out)

        if not targets:
            return GuardResult(body=body, changed=False)
        return GuardResult(
            body=INNER_RUN_OF_SPACES.sub(" ", out), changed=True, detail={"targets": targets}
        )
