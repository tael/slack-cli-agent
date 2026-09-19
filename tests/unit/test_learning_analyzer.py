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
from typing import Any

from engine_support import named
from test_engine import 통과정책

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.engine.base import Engine, EngineRequest, EngineResponse
from slack_cli_agent.engine.runner import DirectInvoker, EngineInvoker, EngineRunner
from slack_cli_agent.learning.analyzer import ProposalAnalyzer, ProposalBuilder
from slack_cli_agent.learning.progress import FailureKind
from slack_cli_agent.learning.proposal import LearningProposal


class FakeEngine(Engine):
    """subprocess 를 실제로 부르지 않고 정해 둔 stdout 을 그대로 파싱하는 엔진."""

    name = "fake"

    def __init__(self, failure_reason: str = "nonzero_exit") -> None:
        # Engine.__init__ 은 profile/settings 를 요구하지만 이 시험은 안 쓴다.
        self.profile = None  # type: ignore[assignment]
        self.settings = None  # type: ignore[assignment]
        self.built_requests: list[EngineRequest] = []
        self._failure_reason = failure_reason

    def build_command(self, request: EngineRequest) -> list[str]:
        self.built_requests.append(request)
        return ["fake"]

    def parse(self, stdout: str, stderr: str, returncode: int) -> EngineResponse:
        if returncode != 0:
            return EngineResponse(
                ok=False, body="실패", session_id=None, model_actual=None,
                elapsed=0.0, turns=None, usage=None, raw={}, failure_reason=self._failure_reason,
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


class 기록감사:
    """운영과 같이 완화를 받아 줄 수 있는 기록기다. 없으면 완화가 거부돼
    학습 배치가 claude 밖의 엔진에서 통째로 막힌다."""

    def __init__(self) -> None:
        self.기록: list[tuple[str, dict[str, Any]]] = []

    def record(self, kind: str, *, channel: str = "", thread_ts: str = "", **fields: Any) -> None:
        self.기록.append((kind, fields))


def make_runner(stdout: str, returncode: int = 0) -> EngineRunner:
    def fake_subprocess(cmd, cwd, timeout, env=None):
        return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)

    settings = RuntimeSettings(request_timeout_sec=10)
    # 운영 조립과 같이 감사 기록기를 준다. 없으면 완화가 거부돼 학습 배치가
    # claude 밖의 엔진에서 통째로 막힌다.
    return EngineRunner(
        settings, subprocess_runner=fake_subprocess, environment_policy=통과정책(),
        audit=기록감사(),
    )


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
        assert outcome.ok
        result = outcome.result
        assert result is not None
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

    def _요청(self):
        engine = FakeEngine()
        runner = make_runner(analysis_stdout())
        analyzer = ProposalAnalyzer(
            DirectInvoker(runner, engine), model="m", effort="low",
            workdir=Path("/tmp"), bot_name="봇",
        )
        analyzer.analyze_channel("2026-09-14", "공지", "기록 본문", [])
        return engine.built_requests[0]

    def test_도구를_하나도_주지_않는다(self) -> None:
        """기록은 프롬프트에 이미 들어간다.

        이름이 빈 것으로는 판정이 안 된다 - 예전 시험이 그것만 봐서, 도구를
        안 준다고 적어 놓고 전체 도구가 열린 채 도는 것을 못 잡았다 (sca-0a7).
        """
        from slack_cli_agent.engine.tool_selection import ToolAccess

        assert self._요청().tools.access is ToolAccess.FORBIDDEN

    def test_그_금지를_엔진에_요구한다(self) -> None:
        """claude 는 강제하고, 못 하는 엔진은 강등이 감사에 남는다. 야간
        배치는 아무도 기다리지 않으므로 막지 않고 기록으로 남긴다."""
        from slack_cli_agent.engine.capability import TOOL_AXIS, ToolRestriction

        요구 = self._요청().requirements
        assert 요구.tool_restriction is ToolRestriction.ALL_FORBIDDEN
        assert 요구.downgradable_axes == frozenset({TOOL_AXIS})

    def test_엔진_실행_실패는_실행_실패로_갈린다(self, tmp_path: Path) -> None:
        engine = FakeEngine()
        runner = make_runner("실패", returncode=1)
        analyzer = ProposalAnalyzer(
            DirectInvoker(runner, engine), model="m", effort="low", workdir=tmp_path, bot_name="봇",
        )
        분석 = analyzer.analyze_channel("2026-09-14", "공지", "기록", [])
        assert 분석.ok is False
        assert 분석.failure is not None
        assert 분석.failure.kind is FailureKind.ENGINE_FAILED
        assert 분석.failure.channel == "공지"

    def test_한도_소진은_그_종류를_잃지_않는다(self, tmp_path: Path) -> None:
        """엔진 계층이 이미 구조화해 둔 값이다. 문자열로 평탄화하면 배치가
        승인만 하면 풀리는 건과 그냥 실패를 구분하지 못한다(sca-b4o).
        """
        engine = FakeEngine(failure_reason="usage_limit")
        runner = make_runner("한도", returncode=1)
        analyzer = ProposalAnalyzer(
            DirectInvoker(runner, engine), model="m", effort="low", workdir=tmp_path, bot_name="봇",
        )
        분석 = analyzer.analyze_channel("2026-09-14", "공지", "기록", [])
        assert 분석.failure is not None
        assert 분석.failure.kind is FailureKind.USAGE_LIMIT

    def test_json이_아닌_응답은_읽기_실패로_갈린다(self, tmp_path: Path) -> None:
        engine = FakeEngine()
        runner = make_runner(json.dumps({"result": "그냥 문장일 뿐이다"}))
        analyzer = ProposalAnalyzer(
            DirectInvoker(runner, engine), model="m", effort="low", workdir=tmp_path, bot_name="봇",
        )
        분석 = analyzer.analyze_channel("2026-09-14", "공지", "기록", [])
        assert 분석.failure is not None
        assert 분석.failure.kind is FailureKind.DECODE_FAILED


class 채널별분석기:
    """지정한 채널만 성공하고 나머지는 실패하는 대역."""

    def __init__(self, 성공: dict[str, str]) -> None:
        self._성공 = 성공

    def analyze_channel(self, day, channel_name, archive_text, reactions):
        from slack_cli_agent.learning.analyzer import ChannelAnalysis
        from slack_cli_agent.learning.decoder import ChannelAnalysisResult
        from slack_cli_agent.learning.progress import ChannelFailure, FailureKind

        if channel_name in self._성공:
            return ChannelAnalysis.succeeded(ChannelAnalysisResult(channel_facts=("사실 A",)))
        return ChannelAnalysis.failed(
            ChannelFailure(channel=channel_name, kind=FailureKind.ENGINE_FAILED, detail="사유"))


class TestProposalBuilder:
    def test_여러_채널_결과를_하나의_제안으로_합친다(self, tmp_path: Path) -> None:
        engine = FakeEngine()
        runner = make_runner(analysis_stdout(channel_facts=["사실 A"]))
        analyzer = ProposalAnalyzer(
            DirectInvoker(runner, engine), model="m", effort="low", workdir=tmp_path, bot_name="봇",
        )
        builder = ProposalBuilder(analyzer)
        결과 = builder.build("2026-09-14", {"공지": "기록1", "잡담": "기록2"})
        proposal = LearningProposal.from_results("2026-09-14", 결과.results)
        assert proposal.day == "2026-09-14"
        assert proposal.channel_knowledge["공지"] == ("사실 A",)
        assert proposal.channel_knowledge["잡담"] == ("사실 A",)

    def test_성공한_채널과_실패한_채널이_갈려서_나온다(self, tmp_path: Path) -> None:
        """실패를 note 문자열로 합치면 배치가 그날을 완료로 찍어도 되는지를
        판단할 근거가 사라진다(sca-b4o).
        """
        analyzer = 채널별분석기({"공지": "기록1"})
        결과 = ProposalBuilder(analyzer).build("2026-09-14", {"공지": "기록1", "잡담": "기록2"})
        assert set(결과.results) == {"공지"}
        assert [f.channel for f in 결과.failures] == ["잡담"]

    def test_운영_실패는_제안의_note에_안_섞인다(self, tmp_path: Path) -> None:
        analyzer = 채널별분석기({})
        결과 = ProposalBuilder(analyzer).build("2026-09-14", {"잡담": "기록2"})
        proposal = LearningProposal.from_results("2026-09-14", 결과.results)
        assert proposal.note == ""


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
        assert outcome.ok
        assert outcome.result is not None
        assert outcome.result.writing_style == ("짧게",)

    def test_배열_자리에_문자열이_오면_판정_불가다(self, tmp_path: Path) -> None:
        본문 = '{"writing_style": "짧게 써라", "channel_facts": [], "corrections": [], "note": ""}'
        outcome = self._분석기(본문, tmp_path).analyze_channel("2026-09-16", "공지", "기록", [])
        assert outcome.failure is not None


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

        class 기록실행부품(EngineInvoker):
            def invoke(self, request: EngineRequest,
                       origin: CallOrigin = CallOrigin.INTERACTIVE) -> EngineResponse:
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

        assert outcome.ok, "응답을 못 읽으면 어느 경로로 갔는지도 못 믿는다"
        assert 받은 == [CallOrigin.BACKGROUND]

    def test_분석이_실행기를_직접_부르지_않는다(self) -> None:
        import inspect

        본문 = inspect.getsource(ProposalAnalyzer)
        assert "_runner.run(" not in 본문
        assert "_invoker.invoke(" in 본문

    def test_전환된_상태에서는_2차_엔진이_분석을_맡는다(self, tmp_path: Path) -> None:
        from slack_cli_agent.engine.runner import FallbackEngine, FallbackInvoker
        from slack_cli_agent.engine.switcher import EngineSwitcher

        primary, secondary = FakeEngine(), named(FakeEngine, "2차")()
        runner = make_runner(analysis_stdout(channel_facts=["사실"]))
        switcher = EngineSwitcher(tmp_path / "engine_state.json")
        switcher.begin_switch("weekly limit", engine_name="2차")
        switcher.approve()
        invoker = FallbackInvoker(FallbackEngine(primary, secondary, switcher, runner))

        analyzer = ProposalAnalyzer(
            invoker, model="m", effort="low", workdir=tmp_path, bot_name="봇",
        )
        outcome = analyzer.analyze_channel("2026-09-16", "공지", "기록", [])

        assert outcome.ok
        assert len(secondary.built_requests) == 1
        assert primary.built_requests == []
