"""프로필, 채널 설정, 실행 상수, 상태 경로."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from slack_cli_agent.config.channel import ChannelRegistry
from slack_cli_agent.config.paths import StatePaths
from slack_cli_agent.config.profile import EngineSpec, McpServerSpec, Profile
from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.core.errors import ConfigError

MINIMAL = {
    "name": "example",
    "primary_engine": {"type": "claude", "binary": "claude", "model": "m"},
    "owner_user_id": "U1",
    "troubleshoot_channel": "C1",
}


def write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")
    # mtime 초 단위 해상도로 변경을 놓치지 않게 한다
    os.utime(path, (0, 0))


class TestEngineSpec:
    def test_소유자용_모델이_없으면_기본_모델을_쓴다(self) -> None:
        spec = EngineSpec.from_dict({"type": "claude", "binary": "c", "model": "m"})
        assert spec.model_for_owner() == "m"

    def test_소유자용_모델이_있으면_그것을_쓴다(self) -> None:
        spec = EngineSpec.from_dict(
            {"type": "claude", "binary": "c", "model": "m", "model_owner": "big"}
        )
        assert spec.model_for_owner() == "big"

    def test_필수_항목이_없으면_설정_오류다(self) -> None:
        with pytest.raises(ConfigError, match="model"):
            EngineSpec.from_dict({"type": "claude", "binary": "c"})

    def test_실행_파일_경로의_물결표를_확장한다(self, tmp_path: Path) -> None:
        spec = EngineSpec.from_dict(
            {"type": "claude", "binary": "~/bin/claude", "model": "m"}
        )
        assert spec.binary == tmp_path / "bin" / "claude"


class TestProfile:
    def test_상태_디렉터리_기본값은_홈_아래_점_이름이다(self, tmp_path: Path) -> None:
        profile = Profile.from_dict(MINIMAL)
        assert profile.state_dir == tmp_path / ".example"

    def test_파생_경로가_상태_디렉터리를_따른다(self, tmp_path: Path) -> None:
        profile = Profile.from_dict({**MINIMAL, "state_dir": str(tmp_path / "st")})
        assert profile.work_root == tmp_path / "st" / "work"
        assert profile.data_dir == tmp_path / "st" / "data"
        assert profile.attach_dir == tmp_path / "st" / "attachments"

    def test_launch_label_기본값은_local_점_이름이다(self) -> None:
        assert Profile.from_dict(MINIMAL).launch_label == "local.example"

    def test_엔진_블록이_없으면_설정_오류다(self) -> None:
        with pytest.raises(ConfigError, match="primary_engine"):
            Profile.from_dict({"name": "x"})

    def test_이름이_없으면_설정_오류다(self) -> None:
        with pytest.raises(ConfigError, match="name"):
            Profile.from_dict({"primary_engine": MINIMAL["primary_engine"]})

    def test_검색_경로에서_먼저_찾은_것을_쓴다(self, tmp_path: Path) -> None:
        first, second = tmp_path / "a", tmp_path / "b"
        first.mkdir()
        second.mkdir()
        write(second / "example.json", MINIMAL)
        write(first / "example.json", {**MINIMAL, "display_name": "앞"})

        assert Profile.load("example", [first, second]).display_name == "앞"

    def test_어디에도_없으면_검색_경로를_알린다(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match=str(tmp_path)):
            Profile.load("없음", [tmp_path])

    def test_소유자와_문제_채널이_비면_검증에_걸린다(self) -> None:
        profile = Profile.from_dict(
            {**MINIMAL, "owner_user_id": "", "troubleshoot_channel": ""}
        )
        problems = profile.validate()
        assert len(problems) == 2

    def test_폴백이_1차와_같은_종류면_검증에_걸린다(self) -> None:
        profile = Profile.from_dict(
            {**MINIMAL, "fallback_engine": MINIMAL["primary_engine"]}
        )
        assert any("같은 종류" in p for p in profile.validate())

    def test_정상_프로필은_문제가_없다(self) -> None:
        assert Profile.from_dict(MINIMAL).validate() == []

    def test_절대경로_실행_파일이_없으면_검증에_걸린다(self, tmp_path: Path) -> None:
        profile = Profile.from_dict(
            {
                **MINIMAL,
                "primary_engine": {
                    "type": "claude",
                    "binary": str(tmp_path / "없는파일"),
                    "model": "m",
                },
            }
        )
        assert any("실행 파일" in p for p in profile.validate())


class TestMcpServerSpec:
    def test_command_형은_command만_있으면_된다(self) -> None:
        spec = McpServerSpec.from_dict("서버", {"command": "node", "args": ["a.js"]})
        assert spec.command == "node"
        assert spec.args == ("a.js",)
        assert spec.is_remote is False

    def test_url_형은_url만_있으면_된다(self) -> None:
        spec = McpServerSpec.from_dict("서버", {"url": "https://example.com/mcp/"})
        assert spec.url == "https://example.com/mcp/"
        assert spec.is_remote is True

    def test_command과_url이_둘_다_없으면_설정_오류다(self) -> None:
        with pytest.raises(ConfigError, match="command"):
            McpServerSpec.from_dict("서버", {})

    def test_command과_url이_둘_다_있으면_설정_오류다(self) -> None:
        with pytest.raises(ConfigError, match="동시에"):
            McpServerSpec.from_dict(
                "서버", {"command": "node", "url": "https://example.com/mcp/"}
            )

    def test_env와_headers_기본값은_빈_딕셔너리다(self) -> None:
        spec = McpServerSpec.from_dict("서버", {"command": "node"})
        assert spec.env == {}
        assert spec.headers == {}

    def test_env를_그대로_보존한다(self) -> None:
        spec = McpServerSpec.from_dict(
            "서버", {"command": "node", "env": {"KEY": "value"}}
        )
        assert spec.env == {"KEY": "value"}

    def test_disabled_기본값은_거짓이다(self) -> None:
        assert McpServerSpec.from_dict("서버", {"command": "node"}).disabled is False

    def test_disabled_tools_기본값은_빈_튜플이다(self) -> None:
        spec = McpServerSpec.from_dict("서버", {"command": "node"})
        assert spec.disabled_tools == ()

    def test_disabled_tools를_튜플로_받는다(self) -> None:
        spec = McpServerSpec.from_dict(
            "서버", {"command": "node", "disabled_tools": ["a", "b"]}
        )
        assert spec.disabled_tools == ("a", "b")

    def test_cwd의_물결표를_확장한다(self, tmp_path: Path) -> None:
        spec = McpServerSpec.from_dict(
            "서버", {"command": "node", "cwd": "~/work"}
        )
        assert spec.cwd == tmp_path / "work"

    def test_cwd가_없으면_None이다(self) -> None:
        assert McpServerSpec.from_dict("서버", {"command": "node"}).cwd is None


class TestProfileMcpServers:
    def test_기본값은_빈_딕셔너리다(self) -> None:
        assert Profile.from_dict(MINIMAL).mcp_servers == {}

    def test_이름별로_McpServerSpec을_만든다(self) -> None:
        profile = Profile.from_dict(
            {**MINIMAL, "mcp_servers": {"a": {"command": "node"}}}
        )
        assert isinstance(profile.mcp_servers["a"], McpServerSpec)
        assert profile.mcp_servers["a"].name == "a"

    def test_잘못된_서버_설정은_설정_오류로_이어진다(self) -> None:
        with pytest.raises(ConfigError, match="command"):
            Profile.from_dict({**MINIMAL, "mcp_servers": {"a": {}}})

    def test_설치물_기본_프로필에는_서버_이름이_없다(self) -> None:
        """새 봇을 만들 때 특정 MCP 서버가 딸려오면 안 된다."""
        assert Profile.from_dict(MINIMAL).mcp_servers == {}

    def test_존재하지_않는_cwd는_검증에_걸린다(self, tmp_path: Path) -> None:
        profile = Profile.from_dict(
            {
                **MINIMAL,
                "mcp_servers": {
                    "a": {"command": "node", "cwd": str(tmp_path / "없음")}
                },
            }
        )
        assert any("cwd" in p for p in profile.validate())

    def test_정상_서버_설정은_검증을_통과한다(self, tmp_path: Path) -> None:
        profile = Profile.from_dict(
            {**MINIMAL, "mcp_servers": {"a": {"command": "node", "cwd": str(tmp_path)}}}
        )
        assert profile.validate() == []


class TestStatePaths:
    def test_봇_이름으로_홈_아래_경로를_만든다(self, tmp_path: Path) -> None:
        paths = StatePaths.for_bot("example")
        assert paths.root == tmp_path / ".example"

    def test_하위_경로가_루트를_따른다(self, tmp_path: Path) -> None:
        paths = StatePaths(tmp_path / "st")
        assert paths.database == tmp_path / "st" / "state.db"
        assert paths.prompts == tmp_path / "st" / "prompts"
        assert paths.engine_settings("codex") == (
            tmp_path / "st" / "engine" / "settings-codex.json"
        )

    def test_필요한_디렉터리를_만든다(self, tmp_path: Path) -> None:
        paths = StatePaths(tmp_path / "st")
        paths.ensure()
        assert paths.prompts.is_dir() and paths.engine_dir.is_dir()


class TestRuntimeSettings:
    def test_지정한_항목만_덮어쓴다(self) -> None:
        settings = RuntimeSettings().override({"max_concurrent": 3})
        assert settings.max_concurrent == 3
        assert settings.request_timeout_sec == 900

    def test_모르는_키는_무시한다(self) -> None:
        assert RuntimeSettings().override({"없는항목": 1}).max_concurrent == 10

    def test_원본을_바꾸지_않는다(self) -> None:
        original = RuntimeSettings()
        original.override({"max_concurrent": 3})
        assert original.max_concurrent == 10


class TestChannelRegistry:
    def test_파일이_없으면_빈_목록이다(self, tmp_path: Path) -> None:
        assert ChannelRegistry(tmp_path / "channels.json").channel_ids() == []

    def test_등록된_채널을_확인한다(self, tmp_path: Path) -> None:
        path = tmp_path / "channels.json"
        write(path, {"C1": {"mode": "helpdesk"}})
        registry = ChannelRegistry(path)
        assert registry.is_registered("C1")
        assert registry.get("C1").mode == "helpdesk"

    def test_파일을_고치면_재기동_없이_반영된다(self, tmp_path: Path) -> None:
        path = tmp_path / "channels.json"
        write(path, {"C1": {"mode": "default"}})
        registry = ChannelRegistry(path)
        registry.channel_ids()

        path.write_text(json.dumps({"C1": {"mode": "helpdesk"}, "C2": {}}), encoding="utf-8")
        os.utime(path, (100, 100))

        assert registry.is_registered("C2")
        assert registry.get("C1").mode == "helpdesk"

    def test_깨진_파일은_직전_설정을_유지한다(self, tmp_path: Path) -> None:
        path = tmp_path / "channels.json"
        write(path, {"C1": {"mode": "default"}})
        registry = ChannelRegistry(path)
        registry.channel_ids()

        path.write_text("{ 편집 중", encoding="utf-8")
        os.utime(path, (100, 100))

        assert registry.is_registered("C1")

    def test_모르는_키는_extra_에_보존한다(self, tmp_path: Path) -> None:
        path = tmp_path / "channels.json"
        write(path, {"C1": {"mode": "default", "플러그인설정": {"a": 1}}})
        assert ChannelRegistry(path).get("C1").extra == {"플러그인설정": {"a": 1}}

    def test_작업_디렉터리의_물결표를_확장한다(self, tmp_path: Path) -> None:
        path = tmp_path / "channels.json"
        write(path, {"C1": {"workdir": "~/Projects/x"}})
        assert ChannelRegistry(path).get("C1").workdir == tmp_path / "Projects" / "x"


class TestRuntimeSettings집합항목:
    """프로필 JSON 에서 온 목록이 집합 항목에 그대로 들어가는 문제.

    `owner_only_channels` 는 `frozenset[str]` 인데 JSON 에는 집합 형식이
    없어 목록으로 온다. 목록 그대로 두면 포함 여부 판정은 되지만 타입
    계약이 깨지고, 같은 값이 중복으로 들어와도 걸러지지 않는다.
    """

    def test_목록으로_준_값이_frozenset이_된다(self) -> None:
        settings = RuntimeSettings().override({"owner_only_channels": ["A", "B", "A"]})
        assert settings.owner_only_channels == frozenset({"A", "B"})
        assert isinstance(settings.owner_only_channels, frozenset)

    def test_집합이_아닌_항목은_그대로_둔다(self) -> None:
        settings = RuntimeSettings().override({"context_limit": {"m": 100}})
        assert settings.context_limit == {"m": 100}


class TestRuntimeSettings지울문구목록:
    """dropped_line_heads — ConfiguredLineDropGuard 가 읽는 설정.

    owner_only_channels 와 같은 이유로 목록이 튜플로 들어와야 타입 계약이
    유지된다. 조직 고유 문구를 코드에 두지 않기 위해 기본값은 빈 목록이다.
    """

    def test_기본값은_빈_목록이다(self) -> None:
        assert RuntimeSettings().dropped_line_heads == ()

    def test_목록으로_준_값이_튜플이_된다(self) -> None:
        settings = RuntimeSettings().override({"dropped_line_heads": ["문구1", "문구2"]})
        assert settings.dropped_line_heads == ("문구1", "문구2")
        assert isinstance(settings.dropped_line_heads, tuple)


class TestStatePaths상태기록:
    def test_상태_기록_파일_경로가_있다(self, tmp_path: Path) -> None:
        """주기적으로 갈아 끼우는 프로세스 상태 파일. 원본 STATE_FILE."""
        from slack_cli_agent.config.paths import StatePaths

        assert StatePaths(tmp_path).state_snapshot == tmp_path / "state.json"
