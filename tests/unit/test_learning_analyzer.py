"""학습 제안 생성 — 채널 기록을 분석해 제안 항목을 뽑는다.

원본 learn.py 의 build_proposal()/main() 의 취합 루프를 대응한다. 분석
실행 자체는 기존 엔진 계층(``Engine``/``EngineRunner``)을 그대로 재사용한다
— learn.py 의 CLI 호출과 원리가 같고(Claude CLI 를 `-p --output-format json`
으로 불러 `result` 필드를 읽는다), 중복 구현할 이유가 없다.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from slack_cli_agent.core.result import OutcomeKind
from slack_cli_agent.engine.base import Engine, EngineRequest, EngineResponse
from slack_cli_agent.engine.runner import EngineRunner
from slack_cli_agent.learning.analyzer import ProposalAnalyzer, ProposalBuilder


class FakeEngine(Engine):
    """subprocess 를 실제로 부르지 않고 정해 둔 stdout 을 그대로 파싱하는 엔진."""

    name = "fake"

    def __init__(self) -> None:
        # Engine.__init__ 은 profile/settings 를 요구하지만 이 테스트는 안 쓴다.
        self.profile = None
        self.settings = None
        self.built_requests: list[EngineRequest] = []

    def build_command(self, request: EngineRequest) -> list[str]:
        self.built_requests.append(request)
        return ["fake"]

    def parse(self, stdout: str, stderr: str, returncode: int) -> EngineResponse:
        if returncode != 0:
            return EngineResponse(
                ok=False, body="실패", session_id=None, model_actual=None,
                elapsed=0.0, turns=None, usage=None, raw={}, failure_reason="nonzero_exit",
            )
        payload = json.loads(stdout)
        return EngineResponse(
            ok=True, body=payload.get("result", ""), session_id=None,
            model_actual=None, elapsed=0.0, turns=None, usage=None, raw=payload,
        )

    def new_session_id(self) -> str:
        return "fixed-session"

    def detect_usage_limit(self, response: EngineResponse):
        return None


def make_runner(stdout: str, returncode: int = 0) -> EngineRunner:
    def fake_subprocess(cmd, cwd, timeout, env=None):
        return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)

    class 통과정책:
        def build(self, source_env):
            return dict(source_env)

    settings = SimpleNamespace(request_timeout_sec=10)
    return EngineRunner(settings, subprocess_runner=fake_subprocess, environment_policy=통과정책())


def analysis_stdout(**fields) -> str:
    data = {"writing_style": [], "channel_facts": [], "corrections": [], "note": ""}
    data.update(fields)
    return json.dumps({"result": json.dumps(data, ensure_ascii=False)})


class TestProposalAnalyzer:
    def test_정상_응답을_채널_분석_결과로_파싱한다(self, tmp_path: Path) -> None:
        engine = FakeEngine()
        runner = make_runner(analysis_stdout(
            channel_facts=["9월 회의는 매주 화요일이다"],
            writing_style=["문장을 짧게 써라"],
        ))
        analyzer = ProposalAnalyzer(
            engine, runner, model="m", effort="low", workdir=tmp_path, bot_name="봇",
        )
        outcome = analyzer.analyze_channel("2026-09-14", "공지", "기록 본문", [])
        assert outcome.is_found
        result = outcome.value()
        assert result.channel_facts == ("9월 회의는 매주 화요일이다",)
        assert result.writing_style == ("문장을 짧게 써라",)

    def test_프롬프트에_채널명과_봇이름이_들어간다(self, tmp_path: Path) -> None:
        engine = FakeEngine()
        runner = make_runner(analysis_stdout())
        analyzer = ProposalAnalyzer(
            engine, runner, model="m", effort="low", workdir=tmp_path, bot_name="봇",
        )
        analyzer.analyze_channel("2026-09-14", "공지", "기록 본문", [])
        request = engine.built_requests[0]
        assert "공지" in request.system_prompt
        assert "봇" in request.system_prompt
        assert "기록 본문" in request.prompt

    def test_엔진_실행_실패는_판정_불가다(self, tmp_path: Path) -> None:
        engine = FakeEngine()
        runner = make_runner("실패", returncode=1)
        analyzer = ProposalAnalyzer(
            engine, runner, model="m", effort="low", workdir=tmp_path, bot_name="봇",
        )
        outcome = analyzer.analyze_channel("2026-09-14", "공지", "기록", [])
        assert outcome.kind is OutcomeKind.UNKNOWN

    def test_json이_아닌_응답은_판정_불가다(self, tmp_path: Path) -> None:
        engine = FakeEngine()
        runner = make_runner(json.dumps({"result": "그냥 문장일 뿐이다"}))
        analyzer = ProposalAnalyzer(
            engine, runner, model="m", effort="low", workdir=tmp_path, bot_name="봇",
        )
        outcome = analyzer.analyze_channel("2026-09-14", "공지", "기록", [])
        assert outcome.kind is OutcomeKind.UNKNOWN


class TestProposalBuilder:
    def test_여러_채널_결과를_하나의_제안으로_합친다(self, tmp_path: Path) -> None:
        engine = FakeEngine()
        runner = make_runner(analysis_stdout(channel_facts=["사실 A"]))
        analyzer = ProposalAnalyzer(
            engine, runner, model="m", effort="low", workdir=tmp_path, bot_name="봇",
        )
        builder = ProposalBuilder(analyzer)
        proposal = builder.build("2026-09-14", {"공지": "기록1", "잡담": "기록2"})
        assert proposal.day == "2026-09-14"
        assert proposal.channel_knowledge["공지"] == ("사실 A",)
        assert proposal.channel_knowledge["잡담"] == ("사실 A",)

    def test_분석_실패한_채널은_note에_사유를_남기고_계속한다(self, tmp_path: Path) -> None:
        engine = FakeEngine()
        runner = make_runner("실패", returncode=1)
        analyzer = ProposalAnalyzer(
            engine, runner, model="m", effort="low", workdir=tmp_path, bot_name="봇",
        )
        builder = ProposalBuilder(analyzer)
        proposal = builder.build("2026-09-14", {"공지": "기록1"})
        assert proposal.channel_knowledge == {}
        assert "공지" in proposal.note
