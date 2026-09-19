"""관리 명령 실행을 프로세스 간에 한 번으로 묶는다 (sca-8m5p).

접수기와 워커는 별도 프로세스라 메모리 중복 방지 기록을 공유하지 않는다.
일반 요청은 jobs 의 UNIQUE(channel, message_ts) 가 막지만(jobs/queue.py:33)
관리 명령은 큐를 안 거쳐 아무 기록도 안 남는다. 그래서 둘이 같은 메시지를
동시에 읽으면 둘 다 실행할 수 있다. `학습 반영` 처럼 두 번 돌면 결과가
달라지는 명령이 있다(admin/learning_commands.py:51).
"""

from __future__ import annotations

import pytest

from slack_cli_agent.storage.admin_claims import AdminClaims, ClaimState


@pytest.fixture
def 점유(database):
    시각 = [100.0]
    return AdminClaims(database, now=lambda: 시각[0]), 시각


class Test먼저_집은_쪽만_실행한다:
    def test_처음_집으면_참이다(self, 점유) -> None:
        claims, _ = 점유
        assert claims.claim("C1", "1.1", owner="ingress") is True

    def test_이미_집힌_것은_거짓이다(self, 점유) -> None:
        claims, _ = 점유
        claims.claim("C1", "1.1", owner="ingress")
        assert claims.claim("C1", "1.1", owner="worker") is False

    def test_다른_메시지는_따로_집는다(self, 점유) -> None:
        claims, _ = 점유
        claims.claim("C1", "1.1", owner="ingress")
        assert claims.claim("C1", "1.2", owner="worker") is True

    def test_끝난_뒤에도_다시_안_집힌다(self, 점유) -> None:
        """완료 기록이 사라지면 캐치업이 같은 명령을 다시 실행한다."""
        claims, _ = 점유
        claims.claim("C1", "1.1", owner="ingress")
        claims.finish("C1", "1.1", owner="ingress", ok=True)
        assert claims.claim("C1", "1.1", owner="worker") is False

    def test_실패한_뒤에도_다시_안_집힌다(self, 점유) -> None:
        """명령 일부가 이미 실행됐을 수 있어 재시도가 더 위험하다."""
        claims, _ = 점유
        claims.claim("C1", "1.1", owner="ingress")
        claims.finish("C1", "1.1", owner="ingress", ok=False, failure="터짐")
        assert claims.claim("C1", "1.1", owner="worker") is False


class Test점유한_프로세스가_죽으면_회수한다:
    """RUNNING 인 채로 남으면 캐치업이 매번 그 메시지를 찾는데 점유에 막혀
    아무도 실행하지 않고 표식도 안 남는다."""

    def test_임계를_넘긴_것만_회수한다(self, 점유) -> None:
        claims, 시각 = 점유
        claims.claim("C1", "1.1", owner="죽은쪽")
        시각[0] = 500.0
        assert [(c.channel, c.ts) for c in claims.reclaim_stale(시각[0] - 300.0)] == [("C1", "1.1")]

    def test_아직_도는_것은_안_건드린다(self, 점유) -> None:
        claims, 시각 = 점유
        claims.claim("C1", "1.1", owner="도는쪽")
        시각[0] = 200.0
        assert claims.reclaim_stale(시각[0] - 300.0) == []

    def test_끝난_것은_회수_대상이_아니다(self, 점유) -> None:
        claims, 시각 = 점유
        claims.claim("C1", "1.1", owner="ingress")
        claims.finish("C1", "1.1", owner="ingress", ok=True)
        시각[0] = 500.0
        assert claims.reclaim_stale(시각[0] - 300.0) == []

    def test_회수한_것은_실패로_남고_다시_안_집힌다(self, 점유) -> None:
        claims, 시각 = 점유
        claims.claim("C1", "1.1", owner="죽은쪽")
        시각[0] = 500.0
        claims.reclaim_stale(시각[0] - 300.0)
        assert claims.claim("C1", "1.1", owner="worker") is False
        assert claims.state("C1", "1.1") is ClaimState.FAILED


class Test종료는_점유한_쪽만_한다:
    """회수된 뒤에도 원래 프로세스가 무조건 DONE 으로 덮으면 회수가 아무
    효과가 없다. 정상 실행과 회수 결과가 경쟁한다 (코덱스 리뷰)."""

    def test_점유한_쪽이_닫으면_참이다(self, 점유) -> None:
        claims, _ = 점유
        claims.claim("C1", "1.1", owner="ingress")
        assert claims.finish("C1", "1.1", owner="ingress", ok=True) is True

    def test_회수된_뒤에는_못_닫는다(self, 점유) -> None:
        claims, 시각 = 점유
        claims.claim("C1", "1.1", owner="느린쪽")
        시각[0] = 500.0
        claims.reclaim_stale(시각[0] - 300.0)
        assert claims.finish("C1", "1.1", owner="느린쪽", ok=True) is False
        assert claims.state("C1", "1.1") is ClaimState.FAILED

    def test_다른_쪽이_닫으려_해도_안_된다(self, 점유) -> None:
        claims, _ = 점유
        claims.claim("C1", "1.1", owner="ingress")
        assert claims.finish("C1", "1.1", owner="worker", ok=True) is False
        assert claims.state("C1", "1.1") is ClaimState.RUNNING


class Test오래된_기록을_지운다:
    """캐치업 창보다 짧게 지우면 그 사이 명령이 다시 실행된다."""

    def test_임계보다_오래된_완료를_지운다(self, 점유) -> None:
        claims, 시각 = 점유
        claims.claim("C1", "1.1", owner="ingress")
        claims.finish("C1", "1.1", owner="ingress", ok=True)
        시각[0] = 1000.0
        assert claims.purge(시각[0] - 500.0, failures_before=시각[0] - 500.0) == 1
        assert claims.state("C1", "1.1") is None

    def test_최근_것은_안_지운다(self, 점유) -> None:
        claims, 시각 = 점유
        claims.claim("C1", "1.1", owner="ingress")
        claims.finish("C1", "1.1", owner="ingress", ok=True)
        시각[0] = 300.0
        assert claims.purge(시각[0] - 500.0, failures_before=시각[0] - 500.0) == 0

    def test_실패는_완료보다_오래_남긴다(self, 점유) -> None:
        """실패 표식(x)은 캐치업 대상이라 계속 다시 발견된다. 이 행이
        사라지면 그때 명령이 다시 실행된다 (코덱스 리뷰)."""
        claims, 시각 = 점유
        claims.claim("C1", "1.1", owner="ingress")
        claims.finish("C1", "1.1", owner="ingress", ok=False, failure="터짐")
        시각[0] = 10000.0
        assert claims.purge(시각[0] - 500.0, failures_before=50.0) == 0
        assert claims.state("C1", "1.1") is ClaimState.FAILED

    def test_실패도_충분히_지나면_지운다(self, 점유) -> None:
        """안 지우면 원장이 무한히 커진다. 캐치업 최대 창 밖으로 나간 뒤에는
        그 메시지가 다시 안 잡히므로 지워도 된다 (코덱스 리뷰)."""
        claims, 시각 = 점유
        claims.claim("C1", "1.1", owner="ingress")
        claims.finish("C1", "1.1", owner="ingress", ok=False, failure="터짐")
        시각[0] = 10000.0
        assert claims.purge(시각[0] - 500.0, failures_before=시각[0] - 100.0) == 1
        assert claims.state("C1", "1.1") is None

    def test_아직_도는_것은_안_지운다(self, 점유) -> None:
        claims, 시각 = 점유
        claims.claim("C1", "1.1", owner="ingress")
        시각[0] = 10000.0
        assert claims.purge(시각[0] - 500.0, failures_before=시각[0] - 500.0) == 0
