"""review/postmortem.py 의 PostmortemTask 테스트.

프롬프트·머리말 문구는 원본 `_run_postmortem()` 의 리터럴을 그대로 옮겨
검증한다. `missing_split_prompt()` 의 기대값은 원본 함수를 AST 추출해
실제로 실행해서 얻었다(POSTMORTEM_SPLIT = "===상세===" 를 주입한 네임스페이스에서
실행).
"""

from __future__ import annotations

from typing import Any

from review_support import assert_header_is_one_table

from slack_cli_agent.review.base import ReviewTarget
from slack_cli_agent.review.postmortem import PostmortemTask


def make_task(**overrides) -> PostmortemTask:
    kwargs: dict[str, Any] = {
        "ledger": None,
        "message_lookup": None,
        "transcript": None,
        "answer_finder": None,
        "reactions": None,
        "permalinks": None,
        "publisher": None,
        "engine": None,
        "troubleshoot_channel": "TS",
        "bot_display_name": "테스트봇",
        "code_dir": "/code",
        "persona_dir": "/persona",
    }
    kwargs.update(overrides)
    return PostmortemTask(**kwargs)


class Test클래스변수:
    def test_log_name은postmortem이다(self) -> None:
        assert PostmortemTask.log_name == "postmortem"

    def test_구분선없으면재시도한다(self) -> None:
        task = make_task()
        assert task.retry_on_missing_split() is True


class Test프롬프트:
    def test_대화록과지목한답변을담는다(self) -> None:
        task = make_task()
        target = ReviewTarget(channel="C1", ts="1.1", by_user="U1", channel_name="회의방", rich=True)
        prompt = task.build_prompt(target, transcript="[대화록]", flagged="지목한 답변", question="")
        assert "회의방" in prompt
        assert "[대화록]" in prompt
        assert "지목한 답변" in prompt
        assert "테스트봇" in prompt
        assert "/code" in prompt
        assert "/persona" in prompt
        # 그 답을 부른 요청이 없으면 그 절 자체를 안 넣는다.
        assert "그 답을 부른 요청" not in prompt

    def test_그답을부른요청이있으면함께담는다(self) -> None:
        task = make_task()
        target = ReviewTarget(channel="C1", ts="1.1", by_user="U1", channel_name="회의방", rich=True)
        prompt = task.build_prompt(target, transcript="[대화록]", flagged="답변", question="원 질문")
        assert "그 답을 부른 요청" in prompt
        assert "원 질문" in prompt


class Test머리말:
    def test_실행기록이있으면모델과소요를보인다(self) -> None:
        task = make_task()
        target = ReviewTarget(channel="C1", ts="1.1", by_user="U1", channel_name="회의방", rich=True)
        record = {"model": "opus", "effort": "high", "elapsed": 12.3, "num_turns": 4}
        header = task.build_header(target, record, "https://slack/x")
        assert "## 부검 : 회의방" in header
        assert "opus / high" in header
        assert "12.3초, 4턴" in header
        assert "https://slack/x" in header
        assert "<@U1>" in header

    def test_실행기록이없으면못찾았다고적는다(self) -> None:
        task = make_task()
        target = ReviewTarget(channel="C1", ts="1.1", by_user="U1", channel_name="회의방", rich=True)
        header = task.build_header(target, None, "")
        assert "감사 기록에서 이 답변을 찾지 못해 뺐습니다" in header

    def test_지적자를표안에한형태로보인다(self) -> None:
        task = make_task()
        target = ReviewTarget(channel="C1", ts="1.1", by_user="U1", channel_name="회의방", rich=True)
        header = task.build_header(target, {"model": "opus", "effort": "high"}, "https://slack/x")
        assert "| 지적한 사람 | <@U1> |" in header
        assert_header_is_one_table(header)


class Test구분선없을때재시도프롬프트:
    def test_missing_split_prompt는원본과같다(self) -> None:
        task = make_task()
        assert task.missing_split_prompt() == (
            "방금 낸 부검에 `===상세===` 구분선이 없다.\n"
            "요약과 상세를 가르지 못해 그대로 올리면 채널이 긴 보고서로 덮인다.\n\n"
            "같은 내용을 시스템 프롬프트의 부검 형식대로 다시 쓴다.\n"
            "구분선 앞에는 요약을, 뒤에는 상세를 둔다.\n"
            "내용을 새로 지어내지 않는다. 방금 쓴 것을 형식에 맞춰 다시 담는다."
        )
