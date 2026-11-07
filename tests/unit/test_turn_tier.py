"""매 턴 권한 등급 고지 (A12).

마메치 2026-10-14 사례 : 모델은 이 턴이 어느 등급인지 모르고 붙은 도구만 보고
추론해, 권한 밖인 수단을 "수단이 없다" 고 썼다. 마메치는 등급을 매 턴 알리고
(bot.py turn_tier_note) 감사 행에 남긴다 (bot.py:7967, authz.tier). 구치파치는
둘 다 없었다.
"""

from __future__ import annotations

from test_pipeline import FakeComposer, build_pipeline, make_ctx, ok_response

from slack_cli_agent.auth.policy import turn_tier
from slack_cli_agent.auth.principal import Principal, TrustLevel, TurnTier
from slack_cli_agent.config.channel import ChannelConfig
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
        ctx = self._ctx("read")
        assert section.applies_to(ctx)
        text = section.render(ctx)
        assert text.startswith("[이 턴 권한 : 조회 전용 턴")
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
