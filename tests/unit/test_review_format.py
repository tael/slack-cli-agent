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

from typing import Any, ClassVar

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
        "rewrite_command": "/venv/bin/python -m slack_cli_agent.cli rewrite --profile example",
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
        prompt = task.build_prompt(
            target, transcript="", flagged="답변", question="", record={"answer": "답변"}
        )
        assert "리치" in prompt
        assert "내용이 맞았는지 틀렸는지는 보지 않는다" in prompt
        assert "/prompts" in prompt
        assert "/persona" in prompt
        assert "/venv/bin/python -m slack_cli_agent.cli rewrite --profile example --channel C1 --update 1.1" in prompt

    def test_원문기록이없으면교정명령을주지않는다(self) -> None:
        """기록이 없으면 본문은 알림 미리보기 한 줄이다. 그걸로 --update 하면 본문이 지워진다."""
        task = make_task()
        target = ReviewTarget(channel="C1", ts="1.1", by_user="U1", channel_name="회의방", rich=True)
        prompt = task.build_prompt(target, transcript="", flagged="미리보기", question="", record=None)
        assert "--update" not in prompt
        assert "원 메시지를 고치지 않는다" in prompt

    def test_원문기록이있으면기록answer로교정한다(self) -> None:
        task = make_task()
        target = ReviewTarget(channel="C1", ts="1.1", by_user="U1", channel_name="회의방", rich=True)
        record = {"answer": "| 표 | 원문 |\n|---|---|\n| a | b |"}
        prompt = task.build_prompt(target, transcript="", flagged="미리보기", question="", record=record)
        assert "| 표 | 원문 |" in prompt
        assert "미리보기" not in prompt.split("===== 지목한 답변 원문 =====")[1].split("===== 답변 끝")[0]
        assert "--channel C1 --update 1.1" in prompt

    def test_여러조각으로나간답이면교정명령을주지않는다(self) -> None:
        """조각 하나에 답 전체 교정본을 쓰면 첫 조각 내용이 겹치고 스레드가 무너진다."""
        task = make_task()
        target = ReviewTarget(channel="C1", ts="1.2", by_user="U1", channel_name="회의방", rich=True)
        record = {"answer": "긴 답 전체", "answer_ts": "1.1", "answer_parts": ["1.1", "1.2"]}
        prompt = task.build_prompt(target, transcript="", flagged="미리보기", question="", record=record)
        assert "--update" not in prompt
        assert "긴 답 전체" in prompt
        assert "2개 메시지" in prompt

    def test_한조각답은교정명령을준다(self) -> None:
        task = make_task()
        target = ReviewTarget(channel="C1", ts="1.1", by_user="U1", channel_name="회의방", rich=True)
        record = {"answer": "짧은 답", "answer_ts": "1.1", "answer_parts": ["1.1"]}
        prompt = task.build_prompt(target, transcript="", flagged="미리보기", question="", record=record)
        assert "--channel C1 --update 1.1" in prompt

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
        assert header.startswith("# 서식 점검 : 회의방\n")
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


class FakeRewriter:
    """MessagePublisher.update 대역. 받은 대상과 본문을 남긴다."""

    def __init__(self, fail: Exception | None = None) -> None:
        self.calls: list[tuple[str, str, str, bool]] = []
        self._fail = fail

    def update(self, channel: str, ts: str, text: str, rich: bool) -> list[str]:
        self.calls.append((channel, ts, text, rich))
        if self._fail is not None:
            raise self._fail
        return ["markdown"]


def _비소유자_대상(**overrides: Any) -> ReviewTarget:
    from slack_cli_agent.auth.principal import Principal, TrustLevel

    requester = Principal(user_id="U2", channel="C1", trust=TrustLevel.TRUSTED, is_direct_message=False)
    kwargs: dict[str, Any] = {
        "channel": "C1", "ts": "1.1", "by_user": "U2", "channel_name": "회의방",
        "rich": True, "requester": requester,
    }
    kwargs.update(overrides)
    return ReviewTarget(**kwargs)


class Test비소유자_교정:
    """비소유자 서식 점검은 명령을 실행하지 않는다. 교정본을 리포트에 담으면
    봇이 지목한 메시지 1건만 고친다 (A18 검토).

    명령 접두 허용(:*)은 --channel, --update, 파일 인자를 모두 열어 두어 다른
    봇 메시지를 덮거나 작업 디렉터리 밖 파일을 게시할 수 있었다. 쓰기 도구가
    없어 교정본 파일을 만들 수도 없었다.
    """

    _기록: ClassVar[dict[str, str]] = {"answer": "| 표 | 원문 |\n|---|---|\n| a | b |"}

    def test_프롬프트에_교정_명령을_주지_않는다(self) -> None:
        task = make_task(rewriter=FakeRewriter())
        prompt = task.build_prompt(
            _비소유자_대상(), transcript="", flagged="미리보기", question="", record=self._기록,
        )
        assert "slack_cli_agent.cli rewrite" not in prompt
        assert "--update" not in prompt
        assert "===== 교정본 =====" in prompt
        assert "명령을 실행하지 않는다" in prompt

    def test_교정본을_지목한_메시지_1건에만_쓴다(self) -> None:
        rewriter = FakeRewriter()
        task = make_task(rewriter=rewriter)
        body = (
            "## 위반\n- 표 앞 빈 줄\n\n## 교정했습니다\n"
            "===== 교정본 =====\n고친 본문\n===== 교정본 끝 =====\n===상세===상세"
        )
        out = task.finish_body(_비소유자_대상(), body, self._기록)
        assert rewriter.calls == [("C1", "1.1", "고친 본문", True)]
        assert "고친 본문" not in out
        assert "===== 교정본" not in out
        assert "원 메시지를 고쳤습니다" in out
        assert out.endswith("===상세===상세")

    def test_여러_조각_답이면_교정본이_있어도_고치지_않는다(self) -> None:
        rewriter = FakeRewriter()
        task = make_task(rewriter=rewriter)
        record = {"answer": "긴 답", "answer_parts": ["1", "2"]}
        out = task.finish_body(
            _비소유자_대상(), "===== 교정본 =====\n고친 본문\n===== 교정본 끝 =====", record,
        )
        assert rewriter.calls == []
        assert "고친 본문" not in out

    def test_원문기록이_없으면_고치지_않는다(self) -> None:
        rewriter = FakeRewriter()
        task = make_task(rewriter=rewriter)
        task.finish_body(_비소유자_대상(), "===== 교정본 =====\n고친 본문\n===== 교정본 끝 =====", None)
        assert rewriter.calls == []

    def test_갱신이_실패하면_실패를_적는다(self) -> None:
        task = make_task(rewriter=FakeRewriter(fail=RuntimeError("chat.update 거부")))
        out = task.finish_body(
            _비소유자_대상(), "===== 교정본 =====\n고친 본문\n===== 교정본 끝 =====", self._기록,
        )
        assert "고치지 못했습니다" in out
        assert "chat.update 거부" in out

    def test_교정본이_없으면_본문을_그대로_둔다(self) -> None:
        rewriter = FakeRewriter()
        task = make_task(rewriter=rewriter)
        assert task.finish_body(_비소유자_대상(), "위반 없음", self._기록) == "위반 없음"
        assert rewriter.calls == []

    def test_소유자_턴은_봇이_고치지_않는다(self) -> None:
        """소유자 턴은 지금처럼 명령으로 직접 고친다. 봇이 한 번 더 쓰지 않는다."""
        rewriter = FakeRewriter()
        task = make_task(rewriter=rewriter)
        target = ReviewTarget(channel="C1", ts="1.1", by_user="U1", channel_name="회의방", rich=True)
        body = "===== 교정본 =====\n고친 본문\n===== 교정본 끝 ====="
        assert task.finish_body(target, body, self._기록) == body
        assert rewriter.calls == []
        prompt = task.build_prompt(target, transcript="", flagged="x", question="", record=self._기록)
        assert "--channel C1 --update 1.1" in prompt
