"""review/format.py 의 FormatReviewTask 테스트.

원본 bot.py 의 `_run_format_review()` 를 옮겼다. 내용이
맞았는지는 보지 않고 서식만 본다.

`usage_rows()`(토큰·세션 사용량 행)는 옮기지 않았다. 그 값은
`observability/progress.py` 소관 자원(세션 맥락 조회)에 의존하는데, 그
모듈은 이 작업과 동시에 다른 담당이 만들고 있어 이 패키지가 건드릴 수
없다. 있으면 좋은 부가 정보이지 서식 점검의 핵심 기능이 아니므로 없이도
점검 결과의 신뢰성에는 영향이 없다.
"""

from __future__ import annotations

from typing import Any

from review_support import assert_header_is_one_table

from slack_cli_agent.review.base import ReviewTarget
from slack_cli_agent.review.format import FormatReviewTask


def make_task(**overrides) -> FormatReviewTask:
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
        "persona_dir": "/persona",
        "prompts_dir": "/prompts",
        "post_rich_command": "python3 /tools/post_rich.py --profile example",
    }
    kwargs.update(overrides)
    return FormatReviewTask(**kwargs)


class Test클래스변수:
    def test_log_name은format_review이다(self) -> None:
        assert FormatReviewTask.log_name == "format_review"

    def test_구분선없어도재시도하지않는다(self) -> None:
        assert make_task().retry_on_missing_split() is False


class Test프롬프트:
    def test_리치채널표기를담는다(self) -> None:
        task = make_task()
        target = ReviewTarget(channel="C1", ts="1.1", by_user="U1", channel_name="회의방", rich=True)
        prompt = task.build_prompt(target, transcript="", flagged="답변", question="")
        assert "리치" in prompt
        assert "내용이 맞았는지 틀렸는지는 보지 않는다" in prompt
        assert "/prompts" in prompt
        assert "/persona" in prompt
        assert "python3 /tools/post_rich.py --profile example --channel C1 --update 1.1" in prompt

    def test_평문채널표기를담는다(self) -> None:
        task = make_task()
        target = ReviewTarget(channel="C1", ts="1.1", by_user="U1", channel_name="회의방", rich=False)
        prompt = task.build_prompt(target, transcript="", flagged="답변", question="")
        assert "평문" in prompt


class Test머리말:
    def test_실행기록이있으면표기행을함께보인다(self) -> None:
        task = make_task()
        target = ReviewTarget(channel="C1", ts="1.1", by_user="U1", channel_name="회의방", rich=True)
        record = {"model": "opus", "effort": "high", "elapsed": 3.0}
        header = task.build_header(target, record, "")
        assert "## 서식 점검 : 회의방" in header
        assert "리치" in header
        assert "opus / high" in header

    def test_실행기록이없으면못찾았다고적는다(self) -> None:
        task = make_task()
        target = ReviewTarget(channel="C1", ts="1.1", by_user="U1", channel_name="회의방", rich=True)
        header = task.build_header(target, None, "")
        assert "감사 기록에서 이 답변을 찾지 못해 뺐습니다" in header
        # 표기는 이 점검의 판정 기준이므로 실행 기록 유무와 무관하게 넣는다.
        assert "리치" in header

    def test_요청자를표안에한형태로보인다(self) -> None:
        task = make_task()
        target = ReviewTarget(channel="C1", ts="1.1", by_user="U1", channel_name="회의방", rich=True)
        header = task.build_header(target, {"model": "opus", "effort": "high"}, "https://slack/x")
        assert "| 요청한 사람 | <@U1> |" in header
        assert_header_is_one_table(header)
