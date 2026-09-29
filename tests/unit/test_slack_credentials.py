"""슬랙 토큰 공급원 — 환경변수 말고 자격 파일에서도 읽는다(sca-jl4.4).

우선순위는 토큰마다 따로 적용한다. 명시 인자, 환경변수, 자격 파일 순이다.
자격 파일을 환경변수보다 앞에 두면, 파일이 남아 있는 것을 모른 채 환경변수를
갈아도 무시된다.

자격 파일은 실제로 그것을 고른 경우에만 검사한다. 어기면 거부한다 - 경고하고
기동하면 비밀이 새는 상태를 받아들이는 것이 된다.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import threading
import types
from pathlib import Path

import pytest
from preflight_support import 통과하는_suite

from slack_cli_agent.cli import IngressCommand, InitCommand, SlackCliAgent
from slack_cli_agent.config.profile import Profile, validate_profile_name
from slack_cli_agent.core.application import Application
from slack_cli_agent.core.errors import ConfigError
from slack_cli_agent.core.secrets import contains_secret, redact
from slack_cli_agent.slack.credentials import (
    APP_TOKEN_ENV,
    BOT_TOKEN_ENV,
    CredentialResolver,
    SlackCredentials,
    resolver_for,
)
from slack_cli_agent.web.editor import ProfileEditor


def _자격파일(path: Path, **tokens: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tokens), encoding="utf-8")
    path.chmod(0o600)
    return path


def _해석기(
    tmp_path: Path, env: dict[str, str] | None = None, file: Path | None = None
) -> CredentialResolver:
    return CredentialResolver(
        default_path=file or tmp_path / "credentials.json",
        configured_path=None,
        env=env or {},
    )


class Test우선순위:
    def test_명시_인자가_가장_앞이다(self, tmp_path: Path) -> None:
        _자격파일(tmp_path / "credentials.json", bot_token="파일값")
        해석기 = _해석기(tmp_path, env={BOT_TOKEN_ENV: "환경값"})
        assert 해석기.bot_token(given="인자값") == "인자값"

    def test_환경변수가_자격_파일보다_앞이다(self, tmp_path: Path) -> None:
        _자격파일(tmp_path / "credentials.json", bot_token="파일값")
        assert _해석기(tmp_path, env={BOT_TOKEN_ENV: "환경값"}).bot_token() == "환경값"

    def test_둘_다_없으면_자격_파일에서_읽는다(self, tmp_path: Path) -> None:
        _자격파일(tmp_path / "credentials.json", bot_token="파일값")
        assert _해석기(tmp_path).bot_token() == "파일값"

    def test_아무_데도_없으면_빈_값이다(self, tmp_path: Path) -> None:
        assert _해석기(tmp_path).bot_token() == ""

    def test_토큰마다_따로_고른다(self, tmp_path: Path) -> None:
        """앱 토큰만 인자로 주고 봇 토큰은 파일에서 읽는 조합이 돼야 한다."""
        _자격파일(tmp_path / "credentials.json", bot_token="파일봇")
        해석기 = _해석기(tmp_path)
        assert 해석기.app_token(given="인자앱") == "인자앱"
        assert 해석기.bot_token() == "파일봇"

    def test_빈_인자는_안_고른_것으로_본다(self, tmp_path: Path) -> None:
        _자격파일(tmp_path / "credentials.json", bot_token="파일값")
        assert _해석기(tmp_path).bot_token(given="") == "파일값"

    def test_앱_토큰도_같은_순서다(self, tmp_path: Path) -> None:
        _자격파일(tmp_path / "credentials.json", app_token="파일값")
        assert _해석기(tmp_path, env={APP_TOKEN_ENV: "환경값"}).app_token() == "환경값"


class Test자격_파일_형식:
    def test_모르는_키는_거부한다(self, tmp_path: Path) -> None:
        _자격파일(tmp_path / "credentials.json", bot_token="값", 웹훅="다른 것")
        with pytest.raises(ConfigError):
            _해석기(tmp_path).bot_token()

    def test_문자열이_아니면_거부한다(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        path.write_text(json.dumps({"bot_token": 1234}), encoding="utf-8")
        path.chmod(0o600)
        with pytest.raises(ConfigError):
            _해석기(tmp_path).bot_token()

    def test_최상위가_객체가_아니면_거부한다(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        path.write_text(json.dumps(["값"]), encoding="utf-8")
        path.chmod(0o600)
        with pytest.raises(ConfigError):
            _해석기(tmp_path).bot_token()

    def test_JSON_이_깨졌으면_거부한다(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        path.write_text("{이건 JSON 이 아니다", encoding="utf-8")
        path.chmod(0o600)
        with pytest.raises(ConfigError):
            _해석기(tmp_path).bot_token()

    def test_오류에_토큰도_본문도_안_담는다(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        path.write_text(json.dumps({"bot_token": "xoxb-비밀", "웹훅": "값"}), encoding="utf-8")
        path.chmod(0o600)
        with pytest.raises(ConfigError) as 잡힌것:
            _해석기(tmp_path).bot_token()
        assert "xoxb-비밀" not in str(잡힌것.value)
        assert str(path) in str(잡힌것.value)

    def test_모르는_키_이름을_오류에_안_담는다(self, tmp_path: Path) -> None:
        """토큰을 키 자리에 붙여넣은 경우, 키 이름을 찍으면 그것이 콘솔로
        나간다(코덱스 리뷰).
        """
        path = tmp_path / "credentials.json"
        path.write_text(json.dumps({"xoxb-비밀이-키에": "값"}), encoding="utf-8")
        path.chmod(0o600)
        with pytest.raises(ConfigError) as 잡힌것:
            _해석기(tmp_path).bot_token()
        assert "xoxb-비밀이-키에" not in str(잡힌것.value)
        assert "1개" in str(잡힌것.value)

    def test_이름있는_파이프는_기동을_멈추지_않고_거부한다(self, tmp_path: Path) -> None:
        """읽기 전용 open 은 쓰는 쪽이 생길 때까지 멈춘다. 멈추면 로그 한 줄
        없이 기동이 서므로 O_NONBLOCK 으로 연다 (코덱스 리뷰).
        """
        path = tmp_path / "credentials.json"
        os.mkfifo(path, 0o600)
        결과: list[object] = []

        def 읽기() -> None:
            try:
                _해석기(tmp_path).bot_token()
            except BaseException as exc:  # noqa: BLE001 — 스레드 밖으로 옮겨 단언한다
                결과.append(exc)

        # 별도 스레드로 두는 이유는 멈춤을 실패로 판정하기 위해서다. 같은
        # 스레드에서 부르면 시험 자체가 안 끝나 CI 가 시간 초과로 죽는다.
        스레드 = threading.Thread(target=읽기, daemon=True)
        스레드.start()
        스레드.join(timeout=5.0)
        assert not 스레드.is_alive(), "자격 파일 읽기가 멈췄다"
        assert 결과 and isinstance(결과[0], ConfigError)
        assert "일반 파일이 아니다" in str(결과[0])

    def test_너무_큰_파일은_거부한다(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        path.write_text(json.dumps({"bot_token": "가" * 100_000}), encoding="utf-8")
        path.chmod(0o600)
        with pytest.raises(ConfigError) as 잡힌것:
            _해석기(tmp_path).bot_token()
        # 잘린 내용을 파싱해 실패하는 것과 구분한다. 그러면 사유가 엉뚱하게 남는다.
        assert "너무 크다" in str(잡힌것.value)

    def test_한쪽_토큰만_있어도_된다(self, tmp_path: Path) -> None:
        _자격파일(tmp_path / "credentials.json", bot_token="값")
        해석기 = _해석기(tmp_path)
        assert 해석기.bot_token() == "값"
        assert 해석기.app_token() == ""


class Test파일_권한:
    def test_다른_사람이_읽을_수_있으면_거부한다(self, tmp_path: Path) -> None:
        path = _자격파일(tmp_path / "credentials.json", bot_token="값")
        path.chmod(0o644)
        with pytest.raises(ConfigError):
            _해석기(tmp_path).bot_token()

    def test_다른_사용자_비트만_켜져도_거부한다(self, tmp_path: Path) -> None:
        """0644 는 그룹 비트도 켜져 있어, 그것만으로는 다른 사용자 비트를
        보는지 못 가린다.
        """
        path = _자격파일(tmp_path / "credentials.json", bot_token="값")
        path.chmod(0o604)
        with pytest.raises(ConfigError):
            _해석기(tmp_path).bot_token()

    def test_그룹_비트만_켜져도_거부한다(self, tmp_path: Path) -> None:
        path = _자격파일(tmp_path / "credentials.json", bot_token="값")
        path.chmod(0o640)
        with pytest.raises(ConfigError):
            _해석기(tmp_path).bot_token()

    def test_실행_비트만_켜져도_거부한다(self, tmp_path: Path) -> None:
        path = _자격파일(tmp_path / "credentials.json", bot_token="값")
        path.chmod(0o601)
        with pytest.raises(ConfigError):
            _해석기(tmp_path).bot_token()

    def test_그룹이_읽을_수_있으면_거부한다(self, tmp_path: Path) -> None:
        path = _자격파일(tmp_path / "credentials.json", bot_token="값")
        path.chmod(0o640)
        with pytest.raises(ConfigError):
            _해석기(tmp_path).bot_token()

    def test_읽기_전용_0400_은_받는다(self, tmp_path: Path) -> None:
        """0600 정확히를 요구하면 더 엄격한 설정을 거부하게 된다."""
        path = _자격파일(tmp_path / "credentials.json", bot_token="값")
        path.chmod(0o400)
        assert _해석기(tmp_path).bot_token() == "값"

    def test_심볼릭_링크는_거부한다(self, tmp_path: Path) -> None:
        진짜 = _자격파일(tmp_path / "진짜.json", bot_token="값")
        링크 = tmp_path / "credentials.json"
        링크.symlink_to(진짜)
        with pytest.raises(ConfigError):
            _해석기(tmp_path).bot_token()

    def test_디렉터리는_거부한다(self, tmp_path: Path) -> None:
        (tmp_path / "credentials.json").mkdir()
        with pytest.raises(ConfigError):
            _해석기(tmp_path).bot_token()

    def test_환경변수가_있으면_파일을_아예_안_본다(self, tmp_path: Path) -> None:
        """기존 운영이 자격 파일 때문에 못 뜨면 안 된다."""
        path = _자격파일(tmp_path / "credentials.json", bot_token="값")
        path.chmod(0o666)
        assert _해석기(tmp_path, env={BOT_TOKEN_ENV: "환경값"}).bot_token() == "환경값"


class Test파일_부재:
    def test_기본_자리에_없으면_빈_값이다(self, tmp_path: Path) -> None:
        """설치 직후에는 자격 파일이 없다. 그것은 설정 오류가 아니다."""
        assert _해석기(tmp_path).bot_token() == ""

    def test_프로필이_가리킨_파일이_없으면_오류다(self, tmp_path: Path) -> None:
        """운영자가 경로를 적었는데 그 파일이 없는 것은 설정 오류다."""
        해석기 = CredentialResolver(
            default_path=tmp_path / "credentials.json",
            configured_path=tmp_path / "없는파일.json",
            env={},
        )
        with pytest.raises(ConfigError):
            해석기.bot_token()

    def test_프로필이_가리킨_파일을_기본보다_먼저_본다(self, tmp_path: Path) -> None:
        _자격파일(tmp_path / "credentials.json", bot_token="기본값")
        _자격파일(tmp_path / "따로.json", bot_token="지정값")
        해석기 = CredentialResolver(
            default_path=tmp_path / "credentials.json",
            configured_path=tmp_path / "따로.json",
            env={},
        )
        assert 해석기.bot_token() == "지정값"

    def test_상대경로를_가리키면_오류다(self, tmp_path: Path) -> None:
        """Profile 이 원본 파일 경로를 안 갖고 있어 기준을 정할 수 없다."""
        with pytest.raises(ConfigError):
            CredentialResolver(
                default_path=tmp_path / "credentials.json",
                configured_path=Path("상대/경로.json"),
                env={},
            )


class Test파일을_한_번만_읽는다:
    def test_두_토큰을_읽어도_파일은_한_번만_연다(self, tmp_path: Path) -> None:
        """두 번 읽으면 그 사이에 바뀐 파일로 봇 토큰과 앱 토큰이 어긋난다."""
        path = _자격파일(tmp_path / "credentials.json", bot_token="봇", app_token="앱")
        해석기 = _해석기(tmp_path)
        assert 해석기.bot_token() == "봇"
        os.remove(path)
        assert 해석기.app_token() == "앱"


class TestSlackCredentials값:
    def test_없는_토큰은_빈_문자열이다(self) -> None:
        assert SlackCredentials().bot_token == ""
        assert SlackCredentials().app_token == ""


class Test토큰_형태_탐지:
    def test_워크플로_토큰도_거부한다(self) -> None:
        """xwfp 는 슬랙 공식 토큰 접두사인데 정규식에 없었다 (코덱스 리뷰)."""
        with pytest.raises(ConfigError):
            Profile.from_dict(
                {
                    "name": "봇",
                    "primary_engine": {"type": "claude", "binary": "/bin/echo", "model": "m"},
                    "owner_dm": "xwfp-비밀값",
                }
            )

    def test_알려진_접두사를_모두_본다(self) -> None:
        for 접두사 in (
            "xoxb",
            "xoxp",
            "xoxa",
            "xoxr",
            "xoxs",
            "xoxe",
            "xoxc",
            "xoxd",
            "xapp",
            "xwfp",
        ):
            assert contains_secret(f"{접두사}-비밀값"), 접두사

    def test_비슷한_문자열은_안_건드린다(self) -> None:
        assert not contains_secret("xoxb")
        assert not contains_secret("보통 문장이다")
        assert redact("보통 경로/파일.json") == "보통 경로/파일.json"


class Testinit_명령:
    """state_dir 은 프로필 JSON 에 그대로 들어간다. 쓰기 전에 막지 않으면
    프로필 값 검사를 우회한다 (코덱스 리뷰).
    """

    def _실행(self, tmp_path: Path, **인자: str) -> tuple[int, str]:
        parser = argparse.ArgumentParser()
        command = InitCommand()
        command.add_arguments(parser)
        원시 = [
            "--name",
            인자.get("name", "봇"),
            "--profile-dir",
            인자.get("profile_dir", str(tmp_path)),
        ]
        if "state_dir" in 인자:
            원시 += ["--state-dir", 인자["state_dir"]]
        출력 = io.StringIO()
        return command.execute(parser.parse_args(원시), 출력), 출력.getvalue()

    def test_토큰이_박힌_상태_디렉터리를_거부한다(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError) as 잡힌것:
            self._실행(tmp_path, state_dir=str(tmp_path / "xoxb-비밀값"))
        assert "xoxb-비밀값" not in str(잡힌것.value)
        assert not list(tmp_path.glob("*.json"))

    def test_토큰이_박힌_이름을_거부한다(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError):
            self._실행(tmp_path, name="xapp-비밀값")

    def test_보통_경로는_그대로_만든다(self, tmp_path: Path) -> None:
        코드, 출력 = self._실행(tmp_path, state_dir=str(tmp_path / "상태"))
        assert 코드 == 0
        assert (tmp_path / "봇.json").is_file()
        assert "상태" in 출력


class Test프로필_이름_검사:
    """이름이 파일 이름이 된다. 웹 콘솔은 URL 에서 그대로 받는다 (코덱스 리뷰)."""

    def test_토큰_형태_이름을_거부한다(self) -> None:
        with pytest.raises(ConfigError):
            validate_profile_name("xoxb-비밀값")

    def test_경로_구분자를_거부한다(self) -> None:
        for 이름 in ("../탈출", "가/나", "..", ".숨김", ""):
            with pytest.raises(ConfigError):
                validate_profile_name(이름)

    def test_보통_이름은_통과한다(self) -> None:
        for 이름 in ("봇", "asuka", "bot-2", "bot_2"):
            assert validate_profile_name(이름) == 이름

    @staticmethod
    def _저장가능(tmp_path: Path) -> dict[str, object]:
        """이름 검사가 없으면 실제로 저장되는 내용이어야 한다. 검증에서 걸리는
        내용을 쓰면 이름과 무관하게 안 써져 시험이 헛돈다.
        """
        return {
            "name": "봇",
            "state_dir": str(tmp_path),
            "work_root": str(tmp_path / "작업"),
            "owner_user_id": "U1",
            "troubleshoot_channel": "C1",
            "primary_engine": {"type": "claude", "binary": "/bin/echo", "model": "m"},
        }

    def test_보통_이름이면_저장된다(self, tmp_path: Path) -> None:
        assert ProfileEditor([tmp_path]).save("정상이름", self._저장가능(tmp_path)) == []
        assert (tmp_path / "정상이름.json").is_file()

    def test_웹_콘솔이_토큰_이름으로_저장하지_않는다(self, tmp_path: Path) -> None:
        오류 = ProfileEditor([tmp_path]).save("xoxb-비밀값", self._저장가능(tmp_path))
        assert 오류
        assert not list(tmp_path.glob("*.json"))
        assert "xoxb-비밀값" not in " ".join(오류)

    def test_웹_콘솔이_검색_디렉터리_밖에_저장하지_않는다(self, tmp_path: Path) -> None:
        안 = tmp_path / "프로필"
        안.mkdir()
        밖 = tmp_path / "밖.json"
        assert ProfileEditor([안]).save("../밖", self._저장가능(tmp_path))
        assert not 밖.exists()

    def test_웹_콘솔이_디렉터리_밖을_읽지_않는다(self, tmp_path: Path) -> None:
        안 = tmp_path / "프로필"
        안.mkdir()
        (tmp_path / "밖.json").write_text("{}", encoding="utf-8")
        with pytest.raises(ConfigError):
            ProfileEditor([안]).read("../밖")


class Test프로필_목록과_조회:
    def test_조회가_검색_디렉터리_밖을_안_읽는다(self, tmp_path: Path) -> None:
        안 = tmp_path / "프로필"
        안.mkdir()
        (tmp_path / "밖.json").write_text(
            json.dumps(
                {
                    "name": "밖",
                    "state_dir": str(tmp_path),
                    "primary_engine": {"type": "claude", "binary": "/bin/echo", "model": "m"},
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ConfigError):
            Profile.load("../밖", [안])

    def test_목록이_토큰_형태_파일명을_안_낸다(self, tmp_path: Path) -> None:
        """손으로 둔 파일은 아무 이름이나 가질 수 있다 (코덱스 리뷰)."""
        (tmp_path / "xoxb-비밀값.json").write_text("{}", encoding="utf-8")
        (tmp_path / "정상봇.json").write_text("{}", encoding="utf-8")
        assert Profile.discover([tmp_path]) == ["정상봇"]


class Test프로필_조회_오류:
    def test_검색_경로의_토큰을_가린다(self, tmp_path: Path) -> None:
        """프로필을 읽기 전 단계라 값 검사가 못 본다 (코덱스 리뷰)."""
        with pytest.raises(ConfigError) as 잡힌것:
            Profile.load("없는봇", [tmp_path / "xoxb-비밀값"])
        assert "xoxb-비밀값" not in str(잡힌것.value)

    def test_프로필_이름의_토큰도_가린다(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError) as 잡힌것:
            Profile.load("xapp-비밀값", [tmp_path])
        assert "xapp-비밀값" not in str(잡힌것.value)


class Test오류_메시지_가림:
    """프로필을 안 거치고 해석기를 직접 만드는 경로가 있다. 그 자리의 오류도
    경로를 그대로 출력하면 토큰이 로그로 샌다 (코덱스 리뷰).
    """

    def test_없는_파일_오류가_경로의_토큰을_가린다(self, tmp_path: Path) -> None:
        비밀경로 = tmp_path / "xoxb-비밀값-cred.json"
        해석기 = CredentialResolver(
            default_path=tmp_path / "credentials.json", configured_path=비밀경로, env={}
        )
        with pytest.raises(ConfigError) as 잡힌것:
            해석기.bot_token()
        assert "xoxb-비밀값" not in str(잡힌것.value)
        assert "***" in str(잡힌것.value)

    def test_권한_오류도_경로의_토큰을_가린다(self, tmp_path: Path) -> None:
        비밀경로 = _자격파일(tmp_path / "xapp-비밀값-cred.json", bot_token="값")
        비밀경로.chmod(0o604)
        해석기 = CredentialResolver(
            default_path=tmp_path / "credentials.json", configured_path=비밀경로, env={}
        )
        with pytest.raises(ConfigError) as 잡힌것:
            해석기.bot_token()
        assert "xapp-비밀값" not in str(잡힌것.value)

    def test_상대경로_거부에서도_가린다(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError) as 잡힌것:
            CredentialResolver(
                default_path=tmp_path / "credentials.json",
                configured_path=Path("xoxb-비밀값/cred.json"),
                env={},
            )
        assert "xoxb-비밀값" not in str(잡힌것.value)


class Test프로필_스키마:
    """프로필 JSON 은 경로만 가리키고 토큰 자체는 못 갖는다.

    .gitignore 는 우발 커밋을 줄일 뿐 강제 추가·복사·백업을 못 막고, 웹 콘솔이
    프로필 JSON 을 읽고 다시 저장하므로 노출 면이 는다(코덱스 설계 논의).
    """

    def _프로필(self, **extra: object) -> dict[str, object]:
        base: dict[str, object] = {
            "name": "봇",
            "primary_engine": {"type": "claude", "binary": "/bin/echo", "model": "m"},
        }
        base.update(extra)
        return base

    def test_봇_토큰_키를_거부한다(self) -> None:
        with pytest.raises(ConfigError):
            Profile.from_dict(self._프로필(bot_token="xoxb-비밀"))

    def test_앱_토큰_키를_거부한다(self) -> None:
        with pytest.raises(ConfigError):
            Profile.from_dict(self._프로필(app_token="xapp-비밀"))

    def test_슬랙_토큰_계열_키를_거부한다(self) -> None:
        with pytest.raises(ConfigError):
            Profile.from_dict(self._프로필(slack_user_token="xoxc-비밀"))

    def test_대문자_키도_거부한다(self) -> None:
        """SLACK_BOT_TOKEN 은 실제 환경변수 이름이라 그대로 적기 쉽다."""
        with pytest.raises(ConfigError):
            Profile.from_dict(self._프로필(SLACK_BOT_TOKEN="xoxb-비밀"))

    def test_섞인_대소문자_키도_거부한다(self) -> None:
        with pytest.raises(ConfigError):
            Profile.from_dict(self._프로필(Bot_Token="xoxb-비밀"))

    def test_중첩_블록의_토큰_키도_거부한다(self) -> None:
        """settings 는 그대로 보존되므로 최상위만 보면 거기로 새어 들어간다
        (코덱스 리뷰).
        """
        with pytest.raises(ConfigError):
            Profile.from_dict(self._프로필(settings={"bot_token": "값"}))

    def test_더_깊은_중첩도_거부한다(self) -> None:
        with pytest.raises(ConfigError):
            Profile.from_dict(self._프로필(settings={"슬랙": {"app_token": "값"}}))

    def test_접두사가_없는_토큰_키도_거부한다(self) -> None:
        """user_token · api_token · token 은 접두사 목록에 안 걸린다."""
        for key in ("user_token", "api_token", "token"):
            with pytest.raises(ConfigError):
                Profile.from_dict(self._프로필(**{key: "값"}))

    def test_키_이름이_달라도_슬랙_토큰_값을_거부한다(self) -> None:
        """키 이름 규칙은 이름을 바꾸면 끝난다. 값 형태로도 본다."""
        with pytest.raises(ConfigError):
            Profile.from_dict(self._프로필(owner_dm="xoxb-비밀"))

    def test_토큰을_키_자리에_붙여넣어도_거부한다(self) -> None:
        with pytest.raises(ConfigError):
            Profile.from_dict(self._프로필(settings={"xoxb-비밀": True}))

    def test_목록_안의_토큰_값도_거부한다(self) -> None:
        with pytest.raises(ConfigError):
            Profile.from_dict(self._프로필(plugins=["정상", "xapp-비밀"]))

    def test_mcp_서버의_env_키_이름은_막지_않는다(self) -> None:
        """MCP 서버는 자기 자격을 env 로 받는 것 말고 다른 경로가 없다.
        슬랙 토큰 형태의 값은 그래도 거부한다.
        """
        서버 = {"깃헙": {"command": "/bin/echo", "env": {"GITHUB_TOKEN": "ghp_값"}}}
        assert Profile.from_dict(self._프로필(mcp_servers=서버)).mcp_servers
        새는것 = {"슬랙": {"command": "/bin/echo", "env": {"X": "xoxb-비밀"}}}
        with pytest.raises(ConfigError):
            Profile.from_dict(self._프로필(mcp_servers=새는것))

    def test_값_가운데_박힌_토큰도_거부한다(self) -> None:
        """startswith 만 보면 Bearer 접두나 URL 쿼리를 그대로 통과시킨다
        (코덱스 리뷰).
        """
        새는것 = {"슬랙": {"command": "/bin/echo", "env": {"X": "Bearer xoxb-비밀값"}}}
        with pytest.raises(ConfigError):
            Profile.from_dict(self._프로필(mcp_servers=새는것))

    def test_URL_쿼리의_토큰도_거부한다(self) -> None:
        서버 = {"원격": {"url": "https://example.com/mcp?access_token=xoxp-비밀값"}}
        with pytest.raises(ConfigError):
            Profile.from_dict(self._프로필(mcp_servers=서버))

    def test_경로에_토큰이_박힌_credentials_file_을_거부한다(self) -> None:
        """파일 이름을 토큰으로 지은 경우다. 값 검사가 문자열 어디든 보므로
        프로필 단계에서 걸린다 (코덱스 리뷰).
        """
        with pytest.raises(ConfigError) as 잡힌것:
            Profile.from_dict(self._프로필(credentials_file="/tmp/xoxb-비밀값-cred.json"))
        assert "xoxb-비밀값" not in str(잡힌것.value)

    def test_중첩_거부_메시지에도_토큰이_안_담긴다(self) -> None:
        with pytest.raises(ConfigError) as 잡힌것:
            Profile.from_dict(self._프로필(settings={"xoxb-비밀": True}))
        assert "xoxb-비밀" not in str(잡힌것.value)

    def test_상대경로를_가리키면_프로필_단계에서_거부한다(self) -> None:
        """웹 콘솔이 프로필을 저장할 때 걸러야 한다. resolver 를 만들 때만
        보면 기동 불가능한 프로필이 저장에 성공한다(코덱스 리뷰).
        """
        with pytest.raises(ConfigError):
            Profile.from_dict(self._프로필(credentials_file="상대/경로.json"))

    def test_거부_메시지에_토큰이_안_담긴다(self) -> None:
        with pytest.raises(ConfigError) as 잡힌것:
            Profile.from_dict(self._프로필(bot_token="xoxb-비밀"))
        assert "xoxb-비밀" not in str(잡힌것.value)

    def test_경로_참조는_받는다(self, tmp_path: Path) -> None:
        프로필 = Profile.from_dict(
            self._프로필(credentials_file=str(tmp_path / "자격.json")),
        )
        assert 프로필.credentials_file == tmp_path / "자격.json"

    def test_경로를_안_적으면_없음이다(self) -> None:
        assert Profile.from_dict(self._프로필()).credentials_file is None

    def test_틸데를_펼친다(self) -> None:
        프로필 = Profile.from_dict(self._프로필(credentials_file="~/자격.json"))
        assert 프로필.credentials_file == Path.home() / "자격.json"

    def test_기본_자격_파일_경로는_상태_디렉터리_안이다(self, tmp_path: Path) -> None:
        프로필 = Profile.from_dict(self._프로필(state_dir=str(tmp_path)))
        assert 프로필.paths.credentials == tmp_path / "credentials.json"


class Test진입점_조립:
    """만든 것과 거기에 연결한 것은 다르다."""

    def _프로필(self, tmp_path: Path) -> Profile:
        return Profile.from_dict(
            {
                "name": "봇",
                "state_dir": str(tmp_path),
                "primary_engine": {"type": "claude", "binary": "/bin/echo", "model": "m"},
            }
        )

    def test_프로필로_해석기를_만든다(self, tmp_path: Path) -> None:
        해석기 = resolver_for(self._프로필(tmp_path), env={})
        _자격파일(tmp_path / "credentials.json", bot_token="파일값")
        assert 해석기.bot_token() == "파일값"

    def test_프로필의_credentials_file_을_따른다(self, tmp_path: Path) -> None:
        따로 = _자격파일(tmp_path / "따로.json", bot_token="지정값")
        프로필 = Profile.from_dict(
            {
                "name": "봇",
                "state_dir": str(tmp_path),
                "credentials_file": str(따로),
                "primary_engine": {"type": "claude", "binary": "/bin/echo", "model": "m"},
            }
        )
        assert resolver_for(프로필, env={}).bot_token() == "지정값"

    def test_애플리케이션이_자격_파일의_봇_토큰을_쓴다(self, tmp_path: Path) -> None:
        _자격파일(tmp_path / "credentials.json", bot_token="파일봇")
        받은: dict[str, str] = {}

        class 가짜클라이언트:
            def __init__(self, token: str = "") -> None:
                받은["token"] = token

        app = Application.from_profile(
            self._프로필(tmp_path),
            client=가짜클라이언트(),
            env={},
        )
        try:
            assert app.bot_token() == "파일봇"
        finally:
            app.close()

    def test_ingress_가_자격_파일의_앱_토큰을_쓴다(self, tmp_path: Path) -> None:
        _자격파일(tmp_path / "credentials.json", app_token="파일앱")
        assert resolver_for(self._프로필(tmp_path), env={}).app_token() == "파일앱"


class Test명령_안내:
    def test_init_이_자격_파일_자리를_안내한다(self, tmp_path: Path) -> None:
        """자동 생성은 안 한다. 빈 파일만 만들면 그 파일이 있다는 것과 토큰이
        채워졌다는 것이 구분되지 않는다(코덱스 설계 논의).
        """
        out = io.StringIO()
        상태 = tmp_path / "state"
        SlackCliAgent().run(
            ["init", "--name", "봇", "--profile-dir", str(tmp_path), "--state-dir", str(상태)],
            stdout=out,
        )
        assert str(상태 / "credentials.json") in out.getvalue()
        assert not (상태 / "credentials.json").exists()

    def test_ingress_가_자격_파일을_한_번만_읽는다(self, tmp_path: Path) -> None:
        """앱 토큰은 cli 가, 봇 토큰은 Application 이 읽는다. 각자 resolver 를
        만들면 파일이 그 사이에 바뀔 때 두 토큰이 다른 판본에서 나온다
        (코덱스 리뷰).
        """
        _자격파일(tmp_path / "credentials.json", bot_token="봇값", app_token="앱값")
        받은: list[object] = []

        class 가짜앱:
            def gateway(self) -> object:
                return type("게이트웨이", (), {"start": lambda _s, _t: None})()

            def ingress(self) -> object:
                return type("등록", (), {"register": lambda _s, _g: None})()

            def connection_watch(self) -> None: ...

            def self_restarter(self) -> None:
                return None

            def ingress_services(self, _restarter: object) -> object:
                return contextlib.nullcontext()

            def close(self) -> None: ...

        def 공장(_profile: Profile, resolver: object) -> 가짜앱:
            받은.append(resolver)
            return 가짜앱()

        parser = argparse.ArgumentParser()
        # 게이트는 이 시험의 대상이 아니다. 자격 해석기 전달만 본다.
        command = IngressCommand(공장, preflight_suite_factory=통과하는_suite)
        command.add_arguments(parser)
        프로필_자리 = tmp_path / "프로필자리"
        프로필_자리.mkdir()
        (프로필_자리 / "봇.json").write_text(
            json.dumps(
                {
                    "name": "봇",
                    "state_dir": str(tmp_path),
                    "primary_engine": {"type": "claude", "binary": "/bin/echo", "model": "m"},
                }
            ),
            encoding="utf-8",
        )
        args = parser.parse_args(["--profile", "봇", "--profile-dir", str(프로필_자리)])
        assert command.execute(args, io.StringIO()) == 0
        # 앱 토큰을 읽은 그 해석기가 그대로 넘어가야 한다. 새로 만들면 파일을
        # 다시 읽어 두 토큰이 다른 판본에서 나온다.
        assert 받은 and isinstance(받은[0], CredentialResolver)
        os.remove(tmp_path / "credentials.json")
        assert 받은[0].bot_token() == "봇값"  # type: ignore[attr-defined]

    def test_클라이언트를_안_주면_자격_파일의_봇_토큰으로_만든다(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        프로필 = Profile.from_dict(
            {
                "name": "봇",
                "state_dir": str(tmp_path),
                "primary_engine": {"type": "claude", "binary": "/bin/echo", "model": "m"},
            }
        )
        _자격파일(tmp_path / "credentials.json", bot_token="봇값", app_token="앱값")
        받은: list[str] = []

        class 가짜클라이언트:
            def __init__(self, token: str = "") -> None:
                받은.append(token)

        # slack_sdk 는 시험 환경에 없다. 실제 import 자리가 도는 것을 보려면
        # 모듈 자체를 세워야 한다.
        가짜모듈 = types.ModuleType("slack_sdk")
        가짜모듈.WebClient = 가짜클라이언트  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "slack_sdk", 가짜모듈)
        app = Application.from_profile(프로필, env={})
        try:
            assert 받은 == ["봇값"]
        finally:
            app.close()

    def test_ingress_가_자격_파일에서_앱_토큰을_찾는다(self, tmp_path: Path) -> None:
        프로필 = tmp_path / "봇.json"
        프로필.write_text(
            json.dumps(
                {
                    "name": "봇",
                    "state_dir": str(tmp_path / "state"),
                    "primary_engine": {"type": "claude", "binary": "/bin/echo", "model": "m"},
                }
            ),
            encoding="utf-8",
        )
        # 봇 토큰도 넣는다 - 접수기는 게이트에서 둘 다 본다(sca-q2k).
        _자격파일(tmp_path / "state" / "credentials.json", bot_token="파일봇", app_token="파일앱")
        받은: list[str] = []

        class 가짜게이트웨이:
            def start(self, token: str) -> None:
                받은.append(token)

        class 가짜앱:
            def gateway(self) -> 가짜게이트웨이:
                return 가짜게이트웨이()

            def ingress(self) -> object:
                return type("등록", (), {"register": lambda _self, _g: None})()

            def connection_watch(self) -> None:
                pass

            def self_restarter(self) -> None:
                return None

            def ingress_services(self, _restarter: object) -> object:
                return contextlib.nullcontext()

            def close(self) -> None:
                pass

        parser = argparse.ArgumentParser()
        # 게이트는 이 시험의 대상이 아니다. 자격 해석기 전달만 본다.
        command = IngressCommand(
            lambda _profile, _resolver: 가짜앱(), preflight_suite_factory=통과하는_suite
        )
        command.add_arguments(parser)
        args = parser.parse_args(["--profile", "봇", "--profile-dir", str(tmp_path)])
        assert command.execute(args, io.StringIO()) == 0
        assert 받은 == ["파일앱"]
