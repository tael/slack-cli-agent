"""GeminiEngine(agy 어댑터) 고유 동작 시험.

계약 시험(test_engine_contract.py)이 다루지 않는 것만 여기서 본다: 모델/effort
정규화 매핑, 단일 JSON 객체 파싱과 status 판정, settings.json 기록.
근거는 docs/agy-실측.md.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from slack_cli_agent.auth.principal import TrustLevel
from slack_cli_agent.config.profile import Profile
from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.engine.base import EngineRequest
from slack_cli_agent.engine.capability import InstructionBoundary
from slack_cli_agent.engine.gemini import UNTRUSTED_INPUT_MARK, GeminiEngine


def _profile(tmp_path: Path, home_dir: Path | None = None) -> Profile:
    primary: dict[str, object] = {"type": "gemini", "binary": "agy", "model": "gemini-3.8-flash"}
    if home_dir is not None:
        primary["home_dir"] = str(home_dir)
    return Profile.from_dict({
        "name": "gemini-test",
        "primary_engine": primary,
        "state_dir": str(tmp_path / "state"),
    })


def _engine(tmp_path: Path, home_dir: Path | None = None) -> GeminiEngine:
    return GeminiEngine(_profile(tmp_path, home_dir), RuntimeSettings())


def _request(**overrides: object) -> EngineRequest:
    base: dict[str, Any] = {
        "prompt": "안녕",
        "system_prompt": "시스템 지침",
        "session_id": "11111111-1111-1111-1111-111111111111",
        "resume": False,
        "model": "gemini-3.8-flash",
        "effort": "medium",
        "workdir": Path("/tmp/gemini-work"),
        "readable_dirs": (),
        "allowed_tools": (),
        "trust_level": TrustLevel.GENERAL,
    }
    base.update(overrides)
    return EngineRequest(**base)


def _joined(cmd: list[str]) -> str:
    return "\x1f".join(cmd)


class TestModelAndEffortNormalization:
    def test_접미사가_붙은_모델은_기본_이름과_effort로_갈린다(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        cmd = engine.build_command(_request(model="gemini-3.8-flash-medium", effort=""))
        joined = _joined(cmd)
        assert "gemini-3.8-flash-medium" not in joined
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "gemini-3.8-flash"
        eidx = cmd.index("--effort")
        assert cmd[eidx + 1] == "medium"

    def test_프로필_effort가_접미사보다_우선한다(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        cmd = engine.build_command(_request(model="gemini-3.8-flash-low", effort="high"))
        eidx = cmd.index("--effort")
        assert cmd[eidx + 1] == "high"

    def test_접미사_없는_모델은_그대로_쓴다(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        cmd = engine.build_command(_request(model="gemini-3.1-pro", effort="low"))
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "gemini-3.1-pro"

    def test_xhigh는_high로_내린다(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        cmd = engine.build_command(_request(effort="xhigh"))
        eidx = cmd.index("--effort")
        assert cmd[eidx + 1] == "high"

    def test_max는_high로_내린다(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        cmd = engine.build_command(_request(effort="max"))
        eidx = cmd.index("--effort")
        assert cmd[eidx + 1] == "high"

    def test_빈_effort는_medium으로_내린다(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        cmd = engine.build_command(_request(model="gemini-3.1-pro", effort=""))
        eidx = cmd.index("--effort")
        assert cmd[eidx + 1] == "medium"

    def test_유효한_effort는_그대로_통과한다(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        for value in ("low", "medium", "high"):
            cmd = engine.build_command(_request(effort=value))
            eidx = cmd.index("--effort")
            assert cmd[eidx + 1] == value


class TestArgumentOrder:
    def test_p는_명령의_마지막_토큰_바로_앞이다(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        cmd = engine.build_command(_request())
        assert cmd[-2] == "-p"

    def test_프롬프트에_시스템_프롬프트가_앞에_붙는다(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        cmd = engine.build_command(_request(system_prompt="지침-X", prompt="본문-Y"))
        prompt_arg = cmd[-1]
        assert prompt_arg.index("지침-X") < prompt_arg.index("본문-Y")

    def test_비신뢰_입력_앞에_경계_표시가_붙는다(self, tmp_path: Path) -> None:
        """agy 에는 시스템 프롬프트 플래그가 없어 지침과 입력이 한 문자열로 간다.
        채널 기록은 비신뢰 입력이라 어디부터가 입력인지 표시라도 있어야 한다
        (sca-dyb.12). 이것은 강제가 아니라 표시이며 capability 도 그렇게 말한다."""
        engine = _engine(tmp_path)
        cmd = engine.build_command(_request(system_prompt="지침-X", prompt="본문-Y"))
        prompt_arg = cmd[-1]
        assert prompt_arg.index("지침-X") < prompt_arg.index(UNTRUSTED_INPUT_MARK)
        assert prompt_arg.index(UNTRUSTED_INPUT_MARK) < prompt_arg.index("본문-Y")

    def test_경계가_표시뿐임을_capability_가_말한다(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        assert engine.capabilities.instruction_boundary is InstructionBoundary.PROMPT_ONLY

    def test_dangerously_skip_permissions가_들어간다(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        cmd = engine.build_command(_request())
        assert "--dangerously-skip-permissions" in cmd

    def test_print_timeout이_설정_초와_함께_들어간다(self, tmp_path: Path) -> None:
        settings = RuntimeSettings(request_timeout_sec=120)
        engine = GeminiEngine(_profile(tmp_path), settings)
        cmd = engine.build_command(_request())
        idx = cmd.index("--print-timeout")
        assert cmd[idx + 1] == "120s"


class TestParse:
    def test_status가_SUCCESS면_ok(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        payload = {
            "conversation_id": "conv-1", "status": "SUCCESS", "response": "답변 본문",
            "error": "", "duration_seconds": 1.5, "num_turns": 2,
            "usage": {"input_tokens": 1, "output_tokens": 2, "thinking_tokens": 3,
                      "cache_read_tokens": 0, "total_tokens": 3},
        }
        resp = engine.parse(json.dumps(payload, ensure_ascii=False), "", 0)
        assert resp.ok
        assert resp.body == "답변 본문"
        assert resp.session_id == "conv-1"
        assert resp.turns == 2

    def test_종료코드_0이어도_status가_SUCCESS가_아니면_실패(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        payload = {
            "conversation_id": "", "status": "ERROR", "response": "",
            "error": "permission denied", "duration_seconds": 0, "num_turns": 0,
            "usage": {"input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0,
                      "cache_read_tokens": 0, "total_tokens": 0},
        }
        resp = engine.parse(json.dumps(payload, ensure_ascii=False), "", 0)
        assert not resp.ok
        assert resp.failure_reason == "is_error"

    def test_종료코드_1_이어도_JSON을_그대로_읽는다(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        payload = {
            "conversation_id": "", "status": "ERROR", "response": "",
            "error": "invalid --effort \"xhigh\"", "duration_seconds": 0, "num_turns": 0,
            "usage": {"input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0,
                      "cache_read_tokens": 0, "total_tokens": 0},
        }
        resp = engine.parse(json.dumps(payload, ensure_ascii=False), "stderr 문구", 1)
        assert not resp.ok
        assert resp.failure_reason == "is_error"

    def test_잘못된_JSON은_bad_json(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        resp = engine.parse("이건 JSON 이 아니다", "", 0)
        assert not resp.ok
        assert resp.failure_reason == "bad_json"

    def test_빈_응답은_empty_response(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        payload = {
            "conversation_id": "conv-2", "status": "SUCCESS", "response": "",
            "error": "", "duration_seconds": 0.1, "num_turns": 1,
            "usage": {"input_tokens": 1, "output_tokens": 0, "thinking_tokens": 0,
                      "cache_read_tokens": 0, "total_tokens": 1},
        }
        resp = engine.parse(json.dumps(payload, ensure_ascii=False), "", 0)
        assert not resp.ok
        assert resp.failure_reason == "empty_response"

    def test_usage의_thinking_tokens는_raw에_남는다(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        payload = {
            "conversation_id": "conv-3", "status": "SUCCESS", "response": "답변",
            "error": "", "duration_seconds": 1.0, "num_turns": 1,
            "usage": {"input_tokens": 1, "output_tokens": 2, "thinking_tokens": 77,
                      "cache_read_tokens": 0, "total_tokens": 3},
        }
        resp = engine.parse(json.dumps(payload, ensure_ascii=False), "", 0)
        assert resp.raw["usage"]["thinking_tokens"] == 77
        assert resp.usage is not None
        assert resp.usage.cache_creation_tokens == 0

    def test_cache_read_tokens가_공통_어휘로_옮겨진다(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        payload = {
            "conversation_id": "conv-4", "status": "SUCCESS", "response": "답변",
            "error": "", "duration_seconds": 1.0, "num_turns": 1,
            "usage": {"input_tokens": 1, "output_tokens": 2, "thinking_tokens": 0,
                      "cache_read_tokens": 9, "total_tokens": 12},
        }
        resp = engine.parse(json.dumps(payload, ensure_ascii=False), "", 0)
        assert resp.usage is not None
        assert resp.usage.cache_read_tokens == 9


class TestDetectUsageLimit:
    def test_항상_None이다(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        payload = {
            "conversation_id": "c", "status": "SUCCESS", "response": "답",
            "error": "", "duration_seconds": 0.1, "num_turns": 1,
            "usage": {"input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0,
                      "cache_read_tokens": 0, "total_tokens": 0},
        }
        resp = engine.parse(json.dumps(payload, ensure_ascii=False), "", 0)
        assert engine.detect_usage_limit(resp) is None


class TestSessionId:
    def test_new_session_id는_문자열이다(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        sid = engine.new_session_id()
        assert isinstance(sid, str) and sid

    def test_session_id_from은_conversation_id를_돌려준다(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        payload = {
            "conversation_id": "conv-9", "status": "SUCCESS", "response": "답",
            "error": "", "duration_seconds": 0.1, "num_turns": 1,
            "usage": {"input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0,
                      "cache_read_tokens": 0, "total_tokens": 0},
        }
        resp = engine.parse(json.dumps(payload, ensure_ascii=False), "", 0)
        assert engine.session_id_from(resp) == "conv-9"


class TestSettingsFile:
    def test_home_dir이_있으면_settings_json을_전면허용으로_쓴다(self, tmp_path: Path) -> None:
        home = tmp_path / "bot-home"
        engine = _engine(tmp_path, home_dir=home)
        engine.prepare(_request())

        settings_path = home / ".gemini" / "antigravity-cli" / "settings.json"
        assert settings_path.is_file()
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        assert data["allowNonWorkspaceAccess"] is True
        assert data["permissions"]["allow"] == ["*"]
        assert data["permissions"]["deny"] == []
        assert data["permissions"]["ask"] == []

    def test_home_dir이_없으면_아무_파일도_안_쓴다(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path, home_dir=None)
        engine.prepare(_request())
        assert not (tmp_path / ".gemini").exists()


class TestSettingsWriteIsNotDestructive:
    """settings.json 은 agy 가 색 구성·편집기·modelProvider 같은 다른 값도 두는
    파일이다. 우리 키만 바꾸고 나머지는 남겨야 한다."""

    def test_기존_설정_키가_남는다(self, tmp_path: Path) -> None:
        home = tmp_path / "gemini-home"
        path = home / ".gemini" / "antigravity-cli" / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({"colorScheme": "dark", "modelProvider": "gemini"}),
            encoding="utf-8",
        )
        engine = _engine(tmp_path, home_dir=home)

        engine.prepare(_request())

        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["colorScheme"] == "dark"
        assert data["modelProvider"] == "gemini"
        assert data["allowNonWorkspaceAccess"] is True
        assert data["permissions"]["allow"] == ["*"]

    def test_깨진_설정_파일이어도_전면허용을_쓴다(self, tmp_path: Path) -> None:
        home = tmp_path / "gemini-home"
        path = home / ".gemini" / "antigravity-cli" / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text("{ 깨진 json", encoding="utf-8")
        engine = _engine(tmp_path, home_dir=home)

        engine.prepare(_request())

        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["permissions"]["allow"] == ["*"]


class TestBuildCommandHasNoSideEffect:
    """명령을 만드는 것과 파일을 쓰는 것은 다른 일이다. 계약 시험은
    build_command() 만 부르므로, 거기서 파일을 쓰면 시험이 디스크를 건드린다."""

    def test_build_command는_파일을_안_쓴다(self, tmp_path: Path) -> None:
        home = tmp_path / "gemini-home"
        engine = _engine(tmp_path, home_dir=home)

        engine.build_command(_request())

        assert not (home / ".gemini").exists()

    def test_실행기가_prepare를_부른다(self, tmp_path: Path) -> None:
        from slack_cli_agent.engine.runner import EngineRunner

        home = tmp_path / "gemini-home"
        engine = _engine(tmp_path, home_dir=home)

        class _Completed:
            stdout = json.dumps({"conversation_id": "c1", "status": "SUCCESS", "response": "답"})
            stderr = ""
            returncode = 0

        def fake_run(cmd: list[str], cwd: str, timeout: float, **_: object) -> _Completed:
            return _Completed()

        runner = EngineRunner(RuntimeSettings(), subprocess_runner=fake_run)
        runner.run(engine, _request())

        path = home / ".gemini" / "antigravity-cli" / "settings.json"
        assert path.is_file()


class TestKeychainLink:
    """agy 는 자격증명을 macOS 로그인 키체인에 저장한다(공식 설치·인증 문서).

    HOME 을 봇별 경로로 바꾸면 그 아래에 Library/Keychains 가 없어서 기본
    키체인을 못 찾고, 토큰을 갱신할 때마다 "저장할 키체인을 찾을 수 없습니다"
    대화상자가 화면에 뜬다(2026-09-15 사용자 보고). 실제 키체인 위치를
    격리 홈에서도 보이게 연결한다.
    """

    def test_격리_홈에_실제_키체인_경로를_연결한다(self, tmp_path: Path) -> None:
        real = Path.home() / "Library" / "Keychains"
        real.mkdir(parents=True, exist_ok=True)
        home = tmp_path / "bot-home"

        _engine(tmp_path, home_dir=home).prepare(_request())

        link = home / "Library" / "Keychains"
        assert link.is_symlink()
        assert link.resolve() == real.resolve()

    def test_홈이_실제_홈이면_연결하지_않는다(self, tmp_path: Path) -> None:
        """자기 자신을 가리키는 연결을 만들지 않는다."""
        real = Path.home() / "Library" / "Keychains"
        real.mkdir(parents=True, exist_ok=True)

        _engine(tmp_path, home_dir=Path.home()).prepare(_request())

        assert not (Path.home() / "Library" / "Keychains").is_symlink()

    def test_이미_실제_디렉터리가_있으면_안_건드린다(self, tmp_path: Path) -> None:
        (Path.home() / "Library" / "Keychains").mkdir(parents=True, exist_ok=True)
        home = tmp_path / "bot-home"
        existing = home / "Library" / "Keychains"
        existing.mkdir(parents=True)
        (existing / "login.keychain-db").write_text("실물", encoding="utf-8")

        _engine(tmp_path, home_dir=home).prepare(_request())

        assert not existing.is_symlink()
        assert (existing / "login.keychain-db").read_text(encoding="utf-8") == "실물"


class Test설정파일을바꿀것이없으면안쓴다:
    """학습 배치는 운영상 읽기 전용이다. prepare 가 매번 파일을 다시 쓰면 그
    의미와 어긋나고, 같은 홈을 보는 실행이 겹칠 때 서로의 파일을 덮는다
    (sca-dyb.13).
    """

    def test_두_번째_prepare_는_settings_json_을_다시_쓰지_않는다(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        engine = _engine(tmp_path, home_dir=home)
        engine.prepare(_request())
        path = home / ".gemini" / "antigravity-cli" / "settings.json"
        before = path.stat().st_mtime_ns

        engine.prepare(_request())

        assert path.stat().st_mtime_ns == before

    def test_값이_달라지면_다시_쓴다(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        engine = _engine(tmp_path, home_dir=home)
        engine.prepare(_request())
        path = home / ".gemini" / "antigravity-cli" / "settings.json"
        기존 = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps({"colorScheme": "dark"}), encoding="utf-8")

        engine.prepare(_request())

        새것 = json.loads(path.read_text(encoding="utf-8"))
        assert 새것["colorScheme"] == "dark"
        for key, value in 기존.items():
            assert 새것[key] == value

    def test_두_번째_prepare_는_mcp_설정을_다시_쓰지_않는다(self, tmp_path: Path) -> None:
        workdir = tmp_path / "work"
        workdir.mkdir()
        profile = Profile.from_dict({
            "name": "gemini-test",
            "primary_engine": {"type": "gemini", "binary": "agy", "model": "gemini-3.8-flash"},
            "state_dir": str(tmp_path / "state"),
            "mcp_servers": {"도구": {"command": "도구-서버"}},
        })
        engine = GeminiEngine(profile, RuntimeSettings())
        engine.prepare(_request(workdir=workdir))
        path = workdir / ".agents" / "mcp_config.json"
        before = path.stat().st_mtime_ns

        engine.prepare(_request(workdir=workdir))

        assert path.stat().st_mtime_ns == before
