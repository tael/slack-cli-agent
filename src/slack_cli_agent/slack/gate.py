"""Decides whether to jump into a thread the bot wasn't addressed in,
and whether the bot's last message was left awaiting a reply.

The regexes are shaped to avoid backtracking blowup; see the comments
on REACTION_WORD for why no alternative there gets its own `+`.

ELAPSED_LINE strips the timing footer before checking for a question.
It comes from core.markers rather than the guard layer, since gate has
no reason to depend on guard.
"""

from __future__ import annotations

import re

from slack_cli_agent.core.markers import ELAPSED_LINE

# Matches the bot's own text ending in a question mark or a Korean
# question-ending suffix.
ASKED_BACK = re.compile(r"(?:[?？]|까요|을까|ㄹ까|나요|린가요|드릴까)[\s.!]*$")

# Reaction words that don't expect a reply. A message made up only of
# these is just an acknowledgment.
#
# No alternative here gets its own `+`. REACTION_ONLY wraps this whole
# group in `+` below, so an inner `+` would produce a form like
# (?:ㅋ+)+, where the number of ways to split the same string doubles
# per character. Repetition is expressed only via the outer `+` — "오오오"
# still matches, as the alternative "오" repeated three times.
REACTION_WORD = (
    r"(?:오|와|우와|헐|아|어|음|흠|네|넵|예|응|good|굿|"
    r"감사(?:합니다|해요|요)?|고맙(?:습니다|다)|반가(?:워요|워|웠어요)|"
    r"좋(?:아요|아|네요|습니다|은데요?)|괜찮(?:아요|네요)|알겠(?:습니다|어요|어)|"
    r"확인(?:했습니다|했어요)?|그렇(?:군요|네요|구나|죠)|맞아요|역시|대박|짱|최고|"
    r"수고(?:하셨습니다|하셨어요|요)?|고생(?:하셨습니다|하셨어요)?|"
    r"ㅇㅇ|ㅋ|ㅎ|ㄱㅅ|굳)"
)
# Characters allowed between reaction words. Deliberately excludes ㅋ/ㅎ
# — including them would let this class overlap with the word
# alternatives above and blow up backtracking.
REACTION_GAP = r"[\s.!~,]*"
# One or more reaction words strung together isn't something awaiting a reply.
REACTION_ONLY = re.compile(rf"^\s*(?:{REACTION_WORD}{REACTION_GAP})+$")
# Caps backtracking cost, not realistic input length. Keep this even
# if REACTION_WORD grows more alternatives. Longer input just skips
# the reaction check and falls through to answering — the safer
# default.
REACTION_MAX_LEN = 120

# A message entirely wrapped in one pair of parentheses, like
# (딴생각) or (하품) — a self-directed aside, not something addressed
# to the bot.
MUTTER_ONLY = re.compile(r"^[\(（].*[\)）]$", re.DOTALL)


class ResponseGate:
    def worth_answering(self, text: str, bot_asked: bool = False) -> bool:
        """Decide whether to jump into a thread the bot wasn't addressed in.

        Filters by vocabulary, not length — Korean commands are short
        ("올려", "보내", "취소해", none over 15 characters), and a
        length cutoff dropped three real approvals in a row on
        2026-08-25. Anything that isn't purely reaction words gets
        forwarded to the model, including the judgment that this
        isn't a place to speak up.
        """
        t = (text or "").strip()
        if not t:
            return False
        # Filter this first: a muttered aside isn't an answer even if the bot just asked something.
        if MUTTER_ONLY.match(t):
            return False
        # A reply to the bot's own question counts no matter how short —
        # "ㅇㅇ" or a thumbs-up emoji is a valid approval.
        if bot_asked:
            return True
        if not re.search(r"[가-힣a-zA-Z0-9]", t):
            return False
        if len(t) <= REACTION_MAX_LEN and REACTION_ONLY.match(t):
            return False
        # A closing pleasantry ("앞으로 잘 부탁") isn't a request even
        # though it contains 부탁, unless it's phrased as a question.
        return not (re.search(r"(?:잘|앞으로|많이)\s*부탁", t) and not re.search(r"[?？]", t))

    def asked_back(self, text: str) -> bool:
        """Whether the bot's last message was left awaiting a reply.

        Strips the timing footer first — on 2026-09-02 a trailing "네"
        after "지금 반영할까요." was misread as a reaction because the
        footer sat between them.
        """
        t = ELAPSED_LINE.sub("", (text or "").strip()).strip()
        return bool(ASKED_BACK.search(t))
