"""소스 한 개의 1회 점검.

상태는 셋이다. 셋을 섞으면 성공률이 거짓이 된다.

    ok    점검을 했고 통과
    fail  점검을 했고 실패(종료코드 비0, 명령 없음, 시간 초과, 경로 없음)
    skip  점검하지 않았다(enabled=false, 엔진 내장 도구). 실패가 아니다

health 명령은 셸 없이 실행한다(shlex 분해, 인자의 `~` 만 홈으로 푼다). 파이프나
리다이렉션이 필요하면 스크립트 파일로 두고 그 경로를 health 에 적는다.
SOURCE.md 는 운영자가 쓴 신뢰 입력이다. 외부 입력을 이 명령에 넣지 않는다.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .sources import Source

BUILTIN_PREFIX = "builtin:"
DEFAULT_TIMEOUT_SEC = 10.0
_DETAIL_LIMIT = 120


@dataclass(frozen=True)
class HealthResult:
    name: str
    status: str  # ok | fail | skip
    detail: str


def _argv(command: str) -> list[str]:
    return [os.path.expanduser(token) for token in shlex.split(command)]


def _tail(text: str) -> str:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return lines[-1][:_DETAIL_LIMIT] if lines else ""


def _looks_like_path(access: str) -> bool:
    return access.startswith(("/", "~", "./"))


def _check_existence(source: Source) -> HealthResult:
    access = source.access
    if access.startswith(BUILTIN_PREFIX):
        return HealthResult(source.name, "skip", f"점검 수단 없음(내장 도구 {access[len(BUILTIN_PREFIX):]})")
    if _looks_like_path(access):
        # 공백이 든 경로(iCloud 볼트)가 있으므로 전체를 먼저 경로로 본다.
        # 아니면 절대경로로 시작하는 실행파일 + 인자일 수 있어 첫 토큰을 본다.
        path = Path(os.path.expanduser(access))
        if path.exists():
            return HealthResult(source.name, "ok", f"경로 있음 {path}")
        try:
            first = _argv(access)[0]
        except (ValueError, IndexError):
            first = ""
        if " " in access and first and shutil.which(first):
            return HealthResult(source.name, "ok", f"실행파일 있음 {first}")
        return HealthResult(source.name, "fail", f"경로 없음 {path}")
    try:
        first = _argv(access)[0]
    except (ValueError, IndexError):
        return HealthResult(source.name, "fail", f"access 를 해석하지 못했다: {access[:40]!r}")
    found = shutil.which(first)
    if found:
        return HealthResult(source.name, "ok", f"실행파일 있음 {found}")
    return HealthResult(source.name, "fail", f"실행파일을 찾지 못했다: {first}")


def check_source(source: Source, *, timeout: float = DEFAULT_TIMEOUT_SEC) -> HealthResult:
    if not source.enabled:
        return HealthResult(source.name, "skip", "enabled=false")
    if not source.health:
        return _check_existence(source)
    try:
        argv = _argv(source.health)
        done = subprocess.run(  # noqa: S603 - SOURCE.md 는 운영자 신뢰 입력, 셸은 쓰지 않는다
            argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return HealthResult(source.name, "fail", f"시간 초과 {timeout:g}초")
    except FileNotFoundError:
        return HealthResult(source.name, "fail", f"명령을 찾지 못했다: {source.health.split()[0]}")
    except (OSError, ValueError) as exc:
        return HealthResult(source.name, "fail", f"실행하지 못했다: {exc}"[:_DETAIL_LIMIT])
    if done.returncode == 0:
        return HealthResult(source.name, "ok", "exit 0")
    extra = _tail(done.stderr) or _tail(done.stdout)
    return HealthResult(source.name, "fail", f"exit {done.returncode}" + (f": {extra}" if extra else ""))
