"""Application 이 정의한 주기 실행기가 전부 어느 프로세스에서든 기동되는가.

여기서 확인하는 것은 실행기의 동작이 아니다. 그것은 각 실행기의 시험이 한다.
이 파일이 확인하는 것은 **정의됐는데 아무도 안 띄우는 실행기가 없는가** 다.
실제로 연결 점검, 끝난 작업 정리, 첨부 정리가 그 형태로 빠져 있었고, 그때
부품 시험은 전부 통과했다.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any

import pytest
from test_application import FakeSlackClient, write_profile

from slack_cli_agent.config.profile import Profile
from slack_cli_agent.core.application import Application


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


class Test모든실행기가어딘가에서기동된다:
    def test_소스에서_실행기_팩토리를_찾는다(self) -> None:
        """대조의 근거가 실제로 잡히는지 먼저 본다. 빈 목록이면 뒤의 시험이 공전한다."""
        assert len(runner_factory_names()) >= 5

    def test_정의된_실행기가_전부_어느_묶음엔가_들어_있다(self, app: Application) -> None:
        묶음 = (*app.ingress_services(lambda 사유: None).runner_names, *app.worker_services().runner_names)
        누락: list[str] = []
        for 이름 in runner_factory_names():
            팩토리 = getattr(app, 이름)
            러너 = 팩토리(lambda 사유: None) if 이름 == "health_runner" else 팩토리()
            if 러너.name not in 묶음:
                누락.append(f"{이름} (러너 이름 {러너.name})")
        assert 누락 == [], f"어느 프로세스에서도 안 띄우는 실행기: {누락}"


class Test묶음구성:
    def test_접수_묶음은_연결점검과_명부갱신과_첨부정리를_띄운다(self, app: Application) -> None:
        assert set(app.ingress_services(lambda 사유: None).runner_names) == {
            "health",
            "roster",
            "attachment_cleanup",
        }

    def test_워커_묶음은_상태기록과_감시와_정리를_띄운다(self, app: Application) -> None:
        assert set(app.worker_services().runner_names) == {"state_snapshot", "watch_jobs", "job_purge"}

    def test_명부_갱신은_접수에만_있다(self, app: Application) -> None:
        """워커는 여럿 뜰 수 있어 거기서 갱신하면 같은 파일을 동시에 쓴다."""
        assert "roster" not in app.worker_services().runner_names

    def test_같은_객체를_돌려준다(self, app: Application) -> None:
        """묶음이 매번 새 실행기를 만들면 기동한 것과 정지 요청을 받는 것이 달라진다."""
        묶음 = app.ingress_services(lambda 사유: None)
        assert 묶음.runners[0] is app.health_runner(lambda 사유: None) or 묶음.runners[1] is app.roster_refresher()


class Test묶음이실제로기동한다:
    def test_with_로_열면_전부_돌고_나오면_멈춘다(self, app: Application, monkeypatch: Any) -> None:
        with app.worker_services() as 묶음:
            assert all(러너.is_running() for 러너 in 묶음.runners)
        for 러너 in 묶음.runners:
            러너.join(timeout=2.0)
        assert not any(러너.is_running() for 러너 in 묶음.runners)


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
        app.attachments().cleanup = lambda now=None: 불린다.append(True)  # type: ignore[method-assign]
        러너 = app.attachment_cleanup_runner()
        러너._task()  # 한 회차만 돌린다
        assert 불린다 == [True]

    def test_정리_실행기가_접수_묶음에_들어_있다(self, app: Application) -> None:
        assert "attachment_cleanup" in app.ingress_services(lambda 사유: None).runner_names
