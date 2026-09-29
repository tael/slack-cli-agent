"""Cross-engine contract — every Engine implementation must produce the same
observable behavior. Add a new engine by adding one EngineFixture to
ENGINE_FIXTURES; every test in this module runs against it automatically.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

import pytest

from slack_cli_agent.auth.policy import DEFAULT_EFFORT, EFFORT_LEVELS
from slack_cli_agent.auth.principal import TrustLevel
from slack_cli_agent.config.profile import Profile
from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.core.errors import ConfigError
from slack_cli_agent.engine.base import Engine, EngineRequest, Usage
from slack_cli_agent.engine.capability import ToolRestriction
from slack_cli_agent.engine.claude import ClaudeEngine
from slack_cli_agent.engine.codex import CodexEngine
from slack_cli_agent.engine.gemini import GeminiEngine
from slack_cli_agent.engine.runner import EngineRunner
from slack_cli_agent.engine.tool_selection import ToolSelection

SETTINGS = RuntimeSettings()


def _profile(primary: dict[str, Any], fallback: dict[str, Any] | None, tmp_path: Path) -> Profile:
    data: dict[str, Any] = {
        "name": "contract",
        "primary_engine": primary,
        "owner_user_id": "U1",
        "troubleshoot_channel": "C1",
        "state_dir": str(tmp_path / "state"),
    }
    if fallback:
        data["fallback_engine"] = fallback
    return Profile.from_dict(data)


class EngineFixture(NamedTuple):
    id: str
    engine_class: type[Engine]
    configured_profile: Callable[[Path], Profile]
    # A profile where this engine has no block at all — the ConfigError case.
    unconfigured_profile: Callable[[Path], Profile]
    resend_system_prompt_on_resume: bool
    # A token that must be present in the command when resume=True and absent
    # when resume=False (e.g. "--resume" for claude, "resume" for codex).
    resume_token: str
    native_usage: dict[str, Any]
    expected_usage: Usage
    # Builds (stdout, stderr, returncode) for engine.parse() from a native usage dict.
    build_stdout: Callable[[dict[str, Any]], tuple[str, str, int]]


def _claude_configured(tmp_path: Path) -> Profile:
    return _profile({"type": "claude", "binary": "claude", "model": "claude-sonnet-5"}, None, tmp_path)


def _claude_unconfigured(tmp_path: Path) -> Profile:
    return _profile({"type": "codex", "binary": "codex", "model": "gpt-5.6-sol"}, None, tmp_path)


def _codex_configured(tmp_path: Path) -> Profile:
    return _profile(
        {"type": "claude", "binary": "claude", "model": "claude-sonnet-5"},
        {"type": "codex", "binary": "codex", "model": "gpt-5.6-sol", "options": {}},
        tmp_path,
    )


def _codex_unconfigured(tmp_path: Path) -> Profile:
    return _profile({"type": "claude", "binary": "claude", "model": "claude-sonnet-5"}, None, tmp_path)


def _gemini_configured(tmp_path: Path) -> Profile:
    return _profile(
        {"type": "claude", "binary": "claude", "model": "claude-sonnet-5"},
        {"type": "gemini", "binary": "agy", "model": "gemini-3.8-flash", "options": {}},
        tmp_path,
    )


def _gemini_unconfigured(tmp_path: Path) -> Profile:
    return _profile({"type": "claude", "binary": "claude", "model": "claude-sonnet-5"}, None, tmp_path)


def _claude_stdout(usage: dict[str, Any]) -> tuple[str, str, int]:
    payload = {
        "result": "답변 본문", "session_id": "s1", "model": "claude-sonnet-5",
        "num_turns": 1, "usage": usage, "is_error": False,
    }
    return json.dumps(payload), "", 0


def _codex_stdout(usage: dict[str, Any]) -> tuple[str, str, int]:
    lines = [
        json.dumps({"type": "thread.started", "thread_id": "th-1"}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "코덱스 답변"}}),
        json.dumps({"type": "turn.completed", "usage": usage}),
    ]
    return "\n".join(lines), "", 0


def _gemini_stdout(usage: dict[str, Any]) -> tuple[str, str, int]:
    payload = {
        "conversation_id": "conv-1", "status": "SUCCESS", "response": "제미나이 답변",
        "error": "", "duration_seconds": 1.2, "num_turns": 1, "usage": usage,
    }
    return json.dumps(payload), "", 0


ENGINE_FIXTURES: list[EngineFixture] = [
    EngineFixture(
        id="claude",
        engine_class=ClaudeEngine,
        configured_profile=_claude_configured,
        unconfigured_profile=_claude_unconfigured,
        resend_system_prompt_on_resume=True,
        resume_token="--resume",
        native_usage={
            "input_tokens": 10, "output_tokens": 20,
            "cache_creation_input_tokens": 3, "cache_read_input_tokens": 4,
        },
        expected_usage=Usage(input_tokens=10, output_tokens=20, cache_creation_tokens=3, cache_read_tokens=4),
        build_stdout=_claude_stdout,
    ),
    EngineFixture(
        id="codex",
        engine_class=CodexEngine,
        configured_profile=_codex_configured,
        unconfigured_profile=_codex_unconfigured,
        # developer_instructions is pinned to the first turn: the CLI ignores a
        # new value on resume (measured 2026-09-19). So codex carries this turn's
        # instructions in the prompt instead, like gemini (sca-ivs).
        resend_system_prompt_on_resume=True,
        resume_token="resume",
        native_usage={"input_tokens": 5, "output_tokens": 6},
        expected_usage=Usage(
            input_tokens=5, output_tokens=6,
            unavailable=frozenset({"cache_creation_tokens", "cache_read_tokens"}),
        ),
        build_stdout=_codex_stdout,
    ),
    EngineFixture(
        id="gemini",
        engine_class=GeminiEngine,
        configured_profile=_gemini_configured,
        unconfigured_profile=_gemini_unconfigured,
        # No pinned-system-prompt mechanism exists at all (unlike Codex's
        # first-turn-only pin), so the prompt is rebuilt with the system
        # prompt prefixed on every turn, resumed or not.
        resend_system_prompt_on_resume=True,
        resume_token="--conversation",
        native_usage={
            "input_tokens": 7, "output_tokens": 9, "thinking_tokens": 11,
            "cache_read_tokens": 3, "total_tokens": 30,
        },
        expected_usage=Usage(
            input_tokens=7, output_tokens=9, cache_creation_tokens=0, cache_read_tokens=3,
            unavailable=frozenset({"cache_creation_tokens"}),
        ),
        build_stdout=_gemini_stdout,
    ),
]


def _fixture_params() -> list[Any]:
    return [pytest.param(fx, id=fx.id) for fx in ENGINE_FIXTURES]


def _request(**overrides: Any) -> EngineRequest:
    base: dict[str, Any] = {
        "prompt": "안녕",
        "system_prompt": "시스템 지침",
        "session_id": "11111111-1111-1111-1111-111111111111",
        "resume": False,
        "model": "engine-model-x",
        "effort": "medium",
        "workdir": Path("/tmp/contract-work"),
        "readable_dirs": (Path("/tmp/read-a"), Path("/tmp/read-b")),
        "tools": ToolSelection.allow(["Read", "Grep"]),
        "trust_level": TrustLevel.GENERAL,
    }
    base.update(overrides)
    return EngineRequest(**base)


def _joined(cmd: list[str]) -> str:
    # Keep token boundaries so substring checks can't false-match across tokens.
    return "\x1f".join(cmd)


class _FakeCompleted:
    def __init__(self, stdout: str, stderr: str, returncode: int) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


@pytest.mark.parametrize("fx", _fixture_params())
class TestEngineContract:
    def test_로그인_만료_문구와_안내를_댄다(self, fx: EngineFixture, tmp_path: Path) -> None:
        """엔진이 문구를 안 대면 그 엔진만 로그인이 풀려도 전환이 안 된다.
        판정은 Engine 한 자리에 있으므로 엔진이 댈 것은 이 둘뿐이다 (sca-sj8r).
        """
        engine = fx.engine_class(fx.configured_profile(tmp_path), SETTINGS)
        assert engine.AUTH_FAILURE_MARKERS, fx.id
        assert "로그인" in engine.AUTH_FAILURE_NOTE, fx.id

    def test_재개_여부가_실제로_다른_명령을_만든다(self, fx: EngineFixture, tmp_path: Path) -> None:
        engine = fx.engine_class(fx.configured_profile(tmp_path), SETTINGS)
        cmd_new = engine.build_command(_request(resume=False))
        cmd_resume = engine.build_command(_request(resume=True))
        assert cmd_new != cmd_resume

    def test_재개_세션은_세션_id를_명령에_담는다(self, fx: EngineFixture, tmp_path: Path) -> None:
        engine = fx.engine_class(fx.configured_profile(tmp_path), SETTINGS)
        cmd = engine.build_command(_request(resume=True, session_id="고유-세션-id-값"))
        assert "고유-세션-id-값" in _joined(cmd)

    def test_재개_전용_토큰은_resume일때만_나온다(self, fx: EngineFixture, tmp_path: Path) -> None:
        engine = fx.engine_class(fx.configured_profile(tmp_path), SETTINGS)
        cmd_new = engine.build_command(_request(resume=False))
        cmd_resume = engine.build_command(_request(resume=True))
        assert fx.resume_token in cmd_resume
        assert fx.resume_token not in cmd_new

    def test_시스템_프롬프트는_새_세션_명령에_실린다(self, fx: EngineFixture, tmp_path: Path) -> None:
        engine = fx.engine_class(fx.configured_profile(tmp_path), SETTINGS)
        cmd = engine.build_command(_request(resume=False, system_prompt="고유한-지침-문구"))
        assert "고유한-지침-문구" in _joined(cmd)

    def test_재개_세션의_시스템_프롬프트_반영_여부는_엔진_설계대로다(
        self, fx: EngineFixture, tmp_path: Path
    ) -> None:
        engine = fx.engine_class(fx.configured_profile(tmp_path), SETTINGS)
        cmd = engine.build_command(_request(resume=True, system_prompt="고유한-지침-문구"))
        assert ("고유한-지침-문구" in _joined(cmd)) == fx.resend_system_prompt_on_resume

    def test_재개_턴의_시스템_지침은_그_턴의_값이다(self, fx: EngineFixture, tmp_path: Path) -> None:
        """턴마다 바뀌는 값은 system_prompt 로 실려 재개 턴에도 갱신된다.
        엔진별 턴 지시 훅이 따로 필요 없는 근거다(sca-r1hc)."""
        engine = fx.engine_class(fx.configured_profile(tmp_path), SETTINGS)
        앞턴 = engine.build_command(_request(resume=True, system_prompt="앞턴-지침-문구"))
        뒤턴 = engine.build_command(_request(resume=True, system_prompt="뒤턴-지침-문구"))
        assert "뒤턴-지침-문구" in _joined(뒤턴)
        assert "앞턴-지침-문구" not in _joined(뒤턴)
        assert 앞턴 != 뒤턴

    def test_작업_디렉터리는_실행기가_그대로_넘긴다(self, fx: EngineFixture, tmp_path: Path) -> None:
        engine = fx.engine_class(fx.configured_profile(tmp_path), SETTINGS)
        seen: dict[str, Any] = {}

        def fake_run(cmd: list[str], cwd: str, timeout: float, **_: Any) -> _FakeCompleted:
            seen["cwd"] = cwd
            return _FakeCompleted(*fx.build_stdout(fx.native_usage))

        runner = EngineRunner(SETTINGS, subprocess_runner=fake_run)
        workdir = tmp_path / "workdir-반영-확인"
        runner.run(engine, _request(workdir=workdir))
        assert seen["cwd"] == str(workdir)

    def test_읽기_허용_경로가_명령에_반영된다(self, fx: EngineFixture, tmp_path: Path) -> None:
        engine = fx.engine_class(fx.configured_profile(tmp_path), SETTINGS)
        dirs = (Path("/tmp/read-only-a-고유"), Path("/tmp/read-only-b-고유"))
        cmd = engine.build_command(_request(resume=False, readable_dirs=dirs))
        joined = _joined(cmd)
        assert "/tmp/read-only-a-고유" in joined
        assert "/tmp/read-only-b-고유" in joined

    def test_모델이_명령에_반영된다(self, fx: EngineFixture, tmp_path: Path) -> None:
        engine = fx.engine_class(fx.configured_profile(tmp_path), SETTINGS)
        cmd = engine.build_command(_request(model="고유-모델-이름-9"))
        assert "고유-모델-이름-9" in _joined(cmd)

    def test_effort가_명령에_반영된다(self, fx: EngineFixture, tmp_path: Path) -> None:
        """엔진마다 받는 값의 집합이 달라 그 엔진이 받는 값으로 확인한다.
        집합은 엔진이 선언하고, 호출자의 어휘에서 그 집합으로 옮기는 자리는
        Engine 한 곳이다(sca-3kzk)."""
        engine = fx.engine_class(fx.configured_profile(tmp_path), SETTINGS)
        value = engine.supported_efforts[-1]
        cmd = engine.build_command(_request(effort=value))
        assert value in _joined(cmd)

    def test_effort가_비어도_명령에_유효한_값이_실린다(self, fx: EngineFixture, tmp_path: Path) -> None:
        """비었다고 생략하거나 빈 값을 그대로 실으면 그 턴이 어느 강도로 돌았는지가
        엔진마다 달라지고 감사 기록과도 어긋난다(sca-3kzk)."""
        engine = fx.engine_class(fx.configured_profile(tmp_path), SETTINGS)
        assert engine.resolve_effort(_request(effort="")) == DEFAULT_EFFORT
        assert DEFAULT_EFFORT in _joined(engine.build_command(_request(effort="")))

    def test_못_받는_effort는_그_엔진이_받는_값으로_내려간다(
        self, fx: EngineFixture, tmp_path: Path
    ) -> None:
        """사다리 맨 위 값은 엔진에 따라 거부된다(agy 는 xhigh 를 거부한다).
        그래도 명령에는 그 엔진이 받는 값이 하나 실려 있어야 한다."""
        engine = fx.engine_class(fx.configured_profile(tmp_path), SETTINGS)
        for value in (*EFFORT_LEVELS, "사다리에-없는-값"):
            resolved = engine.resolve_effort(_request(effort=value))
            assert resolved in engine.supported_efforts, f"{fx.id}: {value}"
            assert resolved in _joined(engine.build_command(_request(effort=value)))

    def test_usage_매핑이_엔진_고유_출력에서_올바로_들어간다(self, fx: EngineFixture, tmp_path: Path) -> None:
        engine = fx.engine_class(fx.configured_profile(tmp_path), SETTINGS)
        stdout, stderr, returncode = fx.build_stdout(fx.native_usage)
        resp = engine.parse(stdout, stderr, returncode)
        assert resp.usage == fx.expected_usage

    def test_응답에_실행한_엔진_이름이_박힌다(self, fx: EngineFixture, tmp_path: Path) -> None:
        """보고 쪽이 이 이름으로 기록 리더를 고른다. 비어 있으면 fallback 응답도
        primary 형식으로 읽는다."""
        engine = fx.engine_class(fx.configured_profile(tmp_path), SETTINGS)

        def fake_run(cmd: list[str], cwd: str, timeout: float, **_: Any) -> _FakeCompleted:
            return _FakeCompleted(*fx.build_stdout(fx.native_usage))

        runner = EngineRunner(SETTINGS, subprocess_runner=fake_run)
        assert runner.run(engine, _request()).engine == engine.name

    def test_시간초과_응답에도_엔진_이름이_박힌다(self, fx: EngineFixture, tmp_path: Path) -> None:
        engine = fx.engine_class(fx.configured_profile(tmp_path), SETTINGS)

        def fake_run(cmd: list[str], cwd: str, timeout: float, **_: Any) -> _FakeCompleted:
            raise subprocess.TimeoutExpired(cmd, timeout)

        runner = EngineRunner(SETTINGS, subprocess_runner=fake_run)
        assert runner.run(engine, _request()).engine == engine.name

    def test_설정이_모자라면_ConfigError다(self, fx: EngineFixture, tmp_path: Path) -> None:
        engine = fx.engine_class(fx.unconfigured_profile(tmp_path), SETTINGS)
        with pytest.raises(ConfigError):
            engine.build_command(_request())


@pytest.mark.parametrize("fx", _fixture_params())
def test_허용_도구_목록이_명령에_반영된다(fx: EngineFixture, tmp_path: Path) -> None:
    """명령에 나타나는 것과 그 엔진이 강제하는 것은 다르다.

    claude 는 --tools 로 도구 집합 자체를 닫는다. codex 와 gemini 는 닫을
    인자가 없어 프롬프트 문구로만 싣는다 (sca-f9k0). 강제 수준의 차이는
    capabilities_for 가 내는 tool_restriction 에 남고, 이 계약은 "요청이
    무엇을 허용했는지가 엔진에 전달은 된다" 까지만 본다.
    """
    engine = fx.engine_class(fx.configured_profile(tmp_path), SETTINGS)
    cmd = engine.build_command(_request(tools=ToolSelection.allow(["전용도구A", "전용도구B"])))
    joined = _joined(cmd)
    assert "전용도구A" in joined
    assert "전용도구B" in joined


@pytest.mark.parametrize("fx", _fixture_params())
def test_허용목록을_강제하거나_문구로_싣거나_둘_중_하나다(fx: EngineFixture, tmp_path: Path) -> None:
    """명령에 이름이 보이는 것으로는 강제와 알림이 구분되지 않는다.

    도구 집합을 실제로 닫는 엔진은 문구를 안 싣는다. 같은 제약을 두 번
    말하는 것이 되고 전송 바이트만 늘기 때문이다. 못 닫는 엔진은 반드시
    싣는다. 안 실으면 허용목록이 모델에 아무 형태로도 안 닿는다.
    """
    engine = fx.engine_class(fx.configured_profile(tmp_path), SETTINGS)
    request = _request(tools=ToolSelection.allow(["전용도구A", "전용도구B"]))
    note = engine.tool_allow_note(request)
    강제한다 = engine.capabilities_for(request).tool_restriction is ToolRestriction.EXACT_ALLOWLIST
    assert 강제한다 is (note == "")
    if note:
        assert note in _joined(engine.build_command(request))


@pytest.mark.parametrize("fx", _fixture_params())
def test_프롬프트가_하이픈으로_시작해도_플래그로_안_읽힌다(
    fx: EngineFixture, tmp_path: Path
) -> None:
    """사용자가 "--version ..." 같은 말을 던지면 CLI 가 그것을 플래그로 읽는다.
    codex 가 실제로 종료 코드 2 로 죽었다(2026-09-15, 실 CLI 스모크 시험).
    엔진마다 막는 방법이 다르므로 계약은 "프롬프트 바로 앞에 -- 가 있거나
    프롬프트가 값 자리에 있다" 로 둔다."""
    engine = fx.engine_class(fx.configured_profile(tmp_path), SETTINGS)
    prompt = "--version 이라는 말은 빼고 답하라"
    cmd = engine.build_command(_request(prompt=prompt))

    # 엔진에 따라 시스템 지침을 프롬프트 앞에 붙이므로 정확히 일치하지 않는다.
    carrier = next(i for i, arg in enumerate(cmd) if prompt in arg)
    assert carrier > 0, "프롬프트가 명령의 첫 인자로 들어갔다"
    assert cmd[carrier - 1] in ("--", "-p"), (
        f"{fx.id}: 프롬프트 앞이 {cmd[carrier - 1]!r} 이다. "
        "하이픈으로 시작하는 프롬프트가 플래그로 읽힌다"
    )


@pytest.mark.parametrize("fx", _fixture_params())
def test_새_세션_id는_그_CLI가_받는_형식이다(fx: EngineFixture, tmp_path: Path) -> None:
    """세 CLI 모두 하이픈 포함 UUID 를 요구한다. 클로드는 하이픈 없는 32자에
    "Error: Invalid session ID. Must be a valid UUID." 로 거부한다(sca-56y 실측).

    형식을 여기서 고정해 둬야, 세션 ID 를 직접 만드는 호출자가 생겼을 때
    무엇을 어겼는지가 드러난다.
    """
    engine = fx.engine_class(fx.configured_profile(tmp_path), SETTINGS)
    session_id = engine.new_session_id()
    assert uuid.UUID(session_id)
    assert str(uuid.UUID(session_id)) == session_id
