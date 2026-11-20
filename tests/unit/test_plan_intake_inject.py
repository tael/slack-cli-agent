"""N5: 계획 주입에 기대 결과·가정·확인 기준 포함(SCA_PLAN_INTAKE_INJECT)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from test_pipeline import build_pipeline, ok_response

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.engine.environment import ClaudeEnvironmentPolicy
from slack_cli_agent.guard import plan_start as ps
from slack_cli_agent.guard.pretool import plan_gate as pg

BRANCHES = [
    {"goal": "a", "source": "s", "depends_on": [], "delegate": "none", "returns": "수치"},
    {"goal": "b", "source": "s", "depends_on": [1], "delegate": "none", "returns": "목록"},
]
INTAKE: dict[str, Any] = {
    "intent": "i", "missing": [], "ask": "", "ack": "k",
    "expected_output": "한 줄 요약", "assumed": ["오늘 기준", "서울"],
    "checks": [{"what": "기온 수치가 있다", "how": "웹 조회"}, {"what": "출처", "how": "URL"}],
}
FULL = {"status": "ok", "type": "branch", "branches": BRANCHES}
SHORT = {"status": "ok", "type": "short", "branches": []}


def _call(tmp_path: Path, name: str = "s") -> dict[str, Any]:
    return {"transcript_path": str(tmp_path / f"{name}.jsonl"), "tool_name": "Read"}


def _write(call: dict[str, Any], **plan: Any) -> None:
    ps.plan_path(call["transcript_path"]).write_text(
        json.dumps({"finished_at": time.time(), **plan}), encoding="utf-8")


@pytest.fixture(autouse=True)
def _gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLAN_GATE", "3")
    monkeypatch.delenv(pg.INTAKE_INJECT_ENV, raising=False)


@pytest.fixture
def on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(pg.INTAKE_INJECT_ENV, "1")


class TestOff:
    def test_꺼져_있으면_필드가_있어도_주입_텍스트가_같다(self, tmp_path: Path) -> None:
        plain, rich = _call(tmp_path, "a"), _call(tmp_path, "b")
        _write(plain, **FULL)
        _write(rich, **FULL, **INTAKE)
        verdict, base = pg.decide(plain)
        assert verdict == "inject"
        assert pg.decide(rich) == ("inject", base)

    def test_꺼져_있으면_short_는_주입하지_않는다(self, tmp_path: Path) -> None:
        call = _call(tmp_path)
        _write(call, **SHORT, **INTAKE)
        assert pg.decide(call) == ("short", "")

    def test_표지_파일은_비어_있다(self, tmp_path: Path) -> None:
        call = _call(tmp_path)
        _write(call, **FULL, **INTAKE)
        pg.decide(call)
        assert pg.injected_path(call["transcript_path"]).read_text() == ""


class TestOn:
    def test_전체_주입에_세_항목이_붙고_작업_순서는_그대로다(self, tmp_path: Path, on: None) -> None:
        call = _call(tmp_path)
        _write(call, **FULL, **INTAKE)
        verdict, text = pg.decide(call)
        assert verdict == "inject" and text.startswith(pg.INJECT_HEAD)
        assert "1. 목표 : a" in text
        assert "기대 결과 : 한 줄 요약" in text
        assert "가정 : 오늘 기준 / 서울" in text and "가정을 답변에 밝혀" in text
        assert "1. 기온 수치가 있다 (확인 : 웹 조회)" in text and "미확인" in text
        assert text.endswith(pg.INJECT_TAIL_END)

    def test_짧은_계획도_checks_가_있으면_간략_주입한다(self, tmp_path: Path, on: None) -> None:
        call = _call(tmp_path)
        _write(call, **SHORT, **INTAKE)
        verdict, text = pg.decide(call)
        assert verdict == "inject"
        assert "기대 결과 : 한 줄 요약" in text and "미확인" in text
        assert "목표 :" not in text and pg.INJECT_HEAD not in text
        assert text.endswith(pg.INJECT_TAIL_END)
        assert pg.decide(call) == ("already", "")

    def test_기대_결과만_있어도_간략_주입한다(self, tmp_path: Path, on: None) -> None:
        call = _call(tmp_path)
        _write(call, **SHORT, expected_output="표")
        assert pg.decide(call)[0] == "inject"

    def test_가정만_있으면_주입하지_않는다(self, tmp_path: Path, on: None) -> None:
        call = _call(tmp_path)
        _write(call, **SHORT, assumed=["a"])
        assert pg.decide(call) == ("short", "")

    @pytest.mark.parametrize("bad", [
        {"expected_output": 3, "checks": "x", "assumed": "y"},
        {"expected_output": " ", "checks": [{"what": "a"}, 5, {"what": 1, "how": 2}], "assumed": [1, ""]},
    ])
    def test_틀린_필드는_무시한다(self, tmp_path: Path, on: None, bad: dict[str, Any]) -> None:
        call = _call(tmp_path)
        _write(call, **SHORT, **bad)
        assert pg.decide(call) == ("short", "")
        _write(call, **FULL, **bad)
        _, text = pg.decide(call)
        assert "기대 결과" not in text and "가정" not in text and "확인 기준" not in text

    def test_표지_파일에_종류를_남긴다(self, tmp_path: Path, on: None) -> None:
        full, short = _call(tmp_path, "f"), _call(tmp_path, "q")
        _write(full, **FULL, **INTAKE)
        _write(short, **SHORT, **INTAKE)
        pg.decide(full)
        pg.decide(short)
        assert pg.injected_path(full["transcript_path"]).read_text() == "full"
        assert pg.injected_path(short["transcript_path"]).read_text() == "compact"

    def test_한_번만_주입한다(self, tmp_path: Path, on: None) -> None:
        call = _call(tmp_path)
        _write(call, **FULL, **INTAKE)
        pg.decide(call)
        assert pg.decide(call) == ("already", "")


class TestWiring:
    def _pipeline(self, tmp_path: Path, intake: bool, inject: bool) -> Any:
        pipeline, _ = build_pipeline(responses=[ok_response()], tmp_path=tmp_path)
        pipeline._plan_gate_responses = 3
        pipeline._plan_intake = intake
        pipeline._plan_intake_inject = inject
        return pipeline

    @pytest.mark.parametrize(("intake", "inject", "expected"), [
        (True, True, True), (True, False, False), (False, True, False), (False, False, False),
    ])
    def test_둘_다_켜졌을_때만_환경에_싣는다(self, tmp_path: Path, intake: bool, inject: bool, expected: bool) -> None:
        env = self._pipeline(tmp_path, intake, inject)._plan_gate_env("owner", None)
        assert (pg.INTAKE_INJECT_ENV in env) is expected

    def test_설정_기본값은_꺼짐이다(self) -> None:
        assert RuntimeSettings().plan_intake_inject is False

    def test_엔진_환경_허용목록에_있다(self) -> None:
        assert pg.INTAKE_INJECT_ENV in ClaudeEnvironmentPolicy.ADDITIONAL_ALLOWED_VARS
