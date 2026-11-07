"""review/records.py 특성화 테스트.

원본 bot.py 의 clean_excerpt(), find_answer_record() 를
옮긴 clean_excerpt() 와 AnswerRecordFinder 를 검증한다.
clean_excerpt 의 기대값은 원본 함수를 AST 추출해 실제로 실행해서 얻었다.

find_answer_record() 원본은 audit.jsonl 을 뒤에서 4000줄 읽어 channel 이
같고 answer 필드가 있는 기록만 추려 최신 순으로 본문 대조하고, 못 찾으면
같은 스레드의 마지막 성공 기록으로 물러선다. 여기서는 jsonl 대신 audit
DB 테이블(kind='request')을 조회해 같은 알고리즘을 재현한다.
"""

from __future__ import annotations

from slack_cli_agent.observability.audit import AuditLog
from slack_cli_agent.review.records import AnswerRecordFinder
from slack_cli_agent.slack.text import clean_excerpt


class Test본문정리:
    def test_엔티티를풀고링크는라벨만남긴다(self) -> None:
        원문 = "&lt;안녕&gt; <https://x.com|라벨> <https://y.com>"
        assert clean_excerpt(원문) == "안녕 라벨 https://y.com"

    def test_빈문자열은그대로다(self) -> None:
        assert clean_excerpt("") == ""

    def test_None은그대로다(self) -> None:
        assert clean_excerpt(None) is None


class Test답변기록찾기:
    def _finder(self, database, tmp_path) -> tuple[AnswerRecordFinder, AuditLog]:
        audit = AuditLog(database, tmp_path / "audit.jsonl")
        return AnswerRecordFinder(database), audit

    def test_본문대조로찾는다(self, database, tmp_path) -> None:
        finder, audit = self._finder(database, tmp_path)
        audit.record_request(
            channel="C1", thread_ts="T1", message_ts="1.1", session_id="s1",
            resumed=False, model="m1", effort="low", elapsed=1.0, ok=True,
            answer="이것이 첫 번째 답변 본문이다",
        )
        audit.record_request(
            channel="C1", thread_ts="T1", message_ts="1.2", session_id="s1",
            resumed=False, model="m2", effort="high", elapsed=2.0, ok=True,
            answer="이것이 두 번째 답변 본문이다",
        )
        rec = finder.find("C1", "T1", "두 번째 답변 본문이다")
        assert rec is not None
        assert rec["model"] == "m2"

    def test_본문도시각도안맞으면같은스레드기록으로물러서지않는다(self, database, tmp_path) -> None:
        """2026-10-19 사례 : 미리보기가 안 맞아 스레드 마지막 기록으로 물러섰고,
        서식 점검이 그 다른 답의 원문으로 지목한 메시지를 덮어썼다."""
        finder, audit = self._finder(database, tmp_path)
        audit.record_request(
            channel="C1", thread_ts="T1", message_ts="1.1", session_id="s1",
            resumed=False, model="m1", effort="low", elapsed=1.0, ok=True,
            answer="예전 답변",
        )
        assert finder.find("C1", "T1", "본문 안에 전혀 없는 글") is None

    def test_채널이다르면못찾는다(self, database, tmp_path) -> None:
        finder, audit = self._finder(database, tmp_path)
        audit.record_request(
            channel="C2", thread_ts="T1", message_ts="1.1", session_id="s1",
            resumed=False, model="m1", effort="low", elapsed=1.0, ok=True,
            answer="다른 채널 답변",
        )
        assert finder.find("C1", "T1", "다른 채널 답변") is None

    def test_answer필드없는기록은대상에서빠진다(self, database, tmp_path) -> None:
        finder, audit = self._finder(database, tmp_path)
        audit.record_request(
            channel="C1", thread_ts="T1", message_ts="1.1", session_id="s1",
            resumed=False, model="m1", effort="low", elapsed=1.0, ok=True,
        )
        assert finder.find("C1", "T1", "아무 글") is None

    def test_아무것도없으면None이다(self, database, tmp_path) -> None:
        finder, _audit = self._finder(database, tmp_path)
        assert finder.find("C1", "T1", "글") is None


class Test답변기록찾기_정렬:
    """2026-10-19 판 find_answer_record 와 같은 규칙을 검증한다."""

    def _audit(self, database, tmp_path, clock):
        return AuditLog(database, tmp_path / "audit.jsonl", now=lambda: clock[0])

    def _rec(self, audit, *, thread_ts="T1", model="m1", answer="답", **extra):
        audit.record_request(
            channel="C1", thread_ts=thread_ts, message_ts="1.1", session_id="s1",
            resumed=False, model=model, effort="low", elapsed=1.0, ok=True,
            answer=answer, **extra,
        )

    def test_게시ts가같은기록을먼저고른다(self, database, tmp_path) -> None:
        clock = [1000.0]
        audit = self._audit(database, tmp_path, clock)
        self._rec(audit, model="m1", answer="첫 답", answer_ts="1000.000100")
        self._rec(audit, model="m2", answer="둘째 답", answer_ts="1000.000200")
        rec = AnswerRecordFinder(database).find("C1", "T1", "알림 미리보기", msg_ts="1000.000100")
        assert rec is not None and rec["model"] == "m1"

    def test_양쪽을같은방식으로다듬어대조한다(self, database, tmp_path) -> None:
        """뒤에 쓴 다른 답이 먼저 대조된다. 슬랙 쪽만 다듬으면 그 다른 답도,
        목표 답도 안 맞아야 하므로 고른 기록의 모델까지 본다."""
        clock = [1000.0]
        audit = self._audit(database, tmp_path, clock)
        self._rec(audit, model="m1", answer="<@U0ACANBHY3U> **장애 보고** [대시보드](https://x.com) 링크를 확인했다")
        self._rec(audit, model="m2", answer="전혀 다른 나중 답변 본문이다")
        text = "<@U0ACANBHY3U> *장애 보고* <https://x.com|대시보드> 링크를 확인했다"
        rec = AnswerRecordFinder(database).find("C1", "T1", text)
        assert rec is not None and rec["model"] == "m1"

    def test_다른스레드의같은본문은고르지않는다(self, database, tmp_path) -> None:
        clock = [1000.0]
        audit = self._audit(database, tmp_path, clock)
        self._rec(audit, thread_ts="T2", answer="이것이 다른 스레드의 답변 본문이다")
        assert AnswerRecordFinder(database).find("C1", "T1", "이것이 다른 스레드의 답변 본문이다") is None

    def test_짧은미리보기로는본문대조하지않는다(self, database, tmp_path) -> None:
        clock = [1000.0]
        audit = self._audit(database, tmp_path, clock)
        self._rec(audit, answer="네 확인했습니다 이후 상세 본문")
        assert AnswerRecordFinder(database).find("C1", "T1", "네") is None

    def test_본문이안맞으면게시시각이가까운기록으로물러선다(self, database, tmp_path) -> None:
        clock = [1000.0]
        audit = self._audit(database, tmp_path, clock)
        self._rec(audit, model="m1", answer="예전 답")
        clock[0] = 2000.0
        self._rec(audit, model="m2", answer="최근 답")
        rec = AnswerRecordFinder(database).find("C1", "T1", "알림 미리보기 한 줄", msg_ts="998.5")
        assert rec is not None and rec["model"] == "m1"

    def test_게시시각과멀면물러서지않는다(self, database, tmp_path) -> None:
        clock = [1000.0]
        audit = self._audit(database, tmp_path, clock)
        self._rec(audit, answer="예전 답")
        assert AnswerRecordFinder(database).find("C1", "T1", "알림 미리보기 한 줄", msg_ts="500.0") is None


class Test답변기록찾기_검토보완:
    """B6 PR 검토에서 재현한 오선택을 막는다."""

    def _audit(self, database, tmp_path, clock):
        return AuditLog(database, tmp_path / "audit.jsonl", now=lambda: clock[0])

    def _rec(self, audit, clock, at, *, thread_ts="T1", model="m1", answer="답", **extra):
        clock[0] = at
        audit.record_request(
            channel="C1", thread_ts=thread_ts, message_ts="1.1", session_id="s1",
            resumed=False, model=model, effort="low", elapsed=1.0, ok=True,
            answer=answer, **extra,
        )

    def test_게시ts가있는다른답은본문이나시각으로고르지않는다(self, database, tmp_path) -> None:
        """case1 : 부모 1000.0 을 지목했는데 같은 스레드 후속 답 기록이 골라졌다."""
        clock = [0.0]
        audit = self._audit(database, tmp_path, clock)
        self._rec(audit, clock, 1001.0, thread_ts="1000.000000", model="followup",
                  answer="루트 답변 본문 원래 내용과 같은 앞부분", answer_ts="1000.500000")
        rec = AnswerRecordFinder(database).find(
            "C1", "1000.000000", "루트 답변 본문 원래 내용과 같은 앞부분", msg_ts="1000.000000"
        )
        assert rec is None

    def test_메시지보다한참뒤에쓴기록은고르지않는다(self, database, tmp_path) -> None:
        """case2 : 기록 없는 감시 확인 2000.0 을 지목했는데 40초 뒤 다른 답이 골라졌다."""
        clock = [0.0]
        audit = self._audit(database, tmp_path, clock)
        self._rec(audit, clock, 2040.5, thread_ts="T2", model="later", answer="나중에 올라간 다른 답")
        assert AnswerRecordFinder(database).find("C1", "T2", "알림 미리보기 한 줄", msg_ts="2000.0") is None

    def test_게시직후에쓴기록은시각으로고른다(self, database, tmp_path) -> None:
        clock = [0.0]
        audit = self._audit(database, tmp_path, clock)
        self._rec(audit, clock, 2001.5, thread_ts="T2", model="mine", answer="이 메시지의 답")
        rec = AnswerRecordFinder(database).find("C1", "T2", "알림 미리보기 한 줄", msg_ts="2000.0")
        assert rec is not None and rec["model"] == "mine"

    def test_DM과flat답은스레드가달라도게시ts로찾는다(self, database, tmp_path) -> None:
        """최상위로 올라간 답은 지목 메시지 thread_ts 가 자기 ts 라 질문 스레드와 다르다."""
        clock = [0.0]
        audit = self._audit(database, tmp_path, clock)
        self._rec(audit, clock, 3001.0, thread_ts="2990.000000", model="dm",
                  answer="DM 답", answer_ts="3000.000000")
        rec = AnswerRecordFinder(database).find(
            "C1", "3000.000000", "알림 미리보기", msg_ts="3000.000000"
        )
        assert rec is not None and rec["model"] == "dm"

    def test_뒤조각을지목하면그답기록과조각표시를돌려준다(self, database, tmp_path) -> None:
        clock = [0.0]
        audit = self._audit(database, tmp_path, clock)
        self._rec(audit, clock, 4002.0, model="long", answer="긴 답 전체 본문",
                  answer_ts="4000.000100", answer_parts=["4000.000100", "4000.000200"])
        rec = AnswerRecordFinder(database).find("C1", "T1", "뒤 조각 미리보기", msg_ts="4000.000200")
        assert rec is not None and rec["model"] == "long"
        assert rec["answer_parts"] == ["4000.000100", "4000.000200"]
