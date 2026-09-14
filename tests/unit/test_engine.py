"""엔진 계층 — Engine 인터페이스, Registry, Claude/Codex 어댑터, 전환, 실행."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from slack_cli_agent.config.profile import EngineSpec, Profile
from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.core.errors import ConfigError
from slack_cli_agent.engine.base import (
    Engine,
    EngineRequest,
    EngineResponse,
    TrustLevel,
    Usage,
    UsageLimit,
)
from slack_cli_agent.engine.claude import ClaudeEngine
from slack_cli_agent.engine.codex import CodexEngine
from slack_cli_agent.engine.registry import EngineRegistry
from slack_cli_agent.engine.runner import EngineRunner, FallbackEngine
from slack_cli_agent.engine.switcher import EngineSwitcher

SETTINGS = RuntimeSettings()


def profile_with(primary: dict, fallback: dict | None = None, tmp_path: Path | None = None) -> Profile:
    data = {
        "name": "example",
        "primary_engine": primary,
        "owner_user_id": "U1",
        "troubleshoot_channel": "C1",
    }
    if fallback:
        data["fallback_engine"] = fallback
    if tmp_path is not None:
        data["state_dir"] = str(tmp_path / "state")
    return Profile.from_dict(data)


def claude_profile(tmp_path: Path, **extra: Any) -> Profile:
    primary = {"type": "claude", "binary": "claude", "model": "claude-sonnet-5", **extra}
    return profile_with(primary, tmp_path=tmp_path)


def request(**overrides: Any) -> EngineRequest:
    base = dict(
        prompt="안녕",
        system_prompt="시스템 지침",
        session_id="11111111-1111-1111-1111-111111111111",
        resume=False,
        model="claude-sonnet-5",
        effort="medium",
        workdir=Path("/tmp/work"),
        readable_dirs=(Path("/tmp/a"), Path("/tmp/b")),
        allowed_tools=("Read", "Grep"),
        trust_level=TrustLevel.GENERAL,
    )
    base.update(overrides)
    return EngineRequest(**base)


class FakeCompleted:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


# ---------------------------------------------------------------------------
# 값 객체


class TestUsage:
    def test_claude_형식_사전을_그대로_읽는다(self) -> None:
        usage = Usage.from_mapping({
            "input_tokens": 10,
            "output_tokens": 20,
            "cache_creation_input_tokens": 3,
            "cache_read_input_tokens": 4,
        })
        assert usage == Usage(input_tokens=10, output_tokens=20,
                              cache_creation_tokens=3, cache_read_tokens=4)

    def test_사전이_아니면_전부_0이다(self) -> None:
        assert Usage.from_mapping(None) == Usage()
        assert Usage.from_mapping("문자열") == Usage()

    def test_누락된_키는_0으로_채운다(self) -> None:
        usage = Usage.from_mapping({"input_tokens": 5})
        assert usage.output_tokens == 0


class TestEngineRequestResponse:
    def test_요청은_불변이다(self) -> None:
        req = request()
        with pytest.raises(Exception):
            req.prompt = "다른 말"  # type: ignore[misc]

    def test_응답은_불변이고_기본값을_가진다(self) -> None:
        resp = EngineResponse(
            ok=True, body="답", session_id="s", model_actual="m",
            elapsed=1.0, turns=1, usage=None,
        )
        assert resp.raw == {}
        assert resp.failure_reason is None


# ---------------------------------------------------------------------------
# Engine ABC 와 Registry


class TestEngineABC:
    def test_직접_인스턴스화할_수_없다(self) -> None:
        with pytest.raises(TypeError):
            Engine(profile=None, settings=SETTINGS)  # type: ignore[abstract]

    def test_기본_메서드는_빈_값을_돌려준다(self, tmp_path: Path) -> None:
        profile = claude_profile(tmp_path)
        engine = ClaudeEngine(profile, SETTINGS)
        assert engine.session_id_from(EngineResponse(
            ok=True, body="", session_id=None, model_actual=None,
            elapsed=0, turns=None, usage=None,
        )) is None
        assert engine.directives_for_turn(request()) == ""
        assert engine.readable_paths_note((Path("/x"),)) == ""


class TestEngineRegistry:
    def test_등록한_엔진을_이름으로_만든다(self, tmp_path: Path) -> None:
        registry = EngineRegistry()
        registry.register(ClaudeEngine)
        profile = claude_profile(tmp_path)
        engine = registry.create("claude", profile, SETTINGS)
        assert isinstance(engine, ClaudeEngine)

    def test_등록되지_않은_이름은_설정_오류다(self, tmp_path: Path) -> None:
        registry = EngineRegistry()
        profile = claude_profile(tmp_path)
        with pytest.raises(ConfigError):
            registry.create("codex", profile, SETTINGS)

    def test_이용_가능한_이름_목록(self) -> None:
        registry = EngineRegistry()
        registry.register(ClaudeEngine)
        registry.register(CodexEngine)
        assert registry.available() == ["claude", "codex"]


# ---------------------------------------------------------------------------
# ClaudeEngine


class TestClaudeEngineBuildCommand:
    def test_새_세션은_session_id_옵션을_쓴다(self, tmp_path: Path) -> None:
        profile = claude_profile(tmp_path)
        engine = ClaudeEngine(profile, SETTINGS)
        cmd = engine.build_command(request(resume=False))
        assert cmd[0] == "claude"
        assert "--session-id" in cmd
        assert "--resume" not in cmd
        idx = cmd.index("--session-id")
        assert cmd[idx + 1] == "11111111-1111-1111-1111-111111111111"

    def test_재개_세션은_resume_옵션을_쓴다(self, tmp_path: Path) -> None:
        profile = claude_profile(tmp_path)
        engine = ClaudeEngine(profile, SETTINGS)
        cmd = engine.build_command(request(resume=True))
        assert "--resume" in cmd
        assert "--session-id" not in cmd

    def test_읽기_경로마다_add_dir을_붙인다(self, tmp_path: Path) -> None:
        profile = claude_profile(tmp_path)
        engine = ClaudeEngine(profile, SETTINGS)
        cmd = engine.build_command(request())
        add_dir_positions = [i for i, tok in enumerate(cmd) if tok == "--add-dir"]
        assert len(add_dir_positions) == 2
        assert cmd[add_dir_positions[0] + 1] == "/tmp/a"
        assert cmd[add_dir_positions[1] + 1] == "/tmp/b"

    def test_프롬프트는_이중_대시_뒤에_마지막으로_온다(self, tmp_path: Path) -> None:
        profile = claude_profile(tmp_path)
        engine = ClaudeEngine(profile, SETTINGS)
        cmd = engine.build_command(request(prompt="-rf 로 시작하는 말"))
        assert cmd[-2:] == ["--", "-rf 로 시작하는 말"]

    def test_허용_도구_목록을_쉼표로_잇는다(self, tmp_path: Path) -> None:
        profile = claude_profile(tmp_path)
        engine = ClaudeEngine(profile, SETTINGS)
        cmd = engine.build_command(request(allowed_tools=("Read", "Grep", "Glob")))
        idx = cmd.index("--allowedTools")
        assert cmd[idx + 1] == "Read,Grep,Glob"

    def test_모델과_effort를_전달한다(self, tmp_path: Path) -> None:
        profile = claude_profile(tmp_path)
        engine = ClaudeEngine(profile, SETTINGS)
        cmd = engine.build_command(request(model="claude-opus-5", effort="high"))
        assert cmd[cmd.index("--model") + 1] == "claude-opus-5"
        assert cmd[cmd.index("--effort") + 1] == "high"


class TestClaudeEngineNewSessionId:
    def test_uuid_형식을_돌려준다(self, tmp_path: Path) -> None:
        import uuid

        profile = claude_profile(tmp_path)
        engine = ClaudeEngine(profile, SETTINGS)
        sid = engine.new_session_id()
        assert uuid.UUID(sid)


class TestClaudeEngineParse:
    def test_정상_응답을_그대로_읽는다(self, tmp_path: Path) -> None:
        profile = claude_profile(tmp_path)
        engine = ClaudeEngine(profile, SETTINGS)
        payload = {
            "result": "답변 본문",
            "session_id": "s1",
            "model": "claude-sonnet-5",
            "num_turns": 3,
            "usage": {"input_tokens": 1, "output_tokens": 2},
            "is_error": False,
        }
        resp = engine.parse(json.dumps(payload), "", 0)
        assert resp.ok is True
        assert resp.body == "답변 본문"
        assert resp.session_id == "s1"
        assert resp.turns == 3
        assert resp.usage == Usage(input_tokens=1, output_tokens=2)
        assert resp.failure_reason is None

    def test_결과가_비면_실패로_본다(self, tmp_path: Path) -> None:
        profile = claude_profile(tmp_path)
        engine = ClaudeEngine(profile, SETTINGS)
        resp = engine.parse(json.dumps({"result": "  ", "is_error": False}), "", 0)
        assert resp.ok is False
        assert resp.failure_reason == "empty_response"

    def test_비정상_종료는_nonzero_exit이다(self, tmp_path: Path) -> None:
        profile = claude_profile(tmp_path)
        engine = ClaudeEngine(profile, SETTINGS)
        resp = engine.parse("이상한 출력", "에러", 1)
        assert resp.ok is False
        assert resp.failure_reason == "nonzero_exit"

    def test_json이_아니면_bad_json이다(self, tmp_path: Path) -> None:
        profile = claude_profile(tmp_path)
        engine = ClaudeEngine(profile, SETTINGS)
        resp = engine.parse("{이건 json이 아니다", "", 0)
        assert resp.failure_reason == "bad_json"

    def test_429_상태코드는_비정상_종료여도_한도_소진이다(self, tmp_path: Path) -> None:
        """subtype 은 success 인데 api_error_status 가 429 인 경우.

        2026-09-11 실측 — 종료 코드만 보면 못 잡는다. 원본 usage_limit_message().
        """
        profile = claude_profile(tmp_path)
        engine = ClaudeEngine(profile, SETTINGS)
        payload = {"subtype": "success", "api_error_status": 429, "result": "limit · resets 3am"}
        resp = engine.parse(json.dumps(payload), "", 1)
        assert resp.ok is False
        assert resp.failure_reason == "usage_limit"

    def test_한도_힌트_문구도_잡는다(self, tmp_path: Path) -> None:
        profile = claude_profile(tmp_path)
        engine = ClaudeEngine(profile, SETTINGS)
        payload = {"subtype": "success", "result": "You have hit your limit for this week"}
        resp = engine.parse(json.dumps(payload), "", 1)
        assert resp.failure_reason == "usage_limit"

    def test_is_error_subtype에_limit이_있으면_한도_소진이다(self, tmp_path: Path) -> None:
        profile = claude_profile(tmp_path)
        engine = ClaudeEngine(profile, SETTINGS)
        payload = {"is_error": True, "subtype": "error_max_limit", "result": ""}
        resp = engine.parse(json.dumps(payload), "", 0)
        assert resp.failure_reason == "usage_limit"

    def test_일반_is_error는_한도가_아니다(self, tmp_path: Path) -> None:
        profile = claude_profile(tmp_path)
        engine = ClaudeEngine(profile, SETTINGS)
        payload = {"is_error": True, "subtype": "error_during_execution"}
        resp = engine.parse(json.dumps(payload), "", 0)
        assert resp.ok is False
        assert resp.failure_reason == "is_error"


class TestClaudeEngineDetectUsageLimit:
    def test_한도_소진이_아니면_none이다(self, tmp_path: Path) -> None:
        profile = claude_profile(tmp_path)
        engine = ClaudeEngine(profile, SETTINGS)
        resp = engine.parse(json.dumps({"result": "정상 답변"}), "", 0)
        assert engine.detect_usage_limit(resp) is None

    def test_한도_소진이면_detail을_담아_돌려준다(self, tmp_path: Path) -> None:
        profile = claude_profile(tmp_path)
        engine = ClaudeEngine(profile, SETTINGS)
        payload = {"subtype": "success", "api_error_status": 429, "result": "weekly limit · resets 3am"}
        resp = engine.parse(json.dumps(payload), "", 1)
        limit = engine.detect_usage_limit(resp)
        assert isinstance(limit, UsageLimit)
        assert "resets" in limit.detail


# ---------------------------------------------------------------------------
# CodexEngine


def codex_profile(tmp_path: Path, options: dict | None = None) -> Profile:
    fallback = {"type": "codex", "binary": "codex", "model": "gpt-5.6-sol",
               "options": options or {}}
    return profile_with(
        {"type": "claude", "binary": "claude", "model": "claude-sonnet-5"},
        fallback=fallback, tmp_path=tmp_path,
    )


class TestCodexEngineBuildCommand:
    def test_새_스레드는_developer_instructions와_sandbox_workdir을_쓴다(self, tmp_path: Path) -> None:
        profile = codex_profile(tmp_path)
        engine = CodexEngine(profile, SETTINGS)
        cmd = engine.build_command(request(resume=False, model="gpt-5.6-sol"))
        assert cmd[0] == "codex"
        assert cmd[1] == "exec"
        assert "resume" not in cmd
        assert any(tok.startswith("developer_instructions=") for tok in cmd)
        assert "--sandbox" in cmd
        assert "-C" in cmd
        assert cmd[-1] == "안녕"

    def test_재개_스레드는_resume과_sandbox_mode를_쓰고_지침을_안_실는다(self, tmp_path: Path) -> None:
        profile = codex_profile(tmp_path)
        engine = CodexEngine(profile, SETTINGS)
        cmd = engine.build_command(request(resume=True, model="gpt-5.6-sol",
                                           session_id="thread-abc"))
        assert cmd[1] == "exec"
        assert cmd[2] == "resume"
        assert not any(tok.startswith("developer_instructions=") for tok in cmd)
        assert any("sandbox_mode=" in tok for tok in cmd)
        assert "thread-abc" in cmd
        assert cmd[-1] == "안녕"

    def test_옵션의_sandbox값을_쓴다(self, tmp_path: Path) -> None:
        profile = codex_profile(tmp_path, options={"sandbox": "workspace-write"})
        engine = CodexEngine(profile, SETTINGS)
        cmd = engine.build_command(request(resume=False, model="gpt-5.6-sol"))
        idx = cmd.index("--sandbox")
        assert cmd[idx + 1] == "workspace-write"


class TestCodexEngineParse:
    def test_jsonl에서_본문과_thread_id를_읽는다(self, tmp_path: Path) -> None:
        profile = codex_profile(tmp_path)
        engine = CodexEngine(profile, SETTINGS)
        lines = [
            json.dumps({"type": "thread.started", "thread_id": "th-1"}),
            json.dumps({"type": "item.completed",
                       "item": {"type": "agent_message", "text": "코덱스 답변"}}),
            json.dumps({"type": "turn.completed",
                       "usage": {"input_tokens": 5, "output_tokens": 6}}),
        ]
        resp = engine.parse("\n".join(lines), "", 0)
        assert resp.ok is True
        assert resp.body == "코덱스 답변"
        assert resp.session_id == "th-1"
        assert resp.usage == Usage(input_tokens=5, output_tokens=6)

    def test_본문이_없으면_empty_response다(self, tmp_path: Path) -> None:
        profile = codex_profile(tmp_path)
        engine = CodexEngine(profile, SETTINGS)
        resp = engine.parse(json.dumps({"type": "thread.started", "thread_id": "th-2"}), "", 0)
        assert resp.ok is False
        assert resp.failure_reason == "empty_response"
        assert resp.session_id == "th-2"

    def test_비정상_종료는_nonzero_exit이다(self, tmp_path: Path) -> None:
        profile = codex_profile(tmp_path)
        engine = CodexEngine(profile, SETTINGS)
        resp = engine.parse("", "실패", 1)
        assert resp.failure_reason == "nonzero_exit"

    def test_jsonl이_아니면_bad_json이다(self, tmp_path: Path) -> None:
        profile = codex_profile(tmp_path)
        engine = CodexEngine(profile, SETTINGS)
        resp = engine.parse("이건 jsonl이 아니다", "", 0)
        assert resp.failure_reason == "bad_json"


class TestCodexEngineSessionIdFrom:
    def test_thread_id를_그대로_돌려준다(self, tmp_path: Path) -> None:
        profile = codex_profile(tmp_path)
        engine = CodexEngine(profile, SETTINGS)
        resp = engine.parse(
            "\n".join([
                json.dumps({"type": "thread.started", "thread_id": "th-9"}),
                json.dumps({"type": "item.completed",
                           "item": {"type": "agent_message", "text": "답"}}),
            ]),
            "", 0,
        )
        assert engine.session_id_from(resp) == "th-9"


class TestCodexEngineTurnBehavior:
    def test_readable_paths_note는_경로를_문장으로_만든다(self, tmp_path: Path) -> None:
        profile = codex_profile(tmp_path)
        engine = CodexEngine(profile, SETTINGS)
        note = engine.readable_paths_note((Path("/a"), Path("/b")))
        assert "/a" in note
        assert "/b" in note

    def test_경로가_없으면_빈_문자열이다(self, tmp_path: Path) -> None:
        profile = codex_profile(tmp_path)
        engine = CodexEngine(profile, SETTINGS)
        assert engine.readable_paths_note(()) == ""

    def test_directives_for_turn은_재개_턴에만_붙는다(self, tmp_path: Path) -> None:
        profile = codex_profile(tmp_path)
        engine = CodexEngine(profile, SETTINGS)
        assert engine.directives_for_turn(request(resume=False)) == ""
        assert engine.directives_for_turn(request(resume=True)) != ""


# ---------------------------------------------------------------------------
# EngineSwitcher


class TestEngineSwitcher:
    def test_상태_파일이_없으면_전환_아님이다(self, tmp_path: Path) -> None:
        switcher = EngineSwitcher(tmp_path / "engine_state.json")
        assert switcher.is_switched() is False
        assert switcher.is_approved() is False
        assert switcher.load() == {}

    def test_전환은_즉시_기록되고_승인은_아직이다(self, tmp_path: Path) -> None:
        switcher = EngineSwitcher(tmp_path / "engine_state.json")
        switcher.begin_switch("weekly limit · resets 3am", engine_name="codex")
        assert switcher.is_switched() is True
        assert switcher.is_approved() is False

    def test_승인하면_승인_상태가_된다(self, tmp_path: Path) -> None:
        switcher = EngineSwitcher(tmp_path / "engine_state.json")
        switcher.begin_switch("detail", engine_name="codex")
        switcher.approve()
        assert switcher.is_approved() is True

    def test_거부하면_승인_상태가_아니다(self, tmp_path: Path) -> None:
        switcher = EngineSwitcher(tmp_path / "engine_state.json")
        switcher.begin_switch("detail", engine_name="codex")
        switcher.deny()
        assert switcher.is_approved() is False
        assert switcher.load()["approval"] == "denied"

    def test_복구하면_상태_파일이_사라진다(self, tmp_path: Path) -> None:
        path = tmp_path / "engine_state.json"
        switcher = EngineSwitcher(path)
        switcher.begin_switch("detail", engine_name="codex")
        assert path.exists()
        switcher.recover()
        assert not path.exists()
        assert switcher.is_switched() is False

    def test_주기가_지나지_않으면_다시_떠보지_않는다(self, tmp_path: Path) -> None:
        switcher = EngineSwitcher(tmp_path / "engine_state.json", probe_interval_sec=600)
        switcher.begin_switch("detail", engine_name="codex")
        assert switcher.should_probe(time_now := switcher.load()["last_probe_at"] + 10) is False

    def test_주기가_지나면_다시_떠본다(self, tmp_path: Path) -> None:
        switcher = EngineSwitcher(tmp_path / "engine_state.json", probe_interval_sec=600)
        switcher.begin_switch("detail", engine_name="codex")
        started_at = switcher.load()["last_probe_at"]
        assert switcher.should_probe(started_at + 601) is True

    def test_떠본_시각을_남기면_다음_판정이_그때부터다(self, tmp_path: Path) -> None:
        switcher = EngineSwitcher(tmp_path / "engine_state.json", probe_interval_sec=600)
        switcher.begin_switch("detail", engine_name="codex")
        switcher.mark_probed(1_000_000.0)
        assert switcher.load()["last_probe_at"] == 1_000_000.0
        assert switcher.should_probe(1_000_000.0 + 10) is False

    def test_한도_안내_문구에_사유를_담는다(self, tmp_path: Path) -> None:
        switcher = EngineSwitcher(tmp_path / "engine_state.json")
        switcher.begin_switch("weekly limit · resets 3am", engine_name="codex")
        assert "weekly limit" in switcher.limit_reply()


# ---------------------------------------------------------------------------
# EngineRunner


class RecordingEngine(Engine):
    """실제 CLI 를 부르지 않는 대역. build_command/parse 호출을 기록한다."""

    name = "fake"

    def __init__(self, profile, settings, response: EngineResponse | None = None) -> None:
        super().__init__(profile, settings)
        self.built: list[EngineRequest] = []
        self.parsed: list[tuple[str, str, int]] = []
        self._response = response

    def build_command(self, request: EngineRequest) -> list[str]:
        self.built.append(request)
        return ["fake-bin", request.prompt]

    def parse(self, stdout: str, stderr: str, returncode: int) -> EngineResponse:
        self.parsed.append((stdout, stderr, returncode))
        if self._response is not None:
            return self._response
        return EngineResponse(
            ok=(returncode == 0), body=stdout, session_id=None, model_actual=None,
            elapsed=0.0, turns=None, usage=None,
        )

    def new_session_id(self) -> str:
        return "fake-session"

    def detect_usage_limit(self, response: EngineResponse) -> UsageLimit | None:
        if response.failure_reason == "usage_limit":
            return UsageLimit(detail=response.body, source="hint")
        return None


class TestEngineRunner:
    def test_엔진이_만든_명령을_실행하고_결과를_파싱한다(self, tmp_path: Path) -> None:
        profile = claude_profile(tmp_path)
        engine = RecordingEngine(profile, SETTINGS)
        calls: list[Any] = []

        def fake_subprocess(cmd, cwd, timeout):
            calls.append((cmd, cwd, timeout))
            return FakeCompleted(stdout="답변", returncode=0)

        runner = EngineRunner(SETTINGS, subprocess_runner=fake_subprocess)
        resp = runner.run(engine, request())
        assert resp.ok is True
        assert resp.body == "답변"
        assert calls[0][0] == ["fake-bin", "안녕"]
        assert engine.built == [request()]

    def test_시간초과면_timeout_실패를_돌려준다(self, tmp_path: Path) -> None:
        profile = claude_profile(tmp_path)
        engine = RecordingEngine(profile, SETTINGS)

        def fake_subprocess(cmd, cwd, timeout):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

        runner = EngineRunner(SETTINGS, subprocess_runner=fake_subprocess)
        resp = runner.run(engine, request())
        assert resp.ok is False
        assert resp.failure_reason == "timeout"


# ---------------------------------------------------------------------------
# FallbackEngine


class TestFallbackEngine:
    def _fallback(self, tmp_path: Path, primary_response=None, secondary_response=None):
        profile = codex_profile(tmp_path)
        primary = RecordingEngine(profile, SETTINGS, response=primary_response)
        primary.name = "claude"
        secondary = RecordingEngine(profile, SETTINGS, response=secondary_response)
        secondary.name = "codex"
        switcher = EngineSwitcher(tmp_path / "engine_state.json")
        calls: list[Any] = []

        def fake_subprocess(cmd, cwd, timeout):
            calls.append(cmd)
            return FakeCompleted(stdout="", returncode=0)

        runner = EngineRunner(SETTINGS, subprocess_runner=fake_subprocess)
        return FallbackEngine(primary, secondary, switcher, runner), primary, secondary, switcher

    def test_평소에는_1차_엔진으로_돈다(self, tmp_path: Path) -> None:
        ok_response = EngineResponse(ok=True, body="1차 응답", session_id="s1",
                                     model_actual=None, elapsed=0, turns=None, usage=None)
        fallback, primary, secondary, switcher = self._fallback(
            tmp_path, primary_response=ok_response)
        resp = fallback.run(request())
        assert resp.body == "1차 응답"
        assert switcher.is_switched() is False
        assert len(secondary.parsed) == 0

    def test_1차가_한도_소진이면_전환_상태를_즉시_남기고_이번_답은_그대로_낸다(self, tmp_path: Path) -> None:
        limit_response = EngineResponse(
            ok=False, body="한도 소진", session_id=None, model_actual=None,
            elapsed=0, turns=None, usage=None, failure_reason="usage_limit",
        )
        probe_response = EngineResponse(ok=True, body="OK", session_id=None,
                                        model_actual=None, elapsed=0, turns=None, usage=None)
        fallback, primary, secondary, switcher = self._fallback(
            tmp_path, primary_response=limit_response, secondary_response=probe_response)
        resp = fallback.run(request())
        assert resp.body == "한도 소진"
        assert switcher.is_switched() is True
        assert switcher.is_approved() is False

    def test_전환됐지만_승인_전이면_한도_안내만_낸다(self, tmp_path: Path) -> None:
        fallback, primary, secondary, switcher = self._fallback(tmp_path)
        switcher.begin_switch("weekly limit", engine_name="codex")
        resp = fallback.run(request())
        assert resp.ok is False
        assert resp.failure_reason == "usage_limit"
        assert len(primary.parsed) == 0
        assert len(secondary.parsed) == 0

    def test_승인되면_2차_엔진으로_돈다(self, tmp_path: Path) -> None:
        secondary_ok = EngineResponse(ok=True, body="코덱스 답변", session_id="th-1",
                                      model_actual=None, elapsed=0, turns=None, usage=None)
        fallback, primary, secondary, switcher = self._fallback(
            tmp_path, secondary_response=secondary_ok)
        switcher.begin_switch("weekly limit", engine_name="codex")
        switcher.approve()
        resp = fallback.run(request())
        assert resp.body == "코덱스 답변"
        assert len(secondary.built) == 1
        # 세션은 이어받지 못한다. 새 세션으로 연다.
        assert secondary.built[0].session_id == "fake-session"
        assert secondary.built[0].resume is False

    def test_주기가_지나_1차가_돌아왔으면_상태를_지운다(self, tmp_path: Path) -> None:
        recovered = EngineResponse(ok=True, body="다시 됩니다", session_id="s2",
                                   model_actual=None, elapsed=0, turns=None, usage=None)
        fallback, primary, secondary, switcher = self._fallback(
            tmp_path, primary_response=recovered)
        switcher.begin_switch("weekly limit", engine_name="codex")
        switcher.mark_probed(switcher.load()["switched_at"] - 700)
        resp = fallback.run(request())
        assert resp.body == "다시 됩니다"
        assert switcher.is_switched() is False


class Test엔진환경격리:
    """엔진 하위 프로세스에 넘길 환경 변수를 실행기가 실제로 제한하는가.

    제한하지 않으면 부모 프로세스의 환경을 통째로 물려받는다. 슬랙 토큰과
    다른 엔진의 자격증명이 그대로 넘어가고, 사용자 개인 설정과 세션을
    그대로 쓰게 돼 격리가 성립하지 않는다.
    """

    def test_정책을_주면_그_결과를_subprocess에_넘긴다(self, tmp_path: Path) -> None:
        seen: dict[str, Any] = {}

        def fake_run(cmd, cwd, timeout, env=None):
            seen["env"] = env
            return FakeCompleted(stdout="{}", returncode=0)

        class 고정정책:
            def build(self, source_env):
                return {"PATH": "/usr/bin", "BOT_PROFILE": "testbot"}

        runner = EngineRunner(
            RuntimeSettings(), subprocess_runner=fake_run, environment_policy=고정정책(),
        )
        runner.run(RecordingEngine(claude_profile(tmp_path), SETTINGS), request())
        assert seen["env"] == {"PATH": "/usr/bin", "BOT_PROFILE": "testbot"}

    def test_정책이_없으면_환경을_안_넘긴다(self, tmp_path: Path) -> None:
        """정책을 안 주는 연결이 아직 있다. 그때는 기존 동작을 유지한다 —
        빈 환경을 넘기면 엔진이 PATH 를 못 찾아 아예 실행되지 않는다."""
        seen: dict[str, Any] = {"env": "안 불림"}

        def fake_run(cmd, cwd, timeout, env=None):
            seen["env"] = env
            return FakeCompleted(stdout="{}", returncode=0)

        runner = EngineRunner(RuntimeSettings(), subprocess_runner=fake_run)
        runner.run(RecordingEngine(claude_profile(tmp_path), SETTINGS), request())
        assert seen["env"] is None
