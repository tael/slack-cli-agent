"""채널 슬러그가 바뀔 때 지식과 아카이브가 따라 옮겨지는지 본다.

슬러그는 `channel_slug` 가 정하고, 등록 전에는 채널 ID 다(bot.py:145 와 같다).
나중에 그 채널을 channels.json 에 등록하면 슬러그가 이름으로 바뀌어,
`persona/knowledge/<ID>.md` · `persona/learned/<ID>.md` · `responses/<ID>/` 에
쌓인 것이 조용히 안 읽히게 된다(sca-do8s). 실제로 rei 의
`persona/learned/C0EXAMPLE01.md` 와 shinji 의 `learned/C0EXAMPLE02.md` 가
그 상태로 남아 있었다.
"""

from __future__ import annotations

import json

import pytest

from slack_cli_agent.config.channel import ChannelRegistry
from slack_cli_agent.config.slug_migration import ChannelSlugMigrator


@pytest.fixture
def 상태(tmp_path):
    knowledge = tmp_path / "persona" / "knowledge"
    learned = tmp_path / "persona" / "learned"
    responses = tmp_path / "responses"
    for 경로 in (knowledge, learned, responses):
        경로.mkdir(parents=True)
    return knowledge, learned, responses


@pytest.fixture
def 이사(상태):
    knowledge, learned, responses = 상태
    return ChannelSlugMigrator(file_dirs=(knowledge, learned), tree_roots=(responses,))


class Test지식_파일_이동:
    def test_채널ID_파일이_새_슬러그로_옮겨진다(self, 상태, 이사) -> None:
        _, learned, _ = 상태
        (learned / "C1.md").write_text("배운 것", encoding="utf-8")

        이사.migrate("C1", "테스트")

        assert (learned / "테스트.md").read_text(encoding="utf-8") == "배운 것"
        assert not (learned / "C1.md").exists()

    def test_여러_디렉터리를_함께_옮긴다(self, 상태, 이사) -> None:
        knowledge, learned, _ = 상태
        (knowledge / "C1.md").write_text("사람이 쓴 것", encoding="utf-8")
        (learned / "C1.md").write_text("배운 것", encoding="utf-8")

        이사.migrate("C1", "테스트")

        assert (knowledge / "테스트.md").exists()
        assert (learned / "테스트.md").exists()

    def test_대상이_이미_있으면_덮지_않고_경고한다(self, 상태, 이사, caplog) -> None:
        _, learned, _ = 상태
        (learned / "C1.md").write_text("옛 것", encoding="utf-8")
        (learned / "테스트.md").write_text("새 것", encoding="utf-8")

        with caplog.at_level("WARNING"):
            이사.migrate("C1", "테스트")

        assert (learned / "테스트.md").read_text(encoding="utf-8") == "새 것"
        assert (learned / "C1.md").read_text(encoding="utf-8") == "옛 것"
        assert "C1" in caplog.text

    def test_옮길_것이_없으면_아무_일도_없다(self, 상태, 이사) -> None:
        _, learned, _ = 상태
        이사.migrate("C1", "테스트")
        assert list(learned.iterdir()) == []

    def test_같은_슬러그면_손대지_않는다(self, 상태, 이사) -> None:
        _, learned, _ = 상태
        (learned / "C1.md").write_text("그대로", encoding="utf-8")

        이사.migrate("C1", "C1")

        assert (learned / "C1.md").read_text(encoding="utf-8") == "그대로"


class Test응답_아카이브_이동:
    def test_채널ID_디렉터리가_새_슬러그로_옮겨진다(self, 상태, 이사) -> None:
        _, _, responses = 상태
        (responses / "C1").mkdir()
        (responses / "C1" / "2026-09-20.md").write_text("응답", encoding="utf-8")

        이사.migrate("C1", "테스트")

        assert (responses / "테스트" / "2026-09-20.md").read_text(encoding="utf-8") == "응답"
        assert not (responses / "C1").exists()

    def test_대상_디렉터리가_있으면_겹치지_않는_날짜만_옮긴다(self, 상태, 이사, caplog) -> None:
        _, _, responses = 상태
        (responses / "C1").mkdir()
        (responses / "C1" / "2026-09-20.md").write_text("옛 날", encoding="utf-8")
        (responses / "C1" / "2026-09-21.md").write_text("겹치는 날 옛 것", encoding="utf-8")
        (responses / "테스트").mkdir()
        (responses / "테스트" / "2026-09-21.md").write_text("겹치는 날 새 것", encoding="utf-8")

        with caplog.at_level("WARNING"):
            이사.migrate("C1", "테스트")

        assert (responses / "테스트" / "2026-09-20.md").read_text(encoding="utf-8") == "옛 날"
        assert (responses / "테스트" / "2026-09-21.md").read_text(encoding="utf-8") == "겹치는 날 새 것"
        assert (responses / "C1" / "2026-09-21.md").exists()
        assert "2026-09-21" in caplog.text


class Test등록부가_이사를_부른다:
    """`ChannelRegistry` 가 슬러그 변화를 감지해 이사를 부른다. 관리 명령·웹
    콘솔·손편집이 전부 이 한 곳을 지난다."""

    @pytest.fixture
    def 설정파일(self, tmp_path):
        return tmp_path / "channels.json"

    @staticmethod
    def _기록(설정파일, 내용):
        설정파일.write_text(json.dumps(내용, ensure_ascii=False), encoding="utf-8")

    def test_이름을_등록하면_지식이_따라_옮겨진다(self, 상태, 이사, 설정파일) -> None:
        _, learned, _ = 상태
        (learned / "C1.md").write_text("배운 것", encoding="utf-8")
        self._기록(설정파일, {})
        registry = ChannelRegistry(설정파일, on_slug_change=이사.migrate)
        registry.all()

        registry.update("C1", {"name": "테스트"})

        assert (learned / "테스트.md").exists()
        assert not (learned / "C1.md").exists()

    def test_이름이_바뀌면_옛_이름의_지식이_따라간다(self, 상태, 이사, 설정파일) -> None:
        _, learned, _ = 상태
        (learned / "옛이름.md").write_text("배운 것", encoding="utf-8")
        self._기록(설정파일, {"C1": {"name": "옛이름"}})
        registry = ChannelRegistry(설정파일, on_slug_change=이사.migrate)
        registry.all()

        registry.update("C1", {"name": "새이름"})

        assert (learned / "새이름.md").exists()

    def test_채널을_해제하면_채널ID_로_되돌린다(self, 상태, 이사, 설정파일) -> None:
        _, learned, _ = 상태
        (learned / "테스트.md").write_text("배운 것", encoding="utf-8")
        self._기록(설정파일, {"C1": {"name": "테스트"}})
        registry = ChannelRegistry(설정파일, on_slug_change=이사.migrate)
        registry.all()

        registry.remove("C1")

        assert (learned / "C1.md").exists()

    def test_손으로_고친_등록도_다시_읽을_때_옮겨진다(self, 상태, 이사, 설정파일) -> None:
        """등록부는 mtime 이 바뀌면 다시 읽는다. 사람이 파일을 직접 고쳐
        등록해도 그 경로를 지난다."""
        _, learned, _ = 상태
        (learned / "C1.md").write_text("배운 것", encoding="utf-8")
        self._기록(설정파일, {})
        registry = ChannelRegistry(설정파일, on_slug_change=이사.migrate)
        registry.all()

        self._기록(설정파일, {"C1": {"name": "테스트"}})
        registry._mtime = -1.0
        registry.all()

        assert (learned / "테스트.md").exists()

    def test_새_프로세스는_남아_있던_채널ID_파일을_거둔다(self, 상태, 이사, 설정파일) -> None:
        """봇이 꺼진 사이에 등록됐으면 변화를 못 본다. 처음 읽을 때 등록된
        채널마다 채널 ID 자리를 확인한다 - rei 가 이 상태였다."""
        _, learned, _ = 상태
        (learned / "C0EXAMPLE01.md").write_text("배운 것", encoding="utf-8")
        self._기록(설정파일, {"C0EXAMPLE01": {"name": "테스트"}})

        ChannelRegistry(설정파일, on_slug_change=이사.migrate).all()

        assert (learned / "테스트.md").exists()

    def test_바뀐_것이_없으면_부르지_않는다(self, 설정파일) -> None:
        불린것: list[tuple[str, str]] = []
        self._기록(설정파일, {"C1": {"name": "테스트"}})
        registry = ChannelRegistry(설정파일, on_slug_change=lambda old, new: 불린것.append((old, new)))
        registry.all()
        불린것.clear()

        registry.update("C1", {"chat": "quiet"})
        registry._mtime = -1.0
        registry.all()

        assert 불린것 == []

    def test_DM_은_설정이_생겨도_옮기지_않는다(self, 설정파일) -> None:
        """DM 슬러그는 등록 여부와 무관하게 `dm` 이다."""
        불린것: list[tuple[str, str]] = []
        self._기록(설정파일, {})
        registry = ChannelRegistry(설정파일, on_slug_change=lambda old, new: 불린것.append((old, new)))
        registry.all()

        registry.update("D1", {"name": "누구와의대화"})

        assert 불린것 == []
