"""BotPlugin 확장점과 PluginLoader 적재."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from slack_cli_agent.plugin.base import BotPlugin
from slack_cli_agent.plugin.loader import PluginLoader, PluginLoadError


def write_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module_name: str, source: str) -> None:
    """임시 모듈 파일을 만들고 import 가능하게 sys.path 에 얹는다."""
    (tmp_path / f"{module_name}.py").write_text(source, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop(module_name, None)


# ---------------------------------------------------------------------------
# BotPlugin — 기본 구현


class _MinimalPlugin(BotPlugin):
    name = "minimal"


class TestBotPluginDefaults:
    def test_전부_기본은_빈_튜플이다(self) -> None:
        plugin = _MinimalPlugin()
        assert plugin.access_extensions() == ()
        assert plugin.admin_commands() == ()
        assert plugin.prompt_sections() == ()
        assert plugin.output_guards() == ()
        assert plugin.preflight_checks() == ()

    def test_ABC라_name_없이는_보통_클래스처럼_인스턴스화된다(self) -> None:
        # BotPlugin 은 추상 메서드가 없어 ABC 라도 인스턴스화 자체는 막히지
        # 않는다. name 이 없다는 것은 서브클래스가 정의를 빠뜨렸다는 뜻이고,
        # 그것은 로더가 잡는다(플러그인 이름 중복·부재 판정에 name 을 쓴다).
        assert isinstance(_MinimalPlugin(), BotPlugin)


# ---------------------------------------------------------------------------
# PluginLoader — 정상 경로


class TestPluginLoaderSuccess:
    def test_module_콜론_클래스명_형식으로_적재한다(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_module(
            tmp_path,
            monkeypatch,
            "fake_plugin_named",
            "from slack_cli_agent.plugin.base import BotPlugin\n\n"
            "class MyPlugin(BotPlugin):\n    name = 'named'\n",
        )
        result = PluginLoader().load(["fake_plugin_named:MyPlugin"])
        assert result.ok is True
        assert [p.name for p in result.plugins] == ["named"]

    def test_콜론이_없으면_Plugin_속성을_찾는다(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_module(
            tmp_path,
            monkeypatch,
            "fake_plugin_default_attr",
            "from slack_cli_agent.plugin.base import BotPlugin\n\n"
            "class Plugin(BotPlugin):\n    name = 'default_attr'\n",
        )
        result = PluginLoader().load(["fake_plugin_default_attr"])
        assert result.ok is True
        assert [p.name for p in result.plugins] == ["default_attr"]

    def test_빈_목록이면_빈_결과다(self) -> None:
        result = PluginLoader().load([])
        assert result.plugins == ()
        assert result.failures == ()
        assert result.ok is True


# ---------------------------------------------------------------------------
# PluginLoader — 실패: 없는 모듈


class TestPluginLoaderMissingModule:
    def test_기본은_경고만_내고_계속한다(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_module(
            tmp_path,
            monkeypatch,
            "fake_plugin_ok_2",
            "from slack_cli_agent.plugin.base import BotPlugin\n\n"
            "class Plugin(BotPlugin):\n    name = 'ok_2'\n",
        )
        result = PluginLoader(strict=False).load(
            ["no_such_module_at_all", "fake_plugin_ok_2"]
        )
        assert result.ok is False
        assert len(result.failures) == 1
        assert result.failures[0].module_path == "no_such_module_at_all"
        assert [p.name for p in result.plugins] == ["ok_2"]

    def test_strict면_첫_실패에서_예외를_던진다(self) -> None:
        with pytest.raises(PluginLoadError):
            PluginLoader(strict=True).load(["no_such_module_at_all"])


# ---------------------------------------------------------------------------
# PluginLoader — 실패: 타입이 BotPlugin 이 아니다


class TestPluginLoaderWrongType:
    def test_BotPlugin이_아니면_실패로_기록한다(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_module(tmp_path, monkeypatch, "fake_plugin_wrong_type", "class Plugin:\n    name = 'x'\n")
        result = PluginLoader(strict=False).load(["fake_plugin_wrong_type"])
        assert result.ok is False
        assert "BotPlugin" in result.failures[0].reason

    def test_strict면_예외를_던진다(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_module(tmp_path, monkeypatch, "fake_plugin_wrong_type_2", "class Plugin:\n    name = 'x'\n")
        with pytest.raises(PluginLoadError):
            PluginLoader(strict=True).load(["fake_plugin_wrong_type_2"])


# ---------------------------------------------------------------------------
# PluginLoader — 이름 충돌


class TestPluginLoaderNameCollision:
    def test_같은_name이면_나중_것을_실패로_기록한다(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_module(
            tmp_path,
            monkeypatch,
            "fake_plugin_dup_a",
            "from slack_cli_agent.plugin.base import BotPlugin\n\n"
            "class Plugin(BotPlugin):\n    name = 'dup'\n",
        )
        write_module(
            tmp_path,
            monkeypatch,
            "fake_plugin_dup_b",
            "from slack_cli_agent.plugin.base import BotPlugin\n\n"
            "class Plugin(BotPlugin):\n    name = 'dup'\n",
        )
        result = PluginLoader(strict=False).load(["fake_plugin_dup_a", "fake_plugin_dup_b"])
        assert result.ok is False
        assert [p.name for p in result.plugins] == ["dup"]
        assert len(result.failures) == 1
        assert "dup" in result.failures[0].reason

    def test_strict면_이름_충돌도_예외다(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_module(
            tmp_path,
            monkeypatch,
            "fake_plugin_dup_c",
            "from slack_cli_agent.plugin.base import BotPlugin\n\n"
            "class Plugin(BotPlugin):\n    name = 'dup2'\n",
        )
        write_module(
            tmp_path,
            monkeypatch,
            "fake_plugin_dup_d",
            "from slack_cli_agent.plugin.base import BotPlugin\n\n"
            "class Plugin(BotPlugin):\n    name = 'dup2'\n",
        )
        with pytest.raises(PluginLoadError):
            PluginLoader(strict=True).load(["fake_plugin_dup_c", "fake_plugin_dup_d"])


# ---------------------------------------------------------------------------
# PluginLoader — 생성자에서 예외를 던지는 플러그인


class TestPluginLoaderConstructionFailure:
    def test_생성_중_예외는_실패로_기록한다(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_module(
            tmp_path,
            monkeypatch,
            "fake_plugin_boom",
            "from slack_cli_agent.plugin.base import BotPlugin\n\n"
            "class Plugin(BotPlugin):\n"
            "    name = 'boom'\n"
            "    def __init__(self):\n"
            "        raise RuntimeError('설정 오류')\n",
        )
        result = PluginLoader(strict=False).load(["fake_plugin_boom"])
        assert result.ok is False
        assert "설정 오류" in result.failures[0].reason


class TestBotPlugin엔진등록:
    """플러그인이 자기 엔진을 더할 수 있는가.

    접근 정책·관리 명령·프롬프트 절·출력 가드·사전 점검은 전부 플러그인이
    더할 수 있는데 엔진만 빠져 있었다. 이 저장소는 특정 조직에 묶이지 않는
    범용 유틸리티를 지향하므로, 엔진 종류가 코드에 고정되면 쓰는 쪽이 자기
    엔진을 못 붙인다.
    """

    def test_기본은_빈_튜플이다(self) -> None:
        class 아무것도안더하는플러그인(BotPlugin):
            name = "plain"

        assert 아무것도안더하는플러그인().engines() == ()
