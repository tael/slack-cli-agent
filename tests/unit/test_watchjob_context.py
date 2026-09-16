"""감시 큐가 등록 시점의 맥락을 보존하는지 고정한다.

스키마에 컬럼이 있어도 큐가 안 읽으면 호출부는 그 값을 못 쓴다.
"""

from __future__ import annotations

import json

import pytest

from slack_cli_agent.auth.principal import TrustLevel
from slack_cli_agent.reliability.watchjobs import WatchJobQueue


@pytest.fixture
def 큐(database):
    시각 = {"값": 1000.0}
    q = WatchJobQueue(database, now=lambda: 시각["값"])
    q.시각 = 시각
    return q


class Test등록맥락보존:
    def test_표식대상메시지가되돌아온다(self, 큐) -> None:
        """완료 시 그 메시지의 감시 표식을 떼려면 ts 가 필요하다."""
        큐.enqueue("C1", "111.1", "배포 확인", msg_ts="222.2")
        작업 = 큐.due(now=2000.0, min_gap=0.0)[0]
        assert 작업.msg_ts == "222.2"

    def test_권한맥락이되돌아온다(self, 큐) -> None:
        """확인 프롬프트를 어느 권한으로 실행할지가 등록 시점에 정해진다."""
        큐.enqueue("C1", "111.1", "배포 확인", trust=TrustLevel.OWNER)
        작업 = 큐.due(now=2000.0, min_gap=0.0)[0]
        assert 작업.trust is TrustLevel.OWNER

    def test_기본권한은일반이다(self, 큐) -> None:
        큐.enqueue("C1", "111.1", "배포 확인")
        작업 = 큐.due(now=2000.0, min_gap=0.0)[0]
        assert 작업.trust is TrustLevel.GENERAL
        assert 작업.msg_ts == ""

    def test_플러그인값이왕복한다(self, 큐) -> None:
        """조직 전용 값은 코어 필드가 아니라 extra 로 오간다."""
        큐.enqueue("C1", "111.1", "배포 확인", extra={"org_admin": True})
        작업 = 큐.due(now=2000.0, min_gap=0.0)[0]
        assert 작업.extra == {"org_admin": True}

    def test_extra가깨져있어도조회가죽지않는다(self, database, 큐) -> None:
        """사람이 손댔거나 옛 판이 쓴 값이 JSON 이 아닐 수 있다."""
        큐.enqueue("C1", "111.1", "배포 확인")
        database.connect().execute("UPDATE watch_jobs SET extra = '{망가짐'")
        작업 = 큐.due(now=2000.0, min_gap=0.0)[0]
        assert 작업.extra == {}

    def test_만료조회도같은맥락을준다(self, 큐) -> None:
        큐.enqueue("C1", "111.1", "배포 확인", msg_ts="222.2", trust=TrustLevel.TRUSTED)
        작업 = 큐.expired(now=99999.0, max_age=10.0)[0]
        assert (작업.msg_ts, 작업.trust) == ("222.2", TrustLevel.TRUSTED)


class Test확인횟수:
    def test_등록직후는0이다(self, 큐) -> None:
        큐.enqueue("C1", "111.1", "배포 확인")
        assert 큐.due(now=2000.0, min_gap=0.0)[0].checks == 0

    def test_확인할때마다증가한다(self, 큐) -> None:
        """상한을 두려면 몇 번 확인했는지가 남아야 한다."""
        작업_id = 큐.enqueue("C1", "111.1", "배포 확인")
        큐.mark_checked(작업_id, at=1100.0)
        큐.mark_checked(작업_id, at=1200.0)
        assert 큐.due(now=2000.0, min_gap=0.0)[0].checks == 2

    def test_상한을넘으면확인대상에서빠진다(self, 큐) -> None:
        """확인만 반복하고 안 끝나는 건이 엔진을 계속 부르는 것을 막는다."""
        작업_id = 큐.enqueue("C1", "111.1", "배포 확인")
        for 회차 in range(3):
            큐.mark_checked(작업_id, at=1100.0 + 회차)
        assert 큐.due(now=2000.0, min_gap=0.0, max_checks=3) == []
        assert len(큐.due(now=2000.0, min_gap=0.0, max_checks=4)) == 1

    def test_상한을안주면제한이없다(self, 큐) -> None:
        작업_id = 큐.enqueue("C1", "111.1", "배포 확인")
        for 회차 in range(50):
            큐.mark_checked(작업_id, at=1100.0 + 회차)
        assert len(큐.due(now=2000.0, min_gap=0.0)) == 1

    def test_상한초과건은만료조회에나온다(self, 큐) -> None:
        """확인 대상에서 빠진 건이 아무 데도 안 나오면 그대로 방치된다."""
        작업_id = 큐.enqueue("C1", "111.1", "배포 확인")
        for 회차 in range(3):
            큐.mark_checked(작업_id, at=1100.0 + 회차)
        포기 = 큐.expired(now=1200.0, max_age=99999.0, max_checks=3)
        assert [작업.id for 작업 in 포기] == [작업_id]

    def test_완료건은상한초과여도만료에안나온다(self, 큐) -> None:
        작업_id = 큐.enqueue("C1", "111.1", "배포 확인")
        for 회차 in range(3):
            큐.mark_checked(작업_id, at=1100.0 + 회차)
        큐.mark_done(작업_id)
        assert 큐.expired(now=1200.0, max_age=99999.0, max_checks=3) == []


class Test직렬화:
    def test_extra는JSON으로저장된다(self, database, 큐) -> None:
        """저장 형식이 정해져 있어야 플러그인이 밖에서도 읽는다."""
        큐.enqueue("C1", "111.1", "배포 확인", extra={"팀": "인프라"})
        값 = database.connect().execute("SELECT extra FROM watch_jobs").fetchone()["extra"]
        assert json.loads(값) == {"팀": "인프라"}

    def test_extra가없으면빈문자열이다(self, database, 큐) -> None:
        """빈 dict 를 '{}' 로 적으면 안 쓴 것과 구분이 안 된다."""
        큐.enqueue("C1", "111.1", "배포 확인")
        값 = database.connect().execute("SELECT extra FROM watch_jobs").fetchone()["extra"]
        assert 값 == ""


class Test실행자리보존:
    """확인 턴은 등록보다 한참 뒤에 다른 프로세스에서 돈다. 그 사이 채널
    설정의 workdir 이 바뀌면, 지금 설정으로 다시 계산한 자리에는 결과 파일이
    없다. 등록 시점의 절대 경로를 그대로 쓴다 (sca-6zt, 코덱스 검토).
    """

    def test_등록한_자리가_되돌아온다(self, 큐) -> None:
        큐.enqueue("C1", "111.1", "배포 확인", workdir="/Users/Shared/bot-work")
        작업 = 큐.due(now=2000.0, min_gap=0.0)[0]
        assert 작업.workdir == "/Users/Shared/bot-work"

    def test_안_주면_비어_있다(self, 큐) -> None:
        """옛 행에는 이 값이 없다. 빈 값이면 호출부가 그때의 기본으로 돌아간다."""
        큐.enqueue("C1", "111.1", "배포 확인")
        작업 = 큐.due(now=2000.0, min_gap=0.0)[0]
        assert 작업.workdir == ""

    def test_결과_파일_이름이_되돌아온다(self, 큐) -> None:
        """여러 감시가 같은 파일을 쓰면 출력이 섞여 잘못 완료 처리된다. 지금은
        컬럼만 있고 값을 발급하는 코드는 sca-17p 에서 온다."""
        큐.enqueue("C1", "111.1", "배포 확인", run_id="9f3a2b")
        작업 = 큐.due(now=2000.0, min_gap=0.0)[0]
        assert 작업.run_id == "9f3a2b"

    def test_작업마다_받은_이름이_따로_보존된다(self, 큐) -> None:
        큐.enqueue("C1", "111.1", "첫째", run_id="aaa")
        큐.enqueue("C1", "222.2", "둘째", run_id="bbb")
        이름들 = {j.run_id for j in 큐.due(now=2000.0, min_gap=0.0)}
        assert 이름들 == {"aaa", "bbb"}
