"""자기 수정 적용 경로 (H3, 마메치 apply.sh 대체).

턴은 apply-request 표식만 쓰고 감독(H2)이 점검, 재기동, 되돌리기, DM 을 한다.
마메치 apply.sh 는 점검(py_compile, 회귀 시험)을 턴 안에서 돌리고 변경 요약을
완료 DM 에 실었다. 여기서는 점검을 감독이 재기동 직전에 돌리고, 요약은 표식
셋째 줄부터 싣는다. 턴이 표식 경로를 알도록 가드 환경도 턴에 넘긴다.
"""

from __future__ import annotations

from pathlib import Path

from test_supervisor import 가짜환경

from slack_cli_agent.reliability.restart import ApplyRequest


def test_표식_셋째_줄부터_변경_요약을_읽는다() -> None:
    request = ApplyRequest.parse("1700000000\nC1:5.0\n답변 형식을 바꿨다\n- a.py 수정\n")
    assert request is not None
    assert request.turn_id == "C1:5.0"
    assert request.note == "답변 형식을 바꿨다\n- a.py 수정"
    assert ApplyRequest.parse(request.render()) == request


def test_턴_없이_요약만_있어도_둘째_줄을_비우지_않는다() -> None:
    text = ApplyRequest(requested_at=1700000000, note="요약").render()
    assert text.splitlines()[1] == "unknown"
    parsed = ApplyRequest.parse(text)
    assert parsed is not None and parsed.turn_id is None and parsed.note == "요약"


def test_점검이_실패하면_재기동하지_않고_실패를_알린다(tmp_path: Path) -> None:
    env = 가짜환경(tmp_path)
    env.표식(age=20)
    sup = env.supervisor(check=lambda: (False, "FAILED tests/x.py::test_a\n1 failed"))
    assert sup.tick() == "check_failed"
    assert env.kick == []
    assert not (tmp_path / "apply-running").exists()
    assert len(env.dm) == 1
    assert "점검 실패" in env.dm[0] and "1 failed" in env.dm[0]


def test_점검이_예외를_내도_재기동하지_않고_알린다(tmp_path: Path) -> None:
    env = 가짜환경(tmp_path)
    env.표식(age=20)

    def 터짐() -> tuple[bool, str]:
        raise OSError("pytest 없음")

    assert env.supervisor(check=터짐).tick() == "check_failed"
    assert env.kick == []
    assert "점검 실패" in env.dm[0]


def test_점검을_통과하면_재기동하고_변경_요약을_DM에_싣는다(tmp_path: Path) -> None:
    env = 가짜환경(tmp_path)
    path = env.표식(age=20)
    stamp = path.stat().st_mtime
    path.write_text(f"{int(stamp)}\nC1:111.2\n답변 형식을 바꿨다\n- a.py 수정\n")
    import os

    os.utime(path, (stamp, stamp))
    calls: list[int] = []

    def 통과() -> tuple[bool, str]:
        calls.append(1)
        return True, "10 passed"

    assert env.supervisor(check=통과).tick() == "restarted"
    assert calls == [1]
    assert "답변 형식을 바꿨다" in env.dm[0] and "- a.py 수정" in env.dm[0]


def test_가드_안내에_표식_경로와_요약_줄이_있다(monkeypatch) -> None:
    from slack_cli_agent.guard.pretool import restart_guard

    monkeypatch.setattr(restart_guard, "_MARKER_PATH", "/x/apply-request")
    hint = restart_guard._marker_hint()
    assert "/x/apply-request" in hint and "변경 요약" in hint


def test_Claude_턴_환경이_가드_표식_경로를_넘긴다() -> None:
    from slack_cli_agent.engine.environment import ClaudeEnvironmentPolicy

    env = ClaudeEnvironmentPolicy("test").build({
        "PATH": "/bin",
        "RESTART_GUARD_MARKER": "/s/apply-request",
        "RESTART_GUARD_LABELS": "local.t.worker,local.t.ingress",
    })
    assert env["RESTART_GUARD_MARKER"] == "/s/apply-request"
    assert env["RESTART_GUARD_LABELS"] == "local.t.worker,local.t.ingress"


def test_파이프라인이_턴에_표식_경로를_싣는다(tmp_path: Path) -> None:
    from test_application import FakeSlackClient, write_profile

    from slack_cli_agent.core.application import Application

    app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
    env = app.restart_guard_env()
    request, _running = app._profile.paths.apply_requests
    assert env["RESTART_GUARD_MARKER"] == str(request)
    assert app._profile.launch_label in env["RESTART_GUARD_LABELS"]


def test_파이프라인이_가드_환경을_엔진_환경에_합친다(tmp_path: Path) -> None:
    from test_pipeline import build_pipeline, make_ctx, ok_response

    pipeline, deps = build_pipeline(responses=[ok_response()], tmp_path=tmp_path)
    pipeline._restart_env = {"RESTART_GUARD_MARKER": "/s/apply-request"}
    pipeline.handle(make_ctx(channel="C1"))
    env = deps["runner"].calls[0].engine_env
    assert env["RESTART_GUARD_MARKER"] == "/s/apply-request"
    assert env["TURN_ID"].startswith("C1:")


def test_점검_명령은_종료코드로_통과를_가르고_출력을_돌려준다() -> None:
    import sys

    from slack_cli_agent.cli import _run_check

    ok, out = _run_check([sys.executable, "-c", "print('1 failed'); raise SystemExit(1)"])
    assert ok is False and "1 failed" in out
    assert _run_check([sys.executable, "-c", "pass"])[0] is True
    ok, out = _run_check(["/없는/명령"])
    assert ok is False and "실행 실패" in out


def test_supervise_가_check_command_를_감독에_넘긴다(tmp_path: Path) -> None:
    import io
    import json

    from slack_cli_agent.cli import SlackCliAgent, SuperviseCommand

    (tmp_path / "t.json").write_text(json.dumps({
        "name": "t", "state_dir": str(tmp_path / "s"), "owner_user_id": "U1",
        "primary_engine": {"type": "claude", "binary": "python3", "model": "m"},
    }), encoding="utf-8")
    seen: list[object] = []

    class 가짜감독:
        def tick(self) -> None:
            return None

    def factory(profile: object, args: object) -> 가짜감독:
        seen.append(getattr(args, "check_command"))
        return 가짜감독()

    SlackCliAgent([SuperviseCommand(supervisor_factory=factory)]).run([
        "supervise", "--profile", "t", "--profile-dir", str(tmp_path), "--once",
        "--check-command", "python -m pytest -q tests",
    ], io.StringIO())
    assert seen == ["python -m pytest -q tests"]
