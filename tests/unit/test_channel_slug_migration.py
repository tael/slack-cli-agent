"""채널 슬러그가 바뀔 때 지식과 아카이브가 따라 옮겨지는지 본다.

슬러그는 `channel_slug` 가 정하고, 등록 전에는 채널 ID 다(bot.py:145 와 같다).
나중에 그 채널을 channels.json 에 등록하면 슬러그가 이름으로 바뀌어,
`persona/knowledge/<ID>.md` · `persona/learned/<ID>.md` · `responses/<ID>/` 에
쌓인 것이 조용히 안 읽히게 된다(sca-do8s). 실제로 rei 의
`persona/learned/C0C1LNABECV.md` 와 shinji 의 `learned/C0B72NX6WN9.md` 가
그 상태로 남아 있었다.
"""

from __future__ import annotations

import fcntl
import json
from pathlib import Path

import pytest

from slack_cli_agent.config import channel as channel_module
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
        (learned / "C0C1LNABECV.md").write_text("배운 것", encoding="utf-8")
        self._기록(설정파일, {"C0C1LNABECV": {"name": "테스트"}})

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


class Test두_프로세스_경합:
    """worker 와 ingress 가 각각 등록부를 다시 읽으면 이사가 두 번 돈다.
    `channel.py` 의 락은 프로세스 안에서만 듣는다(sca-uk55)."""

    def test_다른_프로세스가_이사_중이면_손대지_않고_실패로_알린다(self, 상태, tmp_path) -> None:
        knowledge, learned, responses = 상태
        (learned / "C1.md").write_text("배운 것", encoding="utf-8")
        잠금 = tmp_path / "slug-migration.lock"
        이사 = ChannelSlugMigrator(
            file_dirs=(knowledge, learned),
            tree_roots=(responses,),
            lock_path=잠금,
            lock_timeout=0.05,
        )

        잠금.parent.mkdir(parents=True, exist_ok=True)
        with open(잠금, "w") as 남의손:
            fcntl.flock(남의손, fcntl.LOCK_EX)
            결과 = 이사.migrate("C1", "테스트")

        assert 결과 is False
        assert (learned / "C1.md").exists()
        assert not (learned / "테스트.md").exists()

    def test_잠금_파일을_열지_못하면_옮기지_않고_실패로_알린다(self, 상태, tmp_path, caplog) -> None:
        """상태 디렉터리가 망가져 락을 못 만들면 두 프로세스가 같은 파일을
        동시에 옮길 수 있다. 열기 실패는 미획득으로 본다(sca-tlgp)."""
        knowledge, learned, responses = 상태
        (learned / "C1.md").write_text("배운 것", encoding="utf-8")
        막힌자리 = tmp_path / "막힌곳"
        막힌자리.write_text("디렉터리가 아니다", encoding="utf-8")
        이사 = ChannelSlugMigrator(
            file_dirs=(knowledge, learned),
            tree_roots=(responses,),
            lock_path=막힌자리 / "slug-migration.lock",
            lock_timeout=0.05,
        )

        with caplog.at_level("WARNING"):
            결과 = 이사.migrate("C1", "테스트")

        assert 결과 is False
        assert (learned / "C1.md").exists()
        assert not (learned / "테스트.md").exists()

    def test_락이_비어_있으면_평소대로_옮긴다(self, 상태, tmp_path) -> None:
        knowledge, learned, responses = 상태
        (learned / "C1.md").write_text("배운 것", encoding="utf-8")
        이사 = ChannelSlugMigrator(
            file_dirs=(knowledge, learned),
            tree_roots=(responses,),
            lock_path=tmp_path / "slug-migration.lock",
        )

        assert 이사.migrate("C1", "테스트") is True
        assert (learned / "테스트.md").exists()

    def test_옮기는_중_원본_디렉터리가_사라져도_예외가_나가지_않는다(
        self, 상태, 이사, monkeypatch
    ) -> None:
        """다른 프로세스가 같은 이사를 먼저 끝내면 `iterdir` 이 깨진다."""
        _, _, responses = 상태
        (responses / "C1").mkdir()
        (responses / "C1" / "2026-09-20.md").write_text("응답", encoding="utf-8")
        (responses / "테스트").mkdir()

        원래 = Path.iterdir

        def 사라진다(self):
            if self.name == "C1":
                raise FileNotFoundError(2, "No such file or directory", str(self))
            return 원래(self)

        monkeypatch.setattr(Path, "iterdir", 사라진다)

        assert 이사.migrate("C1", "테스트") is False


class Test이사_실패는_다시_시도된다:
    """등록부가 슬러그를 먼저 갱신해 버리면 일시 실패가 영구 미이사가
    된다(sca-tl2q). 옛 슬러그를 남겨 다음 재파싱에서 다시 부른다."""

    @pytest.fixture
    def 설정파일(self, tmp_path):
        return tmp_path / "channels.json"

    @staticmethod
    def _기록(설정파일, 내용):
        설정파일.write_text(json.dumps(내용, ensure_ascii=False), encoding="utf-8")

    def test_실패하면_다음_접근에서_같은_이사를_다시_부른다(self, 설정파일, monkeypatch) -> None:
        monkeypatch.setattr(channel_module, "SLUG_RETRY_INTERVAL_SEC", 0.0)
        시도: list[tuple[str, str]] = []
        남은실패 = [True]

        def 콜백(old: str, new: str) -> bool:
            시도.append((old, new))
            if 남은실패:
                남은실패.pop()
                return False
            return True

        self._기록(설정파일, {})
        registry = ChannelRegistry(설정파일, on_slug_change=콜백)
        registry.all()

        registry.update("C1", {"name": "테스트"})
        assert 시도 == [("C1", "테스트")]

        registry.all()

        assert 시도 == [("C1", "테스트"), ("C1", "테스트")]

    def test_성공하면_다시_부르지_않는다(self, 설정파일) -> None:
        시도: list[tuple[str, str]] = []

        def 콜백(old: str, new: str) -> bool:
            시도.append((old, new))
            return True

        self._기록(설정파일, {})
        registry = ChannelRegistry(설정파일, on_slug_change=콜백)
        registry.all()

        registry.update("C1", {"name": "테스트"})
        registry.all()
        registry.all()

        assert 시도 == [("C1", "테스트")]

    def test_실패한_이사가_끝나면_더는_부르지_않는다(
        self, 상태, 설정파일, tmp_path, monkeypatch
    ) -> None:
        """락을 쥔 다른 프로세스 때문에 못 옮긴 뒤, 락이 풀리면 옮긴다."""
        monkeypatch.setattr(channel_module, "SLUG_RETRY_INTERVAL_SEC", 0.0)
        knowledge, learned, responses = 상태
        (learned / "C1.md").write_text("배운 것", encoding="utf-8")
        잠금 = tmp_path / "slug-migration.lock"
        이사 = ChannelSlugMigrator(
            file_dirs=(knowledge, learned),
            tree_roots=(responses,),
            lock_path=잠금,
            lock_timeout=0.05,
        )
        self._기록(설정파일, {})
        registry = ChannelRegistry(설정파일, on_slug_change=이사.migrate)
        registry.all()

        잠금.parent.mkdir(parents=True, exist_ok=True)
        with open(잠금, "w") as 남의손:
            fcntl.flock(남의손, fcntl.LOCK_EX)
            registry.update("C1", {"name": "테스트"})
            assert (learned / "C1.md").exists()

        registry.all()

        assert (learned / "테스트.md").exists()


class Test재시도_간격:
    """이사가 계속 실패하면 조회마다 재파싱과 재이사가 돌아, 락 타임아웃 5초가
    붙은 장애 구간에서 호출마다 지연과 경고가 반복된다(sca-c2e7). 횟수 상한이
    아니라 간격을 두어, 실패가 영구 미이사로 굳는 것은 막는다(sca-tl2q)."""

    @pytest.fixture
    def 설정파일(self, tmp_path):
        return tmp_path / "channels.json"

    @staticmethod
    def _기록(설정파일, 내용):
        설정파일.write_text(json.dumps(내용, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def _늘_실패하는_콜백(시도):
        def 콜백(old: str, new: str) -> bool:
            시도.append((old, new))
            return False

        return 콜백

    def test_간격_안의_조회는_다시_부르지_않는다(self, 설정파일) -> None:
        시도: list[tuple[str, str]] = []
        self._기록(설정파일, {})
        registry = ChannelRegistry(설정파일, on_slug_change=self._늘_실패하는_콜백(시도))
        registry.all()

        registry.update("C1", {"name": "테스트"})
        assert 시도 == [("C1", "테스트")]

        registry.all()
        registry.is_registered("C1")
        registry.get("C1")

        assert 시도 == [("C1", "테스트")]

    def test_간격이_지나면_다시_부른다(self, 설정파일, monkeypatch) -> None:
        시계 = [1000.0]
        monkeypatch.setattr(channel_module, "_now", lambda: 시계[0])
        시도: list[tuple[str, str]] = []
        self._기록(설정파일, {})
        registry = ChannelRegistry(설정파일, on_slug_change=self._늘_실패하는_콜백(시도))
        registry.all()

        registry.update("C1", {"name": "테스트"})
        registry.all()
        assert 시도 == [("C1", "테스트")]

        시계[0] += channel_module.SLUG_RETRY_INTERVAL_SEC
        registry.all()

        assert 시도 == [("C1", "테스트"), ("C1", "테스트")]

    def test_다른_채널이_추가돼도_실패한_이사는_간격을_지킨다(self, 설정파일) -> None:
        """파일이 바뀌었다는 것만으로 재시도하면 간격이 무의미해진다. 실제로
        슬러그가 바뀐 채널만 그 자리에서 시도한다(sca-vtqq)."""
        시도: list[tuple[str, str]] = []
        self._기록(설정파일, {})
        registry = ChannelRegistry(설정파일, on_slug_change=self._늘_실패하는_콜백(시도))
        registry.all()

        registry.update("C1", {"name": "테스트"})
        assert 시도 == [("C1", "테스트")]

        self._기록(설정파일, {"C1": {"name": "테스트"}, "C2": {"name": "둘"}})
        registry._mtime = -1.0
        registry.all()

        assert 시도 == [("C1", "테스트"), ("C2", "둘")]

    def test_슬러그가_다시_바뀌면_간격을_기다리지_않는다(self, 설정파일) -> None:
        """이름이 또 바뀐 것은 앞선 실패와 다른 사건이다."""
        시도: list[tuple[str, str]] = []
        self._기록(설정파일, {})
        registry = ChannelRegistry(설정파일, on_slug_change=self._늘_실패하는_콜백(시도))
        registry.all()

        registry.update("C1", {"name": "테스트"})
        registry.update("C1", {"name": "둘째"})

        assert 시도 == [("C1", "테스트"), ("C1", "둘째")]

    def test_설정_명령이_파일을_다시_써도_간격을_지킨다(self, 설정파일) -> None:
        """설정 명령은 channels.json 을 다시 쓴다. 그 mtime 변화가 간격을
        무력화하면 락 장애 중 명령마다 락 타임아웃을 문다(sca-vtqq)."""
        시도: list[tuple[str, str]] = []
        self._기록(설정파일, {})
        registry = ChannelRegistry(설정파일, on_slug_change=self._늘_실패하는_콜백(시도))
        registry.all()

        registry.update("C1", {"name": "테스트"})
        assert 시도 == [("C1", "테스트")]

        registry.update("C1", {"chat": "quiet"})
        registry.update("C1", {"chat": "normal"})

        assert 시도 == [("C1", "테스트")]

    def test_다른_프로세스와_재시도_간격을_공유한다(self, 설정파일, monkeypatch) -> None:
        """간격이 인스턴스 상태면 프로세스 수만큼 락 타임아웃이 쌓인다
        (sca-vtqq). 표식 파일로 간격을 프로세스 밖에 둔다."""
        시계 = [1000.0]
        monkeypatch.setattr(channel_module, "_now", lambda: 시계[0])
        갑시도: list[tuple[str, str]] = []
        을시도: list[tuple[str, str]] = []
        self._기록(설정파일, {})
        갑 = ChannelRegistry(설정파일, on_slug_change=self._늘_실패하는_콜백(갑시도))
        을 = ChannelRegistry(설정파일, on_slug_change=self._늘_실패하는_콜백(을시도))
        갑.all()
        을.all()

        갑.update("C1", {"name": "테스트"})
        을.all()
        assert 갑시도 == [("C1", "테스트")]
        assert 을시도 == [("C1", "테스트")]

        시계[0] += channel_module.SLUG_RETRY_INTERVAL_SEC
        갑.all()
        을.all()

        assert 갑시도 == [("C1", "테스트"), ("C1", "테스트")]
        assert 을시도 == [("C1", "테스트")]
