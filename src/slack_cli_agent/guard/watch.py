"""Detects the [[WATCH:]] tag and empty follow-up promises without one.

`ELAPSED_LINE`/`ELAPSED_MODEL_LINE` actually live in `core.markers` (the
Slack layer needs them too and shouldn't depend on the guard layer), but are
re-exported here to keep existing `guard.watch.ELAPSED_LINE` imports working.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import ClassVar

# Must stay the same object as core.markers' — re-exported so a duplicate
# regex doesn't drift out of sync if only one copy gets edited.
from slack_cli_agent.core.markers import ELAPSED_LINE, ELAPSED_MODEL_LINE
from slack_cli_agent.core.timezones import KST
from slack_cli_agent.guard.base import GuardContext, GuardResult, OutputGuard, RerunRequest

# Declared to make the re-export explicit; otherwise linters flag it as unused.
__all__ = ["ELAPSED_LINE", "ELAPSED_MODEL_LINE"]

log = logging.getLogger(__name__)

# Internal markers the model returns with its check result; never shown to the user.
WATCH_DONE_TAG = "[[WATCH_DONE]]"
WATCH_STILL_TAG = "[[WATCH_STILL]]"
# The model's own verdict that a completion report it's about to write has
# already been said in the thread by the time the check ran (bot.py:1525,
# 2026-09-30). Never shown to the user either.
WATCH_SUPERSEDED_TAG = "[[WATCH_SUPERSEDED]]"
# Reaction left on a watched message. Kept distinct from in-progress/done
# marks so a completed watch isn't picked up again as unfinished.
WATCH_MARK_EMOJI = "mag"
# Only the final line counts, to avoid picking up a watch mention elsewhere in
# the body. The optional `@YYYY-MM-DD` group is a deadline (bot.py:1530-1535,
# 2026-09-19): the model names a date and the watch isn't checked before it
# arrives. Without it a week-long promise gets polled every five minutes for
# nothing.
WATCH_RE = re.compile(
    r"\n{0,2}\[\[WATCH(?:@(?P<due>\d{4}-\d{2}-\d{2}))?:\s*(?P<desc>.+?)\s*\]\]\s*\Z",
    re.DOTALL,
)


def watch_due_at(due: str | None) -> float | None:
    """Turns a `[[WATCH@2026-09-26: ...]]` date into that day's 09:00 KST,
    as an epoch timestamp. Matches bot.py:10769-10783's `watch_due_at`.

    Only the date matters, not the hour — this deadline is never a
    minute-precision commitment, just "sometime around the start of that
    business day". A malformed date falls back to no deadline rather than
    dropping the registration altogether; losing the promise entirely is
    worse than checking it without a deadline.
    """
    if not due:
        return None
    try:
        return datetime.strptime(due, "%Y-%m-%d").replace(hour=9, tzinfo=KST).timestamp()
    except ValueError:
        log.warning("감시 기한 형식이 틀렸다. 기한 없이 등록한다 : %s", due)
        return None


# Long, distinguishing identifier tokens only (dag_id, run_id, ticket keys).
# Ordinary words show up in any watch description and can't tell targets apart.
_WATCH_TARGET_TOKEN_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:+-]{9,}")


def _watch_target_keys(desc: str) -> set[str]:
    return {t.lower() for t in _WATCH_TARGET_TOKEN_RE.findall(desc or "")}


def same_watch_target(a: str, b: str) -> bool:
    """Whether two watch descriptions point at the same thing.

    Matches bot.py:10795-10804's `_same_watch_target`. Same target means the
    wording got updated but most identifying tokens survived; different
    targets share almost none. Descriptions with no qualifying token (plain
    Korean sentences, mostly) never match — there's nothing to compare.
    """
    ka, kb = _watch_target_keys(a), _watch_target_keys(b)
    if not ka or not kb:
        return False
    return len(ka & kb) / len(ka | kb) >= 0.5


def distinct_watch_targets(a: str, b: str) -> bool:
    """Whether two watch descriptions clearly name different things.

    True only when both carry identifying tokens and share none -- two
    different dag runs in one thread, say. Plain sentences, or one side
    with no token, are not proof of a different target: 2026-11-06's
    "old workers are gone" watch named no token at all and was about the
    same restart as the pid-file watch before it.
    """
    ka, kb = _watch_target_keys(a), _watch_target_keys(b)
    return bool(ka) and bool(kb) and not (ka & kb)

# The model has repeatedly said "I'll keep an eye on it" / "I'll report back"
# without the [[WATCH: ...]] tag, even with prompt guidance in place. Without
# the tag nothing gets queued and no one actually follows up, so this is
# caught in code instead of relying on the prompt alone.
PROMISE_WITHOUT_WATCH_RE = re.compile(
    r"(지켜보|끝나면|완료되면|확인되면|반영되면|반영후|배포\s*후)"
    r".{0,20}(보고|말씀|알려|다시\s*답)"
)


def promise_without_watch_prompt(body: str) -> str:
    """Rewrite prompt asking the model to either add the [[WATCH: ...]] tag
    or drop the follow-up promise entirely.
    """
    return (
        "방금 낸 답에 지켜보다가 결과가 나오면 보고하겠다는 취지의 말이 있는데, "
        "맨 끝에 [[WATCH: ...]] 태그가 없다.\n"
        "이 태그가 없으면 감시 큐에 등록되지 않아 실제로는 아무도 다시 보지 않는다.\n\n"
        f"방금 쓴 답 :\n{body}\n\n"
        "아래 둘 중 하나로 다시 쓴다.\n"
        "1. 지금 실행이 끝난 뒤에도 확인할 대상이 실제로 있으면, 같은 내용에 "
        "맨 끝 줄로 [[WATCH: 확인할 대상을 구체적으로. dag_id, run_id, task_id 등 "
        "나중에 이 문장 하나만 보고 다시 조회할 수 있을 만큼 값을 담은 한국어 문장]] "
        "을 붙인다.\n"
        "2. 지켜볼 대상이 없으면 지켜보겠다, 끝나면 보고하겠다는 말 자체를 빼고, "
        "지금까지 한 조치와 지금 상태만 말한 뒤 확인이 더 필요하면 다시 물어봐 "
        "달라고 맺는다."
    )


# A watch check turn runs read-only (tool_list_for(readonly=True)): it can
# read files, logs and Slack, but cannot run ps, send a test request or call
# anything that acts. 2026-11-07 17:23 a reply put "check the worker PID
# changed and send a request to see the status line" into a watch; the 17:41
# check turn could do neither and handed both back to the owner. Prompt rules
# had already said WATCH is for checking only (watch_note.md) and still the
# tag went through, so the registration itself is stopped here.
WATCH_NEEDS_ACTION_RE = re.compile(
    r"(\bps\b|pgrep|프로세스\s*목록|\[테스트\]|테스트\s*(요청|메시지|질문)"
    r"|요청을?\s*(보내|넣|날려)|(직접|실제로?)\s*(요청|실행|호출|보내)"
    r"|실행해\s*(보|서)|돌려\s*(보|서))"
)


def watch_needs_action_prompt(body: str, desc: str) -> str:
    return (
        "방금 낸 답의 [[WATCH: ...]] 에 실행해야 확인되는 항목이 있다.\n"
        f"감시 문장 : {desc}\n\n"
        "감시 확인 턴은 읽기 전용이라 ps, 테스트 요청 보내기, 명령 실행을 하지 못한다. "
        "그대로 등록하면 확인 턴은 그 항목을 미완료로 남기고 사람에게 넘긴다.\n\n"
        f"방금 쓴 답 :\n{body}\n\n"
        "아래 둘 중 하나로 다시 쓴다.\n"
        "1. 이 턴에 실행할 수 있으면 지금 실행하고 결과를 답에 쓴다. 그 항목은 WATCH 에서 뺀다.\n"
        "2. 이 턴이 끝난 뒤에만 생기는 결과면, 읽기만으로 판정할 증거로 감시 문장을 바꾼다. "
        "예 : worker.pid 와 state.json started_at 이 어느 시각 이후인지, worker 로그에 "
        "특정 문구가 찍혔는지, 슬랙 스레드에 어떤 답이 올라왔는지."
    )


class WatchPromiseGuard(OutputGuard):
    """Extracts the [[WATCH:]] target for registration, and requests a
    rewrite when a follow-up promise has no tag.

    Actually registering the watch job and calling the engine again are the
    caller's job; this only returns the target via `detail["watch_desc"]`
    or a `rerun` request.
    """

    name: ClassVar[str] = "watch_promise"

    def apply(self, body: str, ctx: GuardContext) -> GuardResult:
        m = WATCH_RE.search(body or "")
        if m:
            desc = m.group("desc").strip()
            due = m.group("due") or ""
            if WATCH_NEEDS_ACTION_RE.search(desc):
                if not ctx.is_rewrite_retry:
                    return GuardResult(
                        body=body,
                        changed=False,
                        detail={"watch_needs_action": desc},
                        rerun=RerunRequest(
                            reason="watch_needs_action",
                            rewrite_prompt=watch_needs_action_prompt(body, desc),
                            guard_name=self.name,
                        ),
                    )
                log.warning("실행이 필요한 감시가 재작성 뒤에도 남아 그대로 등록한다 : %s", desc)
            stripped = WATCH_RE.sub("", body).rstrip()
            return GuardResult(
                body=stripped, changed=True, detail={"watch_desc": desc, "watch_due": due},
            )

        if not PROMISE_WITHOUT_WATCH_RE.search(body or ""):
            return GuardResult(body=body, changed=False)

        if ctx.is_rewrite_retry:
            # Still has an untagged promise after a retry. Don't send it as-is —
            # cut the promise text out and append the standard fallback line.
            cut = PROMISE_WITHOUT_WATCH_RE.search(body)
            trimmed = body[: cut.start()].rstrip() if cut else body
            trimmed += (
                "\n\n확인이 더 필요하면 다시 말씀해 주세요."
                if trimmed
                else "확인이 더 필요하면 다시 말씀해 주세요."
            )
            return GuardResult(
                body=trimmed,
                changed=True,
                detail={"promise_without_watch_retry_failed": True},
            )

        # First occurrence: don't fix it here, ask the model to rewrite instead.
        return GuardResult(
            body=body,
            changed=False,
            detail={"promise_without_watch": True},
            rerun=RerunRequest(
                reason="promise_without_watch",
                rewrite_prompt=promise_without_watch_prompt(body),
                guard_name=self.name,
            ),
        )
