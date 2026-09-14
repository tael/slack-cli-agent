"""엔진 프로세스에 넘길 환경 변수를 고른다.

원본 bot.py 의 두 지점을 이관한다.

- ``codex_environment()``(bot.py:1524) — Codex 프로세스 환경. 허용 목록
  방식이다. PATH·LANG·HOME 최소값과 BOT_PROFILE, 그리고 프로필이 지정한
  엔진 전용 홈 경로(CODEX_HOME)만 넘긴다. 슬랙 토큰도 다른 엔진의
  자격증명도 애초에 목록에 없어 넘어가지 않는다.
- ``_run_claude()``(bot.py:1678-1681) — Claude 프로세스 환경. 실행 환경을
  통째로 복사하고 ANTHROPIC_API_KEY·ANTHROPIC_AUTH_TOKEN 만 제거하는 차단
  목록 방식이다. OAuth 토큰 경로를 강제하기 위한 것으로, Codex 와 계약이
  다르다.

엔진 전용 홈 경로를 빼면 사용자 개인 설정과 세션·인증을 그대로 쓰게 돼
격리가 무너진다 — 이 모듈의 존재 이유다.

``os.environ`` 을 직접 읽지 않는다. 호출부가 ``source_env`` 로 넘긴 값만
쓴다. 그러지 않으면 시험이 실행 환경에 매인다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from pathlib import Path

from ..core.errors import ConfigError


class EngineEnvironmentPolicy(ABC):
    """엔진 하나에 넘길 환경 변수를 고르는 계약.

    엔진마다 규칙이 달라(허용 목록 대 차단 목록) 기반 클래스는 공통 얼개만
    쥐고 실제 선별은 하위 클래스가 한다.
    """

    #: 봇 전용 홈 경로를 넘길 환경 변수 이름. 엔진이 홈 경로 개념이 없으면
    #: ``None`` 으로 두고, 그러면 home_dir 이 있어도 넘기지 않는다.
    HOME_ENV_VAR: str | None = None

    def __init__(self, profile_name: str, home_dir: Path | None = None) -> None:
        self.profile_name = profile_name
        self.home_dir = home_dir

    def build(self, source_env: Mapping[str, str]) -> dict[str, str]:
        """이 엔진 프로세스에 넘길 환경 변수를 돌려준다.

        ``source_env`` 는 읽기만 하고 고치지 않는다.
        """
        env = self._base_env(source_env)
        if self.HOME_ENV_VAR and self.home_dir is not None:
            env[self.HOME_ENV_VAR] = str(self.home_dir)
        return env

    @abstractmethod
    def _base_env(self, source_env: Mapping[str, str]) -> dict[str, str]:
        """홈 경로를 얹기 전, 이 엔진 고유의 선별 규칙."""


class CodexEnvironmentPolicy(EngineEnvironmentPolicy):
    """Codex 프로세스 환경. 원본 ``codex_environment()`` 그대로.

    허용 목록 방식이다 — 여기서 값을 채우지 않으면 그 변수는 아예 안
    넘어간다. 슬랙 토큰과 Claude 자격증명을 하나씩 걸러낼 필요가 없다.
    """

    HOME_ENV_VAR = "CODEX_HOME"

    def _base_env(self, source_env: Mapping[str, str]) -> dict[str, str]:
        return {
            "PATH": source_env.get("PATH", "/usr/bin:/bin"),
            "LANG": source_env.get("LANG", "ko_KR.UTF-8"),
            "HOME": source_env.get("HOME", ""),
            # 봇이 부르는 보조 스크립트가 어느 봇인지 알아야 한다. 빠지면
            # 다른 봇 이름으로 토큰과 채널 설정을 잘못 고른다.
            "BOT_PROFILE": self.profile_name,
        }


class ClaudeEnvironmentPolicy(EngineEnvironmentPolicy):
    """Claude 프로세스 환경. 원본 ``_run_claude()`` 의 처리 그대로.

    차단 목록 방식이다 — 실행 환경을 통째로 복사하고 API 키 인증 경로만
    제거해 OAuth 토큰 경로를 강제한다.
    """

    EXCLUDED_VARS: frozenset[str] = frozenset({
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
    })

    def _base_env(self, source_env: Mapping[str, str]) -> dict[str, str]:
        return {
            key: value for key, value in source_env.items()
            if key not in self.EXCLUDED_VARS
        }


_POLICY_CLASSES: dict[str, type[EngineEnvironmentPolicy]] = {
    "codex": CodexEnvironmentPolicy,
    "claude": ClaudeEnvironmentPolicy,
}


def create_environment_policy(
    engine_name: str, profile_name: str, home_dir: Path | None,
) -> EngineEnvironmentPolicy:
    """엔진 이름으로 알맞은 정책을 만든다.

    ``engine/registry.py`` 의 이름 기반 등록소와 같은 방식이다.
    """
    cls = _POLICY_CLASSES.get(engine_name)
    if cls is None:
        known = ", ".join(sorted(_POLICY_CLASSES)) or "없음"
        raise ConfigError(f"엔진 {engine_name} 의 환경 변수 정책이 없다. 등록된 엔진: {known}")
    return cls(profile_name=profile_name, home_dir=home_dir)
