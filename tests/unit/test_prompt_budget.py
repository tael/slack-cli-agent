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


class Test예산이_걸린_사실이_기록에_남는다:
    """상한이 무엇을 뺐는지 아무도 못 보면 상한을 조정할 근거가 안 생긴다.
    모델은 생략 헤더로, 운영자는 이 보고로 안다 (sca-ygd)."""

    def _보고(self, tmp_path: Path, library, 예산: int):
        return SystemPromptComposer(
            library, 로더(tmp_path), base_sections(), budget_bytes=예산,
        ).compose_with_report(ctx())

    def test_안_걸리면_뺀_것이_없다(self, tmp_path: Path, 지식폴더: Path, library) -> None:
        write(지식폴더 / "_가.md", "가나다")
        본문, 보고 = self._보고(tmp_path, library, 예산=100_000)
        assert "가나다" in 본문
        assert 보고.budget_limited is False
        assert 보고.omitted_document_count == 0
        assert 보고.budget_bytes == 100_000

    def test_뺀_건수와_바이트를_센다(self, tmp_path: Path, 지식폴더: Path, library) -> None:
        write(지식폴더 / "_크다.md", "가" * 4000)
        write(지식폴더 / "_더크다.md", "나" * 4000)
        보고 = self._보고(tmp_path, library, 예산=3000)[1]
        assert 보고.budget_limited is True
        assert 보고.omitted_document_count == 2
        assert 보고.omitted_document_bytes == 2 * len(("가" * 4000).encode("utf-8"))

    def test_자르기_전후_크기를_함께_낸다(self, tmp_path: Path, 지식폴더: Path, library) -> None:
        """이 둘이 있어야 상한이 실제로 무엇을 줄였는지 말할 수 있다."""
        write(지식폴더 / "_크다.md", "가" * 4000)
        본문, 보고 = self._보고(tmp_path, library, 예산=3000)
        assert 보고.bytes_after == len(본문.encode("utf-8"))
        assert 보고.bytes_before > 보고.bytes_after

    def test_상한이_없으면_전후가_같다(self, tmp_path: Path, 지식폴더: Path, library) -> None:
        write(지식폴더 / "_크다.md", "가" * 4000)
        보고 = self._보고(tmp_path, library, 예산=0)[1]
        assert 보고.budget_bytes is None
        assert 보고.bytes_before == 보고.bytes_after
        assert 보고.budget_limited is False


class Test예산_경계와_단위:
    """리뷰가 짚은 공백이다 - 글자 수로 재도 통과하는 시험, 경계값 없는 시험,
    학습 지식을 안 쓰는 fixture (2026-09-19)."""

    def _본문(self, tmp_path: Path, library, 예산: int, learned: Path | None = None) -> str:
        loader = KnowledgeLoader(
            tmp_path / "persona" / "PERSONA.md", tmp_path / "persona" / "knowledge",
            learned_dir=learned,
        )
        return SystemPromptComposer(
            library, loader, base_sections(), budget_bytes=예산,
        ).compose(ctx())

    def test_글자_수가_아니라_바이트로_잰다(self, tmp_path: Path, 지식폴더: Path, library) -> None:
        """한글 한 글자가 3바이트다. 글자 수로 재면 세 배를 싣게 된다."""
        write(지식폴더 / "_한글.md", "가" * 500)  # 500자 = 1500바이트
        조합 = SystemPromptComposer(
            library, 로더(tmp_path), base_sections(), budget_bytes=100_000,
        )
        고정 = len(조합.compose(ctx()).encode("utf-8")) - 1500
        덩어리 = "가" * 100  # 안내문에도 '가' 한 글자는 나오므로 덩어리로 본다
        assert 덩어리 not in self._본문(tmp_path, library, 예산=고정 + 1499)
        assert 덩어리 in self._본문(tmp_path, library, 예산=고정 + 1600)

    def test_딱_맞으면_싣는다(self, tmp_path: Path, 지식폴더: Path, library) -> None:
        write(지식폴더 / "_가.md", "가나다")
        전체 = len(self._본문(tmp_path, library, 예산=100_000).encode("utf-8"))
        assert "가나다" in self._본문(tmp_path, library, 예산=전체)
        assert "가나다" not in self._본문(tmp_path, library, 예산=전체 - 1)

    def test_큰_것을_건너뛰고_뒤의_작은_것을_싣는다(self, tmp_path: Path, 지식폴더: Path, library) -> None:
        """파일 순서는 알파벳순이지 우선순위가 아니다. 앞의 큰 파일 하나가
        뒤의 것을 전부 버리게 하면 안 된다."""
        write(지식폴더 / "_1큰것.md", "가" * 4000)
        write(지식폴더 / "_2작은것.md", "살아남는 지식")
        본문 = self._본문(tmp_path, library, 예산=4000)
        assert "살아남는 지식" in 본문
        assert "가" * 4000 not in 본문

    def test_학습_지식도_파일_단위로_뺀다(self, tmp_path: Path, 지식폴더: Path, library) -> None:
        """하나로 합치면 통째로 떨어지고 생략 안내에 첫 파일만 적힌다."""
        learned = tmp_path / "learned"
        learned.mkdir()
        write(learned / "_큰학습.md", "가" * 4000)
        write(learned / "_작은학습.md", "남는 학습")
        본문 = self._본문(tmp_path, library, 예산=4000, learned=learned)
        assert "남는 학습" in 본문
        assert "큰학습" in 본문  # 생략 안내에 이름이 적힌다

    def test_결과가_예산을_넘지_않는다(self, tmp_path: Path, 지식폴더: Path, library) -> None:
        """생략 안내와 헤더까지 포함해 재야 상한이 상한 노릇을 한다."""
        for i in range(6):
            write(지식폴더 / f"_{i}.md", "가" * 900)
        조합 = SystemPromptComposer(
            library, 로더(tmp_path), base_sections(), budget_bytes=100_000,
        )
        고정 = len(조합.compose(ctx()).encode("utf-8")) - 6 * 2700
        예산 = 고정 + 5000
        assert len(self._본문(tmp_path, library, 예산=예산).encode("utf-8")) <= 예산
