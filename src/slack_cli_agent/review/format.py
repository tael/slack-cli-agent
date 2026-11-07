# `rewrite_command` is passed in pre-assembled (e.g. "/path/python -m
# slack_cli_agent.cli rewrite --profile example") since the interpreter and
# the profile are deployment config this package has no business knowing --
# only "--channel --update" gets appended here.

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from slack_cli_agent.review.base import ReviewTarget, ReviewTask, run_info_rows


class FormatReviewTask(ReviewTask):

    log_name: ClassVar[str] = "format_review"
    emoji: ClassVar[str] = "pencil2"

    def __init__(
        self,
        *,
        bot_display_name: str,
        persona_dir: str,
        prompts_dir: str,
        rewrite_command: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._bot_display_name = bot_display_name
        self._persona_dir = persona_dir
        self._prompts_dir = prompts_dir
        self._rewrite_command = rewrite_command

    def _rich_label(self, target: ReviewTarget) -> str:
        return "리치" if target.rich else "평문"

    def prompt_extras(self, record: Mapping[str, Any] | None) -> dict[str, Any]:
        return {"record": record}

    def build_prompt(
        self,
        target: ReviewTarget,
        *,
        transcript: str,
        flagged: str,
        question: str,
        record: Mapping[str, Any] | None = None,
    ) -> str:
        # On a rich channel the message text Slack hands back is the
        # notification preview, not the stored body. Rewriting from it wipes
        # the message, so the stored answer is the source, and without one
        # the review points things out but never runs --update
        # (bot.py _run_format_review, 2026-10-19).
        answer = (record or {}).get("answer") or ""
        body_src = answer or flagged
        # A long rich answer posts as several messages and the record holds
        # the whole body. --update rewrites one message, so writing the
        # whole correction into one chunk would repeat the others and break
        # the thread (B6 review).
        parts = len((record or {}).get("answer_parts") or ())
        if answer and parts > 1:
            fix_rule = (
                f"이 답변은 {parts}개 메시지로 나뉘어 올라갔고 위 본문은 그 전체다. "
                "메시지 하나만 고치는 명령으로는 교정할 수 없으므로 원 메시지를 고치지 않는다. "
                "지적만 하고, 리포트의 교정했습니다 절에는 여러 메시지로 나뉜 답이라 "
                "교정하지 않았다고 적는다."
            )
        elif answer:
            fix_rule = (
                "위반을 찾았으면 지적으로 끝내지 않는다. 교정본을 파일로 쓴 뒤 아래 명령으로 "
                "원 메시지를 고친다. 새 메시지를 올리지 않는다.\n"
                f"{self._rewrite_command} --channel {target.channel} --update {target.ts} <교정본 파일>\n"
                "교정 범위는 서식뿐이다. 사실, 수치, 판단, 결론을 바꾸지 않는다. "
                "실행 결과의 ok 와 blocks 종류를 확인하고 리포트의 교정했습니다 절에 적는다."
            )
        else:
            fix_rule = (
                "이 답변의 원문 기록을 찾지 못해 위 본문은 알림 미리보기 한 줄뿐일 수 있다. "
                "원 메시지를 고치지 않는다. 고치면 본문이 지워진다. 지적만 하고, "
                "리포트의 교정했습니다 절에는 원문을 찾지 못해 교정하지 않았다고 적는다."
            )
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
            f"{body_src}\n\n"
            "===== 답변 끝 =====\n\n"
            "이 답변의 서식 점검을 낸다. 형식은 시스템 프롬프트에 적힌 서식 점검 형식을 "
            "그대로 따른다. 여기서 형식을 새로 정하지 않는다.\n\n"
            f"이 답변이 걸린 채널은 {target.channel_name} 이고, 슬랙 표기 규약은 "
            f"{self._rich_label(target)} 이다.\n"
            f"지침은 {self._persona_dir} 와 {self._prompts_dir} 에 있다. "
            "어느 절을 어겼는지는 그 파일을 실제로 열어 확인하고 파일 이름과 절 제목을 짚는다.\n\n"
            + fix_rule
        )

    def header_title(self, target: ReviewTarget) -> str:
        return f"서식 점검 : {target.channel_name}"

    def header_rows(
        self, target: ReviewTarget, record: Mapping[str, Any] | None, link: str
    ) -> list[tuple[str, str]]:
        rows: list[tuple[str, str]] = [("대화", target.channel_name)]
        if link:
            rows.append(("대상 답변", link))
        rows.extend(run_info_rows(record))
        # Which notation the channel uses is what this review judges against,
        # so it belongs in the header whether or not the run record was found.
        rows.append(("표기", self._rich_label(target)))
        return rows

    def not_ok_message(self, body: str) -> str:
        return f"서식 점검을 내지 못했습니다. {body}"

    def stopped_title(self) -> str:
        return "서식 점검 중단"

    def retry_hint(self) -> str:
        return "다시 연필을 붙이면 재시도합니다."
