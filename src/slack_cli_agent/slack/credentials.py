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
import logging
import os
import stat
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..core.errors import ConfigError
from ..core.secrets import redact

if TYPE_CHECKING:
    from ..config.profile import Profile

log = logging.getLogger(__name__)

BOT_TOKEN_ENV = "SLACK_BOT_TOKEN"
APP_TOKEN_ENV = "SLACK_APP_TOKEN"
# 오너 개인 세션 토큰. 봇 토큰으로 못 보는 채널(미초대 공개 채널)을 읽을 때만
# 쓰는 폴백이라 다른 둘과 성질이 다르지만, 같은 파일·같은 우선순위 규칙을
# 쓰는 것이 그 파일이 둘로 갈라지는 것보다 낫다(gaps.md C40).
USER_TOKEN_ENV = "SLACK_USER_TOKEN"
# 브라우저 세션 토큰(xoxc)은 d 쿠키(xoxd)가 함께 가야 통한다. 소유자 턴의
# 링크 선읽기가 쓴다(D5). xoxp 토큰이면 비워 둔다.
USER_COOKIE_ENV = "SLACK_USER_COOKIE"
# 소유자 세션 토큰의 보관 원천은 macOS 키체인이다. 자격 파일과 환경이 비면
# 여기서 읽는다. 이 값이 빈 문자열이면 키체인을 보지 않는다. 2026-11-28 12:03
# 부터 11-29 01:19 까지 자격 파일에 user_token 이 없어 소유자 세션이 꺼진 채
# 돌았고, 키체인에는 값이 있었다.
USER_KEYCHAIN_ENV = "SLACK_USER_KEYCHAIN_SERVICE"
#: 키체인 서비스 하나에 계정 이름으로 두 값이 들어 있다.
KEYCHAIN_ACCOUNTS = {"user_token": "xoxc", "user_cookie": "xoxd"}

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


def read_keychain_item(service: str, account: str) -> str:
    """macOS 키체인의 한 항목 값. 없으면 빈 문자열이다.

    값은 반환만 하고 로그에 남기지 않는다. 실패도 종료 코드로만 본다.
    """
    try:
        done = subprocess.run(  # noqa: S603 -- 인자가 고정된 security 명령이다
            ["/usr/bin/security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if done.returncode != 0:
        return ""
    return done.stdout.strip()


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
        # 키체인에서 한 항목을 읽는다. (서비스, 계정) 을 받아 값이나 빈
        # 문자열을 낸다. 테스트가 security 명령 없이 돌게 주입으로 둔다.
        keychain_reader: Callable[[str, str], str] | None = None,
    ) -> None:
        if configured_path is not None and not configured_path.is_absolute():
            raise ConfigError(
                f"credentials_file 은 절대경로여야 한다 : {redact(str(configured_path))}"
            )
        self._default_path = default_path
        self._configured_path = configured_path
        self._env = os.environ if env is None else env
        self._loaded: SlackCredentials | None = None
        self._keychain_reader = keychain_reader or read_keychain_item

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
        from_file = str(getattr(self._load(), field))
        if from_file:
            return from_file
        return self._from_keychain(field)

    def _from_keychain(self, field: str) -> str:
        """자격 파일과 환경이 비었을 때 키체인에서 소유자 세션 값을 읽는다.

        봇 토큰과 앱 토큰은 대상이 아니다. 그 둘은 자격 파일에 있고, 없으면
        기동 자체가 실패해 누락이 곧바로 드러난다.
        """
        account = KEYCHAIN_ACCOUNTS.get(field, "")
        service = self._env.get(USER_KEYCHAIN_ENV, "")
        if not account or not service:
            return ""
        try:
            return self._keychain_reader(service, account)
        except Exception:  # noqa: BLE001 -- 키체인이 막혀도 턴을 멈추지 않는다
            log.warning("키체인에서 소유자 세션 값을 읽지 못했다 (계정 %s)", account)
            return ""

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
                f"자격 파일에 모르는 키가 {unknown}개 있다. "
                f"bot_token, app_token, user_token, user_cookie 만 쓴다 : {path}"
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


def owner_session_client(resolver: CredentialResolver, *, owner_user_id: str = "") -> object | None:
    """소유자 사용자 세션으로 슬랙을 읽는 클라이언트. user_token 이 없으면 None 이고,
    그러면 소유자 턴의 링크 선읽기도 봇 토큰만 쓴다(D5).

    호출하는 쪽이 소유자 턴에서만 쓴다. 이 클라이언트는 소유자가 멤버인 비공개
    대화까지 읽는다.

    owner_user_id 가 있는데 세션을 만들지 못하면 경고를 남긴다. 2026-11-07 실측에서
    #114 머지 뒤에도 운영 자격 파일에 user_token 이 없어 이 경로가 꺼진 채 떴고,
    그 사실이 로그에 없어 원인을 바로 찾지 못했다.
    """
    token = resolver.user_token()
    if not token:
        if owner_user_id:
            log.warning(
                "소유자 세션 토큰이 없다. 자격 파일 user_token 또는 %s 가 비어 있어 "
                "봇이 초대되지 않은 비공개 링크는 소유자 턴에도 읽지 않는다",
                USER_TOKEN_ENV,
            )
        return None
    cookie = resolver.user_cookie()
    if token.startswith("xoxc-") and not cookie:
        # 브라우저 세션 토큰은 d 쿠키 없이 invalid_auth 다. 매 링크마다 실패할
        # 클라이언트를 만드는 대신 기동 시점에 한 번 밝히고 끈다.
        log.warning(
            "소유자 세션 토큰이 xoxc 인데 user_cookie(%s) 가 없어 소유자 세션을 끈다",
            USER_COOKIE_ENV,
        )
        return None
    from slack_sdk import WebClient  # lazy import so assembly tests don't need the SDK

    headers = {"Cookie": "d=" + cookie} if cookie else {}
    return WebClient(token=token, headers=headers)


def resolver_for(profile: Profile, env: Mapping[str, str] | None = None) -> CredentialResolver:
    """One construction site, so every entry point resolves tokens the same way."""
    return CredentialResolver(
        default_path=profile.paths.credentials,
        configured_path=profile.credentials_file,
        env=env,
    )
