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
from slack_cli_agent.review.records import AnswerRecordFinder, clean_excerpt


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

    def test_본문대조실패시같은스레드마지막성공으로물러선다(self, database, tmp_path) -> None:
        finder, audit = self._finder(database, tmp_path)
        audit.record_request(
            channel="C1", thread_ts="T1", message_ts="1.1", session_id="s1",
            resumed=False, model="m1", effort="low", elapsed=1.0, ok=True,
            answer="예전 답변",
        )
        rec = finder.find("C1", "T1", "본문 안에 전혀 없는 글")
        assert rec is not None
        assert rec["model"] == "m1"

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
