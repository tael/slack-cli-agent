"""Application 이 정의한 주기 실행기가 전부 어느 프로세스에서든 기동되는가.

여기서 확인하는 것은 실행기의 동작이 아니다. 그것은 각 실행기의 시험이 한다.
이 파일이 확인하는 것은 **정의됐는데 아무도 안 띄우는 실행기가 없는가** 다.
실제로 연결 점검, 끝난 작업 정리, 첨부 정리가 그 형태로 빠져 있었고, 그때
부품 시험은 전부 통과했다.
"""

from __future__ import annotations

import ast
import inspect
import logging
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from test_application import FakeSlackClient, write_profile

from slack_cli_agent.config.profile import Profile
from slack_cli_agent.core.application import Application
from slack_cli_agent.core.context import RequestContext
from slack_cli_agent.core.periodic import PeriodicRunner
from slack_cli_agent.core.usage_check import CommandResult


@pytest.fixture
def client() -> FakeSlackClient:
    return FakeSlackClient()


@pytest.fixture
def profile(tmp_path: Path) -> Profile:
    return write_profile(tmp_path)


@pytest.fixture
def app(profile: Profile, client: FakeSlackClient) -> Application:
    return Application(profile, client)


def runner_factory_names() -> list[str]:
    """`-> PeriodicRunner` 로 선언된 공개 메서드 이름을 소스에서 뽑는다.

    메서드 목록을 시험에 손으로 적으면 새 실행기를 추가할 때 그 목록도 함께
    빠뜨린다. 그러면 이 시험은 통과하는데 실행기는 안 도는 상태가 된다.
    """
    source = Path(inspect.getsourcefile(Application) or "").read_text(encoding="utf-8")
    tree = ast.parse(source)
    names: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "Application":
            continue
        for item in node.body:
            if not isinstance(item, ast.FunctionDef) or item.name.startswith("_"):
                continue
            returns = item.returns
            if isinstance(returns, ast.Name) and returns.id == "PeriodicRunner":
                names.append(item.name)
    return names


RUNNER_ARGS: dict[str, Any] = {
    "job_purge_runner": lambda app: (),
    "epoch_purge_runner": lambda app: (),
    "admin_claim_purge_runner": lambda app: (),
    "admin_claim_reclaim_runner": lambda app: (),
    "watch_runner": lambda app: (),
    "state_snapshot_runner": lambda app: (),
    "roster_refresher": lambda app: (),
    "owner_only_audit_runner": lambda app: (),
    "usage_check_runner": lambda app: (),
    "attachment_cleanup_runner": lambda app: (),
    "watch_result_cleanup_runner": lambda app: (),
    "health_runner": lambda app: (lambda 사유: None,),
    "catchup_retry_runner": lambda app: (app.worker(),),
    "startup_catchup_runner": lambda app: (app.worker(),),
    "connection_catchup_runner": lambda app: (app.worker(),),
    "stale_reclaim_runner": lambda app: (app.worker(),),
    "pending_report_runner": lambda app: (),
    "stale_review_runner": lambda app: (),
    "learning_batch_runner": lambda app: (),
}


class Test모든실행기가어딘가에서기동된다:
    def test_소스에서_실행기_팩토리를_찾는다(self) -> None:
        """대조의 근거가 실제로 잡히는지 먼저 본다. 빈 목록이면 뒤의 시험이 공전한다."""
        assert len(runner_factory_names()) >= 5

    def test_모든_팩토리의_호출법을_이_시험이_안다(self, app: Application) -> None:
        """인자가 필요한 팩토리는 여기 적어 둔다. 새 팩토리가 생기면 먼저 여기서 걸린다."""
        미상 = [이름 for 이름 in runner_factory_names() if 이름 not in RUNNER_ARGS]
        assert 미상 == [], f"호출법을 모르는 실행기 팩토리: {미상}"

    def test_정의된_실행기가_전부_어느_묶음엔가_들어_있다(self, app: Application) -> None:
        묶음 = (
            *app.ingress_services(lambda 사유: None).runner_names,
            *app.worker_services(app.worker()).runner_names,
        )
        누락: list[str] = []
        for 이름 in runner_factory_names():
            러너 = getattr(app, 이름)(*RUNNER_ARGS[이름](app))
            if 러너.name not in 묶음:
                누락.append(f"{이름} (러너 이름 {러너.name})")
        assert 누락 == [], f"어느 프로세스에서도 안 띄우는 실행기: {누락}"


class Test묶음구성:
    def test_접수_묶음은_연결점검과_명부갱신과_첨부정리를_띄운다(self, app: Application) -> None:
        assert set(app.ingress_services(lambda 사유: None).runner_names) == {
            "health",
            "roster",
            "owner_only_audit",
            "usage_check",
            "attachment_cleanup",
            "pending_report",
            "stale_review",
        }

    def test_워커_묶음은_상태기록과_감시와_정리와_캐치업을_띄운다(self, app: Application) -> None:
        assert set(app.worker_services(app.worker()).runner_names) == {
            "state_snapshot",
            "watch_jobs",
            "watch_result_cleanup",
            "job_purge",
            "epoch_purge",
            "admin_claim_purge",
            "admin_claim_reclaim",
            "startup_catchup",
            "connection_catchup",
            "catchup_retry",
            "pending_report",
            "learning_batch",
            "stale_reclaim",
        }

    def test_정체_작업_회수는_주기로_돈다(self, app: Application) -> None:
        """기동 때 1회만 회수하면, 워커 하나가 죽어도 다른 워커가 살아 있는
        구성에서는 죽은 워커의 작업이 재기동 전까지 아무도 안 집는다."""
        assert "stale_reclaim" in app.worker_services(app.worker()).runner_names

    def test_회수_주기는_정체_판정_시간보다_길다(self, app: Application) -> None:
        """짧으면 박동이 한 번 늦은 멀쩡한 워커의 작업까지 뺏는다."""
        assert app.settings.stale_reclaim_interval_sec > app.settings.heartbeat_stale_sec

    def test_명부_갱신은_접수에만_있다(self, app: Application) -> None:
        """워커는 여럿 뜰 수 있어 거기서 갱신하면 같은 파일을 동시에 쓴다."""
        assert "roster" not in app.worker_services(app.worker()).runner_names

    def test_같은_객체를_돌려준다(self, app: Application) -> None:
        """묶음이 매번 새 실행기를 만들면 기동한 것과 정지 요청을 받는 것이 달라진다."""
        묶음 = app.ingress_services(lambda 사유: None)
        assert 묶음.runners[0] is app.health_runner(lambda 사유: None) or 묶음.runners[1] is app.roster_refresher()


class Test묶음이실제로기동한다:
    def test_with_로_열면_전부_돌고_나오면_멈춘다(self, app: Application, monkeypatch: Any) -> None:
        with app.worker_services(app.worker()) as 묶음:
            러너들 = [러너 for 러너 in 묶음.runners if isinstance(러너, PeriodicRunner)]
            assert 러너들 and len(러너들) == len(묶음.runners)
            assert all(러너.is_running() for 러너 in 러너들)
        for 러너 in 러너들:
            러너.join(timeout=2.0)
        assert not any(러너.is_running() for 러너 in 러너들)


class Test첨부정리:
    """받아 놓은 첨부를 주기적으로 지우는가.

    원본은 기동 3초 뒤 한 번만 지웠다. 접수 프로세스는 며칠씩 돌므로 한 번으로는
    그 뒤에 받은 첨부가 계속 남는다. 주기 실행기로 옮긴다.
    """

    def test_첨부_저장소를_한_번만_만든다(self, app: Application) -> None:
        """정리 실행기와 접수가 다른 객체를 보면 지우는 위치와 저장하는 위치가 갈린다."""
        assert app.attachments() is app.attachments()

    def test_접수가_그_저장소를_쓴다(self, app: Application) -> None:
        app.ingress()
        assert app.attachments() is app.ingress()._attachments

    def test_정리_실행기가_저장소의_정리를_부른다(self, app: Application, tmp_path: Path) -> None:
        불린다: list[bool] = []

        def 정리(now: float | None = None) -> int:
            불린다.append(True)
            return 0

        app.attachments().cleanup = 정리  # type: ignore[method-assign]
        러너 = app.attachment_cleanup_runner()
        러너._task()  # 한 회차만 돌린다
        assert 불린다 == [True]

    def test_정리_건수가_0이어도_로그에_남는다(
        self, app: Application, caplog: pytest.LogCaptureFixture
    ) -> None:
        """0건과 아예 안 돈 것이 로그에서 갈려야 한다(sca-mf6)."""
        app.attachments().cleanup = lambda now=None: 0  # type: ignore[method-assign]
        with caplog.at_level(logging.INFO):
            app.attachment_cleanup_runner()._task()
        assert any("첨부" in r.getMessage() for r in caplog.records)

    def test_정리_실행기가_접수_묶음에_들어_있다(self, app: Application) -> None:
        assert "attachment_cleanup" in app.ingress_services(lambda 사유: None).runner_names

    def test_감시_결과_정리가_워커_묶음에_들어_있다(self, app: Application) -> None:
        """등록에 실패한 건의 결과 파일은 아무 완료 경로도 안 지난다. 주기
        순회가 유일한 정리 계기다(sca-y6g)."""
        assert "watch_result_cleanup" in app.worker_services(app.worker()).runner_names


class Test캐치업재시도:
    """마치지 못한 캐치업을 다시 보는 실행기가 실제로 도는가.

    `CatchupService.retry_pending()` 은 호출처가 없으면 한 번도 실행되지 않는다.
    그러면 슬랙이 채널 기록을 빈 목록으로 준 구간의 요청은 영영 안 잡힌다.
    """

    def test_실행기가_워커의_재시도를_부른다(self, app: Application) -> None:
        from slack_cli_agent.reliability.catchup import RetryStatus

        불린다: list[bool] = []
        워커 = app.worker()

        def 기록하고_빈_목록을_낸다() -> list[RetryStatus]:
            불린다.append(True)
            return []

        워커.retry_catchup = 기록하고_빈_목록을_낸다  # type: ignore[method-assign]
        app.catchup_retry_runner(워커)._task()
        assert 불린다 == [True]

    def test_오래_막힌_채널은_소유자에게_알린다(self, app: Application, client: FakeSlackClient) -> None:
        """알리지 않으면 캐치업이 몇 시간째 안 되는 것을 아무도 모른다."""
        from slack_cli_agent.reliability.catchup import RetryStatus

        워커 = app.worker()
        워커.retry_catchup = lambda: [RetryStatus(channel="C9", stuck_sec=7200.0, alert=True)]  # type: ignore[method-assign]
        app.catchup_retry_runner(워커)._task()
        보낸것 = [kwargs for 이름, kwargs in client.calls if 이름 == "chat_postMessage"]
        assert any("C9" in str(kwargs) for kwargs in 보낸것), 보낸것

    def test_막히지_않았으면_안_알린다(self, app: Application, client: FakeSlackClient) -> None:
        from slack_cli_agent.reliability.catchup import RetryStatus

        워커 = app.worker()
        워커.retry_catchup = lambda: [RetryStatus(channel="C1", stuck_sec=1.0, alert=False)]  # type: ignore[method-assign]
        app.catchup_retry_runner(워커)._task()
        assert [이름 for 이름, _ in client.calls if 이름 == "chat_postMessage"] == []


class Test보내지못한보고:
    """발송이 실패한 보고가 남았다가 다시 나가는가.

    재기동 사유를 알리는 그 순간은 소켓이 불안정한 시점이라 발송이 실패하기
    쉽다. 로그만 남기고 끝내면 운영자는 장애가 났다는 사실 자체를 못 받는다.
    """

    def test_발송이_실패하면_남긴다(self, app: Application, monkeypatch: Any) -> None:
        def 실패(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("슬랙에 못 닿는다")

        monkeypatch.setattr(app.publisher(), "post", 실패)
        app._notify_owner("재기동 사유")
        assert "재기동 사유" in app.pending_report()._path.read_text(encoding="utf-8")

    def test_발송에_성공하면_안_남긴다(self, app: Application) -> None:
        app._notify_owner("정상 보고")
        assert not app.pending_report()._path.exists()

    def test_실행기가_남은_보고를_다시_보낸다(self, app: Application, client: FakeSlackClient) -> None:
        app.pending_report().save("밀린 보고")
        app.pending_report_runner()._task()
        assert not app.pending_report()._path.exists()
        assert any("밀린 보고" in str(kwargs) for 이름, kwargs in client.calls if 이름 == "chat_postMessage")

    def test_실행기가_양쪽_묶음에_들어_있다(self, app: Application) -> None:
        """접수에서 생긴 보류를 워커가, 워커에서 생긴 것을 접수가 못 보내면 그 사이 프로세스가 꺼진다."""
        assert "pending_report" in app.ingress_services(lambda 사유: None).runner_names
        assert "pending_report" in app.worker_services(app.worker()).runner_names


class Test복구직후캐치업:
    """슬랙에 닿지 않던 동안 들어온 요청은 소켓 이벤트로 다시 오지 않는다.

    돌아왔을 때 캐치업하지 않으면 그 시간의 요청은 어느 경로에서도 처리되지 않는다.
    끊겼던 시간이 기본 창보다 길면 그만큼 넓게 본다.
    """

    @staticmethod
    def 워커를_바꾼다(app: Application, 닿는다: list[bool]) -> tuple[Any, list[tuple[list[str], float | None]]]:
        from slack_cli_agent.reliability.catchup import CatchupReport

        기록: list[tuple[list[str], float | None]] = []
        워커 = app.worker()
        워커.retry_catchup = list  # type: ignore[method-assign]

        def 캐치업한다(channels: list[str], window_sec: float | None = None) -> CatchupReport:
            기록.append((channels, window_sec))
            return CatchupReport(missed=[], skipped=[], unchecked_channels=[])

        워커.catch_up = 캐치업한다  # type: ignore[method-assign]
        app._slack_reachable = lambda: 닿는다[0]  # type: ignore[method-assign]
        return 워커, 기록

    def test_계속_닿으면_캐치업하지_않는다(self, app: Application) -> None:
        워커, 기록 = self.워커를_바꾼다(app, [True])
        러너 = app.catchup_retry_runner(워커)
        러너._task()
        러너._task()
        assert 기록 == []

    def test_돌아온_회차에_캐치업한다(self, app: Application) -> None:
        닿는다 = [True]
        워커, 기록 = self.워커를_바꾼다(app, 닿는다)
        러너 = app.catchup_retry_runner(워커)
        러너._task()
        닿는다[0] = False
        러너._task()
        닿는다[0] = True
        러너._task()
        assert len(기록) == 1

    def test_끊긴_시간이_길면_창을_넓힌다(self, profile: Profile, client: FakeSlackClient) -> None:
        시각 = [1000.0]
        app = Application(profile, client, clock=lambda: 시각[0])
        닿는다 = [True]
        워커, 기록 = self.워커를_바꾼다(app, 닿는다)
        러너 = app.catchup_retry_runner(워커)
        러너._task()
        닿는다[0] = False
        러너._task()
        시각[0] += 20000.0
        닿는다[0] = True
        러너._task()
        # 끊긴 시간에 여유를 더한 값이 기본 창보다 크면 그쪽을 쓴다
        assert 기록[0][1] == 20000.0 + 600

    def test_창은_최대값을_넘지_않는다(self, profile: Profile, client: FakeSlackClient) -> None:
        """무한정 넓히면 한 회차가 채널 전체의 며칠치 기록을 읽는다."""
        시각 = [1000.0]
        app = Application(profile, client, clock=lambda: 시각[0])
        닿는다 = [True]
        워커, 기록 = self.워커를_바꾼다(app, 닿는다)
        러너 = app.catchup_retry_runner(워커)
        러너._task()
        닿는다[0] = False
        러너._task()
        시각[0] += 10_000_000.0
        닿는다[0] = True
        러너._task()
        assert 기록[0][1] == app.settings.catchup_max_window_sec


class Test복구보고:
    """끊겼다 돌아온 사실을 소유자에게 알리는가.

    원본은 복구 시각과 캐치업으로 처리한 건수를 개인 대화로 보낸다(bot.py:6852).
    알리지 않으면 운영자는 장애가 있었다는 것도, 그 구간이 회수됐는지도 모른다.
    """

    @staticmethod
    def 복구시킨다(app: Application) -> list[tuple[str, dict[str, Any]]]:
        from slack_cli_agent.reliability.catchup import CatchupReport

        닿는다 = [True]
        워커 = app.worker()
        워커.retry_catchup = list  # type: ignore[method-assign]
        워커.catch_up = lambda channels, window_sec=None: CatchupReport(  # type: ignore[method-assign]
            missed=[
                RequestContext(channel="C1", user="U1", ts="1.1", thread_ts="1.1", text="안녕"),
                RequestContext(channel="C1", user="U1", ts="1.2", thread_ts="1.2", text="또"),
            ],
            skipped=[],
            unchecked_channels=[],
        )
        app._slack_reachable = lambda: 닿는다[0]  # type: ignore[method-assign]
        러너 = app.catchup_retry_runner(워커)
        러너._task()
        닿는다[0] = False
        러너._task()
        닿는다[0] = True
        러너._task()
        return app._client.calls  # type: ignore[attr-defined]

    def test_복구를_소유자에게_알린다(self, app: Application, client: FakeSlackClient) -> None:
        보낸것 = [kwargs for 이름, kwargs in self.복구시킨다(app) if 이름 == "chat_postMessage"]
        assert any("연결 복구" in str(kwargs) for kwargs in 보낸것), 보낸것

    def test_캐치업으로_처리한_건수를_함께_알린다(self, app: Application, client: FakeSlackClient) -> None:
        """건수가 없으면 회수가 됐는지 0건이었는지 구분되지 않는다."""
        보낸것 = [kwargs for 이름, kwargs in self.복구시킨다(app) if 이름 == "chat_postMessage"]
        assert any("2건" in str(kwargs) for kwargs in 보낸것), 보낸것

    def test_MCP_서버_상태를_함께_알린다(self, app: Application, client: FakeSlackClient) -> None:
        """원본 bot.py:6857 이 보고에 담는 줄이다. 노트북이 잠들었다 깨어난 뒤
        MCP 가 조용히 빠진 상태를 알아챌 계기가 여기 말고 없다."""
        보낸것 = [kwargs for 이름, kwargs in self.복구시킨다(app) if 이름 == "chat_postMessage"]
        assert any("MCP 서버" in str(kwargs) for kwargs in 보낸것), 보낸것

    def test_기동_못_하는_MCP_서버를_이름으로_알린다(
        self, tmp_path: Path, client: FakeSlackClient
    ) -> None:
        profile = write_profile(
            tmp_path, mcp_servers={"없는서버": {"command": "/없는/경로/binary"}}
        )
        보낸것 = [
            kwargs for 이름, kwargs in self.복구시킨다(Application(profile, client))
            if 이름 == "chat_postMessage"
        ]
        합친것 = str(보낸것)
        assert "없는서버" in 합친것, 보낸것
        assert "도구가 빠진 채로" in 합친것, 보낸것

    def test_끊기지_않았으면_안_알린다(self, app: Application, client: FakeSlackClient) -> None:
        워커 = app.worker()
        워커.retry_catchup = list  # type: ignore[method-assign]
        app._slack_reachable = lambda: True  # type: ignore[method-assign]
        러너 = app.catchup_retry_runner(워커)
        러너._task()
        러너._task()
        assert [이름 for 이름, _ in client.calls if 이름 == "chat_postMessage"] == []


class Test느린요청보고의기록리더:
    """엔진에 맞는 기록 리더를 쓰는가.

    조립이 `ClaudeTranscriptReader` 를 직접 만들고 있었다. codex 프로필에서도
    Claude 기록 경로를 뒤진다는 뜻이다. 원본은 codex 면 경로를 만들지 않는다
    (bot.py:3083).
    """

    def test_claude_프로필은_claude_리더를_쓴다(self, tmp_path: Path, client: FakeSlackClient) -> None:
        from slack_cli_agent.engine.transcript import ClaudeTranscriptReader

        app = Application(write_profile(tmp_path), client)
        assert isinstance(app.transcript_reader(), ClaudeTranscriptReader)

    def test_codex_프로필은_claude_리더를_안_쓴다(self, tmp_path: Path, client: FakeSlackClient) -> None:
        binary = tmp_path / "bin" / "fake-engine"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        profile = write_profile(
            tmp_path, primary_engine={"type": "codex", "binary": str(binary), "model": "model-a"}
        )
        from slack_cli_agent.engine.transcript import ClaudeTranscriptReader

        app = Application(profile, client)
        # 결과가 빈 목록인 것만으로는 안 드러난다 — Claude 리더도 파일이
        # 없으면 빈 목록이다. 어느 리더를 골랐는지를 본다.
        assert not isinstance(app.transcript_reader(), ClaudeTranscriptReader)

    def test_한_번_만든_리더를_계속_쓴다(self, app: Application) -> None:
        """구간 분해와 사용량 행이 같은 리더를 본다. 따로 만들면 조회가 두 배다."""
        assert app.transcript_reader() is app.transcript_reader()


class Test링크된스레드채널명:
    """미등록 채널의 이름을 슬랙에서 가져오는가.

    조립이 빠지면 안내에 'C0C1LNABECV' 같은 채널 ID 가 그대로 나간다.
    부품 시험은 이것을 안 본다 - 조립 쪽에서 넘기는 함수가 설정만 보고 있어도
    ChannelNameResolver 자체 시험은 전부 통과한다.
    """

    LINK = "https://vroong.slack.com/archives/C0C1LNABECV/p1788253544408049"

    def test_미등록_채널이면_슬랙에_조회한_이름을_쓴다(
        self, profile: Profile, client: FakeSlackClient
    ) -> None:
        client.channel_names["C0C1LNABECV"] = "운영-공지"
        app = Application(profile, client)

        note = app._linked_threads().of(self.LINK)

        assert "운영-공지" in note
        assert "C0C1LNABECV" not in note

    def test_조회에_실패하면_채널_ID_를_쓴다(
        self, profile: Profile, client: FakeSlackClient
    ) -> None:
        client.channel_info_error = "조회 실패"
        app = Application(profile, client)

        assert "C0C1LNABECV" in app._linked_threads().of(self.LINK)

    def test_조회_결과를_캐시한다(self, profile: Profile, client: FakeSlackClient) -> None:
        client.channel_names["C0C1LNABECV"] = "운영-공지"
        app = Application(profile, client)

        app._linked_threads().of(self.LINK)
        app._linked_threads().of(self.LINK)

        조회 = [call for call in client.calls if call[0] == "conversations_info"]
        assert len(조회) == 1


class Test사용량확인실행기:
    """운영자가 건 사용량 확인 명령이 주기로 도는가.

    판정과 알림은 그 명령이 한다. 여기서 보는 것은 명령이 실제로 불리는가와,
    설정이 없을 때 아무것도 안 부르는가다.
    """

    def test_프로필에_명령이_있으면_그_명령을_부른다(
        self, tmp_path: Path, client: FakeSlackClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        profile = write_profile(tmp_path, usage_check_command=["usage.py", "--check"])
        app = Application(profile, client)
        기록: list[Sequence[str]] = []

        def run(command: Sequence[str], timeout_sec: float) -> CommandResult:
            기록.append(command)
            return CommandResult(returncode=0, stdout="", stderr="")

        app._usage_check_run = run

        app.usage_check_runner()._task()  # type: ignore[attr-defined]

        assert 기록 == [("usage.py", "--check")]

    def test_명령이_없으면_아무것도_부르지_않는다(
        self, tmp_path: Path, client: FakeSlackClient
    ) -> None:
        app = Application(write_profile(tmp_path), client)
        기록: list[Sequence[str]] = []

        def run(command: Sequence[str], timeout_sec: float) -> CommandResult:
            기록.append(command)
            return CommandResult(returncode=0, stdout="", stderr="")

        app._usage_check_run = run

        app.usage_check_runner()._task()  # type: ignore[attr-defined]

        assert 기록 == []


class Test묶음자기감시:
    """주기 실행기 스레드가 끝나도 프로세스는 계속 산다. 그것을 보는 자리가
    묶음에 붙어 있는가 (sca-2g0).
    """

    def test_접수_묶음에_감시_주기가_붙는다(self, app: Application) -> None:
        group = app.ingress_services(lambda 사유: None)

        assert group._watch_interval_sec > 0  # type: ignore[attr-defined]

    def test_워커_묶음에도_붙는다(self, app: Application) -> None:
        group = app.worker_services(app.worker())

        assert group._watch_interval_sec > 0  # type: ignore[attr-defined]

    def test_소유자가_있으면_알림_경로가_붙는다(self, app: Application) -> None:
        group = app.ingress_services(lambda 사유: None)

        assert group._notify is not None  # type: ignore[attr-defined]


class Test끝난연결세대정리:
    """purge_done 을 부르는 자리가 없어 원장이 상한 없이 늘었다 (sca-zb9)."""

    def test_보존_기간을_지나서_부른다(self, app: Application) -> None:
        불린값: list[float] = []

        def 기록(older_than: float) -> int:
            불린값.append(older_than)
            return 0

        app.connection_epochs().purge_done = 기록  # type: ignore[method-assign]
        app._purge_done_epochs()
        assert 불린값
        기대 = time.time() - app._settings.epoch_retention_sec
        assert abs(불린값[0] - 기대) < 5

    def test_보존_기간이_캐치업_유예보다_길다(self, app: Application) -> None:
        """세대를 지우면 그 구간의 공백 회수 근거가 사라진다."""
        assert app._settings.epoch_retention_sec > app._settings.catchup_grace_sec


class Test소유자전용점검이실제로조회한다:
    """조립 지점만 보는 시험은 client 가 property 인지 함수인지를 안 본다.
    운영에서는 매 주기 'WebClient' object is not callable 로 끝났다 (sca-3n0)."""

    def test_멤버_조회가_슬랙까지_나간다(self, tmp_path: Path, client: FakeSlackClient) -> None:
        profile = write_profile(tmp_path, settings={"owner_only_channels": ["C1"]})
        app = Application(profile, client)
        app.owner_only_audit_runner()._task()
        assert [name for name, _ in client.calls if name == "conversations_members"]

    def test_봇_판정이_슬랙까지_나간다(self, tmp_path: Path, client: FakeSlackClient) -> None:
        app = Application(write_profile(tmp_path), client)
        app._is_bot_user("U1")
        assert [name for name, _ in client.calls if name == "users_info"]


class Test접수의_잠금예산:
    """부품이 있는 것과 배선된 것은 다르다 (sca-9l1)."""

    def test_접수에_잠금예산이_배선된다(self, app: Application) -> None:
        assert app.ingress()._lock_budget is not None

    def test_예산_안에서_DB_잠금_대기가_짧아진다(self, app: Application) -> None:
        예산 = app.ingress()._lock_budget
        assert 예산 is not None
        database = app.database
        기본 = int(database.connect().execute("PRAGMA busy_timeout").fetchone()[0])
        with 예산():
            안 = int(database.connect().execute("PRAGMA busy_timeout").fetchone()[0])
        assert 기본 == 30_000
        assert 안 == int(app._settings.ingress_lock_budget_sec * 1000)

    def test_예산_합이_슬랙_ACK_한도보다_짧다(self, app: Application) -> None:
        """처리 풀이 전부 막히면 뒤 이벤트의 콜백 시작이 그만큼 밀린다.
        적재 시도뿐 아니라 적재 뒤의 대기 여부 조회도 예산을 한 번 더 쓴다."""
        settings = app._settings
        ingress = app.ingress()
        시도 = ingress._enqueue_attempts
        잠금_접근_횟수 = 시도 + 1  # enqueue 시도들 + blocked_on_thread
        대기 = settings.ingress_lock_budget_sec * 잠금_접근_횟수
        백오프 = sum(ingress._enqueue_retry_wait_sec * (n + 1) for n in range(시도 - 1))
        상한 = 대기 + 백오프
        assert 상한 <= 1.5, f"접수 경로 상한이 {상한:.2f}초다"
