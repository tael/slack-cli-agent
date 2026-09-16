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
    """홈을 임시 경로로 바꾼다. 저장소와 파이썬 설치 경로만 예외로 둔다."""
    repo = str(Path(__file__).resolve().parents[1])
    _ALLOWED_PREFIXES[:] = [repo, str(tmp_path), sys.prefix, sys.base_prefix]
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    yield tmp_path
    _ALLOWED_PREFIXES.clear()


@pytest.fixture
def database(tmp_path: Path):
    from slack_cli_agent.storage.database import Database

    db = Database(tmp_path / "state.db")
    db.migrate()
    return db
