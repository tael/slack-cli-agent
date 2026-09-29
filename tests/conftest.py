"""테스트 격리. 규칙이 아니라 기본값으로 둔다.

실제 상태 디렉터리를 여는 코드가 테스트 맥락에서 실행되면 예외를 낸다. 파일만
격리하면 실제 시계·실제 경로가 남아 코드를 안 고쳐도 실패하는 시험이 생긴다.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

_REAL_HOME = Path.home()
_ALLOWED_PREFIXES: list[str] = []

#: 실행한 셸에 있으면 시험이 그 값을 읽어 버리는 자격 변수. 하위 프로세스를 띄우는
#: 시험은 `os.environ` 을 그대로 물려주므로, 지우지 않으면 운영 토큰이 시험에
#: 흘러들고 실패 메시지에 그 값이 찍힌다.
_CREDENTIAL_ENV = (
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "SLACK_USER_TOKEN",
    "SLACK_SIGNING_SECRET",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
)


def _guard(event: str, args: tuple) -> None:
    if event != "open" or not _ALLOWED_PREFIXES:
        return
    target = args[0]
    if not isinstance(target, (str, bytes, os.PathLike)):
        return
    path = os.fspath(target)
    if isinstance(path, bytes):
        path = path.decode("utf-8", "replace")
    if not path.startswith(str(_REAL_HOME)):
        return
    if any(path.startswith(p) for p in _ALLOWED_PREFIXES):
        return
    raise RuntimeError(f"테스트가 실제 홈 경로를 열려고 했다: {path}")


sys.addaudithook(_guard)


@pytest.fixture(autouse=True)
def isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """홈을 임시 경로로 바꾼다. 저장소와 파이썬 설치 경로만 예외로 둔다.

    settings 판독 기준선도 여기서 비운다. 그 기준선은 프로세스 전역이라, 실제
    기동에서는 preflight 가 잡은 뒤 같은 프로세스에서 안 비워져야 하고(코덱스
    6차 리뷰 결함1·2), 시험 사이의 격리는 이 fixture 처럼 구조로 만든다.

    자격 환경변수도 함께 비운다. 값이 필요한 시험은 스스로 `monkeypatch.setenv`
    로 넣는다.
    """
    from slack_cli_agent.engine.lifecycle import reset_engine_state

    repo = str(Path(__file__).resolve().parents[1])
    _ALLOWED_PREFIXES[:] = [repo, str(tmp_path), sys.prefix, sys.base_prefix]
    monkeypatch.setenv("HOME", str(tmp_path))
    for 이름 in _CREDENTIAL_ENV:
        monkeypatch.delenv(이름, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    reset_engine_state()
    yield tmp_path
    _ALLOWED_PREFIXES.clear()


@pytest.fixture
def database(tmp_path: Path):
    from slack_cli_agent.storage.database import Database

    db = Database(tmp_path / "state.db")
    db.migrate()
    return db
