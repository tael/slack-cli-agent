"""[[WATCH:]] 태그 판정과 빈 약속 검출.

원본 `WATCH_RE`, `PROMISE_WITHOUT_WATCH_RE` 를 그대로 옮겼다.
`ELAPSED_LINE`, `ELAPSED_MODEL_LINE` 은 발신 직전 보정용 정규식이라
슬랙 계층에서도 쓰는데, 슬랙 계층이 가드 계층을 import 할 이유가 없어
`core.markers` 로 옮겼다. 기존 import 경로(`guard.watch.ELAPSED_LINE` 등)를
유지하려고 여기서 재노출한다.
"""

from __future__ import annotations

import re
from typing import ClassVar

from slack_cli_agent.core.markers import ELAPSED_LINE, ELAPSED_MODEL_LINE
from slack_cli_agent.guard.base import GuardContext, GuardResult, OutputGuard, RerunRequest

# 모델이 확인 결과를 담아 돌려주는 표식. 사용자에게는 보이지 않고 여기서만 본다.
WATCH_DONE_TAG = "[[WATCH_DONE]]"
WATCH_STILL_TAG = "[[WATCH_STILL]]"
# 감시 중인 메시지에 남기는 표식. 처리중(eyes)도 완료(white_check_mark)도 아닌
# 제3의 상태라 따로 둔다. 이게 없으면 완료 표식이 먼저 붙어 미완료 복구 대상에서 빠진다.
WATCH_MARK_EMOJI = "mag"
# 모델이 "지켜보겠다"고 답한 마지막 줄에서 감시 대상을 뽑는다. 맨 끝 한 줄로만 인정한다.
WATCH_RE = re.compile(r"\n{0,2}\[\[WATCH:\s*(.+?)\s*\]\]\s*\Z", re.DOTALL)

# 2026-09-01 재발방지 : WATCH_NOTE 지침이 시스템 프롬프트에 있어도 모델이
# [[WATCH: ...]] 태그 없이 "지켜보겠다/끝나면 보고하겠다"는 말만 하고 끝내는
# 사례가 원본 운영에서 반복 관측됐다.
# 이러면 job_watch 큐에 아무것도 안 들어가 실제로는 아무도 다시 안 본다.
# 태그가 없는데 이 패턴이 잡히면 코드가 직접 보정한다. 프롬프트 문구만으로는
# 재발을 막지 못한다는 것이 이번 사건의 근본원인이다.
PROMISE_WITHOUT_WATCH_RE = re.compile(
    r"(지켜보|끝나면|완료되면|확인되면|반영되면|반영후|배포\s*후)"
    r".{0,20}(보고|말씀|알려|다시\s*답)"
)


def promise_without_watch_prompt(body: str) -> str:
    """[[WATCH: ...]] 태그 없이 빈 약속만 한 답을 바로잡게 하는 재작성 요청문.

    2026-09-01 재발방지. 방금 쓴 답이 아래 둘 중 하나가 되도록 다시 쓰게 한다.
    지켜볼 대상이 실제로 있으면 태그를 붙이고, 없으면 약속하는 말 자체를 뺀다.
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
    """[[WATCH:]] 태그를 등록 대상으로 뽑아내고, 태그 없는 빈 약속은 다시 쓰게 한다.

    실제 감시 큐 등록(register_watch_job)과 엔진 재호출은 이 클래스의 책임이
    아니다. 등록에 쓸 값은 `detail["watch_desc"]` 로, 재작성이 필요하면
    `rerun` 으로 돌려준다.
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
            # 재시도에도 빈 약속이 남아 있다. 그대로 내보내지 않는다.
            # 지켜보겠다는 말 자체를 코드로 잘라내고 표준 대체 문구를 붙인다.
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

        # 태그 없이 "지켜보겠다/끝나면 보고하겠다"만 하고 끝난 경우. 여기서
        # 직접 고치지 않는다 — 모델에게 다시 쓰게 해야 한다.
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
