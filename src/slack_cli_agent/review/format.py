# `post_rich_command` is passed in pre-assembled (e.g. "python3
# /path/post_rich.py --profile example") since the script path and profile
# name are deployment config this package has no business knowing --
# only "--channel --update" gets appended here.

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from slack_cli_agent.review.base import ReviewTarget, ReviewTask, as_table, model_effort_cell


class FormatReviewTask(ReviewTask):

    log_name: ClassVar[str] = "format_review"
    emoji: ClassVar[str] = "pencil2"

    def __init__(
        self,
        *,
        bot_display_name: str,
        persona_dir: str,
        prompts_dir: str,
        post_rich_command: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._bot_display_name = bot_display_name
        self._persona_dir = persona_dir
        self._prompts_dir = prompts_dir
        self._post_rich_command = post_rich_command

    def _rich_label(self, target: ReviewTarget) -> str:
        return "리치" if target.rich else "평문"

    def build_prompt(self, target: ReviewTarget, *, transcript: str, flagged: str, question: str) -> str:
        return (
            f"아래는 {target.channel_name} 에서 {self._bot_display_name}가 낸 답변이다. "
            "이 답변의 서식만 점검한다.\n"
            "내용이 맞았는지 틀렸는지는 보지 않는다. 사실관계를 다시 조사하지 않는다.\n\n"
            + (
                f"===== 그 답을 부른 요청 =====\n\n{question}\n\n===== 요청 끝 =====\n\n"
                if question
                else ""
            )
            + "===== 지목한 답변 원문 =====\n\n"
            f"{flagged}\n\n"
            "===== 답변 끝 =====\n\n"
            "이 답변의 서식 점검을 낸다. 형식은 시스템 프롬프트에 적힌 서식 점검 형식을 "
            "그대로 따른다. 여기서 형식을 새로 정하지 않는다.\n\n"
            f"이 답변이 걸린 채널은 {target.channel_name} 이고, 슬랙 표기 규약은 "
            f"{self._rich_label(target)} 이다.\n"
            f"지침은 {self._persona_dir} 와 {self._prompts_dir} 에 있다. "
            "어느 절을 어겼는지는 그 파일을 실제로 열어 확인하고 파일 이름과 절 제목을 짚는다.\n\n"
            "위반을 찾았으면 지적으로 끝내지 않는다. 교정본을 파일로 쓴 뒤 아래 명령으로 "
            "원 메시지를 고친다. 새 메시지를 올리지 않는다.\n"
            f"{self._post_rich_command} --channel {target.channel} --update {target.ts} <교정본 파일>\n"
            "교정 범위는 서식뿐이다. 사실, 수치, 판단, 결론을 바꾸지 않는다. "
            "실행 결과의 ok 와 blocks 종류를 확인하고 리포트의 교정했습니다 절에 적는다."
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
            rows.append(("표기", self._rich_label(target)))
        else:
            rows.append(("실행 정보", "감사 기록에서 이 답변을 찾지 못해 뺐습니다"))

        return (
            f"## 서식 점검 : {target.channel_name}\n\n"
            + as_table(rows)
            + f"\n\n요청한 사람 : <@{target.by_user}>\n\n"
        )

    def not_ok_message(self, body: str) -> str:
        return f"서식 점검을 내지 못했습니다. {body}"

    def stopped_title(self) -> str:
        return "서식 점검 중단"

    def retry_hint(self) -> str:
        return "다시 연필을 붙이면 재시도합니다."
