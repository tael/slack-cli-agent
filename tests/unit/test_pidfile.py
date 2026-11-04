"""프로세스 PID 를 상태 디렉터리에 기록해 두 봇 프로세스를 구분한다 (F54).

마메치 bot.py 는 단일 프로세스라 bot.pid 하나로 충분했지만, 이 엔진은
ingress 와 worker 두 프로세스로 나뉘어 있어 한 파일을 공유하면 서로
덮어쓴다. 그래서 역할별 파일(`<role>.pid`)로 기록한다.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from slack_cli_agent.config.paths import StatePaths
from slack_cli_agent.reliability.pidfile import PidFile


class TestStatePathsPidFile:
    def test_pid_file_is_named_per_role_under_root(self, tmp_path: Path) -> None:
        paths = StatePaths(tmp_path)
        assert paths.pid_file("worker") == tmp_path / "worker.pid"
        assert paths.pid_file("ingress") == tmp_path / "ingress.pid"


class TestPidFile:
    def test_writes_current_pid_on_enter(self, tmp_path: Path) -> None:
        target = tmp_path / "worker.pid"
        with PidFile(target):
            assert target.read_text().strip() == str(os.getpid())

    def test_removes_file_on_clean_exit(self, tmp_path: Path) -> None:
        target = tmp_path / "worker.pid"
        with PidFile(target):
            assert target.exists()
        assert not target.exists()

    def test_removes_file_even_on_exception(self, tmp_path: Path) -> None:
        target = tmp_path / "worker.pid"
        with pytest.raises(RuntimeError), PidFile(target):
            raise RuntimeError("boom")
        assert not target.exists()

    def test_creates_parent_directory(self, tmp_path: Path) -> None:
        target = tmp_path / "nested" / "worker.pid"
        with PidFile(target):
            assert target.read_text().strip() == str(os.getpid())

    def test_leaves_another_processes_file_in_place(self, tmp_path: Path) -> None:
        # A stale file owned by a *different* pid must not be deleted on exit:
        # removing it would erase the live process's own record.
        target = tmp_path / "worker.pid"
        with PidFile(target):
            target.write_text("999999")
        assert target.read_text().strip() == "999999"

    def test_overwrites_stale_file_on_enter(self, tmp_path: Path) -> None:
        target = tmp_path / "worker.pid"
        target.write_text("123")
        with PidFile(target):
            assert target.read_text().strip() == str(os.getpid())
