"""프로필 검색 경로 기본값 — 설치해 쓰는 형태에 맞춘다(sca-jl4.3).

기본값이 현재 디렉터리면 설치물로 쓸 때 어디서 띄우느냐에 따라 프로필을
못 찾는다. 환경변수, 사용자 설정 디렉터리, 현재 디렉터리 순으로 본다.
세 진입점(preflight 계열·init·console)이 같은 함수를 쓴다.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from collections.abc import Sequence
from pathlib import Path

import pytest

from slack_cli_agent.cli import SlackCliAgent, WebCommand
from slack_cli_agent.config.paths import (
    PROFILE_DIR_ENV,
    default_profile_dirs,
    default_profile_write_dir,
    user_profile_dir,
)
from slack_cli_agent.config.profile import Profile
from slack_cli_agent.core.errors import ConfigError


class Test검색_경로_순서:
    def test_환경변수가_맨_앞이다(self) -> None:
        dirs = default_profile_dirs(
            env={PROFILE_DIR_ENV: "/tmp/개인프로필"}, home=Path("/home/x"), cwd=Path("/work"),
        )
        assert dirs[0] == Path("/tmp/개인프로필")

    def test_환경변수는_여러_경로를_받는다(self) -> None:
        dirs = default_profile_dirs(
            env={PROFILE_DIR_ENV: "/a:/b"}, home=Path("/home/x"), cwd=Path("/work"),
        )
        assert dirs[:2] == (Path("/a"), Path("/b"))

    def test_환경변수의_빈_조각은_버린다(self) -> None:
        dirs = default_profile_dirs(
            env={PROFILE_DIR_ENV: "/a::"}, home=Path("/home/x"), cwd=Path("/work"),
        )
        assert dirs[0] == Path("/a")
        assert Path("") not in dirs

    def test_환경변수가_없으면_사용자_설정_디렉터리가_앞이다(self) -> None:
        dirs = default_profile_dirs(env={}, home=Path("/home/x"), cwd=Path("/work"))
        assert dirs[0] == Path("/home/x/.config/slack-cli-agent/profiles")

    def test_현재_디렉터리가_맨_뒤에_남는다(self) -> None:
        """저장소 안에서 띄우는 기존 사용법을 깨지 않는다."""
        dirs = default_profile_dirs(env={}, home=Path("/home/x"), cwd=Path("/work"))
        assert dirs[-1] == Path("/work")

    def test_XDG_CONFIG_HOME_을_따른다(self) -> None:
        dirs = default_profile_dirs(
            env={"XDG_CONFIG_HOME": "/xdg"}, home=Path("/home/x"), cwd=Path("/work"),
        )
        assert dirs[0] == Path("/xdg/slack-cli-agent/profiles")

    def test_XDG_CONFIG_HOME_이_상대경로면_무시한다(self) -> None:
        """XDG 규격은 절대경로가 아니면 무시하라고 정한다."""
        dirs = default_profile_dirs(
            env={"XDG_CONFIG_HOME": "상대"}, home=Path("/home/x"), cwd=Path("/work"),
        )
        assert dirs[0] == Path("/home/x/.config/slack-cli-agent/profiles")

    def test_틸데를_펼친다(self) -> None:
        dirs = default_profile_dirs(
            env={PROFILE_DIR_ENV: "~/프로필"}, home=Path("/home/x"), cwd=Path("/work"),
        )
        assert dirs[0] == Path.home() / "프로필"

    def test_중복_경로는_한_번만_넣는다(self) -> None:
        dirs = default_profile_dirs(
            env={PROFILE_DIR_ENV: "/work"}, home=Path("/home/x"), cwd=Path("/work"),
        )
        assert dirs.count(Path("/work")) == 1


class Test쓰기_자리:
    def test_환경변수의_첫_경로에_쓴다(self) -> None:
        assert default_profile_write_dir(
            env={PROFILE_DIR_ENV: "/a:/b"}, home=Path("/home/x"),
        ) == Path("/a")

    def test_환경변수가_없으면_사용자_설정_디렉터리다(self) -> None:
        """현재 디렉터리에 쓰면 검색 기본값이 그것을 맨 뒤에서만 보므로,
        init 이 만든 프로필이 다른 프로필에 가릴 수 있다."""
        assert default_profile_write_dir(env={}, home=Path("/home/x")) == Path(
            "/home/x/.config/slack-cli-agent/profiles"
        )

    def test_쓰기_자리가_검색_경로_안에_있다(self) -> None:
        env = {PROFILE_DIR_ENV: "/a:/b"}
        assert default_profile_write_dir(env=env, home=Path("/home/x")) in default_profile_dirs(
            env=env, home=Path("/home/x"), cwd=Path("/work"),
        )


class Test사용자_설정_디렉터리:
    def test_경로를_만든다(self) -> None:
        assert user_profile_dir(env={}, home=Path("/home/x")) == Path(
            "/home/x/.config/slack-cli-agent/profiles"
        )


class Test진입점_조립:
    """세 진입점이 같은 기본값을 쓰는지 본다. 함수를 만든 것과 거기에 연결한
    것은 다르다.
    """

    def test_preflight_가_환경변수의_프로필을_찾는다(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        프로필들 = tmp_path / "프로필"
        프로필들.mkdir()
        (프로필들 / "봇.json").write_text(
            json.dumps(_뼈대(tmp_path / "state")), encoding="utf-8",
        )
        빈자리 = tmp_path / "빈자리"
        빈자리.mkdir()
        monkeypatch.setenv(PROFILE_DIR_ENV, str(프로필들))
        monkeypatch.chdir(빈자리)
        out = io.StringIO()
        code = SlackCliAgent().run(["preflight", "--profile", "봇"], stdout=out)
        # 점검 자체는 통과해도 되고 실패해도 된다. 확인하는 것은 프로필을 읽어
        # 점검을 돌렸다는 것이다. 못 찾으면 ConfigError 를 잡아 2 가 나오고
        # 점검 보고가 한 줄도 안 나온다(코덱스 2차 리뷰).
        assert code != 2, out.getvalue()
        assert "기동" in out.getvalue()

    def test_init_이_환경변수의_첫_경로에_쓴다(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        목표 = tmp_path / "설정자리"
        monkeypatch.setenv(PROFILE_DIR_ENV, f"{목표}{os.pathsep}{tmp_path / '뒤'}")
        monkeypatch.chdir(tmp_path)
        out = io.StringIO()
        code = SlackCliAgent().run(
            ["init", "--name", "봇", "--state-dir", str(tmp_path / "state")], stdout=out,
        )
        assert code == 0
        assert (목표 / "봇.json").is_file()
        assert not (tmp_path / "봇.json").exists()

    def test_web_이_같은_검색_경로를_쓴다(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(PROFILE_DIR_ENV, str(tmp_path / "프로필"))
        monkeypatch.chdir(tmp_path)
        받은: list[Sequence[Path]] = []

        class 가짜서버:
            port = 1

            def start(self) -> None:
                받은.append(검색경로)
                raise KeyboardInterrupt

            def serve_forever(self) -> None:
                pass

            def stop(self) -> None:
                pass

        검색경로: Sequence[Path] = ()

        def factory(dirs: Sequence[Path], port: int) -> 가짜서버:
            nonlocal 검색경로
            검색경로 = dirs
            return 가짜서버()

        command = WebCommand(factory)
        parser = argparse.ArgumentParser()
        command.add_arguments(parser)
        args = parser.parse_args([])
        with contextlib.suppress(KeyboardInterrupt):
            command.execute(args, io.StringIO())
        assert 검색경로[0] == tmp_path / "프로필"
        assert 검색경로[-1] == tmp_path


class Test읽기_실패는_어느_파일인지_알려준다:
    """설치 직후 사람이 처음 만나는 오류다. 파일을 못 짚으면 고칠 자리를 모른다.

    2026-09-17 실측 - init 이 만든 뼈대는 model 이 비어 있어 그대로는 안 읽힌다.
    그때 나오던 문구가 "엔진 설정에 model 가 없다" 뿐이라, 프로필 파일이 여러
    검색 경로에 흩어져 있으면 어느 것을 고쳐야 하는지 알 수 없었다.
    """

    def test_값이_빠지면_프로필_경로를_함께_낸다(self, tmp_path: Path) -> None:
        경로 = tmp_path / "봇.json"
        경로.write_text(
            json.dumps({"name": "봇", "primary_engine": {"type": "claude", "binary": "/bin/echo"}}),
            encoding="utf-8",
        )
        with pytest.raises(ConfigError) as 잡힌것:
            Profile.load("봇", [tmp_path])
        문구 = str(잡힌것.value)
        assert str(경로) in 문구, 문구
        assert "model" in 문구, 문구

    def test_JSON_이_깨졌으면_ConfigError_로_낸다(self, tmp_path: Path) -> None:
        """지금은 JSONDecodeError 가 그대로 올라와 역추적이 통째로 찍힌다."""
        경로 = tmp_path / "봇.json"
        경로.write_text("{", encoding="utf-8")
        with pytest.raises(ConfigError) as 잡힌것:
            Profile.load("봇", [tmp_path])
        assert str(경로) in str(잡힌것.value)

    def test_깨진_프로필로_preflight_를_돌리면_역추적이_아니라_한_줄로_막힌다(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "봇.json").write_text("{", encoding="utf-8")
        out = io.StringIO()
        결과 = SlackCliAgent().run(
            ["preflight", "--profile", "봇", "--profile-dir", str(tmp_path)], stdout=out
        )
        assert 결과 != 0



def _뼈대(state_dir: Path) -> dict[str, object]:
    return {
        "name": "봇",
        "state_dir": str(state_dir),
        "primary_engine": {"type": "claude", "binary": "/bin/echo", "model": "m"},
    }
