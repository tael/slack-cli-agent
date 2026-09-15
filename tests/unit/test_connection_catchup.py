"""소켓 재연결 전이가 캐치업을 부르는 계약.

접수기와 워커는 별도 프로세스라 소켓 상태가 함수 호출로 전달되지 않는다. 접수기가
연결 세대를 원장에 남기고 워커가 그것을 점유해 캐치업을 돌리는 것이 유일한 통로이므로,
그 통로를 프로세스를 띄우지 않고 이 시험으로 고정한다.
"""

from __future__ import annotations

import pytest

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.reliability.connection import (
    ConnectionCatchupCoordinator,
    ConnectionKind,
)
from slack_cli_agent.storage.connection_epochs import SqliteConnectionEpochs


class Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def 시계() -> Clock:
    return Clock()


@pytest.fixture
def 원장(database, 시계) -> SqliteConnectionEpochs:
    return SqliteConnectionEpochs(database, now=시계, heartbeat_sec=0.0)


class Test연결세대기록:
    def test_최초_연결이_세대_1을_남긴다(self, 원장, 시계):
        원장.record_connection(ConnectionKind.INITIAL)
        assert [e.generation for e in 원장.pending()] == [1]

    def test_재연결마다_세대가_늘어난다(self, 원장, 시계):
        원장.record_connection(ConnectionKind.INITIAL)
        시계.now = 1100.0
        원장.record_connection(ConnectionKind.RECONNECT)
        assert [e.generation for e in 원장.pending()] == [1, 2]

    def test_공백_시작은_직전에_살아_있던_시각이다(self, 원장, 시계):
        원장.record_connection(ConnectionKind.INITIAL)
        시계.now = 1050.0
        원장.note_alive()
        시계.now = 1200.0
        원장.record_connection(ConnectionKind.RECONNECT)

        새세대 = 원장.pending()[-1]
        assert 새세대.gap_started_at == 1050.0
        assert 새세대.connected_at == 1200.0

    def test_기록이_없으면_공백_시작을_연결_시각으로_둔다(self, 원장, 시계):
        원장.record_connection(ConnectionKind.INITIAL)
        assert 원장.pending()[0].gap_started_at == 1000.0

    def test_접수기가_강제_종료돼도_마지막_생존_시각이_남는다(self, database, 시계):
        """SIGKILL 은 단절을 못 남긴다. 다음 기동의 최초 연결이 그 공백을 잇는다."""
        첫판 = SqliteConnectionEpochs(database, now=시계, heartbeat_sec=0.0)
        첫판.record_connection(ConnectionKind.INITIAL)
        시계.now = 1080.0
        첫판.note_alive()

        시계.now = 1500.0
        새판 = SqliteConnectionEpochs(database, now=시계, heartbeat_sec=0.0)
        새판.record_connection(ConnectionKind.INITIAL)

        assert 새판.pending()[-1].gap_started_at == 1080.0


class Test점유:
    def test_워커가_여럿이어도_한_번만_돈다(self, database, 시계):
        원장 = SqliteConnectionEpochs(database, now=시계, heartbeat_sec=0.0)
        원장.record_connection(ConnectionKind.INITIAL)

        하나 = SqliteConnectionEpochs(database, now=시계, heartbeat_sec=0.0)
        둘 = SqliteConnectionEpochs(database, now=시계, heartbeat_sec=0.0)
        첫점유 = 하나.claim("w1", lease_sec=60.0)
        둘째점유 = 둘.claim("w2", lease_sec=60.0)

        assert 첫점유 is not None
        assert 둘째점유 is None

    def test_미처리_세대를_하나의_구간으로_합친다(self, 원장, 시계):
        원장.record_connection(ConnectionKind.INITIAL)
        시계.now = 1300.0
        원장.record_connection(ConnectionKind.RECONNECT)

        점유 = 원장.claim("w1", lease_sec=60.0)
        assert 점유 is not None
        assert 점유.generations == (1, 2)
        assert 점유.gap_started_at == 1000.0
        assert 점유.connected_at == 1300.0

    def test_완료하면_다시_점유되지_않는다(self, 원장):
        원장.record_connection(ConnectionKind.INITIAL)
        점유 = 원장.claim("w1", lease_sec=60.0)
        assert 점유 is not None
        원장.complete(점유.generations)
        assert 원장.claim("w1", lease_sec=60.0) is None

    def test_임대가_만료되면_다른_워커가_다시_집는다(self, 원장, 시계):
        원장.record_connection(ConnectionKind.INITIAL)
        원장.claim("w1", lease_sec=60.0)

        시계.now = 1030.0
        assert 원장.claim("w2", lease_sec=60.0) is None

        시계.now = 1061.0
        다시 = 원장.claim("w2", lease_sec=60.0)
        assert 다시 is not None
        assert 다시.generations == (1,)

    def test_반납하면_즉시_다시_집힌다(self, 원장):
        원장.record_connection(ConnectionKind.INITIAL)
        점유 = 원장.claim("w1", lease_sec=60.0)
        assert 점유 is not None
        원장.release(점유.generations)
        assert 원장.claim("w2", lease_sec=60.0) is not None

    def test_남은_것이_없으면_점유가_없다(self, 원장):
        assert 원장.claim("w1", lease_sec=60.0) is None


class Fake캐치업:
    def __init__(self) -> None:
        self.windows: list[float] = []

    def __call__(self, window: float) -> int:
        self.windows.append(window)
        return 0


class Test조정자:
    def 조정자(self, 원장, 시계, 캐치업, **설정) -> ConnectionCatchupCoordinator:
        return ConnectionCatchupCoordinator(
            store=원장,
            catch_up=캐치업,
            settings=RuntimeSettings(**설정),
            owner="w1",
            now=시계,
        )

    def test_연결_기록이_있으면_캐치업을_부른다(self, 원장, 시계):
        캐치업 = Fake캐치업()
        원장.record_connection(ConnectionKind.INITIAL)
        self.조정자(원장, 시계, 캐치업).tick()
        assert len(캐치업.windows) == 1

    def test_기록이_없으면_부르지_않는다(self, 원장, 시계):
        캐치업 = Fake캐치업()
        self.조정자(원장, 시계, 캐치업).tick()
        assert 캐치업.windows == []

    def test_같은_세대를_두_번_부르지_않는다(self, 원장, 시계):
        캐치업 = Fake캐치업()
        원장.record_connection(ConnectionKind.INITIAL)
        조정자 = self.조정자(원장, 시계, 캐치업)
        조정자.tick()
        조정자.tick()
        assert len(캐치업.windows) == 1

    def test_워커_둘이_같은_원장을_봐도_한_번만_돈다(self, database, 시계):
        캐치업 = Fake캐치업()
        SqliteConnectionEpochs(database, now=시계, heartbeat_sec=0.0).record_connection(
            ConnectionKind.INITIAL
        )
        for owner in ("w1", "w2"):
            ConnectionCatchupCoordinator(
                store=SqliteConnectionEpochs(database, now=시계, heartbeat_sec=0.0),
                catch_up=캐치업,
                settings=RuntimeSettings(),
                owner=owner,
                now=시계,
            ).tick()
        assert len(캐치업.windows) == 1

    def test_공백이_기본_창보다_짧으면_기본_창을_쓴다(self, 원장, 시계):
        캐치업 = Fake캐치업()
        원장.record_connection(ConnectionKind.INITIAL)
        self.조정자(원장, 시계, 캐치업, catchup_window_sec=7200).tick()
        assert 캐치업.windows == [7200]

    def test_공백이_길면_그_공백에_유예를_더해_넓힌다(self, 원장, 시계):
        캐치업 = Fake캐치업()
        원장.record_connection(ConnectionKind.INITIAL)
        시계.now = 1000.0 + 10000.0
        원장.record_connection(ConnectionKind.RECONNECT)
        self.조정자(
            원장, 시계, 캐치업, catchup_window_sec=7200, catchup_grace_sec=120
        ).tick()
        assert 캐치업.windows == [10120]

    def test_창은_최대치를_넘지_않는다(self, 원장, 시계):
        캐치업 = Fake캐치업()
        원장.record_connection(ConnectionKind.INITIAL)
        시계.now = 1000.0 + 999999.0
        원장.record_connection(ConnectionKind.RECONNECT)
        self.조정자(원장, 시계, 캐치업, catchup_max_window_sec=86400).tick()
        assert 캐치업.windows == [86400]

    def test_캐치업이_실패하면_반납해_다음에_다시_돈다(self, 원장, 시계):
        원장.record_connection(ConnectionKind.INITIAL)

        def 터진다(window: float) -> int:
            raise RuntimeError("history 읽기 실패")

        조정자 = self.조정자(원장, 시계, 터진다)
        with pytest.raises(RuntimeError):
            조정자.tick()

        캐치업 = Fake캐치업()
        self.조정자(원장, 시계, 캐치업).tick()
        assert len(캐치업.windows) == 1


class Fake기록기:
    def __init__(self) -> None:
        self.연결: list[ConnectionKind] = []
        self.생존 = 0

    def record_connection(self, kind: ConnectionKind):
        self.연결.append(kind)

    def note_alive(self) -> None:
        self.생존 += 1


class Test게이트웨이_배선:
    """소켓 상태 관측이 원장 기록으로 이어지는지. 여기가 끊기면 워커는 영원히
    점유할 것이 없고, 그 침묵은 무사고와 구분되지 않는다."""

    def 게이트웨이(self, 기록기):
        from slack_cli_agent.slack.gateway import SlackGateway

        class 조용한클라이언트:
            def auth_test(self):
                return {"team": "t", "user": "u"}

        return SlackGateway(조용한클라이언트(), epoch_recorder=기록기)

    def test_처음_붙으면_최초_연결로_남긴다(self):
        기록기 = Fake기록기()
        self.게이트웨이(기록기).observe_connection(True)
        assert 기록기.연결 == [ConnectionKind.INITIAL]

    def test_끊겼다_붙으면_재연결로_남긴다(self):
        기록기 = Fake기록기()
        gw = self.게이트웨이(기록기)
        gw.observe_connection(True)
        gw.observe_connection(False)
        gw.observe_connection(True)
        assert 기록기.연결 == [ConnectionKind.INITIAL, ConnectionKind.RECONNECT]

    def test_붙어_있는_동안은_세대를_더_만들지_않는다(self):
        기록기 = Fake기록기()
        gw = self.게이트웨이(기록기)
        for _ in range(5):
            gw.observe_connection(True)
        assert 기록기.연결 == [ConnectionKind.INITIAL]

    def test_붙어_있는_동안_생존을_남긴다(self):
        """전이 폴링은 세대를 남기므로 생존을 따로 안 남긴다. 그 뒤부터가 생존이다."""
        기록기 = Fake기록기()
        gw = self.게이트웨이(기록기)
        gw.observe_connection(True)
        gw.observe_connection(True)
        gw.observe_connection(True)
        assert 기록기.생존 == 2

    def test_끊긴_동안은_생존을_남기지_않는다(self):
        기록기 = Fake기록기()
        gw = self.게이트웨이(기록기)
        gw.observe_connection(False)
        assert 기록기.생존 == 0

    def test_기록기가_없어도_관측이_터지지_않는다(self):
        from slack_cli_agent.slack.gateway import SlackGateway

        class 조용한클라이언트:
            def auth_test(self):
                return {}

        SlackGateway(조용한클라이언트()).observe_connection(True)


@pytest.fixture
def app(tmp_path):
    from test_application import FakeSlackClient, write_profile

    from slack_cli_agent.core.application import Application

    return Application(write_profile(tmp_path), FakeSlackClient())


class Test조립:
    """부품을 만든 것과 배선한 것은 다르다. 주입이 빠지면 단위 시험은 전부
    통과하면서 운영에서만 안 돈다."""

    def test_접수기_게이트웨이에_기록기가_붙어_있다(self, app):
        assert app.gateway()._epoch_recorder is app.connection_epochs()

    def test_워커_서비스에_연결_캐치업이_있다(self, app):
        assert "connection_catchup" in app.worker_services(app.worker()).runner_names

    def test_연결_캐치업_러너가_조정자를_부른다(self, app):
        기록 = []
        조정자 = app.connection_catchup_coordinator(app.worker())
        조정자._catch_up = lambda window: 기록.append(window)
        app.connection_epochs().record_connection(ConnectionKind.INITIAL)
        조정자.tick()
        assert len(기록) == 1
