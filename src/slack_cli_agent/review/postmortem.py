# Only this review kind retries when the `===상세===` divider is missing --
# without it, summary and detail can't be split and a long report floods
# the channel.

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from slack_cli_agent.review.base import ReviewTarget, ReviewTask, run_info_rows


class PostmortemTask(ReviewTask):

    log_name: ClassVar[str] = "postmortem"
    emoji: ClassVar[str] = "dango"
    requester_label: ClassVar[str] = "지적한 사람"

    def __init__(self, *, bot_display_name: str, code_dir: str, persona_dir: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._bot_display_name = bot_display_name
        self._code_dir = code_dir
        self._persona_dir = persona_dir

    def retry_on_missing_split(self) -> bool:
        return True

    def missing_split_prompt(self) -> str:
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

    def header_title(self, target: ReviewTarget) -> str:
        return f"부검 : {target.channel_name}"

    def header_rows(
        self, target: ReviewTarget, record: Mapping[str, Any] | None, link: str
    ) -> list[tuple[str, str]]:
        rows: list[tuple[str, str]] = [("대화", target.channel_name)]
        if link:
            rows.append(("대상 답변", link))
        rows.extend(run_info_rows(record))
        return rows

    def not_ok_message(self, body: str) -> str:
        return f"부검을 마치지 못했습니다. {body}"

    def stopped_title(self) -> str:
        return "부검 중단"

    def retry_hint(self) -> str:
        return "다시 경단을 붙이면 재시도합니다."
