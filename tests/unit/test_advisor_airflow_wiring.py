"""advisor·airflow 채널 설정이 엔진 환경과 시스템 지침에 닿는지 본다.

- 파이프라인이 claude 자식 환경용 engine_env 에 POST_CHANNEL,
  ADVISOR_CHANNELS, CODE_FIRST_CHANNELS 를 싣는다 (G16/G13, Stop 훅이 읽는다).
- AdvisorSection 은 aside 가 아니고, 화자가 소유자나 operator 이고, 채널에
  advisor 설정이 있을 때만 ADVISOR_NOTE 를 싣는다 (마메치 bot.py
  build_system_prompt 와 같은 조건).
- AirflowSection 은 airflow 관리자이고 aside 가 아닐 때만 AIRFLOW_NOTE 를
  싣는다. 순서는 AUTHORITY 뒤 WATCH 앞, ADVISOR 는 WATCH 뒤다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_pipeline import build_pipeline, make_ctx, ok_response

from slack_cli_agent.auth.principal import Principal, TrustLevel
from slack_cli_agent.config.channel import (
    ChannelConfig,
    advisor_enabled,
    airflow_admins,
    is_channel_operator,
)
from slack_cli_agent.prompt.composer import SystemPromptComposer
from slack_cli_agent.prompt.knowledge import KnowledgeLoader
from slack_cli_agent.prompt.library import PromptLibrary
from slack_cli_agent.prompt.sections import (
    AdvisorSection,
    AirflowSection,
    AuthoritySection,
    CompositionContext,
    WatchSection,
)

ADVISOR = {"advisor": {"executor": "claude-opus-5-5"}}


def _config(channel_id: str, **data: Any) -> ChannelConfig:
    return ChannelConfig.from_dict(channel_id, data)


class ListingChannels:
    """get 과 all 을 둘 다 갖는 채널 조회 대역 (ChannelRegistry 와 같은 면)."""

    def __init__(self, configs: dict[str, ChannelConfig]) -> None:
        self._configs = configs

    def get(self, channel_id: str) -> ChannelConfig | None:
        return self._configs.get(channel_id)

    def all(self) -> dict[str, ChannelConfig]:
        return dict(self._configs)


class Test채널_설정_판정:
    def test_advisor_는_dict_일_때만_켜진다(self) -> None:
        assert advisor_enabled(_config("C", **ADVISOR)) is True
        assert advisor_enabled(_config("C", advisor=True)) is False
        assert advisor_enabled(_config("C")) is False
        assert advisor_enabled(None) is False

    def test_airflow_admins_는_채널_명단이다(self) -> None:
        assert airflow_admins(_config("C", airflow_admins=["U1", "U2"])) == {"U1", "U2"}
        assert airflow_admins(_config("C")) == frozenset()
        assert airflow_admins(None) == frozenset()

    def test_operator_는_명단이나_별표이고_봇은_아니다(self) -> None:
        assert is_channel_operator(_config("C", operators=["U1"]), "U1") is True
        assert is_channel_operator(_config("C", operators=["U1"]), "U2") is False
        assert is_channel_operator(_config("C", operators=["*"]), "U9") is True
        assert is_channel_operator(_config("C", operators=["*"]), "B1", is_bot=True) is False
        assert is_channel_operator(None, "U1") is False


class Test엔진_환경:
    def test_advisor_채널의_요청은_ADVISOR_CHANNELS_를_싣는다(self, tmp_path: Path) -> None:
        channels = ListingChannels({
            "C1": _config("C1", **ADVISOR),
            "C2": _config("C2", **ADVISOR, code_first=True),
            "C3": _config("C3"),
        })
        pipeline, deps = build_pipeline(
            responses=[ok_response()], channels=channels, tmp_path=tmp_path,
        )
        pipeline.handle(make_ctx(channel="C1"))

        env = deps["runner"].calls[0].engine_env
        assert env["POST_CHANNEL"] == "C1"
        assert env["ADVISOR_CHANNELS"] == "C1,C2"
        assert env["CODE_FIRST_CHANNELS"] == "C2"

    def test_advisor_없는_채널은_빈_집합이다(self, tmp_path: Path) -> None:
        channels = ListingChannels({"C1": _config("C1")})
        pipeline, deps = build_pipeline(
            responses=[ok_response()], channels=channels, tmp_path=tmp_path,
        )
        pipeline.handle(make_ctx(channel="C1"))

        env = deps["runner"].calls[0].engine_env
        assert env["POST_CHANNEL"] == "C1"
        assert env["ADVISOR_CHANNELS"] == ""
        assert env["CODE_FIRST_CHANNELS"] == ""

    def test_all_이_없는_조회기도_현재_채널은_판정된다(self, tmp_path: Path) -> None:
        pipeline, deps = build_pipeline(
            responses=[ok_response()],
            channels={"C1": _config("C1", **ADVISOR, code_first=True)},
            tmp_path=tmp_path,
        )
        pipeline.handle(make_ctx(channel="C1"))

        env = deps["runner"].calls[0].engine_env
        assert env["ADVISOR_CHANNELS"] == "C1"
        assert env["CODE_FIRST_CHANNELS"] == "C1"


class Test지침_맥락_플래그:
    def _composed(self, tmp_path: Path, config: ChannelConfig, user: str) -> Any:
        pipeline, deps = build_pipeline(
            responses=[ok_response()], channels=ListingChannels({"C1": config}),
            tmp_path=tmp_path,
        )
        pipeline.handle(make_ctx(channel="C1", user=user))
        return deps["composer"].contexts[0]

    def test_airflow_관리자와_operator_와_advisor_가_맥락에_실린다(self, tmp_path: Path) -> None:
        config = _config("C1", **ADVISOR, airflow_admins=["U1"], operators=["U1"])
        ctx = self._composed(tmp_path, config, "U1")
        assert ctx.airflow_admin is True
        assert ctx.operator is True
        assert ctx.advisor_enabled is True

    def test_명단에_없는_화자는_꺼진다(self, tmp_path: Path) -> None:
        config = _config("C1", **ADVISOR, airflow_admins=["U1"], operators=["U1"])
        ctx = self._composed(tmp_path, config, "U2")
        assert ctx.airflow_admin is False
        assert ctx.operator is False
        assert ctx.advisor_enabled is True


def _principal(trust: TrustLevel) -> Principal:
    return Principal(user_id="U1", channel="C1", trust=trust, is_direct_message=False)


def _library(tmp_path: Path) -> PromptLibrary:
    prompts = tmp_path / "prompts"
    prompts.mkdir(exist_ok=True)
    (prompts / "advisor_note.md").write_text("ADVISOR 본문", encoding="utf-8")
    (prompts / "airflow_note.md").write_text("AIRFLOW 본문", encoding="utf-8")
    return PromptLibrary(prompts)


def _ctx(tmp_path: Path, trust: TrustLevel = TrustLevel.OWNER, **overrides: Any) -> CompositionContext:
    base: dict[str, Any] = {"principal": _principal(trust), "library": _library(tmp_path)}
    base.update(overrides)
    return CompositionContext(**base)


class TestAdvisorSection:
    def test_소유자_advisor_채널이면_싣는다(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path, advisor_enabled=True)
        assert AdvisorSection().applies_to(ctx) is True
        assert AdvisorSection().render(ctx) == "ADVISOR 본문"

    def test_operator_도_싣는다(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path, TrustLevel.GENERAL, advisor_enabled=True, operator=True)
        assert AdvisorSection().applies_to(ctx) is True

    def test_일반_화자는_싣지_않는다(self, tmp_path: Path) -> None:
        for trust in (TrustLevel.GENERAL, TrustLevel.TRUSTED):
            ctx = _ctx(tmp_path, trust, advisor_enabled=True)
            assert AdvisorSection().applies_to(ctx) is False

    def test_advisor_설정이_없으면_싣지_않는다(self, tmp_path: Path) -> None:
        assert AdvisorSection().applies_to(_ctx(tmp_path)) is False

    @pytest.mark.parametrize("aside", ["postmortem", "debug_trace", "format_review"])
    def test_aside_에는_싣지_않는다(self, tmp_path: Path, aside: str) -> None:
        ctx = _ctx(tmp_path, advisor_enabled=True, **{aside: True})
        assert AdvisorSection().applies_to(ctx) is False


class TestAirflowSection:
    def test_airflow_관리자면_싣는다(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path, TrustLevel.GENERAL, airflow_admin=True)
        assert AirflowSection().applies_to(ctx) is True
        assert AirflowSection().render(ctx) == "AIRFLOW 본문"

    def test_관리자가_아니면_싣지_않는다(self, tmp_path: Path) -> None:
        assert AirflowSection().applies_to(_ctx(tmp_path)) is False

    @pytest.mark.parametrize("aside", ["postmortem", "debug_trace", "format_review"])
    def test_aside_에는_싣지_않는다(self, tmp_path: Path, aside: str) -> None:
        ctx = _ctx(tmp_path, airflow_admin=True, **{aside: True})
        assert AirflowSection().applies_to(ctx) is False


class Test섹션_순서:
    def test_AUTHORITY_AIRFLOW_WATCH_ADVISOR_순서로_실린다(self, tmp_path: Path) -> None:
        library = _library(tmp_path)
        prompts = tmp_path / "prompts"
        (prompts / "full_authority_note.md").write_text("AUTHORITY 본문", encoding="utf-8")
        (prompts / "watch_note.md").write_text("WATCH 본문", encoding="utf-8")
        knowledge = KnowledgeLoader(tmp_path / "PERSONA.md", tmp_path / "knowledge")
        composer = SystemPromptComposer(
            library, knowledge,
            [AuthoritySection(), AirflowSection(), WatchSection(), AdvisorSection()],
        )
        principal = Principal(
            user_id="U1", channel="D1", trust=TrustLevel.OWNER, is_direct_message=True,
        )
        text = composer.compose(CompositionContext(
            principal=principal, airflow_admin=True, advisor_enabled=True,
        ))
        positions = [text.index(s) for s in ("AUTHORITY", "AIRFLOW", "WATCH", "ADVISOR")]
        assert positions == sorted(positions)
