"""Where the two Slack tokens come from.

Slack needs two separate credentials: a bot token for workspace API calls and
an app token for the Socket Mode connection. Each is resolved on its own, so a
deployment can pass one as an argument and keep the other in a file.

The file is only opened when it is actually the chosen source, which keeps a
deployment that already sets the environment variables from being blocked by a
credentials file it does not use.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..core.errors import ConfigError
from ..core.secrets import redact

if TYPE_CHECKING:
    from ..config.profile import Profile

BOT_TOKEN_ENV = "SLACK_BOT_TOKEN"
APP_TOKEN_ENV = "SLACK_APP_TOKEN"
# 오너 개인 세션 토큰. 봇 토큰으로 못 보는 채널(미초대 공개 채널)을 읽을 때만
# 쓰는 폴백이라 다른 둘과 성질이 다르지만, 같은 파일·같은 우선순위 규칙을
# 쓰는 것이 그 파일이 둘로 갈라지는 것보다 낫다(gaps.md C40).
USER_TOKEN_ENV = "SLACK_USER_TOKEN"
# 브라우저 세션 토큰(xoxc)은 d 쿠키(xoxd)가 함께 가야 통한다. 소유자 턴의
# 링크 선읽기가 쓴다(D5). xoxp 토큰이면 비워 둔다.
USER_COOKIE_ENV = "SLACK_USER_COOKIE"

_ALLOWED_KEYS = frozenset({"bot_token", "app_token", "user_token", "user_cookie"})
# Anything readable by group or other. An 0400 file is stricter than 0600 and
# is accepted; requiring exactly 0600 would reject it.
_SHARED_BITS = stat.S_IRWXG | stat.S_IRWXO
# A credentials file is two short tokens; anything larger is not one.
_MAX_FILE_BYTES = 64 * 1024


@dataclass(frozen=True)
class SlackCredentials:
    bot_token: str = ""
    app_token: str = ""
    user_token: str = ""
    user_cookie: str = ""


class CredentialResolver:
    """Resolves each token from arguments, the environment, then the file.

    The file is read at most once. Reading it per token would let the two
    tokens come from different versions of the same file.
    """

    def __init__(
        self,
        *,
        default_path: Path,
        configured_path: Path | None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        if configured_path is not None and not configured_path.is_absolute():
            raise ConfigError(
                f"credentials_file 은 절대경로여야 한다 : {redact(str(configured_path))}"
            )
        self._default_path = default_path
        self._configured_path = configured_path
        self._env = os.environ if env is None else env
        self._loaded: SlackCredentials | None = None

    def bot_token(self, given: str | None = None) -> str:
        return self._resolve(given, BOT_TOKEN_ENV, "bot_token")

    def app_token(self, given: str | None = None) -> str:
        return self._resolve(given, APP_TOKEN_ENV, "app_token")

    def user_token(self, given: str | None = None) -> str:
        """오너 개인 세션 토큰. 설정돼 있지 않으면 빈 문자열이다 — 그 자체가
        폴백을 끄는 스위치다(gaps.md C40, 기본값 아니오)."""
        return self._resolve(given, USER_TOKEN_ENV, "user_token")

    def user_cookie(self, given: str | None = None) -> str:
        """user_token 이 xoxc 일 때 함께 보내는 d 쿠키 값(xoxd)."""
        return self._resolve(given, USER_COOKIE_ENV, "user_cookie")

    def _resolve(self, given: str | None, env_name: str, field: str) -> str:
        if given:
            return given
        from_env = self._env.get(env_name, "")
        if from_env:
            return from_env
        return str(getattr(self._load(), field))

    def _load(self) -> SlackCredentials:
        if self._loaded is None:
            self._loaded = self._read_file()
        return self._loaded

    def _read_file(self) -> SlackCredentials:
        raw_path = self._configured_path or self._default_path
        # The path itself can carry a token — someone names the file after it
        # (코덱스 리뷰). Every message below prints this, not the real path.
        path = redact(str(raw_path))
        try:
            # O_NOFOLLOW plus fstat on the open descriptor: checking the path
            # and then opening it again would let the file be swapped for a
            # symlink in between. O_NONBLOCK because opening a FIFO read-only
            # blocks until a writer appears, which would hang startup with no
            # log line — the regular-file check runs after the open (코덱스 리뷰).
            fd = os.open(raw_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            if self._configured_path is not None:
                raise ConfigError(f"프로필이 가리킨 자격 파일이 없다 : {path}") from None
            # Nothing configured anywhere is a normal fresh install; whoever
            # needs the token reports that it is missing.
            return SlackCredentials()
        except OSError as exc:
            raise ConfigError(f"자격 파일을 열지 못했다 : {path} ({type(exc).__name__})") from exc
        try:
            self._check_descriptor(fd, path)
            return self._parse(os.read(fd, _MAX_FILE_BYTES + 1), path)
        finally:
            os.close(fd)

    @staticmethod
    def _check_descriptor(fd: int, path: str) -> None:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ConfigError(f"자격 파일이 일반 파일이 아니다 : {path}")
        if info.st_uid != os.getuid():
            raise ConfigError(f"자격 파일의 소유자가 현재 사용자가 아니다 : {path}")
        if info.st_mode & _SHARED_BITS:
            raise ConfigError(
                f"자격 파일을 소유자 말고도 읽을 수 있다. 권한을 600 으로 바꿔라 : {path}"
            )
        if info.st_size > _MAX_FILE_BYTES:
            raise ConfigError(f"자격 파일이 너무 크다 : {path}")

    @staticmethod
    def _parse(payload: bytes, path: str) -> SlackCredentials:
        # Nothing read out of this file reaches an error message, key names
        # included: a token pasted as a key would otherwise print (코덱스 리뷰).
        try:
            data = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConfigError(f"자격 파일을 읽지 못했다 : {path} ({type(exc).__name__})") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"자격 파일의 최상위가 객체가 아니다 : {path}")
        unknown = len(set(data) - _ALLOWED_KEYS)
        if unknown:
            raise ConfigError(
                f"자격 파일에 모르는 키가 {unknown}개 있다. bot_token 과 app_token 만 쓴다 : {path}"
            )
        for key in sorted(data):
            value = data[key]
            if not isinstance(value, str) or not value:
                raise ConfigError(f"자격 파일의 {key} 가 비어 있지 않은 문자열이 아니다 : {path}")
        return SlackCredentials(
            bot_token=str(data.get("bot_token", "")),
            app_token=str(data.get("app_token", "")),
            user_token=str(data.get("user_token", "")),
            user_cookie=str(data.get("user_cookie", "")),
        )


def owner_session_client(resolver: CredentialResolver) -> object | None:
    """소유자 사용자 세션으로 슬랙을 읽는 클라이언트. user_token 이 없으면 None 이고,
    그러면 소유자 턴의 링크 선읽기도 봇 토큰만 쓴다(D5).

    호출하는 쪽이 소유자 턴에서만 쓴다. 이 클라이언트는 소유자가 멤버인 비공개
    대화까지 읽는다.
    """
    token = resolver.user_token()
    if not token:
        return None
    from slack_sdk import WebClient  # lazy import so assembly tests don't need the SDK

    cookie = resolver.user_cookie()
    headers = {"Cookie": "d=" + cookie} if cookie else {}
    return WebClient(token=token, headers=headers)


def resolver_for(profile: Profile, env: Mapping[str, str] | None = None) -> CredentialResolver:
    """One construction site, so every entry point resolves tokens the same way."""
    return CredentialResolver(
        default_path=profile.paths.credentials,
        configured_path=profile.credentials_file,
        env=env,
    )
