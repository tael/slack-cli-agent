"""Detects the [[WATCH:]] tag and empty follow-up promises without one.

`ELAPSED_LINE`/`ELAPSED_MODEL_LINE` actually live in `core.markers` (the
Slack layer needs them too and shouldn't depend on the guard layer), but are
re-exported here to keep existing `guard.watch.ELAPSED_LINE` imports working.
"""

from __future__ import annotations

import re
from typing import ClassVar

# Must stay the same object as core.markers' — re-exported so a duplicate
# regex doesn't drift out of sync if only one copy gets edited.
from slack_cli_agent.core.markers import ELAPSED_LINE, ELAPSED_MODEL_LINE
from slack_cli_agent.guard.base import GuardContext, GuardResult, OutputGuard, RerunRequest

# Declared to make the re-export explicit; otherwise linters flag it as unused.
__all__ = ["ELAPSED_LINE", "ELAPSED_MODEL_LINE"]

# Internal markers the model returns with its check result; never shown to the user.
WATCH_DONE_TAG = "[[WATCH_DONE]]"
WATCH_STILL_TAG = "[[WATCH_STILL]]"
# Reaction left on a watched message. Kept distinct from in-progress/done
# marks so a completed watch isn't picked up again as unfinished.
WATCH_MARK_EMOJI = "mag"
# Only the final line counts, to avoid picking up a watch mention elsewhere in the body.
WATCH_RE = re.compile(r"\n{0,2}\[\[WATCH:\s*(.+?)\s*\]\]\s*\Z", re.DOTALL)

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
            desc = m.group(1).strip()
            stripped = WATCH_RE.sub("", body).rstrip()
            return GuardResult(body=stripped, changed=True, detail={"watch_desc": desc})

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
