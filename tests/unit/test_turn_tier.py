"""매 턴 권한 등급 고지 (A12).

마메치 2026-10-14 사례 : 모델은 이 턴이 어느 등급인지 모르고 붙은 도구만 보고
추론해, 권한 밖인 수단을 "수단이 없다" 고 썼다. 마메치는 등급을 매 턴 알리고
(bot.py turn_tier_note) 감사 행에 남긴다 (bot.py:7967, authz.tier). 구치파치는
둘 다 없었다.
"""

from __future__ import annotations

from typing import Any

from test_pipeline import (
    FakeComposer,
    FakeEngine,
    _도구정책,
    build_pipeline,
    fail_response,
    make_ctx,
    ok_response,
)

from slack_cli_agent.auth.policy import turn_tier
from slack_cli_agent.auth.principal import Principal, TrustLevel, TurnTier
from slack_cli_agent.config.channel import ChannelConfig
from slack_cli_agent.engine.capability import EngineCapabilities, ExecutionIsolation
from slack_cli_agent.prompt.sections import TIER_NOTES, CompositionContext, TurnTierSection


def _principal(user: str = "U1", trust: TrustLevel = TrustLevel.GENERAL) -> Principal:
    return Principal(user_id=user, channel="C1", trust=trust, is_direct_message=False)


def _channel(**fields: object) -> ChannelConfig:
    return ChannelConfig(channel_id="C1", name="테스트", **fields)  # type: ignore[arg-type]


class Test등급_판정:
    def test_등급은_마메치와_같은_9개다(self) -> None:
        assert {t.value for t in TurnTier} == {
            "readonly", "owner_trusted", "owner", "delegated", "bot",
            "operator_full", "operator", "airflow", "read",
        }

    def test_조회_전용_흐름은_누가_불러도_readonly다(self) -> None:
        owner = _principal("UOWNER", TrustLevel.OWNER)
        assert turn_tier(owner, None, full_authority=True, readonly=True) is TurnTier.READONLY

    def test_소유자는_전권_자리에서_owner_trusted다(self) -> None:
        owner = _principal("UOWNER", TrustLevel.OWNER)
        assert turn_tier(owner, None, full_authority=True) is TurnTier.OWNER_TRUSTED
        assert turn_tier(owner, None, full_authority=False) is TurnTier.OWNER

    def test_위임은_봇이어도_delegated다(self) -> None:
        assert turn_tier(_principal(), None, full_authority=False, is_bot=True,
                         delegated=True) is TurnTier.DELEGATED

    def test_봇은_operators_에_있어도_bot이다(self) -> None:
        config = _channel(operators=frozenset({"*"}))
        assert turn_tier(_principal(), config, full_authority=False, is_bot=True) is TurnTier.BOT

    def test_operators_는_operator_full_여부로_갈린다(self) -> None:
        assert turn_tier(_principal(), _channel(operators=frozenset({"U1"})),
                         full_authority=False) is TurnTier.OPERATOR
        assert turn_tier(_principal(), _channel(operators=frozenset({"*"}), operator_full=True),
                         full_authority=False) is TurnTier.OPERATOR_FULL

    def test_airflow_admins_는_airflow다(self) -> None:
        config = _channel(airflow_admins=frozenset({"U1"}))
        assert turn_tier(_principal(), config, full_authority=False) is TurnTier.AIRFLOW

    def test_그_밖의_비소유자는_read다(self) -> None:
        assert turn_tier(_principal(), _channel(), full_authority=False) is TurnTier.READ
        assert turn_tier(_principal(), None, full_authority=False) is TurnTier.READ


class Test등급_고지_절:
    def _ctx(self, tier: str) -> CompositionContext:
        return CompositionContext(principal=_principal(), turn_tier=tier)

    def test_모든_등급에_고지_문구가_있다(self) -> None:
        assert set(TIER_NOTES) == set(TurnTier)

    def test_조회_전용_턴은_수단이_없다고_쓰지_말라고_적는다(self) -> None:
        section = TurnTierSection()
        ctx = CompositionContext(principal=_principal(), turn_tier="read", turn_reads_only=True)
        assert section.applies_to(ctx)
        text = section.render(ctx)
        assert text.startswith("[이 턴 권한 : 비소유자 턴. 이 턴은 조회만 한다")
        assert "'수단이 없다'고 쓰지 않는다" in text
        assert "'이 턴 권한 밖'" in text

    def test_등급을_모르면_고지하지_않는다(self) -> None:
        assert not TurnTierSection().applies_to(self._ctx(""))


class Test파이프라인:
    def test_비소유자_턴은_지침과_감사_행에_read_등급이_남는다(self) -> None:
        composer = FakeComposer()
        pipeline, parts = build_pipeline(responses=[ok_response()], composer=composer)

        pipeline.handle(make_ctx(user="U1"))

        assert composer.contexts[0].turn_tier == "read"
        assert parts["audit"].records[0]["tier"] == "read"

    def test_operators_채널의_비소유자는_operator_등급이다(self) -> None:
        composer = FakeComposer()
        pipeline, parts = build_pipeline(
            responses=[ok_response()], composer=composer,
            channels={"C1": _channel(operators=frozenset({"U1"}))},
        )

        pipeline.handle(make_ctx(user="U1"))

        assert composer.contexts[0].turn_tier == "operator"
        assert parts["audit"].records[0]["tier"] == "operator"

    def test_소유자_DM은_owner_trusted다(self) -> None:
        pipeline, parts = build_pipeline(responses=[ok_response()])

        pipeline.handle(make_ctx(user="UOWNER", channel="D1", thread_ts="1700000001.000100"))

        assert parts["audit"].records[0]["tier"] == "owner_trusted"


def _render(**fields: Any) -> str:
    ctx = CompositionContext(principal=_principal(), **fields)
    return TurnTierSection().render(ctx)


class Test실제_도구로_고지한다:
    """검토 결함 (A12 리뷰). 등급 문구가 엔진과 실제 부여 도구를 보지 않았다."""

    def test_조회만_하는_턴은_실행을_권한_밖으로_적는다(self) -> None:
        text = _render(turn_tier="operator_full", turn_reads_only=True)
        assert "이 턴은 조회만 한다" in text
        assert "제한 없는 셸" not in text

    def test_airflow_래퍼가_없으면_Airflow_제어를_약속하지_않는다(self) -> None:
        text = _render(turn_tier="airflow", turn_reads_only=True)
        assert "Airflow 제어" not in text

    def test_read_턴이_범위_부여를_받으면_그_도구를_적는다(self) -> None:
        text = _render(turn_tier="read", turn_acting_tools=("Bash(ops_ctl.sh:*)",))
        assert "Bash(ops_ctl.sh:*)" in text
        assert "실행 도구는 없다" not in text

    def test_실행_도구가_많으면_줄여_적는다(self) -> None:
        names = tuple(f"mcp__x__t{i}" for i in range(10))
        text = _render(turn_tier="owner", turn_acting_tools=names)
        assert "mcp__x__t7" in text and "mcp__x__t8" not in text
        assert "외 2개" in text


class Test파이프라인_경로:
    def test_새_세션_재시도도_등급을_고지한다(self) -> None:
        """재시도가 tier 없이 지침을 다시 조립해 고지가 빠지고 감사 행에만 남았다."""
        composer = FakeComposer()
        pipeline, parts = build_pipeline(
            responses=[fail_response("nonzero_exit"), ok_response()], composer=composer,
        )

        pipeline.handle(make_ctx(user="U1"))

        assert len(parts["runner"].calls) == 2
        assert [c.turn_tier for c in composer.contexts] == ["read", "read"]
        assert parts["runner"].calls[1].turn_tier == "read"
        assert parts["audit"].records[0]["tier"] == "read"

    def test_범위_부여를_받은_비소유자는_그_도구가_고지된다(self) -> None:
        composer = FakeComposer()
        pipeline, _ = build_pipeline(
            responses=[ok_response()], composer=composer,
            tool_policy=_도구정책(("Read", "Bash(ops_ctl.sh:*)")),
        )

        pipeline.handle(make_ctx(user="U1"))

        assert composer.contexts[0].turn_reads_only is False
        assert composer.contexts[0].turn_acting_tools == ("Bash(ops_ctl.sh:*)",)

    def test_읽기전용_샌드박스_엔진은_operator_턴도_조회만으로_고지한다(self, monkeypatch) -> None:
        """codex 는 비소유자 턴을 늘 read-only 로 돌린다 (_non_owner_reads_only)."""
        monkeypatch.setattr(
            FakeEngine, "capabilities_for",
            lambda self, request: EngineCapabilities(
                execution_isolation=ExecutionIsolation.READONLY_SANDBOX,
            ),
        )
        composer = FakeComposer()
        pipeline, _ = build_pipeline(
            responses=[ok_response()], composer=composer,
            channels={"C1": _channel(operators=frozenset({"U1"}), operator_full=True)},
            tool_policy=_도구정책(("Read", "Bash", "Edit")),
        )

        pipeline.handle(make_ctx(user="U1"))

        assert composer.contexts[0].turn_tier == "operator_full"
        assert composer.contexts[0].turn_reads_only is True
        assert composer.contexts[0].turn_acting_tools == ()


class Test감시와_리뷰:
    def _app(self, tmp_path, monkeypatch):
        from test_application import FakeSlackClient, _프롬프트조립대역, write_profile

        from slack_cli_agent.core.application import Application
        from slack_cli_agent.engine.runner import EngineRunner

        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        composer = _프롬프트조립대역()
        app._composer = lambda: composer  # type: ignore[method-assign]
        sent: list[Any] = []
        monkeypatch.setattr(EngineRunner, "run", lambda self, engine, request: sent.append(request))
        return app, composer, sent

    def test_감시_확인_턴은_readonly_등급을_고지하고_요청에_남긴다(self, tmp_path, monkeypatch) -> None:
        from slack_cli_agent.reliability.watchjobs import WatchJob
        from slack_cli_agent.reliability.watchresult import WatchOutcome

        app, composer, sent = self._app(tmp_path, monkeypatch)
        app._watch_run_check(WatchJob(
            id=1, channel="C1", thread_ts="1.1", condition="배포 확인",
            created_at=0.0, last_run=None, trust=TrustLevel.GENERAL,
        ), WatchOutcome.UNKNOWN)

        assert composer.받은맥락[0].turn_tier == "readonly"
        assert composer.받은맥락[0].turn_reads_only is True
        assert sent[0].turn_tier == "readonly"

    def test_리뷰_턴은_소유자_등급을_고지하고_요청에_남긴다(self, tmp_path, monkeypatch) -> None:
        app, composer, sent = self._app(tmp_path, monkeypatch)
        caller = app._review_engine(postmortem=True)

        caller.run("부검 대상", None, False)

        assert composer.받은맥락[0].turn_tier in {"owner", "owner_trusted"}
        assert sent[0].turn_tier == composer.받은맥락[0].turn_tier


class Test보장_기록:
    def test_보장_기록에_등급이_남는다(self) -> None:
        from pathlib import Path

        from slack_cli_agent.engine.base import EngineRequest
        from slack_cli_agent.engine.runner import EngineRunner

        records: list[dict[str, Any]] = []

        class _Audit:
            def record(self, kind: str, **fields: Any) -> None:
                records.append(fields)

        runner = EngineRunner.__new__(EngineRunner)
        runner._audit = _Audit()  # type: ignore[attr-defined]
        request = EngineRequest(
            prompt="p", system_prompt="", session_id=None, resume=False, model=None,
            effort="", workdir=Path("/tmp"), turn_tier="readonly",
        )
        runner._record_capabilities(FakeEngine(), request, EngineCapabilities())

        assert records[0]["tier"] == "readonly"
