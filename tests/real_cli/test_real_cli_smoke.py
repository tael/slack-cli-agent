"""엔진 어댑터가 실제 CLI 와 맞는지 본다.

단위 시험은 우리가 만든 표본으로 파싱을 확인한다. 그 표본이 실제 CLI 출력과
어긋나면 시험은 통과하는데 봇은 안 돈다 — claude CLI 가 단일 객체에서 이벤트
배열로 출력을 바꿨을 때 실기기 확인에서야 알았다.

기본 실행에서 제외된다. 돌리려면 표식을 지정한다.

    python3 -m pytest tests/real_cli -m real_cli -p no:randomly

각 엔진은 로그인돼 있어야 한다. 안 돼 있으면 그 엔진만 건너뛴다.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from slack_cli_agent.auth.principal import TrustLevel
from slack_cli_agent.config.profile import Profile
from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.engine.base import Engine, EngineRequest
from slack_cli_agent.engine.claude import ClaudeEngine
from slack_cli_agent.engine.codex import CodexEngine
from slack_cli_agent.engine.environment import create_environment_policy
from slack_cli_agent.engine.gemini import GeminiEngine
from slack_cli_agent.engine.runner import EngineRunner

pytestmark = pytest.mark.real_cli

# 답이 한 가지로 정해지는 질문을 쓴다. 모델이 달라도 같은 답이 나와야
# 어댑터의 문제와 모델의 문제를 가를 수 있다.
QUESTION = "2 더하기 3은? 숫자만 답하라"
ANSWER = "5"

ENGINES: list[dict[str, Any]] = [
    {"id": "claude", "cls": ClaudeEngine, "type": "claude",
     "binary": "claude", "model": "claude-opus-5", "effort": "low"},
    {"id": "codex", "cls": CodexEngine, "type": "codex",
     "binary": "codex", "model": "gpt-5.6-luna", "effort": "low",
     "options": {"sandbox": "read-only", "network": False}},
    {"id": "gemini", "cls": GeminiEngine, "type": "gemini",
     "binary": "agy", "model": "gemini-3.8-flash", "effort": "low"},
]


def _build(spec: dict[str, Any], tmp_path: Path, env: dict[str, str]) -> tuple[Engine, EngineRunner]:
    binary = shutil.which(spec["binary"])
    if binary is None:
        pytest.skip(f"{spec['binary']} 가 설치돼 있지 않다")
    engine_block: dict[str, Any] = {
        "type": spec["type"], "binary": binary, "model": spec["model"],
    }
    if spec.get("options"):
        engine_block["options"] = spec["options"]
    profile = Profile.from_dict({
        "name": f"smoke-{spec['id']}",
        "primary_engine": engine_block,
        "state_dir": str(tmp_path / "state"),
        "work_root": str(tmp_path / "work"),
    })
    settings = RuntimeSettings(request_timeout_sec=180)
    policy = create_environment_policy(spec["type"], profile.name, None)
    runner = EngineRunner(settings, environment_policy=policy, source_env=env)
    return spec["cls"](profile, settings), runner


def _request(spec: dict[str, Any], engine: Engine, workdir: Path, **over: Any) -> EngineRequest:
    base: dict[str, Any] = {
        "prompt": QUESTION,
        "system_prompt": "질문에 짧게 답한다.",
        "session_id": engine.new_session_id(),
        "resume": False,
        "model": spec["model"],
        "effort": spec["effort"],
        "workdir": workdir,
        "readable_dirs": (),
        "allowed_tools": ("Read",),
        "trust_level": TrustLevel.OWNER,
    }
    base.update(over)
    return EngineRequest(**base)


def _params() -> list[Any]:
    return [pytest.param(s, id=s["id"]) for s in ENGINES]


@pytest.mark.parametrize("spec", _params())
def test_한_문장_질문에_답한다(
    spec: dict[str, Any], tmp_path: Path, real_env: dict[str, str],
) -> None:
    engine, runner = _build(spec, tmp_path, real_env)
    workdir = tmp_path / "work"
    workdir.mkdir(parents=True, exist_ok=True)

    try:
        response = runner.run(engine, _request(spec, engine, workdir))
    except subprocess.SubprocessError as exc:
        pytest.fail(f"{spec['id']} 실행 자체가 실패했다: {exc}")

    if not response.ok and "로그인" in response.body:
        pytest.skip(f"{spec['id']} 가 로그인돼 있지 않다")
    assert response.ok, f"{spec['id']} 실패: {response.failure_reason} / {response.body[:200]}"
    assert ANSWER in response.body


@pytest.mark.parametrize("spec", _params())
def test_세션을_재개하면_앞_대화를_안다(
    spec: dict[str, Any], tmp_path: Path, real_env: dict[str, str],
) -> None:
    engine, runner = _build(spec, tmp_path, real_env)
    workdir = tmp_path / "work"
    workdir.mkdir(parents=True, exist_ok=True)

    first = runner.run(engine, _request(
        spec, engine, workdir, prompt="내가 고른 숫자는 741이다. 알겠다고만 답하라",
    ))
    if not first.ok and "로그인" in first.body:
        pytest.skip(f"{spec['id']} 가 로그인돼 있지 않다")
    assert first.ok, f"{spec['id']} 첫 턴 실패: {first.failure_reason} / {first.body[:200]}"

    # 세션 식별자를 누가 발행하는지는 엔진마다 다르다. codex·gemini 는 CLI 가
    # 발행하고 claude 는 우리가 준다.
    session_id = engine.session_id_from(first) or first.session_id
    assert session_id, f"{spec['id']} 가 재개용 세션 식별자를 안 냈다"

    second = runner.run(engine, _request(
        spec, engine, workdir, resume=True, session_id=session_id,
        prompt="내가 고른 숫자가 뭐였지? 숫자만 답하라",
    ))
    assert second.ok, f"{spec['id']} 재개 실패: {second.failure_reason} / {second.body[:200]}"
    assert "741" in second.body


@pytest.mark.parametrize("spec", _params())
def test_하이픈으로_시작하는_말도_프롬프트로_간다(
    spec: dict[str, Any], tmp_path: Path, real_env: dict[str, str],
) -> None:
    """사용자가 '-h' 나 '--help' 로 시작하는 말을 던지면 CLI 가 그것을 플래그로
    읽는다. claude 는 `--` 구분자로, 나머지는 인자 위치로 막는다."""
    engine, runner = _build(spec, tmp_path, real_env)
    workdir = tmp_path / "work"
    workdir.mkdir(parents=True, exist_ok=True)

    response = runner.run(engine, _request(
        spec, engine, workdir, prompt="--version 이라는 말은 빼고, 2 더하기 3만 숫자로 답하라",
    ))
    if not response.ok and "로그인" in response.body:
        pytest.skip(f"{spec['id']} 가 로그인돼 있지 않다")
    assert response.ok, f"{spec['id']} 실패: {response.failure_reason} / {response.body[:200]}"
    assert ANSWER in response.body
