"""위키 목차를 매 턴 싣는다 (H5, 마메치 bot.py load_persona 대응).

위키 본문 페이지는 모델이 골라 연다. 그러려면 목차가 when: 표기와 무관하게
매 턴 실려야 하고, 목차 안의 경로가 Read 로 열리는 절대경로여야 한다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from slack_cli_agent.config.paths import StatePaths
from slack_cli_agent.prompt.knowledge import WIKI_INDEX_MIRROR, KnowledgeLoader

INDEX = (
    "<!-- 매 턴 실린다 -->\n# 위키 목차\n\n"
    "주제가 맞으면 {wiki}/<이름>.md 를 Read 로 연다.\n\n"
    "- 월정산-원천 : 월정산이 읽는 원천\n"
)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    knowledge = tmp_path / "persona" / "knowledge"
    wiki = tmp_path / "persona" / "wiki"
    knowledge.mkdir(parents=True)
    wiki.mkdir(parents=True)
    return knowledge, wiki


def loader(knowledge: Path, wiki: Path | None) -> KnowledgeLoader:
    return KnowledgeLoader(knowledge.parent / "PERSONA.md", knowledge, wiki_dir=wiki)


class Test위키_목차를_싣는다:
    def test_요청_낱말과_무관하게_싣는다(self, dirs: tuple[Path, Path]) -> None:
        knowledge, wiki = dirs
        write(wiki / "index.md", INDEX.format(wiki=wiki))
        text = loader(knowledge, wiki).knowledge_text("", prompt="아무 말")
        assert "# 위키 목차" in text
        assert "월정산-원천" in text

    def test_본문_페이지는_싣지_않는다(self, dirs: tuple[Path, Path]) -> None:
        knowledge, wiki = dirs
        write(wiki / "index.md", INDEX.format(wiki=wiki))
        write(wiki / "월정산-원천.md", "본문 내용")
        assert "본문 내용" not in loader(knowledge, wiki).knowledge_text("")

    def test_물결표_경로를_절대경로로_편다(
        self, dirs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Read 는 절대경로만 받는다. ~ 가 남으면 모델이 페이지를 열지 못한다."""
        knowledge, wiki = dirs
        home = wiki.parent.parent
        monkeypatch.setenv("HOME", str(home))
        rel = wiki.relative_to(home)
        write(wiki / "index.md", INDEX.format(wiki=f"~/{rel}"))
        text = loader(knowledge, wiki).knowledge_text("")
        assert f"{wiki}/<이름>.md" in text
        assert "~/" not in text

    def test_미러가_만든_사본은_두_번_싣지_않는다(self, dirs: tuple[Path, Path]) -> None:
        """미러가 knowledge/_위키목차.md 를 계속 만들어도 목차는 한 번만 실린다."""
        knowledge, wiki = dirs
        write(wiki / "index.md", INDEX.format(wiki=wiki))
        write(knowledge / WIKI_INDEX_MIRROR, INDEX.format(wiki=wiki))
        text = loader(knowledge, wiki).knowledge_text("")
        assert text.count("# 위키 목차") == 1

    def test_목차가_없으면_미러_사본을_그대로_싣는다(self, dirs: tuple[Path, Path]) -> None:
        knowledge, wiki = dirs
        write(knowledge / WIKI_INDEX_MIRROR, INDEX.format(wiki=wiki))
        assert loader(knowledge, wiki).knowledge_text("").count("# 위키 목차") == 1

    def test_위키_디렉터리를_안_주면_예전대로_동작한다(self, dirs: tuple[Path, Path]) -> None:
        knowledge, wiki = dirs
        write(wiki / "index.md", INDEX.format(wiki=wiki))
        write(knowledge / "_공통.md", "공통 지식")
        text = loader(knowledge, None).knowledge_text("")
        assert "공통 지식" in text
        assert "# 위키 목차" not in text

    def test_공통_지식_뒤_채널_지식_앞에_온다(self, dirs: tuple[Path, Path]) -> None:
        """마메치와 같은 순서다. 채널 지식이 목차보다 뒤에 와야 채널 쪽이 이긴다."""
        knowledge, wiki = dirs
        write(wiki / "index.md", INDEX.format(wiki=wiki))
        write(knowledge / "_공통.md", "공통 지식")
        write(knowledge / "잡담.md", "채널 지식")
        text = loader(knowledge, wiki).knowledge_text("잡담")
        assert text.index("공통 지식") < text.index("# 위키 목차") < text.index("채널 지식")


def test_봇_경로에_위키_디렉터리가_있다(tmp_path: Path) -> None:
    paths = StatePaths(tmp_path)
    assert paths.wiki == paths.persona / "wiki"
