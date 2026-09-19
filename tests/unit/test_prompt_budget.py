"""지침 전체에 바이트 상한을 두고, 넘으면 선택 지식부터 뺀다 (sca-ygd 3단계).

codex 와 gemini 는 재개 턴마다 지침 전체를 user prompt 에 다시 싣는다. 상한이
없으면 지식 파일이 늘어난 만큼 매 턴 비용이 는다. 상한은 관측 분포가 아니라
docs/지침-예산.md 의 계약에서 나온다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import test_prompt
from test_prompt import OWNER, base_sections, write

from slack_cli_agent.prompt.composer import SystemPromptComposer
from slack_cli_agent.prompt.knowledge import BUDGET_OMITTED_HEADER, KnowledgeLoader
from slack_cli_agent.prompt.sections import CompositionContext

# fixture 를 이 모듈에서도 쓸 수 있게 다시 내건다. from-import 로 받으면 같은
# 이름의 시험 파라미터가 재정의로 잡힌다.
library = test_prompt.library
prompts_dir = test_prompt.prompts_dir


@pytest.fixture
def 지식폴더(tmp_path: Path) -> Path:
    폴더 = tmp_path / "persona" / "knowledge"
    폴더.mkdir(parents=True)
    write(tmp_path / "persona" / "PERSONA.md", "페르소나 본문")
    return 폴더


def 로더(tmp_path: Path) -> KnowledgeLoader:
    return KnowledgeLoader(tmp_path / "persona" / "PERSONA.md", tmp_path / "persona" / "knowledge")


def ctx() -> CompositionContext:
    return CompositionContext(principal=OWNER, channel_mode="private")


def 조립(tmp_path: Path, library, 예산: int) -> str:
    return SystemPromptComposer(
        library, 로더(tmp_path), base_sections(), budget_bytes=예산,
    ).compose(ctx())


class Test예산_안이면_그대로_싣는다:
    def test_지식이_전부_실린다(self, tmp_path: Path, 지식폴더: Path, library) -> None:
        write(지식폴더 / "_가.md", "가나다")
        write(지식폴더 / "_나.md", "라마바")
        본문 = 조립(tmp_path, library, 예산=100_000)
        assert "가나다" in 본문
        assert "라마바" in 본문
        assert BUDGET_OMITTED_HEADER not in 본문


class Test예산을_넘으면_선택_지식부터_뺀다:
    def test_큰_파일이_통째로_빠진다(self, tmp_path: Path, 지식폴더: Path, library) -> None:
        write(지식폴더 / "_작다.md", "짧은 지식")
        write(지식폴더 / "_크다.md", "긴 지식 " + "가" * 4000)
        본문 = 조립(tmp_path, library, 예산=4000)
        assert "짧은 지식" in 본문
        assert "가" * 4000 not in 본문

    def test_문자_중간에서_자르지_않는다(self, tmp_path: Path, 지식폴더: Path, library) -> None:
        """지식 파일은 의미 단위다. 중간을 끊으면 조건과 예외가 떨어져 나가
        지침의 뜻이 바뀐다."""
        write(지식폴더 / "_크다.md", "머리말 " + "가" * 4000 + " 꼬리말")
        본문 = 조립(tmp_path, library, 예산=4000)
        assert "머리말" not in 본문

    def test_뺀_파일을_예산_헤더로_알린다(self, tmp_path: Path, 지식폴더: Path, library) -> None:
        write(지식폴더 / "_크다.md", "가" * 4000)
        본문 = 조립(tmp_path, library, 예산=3000)
        assert BUDGET_OMITTED_HEADER in 본문
        assert "크다" in 본문

    def test_낱말_불일치와_예산_초과를_가른다(self, tmp_path: Path, 지식폴더: Path, library) -> None:
        """안 맞아서 안 실은 것과 자리가 없어 못 실은 것은 다른 사실이다.
        같은 헤더로 알리면 모델에게 거짓을 말하는 것이 된다."""
        write(지식폴더 / "_낱말.md", "<!-- when: 없는낱말 -->\n안 맞는 지식")
        write(지식폴더 / "_크다.md", "가" * 4000)
        본문 = 조립(tmp_path, library, 예산=3000)
        assert "지금 싣지 않은 주제별 지식" in 본문
        assert BUDGET_OMITTED_HEADER in 본문

    def test_페르소나는_예산_때문에_빠지지_않는다(self, tmp_path: Path, 지식폴더: Path, library) -> None:
        """정체성과 안전 지침은 자르지 않는다. 그것까지 넘으면 구성 오류이지
        런타임에 조용히 뺄 일이 아니다."""
        write(지식폴더 / "_크다.md", "가" * 4000)
        본문 = 조립(tmp_path, library, 예산=10)
        assert "페르소나 본문" in 본문


class Test운영_조립에도_예산이_걸린다:
    """상한을 만들어 두고 조립에 안 걸면 실제 요청은 그대로 커진다
    (sca-ygd)."""

    def test_기본_예산이_설정에서_온다(self) -> None:
        from slack_cli_agent.config.settings import RuntimeSettings
        from slack_cli_agent.prompt.composer import DEFAULT_SYSTEM_PROMPT_BUDGET_BYTES

        assert RuntimeSettings().system_prompt_budget_bytes == DEFAULT_SYSTEM_PROMPT_BUDGET_BYTES

    def test_조립기가_그_값을_받는다(self, tmp_path: Path, 지식폴더: Path, library) -> None:
        """0 이하는 상한 없음으로 읽는다. 끄는 수단이 있어야 사고 때 되돌린다."""
        composer = SystemPromptComposer(library, 로더(tmp_path), base_sections(), budget_bytes=0)
        write(지식폴더 / "_크다.md", "가" * 4000)
        assert "가" * 4000 in composer.compose(ctx())
