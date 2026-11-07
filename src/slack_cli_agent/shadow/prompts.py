"""Prompts for the blind answer and the next-day verdict (bot.py
build_shadow_answer_prompt, build_shadow_judge_prompt). Only the blind rules
live in code; how to word the answer belongs to the channel's guidance."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any


def answer_prompt(
    *, bot: str, source_id: str, source_name: str, question: str, author: str,
    asked_at: datetime, files: Sequence[Mapping[str, Any]] = (),
) -> str:
    files_note = ""
    if files:
        names = ", ".join(str(f.get("name") or "파일") for f in files)
        files_note = f"첨부 : {names}. 첨부 내용은 열지 않았다. 본문만으로 답한다.\n"
    day = asked_at.strftime("%Y-%m-%d")
    return (
        f"아래는 {source_name} 채널에 {asked_at:%Y-%m-%d %H:%M} KST 에 올라온 질문이다. "
        f"그 채널 담당자가 답하기 전 시점이라고 보고, {bot}가 독자적으로 답한다.\n"
        f"작성자 : {author}\n{files_note}"
        f"질문 :\n{question}\n\n"
        "블라인드 규칙. 이 답은 실제 답과 다음 날 대조해 정확도를 재는 시험이다.\n"
        f"- 원본 채널 {source_id} 의 이 질문 스레드와 그 뒤 글을 열지 않는다. "
        "링크 읽기, 스레드 답글 조회, 채널 기록 조회를 이 채널에 쓰지 않는다\n"
        f"- 슬랙 검색을 쓸 때는 검색어에 반드시 before:{day} 를 붙인다. "
        "질문 당일 이후 글이 섞이지 않게 한다\n"
        "- 노션, 지라, 사내 문서, 웹 검색은 자유롭게 쓴다. "
        "과거의 비슷한 질문과 답은 답을 찾는 데 써도 된다\n\n"
        "쓰는 방식은 이 채널 지침의 섀도 테스트 답변 절을 따른다. "
        "그 절이 시스템 지침의 구조, 섹션, 근거 제시 규칙보다 우선한다\n"
        "- 수신자 표식은 [[RECIPIENT:ASKER]] 로 둔다"
    )


def judge_prompt(*, bot: str, question: str, answer: str, replies: str) -> str:
    return (
        f"어제 원본 채널 질문에 {bot}가 실제 답을 보지 않고 낸 답이 있다. "
        "원본 스레드의 실제 답과 대조해 판정한다.\n\n"
        f"질문 :\n{question}\n\n{bot} 답 :\n{answer}\n\n원본 스레드의 실제 답 :\n{replies}\n\n"
        "판정 기준\n"
        "- 맞음 : 실제 답과 결론이 같고 질문자가 그대로 따라도 문제가 없다\n"
        "- 일부 맞음 : 결론 방향은 같으나 빠진 절차, 담당자, 수치가 있거나 틀린 세부가 섞였다\n"
        "- 틀림 : 결론이 다르거나 따르면 잘못된 행동을 하게 된다\n"
        "- 판정 불가 : 실제 답이 결론을 내지 않았거나 질문과 무관하다\n"
        "쓰는 방식\n"
        "- 첫 줄은 '판정 : 맞음' 형식으로만 쓴다\n"
        f"- 이어서 실제 답 요지 한 줄, {bot} 답과 다른 점, "
        "다음에 맞히려면 알아야 했던 사실을 짧은 불릿으로 쓴다\n"
        "- 실제 답을 낸 사람은 이름에 님을 붙여 쓰고 멘션하지 않는다\n"
        "- 도구를 쓰지 않아도 된다. 판정에 필요한 사실 확인만 한다\n"
        "- 수신자 표식은 [[RECIPIENT:ASKER]] 로 둔다"
    )
