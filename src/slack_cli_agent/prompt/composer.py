"""시스템 프롬프트 조립.

원본 build_system_prompt 는 12단계 분기를 한 함수에 담고 있었다. 여기서는
그 단계들을 PromptSection 목록으로 등록 순서대로 적용한다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from .knowledge import KnowledgeLoader
from .library import PromptLibrary
from .sections import AuthoritySection, CompositionContext, PromptSection, SlackFormatSection


class SystemPromptComposer:
    """조각을 순서대로 적용해 시스템 프롬프트 한 덩어리를 만든다."""

    def __init__(
        self,
        library: PromptLibrary,
        knowledge: KnowledgeLoader,
        sections: Sequence[PromptSection],
    ) -> None:
        self._library = library
        self._knowledge = knowledge
        self._sections = tuple(sections)

    def compose(self, ctx: CompositionContext) -> str:
        """등록 순서대로 적용 가능한 조각만 이어붙인다.

        SlackFormatSection 은 다른 조각과 달리 출력을 그대로 이어붙이지
        않는다. 본문 어디에 있든 <<SLACK_FORMAT>> 자리표를 그 값으로 채우는
        치환이라, 다른 조각이 그 자리표를 남긴 뒤에 실행돼야 하기 때문이다.
        전체를 이어붙인 뒤 한 번에 치환해도 결과는 같다 — 뒤에 오는 조각은
        이 자리표를 담지 않는다.

        NEVER_DISCLOSE 자리표도 같은 이유로 마지막에 지운다.
        AuthoritySection 이 최고권한/구조공개 안내를 붙인 뒤에, 앞선
        ChannelModeSection·OwnerNoteSection 이 남겨 둔 자리표를 지운다.
        """
        full_ctx = replace(ctx, library=self._library, knowledge=self._knowledge)
        parts: list[str] = []
        format_value = ""
        for section in self._sections:
            if not section.applies_to(full_ctx):
                continue
            rendered = section.render(full_ctx)
            if isinstance(section, SlackFormatSection):
                format_value = rendered
                continue
            parts.append(rendered)
        text = "".join(parts)
        if format_value:
            text = text.replace(SlackFormatSection.PLACEHOLDER, format_value)
        if full_ctx.may_disclose_mechanism:
            text = text.replace(AuthoritySection.FULL_PLACEHOLDER, "")
        return text
