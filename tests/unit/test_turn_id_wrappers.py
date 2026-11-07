"""보조 스크립트와 MCP 서버가 턴 식별자를 받는지 본다 (C10).

마메치는 엔진 자식 환경에 MAMETCHI_TURN 을 넣어 mb_card.py 같은 래퍼가 감사
기록에 어느 요청에서 불렸는지 남기게 했다. 이 엔진은 TURN_ID 만 넘겨, 기존
이름을 읽는 래퍼는 빈 값을 기록했다. 또 Codex 는 MCP 서버에 부모 환경을
물려주지 않아 MCP 서버는 턴 식별자를 받을 경로가 없었고, 2차 엔진으로 전환한
턴은 턴 환경 전체를 잃었다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from slack_cli_agent.config.profile import McpServerSpec
from slack_cli_agent.core.errors import ConfigError
from slack_cli_agent.engine.environment import (
    TURN_ID_ALIASES_ENV,
    ClaudeEnvironmentPolicy,
    CodexEnvironmentPolicy,
    GeminiEnvironmentPolicy,
)

_정책들 = (ClaudeEnvironmentPolicy, CodexEnvironmentPolicy, GeminiEnvironmentPolicy)


class Test래퍼_별칭:
    @pytest.mark.parametrize("policy", _정책들, ids=lambda p: p.__name__)
    def test_별칭_이름으로도_턴_식별자를_넘긴다(self, policy) -> None:
        env = policy("test").build({
            "PATH": "/bin", "TURN_ID": "C1:5.0", TURN_ID_ALIASES_ENV: "MAMETCHI_TURN",
        })
        assert env["TURN_ID"] == "C1:5.0"
        assert env["MAMETCHI_TURN"] == "C1:5.0"

    @pytest.mark.parametrize("policy", _정책들, ids=lambda p: p.__name__)
    def test_턴_식별자가_없으면_별칭도_없다(self, policy) -> None:
        env = policy("test").build({"PATH": "/bin", TURN_ID_ALIASES_ENV: "MAMETCHI_TURN"})
        assert "MAMETCHI_TURN" not in env

    @pytest.mark.parametrize("policy", _정책들, ids=lambda p: p.__name__)
    def test_기본_변수를_덮는_별칭은_무시한다(self, policy) -> None:
        env = policy("test").build({
            "PATH": "/bin", "HOME": "/h", "TURN_ID": "C1:5.0",
            TURN_ID_ALIASES_ENV: "PATH, HOME,BOT_PROFILE,bad-name,OK_TURN",
        })
        assert env["PATH"] == "/bin"
        assert env["BOT_PROFILE"] == "test"
        assert "bad-name" not in env
        assert env["OK_TURN"] == "C1:5.0"

    def test_별칭이_정책이_채운_가드_변수를_덮지_않는다(self) -> None:
        """SCA_VERIFY_NESTED 가 비어 있지 않으면 Stop 훅 모델 검증이 꺼진다
        (finish_review). 비어 있는 가드 변수도 별칭으로 채워지면 안 된다."""
        names = "SCA_VERIFY_NESTED,TURN_DEADLINE_MONO,CI,POST_CHANNEL,MCP_TOOL_TIMEOUT,OK_TURN"
        env = ClaudeEnvironmentPolicy("test").build({
            "PATH": "/bin", "TURN_ID": "C1:5.0", "TURN_DEADLINE_MONO": "123.5",
            "POST_CHANNEL": "C9", TURN_ID_ALIASES_ENV: names,
        })
        assert "SCA_VERIFY_NESTED" not in env
        assert env["TURN_DEADLINE_MONO"] == "123.5"
        assert env["CI"] == "1"
        assert env["POST_CHANNEL"] == "C9"
        assert env["MCP_TOOL_TIMEOUT"] != "C1:5.0"
        assert env["OK_TURN"] == "C1:5.0"

    @pytest.mark.parametrize("policy", (CodexEnvironmentPolicy, GeminiEnvironmentPolicy),
                             ids=lambda p: p.__name__)
    def test_다른_엔진도_가드_변수_이름을_별칭으로_만들지_않는다(self, policy) -> None:
        env = policy("test").build({
            "PATH": "/bin", "TURN_ID": "C1:5.0",
            TURN_ID_ALIASES_ENV: "CI,SCA_VERIFY_NESTED,GIT_TERMINAL_PROMPT,OK_TURN",
        })
        assert env["CI"] == "1"
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert "SCA_VERIFY_NESTED" not in env
        assert env["OK_TURN"] == "C1:5.0"

    def test_별칭_목록_변수_자체는_자식에_넘기지_않는다(self) -> None:
        for policy in _정책들:
            env = policy("test").build({"TURN_ID": "C1:5.0", TURN_ID_ALIASES_ENV: "X_TURN"})
            assert TURN_ID_ALIASES_ENV not in env, policy.__name__


class TestMCP_턴키:
    def _spec(self) -> McpServerSpec:
        return McpServerSpec.from_dict(
            "metabase", {"command": "서버", "turn_env": {"MAMETCHI_TURN": "turn_id"}}
        )

    def test_턴_식별자를_서버_env_에_넣는다(self) -> None:
        assert self._spec().turn_env_for(
            can_act=False, turn_context={"turn_id": "C1:5.0"}
        ) == {"MAMETCHI_TURN": "C1:5.0"}

    def test_턴_식별자가_비면_뺀다(self) -> None:
        assert self._spec().turn_env_for(can_act=True, turn_context={}) == {}

    def test_모르는_키는_여전히_막는다(self) -> None:
        with pytest.raises(ConfigError):
            McpServerSpec.from_dict("x", {"command": "서버", "turn_env": {"A": "turn"}})


def test_파이프라인이_턴_식별자를_MCP_맥락에_싣는다(tmp_path: Path) -> None:
    from test_pipeline import build_pipeline, make_ctx, ok_response

    pipeline, deps = build_pipeline(responses=[ok_response()], tmp_path=tmp_path)
    context = make_ctx(channel="C1")
    pipeline.handle(context)

    request = deps["runner"].calls[0]
    assert request.turn_context["turn_id"] == f"C1:{context.ts}"
    assert request.turn_context["channel"] == "C1"


def test_2차_엔진_전환_턴도_턴_환경을_유지한다(tmp_path: Path) -> None:
    from test_engine import TestFallbackEngine, request

    fallback, _primary, secondary, switcher = TestFallbackEngine()._fallback(tmp_path)
    switcher.begin_switch("weekly limit", engine_name="codex")
    switcher.approve()

    fallback.run(request(
        engine_env={"TURN_ID": "C1:5.0", "POST_CHANNEL": "C1"},
        turn_context={"channel": "C1", "turn_id": "C1:5.0"},
    ))

    built = secondary.built[-1]
    assert built.engine_env["TURN_ID"] == "C1:5.0"
    assert built.turn_context["turn_id"] == "C1:5.0"


class Test턴_밖_호출의_턴키:
    """감시, 리뷰, 학습 호출은 파이프라인의 TURN_ID 없이 엔진을 부른다. 마메치는
    이런 호출에 MAMETCHI_TURN=unknown 을 넘겨 사람의 터미널 호출과 구분했다."""

    def _run(self, tmp_path: Path, req):
        from test_engine import SETTINGS, FakeCompleted, RecordingEngine, claude_profile

        from slack_cli_agent.engine.runner import EngineRunner

        engine = RecordingEngine(claude_profile(tmp_path), SETTINGS)
        envs: list = []

        def fake_subprocess(cmd, cwd, timeout, env=None):
            envs.append(env)
            return FakeCompleted(stdout="답변", returncode=0)

        EngineRunner(
            SETTINGS, subprocess_runner=fake_subprocess,
            environment_policy=ClaudeEnvironmentPolicy("test"),
            source_env={"PATH": "/bin", TURN_ID_ALIASES_ENV: "MAMETCHI_TURN"},
        ).run(engine, req)
        return engine.built[-1], envs[0]

    def test_요청_식별자를_턴키로_쓴다(self, tmp_path: Path) -> None:
        from test_engine import request

        built, env = self._run(tmp_path, request(request_id="watch-7-3"))
        assert env["TURN_ID"] == "watch-7-3"
        assert env["MAMETCHI_TURN"] == "watch-7-3"
        assert built.turn_context["turn_id"] == "watch-7-3"

    def test_식별자도_없으면_unknown(self, tmp_path: Path) -> None:
        from test_engine import request

        built, env = self._run(tmp_path, request())
        assert env["TURN_ID"] == "unknown"
        assert built.turn_context["turn_id"] == "unknown"

    def test_호출자가_준_턴키는_바꾸지_않는다(self, tmp_path: Path) -> None:
        from test_engine import request

        built, env = self._run(tmp_path, request(
            request_id="r1", engine_env={"TURN_ID": "C1:5.0"},
            turn_context={"channel": "C1", "turn_id": "C1:5.0"},
        ))
        assert env["TURN_ID"] == "C1:5.0"
        assert built.turn_context == {"channel": "C1", "turn_id": "C1:5.0"}
