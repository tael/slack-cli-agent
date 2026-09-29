"""실 CLI 시험만 실제 홈을 쓴다.

tests/conftest.py 의 isolate_home 이 HOME 을 임시 경로로 바꾼다. 그 격리는
옳지만, 엔진 CLI 의 로그인 정보가 실제 홈에 있어 이 묶음에서는 그대로 두면
전부 인증 실패로 끝난다. 여기서만 실제 값을 따로 들고 있는다.

**엔진에 넘기는 환경변수로만 쓴다.** 파이썬 쪽 Path.home() 은 여전히 임시
경로이므로, 시험 코드가 실제 홈에 쓰는 일은 여전히 막혀 있다.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# 모듈이 처음 로드될 때 잡는다. isolate_home 이 적용된 뒤에는 늦다.
_REAL_ENV = dict(os.environ)
_REAL_HOME = str(Path.home())


@pytest.fixture
def real_env() -> dict[str, str]:
    env = dict(_REAL_ENV)
    env["HOME"] = _REAL_HOME
    return env
