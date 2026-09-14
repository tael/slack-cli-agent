"""디버그 추적 — 답변 하나가 무엇을 입력받아 어떻게 판단해 무엇을 냈는지만 보여준다.

원본 bot.py 의 `_run_debug_trace()` 를 옮겼다. 부검과
달리 무엇이 잘못됐는지 판단하지 않는다.

원본은 요청자 이름을 문자열로 박아 뒀다 — 이 봇을 실제로 운영하는 조직의
고유값이라 새 코드베이스에 그대로 옮기면 조직 종속 코드가 된다. 대신
`owner_display_name` 생성자 인자로 받는다.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from slack_cli_agent.review.base import ReviewTarget, ReviewTask


class DebugTraceTask(ReviewTask):
    """리액션 "brain" 으로 시작하는 디버그 추적."""

    log_name: ClassVar[str] = "debug_trace"
    emoji: ClassVar[str] = "brain"

    def __init__(
        self,
        *,
        bot_display_name: str,
        code_dir: str,
        persona_dir: str,
        owner_display_name: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._bot_display_name = bot_display_name
        self._code_dir = code_dir
        self._persona_dir = persona_dir
        self._owner_display_name = owner_display_name

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
            + f"{self._owner_display_name}이 {self._bot_display_name}가 낸 아래 답변의 디버깅 흐름을 보고 싶어한다.\n"
            "잘못됐다는 지적이 아니다. 무엇을 입력받아 무엇을 판단해 무엇을 냈는지 궁금한 것이다.\n\n"
            "===== 지목한 답변 =====\n\n"
            f"{flagged}\n\n"
            "===== 답변 끝 =====\n\n"
            "이 답변의 디버그 흐름을 낸다. 형식은 시스템 프롬프트에 적힌 디버그 흐름 형식을 그대로 따른다.\n"
            "여기서 형식을 새로 정하지 않는다.\n\n"
            f"{self._bot_display_name}의 코드는 {self._code_dir} 에, "
            f"지침은 {self._persona_dir} 에 있다.\n"
            "어느 지침과 어느 판단이 이 답을 만들었는지는 그 파일을 실제로 열어 확인하고 "
            "파일 이름과 함수 이름을 짚는다."
        )

    def build_header(self, target: ReviewTarget, record: Mapping[str, Any] | None, link: str) -> str:
        return (
            f"## 디버그 : {target.channel_name}\n\n"
            f"- 요청한 사람 : <@{target.by_user}>\n"
            + (f"- 대상 답변 : {link}\n" if link else "")
            + "\n"
        )

    def not_ok_message(self, body: str) -> str:
        return f"디버그 흐름을 내지 못했습니다. {body}"

    def stopped_title(self) -> str:
        return "디버그 흐름 중단"

    def retry_hint(self) -> str:
        return "다시 뇌를 붙이면 재시도합니다."
