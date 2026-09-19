"""엔진 계층 — Engine 인터페이스, Registry, Claude/Codex 어댑터, 전환, 실행."""

from __future__ import annotations

import dataclasses
import json
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from engine_support import named

from slack_cli_agent.auth.principal import TrustLevel
from slack_cli_agent.config.profile import Profile
from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.core.errors import ConfigError
from slack_cli_agent.engine.base import (
    UNTRUSTED_INPUT_MARK,
    CallOrigin,
    ElapsedSource,
    Engine,
    EngineRequest,
    EngineResponse,
    FailureDetail,
    Usage,
    UsageLimit,
    equivalent_values,
)
from slack_cli_agent.engine.capability import InstructionBoundary
from slack_cli_agent.engine.claude import ClaudeEngine
from slack_cli_agent.engine.codex import CodexEngine
from slack_cli_agent.engine.environment import EngineEnvironmentPolicy
from slack_cli_agent.engine.gemini import GeminiEngine
from slack_cli_agent.engine.registry import EngineRegistry
from slack_cli_agent.engine.runner import EngineRunner, FallbackEngine
from slack_cli_agent.engine.switcher import EngineSwitcher
from slack_cli_agent.engine.tool_selection import ToolSelection

SETTINGS = RuntimeSettings()


def profile_with(
    primary: dict,
    fallback: dict | None = None,
    tmp_path: Path | None = None,
    mcp_servers: dict[str, Any] | None = None,
) -> Profile:
    data: dict[str, Any] = {
        "name": "example",
        "primary_engine": primary,
        "owner_user_id": "U1",
        "troubleshoot_channel": "C1",
    }
    if mcp_servers:
        data["mcp_servers"] = mcp_servers
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
    base: dict[str, Any] = {
        "prompt": "안녕",
        "system_prompt": "시스템 지침",
        "session_id": "11111111-1111-1111-1111-111111111111",
        "resume": False,
        "model": "claude-sonnet-5",
        "effort": "medium",
        "workdir": Path("/tmp/work"),
        "readable_dirs": (Path("/tmp/a"), Path("/tmp/b")),
        "tools": ToolSelection.allow(["Read", "Grep"]),
        "trust_level": TrustLevel.GENERAL,
    }
    base.update(overrides)
    return EngineRequest(**base)


class FakeCompleted:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


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
        assert equivalent_values(
            Usage.from_native("문자열", key_map),  # type: ignore[arg-type]
            Usage(),
        )

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
        assert Usage.from_native(
            "문자열",  # type: ignore[arg-type]
            {"input_tokens": "in"},
        ).unavailable == expected

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
        값: dict[str, Any] = {field_name: 1}
        assert not equivalent_values(Usage(**값), Usage())

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


# Engine ABC 와 Registry


class TestEngineABC:
    def test_직접_인스턴스화할_수_없다(self) -> None:
        with pytest.raises(TypeError):
            Engine(profile=None, settings=SETTINGS)  # type: ignore[abstract, arg-type]

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
        cmd = engine.build_command(request(tools=ToolSelection.allow(["Read", "Grep", "Glob"])))
        idx = cmd.index("--allowedTools")
        assert cmd[idx + 1] == "Read,Grep,Glob"

    def test_모델과_effort를_전달한다(self, tmp_path: Path) -> None:
        profile = claude_profile(tmp_path)
        engine = ClaudeEngine(profile, SETTINGS)
        cmd = engine.build_command(request(model="claude-opus-5", effort="high"))
        assert cmd[cmd.index("--model") + 1] == "claude-opus-5"
        assert cmd[cmd.index("--effort") + 1] == "high"

    def test_진행_로그가_없으면_settings를_안_붙인다(self, tmp_path: Path) -> None:
        profile = claude_profile(tmp_path)
        engine = ClaudeEngine(profile, SETTINGS)
        assert "--settings" not in engine.build_command(request())

    def test_진행_로그가_있으면_도구_시작_훅을_등록한다(self, tmp_path: Path) -> None:
        import sys

        profile = claude_profile(tmp_path)
        engine = ClaudeEngine(profile, SETTINGS)
        log_path = tmp_path / "progress" / "C1-1.0.log"
        cmd = engine.build_command(request(progress_log=log_path))
        loaded = json.loads(cmd[cmd.index("--settings") + 1])
        hooks = loaded["hooks"]["PreToolUse"][0]["hooks"]
        assert len(hooks) == 1
        # 엔진 환경은 최소 허용 목록이라 PATH 의 python3 에 기댈 수 없다
        assert hooks[0]["command"].startswith(sys.executable)

    def test_공백이_든_경로도_한_인자로_전달된다(self, tmp_path: Path) -> None:
        import shlex

        profile = claude_profile(tmp_path)
        engine = ClaudeEngine(profile, SETTINGS)
        log_path = tmp_path / "빈 칸 있는 경로" / "C1-1.0.log"
        cmd = engine.build_command(request(progress_log=log_path))
        loaded = json.loads(cmd[cmd.index("--settings") + 1])
        command = loaded["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        assert shlex.split(command)[-1] == str(log_path)


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

    def test_is_error_한도_안내는_사람에게_보이는_문구다(self, tmp_path: Path) -> None:
        """한도 감지·폴백 전환은 이 경로에서 일어나는데 안내만 안 나가면
        사람은 실패 표식만 보고 이유를 모른다 (sca-5sc)."""
        profile = claude_profile(tmp_path)
        engine = ClaudeEngine(profile, SETTINGS)
        payload = {"is_error": True, "subtype": "error_max_limit", "result": ""}
        resp = engine.parse(json.dumps(payload), "", 0)
        assert resp.user_facing is True
        assert resp.body.strip()

    def test_일반_is_error는_안내를_게시하지_않는다(self, tmp_path: Path) -> None:
        """한도가 아닌 실패의 문구는 실패 표식에 더할 정보가 없다."""
        profile = claude_profile(tmp_path)
        engine = ClaudeEngine(profile, SETTINGS)
        payload = {"is_error": True, "subtype": "error_during_execution"}
        resp = engine.parse(json.dumps(payload), "", 0)
        assert resp.user_facing is False

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

    def test_재개_스레드는_resume과_sandbox_mode를_쓴다(self, tmp_path: Path) -> None:
        profile = codex_profile(tmp_path)
        engine = CodexEngine(profile, SETTINGS)
        cmd = engine.build_command(request(resume=True, model="gpt-5.6-sol",
                                           session_id="thread-abc"))
        assert cmd[1] == "exec"
        assert cmd[2] == "resume"
        # CLI 가 재개 세션의 지침을 안 바꾼다(2026-09-19 실측). 그 자리에 실어도
        # 첫 턴 지침이 그대로 쓰이므로 안 싣는다.
        assert not any(tok.startswith("developer_instructions=") for tok in cmd)
        assert any("sandbox_mode=" in tok for tok in cmd)
        assert "thread-abc" in cmd

    def test_재개_스레드는_갱신된_지침을_프롬프트에_싣는다(self, tmp_path: Path) -> None:
        """시스템 지침은 턴마다 달라진다(run_id, 수정된 프롬프트 파일, 이번
        요청에 맞춘 지식). 재개 턴이 옛 지침을 쓰면 claude·gemini 와 다른
        동작이 된다 (sca-ivs)."""
        profile = codex_profile(tmp_path)
        engine = CodexEngine(profile, SETTINGS)
        cmd = engine.build_command(request(resume=True, model="gpt-5.6-sol",
                                           session_id="thread-abc",
                                           system_prompt="이번턴만의지침"))
        assert "이번턴만의지침" in cmd[-1]
        assert cmd[-1].endswith("안녕")
        # 표식은 지침 뒤, 슬랙 입력 앞이어야 뜻이 있다.
        지침끝 = cmd[-1].index("이번턴만의지침")
        표식 = cmd[-1].index(UNTRUSTED_INPUT_MARK)
        assert 지침끝 < 표식 < cmd[-1].index("안녕")

    def test_지침이_비어도_슬랙_입력에_표식을_붙인다(self, tmp_path: Path) -> None:
        """리뷰 경로는 system_prompt 를 빈 값으로 고정한 채 재개한다. 그때
        표식을 빼면 그 경로에서만 비신뢰 입력 표시가 사라져 gemini 와
        동작이 갈린다 (sca-ivs 리뷰 [중간])."""
        profile = codex_profile(tmp_path)
        engine = CodexEngine(profile, SETTINGS)
        cmd = engine.build_command(request(resume=True, model="gpt-5.6-sol",
                                           session_id="thread-abc",
                                           system_prompt=""))
        assert UNTRUSTED_INPUT_MARK in cmd[-1]
        assert cmd[-1].endswith("안녕")

    def test_재개_턴의_지침은_슬랙_입력과_같은_계층이다(self, tmp_path: Path) -> None:
        """프롬프트 한 문자열 안에 든 지침은 native 경계가 아니다. 선언을
        그대로 두면 요구 대조가 거짓이 된다."""
        profile = codex_profile(tmp_path)
        engine = CodexEngine(profile, SETTINGS)
        신규 = engine.capabilities_for(request(resume=False, model="gpt-5.6-sol"))
        재개 = engine.capabilities_for(request(resume=True, model="gpt-5.6-sol",
                                              session_id="thread-abc"))
        assert 신규.instruction_boundary is InstructionBoundary.NATIVE
        assert 재개.instruction_boundary is InstructionBoundary.PROMPT_ONLY

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
        assert resp.usage is not None
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


class Test실패해도_CLI_가_발행한_값을_보존한다:
    """codex 는 세션 ID 를 CLI 가 발행한다. 실패 응답에서 그것을 버리면 느린
    요청 보고가 rollout transcript 를 못 찾는다 - 그 파일 이름이 thread ID 다
    (core/pipeline.py:197). gemini 는 같은 조건에서 보존한다 (sca-biud)."""

    def _실패줄들(self) -> str:
        return "\n".join([
            json.dumps({"type": "thread.started", "thread_id": "th-1"}),
            json.dumps({"type": "turn.completed",
                        "usage": {"input_tokens": 5, "output_tokens": 6}}),
        ])

    def test_종료코드가_0이_아니어도_세션을_보존한다(self, tmp_path: Path) -> None:
        engine = CodexEngine(codex_profile(tmp_path), SETTINGS)
        resp = engine.parse(self._실패줄들(), "터짐", 1)
        assert resp.ok is False
        assert resp.failure_reason == "nonzero_exit"
        assert resp.session_id == "th-1"

    def test_종료코드가_0이_아니어도_사용량을_보존한다(self, tmp_path: Path) -> None:
        """실패한 턴도 토큰을 쓴다. 안 세면 집계가 실제보다 작아진다."""
        engine = CodexEngine(codex_profile(tmp_path), SETTINGS)
        resp = engine.parse(self._실패줄들(), "터짐", 1)
        assert resp.usage is not None and resp.usage.input_tokens == 5

    def test_뒷줄이_깨져도_앞줄에서_읽은_것은_남긴다(self, tmp_path: Path) -> None:
        """CLI 가 중간에 죽으면 마지막 줄이 잘린다. 그때가 transcript 가 가장
        필요한 때다."""
        engine = CodexEngine(codex_profile(tmp_path), SETTINGS)
        stdout = self._실패줄들() + '\n{"type": "item.compl'
        resp = engine.parse(stdout, "", 1)
        assert resp.session_id == "th-1"

    def test_읽을_것이_없으면_그대로_비운다(self, tmp_path: Path) -> None:
        """없는 값을 지어내지 않는다."""
        engine = CodexEngine(codex_profile(tmp_path), SETTINGS)
        resp = engine.parse("", "터짐", 1)
        assert resp.session_id is None
        assert resp.usage is None

    def test_이벤트_속_형식이_어긋나도_안_터진다(self, tmp_path: Path) -> None:
        """item 이 객체가 아니면 AttributeError 로 죽었다. 잡는 예외 목록에
        없어 실패 응답조차 못 냈다 (코덱스 리뷰)."""
        engine = CodexEngine(codex_profile(tmp_path), SETTINGS)
        stdout = "\n".join([
            json.dumps({"type": "thread.started", "thread_id": "th-1"}),
            json.dumps({"type": "item.completed", "item": "문자열"}),
        ])
        resp = engine.parse(stdout, "", 0)
        assert resp.failure_reason == "bad_json"
        assert resp.session_id == "th-1"

    def test_세션_ID_가_문자열이_아니면_안_쓴다(self, tmp_path: Path) -> None:
        """숫자를 그대로 넘기면 뒤에서 타입 오류가 난다."""
        engine = CodexEngine(codex_profile(tmp_path), SETTINGS)
        resp = engine.parse(json.dumps({"type": "thread.started", "thread_id": 7}), "", 0)
        assert resp.session_id is None
        assert resp.failure_reason == "bad_json"

    def test_형식을_못_읽어도_세션은_보존한다(self, tmp_path: Path) -> None:
        """bad_json 판정은 유지하되 읽은 것까지 버리지는 않는다."""
        engine = CodexEngine(codex_profile(tmp_path), SETTINGS)
        stdout = json.dumps({"type": "thread.started", "thread_id": "th-1"}) + "\n[1, 2]"
        resp = engine.parse(stdout, "", 0)
        assert resp.failure_reason == "bad_json"
        assert resp.session_id == "th-1"


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


# GeminiEngine — 계약 시험(tests/engine/test_engine_contract.py, test_gemini_engine.py)이
# 이미 다루는 것은 다시 안 만든다. 여기서는 sca-cfa/sca-dyb.4 로 새로 생긴
# elapsed_source·unavailable 판정만 본다.


class TestGemini종료코드:
    """agy 만 종료코드를 실패 판정에 안 썼다(sca-e9a).

    2026-09-17 실측 — agy --output-format json 은 실패 시 exit 1 과
    status=ERROR 를 함께 낸다(없는 모델 지정). 성공은 exit 0 과 SUCCESS 다.
    원본 주석의 '권한 거부도 exit 0' 경로는 exit 0 이므로 status 판정이
    그대로 남는다. 종료코드는 조건을 더하는 것이지 status 를 대체하지 않는다.
    """

    def test_종료코드가_0이_아니면_SUCCESS_라도_실패다(self, tmp_path: Path) -> None:
        engine = GeminiEngine(gemini_profile(tmp_path), SETTINGS)
        payload = {
            "conversation_id": "c1", "status": "SUCCESS", "response": "답",
            "duration_seconds": 2.0, "num_turns": 1, "usage": {},
        }
        resp = engine.parse(json.dumps(payload), "", 1)
        assert resp.ok is False
        assert resp.failure_reason == "nonzero_exit"
        assert resp.failure_detail.exit_code == 1

    def test_실패로_판정해도_세션과_사용량은_보존한다(self, tmp_path: Path) -> None:
        """이어가기와 사용량 집계가 그 값을 쓴다. 버리면 다음 턴이 새 대화가 된다."""
        engine = GeminiEngine(gemini_profile(tmp_path), SETTINGS)
        payload = {
            "conversation_id": "c1", "status": "SUCCESS", "response": "답",
            "duration_seconds": 2.0, "num_turns": 3,
            "usage": {"input_tokens": 5, "output_tokens": 6},
        }
        resp = engine.parse(json.dumps(payload), "", 2)
        assert resp.session_id == "c1"
        assert resp.turns == 3
        assert resp.usage is not None and resp.usage.input_tokens == 5

    def test_status_가_ERROR_면_그_사유를_유지한다(self, tmp_path: Path) -> None:
        """더 구체적인 사유를 종료코드로 덮으면 원인 구분이 사라진다."""
        engine = GeminiEngine(gemini_profile(tmp_path), SETTINGS)
        payload = {"conversation_id": "", "status": "ERROR", "response": "", "error": "모델 없음"}
        resp = engine.parse(json.dumps(payload), "", 1)
        assert resp.failure_reason == "is_error"
        assert resp.body == "모델 없음"

    def test_종료코드가_0이면_지금과_같다(self, tmp_path: Path) -> None:
        engine = GeminiEngine(gemini_profile(tmp_path), SETTINGS)
        payload = {
            "conversation_id": "c1", "status": "SUCCESS", "response": "답",
            "duration_seconds": 2.0, "num_turns": 1, "usage": {},
        }
        assert engine.parse(json.dumps(payload), "", 0).ok is True


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


class 통과정책(EngineEnvironmentPolicy):
    """격리 자체를 보지 않는 시험용 — 받은 환경을 그대로 돌려준다."""

    def __init__(self) -> None:
        super().__init__("test")

    def _base_env(self, source_env: Mapping[str, str]) -> dict[str, str]:
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
            elapsed=9.9, turns=None, usage=None, elapsed_source=ElapsedSource.ENGINE,
        )
        engine = RecordingEngine(profile, SETTINGS, response=engine_response)

        def fake_subprocess(cmd, cwd, timeout, env=None):
            return FakeCompleted(stdout="{}", returncode=0)

        runner = EngineRunner(SETTINGS, subprocess_runner=fake_subprocess, environment_policy=통과정책())
        resp = runner.run(engine, request())
        assert resp.elapsed == 9.9
        assert resp.elapsed_source == "engine"


# FallbackEngine


class TestFallbackEngine:
    def _fallback(self, tmp_path: Path, primary_response=None, secondary_response=None):
        profile = codex_profile(tmp_path)
        primary = named(RecordingEngine, "claude")(profile, SETTINGS, response=primary_response)
        secondary = named(RecordingEngine, "codex")(profile, SETTINGS, response=secondary_response)
        switcher = EngineSwitcher(tmp_path / "engine_state.json")
        calls: list[Any] = []

        def fake_subprocess(cmd, cwd, timeout, env=None):
            calls.append(cmd)
            return FakeCompleted(stdout="", returncode=0)

        runner = EngineRunner(SETTINGS, subprocess_runner=fake_subprocess, environment_policy=통과정책())
        return FallbackEngine(primary, secondary, switcher, runner), primary, secondary, switcher

    def test_전환_뒤_2차로_도는_요청은_2차_형식의_세션_id를_받는다(self, tmp_path: Path) -> None:
        """세션 ID 형식은 그 요청을 실제로 받는 CLI 의 것이다(sca-56y 계열)."""
        fallback, primary, secondary, switcher = self._fallback(tmp_path)
        primary.new_session_id = lambda: "1차-형식"  # type: ignore[method-assign]
        secondary.new_session_id = lambda: "2차-형식"  # type: ignore[method-assign]
        switcher.begin_switch("weekly limit", engine_name="codex")
        switcher.approve()

        fallback.run(request(session_id=None))

        assert [r.session_id for r in secondary.built] == ["2차-형식"]

    def test_한도_알림_응답에도_요청_모델이_실린다(self, tmp_path: Path) -> None:
        """승인 전에는 실행 없이 알림만 돌려준다. 그 요청도 기록에 남으므로
        모델 칸이 비면 안 된다 (sca-cr2b)."""
        fallback, _primary, _secondary, switcher = self._fallback(tmp_path)
        switcher.begin_switch("weekly limit", engine_name="codex")

        resp = fallback.run(request(model="claude-opus-5"))

        assert resp.ok is False
        assert resp.model_asked == "claude-opus-5"

    def test_전환_뒤_2차로_도는_요청은_2차의_모델을_받는다(self, tmp_path: Path) -> None:
        """1차 모델명을 그대로 2차 CLI 에 넘기면 없는 모델이 된다(sca-dyb.10)."""
        fallback, _primary, secondary, switcher = self._fallback(tmp_path)
        switcher.begin_switch("weekly limit", engine_name="codex")
        switcher.approve()

        fallback.run(request(model="claude-sonnet-5"))

        assert [r.model for r in secondary.built] == ["gpt-5.6-sol"]

    def _소유자모델폴백(self, tmp_path: Path):
        profile = profile_with(
            {"type": "claude", "binary": "claude", "model": "claude-sonnet-5",
             "model_owner": "claude-opus-5"},
            fallback={"type": "codex", "binary": "codex", "model": "gpt-5.6-sol",
                      "model_owner": "gpt-5.6-pro"},
            tmp_path=tmp_path,
        )
        primary = named(RecordingEngine, "claude")(profile, SETTINGS)
        secondary = named(RecordingEngine, "codex")(profile, SETTINGS)
        switcher = EngineSwitcher(tmp_path / "engine_state.json")
        runner = EngineRunner(
            SETTINGS,
            subprocess_runner=lambda cmd, cwd, timeout, env=None: FakeCompleted(stdout="", returncode=0),
            environment_policy=통과정책(),
        )
        switcher.begin_switch("weekly limit", engine_name="codex")
        switcher.approve()
        return FallbackEngine(primary, secondary, switcher, runner), secondary

    def test_소유자_요청은_2차에서도_소유자_모델을_받는다(self, tmp_path: Path) -> None:
        """1차에서 등급을 올려 보낸 요청이 2차의 일반 모델로 떨어지면
        그 등급이 조용히 사라진다(sca-14h). 엔진마다 모델명이 달라 1차 이름을
        그대로 옮길 수는 없으므로 2차의 소유자 모델을 쓴다."""
        fallback, secondary = self._소유자모델폴백(tmp_path)
        fallback.run(request(model="claude-opus-5", trust_level=TrustLevel.OWNER))
        assert [r.model for r in secondary.built] == ["gpt-5.6-pro"]

    def test_일반_요청은_2차의_일반_모델을_받는다(self, tmp_path: Path) -> None:
        fallback, secondary = self._소유자모델폴백(tmp_path)
        fallback.run(request(model="claude-sonnet-5", trust_level=TrustLevel.GENERAL))
        assert [r.model for r in secondary.built] == ["gpt-5.6-sol"]

    def test_2차에_소유자_모델이_없으면_일반_모델을_쓴다(self, tmp_path: Path) -> None:
        fallback, _primary, secondary, switcher = self._fallback(tmp_path)
        switcher.begin_switch("weekly limit", engine_name="codex")
        switcher.approve()
        fallback.run(request(trust_level=TrustLevel.OWNER))
        assert [r.model for r in secondary.built] == ["gpt-5.6-sol"]

    def test_복구_프로브가_1차로_돌면_1차의_모델을_받는다(self, tmp_path: Path) -> None:
        """모델을 안 정한 요청이 2차 상태에서 1차 프로브로 흘러도 1차 것을 써야 한다."""
        ok = EngineResponse(ok=True, body="복구", session_id=None, model_actual=None,
                            elapsed=0, turns=None, usage=None)
        fallback, primary, secondary, switcher = self._fallback(tmp_path, primary_response=ok)
        switcher.begin_switch("weekly limit", engine_name="codex")
        switcher.approve()
        fallback._active = secondary
        switcher.mark_probed(0.0)

        fallback.run(request(model=None))

        assert [r.model for r in primary.built] == ["claude-sonnet-5"]

    def test_복구_프로브가_1차로_돌면_1차_형식의_세션_id를_받는다(self, tmp_path: Path) -> None:
        """라우팅은 run() 안에서 정해진다. 요청 전에 id 를 먼저 꺼내면 2차
        상태에서 꺼낸 값이 1차 프로브로 흘러 그 CLI 가 거부한다.
        """
        ok = EngineResponse(ok=True, body="복구", session_id=None, model_actual=None,
                            elapsed=0, turns=None, usage=None)
        fallback, primary, secondary, switcher = self._fallback(tmp_path, primary_response=ok)
        primary.new_session_id = lambda: "1차-형식"  # type: ignore[method-assign]
        secondary.new_session_id = lambda: "2차-형식"  # type: ignore[method-assign]
        switcher.begin_switch("weekly limit", engine_name="codex")
        switcher.approve()
        fallback._active = secondary
        switcher.mark_probed(0.0)

        fallback.run(request(session_id=None))

        assert [r.session_id for r in primary.built] == ["1차-형식"]
        assert secondary.built == []

    def test_배치_요청은_복구_프로브를_소비하지_않는다(self, tmp_path: Path) -> None:
        """복구 프로브는 1차가 살아났는지를 실제 요청으로 확인하는 것이라
        그 요청 하나가 프로브 한 번을 쓴다. 사람이 안 기다리는 배치가 그것을
        먼저 쓰면, 실패했을 때 last_probe_at 이 밀려 직후의 사람 요청이
        프로브 주기 내내 복구 혜택을 못 받는다.
        """
        fallback, primary, secondary, switcher = self._fallback(tmp_path)
        switcher.begin_switch("weekly limit", engine_name="codex")
        switcher.approve()
        switcher.mark_probed(0.0)

        fallback.run(request(session_id=None), origin=CallOrigin.BACKGROUND)

        assert primary.built == []
        assert len(secondary.built) == 1
        assert switcher.should_probe(time.time()) is True

    def test_사람이_기다리는_요청은_복구_프로브를_쓴다(self, tmp_path: Path) -> None:
        ok = EngineResponse(ok=True, body="복구", session_id=None, model_actual=None,
                            elapsed=0, turns=None, usage=None)
        fallback, primary, _secondary, switcher = self._fallback(tmp_path, primary_response=ok)
        switcher.begin_switch("weekly limit", engine_name="codex")
        switcher.approve()
        switcher.mark_probed(0.0)

        response = fallback.run(request(session_id=None))

        assert response.body == "복구"
        assert len(primary.built) == 1
        assert switcher.is_switched() is False

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
            # 실제 claude 의 한도 응답은 사람에게 그대로 나간다(claude.py 의
            # user_facing=is_limit). 비공개로 두면 이 시험이 답 없이 끝나는
            # 경우를 정상으로 굳힌다 (sca-3hh4).
            user_facing=True,
        )
        probe_response = EngineResponse(ok=True, body="OK", session_id=None,
                                        model_actual=None, elapsed=0, turns=None, usage=None)
        fallback, _primary, _secondary, switcher = self._fallback(
            tmp_path, primary_response=limit_response, secondary_response=probe_response)
        resp = fallback.run(request())
        assert resp.body == "한도 소진"
        assert switcher.is_switched() is True
        assert switcher.is_approved() is False

    def test_전환을_시작한_첫_요청도_사람에게_답을_낸다(self, tmp_path: Path) -> None:
        """1차의 실패 응답은 기본이 비공개다. 그대로 돌려주면 전환을 만든 그
        요청만 아무 답도 없이 끝나 봇이 멈춘 것으로 보인다 (sca-3hh4).
        두 번째 요청부터는 승인 대기 분기가 같은 문구를 낸다."""
        fallback, _primary, _secondary, _switcher = self._인증실패_1차(tmp_path)

        resp = fallback.run(request())

        assert resp.user_facing is True
        assert "로그인" in resp.body
        assert resp.failure_reason == EngineSwitcher.AUTH_FAILURE

    def test_1차가_이미_사람에게_낼_답을_냈으면_그대로_쓴다(self, tmp_path: Path) -> None:
        """한도 안내는 엔진이 직접 낸다. 덮어쓰면 남은 한도 시각 같은 내용이
        사라진다."""
        limit_response = EngineResponse(
            ok=False, body="주간 한도를 다 썼습니다. 목요일에 풀립니다.", session_id=None,
            model_actual=None, elapsed=0, turns=None, usage=None,
            failure_reason="usage_limit", user_facing=True,
        )
        probe = EngineResponse(ok=True, body="OK", session_id=None, model_actual=None,
                               elapsed=0, turns=None, usage=None)
        fallback, _primary, _secondary, _switcher = self._fallback(
            tmp_path, primary_response=limit_response, secondary_response=probe)

        resp = fallback.run(request())

        assert resp.body == "주간 한도를 다 썼습니다. 목요일에 풀립니다."

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

    # -- 로그인 만료로 전환하는 경로 (2026-09-19 asuka 실측)

    def _인증실패_1차(self, tmp_path: Path):
        """1차가 종료코드 1 로 끝나고 그 실패가 로그인 만료인 경우."""
        failed = EngineResponse(
            ok=False, body="Codex 실행에 실패했습니다.", session_id=None, model_actual=None,
            elapsed=0, turns=None, usage=None, failure_reason="nonzero_exit",
        )
        probe_ok = EngineResponse(ok=True, body="OK", session_id=None, model_actual=None,
                                  elapsed=0, turns=None, usage=None)
        fallback, primary, secondary, switcher = self._fallback(
            tmp_path, primary_response=failed, secondary_response=probe_ok)
        primary.detect_auth_failure = (  # type: ignore[method-assign]
            lambda response: "codex 로그인이 풀렸다."
        )
        return fallback, primary, secondary, switcher

    def test_실제_codex_실패_출력으로도_전환된다(self, tmp_path: Path) -> None:
        """대역이 detect_auth_failure 를 통째로 바꿔 끼면, 실제 문구 판정과
        전환이 이어지는지는 아무 시험도 안 본다 (리뷰 지적 2026-09-19).
        여기서는 codex 가 실제로 내는 stderr 를 parse 에 먹여 그 결과로
        전환까지 가는지 본다."""
        codex = CodexEngine(codex_profile(tmp_path), SETTINGS)
        실패 = codex.parse(
            stdout="",
            stderr="ERROR codex_login::auth::manager: Failed to refresh token: "
                   '401 Unauthorized: {"code": "refresh_token_invalidated"}',
            returncode=1,
        )
        probe_ok = EngineResponse(ok=True, body="OK", session_id=None, model_actual=None,
                                  elapsed=0, turns=None, usage=None)
        fallback, primary, _secondary, switcher = self._fallback(
            tmp_path, primary_response=실패, secondary_response=probe_ok)
        primary.detect_auth_failure = codex.detect_auth_failure  # type: ignore[method-assign]

        resp = fallback.run(request())

        assert switcher.reason() == EngineSwitcher.AUTH_FAILURE
        assert resp.user_facing is True

    def test_1차_로그인이_풀리면_전환_상태를_남긴다(self, tmp_path: Path) -> None:
        """한도가 아니어도 1차가 당분간 아무 요청도 못 받는 상태면 전환한다.
        전환 계기가 없어서 codex 로그인 만료 때 2차로 안 넘어갔다.
        """
        fallback, _primary, _secondary, switcher = self._인증실패_1차(tmp_path)

        fallback.run(request())

        assert switcher.is_switched() is True
        assert switcher.reason() == EngineSwitcher.AUTH_FAILURE

    def test_로그인이_풀린_전환은_한도와_다른_안내를_낸다(self, tmp_path: Path) -> None:
        """한도로 안내하면 기다리면 되는 것으로 읽혀 아무도 다시 로그인하지 않는다."""
        fallback, _primary, _secondary, _switcher = self._인증실패_1차(tmp_path)
        fallback.run(request())

        resp = fallback.run(request())

        assert resp.failure_reason == EngineSwitcher.AUTH_FAILURE
        assert "로그인" in resp.body
        assert "한도" not in resp.body

    def test_로그인_만료가_아닌_실패는_전환_계기가_아니다(self, tmp_path: Path) -> None:
        """종료코드 1 전체를 계기로 삼으면 한 번 끊긴 것으로 엔진이 바뀐다."""
        failed = EngineResponse(
            ok=False, body="Codex 실행에 실패했습니다.", session_id=None, model_actual=None,
            elapsed=0, turns=None, usage=None, failure_reason="nonzero_exit",
        )
        fallback, _primary, _secondary, switcher = self._fallback(
            tmp_path, primary_response=failed)

        fallback.run(request())

        assert switcher.is_switched() is False

    def test_로그인_만료는_복구_확인_주기가_한도보다_길다(self, tmp_path: Path) -> None:
        """확인 자체가 실제 요청이라 주기마다 사람 하나가 1차 실패를 기다린다.
        로그인 만료는 시간이 지나도 안 풀리므로 그 대기를 자주 만들지 않는다.
        """
        switcher = EngineSwitcher(tmp_path / "engine_state.json")
        switcher.begin_switch("로그인 만료", engine_name="codex",
                              reason=EngineSwitcher.AUTH_FAILURE)
        시작 = switcher.load()["switched_at"]

        assert switcher.should_probe(시작 + 700) is False
        assert switcher.should_probe(시작 + 3700) is True


class Test코덱스_로그인_만료_검출:
    """codex CLI 는 로그인 만료도 다른 실패도 종료코드 1 로 낸다."""

    def _응답(self, stderr: str) -> tuple[CodexEngine, EngineResponse]:
        engine = CodexEngine(codex_profile(Path("/tmp")), SETTINGS)
        return engine, engine.parse(stdout="", stderr=stderr, returncode=1)

    def test_토큰_폐기_문구가_있으면_인증_실패로_본다(self) -> None:
        engine, resp = self._응답(
            "ERROR codex_login::auth::manager: Failed to refresh token: 401 Unauthorized: "
            '{"code": "refresh_token_invalidated"}'
        )
        assert engine.detect_auth_failure(resp) is not None

    def test_401만_있으면_인증_실패로_보지_않는다(self) -> None:
        """MCP 서버 하나가 401 을 내도 codex 자체의 로그인은 멀쩡하다."""
        engine, resp = self._응답(
            "ERROR rmcp::transport::worker: worker quit with fatal: HTTP 401"
        )
        assert engine.detect_auth_failure(resp) is None

    def test_정상_응답은_인증_실패가_아니다(self) -> None:
        engine = CodexEngine(codex_profile(Path("/tmp")), SETTINGS)
        resp = EngineResponse(ok=True, body="답", session_id=None, model_actual=None,
                              elapsed=0, turns=None, usage=None)
        assert engine.detect_auth_failure(resp) is None


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

        class 고정정책(EngineEnvironmentPolicy):
            def __init__(self) -> None:
                super().__init__("testbot")

            def _base_env(self, source_env: Mapping[str, str]) -> dict[str, str]:
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

    def test_직접실행은_실행기를_거친다(self, tmp_path: Path) -> None:
        from slack_cli_agent.engine.runner import DirectInvoker

        부른것: list[tuple[Any, Any]] = []
        응답 = EngineResponse(ok=True, body="답", session_id=None, model_actual=None,
                             elapsed=0, turns=None, usage=None)

        class 실행기대역:
            def run(self, engine: Engine, request: EngineRequest,
                    timeout_sec: float | None = None) -> EngineResponse:
                부른것.append((engine, request))
                return 응답

        engine = RecordingEngine(claude_profile(tmp_path), SETTINGS)
        요청 = request()
        # 대역은 EngineRunner 를 상속하지 않는다. run 하나만 쓰는 위임 확인이다.
        invoker = DirectInvoker(실행기대역(), engine)  # type: ignore[arg-type]
        assert invoker.invoke(요청) is 응답
        assert 부른것 == [(engine, 요청)]

    def test_폴백실행은_엔진자신의_run_을_부른다(self) -> None:
        """실행기를 거치면 전환 판정이 건너뛰어진다."""
        from slack_cli_agent.engine.runner import FallbackInvoker

        부른것: list[Any] = []
        응답 = EngineResponse(ok=True, body="전환된 답", session_id=None, model_actual=None,
                             elapsed=0, turns=None, usage=None)

        class 폴백대역:
            def run(self, request: EngineRequest,
                    origin: CallOrigin = CallOrigin.INTERACTIVE) -> EngineResponse:
                부른것.append(request)
                return 응답

        요청 = request()
        # 대역은 FallbackEngine 을 상속하지 않는다. run 하나만 쓰는 위임 확인이다.
        invoker = FallbackInvoker(폴백대역())  # type: ignore[arg-type]
        assert invoker.invoke(요청) is 응답
        assert 부른것 == [요청]


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


class Test실행_시점에_세션_id를_만든다:
    """세션 ID 형식은 그것을 받는 CLI 의 것이다. 어느 엔진이 이번 요청을
    실행하는지는 실행 직전에야 정해지므로(폴백 전환·복구 프로브), 발급도
    그 자리에서 한다. 호출자가 미리 만들면 형식이 어긋나도 드러나지 않는다(sca-56y).
    """

    def _runner(self) -> EngineRunner:
        def fake_subprocess(cmd, cwd, timeout, env=None):
            return FakeCompleted(stdout="", returncode=0)

        return EngineRunner(SETTINGS, subprocess_runner=fake_subprocess,
                            environment_policy=통과정책())

    def test_비워_보내면_그_엔진이_만든_값이_채워진다(self, tmp_path: Path) -> None:
        engine = RecordingEngine(codex_profile(tmp_path), SETTINGS)
        self._runner().run(engine, request(session_id=None))
        assert [r.session_id for r in engine.built] == ["fake-session"]

    def test_이미_있는_값은_덮어쓰지_않는다(self, tmp_path: Path) -> None:
        """resume 은 기존 세션을 이어받는 것이라 그 id 를 바꾸면 대화가 끊긴다."""
        engine = RecordingEngine(codex_profile(tmp_path), SETTINGS)
        self._runner().run(engine, request(session_id="기존-세션", resume=True))
        assert [r.session_id for r in engine.built] == ["기존-세션"]

    @pytest.mark.parametrize("빈값", [None, ""])
    def test_아직_안_정해졌으면_명령_조립이_막힌다(self, 빈값: str | None) -> None:
        """엔진을 EngineRunner 없이 직접 부르면 형식을 아는 자리를 건너뛴 것이다.
        빈 문자열도 같다 — CLI 에는 빈 인자로 도착해 추적이 더 어렵다.
        """
        with pytest.raises(ValueError):
            request(session_id=빈값).require_session_id()


class Test모델도_실행_엔진이_정한다:
    """모델명 형식은 엔진마다 다르다. 어느 엔진이 이번 요청을 실행하는지는
    실행 직전에야 정해지므로 model=None 의 해석도 그 자리에서 한다(sca-dyb.10).

    관찰 지점은 EngineRequest 가 아니라 실제로 조립된 명령줄이다. 요청 객체만
    보면 채워진 값이 그 엔진의 CLI 에 유효한지가 안 드러난다.
    """

    def _capture(self, engine: Engine, req: EngineRequest) -> list[str]:
        기록: list[list[str]] = []

        def fake_subprocess(cmd, cwd, timeout, env=None):
            기록.append(list(cmd))
            return FakeCompleted(stdout="", returncode=0)

        runner = EngineRunner(SETTINGS, subprocess_runner=fake_subprocess,
                              environment_policy=통과정책())
        runner.run(engine, req)
        return 기록[0]

    def _모델인자(self, cmd: list[str], 플래그: str) -> str:
        return cmd[cmd.index(플래그) + 1]

    def test_클로드는_자기_프로필_모델을_받는다(self, tmp_path: Path) -> None:
        engine = ClaudeEngine(claude_profile(tmp_path), SETTINGS)
        cmd = self._capture(engine, request(model=None, workdir=tmp_path))
        assert self._모델인자(cmd, "--model") == "claude-sonnet-5"

    def test_코덱스는_자기_프로필_모델을_받는다(self, tmp_path: Path) -> None:
        profile = profile_with(
            {"type": "codex", "binary": "codex", "model": "gpt-5.6-sol"}, tmp_path=tmp_path)
        engine = CodexEngine(profile, SETTINGS)
        cmd = self._capture(engine, request(model=None, workdir=tmp_path))
        assert self._모델인자(cmd, "-m") == "gpt-5.6-sol"

    def test_제미나이는_자기_프로필_모델을_받는다(self, tmp_path: Path) -> None:
        engine = GeminiEngine(gemini_profile(tmp_path), SETTINGS)
        cmd = self._capture(engine, request(model=None, workdir=tmp_path))
        assert self._모델인자(cmd, "--model") == "gemini-3.8-flash"

    def test_호출부가_고른_모델은_그대로_간다(self, tmp_path: Path) -> None:
        engine = ClaudeEngine(claude_profile(tmp_path), SETTINGS)
        cmd = self._capture(engine, request(model="claude-opus-5", workdir=tmp_path))
        assert self._모델인자(cmd, "--model") == "claude-opus-5"

    @pytest.mark.parametrize("빈값", [None, ""])
    def test_아직_안_정해졌으면_명령_조립이_막힌다(self, 빈값: str | None) -> None:
        with pytest.raises(ValueError):
            request(model=빈값).require_model()


class Test실패_사유의_진단값:
    """감시 확인 실패 로그가 '사유 nonzero_exit, 응답 11자' 만 남겼다. 실제
    원인인 CLI 출력은 raw 에만 있어 아무도 안 읽는다. 엔진이 만들어 낸 값만
    골라 로그에 낼 수 있게 승격한다(sca-dyb.14).

    값은 숫자이거나 미리 정한 목록 안의 낱말뿐이다. 문자 치환으로 걸러도
    ASCII 로 된 비밀값은 그대로 통과하므로 금지 목록이 아니라 허용 목록이어야
    한다(코덱스 리뷰).
    """

    def test_담은_항목만_이름과_함께_나온다(self) -> None:
        assert str(FailureDetail(exit_code=1, code="error_max_turns")) == "exit_code=1 code=error_max_turns"

    def test_안_담은_항목은_빠진다(self) -> None:
        assert str(FailureDetail(exit_code=2)) == "exit_code=2"

    def test_종료코드_0_도_담는다(self) -> None:
        """0 은 없는 값이 아니다. 정상 종료인데 실패한 것이 곧 진단이다."""
        assert str(FailureDetail(exit_code=0)) == "exit_code=0"

    def test_빈_진단값은_거짓이다(self) -> None:
        assert not FailureDetail()
        assert FailureDetail(exit_code=0)

    def test_빈_진단값은_빈_문자열이_된다(self) -> None:
        assert str(FailureDetail()) == ""

    def test_모르는_낱말은_길이만_남긴다(self) -> None:
        """CLI 가 enum 자리에 대화 내용을 넣어도 그 내용이 안 나간다."""
        assert str(FailureDetail(code="내부 대화 내용 유출")) == "code=unknown:11"

    def test_ASCII_비밀값도_길이만_남긴다(self) -> None:
        assert str(FailureDetail(code="xoxb-1234567890-abcdef")) == "code=unknown:22"

    def test_경로도_길이만_남긴다(self) -> None:
        assert str(FailureDetail(code="/Users/example/비밀/파일.md")) == "code=unknown:23"

    def test_아는_낱말은_그대로_남긴다(self) -> None:
        assert str(FailureDetail(code="SUCCESS")) == "code=SUCCESS"

    def test_클로드_비정상_종료는_종료코드를_담는다(self, tmp_path: Path) -> None:
        engine = ClaudeEngine(claude_profile(tmp_path), SETTINGS)
        assert str(engine.parse("이상한 출력", "에러", 3).failure_detail) == "exit_code=3"

    def test_클로드_is_error_는_subtype_을_담는다(self, tmp_path: Path) -> None:
        engine = ClaudeEngine(claude_profile(tmp_path), SETTINGS)
        payload = {"type": "result", "is_error": True, "subtype": "error_max_turns"}
        assert str(engine.parse(json.dumps(payload), "", 0).failure_detail) == "code=error_max_turns"

    def test_클로드_빈_응답도_진단값을_남긴다(self, tmp_path: Path) -> None:
        engine = ClaudeEngine(claude_profile(tmp_path), SETTINGS)
        payload = {"type": "result", "result": "   ", "session_id": "s1"}
        나온것 = engine.parse(json.dumps(payload), "", 0)
        assert 나온것.failure_reason == "empty_response"
        assert 나온것.failure_detail

    def test_코덱스_비정상_종료는_종료코드를_담는다(self, tmp_path: Path) -> None:
        engine = CodexEngine(codex_profile(tmp_path), SETTINGS)
        assert str(engine.parse("", "실패", 7).failure_detail) == "exit_code=7"

    def test_제미나이_실패_상태는_status_를_담는다(self, tmp_path: Path) -> None:
        engine = GeminiEngine(gemini_profile(tmp_path), SETTINGS)
        payload = {"status": "ERROR", "error": "사용자 대화가 섞일 수 있는 문구"}
        assert str(engine.parse(json.dumps(payload), "", 0).failure_detail) == "code=ERROR"

    def test_제미나이_빈_응답도_진단값을_남긴다(self, tmp_path: Path) -> None:
        engine = GeminiEngine(gemini_profile(tmp_path), SETTINGS)
        payload = {"status": "SUCCESS", "response": "  "}
        나온것 = engine.parse(json.dumps(payload), "", 0)
        assert 나온것.failure_reason == "empty_response"
        assert 나온것.failure_detail

    def test_수치_자리에_문자열을_못_넣는다(self) -> None:
        """타입 표기만으로는 실행 중에 아무도 안 막는다. 여기가 막히지 않으면
        FailureDetail 은 경계가 아니라 권고다(코덱스 2차 리뷰).
        """
        with pytest.raises(TypeError):
            FailureDetail(exit_code="대화 내용")  # type: ignore[arg-type]

    def test_수치_자리에_참거짓을_못_넣는다(self) -> None:
        with pytest.raises(TypeError):
            FailureDetail(stdout_chars=True)  # type: ignore[arg-type]

    def test_낱말_자리에_문자열이_아닌_것을_못_넣는다(self) -> None:
        with pytest.raises(TypeError):
            FailureDetail(code=123)  # type: ignore[arg-type]

    def test_응답의_진단값_자리에_문자열을_못_넣는다(self) -> None:
        with pytest.raises(TypeError):
            EngineResponse(
                ok=False, body="", session_id=None, model_actual=None, elapsed=0.0,
                turns=None, usage=None, failure_detail="xoxb-비밀",  # type: ignore[arg-type]
            )

    def test_성공한_응답에는_진단값이_없다(self, tmp_path: Path) -> None:
        engine = ClaudeEngine(claude_profile(tmp_path), SETTINGS)
        payload = {"type": "result", "result": "답", "session_id": "s1"}
        assert not engine.parse(json.dumps(payload), "", 0).failure_detail


# sca-8ks — codex·gemini 의 진행 단계


class Test엔진_stdout에서_도구_이름을_읽는다:
    """실측(2026-09-19) — codex 는 item.started/item.completed 의 item.type 에,
    agy 는 step_update.tool_name 에 도구 이름이 온다."""

    def codex(self, tmp_path: Path) -> CodexEngine:
        return CodexEngine(profile_with({"type": "codex", "binary": "codex", "model": "gpt-5"},
                                        tmp_path=tmp_path), SETTINGS)

    def gemini(self, tmp_path: Path) -> GeminiEngine:
        return GeminiEngine(gemini_profile(tmp_path), SETTINGS)

    def test_codex는_item_started의_item_type을_낸다(self, tmp_path: Path) -> None:
        line = json.dumps({"type": "item.started",
                           "item": {"id": "item_1", "type": "command_execution", "command": "ls"}})
        assert self.codex(tmp_path).progress_tool_name(line) == "command_execution"

    def test_codex는_item_completed도_읽는다(self, tmp_path: Path) -> None:
        line = json.dumps({"type": "item.completed", "item": {"type": "file_change"}})
        assert self.codex(tmp_path).progress_tool_name(line) == "file_change"

    def test_codex는_도구가_아닌_이벤트에_빈_값을_낸다(self, tmp_path: Path) -> None:
        engine = self.codex(tmp_path)
        assert engine.progress_tool_name(json.dumps({"type": "turn.started"})) == ""
        assert engine.progress_tool_name(json.dumps({"type": "thread.started", "thread_id": "x"})) == ""

    def test_gemini는_도구_단계의_tool_name을_낸다(self, tmp_path: Path) -> None:
        line = json.dumps({"event": "step_update",
                           "step_update": {"step_type": "tool", "tool_name": "run_command"}})
        assert self.gemini(tmp_path).progress_tool_name(line) == "run_command"

    def test_gemini는_모르는_단계에_빈_값을_낸다(self, tmp_path: Path) -> None:
        """agent_response 는 sca-92g 에서 표시 대상이 됐다. 그 밖의 단계는
        무엇을 하는 중인지 모르므로 이름을 안 낸다."""
        engine = self.gemini(tmp_path)
        line = json.dumps({"event": "step_update", "step_update": {"step_type": "plan"}})
        assert engine.progress_tool_name(line) == ""
        assert engine.progress_tool_name(json.dumps({"event": "init", "init": {"tools": ["a"]}})) == ""

    @pytest.mark.parametrize("line", ["", "   ", "{깨진", "[]", "null"])
    def test_형식이_깨진_줄은_빈_값이다(self, tmp_path: Path, line: str) -> None:
        assert self.codex(tmp_path).progress_tool_name(line) == ""
        assert self.gemini(tmp_path).progress_tool_name(line) == ""

    def test_클로드는_stdout_스트리밍_대상이_아니다(self, tmp_path: Path) -> None:
        """클로드는 훅 프로세스가 진행 로그를 쓴다. stdout 을 겹쳐 읽으면 같은
        호출이 두 번 기록된다."""
        assert ClaudeEngine(claude_profile(tmp_path), SETTINGS).streams_progress is False
        assert self.codex(tmp_path).streams_progress is True
        assert self.gemini(tmp_path).streams_progress is True


class Test실행기가_진행_로그를_스트리밍으로_쓴다:
    def test_도구_이름을_진행_로그에_한_줄씩_쓴다(self, tmp_path: Path) -> None:
        engine = CodexEngine(profile_with({"type": "codex", "binary": "codex", "model": "gpt-5"},
                                          tmp_path=tmp_path), SETTINGS)
        log = tmp_path / "progress.jsonl"
        events = [
            json.dumps({"type": "thread.started", "thread_id": "t1"}),
            json.dumps({"type": "item.started", "item": {"type": "command_execution"}}),
            json.dumps({"type": "item.started", "item": {"type": "web_search"}}),
            json.dumps({"type": "item.completed",
                        "item": {"type": "agent_message", "text": "답"}}),
        ]

        def fake_subprocess(cmd, cwd, timeout, env=None, on_stdout_line=None):
            assert on_stdout_line is not None
            for event in events:
                on_stdout_line(event + "\n")
            return FakeCompleted(stdout="\n".join(events), returncode=0)

        runner = EngineRunner(SETTINGS, subprocess_runner=fake_subprocess, environment_policy=통과정책())
        resp = runner.run(engine, request(progress_log=log, model="gpt-5"))
        assert resp.ok is True
        written = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        assert written == [{"tool": "command_execution"}, {"tool": "web_search"},
                           {"tool": "agent_message"}]

    def test_진행_로그가_없으면_스트리밍을_안_건다(self, tmp_path: Path) -> None:
        """진행 표시가 꺼진 채널이다. 쓸 자리가 없으니 stdout 을 겹쳐 읽지 않는다."""
        engine = CodexEngine(profile_with({"type": "codex", "binary": "codex", "model": "gpt-5"},
                                          tmp_path=tmp_path), SETTINGS)
        받은것: list[Any] = []

        def fake_subprocess(cmd, cwd, timeout, env=None, **kwargs):
            받은것.append(kwargs)
            return FakeCompleted(stdout=json.dumps(
                {"type": "item.completed", "item": {"type": "agent_message", "text": "답"}}),
                returncode=0)

        runner = EngineRunner(SETTINGS, subprocess_runner=fake_subprocess, environment_policy=통과정책())
        runner.run(engine, request(progress_log=None, model="gpt-5"))
        assert 받은것 == [{}]

    def test_스트리밍을_안_하는_엔진에는_안_건다(self, tmp_path: Path) -> None:
        engine = RecordingEngine(claude_profile(tmp_path), SETTINGS)
        받은것: list[Any] = []

        def fake_subprocess(cmd, cwd, timeout, env=None, **kwargs):
            받은것.append(kwargs)
            return FakeCompleted(stdout="답변", returncode=0)

        runner = EngineRunner(SETTINGS, subprocess_runner=fake_subprocess, environment_policy=통과정책())
        runner.run(engine, request(progress_log=tmp_path / "p.jsonl"))
        assert 받은것 == [{}]

    def test_진행_로그를_못_써도_실행은_계속한다(self, tmp_path: Path) -> None:
        """진행 표시는 부가 기능이다. 쓸 수 없으면 그 줄만 버리고 답은 낸다."""
        engine = CodexEngine(profile_with({"type": "codex", "binary": "codex", "model": "gpt-5"},
                                          tmp_path=tmp_path), SETTINGS)
        answer = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "답"}})

        def fake_subprocess(cmd, cwd, timeout, env=None, on_stdout_line=None):
            assert on_stdout_line is not None
            on_stdout_line(json.dumps({"type": "item.started", "item": {"type": "web_search"}}) + "\n")
            return FakeCompleted(stdout=answer, returncode=0)

        runner = EngineRunner(SETTINGS, subprocess_runner=fake_subprocess, environment_policy=통과정책())
        # 디렉터리가 없는 경로라 append 가 실패한다.
        resp = runner.run(engine, request(progress_log=tmp_path / "없는자리" / "p.jsonl", model="gpt-5"))
        assert resp.ok is True
        assert resp.body == "답"


class Test제미나이가_답을_쓰는_구간도_표시한다:
    """sca-92g — 도구를 안 부르고 답만 길게 쓰는 구간에 아무 표시가 없었다.

    코덱스는 agent_message 로 같은 구간을 이미 내고 있어 둘을 맞춘다.
    """

    def engine(self, tmp_path: Path) -> GeminiEngine:
        return GeminiEngine(gemini_profile(tmp_path), SETTINGS)

    def 줄(self, step: dict[str, Any]) -> str:
        return json.dumps({"event": "step_update", "step_update": step})

    def test_agent_response_단계를_낸다(self, tmp_path: Path) -> None:
        line = self.줄({"step_type": "agent_response", "text_delta": "답의 한 조각"})
        assert self.engine(tmp_path).progress_tool_name(line) == "agent_response"

    def test_도구_단계는_그대로_도구_이름이다(self, tmp_path: Path) -> None:
        """답 쓰는 구간을 더하면서 도구 이름 경로가 가려지면 표시가 뭉개진다."""
        line = self.줄({"step_type": "tool", "tool_name": "run_command"})
        assert self.engine(tmp_path).progress_tool_name(line) == "run_command"

    def test_모르는_단계는_안_낸다(self, tmp_path: Path) -> None:
        line = self.줄({"step_type": "plan", "text_delta": "..."})
        assert self.engine(tmp_path).progress_tool_name(line) == ""


class Test제미나이_스트리밍_출력:
    """sca-8ks — 진행 단계를 내려면 이벤트가 도는 동안 나와야 한다.

    --output-format json 은 끝에 한 덩어리로만 낸다. stream-json 은 같은
    payload 를 마지막 result 이벤트에 담아 낸다(2026-09-19 실측).
    """

    def engine(self, tmp_path: Path) -> GeminiEngine:
        return GeminiEngine(gemini_profile(tmp_path), SETTINGS)

    def 성공_payload(self) -> dict[str, Any]:
        return {
            "conversation_id": "c1", "status": "SUCCESS", "response": "답",
            "duration_seconds": 2.0, "num_turns": 1,
            "usage": {"input_tokens": 5, "output_tokens": 6},
        }

    def stream(self, payload: dict[str, Any]) -> str:
        lines = [
            json.dumps({"event": "init", "init": {"conversation_id": "c1", "tools": ["run_command"]}}),
            json.dumps({"event": "step_update",
                        "step_update": {"step_type": "tool", "tool_name": "run_command"}}),
            json.dumps({"event": "result", "result": payload}),
        ]
        return "\n".join(lines) + "\n"

    def test_명령이_stream_json을_쓴다(self, tmp_path: Path) -> None:
        cmd = self.engine(tmp_path).build_command(request(model="gemini-3.8-flash"))
        assert cmd[cmd.index("--output-format") + 1] == "stream-json"

    def test_result_이벤트에서_답을_읽는다(self, tmp_path: Path) -> None:
        resp = self.engine(tmp_path).parse(self.stream(self.성공_payload()), "", 0)
        assert resp.ok is True
        assert resp.body == "답"
        assert resp.session_id == "c1"
        assert resp.turns == 1
        assert resp.usage is not None and resp.usage.input_tokens == 5
        assert resp.elapsed == 2.0
        assert resp.elapsed_source == "engine"

    def test_result_이벤트의_실패도_그대로_읽는다(self, tmp_path: Path) -> None:
        payload = {"conversation_id": "", "status": "ERROR", "response": "", "error": "모델 없음"}
        resp = self.engine(tmp_path).parse(self.stream(payload), "", 1)
        assert resp.ok is False
        assert resp.failure_reason == "is_error"
        assert resp.body == "모델 없음"

    def test_한_덩어리_json도_계속_읽는다(self, tmp_path: Path) -> None:
        """폴백 - 판이 바뀌어 stream-json 을 못 쓰게 돼도 답은 나와야 한다."""
        resp = self.engine(tmp_path).parse(json.dumps(self.성공_payload()), "", 0)
        assert resp.ok is True
        assert resp.body == "답"

    def test_result_이벤트가_없으면_형식_실패다(self, tmp_path: Path) -> None:
        """중간에 끊긴 출력이다. 진행 이벤트만 있는 것을 성공으로 읽으면 빈 답이 나간다."""
        partial = json.dumps({"event": "step_update",
                              "step_update": {"step_type": "tool", "tool_name": "run_command"}}) + "\n"
        resp = self.engine(tmp_path).parse(partial, "", 0)
        assert resp.ok is False
        assert resp.failure_reason == "bad_json"

    def test_마지막_result_이벤트를_쓴다(self, tmp_path: Path) -> None:
        first = {"conversation_id": "c0", "status": "ERROR", "response": "", "error": "옛것"}
        text = self.stream(first) + json.dumps(
            {"event": "result", "result": self.성공_payload()}) + "\n"
        resp = self.engine(tmp_path).parse(text, "", 0)
        assert resp.ok is True
        assert resp.body == "답"


class Test실제로_돈_모델을_읽는다:
    """claude 의 result 이벤트에는 `model` 키가 없다(2026-09-19 실측, 2.1.263).
    모델별 사용량 표인 modelUsage 만 온다. 원본 bot.py:207 은 처음부터 그 표의
    출력 토큰 최대값을 골랐다 (sca-asrp)."""

    def _payload(self, **extra: Any) -> dict[str, Any]:
        return {"result": "답", "session_id": "s1", "is_error": False, **extra}

    def test_modelUsage_에서_고른다(self, tmp_path: Path) -> None:
        engine = ClaudeEngine(claude_profile(tmp_path), SETTINGS)
        payload = self._payload(modelUsage={"claude-sonnet-5": {"outputTokens": 4}})
        assert engine.parse(json.dumps(payload), "", 0).model_actual == "claude-sonnet-5"

    def test_여러_모델이_있으면_출력_토큰이_가장_많은_쪽이다(self, tmp_path: Path) -> None:
        """이어받은 세션에서 모델을 바꾸면 표에 둘이 함께 온다."""
        engine = ClaudeEngine(claude_profile(tmp_path), SETTINGS)
        payload = self._payload(modelUsage={
            "claude-haiku-4-5": {"outputTokens": 3},
            "claude-opus-5": {"outputTokens": 90},
        })
        assert engine.parse(json.dumps(payload), "", 0).model_actual == "claude-opus-5"

    def test_표가_없으면_None_이다(self, tmp_path: Path) -> None:
        """모르면 모른다고 남긴다. 요청 모델로 채우면 '같았다' 와 '몰랐다' 가
        기록에서 영영 안 갈린다."""
        engine = ClaudeEngine(claude_profile(tmp_path), SETTINGS)
        assert engine.parse(json.dumps(self._payload()), "", 0).model_actual is None
        assert engine.parse(json.dumps(self._payload(modelUsage={})), "", 0).model_actual is None

    def test_표가_사전이_아니면_None_이다(self, tmp_path: Path) -> None:
        engine = ClaudeEngine(claude_profile(tmp_path), SETTINGS)
        payload = self._payload(modelUsage=["claude-sonnet-5"])
        assert engine.parse(json.dumps(payload), "", 0).model_actual is None

    def test_값이_사전이_아닌_항목도_견딘다(self, tmp_path: Path) -> None:
        engine = ClaudeEngine(claude_profile(tmp_path), SETTINGS)
        payload = self._payload(modelUsage={"a": None, "b": {"outputTokens": 1}})
        assert engine.parse(json.dumps(payload), "", 0).model_actual == "b"

    def test_하나뿐이면_토큰이_0_이어도_그것이다(self, tmp_path: Path) -> None:
        """표에 올라온 것 자체가 그 모델이 돌았다는 기록이다."""
        engine = ClaudeEngine(claude_profile(tmp_path), SETTINGS)
        payload = self._payload(modelUsage={"claude-opus-5": {}})
        assert engine.parse(json.dumps(payload), "", 0).model_actual == "claude-opus-5"

    def test_여럿인데_최대가_0_이면_None_이다(self, tmp_path: Path) -> None:
        """어느 쪽이 답을 냈는지 자료로 안 갈린다. 삽입 순서로 고르면 기록이
        틀린 값을 확정으로 남긴다 (코덱스 리뷰 지적)."""
        engine = ClaudeEngine(claude_profile(tmp_path), SETTINGS)
        payload = self._payload(modelUsage={"a": {}, "b": {"outputTokens": 0}})
        assert engine.parse(json.dumps(payload), "", 0).model_actual is None

    def test_최대가_동률이면_None_이다(self, tmp_path: Path) -> None:
        engine = ClaudeEngine(claude_profile(tmp_path), SETTINGS)
        payload = self._payload(modelUsage={
            "a": {"outputTokens": 5}, "b": {"outputTokens": 5},
        })
        assert engine.parse(json.dumps(payload), "", 0).model_actual is None

    def test_토큰이_숫자가_아니면_요청이_안_깨진다(self, tmp_path: Path) -> None:
        """표는 CLI 가 주는 값이다. 여기서 예외가 나면 답을 받고도 응답을
        못 만든다 (코덱스 리뷰 지적)."""
        engine = ClaudeEngine(claude_profile(tmp_path), SETTINGS)
        payload = self._payload(modelUsage={
            "a": {"outputTokens": "unknown"}, "b": {"outputTokens": 2},
        })
        resp = engine.parse(json.dumps(payload), "", 0)
        assert resp.ok is True
        assert resp.model_actual == "b"


class Test요청_모델도_실행기가_찍는다:
    """기록하는 자리마다 요청 모델을 따로 구하면, 엔진을 쥐고 있지 않은 자리
    (감시 점검)는 그 값을 못 얻어 실제 모델을 요청 칸에 넣게 된다. 엔진 이름과
    같은 자리에서 한 번 찍는다 (sca-cr2b)."""

    def _실행기(self, stdout: str = "답변") -> Any:
        def fake_subprocess(cmd, cwd, timeout, env=None):
            return FakeCompleted(stdout=stdout, returncode=0)

        return EngineRunner(SETTINGS, subprocess_runner=fake_subprocess, environment_policy=통과정책())

    def test_요청한_모델이_응답에_실린다(self, tmp_path: Path) -> None:
        engine = RecordingEngine(claude_profile(tmp_path), SETTINGS)
        resp = self._실행기().run(engine, request(model="claude-opus-5"))
        assert resp.model_asked == "claude-opus-5"

    def test_요청이_비면_엔진이_정한_모델이_실린다(self, tmp_path: Path) -> None:
        """실행기가 채우는 값이 실제로 돈 요청이다. 호출자가 비워 보낸 값을
        기록하면 그 요청의 모델이 빈 칸으로 남는다."""
        engine = ClaudeEngine(claude_profile(tmp_path), SETTINGS)
        resp = self._실행기('{"result": "답", "is_error": false}').run(engine, request(model=""))
        assert resp.model_asked == engine.spec.model

    def test_시간초과_응답에도_실린다(self, tmp_path: Path) -> None:
        engine = RecordingEngine(claude_profile(tmp_path), SETTINGS)

        def fake_subprocess(cmd, cwd, timeout, env=None):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

        runner = EngineRunner(SETTINGS, subprocess_runner=fake_subprocess, environment_policy=통과정책())
        resp = runner.run(engine, request(model="claude-opus-5"))
        assert resp.model_asked == "claude-opus-5"


class Test셸이_붙는_엔진에_바깥_쓰기_수단을_알려_준다:
    """codex 와 gemini 는 셸이 늘 붙어 있어 모델이 curl 로 무엇이든 된다고
    보고 답한다. 샌드박스가 쓰기를 막으면 이름 해석에서 끊겨 '등록 실패' 만
    남는다. claude 는 도구 목록 자체가 권한이라 이 문제가 없다.
    원본 bot.py:1571 write_paths_note 대조 (sca-w415).
    """

    def _codex(self, tmp_path: Path, **options: Any) -> CodexEngine:
        return CodexEngine(
            profile_with(
                {"type": "codex", "binary": "codex", "model": "gpt-5", "options": options},
                tmp_path=tmp_path,
            ),
            SETTINGS,
        )

    def test_클로드는_안_붙인다(self, tmp_path: Path) -> None:
        """도구 목록이 곧 권한이라 없는 일을 시도하지 않는다."""
        engine = ClaudeEngine(claude_profile(tmp_path), SETTINGS)
        assert engine.write_paths_note(request()) == ""

    def test_샌드박스가_쓰기를_막으면_붙인다(self, tmp_path: Path) -> None:
        note = self._codex(tmp_path, sandbox="read-only").write_paths_note(request())
        assert "셸" in note and "curl" in note

    def test_샌드박스가_안_막으면_안_붙인다(self, tmp_path: Path) -> None:
        """막힌 적이 없는데 막혔다고 적으면 할 수 있는 일을 안 한다."""
        assert self._codex(tmp_path, sandbox="danger-full-access").write_paths_note(request()) == ""

    def test_이번_턴에_허용된_도구를_적는다(self, tmp_path: Path) -> None:
        note = self._codex(tmp_path, sandbox="read-only").write_paths_note(
            request(tools=ToolSelection.allow(["mcp__x__post", "Read"]))
        )
        assert "mcp__x__post" in note

    def test_도구가_없으면_없다고_적는다(self, tmp_path: Path) -> None:
        note = self._codex(tmp_path, sandbox="read-only").write_paths_note(
            request(tools=ToolSelection.forbid_all())
        )
        assert "없다" in note

    def test_제한이_없으면_붙은_서버를_적는다(self, tmp_path: Path) -> None:
        """이름을 코드에 박지 않는다. 프로필이 무엇을 붙였는지에서 뽑는다."""
        profile = profile_with(
            {"type": "codex", "binary": "codex", "model": "gpt-5",
             "options": {"sandbox": "read-only"}},
            tmp_path=tmp_path,
            mcp_servers={"jira": {"command": "jira-mcp"}},
        )
        note = CodexEngine(profile, SETTINGS).write_paths_note(
            request(tools=ToolSelection.unrestricted())
        )
        assert "jira" in note

    def test_약속과_실행이_어긋나지_않게_못박는다(self, tmp_path: Path) -> None:
        note = self._codex(tmp_path, sandbox="read-only").write_paths_note(request())
        assert "도구 응답" in note

    def test_제미나이는_격리가_없어_안_붙인다(self, tmp_path: Path) -> None:
        """agy 는 --sandbox 를 안 쓰고 --dangerously-skip-permissions 로만 돈다
        (gemini.py:183, docs/agy-실측.md). 프로필에 sandbox 를 적어도 명령에
        안 실리므로 막혔다고 안내하면 거짓이다 (코덱스 리뷰)."""
        engine = GeminiEngine(gemini_profile(tmp_path, options={"sandbox": "read-only"}), SETTINGS)
        assert engine.write_paths_note(request()) == ""

    def test_제미나이_조립에도_같은_문장이_실린다(self, tmp_path: Path) -> None:
        """격리가 켜지는 날 문장이 두 벌로 갈리지 않게 한다."""

        class 격리된제미나이(GeminiEngine):
            def blocks_outbound_writes(self, request: EngineRequest) -> bool:
                return True

        engine = 격리된제미나이(gemini_profile(tmp_path), SETTINGS)
        assert "curl" in " ".join(engine.build_command(request()))
        assert engine.write_paths_note(request()) == self._codex(
            tmp_path, sandbox="read-only"
        ).write_paths_note(request())

    def test_네트워크가_열려_있으면_안_붙인다(self, tmp_path: Path) -> None:
        """workspace-write 는 프로필이 network 를 열면 바깥으로 나간다
        (codex.py:176). 막혔다고 적으면 되는 일을 안 한다 (코덱스 리뷰)."""
        engine = self._codex(tmp_path, sandbox="workspace-write", network=True)
        assert engine.write_paths_note(request()) == ""

    def test_workspace_write_는_기본으로_막힌다(self, tmp_path: Path) -> None:
        engine = self._codex(tmp_path, sandbox="workspace-write")
        assert "curl" in engine.write_paths_note(request())

    def test_읽기전용은_network_를_열어도_막힌다(self, tmp_path: Path) -> None:
        """network_access 키는 workspace-write 샌드박스에만 걸린다."""
        engine = self._codex(tmp_path, sandbox="read-only", network=True)
        assert "curl" in engine.write_paths_note(request())

    def test_꺼진_서버는_목록에_없다(self, tmp_path: Path) -> None:
        profile = profile_with(
            {"type": "codex", "binary": "codex", "model": "gpt-5",
             "options": {"sandbox": "read-only"}},
            tmp_path=tmp_path,
            mcp_servers={"jira": {"command": "jira-mcp"},
                         "old": {"command": "old-mcp", "disabled": True}},
        )
        note = CodexEngine(profile, SETTINGS).write_paths_note(
            request(tools=ToolSelection.unrestricted())
        )
        assert "jira" in note and "old" not in note

    def test_이어가는_턴에도_실린다(self, tmp_path: Path) -> None:
        """codex 는 resume 에서 developer_instructions 를 못 바꿔 지침이
        프롬프트로 간다. 거기에 노트가 빠져 스레드 두 번째 메시지부터
        안내가 사라졌다."""
        engine = self._codex(tmp_path, sandbox="read-only")
        cmd = engine.build_command(request(resume=True, session_id="S1"))
        assert "curl" in cmd[-1]

    def test_이어가는_턴의_바이트가_footprint_에_잡힌다(self, tmp_path: Path) -> None:
        """실제로 보내는데 안 세면 예산이 실제보다 작게 나온다 (코덱스 리뷰)."""
        요청 = request(resume=True, session_id="S1")
        막힌쪽 = self._codex(tmp_path, sandbox="read-only")
        열린쪽 = self._codex(tmp_path, sandbox="danger-full-access")
        차이 = (
            막힌쪽.footprint_for(요청).adapter_added_bytes
            - 열린쪽.footprint_for(요청).adapter_added_bytes
        )
        assert 차이 == len(막힌쪽.write_paths_note(요청).encode())

    def test_시스템_지침에_실린다(self, tmp_path: Path) -> None:
        """만든 것과 프롬프트에 실은 것은 다르다."""
        engine = self._codex(tmp_path, sandbox="read-only")
        붙은것 = engine._session_path_note(request())
        assert "curl" in 붙은것
