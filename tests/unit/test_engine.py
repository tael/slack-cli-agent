"""엔진 계층 — Engine 인터페이스, Registry, Claude/Codex 어댑터, 전환, 실행."""

from __future__ import annotations

import dataclasses
import json
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from slack_cli_agent.config.profile import Profile
from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.core.errors import ConfigError
from slack_cli_agent.engine.base import (
    ElapsedSource,
    Engine,
    EngineRequest,
    EngineResponse,
    TrustLevel,
    Usage,
    UsageLimit,
    equivalent_values,
)
from slack_cli_agent.engine.claude import ClaudeEngine
from slack_cli_agent.engine.codex import CodexEngine
from slack_cli_agent.engine.gemini import GeminiEngine
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


def gemini_profile(tmp_path: Path, **extra: Any) -> Profile:
    primary = {"type": "gemini", "binary": "agy", "model": "gemini-3.8-flash", **extra}
    return profile_with(primary, tmp_path=tmp_path)


def request(**overrides: Any) -> EngineRequest:
    base = {
        "prompt": "안녕",
        "system_prompt": "시스템 지침",
        "session_id": "11111111-1111-1111-1111-111111111111",
        "resume": False,
        "model": "claude-sonnet-5",
        "effort": "medium",
        "workdir": Path("/tmp/work"),
        "readable_dirs": (Path("/tmp/a"), Path("/tmp/b")),
        "allowed_tools": ("Read", "Grep"),
        "trust_level": TrustLevel.GENERAL,
    }
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
    def test_고유_키_맵으로_전체_필드가_있으면_판정_불가가_없다(self) -> None:
        key_map = {
            "input_tokens": "input_tokens", "output_tokens": "output_tokens",
            "cache_creation_tokens": "cache_creation_input_tokens",
            "cache_read_tokens": "cache_read_input_tokens",
        }
        usage = Usage.from_native({
            "input_tokens": 10, "output_tokens": 20,
            "cache_creation_input_tokens": 3, "cache_read_input_tokens": 4,
        }, key_map)
        assert usage == Usage(input_tokens=10, output_tokens=20,
                              cache_creation_tokens=3, cache_read_tokens=4)

    def test_사전이_아니면_숫자는_전부_0이다(self) -> None:
        key_map = {"input_tokens": "in"}
        assert equivalent_values(Usage.from_native(None, key_map), Usage())
        assert equivalent_values(Usage.from_native("문자열", key_map), Usage())

    def test_누락된_키는_0으로_채운다(self) -> None:
        key_map = {"input_tokens": "input_tokens"}
        usage = Usage.from_native({"input_tokens": 5}, key_map)
        assert usage.output_tokens == 0

    def test_사전이_아니면_네_항목_모두_판정_불가로_표시한다(self) -> None:
        """sca-dyb.4 — 값이 없음과 0이었음을 구분한다. 못 읽은 것을 0으로 세면
        안 된다는 요구를 unavailable 로 만족한다."""
        expected = frozenset(
            {"input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens"}
        )
        assert Usage.from_native(None, {"input_tokens": "in"}).unavailable == expected
        assert Usage.from_native("문자열", {"input_tokens": "in"}).unavailable == expected

    def test_unavailable이_다르면_동등비교가_실패한다(self) -> None:
        """코덱스 리뷰 지적 2번 — 판정 불가는 값만큼 중요한 상태라 동등비교에서
        빠지면 시험이 그 차이를 못 본다. compare=False 를 되돌린 근거다."""
        a = Usage(input_tokens=1)
        b = Usage(input_tokens=1, unavailable=frozenset({"output_tokens"}))
        assert a != b

    def test_unavailable이_같으면_동등하다(self) -> None:
        a = Usage(input_tokens=1, unavailable=frozenset({"output_tokens"}))
        b = Usage(input_tokens=1, unavailable=frozenset({"output_tokens"}))
        assert a == b

    def test_equivalent_values는_unavailable을_무시하고_숫자만_비교한다(self) -> None:
        """unavailable 을 무시하고 값만 비교해야 하는 기존 시험이 쓸 helper."""
        a = Usage(input_tokens=1, unavailable=frozenset({"output_tokens"}))
        b = Usage(input_tokens=1)
        assert a != b
        assert equivalent_values(a, b)

    @pytest.mark.parametrize(
        "field_name", ["input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens"],
    )
    def test_equivalent_values는_숫자가_다르면_False다(self, field_name: str) -> None:
        """네 필드 중 하나만 빼먹고 비교하면 그 필드의 매핑 오류를 시험이 못 본다."""
        assert not equivalent_values(Usage(**{field_name: 1}), Usage())

    def test_고유_키_맵으로_공통_어휘로_번역한다(self) -> None:
        key_map = {
            "input_tokens": "in", "output_tokens": "out",
            "cache_read_tokens": "cached", "cache_creation_tokens": "written",
        }
        usage = Usage.from_native({"in": 3, "out": 4, "cached": 5, "written": 6}, key_map)
        assert usage == Usage(input_tokens=3, output_tokens=4, cache_creation_tokens=6, cache_read_tokens=5)
        assert usage.unavailable == frozenset()

    def test_키_맵에_없는_공통_필드는_판정_불가다(self) -> None:
        """에이전트가 아예 대응 지표를 안 내는 경우(예: agy 의 cache_creation)."""
        key_map = {"input_tokens": "in", "output_tokens": "out"}
        usage = Usage.from_native({"in": 3, "out": 4}, key_map)
        assert usage.cache_creation_tokens == 0
        assert usage.cache_read_tokens == 0
        assert usage.unavailable == frozenset({"cache_creation_tokens", "cache_read_tokens"})

    def test_키_맵에_있어도_실제_데이터에_없으면_판정_불가다(self) -> None:
        key_map = {"input_tokens": "in", "cache_read_tokens": "cached"}
        usage = Usage.from_native({"in": 3}, key_map)
        assert usage.unavailable == frozenset({"output_tokens", "cache_creation_tokens", "cache_read_tokens"})

    def test_from_native에_사전이_아닌_값을_주면_전부_판정_불가다(self) -> None:
        usage = Usage.from_native(None, {"input_tokens": "in"})
        assert equivalent_values(usage, Usage())
        assert usage.unavailable == frozenset(
            {"input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens"}
        )


class TestEngineRequestResponse:
    def test_요청은_불변이다(self) -> None:
        req = request()
        with pytest.raises(dataclasses.FrozenInstanceError):
            req.prompt = "다른 말"  # type: ignore[misc]

    def test_응답은_불변이고_기본값을_가진다(self) -> None:
        resp = EngineResponse(
            ok=True, body="답", session_id="s", model_actual="m",
            elapsed=1.0, turns=1, usage=None,
        )
        assert resp.raw == {}
        assert resp.failure_reason is None
        assert resp.elapsed_source == ElapsedSource.UNKNOWN


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
        # usage 사전에 cache 키가 없어 그 두 필드는 판정 불가다(claude 도
        # 2026-09-16 이후 from_native 를 쓴다 — 코덱스 리뷰 지적 3번).
        assert resp.usage == Usage(
            input_tokens=1, output_tokens=2,
            unavailable=frozenset({"cache_creation_tokens", "cache_read_tokens"}),
        )
        assert resp.failure_reason is None

    def test_결과가_비면_실패로_본다(self, tmp_path: Path) -> None:
        profile = claude_profile(tmp_path)
        engine = ClaudeEngine(profile, SETTINGS)
        resp = engine.parse(json.dumps({"result": "  ", "is_error": False}), "", 0)
        assert resp.ok is False
        assert resp.failure_reason == "empty_response"

    def test_usage_사전에_캐시_키가_없으면_판정_불가다(self, tmp_path: Path) -> None:
        """코덱스 리뷰 지적 3번 — claude 도 codex/gemini 와 같은 from_native
        경로를 쓴다. 이전에는 from_mapping 이 누락 키를 0으로 읽어, 같은
        모양의 부분 usage 사전을 codex/gemini 와 다르게 판정했다."""
        profile = claude_profile(tmp_path)
        engine = ClaudeEngine(profile, SETTINGS)
        payload = {
            "result": "답", "session_id": "s1", "is_error": False,
            "usage": {"input_tokens": 1, "output_tokens": 2},
        }
        resp = engine.parse(json.dumps(payload), "", 0)
        assert resp.usage is not None
        assert resp.usage.unavailable == frozenset({"cache_creation_tokens", "cache_read_tokens"})

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

    def test_duration_ms를_초로_바꿔_elapsed에_담는다(self, tmp_path: Path) -> None:
        """sca-cfa — 2026-09-16 실측: claude -p --output-format json 의 result
        이벤트가 duration_ms 를 낸다. 엔진이 직접 채우는 값이라 elapsed_source
        는 'engine' 이다."""
        profile = claude_profile(tmp_path)
        engine = ClaudeEngine(profile, SETTINGS)
        payload = {"result": "답", "is_error": False, "duration_ms": 4369}
        resp = engine.parse(json.dumps(payload), "", 0)
        assert resp.elapsed == pytest.approx(4.369)
        assert resp.elapsed_source == "engine"

    def test_duration_ms가_없으면_elapsed는_판정_불가로_남는다(self, tmp_path: Path) -> None:
        profile = claude_profile(tmp_path)
        engine = ClaudeEngine(profile, SETTINGS)
        payload = {"result": "답", "is_error": False}
        resp = engine.parse(json.dumps(payload), "", 0)
        assert resp.elapsed == 0.0
        assert resp.elapsed_source == "unknown"


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

    def test_기본_sandbox는_제한하지_않는다(self, tmp_path: Path) -> None:
        """봇이 읽기와 쓰기를 다 할 수 있어야 한다(2026-09-15 사용자 지시).
        전에는 기본이 read-only 라 프로필에 옵션을 안 적으면 조용히 막혔다."""
        profile = codex_profile(tmp_path)
        engine = CodexEngine(profile, SETTINGS)
        cmd = engine.build_command(request(resume=False, model="gpt-5.6-sol"))
        idx = cmd.index("--sandbox")
        assert cmd[idx + 1] == "danger-full-access"

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
        assert resp.usage == Usage(
            input_tokens=5, output_tokens=6,
            unavailable=frozenset({"cache_creation_tokens", "cache_read_tokens"}),
        )

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

    def test_codex_고유_캐시_키를_공통_어휘로_옮긴다(self, tmp_path: Path) -> None:
        """sca-dyb.4 — 2026-09-16 실측: codex exec --json 의 turn.completed 는
        cache_read_input_tokens 가 아니라 cached_input_tokens, cache_write_input_tokens
        를 낸다. 기존 코드는 Usage.from_mapping 이 claude 키 이름만 찾아 늘 0이었다."""
        profile = codex_profile(tmp_path)
        engine = CodexEngine(profile, SETTINGS)
        lines = [
            json.dumps({"type": "thread.started", "thread_id": "th-1"}),
            json.dumps({"type": "item.completed",
                       "item": {"type": "agent_message", "text": "답"}}),
            json.dumps({"type": "turn.completed", "usage": {
                "input_tokens": 58274, "cached_input_tokens": 34304,
                "cache_write_input_tokens": 100, "output_tokens": 232,
                "reasoning_output_tokens": 76,
            }}),
        ]
        resp = engine.parse("\n".join(lines), "", 0)
        assert resp.usage == Usage(
            input_tokens=58274, output_tokens=232, cache_creation_tokens=100, cache_read_tokens=34304,
        )
        assert resp.usage.unavailable == frozenset()

    def test_사용량_사전에_캐시_키가_없으면_판정_불가다(self, tmp_path: Path) -> None:
        profile = codex_profile(tmp_path)
        engine = CodexEngine(profile, SETTINGS)
        lines = [
            json.dumps({"type": "thread.started", "thread_id": "th-1"}),
            json.dumps({"type": "item.completed",
                       "item": {"type": "agent_message", "text": "답"}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 6}}),
        ]
        resp = engine.parse("\n".join(lines), "", 0)
        assert resp.usage.unavailable == frozenset({"cache_creation_tokens", "cache_read_tokens"})

    def test_codex는_출력에_duration이_없어_elapsed가_늘_판정_불가다(self, tmp_path: Path) -> None:
        """sca-cfa — 2026-09-16 실측: codex exec --json 은 어떤 이벤트에도
        경과시간 필드를 안 낸다. 실행기의 벽시계 대체값이 채워야 한다."""
        profile = codex_profile(tmp_path)
        engine = CodexEngine(profile, SETTINGS)
        lines = [
            json.dumps({"type": "thread.started", "thread_id": "th-1"}),
            json.dumps({"type": "item.completed",
                       "item": {"type": "agent_message", "text": "답"}}),
        ]
        resp = engine.parse("\n".join(lines), "", 0)
        assert resp.elapsed == 0.0
        assert resp.elapsed_source == "unknown"


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
# GeminiEngine — 계약 시험(tests/engine/test_engine_contract.py, test_gemini_engine.py)이
# 이미 다루는 것은 다시 안 만든다. 여기서는 sca-cfa/sca-dyb.4 로 새로 생긴
# elapsed_source·unavailable 판정만 본다.


class TestGeminiEngineUsageAndElapsed:
    def test_agy_캐시_읽기_키를_옮기고_생성_키는_판정_불가다(self, tmp_path: Path) -> None:
        profile = gemini_profile(tmp_path)
        engine = GeminiEngine(profile, SETTINGS)
        payload = {
            "conversation_id": "c1", "status": "SUCCESS", "response": "답",
            "duration_seconds": 2.5, "num_turns": 1,
            "usage": {"input_tokens": 7, "output_tokens": 9, "thinking_tokens": 11,
                     "cache_read_tokens": 3, "total_tokens": 30},
        }
        resp = engine.parse(json.dumps(payload), "", 0)
        assert resp.usage == Usage(
            input_tokens=7, output_tokens=9, cache_read_tokens=3,
            unavailable=frozenset({"cache_creation_tokens"}),
        )

    def test_duration_seconds를_그대로_elapsed로_쓰고_엔진_출처로_표시한다(self, tmp_path: Path) -> None:
        """sca-cfa — 2026-09-16 실측: agy -p --output-format json 은 이미
        duration_seconds 를 낸다(항상 채워져 있던 유일한 엔진)."""
        profile = gemini_profile(tmp_path)
        engine = GeminiEngine(profile, SETTINGS)
        payload = {
            "conversation_id": "c1", "status": "SUCCESS", "response": "답",
            "duration_seconds": 49.124253, "num_turns": 1, "usage": {},
        }
        resp = engine.parse(json.dumps(payload), "", 0)
        assert resp.elapsed == pytest.approx(49.124253)
        assert resp.elapsed_source == "engine"

    def test_duration_seconds_키_자체가_없으면_판정_불가다(self, tmp_path: Path) -> None:
        profile = gemini_profile(tmp_path)
        engine = GeminiEngine(profile, SETTINGS)
        payload = {"conversation_id": "c1", "status": "SUCCESS", "response": "답", "usage": {}}
        resp = engine.parse(json.dumps(payload), "", 0)
        assert resp.elapsed == 0.0
        assert resp.elapsed_source == "unknown"


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
        assert switcher.should_probe(switcher.load()["last_probe_at"] + 10) is False

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


class 통과정책:
    """격리 자체를 보지 않는 시험용 — 받은 환경을 그대로 돌려준다."""

    def build(self, source_env):
        return dict(source_env)


class TestEngineRunner:
    def test_엔진이_만든_명령을_실행하고_결과를_파싱한다(self, tmp_path: Path) -> None:
        profile = claude_profile(tmp_path)
        engine = RecordingEngine(profile, SETTINGS)
        calls: list[Any] = []

        def fake_subprocess(cmd, cwd, timeout, env=None):
            calls.append((cmd, cwd, timeout))
            return FakeCompleted(stdout="답변", returncode=0)

        runner = EngineRunner(SETTINGS, subprocess_runner=fake_subprocess, environment_policy=통과정책())
        resp = runner.run(engine, request())
        assert resp.ok is True
        assert resp.body == "답변"
        assert calls[0][0] == ["fake-bin", "안녕"]
        assert engine.built == [request()]

    def test_시간초과면_timeout_실패를_돌려준다(self, tmp_path: Path) -> None:
        profile = claude_profile(tmp_path)
        engine = RecordingEngine(profile, SETTINGS)

        def fake_subprocess(cmd, cwd, timeout, env=None):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

        runner = EngineRunner(SETTINGS, subprocess_runner=fake_subprocess, environment_policy=통과정책())
        resp = runner.run(engine, request())
        assert resp.ok is False
        assert resp.failure_reason == "timeout"
        assert resp.elapsed_source == "runner"

    def test_엔진이_elapsed를_안_채우면_실행기가_벽시계로_채운다(self, tmp_path: Path) -> None:
        """sca-cfa — codex 처럼 elapsed_source 가 'unknown' 인 응답만 실행기가
        벽시계 측정값으로 채운다. RecordingEngine.parse() 는 elapsed_source 를
        안 주므로 기본값 'unknown' 이다."""
        profile = claude_profile(tmp_path)
        engine = RecordingEngine(profile, SETTINGS)

        def fake_subprocess(cmd, cwd, timeout, env=None):
            time.sleep(0.05)
            return FakeCompleted(stdout="답변", returncode=0)

        runner = EngineRunner(SETTINGS, subprocess_runner=fake_subprocess, environment_policy=통과정책())
        resp = runner.run(engine, request())
        assert resp.elapsed_source == "runner"
        assert resp.elapsed >= 0.05

    def test_엔진이_이미_elapsed를_채웠으면_실행기가_안_건드린다(self, tmp_path: Path) -> None:
        profile = claude_profile(tmp_path)
        engine_response = EngineResponse(
            ok=True, body="답", session_id=None, model_actual=None,
            elapsed=9.9, turns=None, usage=None, elapsed_source="engine",
        )
        engine = RecordingEngine(profile, SETTINGS, response=engine_response)

        def fake_subprocess(cmd, cwd, timeout, env=None):
            return FakeCompleted(stdout="{}", returncode=0)

        runner = EngineRunner(SETTINGS, subprocess_runner=fake_subprocess, environment_policy=통과정책())
        resp = runner.run(engine, request())
        assert resp.elapsed == 9.9
        assert resp.elapsed_source == "engine"


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

        def fake_subprocess(cmd, cwd, timeout, env=None):
            calls.append(cmd)
            return FakeCompleted(stdout="", returncode=0)

        runner = EngineRunner(SETTINGS, subprocess_runner=fake_subprocess, environment_policy=통과정책())
        return FallbackEngine(primary, secondary, switcher, runner), primary, secondary, switcher

    def test_평소에는_1차_엔진으로_돈다(self, tmp_path: Path) -> None:
        ok_response = EngineResponse(ok=True, body="1차 응답", session_id="s1",
                                     model_actual=None, elapsed=0, turns=None, usage=None)
        fallback, _primary, secondary, switcher = self._fallback(
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
        fallback, _primary, _secondary, switcher = self._fallback(
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
        fallback, _primary, secondary, switcher = self._fallback(
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
        fallback, _primary, _secondary, switcher = self._fallback(
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

    def test_정책을_못_만들면_준비_단계_전에_멈춘다(self, tmp_path: Path) -> None:
        """prepare() 는 설정 파일을 쓴다. 정책 확인이 그 뒤면 실행되지도 않을
        요청이 파일을 남긴다."""
        engine = RecordingEngine(claude_profile(tmp_path), SETTINGS)  # 프로필에 fake 블록이 없다
        runner = EngineRunner(RuntimeSettings(), subprocess_runner=lambda *a, **k: None)
        with pytest.raises(ConfigError):
            runner.run(engine, request())
        assert engine.built == []

    def test_정책을_안_주면_엔진_자신의_정책을_쓴다(self, tmp_path: Path) -> None:
        """fallback 은 primary 와 다른 엔진, 다른 home 으로 돈다. 실행기에 정책을
        하나 박아 두면 2차 엔진이 1차의 home 으로 돌아 기록 위치가 어긋난다."""
        seen: dict[str, Any] = {}

        def fake_run(cmd, cwd, timeout, env=None):
            seen["env"] = env
            return FakeCompleted(stdout="{}", returncode=0)

        profile = profile_with(
            {"type": "claude", "binary": "claude", "model": "m"}, tmp_path=tmp_path,
            fallback={"type": "codex", "binary": "codex", "model": "m2",
                      "home_dir": str(tmp_path / "codex-home")},
        )
        runner = EngineRunner(RuntimeSettings(), subprocess_runner=fake_run)

        runner.run(ClaudeEngine(profile, SETTINGS), request())
        assert "CODEX_HOME" not in seen["env"]

        runner.run(CodexEngine(profile, SETTINGS), request())
        assert seen["env"]["CODEX_HOME"] == str(tmp_path / "codex-home")


class Test엔진호출부품:
    """엔진 한 번 실행을 감싸는 부품.

    파이프라인은 폴백이 설정돼 있는지 몰라야 한다. `EngineRunner.run()` 을
    직접 부르면 `FallbackEngine` 을 감싸 둬도 그 `run()` 이 안 불려, 한도
    소진 때 대체 엔진 전환과 상태 기록이 일어나지 않는다.
    """

    def test_직접실행은_실행기를_거친다(self) -> None:
        from slack_cli_agent.engine.runner import DirectInvoker

        부른것: list[tuple[object, object]] = []

        class 실행기대역:
            def run(self, engine, request, timeout_sec=None):
                부른것.append((engine, request))
                return "응답"

        engine = object()
        request = object()
        assert DirectInvoker(실행기대역(), engine).invoke(request) == "응답"
        assert 부른것 == [(engine, request)]

    def test_폴백실행은_엔진자신의_run_을_부른다(self) -> None:
        """실행기를 거치면 전환 판정이 건너뛰어진다."""
        from slack_cli_agent.engine.runner import FallbackInvoker

        부른것: list[object] = []

        class 폴백대역:
            def run(self, request):
                부른것.append(request)
                return "전환된 응답"

        request = object()
        assert FallbackInvoker(폴백대역()).invoke(request) == "전환된 응답"
        assert 부른것 == [request]


class TestUsage직렬화:
    """audit 기록은 JSON 한 줄이다. Usage 가 JSON 에 못 담는 타입을 내면 그 요청
    기록 전체가 사라지고, 실제로 요청 자체가 실패했다(sca-kwv).
    """

    def test_audit용_dict는_json으로_바로_직렬화된다(self) -> None:
        usage = Usage.from_native({"input_tokens": 3}, {"input_tokens": "input_tokens"})
        assert json.loads(json.dumps(usage.as_audit_dict()))["input_tokens"] == 3

    def test_판정_불가_필드가_목록으로_남는다(self) -> None:
        usage = Usage.from_native({"input_tokens": 3}, {"input_tokens": "input_tokens"})
        기록 = usage.as_audit_dict()
        assert sorted(기록["unavailable"]) == ["cache_creation_tokens", "cache_read_tokens", "output_tokens"]

    def test_판정_불가가_없으면_빈_목록이다(self) -> None:
        usage = Usage.from_native(
            dict.fromkeys(("input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens"), 1),
            {n: n for n in ("input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens")},
        )
        assert usage.as_audit_dict()["unavailable"] == []
