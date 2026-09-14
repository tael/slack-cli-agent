"""부검 — 지적받은 답변이 왜 잘못됐고 어떻게 고칠지를 낸다.

원본 bot.py 의 `_run_postmortem()` 이 만들던 프롬프트·
머리말·재시도 요청을 그대로 옮겼다. 구분선(`===상세===`)이 없으면 형식을
지켜 다시 쓰게 하는 것은 세 점검 중 부검만 하던 것이라 `retry_on_missing_split()`
을 여기서만 켠다.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from slack_cli_agent.review.base import ReviewTarget, ReviewTask, as_table, model_effort_cell


class PostmortemTask(ReviewTask):
    """리액션 "dango" 로 시작하는 부검."""

    log_name: ClassVar[str] = "postmortem"
    emoji: ClassVar[str] = "dango"

    def __init__(self, *, bot_display_name: str, code_dir: str, persona_dir: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._bot_display_name = bot_display_name
        self._code_dir = code_dir
        self._persona_dir = persona_dir

    def retry_on_missing_split(self) -> bool:
        return True

    def missing_split_prompt(self) -> str:
        """형식을 지키지 않은 부검을 다시 쓰게 하는 요청. 문구는 원본 그대로다."""
        return (
            "방금 낸 부검에 `===상세===` 구분선이 없다.\n"
            "요약과 상세를 가르지 못해 그대로 올리면 채널이 긴 보고서로 덮인다.\n\n"
            "같은 내용을 시스템 프롬프트의 부검 형식대로 다시 쓴다.\n"
            "구분선 앞에는 요약을, 뒤에는 상세를 둔다.\n"
            "내용을 새로 지어내지 않는다. 방금 쓴 것을 형식에 맞춰 다시 담는다."
        )

    def build_prompt(self, target: ReviewTarget, *, transcript: str, flagged: str, question: str) -> str:
        return (
            f"아래는 {target.channel_name} 에서 오간 대화다. 대괄호 안이 시각과 그 말을 한 사람이다.\n\n"
            "===== 대화 =====\n\n"
            f"{transcript}\n\n"
            "===== 대화 끝 =====\n\n"
            + (
                f"===== 그 답을 부른 요청 =====\n\n{question}\n\n===== 요청 끝 =====\n\n"
                if question
                else ""
            )
            + f"이 중 {self._bot_display_name}가 낸 아래 답변에 사람이 문제가 있다고 표시했다.\n\n"
            "===== 지적받은 답변 =====\n\n"
            f"{flagged}\n\n"
            "===== 답변 끝 =====\n\n"
            "이 답변을 부검한다. 형식은 시스템 프롬프트에 적힌 부검 형식을 그대로 따른다.\n"
            "여기서 형식을 새로 정하지 않는다.\n\n"
            f"{self._bot_display_name}의 코드는 {self._code_dir} 에, "
            f"지침은 {self._persona_dir} 에 있다.\n"
            "왜 그렇게 했는지는 그 파일을 실제로 열어 확인하고 "
            "파일 이름과 함수 이름을 짚는다."
        )

    def build_header(self, target: ReviewTarget, record: Mapping[str, Any] | None, link: str) -> str:
        rows: list[tuple[str, str]] = [("대화", target.channel_name)]
        if link:
            rows.append(("대상 답변", link))
        if record:
            rows.append(("모델 / effort", model_effort_cell(record)))
            elapsed = f"{record.get('elapsed') or 0:.1f}초"
            if record.get("num_turns"):
                elapsed += f", {record['num_turns']}턴"
            rows.append(("소요", elapsed))
        else:
            rows.append(("실행 정보", "감사 기록에서 이 답변을 찾지 못해 뺐습니다"))

        return (
            f"## 부검 : {target.channel_name}\n\n"
            + as_table(rows)
            + f"\n\n지적한 사람 : <@{target.by_user}>\n\n"
        )

    def not_ok_message(self, body: str) -> str:
        return f"부검을 마치지 못했습니다. {body}"

    def stopped_title(self) -> str:
        return "부검 중단"

    def retry_hint(self) -> str:
        return "다시 경단을 붙이면 재시도합니다."
