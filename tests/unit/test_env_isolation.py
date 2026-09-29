"""자격 환경변수 격리. 시험이 실행한 셸의 토큰을 읽으면 안 된다.

하위 프로세스를 띄우는 시험은 `os.environ` 을 그대로 물려준다. 격리가 없으면
운영 토큰이 시험에 흘러들고, 값을 비교하는 단언이 깨질 때 실패 메시지에 그
토큰이 그대로 찍힌다(CI 로그에 남는 경로다).
"""

from __future__ import annotations

import os

import pytest

from tests.conftest import _CREDENTIAL_ENV


@pytest.mark.parametrize("이름", _CREDENTIAL_ENV)
def test_자격_환경변수가_비워진다(이름: str) -> None:
    assert 이름 not in os.environ


def test_슬랙_토큰_두_개가_격리_목록에_있다() -> None:
    assert {"SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"} <= set(_CREDENTIAL_ENV)
