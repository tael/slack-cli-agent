"""operator 화자 지침 분기와 구조 공개 채널 연결 (A11).

- 채널 operators 가 지목한 비소유자에게는 실행 도구가 붙는다 (A9). 그 턴에
  NON_OWNER_NOTE 를 실으면 지침이 읽기 전용이라고 말해 할 수 있는 일을 못 한다고
  답한다. 마메치 build_system_prompt (bot.py speaker_note) 와 같이 OPERATOR_NOTE,
  operator_full 채널이면 OPERATOR_FULL_NOTE 를 싣는다. 부검·디버그·서식 점검은
  실행 도구가 빠지므로 읽기 전용 지침을 유지한다.
- 채널 disclose_mechanism 은 AccessPolicy.may_disclose_mechanism 으로 판정되지만
  파이프라인이 그 값을 CompositionContext.mechanism_open 에 넘기지 않아 구조 설명
  지침(MECHANISM_NOTE)이 비소유자 턴에 실리지 않았다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_pipeline import build_pipeline, make_ctx, ok_response

from slack_cli_agent.auth.principal import Principal, TrustLevel
from slack_cli_agent.config.channel import ChannelConfig
from slack_cli_agent.prompt.composer import SystemPromptComposer
from slack_cli_agent.prompt.knowledge import KnowledgeLoader
from slack_cli_agent.prompt.library import PromptLibrary
from slack_cli_agent.prompt.sections import (
    AuthoritySection,
    ChannelModeSection,
    CompositionContext,
    OwnerNoteSection,
)


def _principal(trust: TrustLevel) -> Principal:
    return Principal(user_id="U1", channel="C1", trust=trust, is_direct_message=False)


def _library(tmp_path: Path) -> PromptLibrary:
    prompts = tmp_path / "prompts"
    prompts.mkdir(exist_ok=True)
    for name, body in {
        "prompt_default": "채널 프롬프트 <<NEVER_DISCLOSE>>",
        "owner_note": "소유자 안내",
        "non_owner_note": "읽기 전용 안내",
        "operator_note": "operator 안내",
        "operator_full_note": "operator 전체 안내",
        "mechanism_note": "구조공개 안내",
        "full_authority_note": "최고권한 안내",
    }.items():
        (prompts / f"{name}.md").write_text(body, encoding="utf-8")
    return PromptLibrary(prompts, placeholders={"NEVER_DISCLOSE": "[절대금지]"})


def _compose(tmp_path: Path, trust: TrustLevel, **kwargs: Any) -> str:
    persona = tmp_path / "persona"
    persona.mkdir(exist_ok=True)
    (persona / "knowledge").mkdir(exist_ok=True)
    composer = SystemPromptComposer(
        _library(tmp_path),
        KnowledgeLoader(persona / "PERSONA.md", persona / "knowledge"),
        [ChannelModeSection({}, default_prompt="PROMPT_DEFAULT"), OwnerNoteSection(), AuthoritySection()],
    )
    return composer.compose(CompositionContext(principal=_principal(trust), **kwargs))


class Test화자_지침_분기:
    def test_operator_비소유자는_operator_지침을_받는다(self, tmp_path: Path) -> None:
        text = _compose(tmp_path, TrustLevel.GENERAL, operator=True)
        assert "operator 안내" in text
        assert "읽기 전용 안내" not in text

    def test_operator_full_채널은_전체_지침을_받는다(self, tmp_path: Path) -> None:
        text = _compose(tmp_path, TrustLevel.TRUSTED, operator=True, operator_full=True)
        assert "operator 전체 안내" in text
        assert "읽기 전용 안내" not in text

    @pytest.mark.parametrize("aside", ["postmortem", "debug_trace", "format_review"])
    def test_aside_턴은_읽기_전용_지침을_유지한다(self, tmp_path: Path, aside: str) -> None:
        text = _compose(tmp_path, TrustLevel.GENERAL, operator=True, **{aside: True})
        assert "읽기 전용 안내" in text
        assert "operator" not in text

    def test_operator_가_아닌_비소유자는_읽기_전용_지침이다(self, tmp_path: Path) -> None:
        text = _compose(tmp_path, TrustLevel.GENERAL, operator_full=True)
        assert "읽기 전용 안내" in text
        assert "operator" not in text

    def test_소유자는_소유자_지침이다(self, tmp_path: Path) -> None:
        text = _compose(tmp_path, TrustLevel.OWNER, operator=True)
        assert "소유자 안내" in text
        assert "operator" not in text

    def test_구조공개_채널은_가드_자리표를_비우고_구조_안내를_싣는다(self, tmp_path: Path) -> None:
        text = _compose(tmp_path, TrustLevel.GENERAL, operator=True, mechanism_open=True)
        assert "구조공개 안내" in text
        assert "[절대금지]" not in text
        assert "<<NEVER_DISCLOSE>>" not in text


class Test동봉_기본_지침:
    @pytest.mark.parametrize("name", ["OPERATOR_NOTE", "OPERATOR_FULL_NOTE"])
    def test_상태_디렉터리에_없어도_동봉본으로_읽힌다(self, tmp_path: Path, name: str) -> None:
        empty = tmp_path / "state_prompts"
        empty.mkdir()
        assert PromptLibrary(empty).text(name).strip()


class Test파이프라인_연결:
    def _ctx(self, tmp_path: Path, config: ChannelConfig, user: str, *, disclose: bool = False) -> Any:
        pipeline, deps = build_pipeline(
            responses=[ok_response()], channels={"C1": config}, tmp_path=tmp_path,
        )
        deps["access"].disclose_channels = {"C1"} if disclose else set()
        pipeline.handle(make_ctx(channel="C1", user=user))
        return deps["composer"].contexts[0]

    def test_구조공개_판정이_맥락에_실린다(self, tmp_path: Path) -> None:
        config = ChannelConfig.from_dict("C1", {"disclose_mechanism": True})
        assert self._ctx(tmp_path, config, "U1", disclose=True).mechanism_open is True

    def test_구조공개가_아니면_꺼진다(self, tmp_path: Path) -> None:
        config = ChannelConfig.from_dict("C1", {})
        assert self._ctx(tmp_path, config, "U1").mechanism_open is False

    def test_operator_full_채널의_operator_면_켜진다(self, tmp_path: Path) -> None:
        config = ChannelConfig.from_dict("C1", {"operators": ["U1"], "operator_full": True})
        ctx = self._ctx(tmp_path, config, "U1")
        assert ctx.operator is True
        assert ctx.operator_full is True

    def test_operator_가_아니면_operator_full_도_꺼진다(self, tmp_path: Path) -> None:
        config = ChannelConfig.from_dict("C1", {"operators": ["U1"], "operator_full": True})
        ctx = self._ctx(tmp_path, config, "U2")
        assert ctx.operator is False
        assert ctx.operator_full is False
