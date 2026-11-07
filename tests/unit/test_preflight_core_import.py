"""코어 모듈 import 실패를 기동 게이트에서 막는다.

worker 와 ingress 는 사전 점검을 통과한 뒤에야 `core.application` 을 늦게
import 한다. 사전 점검이 그 import 를 시도하지 않으면 깨진 코어가 "기동
가능" 으로 통과하고, 그 뒤 ImportError 가 종료코드 1 로 나가 launchd 가
10초마다 재기동한다. 설정 오류 종료코드(78)로 나가야 run.sh 가 차단 표식을
남기고 재기동이 멈춘다.
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

import pytest

from slack_cli_agent.cli import BLOCKED_EXIT, WorkerCommand
from slack_cli_agent.config.profile import Profile
from slack_cli_agent.preflight.check import PreflightContext
from slack_cli_agent.preflight.suite import PreflightSuite

CORE_MODULE = "slack_cli_agent.core.application"


def make_profile(tmp_path: Path) -> Profile:
    return Profile.from_dict(
        {
            "name": "example",
            "primary_engine": {
                "type": "claude",
                "binary": str(tmp_path / "claude_bin"),
                "model": "m",
            },
            "owner_user_id": "U1",
            "troubleshoot_channel": "C1",
            "state_dir": str(tmp_path / "state"),
        }
    )


def _import_check(suite: PreflightSuite):
    from slack_cli_agent.preflight.checks import CoreImportCheck

    found = [c for c in suite.checks if isinstance(c, CoreImportCheck)]
    assert found, "표준 점검 목록에 코어 import 점검이 없다"
    return found[0]


def test_표준_점검이_코어_import_실패를_기동_불가로_잡는다(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # sys.modules 의 None 은 그 이름의 import 를 ImportError 로 만든다.
    monkeypatch.setitem(sys.modules, CORE_MODULE, None)
    report = PreflightSuite().run(make_profile(tmp_path))

    hits = [r for r in report.fatal_failures() if CORE_MODULE in r.detail]
    assert hits, [r.detail for r in report.results]
    assert report.bootable is False


def test_코어가_정상이면_통과한다(tmp_path: Path) -> None:
    check = _import_check(PreflightSuite())
    result = check.run(PreflightContext(profile=make_profile(tmp_path)))
    assert result.ok is True, result.detail


def test_import_중_예외도_실패로_적는다(tmp_path: Path) -> None:
    from slack_cli_agent.preflight.checks import CoreImportCheck

    pkg = tmp_path / "broken_core_pkg_w5"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    try:
        result = CoreImportCheck(modules=("broken_core_pkg_w5",)).run(PreflightContext(profile=make_profile(tmp_path)))
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("broken_core_pkg_w5", None)
    assert result.ok is False
    assert result.fatal is True
    assert "broken_core_pkg_w5" in result.detail
    assert "boom" in result.detail


def test_worker_게이트가_코어_import_실패에_78로_끝난다(tmp_path: Path) -> None:
    from slack_cli_agent.preflight.checks import CoreImportCheck

    def 터지는_앱(*_args, **_kwargs):
        raise AssertionError("게이트가 막았어야 앱 조립까지 오지 않는다")

    command = WorkerCommand(
        application_factory=터지는_앱,
        signal_register=lambda *_a: None,
        preflight_suite_factory=lambda: PreflightSuite(checks=(CoreImportCheck(modules=("no_such_core_module_w5",)),)),
    )
    args = argparse.Namespace(worker_id="worker", once=True, catch_up=False)
    out = io.StringIO()
    code = command.execute_with_profile(make_profile(tmp_path), args, out)
    assert code == BLOCKED_EXIT
    assert "no_such_core_module_w5" in out.getvalue()
