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
from slack_cli_agent.engine.runner import DirectInvoker, EngineRunner
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

    @property
    def spec(self):
        # Engine.spec 은 프로필 블록을 찾는데 이 대역에는 프로필이 없다.
        return SimpleNamespace(model=f"{self.name}-모델")

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
            DirectInvoker(runner, engine), model="m", effort="low", workdir=tmp_path, bot_name="봇",
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
            DirectInvoker(runner, engine), model="m", effort="low", workdir=tmp_path, bot_name="봇",
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
            DirectInvoker(runner, engine), model="m", effort="low", workdir=tmp_path, bot_name="봇",
        )
        outcome = analyzer.analyze_channel("2026-09-14", "공지", "기록", [])
        assert outcome.kind is OutcomeKind.UNKNOWN

    def test_json이_아닌_응답은_판정_불가다(self, tmp_path: Path) -> None:
        engine = FakeEngine()
        runner = make_runner(json.dumps({"result": "그냥 문장일 뿐이다"}))
        analyzer = ProposalAnalyzer(
            DirectInvoker(runner, engine), model="m", effort="low", workdir=tmp_path, bot_name="봇",
        )
        outcome = analyzer.analyze_channel("2026-09-14", "공지", "기록", [])
        assert outcome.kind is OutcomeKind.UNKNOWN


class TestProposalBuilder:
    def test_여러_채널_결과를_하나의_제안으로_합친다(self, tmp_path: Path) -> None:
        engine = FakeEngine()
        runner = make_runner(analysis_stdout(channel_facts=["사실 A"]))
        analyzer = ProposalAnalyzer(
            DirectInvoker(runner, engine), model="m", effort="low", workdir=tmp_path, bot_name="봇",
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
            DirectInvoker(runner, engine), model="m", effort="low", workdir=tmp_path, bot_name="봇",
        )
        builder = ProposalBuilder(analyzer)
        proposal = builder.build("2026-09-14", {"공지": "기록1"})
        assert proposal.channel_knowledge == {}
        assert "공지" in proposal.note


class TestAnalyzer가_디코더를_쓰는가:
    """디코더를 만든 것과 analyzer 가 그것을 쓰는 것은 다르다.
    여기 없으면 디코더 시험 22건이 전부 통과하는 채로 배치는 옛 파서로 돈다.
    """

    def _분석기(self, stdout: str, tmp_path: Path) -> ProposalAnalyzer:
        return ProposalAnalyzer(
            DirectInvoker(make_runner(json.dumps({"result": stdout})), FakeEngine()),
            model="m", effort="low", workdir=tmp_path, bot_name="봇",
        )

    def test_앞뒤에_설명이_붙어도_제안을_읽는다(self, tmp_path: Path) -> None:
        제안 = json.dumps({"writing_style": ["짧게"], "channel_facts": [],
                          "corrections": [], "note": ""}, ensure_ascii=False)
        본문 = f"분석 결과입니다.\n\n{제안}\n\n이상입니다."
        outcome = self._분석기(본문, tmp_path).analyze_channel("2026-09-16", "공지", "기록", [])
        assert outcome.is_found
        assert outcome.value().writing_style == ("짧게",)

    def test_배열_자리에_문자열이_오면_판정_불가다(self, tmp_path: Path) -> None:
        본문 = '{"writing_style": "짧게 써라", "channel_facts": [], "corrections": [], "note": ""}'
        outcome = self._분석기(본문, tmp_path).analyze_channel("2026-09-16", "공지", "기록", [])
        assert outcome.kind is OutcomeKind.UNKNOWN


class Test학습도_같은_호출부품을_쓴다:
    """실행기를 직접 부르면 폴백이 설정돼 있어도 FallbackEngine.run() 이 안
    불린다. 한도 소진 때 전환·probe·상태 기록이 통째로 건너뛰어진다. 학습
    배치가 정확히 그 경로였다(sca-dyb.9).
    """

    def test_분석은_배치로_표시된다(self, tmp_path: Path) -> None:
        """사람이 안 기다리는 배치가 폴백 복구 프로브를 대신 쓰면 직후의
        사람 요청이 주기 내내 복구 혜택을 못 받는다.
        """
        from slack_cli_agent.engine.base import CallOrigin

        받은: list[object] = []

        class 기록실행부품:
            def invoke(self, request, origin=CallOrigin.INTERACTIVE):
                받은.append(origin)
                return EngineResponse(
                    ok=True, session_id=None, model_actual=None,
                    body='{"writing_style": [], "channel_facts": [], "corrections": [], "note": ""}',
                    elapsed=0.0, turns=None, usage=None,
                )

        analyzer = ProposalAnalyzer(
            기록실행부품(), model="m", effort="low", workdir=tmp_path, bot_name="봇",
        )
        outcome = analyzer.analyze_channel("2026-09-16", "공지", "기록", [])

        assert outcome.is_found, "응답을 못 읽으면 어느 경로로 갔는지도 못 믿는다"
        assert 받은 == [CallOrigin.BACKGROUND]

    def test_분석이_실행기를_직접_부르지_않는다(self) -> None:
        import inspect

        본문 = inspect.getsource(ProposalAnalyzer)
        assert "_runner.run(" not in 본문
        assert "_invoker.invoke(" in 본문

    def test_전환된_상태에서는_2차_엔진이_분석을_맡는다(self, tmp_path: Path) -> None:
        from slack_cli_agent.engine.runner import FallbackEngine, FallbackInvoker
        from slack_cli_agent.engine.switcher import EngineSwitcher

        primary, secondary = FakeEngine(), FakeEngine()
        secondary.name = "2차"
        runner = make_runner(analysis_stdout(channel_facts=["사실"]))
        switcher = EngineSwitcher(tmp_path / "engine_state.json")
        switcher.begin_switch("weekly limit", engine_name="2차")
        switcher.approve()
        invoker = FallbackInvoker(FallbackEngine(primary, secondary, switcher, runner))

        analyzer = ProposalAnalyzer(
            invoker, model="m", effort="low", workdir=tmp_path, bot_name="봇",
        )
        outcome = analyzer.analyze_channel("2026-09-16", "공지", "기록", [])

        assert outcome.is_found
        assert len(secondary.built_requests) == 1
        assert primary.built_requests == []
