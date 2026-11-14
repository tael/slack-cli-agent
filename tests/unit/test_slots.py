"""ConcurrencySlots — 워커와 접수 프로세스가 함께 쓰는 엔진 호출 상한 (A16)."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import slack_cli_agent
from slack_cli_agent.core.slots import FileSlots, ThreadSlots

#: 하위 프로세스가 설치본이 아니라 시험 중인 소스를 읽게 한다.
_SRC = str(Path(slack_cli_agent.__file__).resolve().parents[1])


class TestThreadSlots:
    def test_상한을_넘으면_None(self) -> None:
        slots = ThreadSlots(1)
        token = slots.acquire(timeout=0)
        assert token is not None
        assert slots.acquire(timeout=0) is None
        slots.release(token)
        assert slots.acquire(timeout=0) is not None


class TestFileSlots:
    def test_같은_디렉터리를_쓰는_인스턴스끼리_상한을_나눈다(self, tmp_path: Path) -> None:
        a = FileSlots(tmp_path, 2)
        b = FileSlots(tmp_path, 2)
        first = a.acquire(timeout=0)
        second = b.acquire(timeout=0)
        assert first is not None and second is not None
        assert a.acquire(timeout=0) is None
        assert b.acquire(timeout=0) is None
        a.release(first)
        third = b.acquire(timeout=0)
        assert third is not None
        b.release(second)
        b.release(third)

    def test_다른_프로세스가_쥔_자리는_못_쓰고_그_프로세스가_끝나면_풀린다(
        self, tmp_path: Path
    ) -> None:
        holder = subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent(f"""
                import sys
                from pathlib import Path
                from slack_cli_agent.core.slots import FileSlots
                token = FileSlots(Path({str(tmp_path)!r}), 1).acquire(timeout=0)
                print("held" if token is not None else "none", flush=True)
                sys.stdin.readline()
            """)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            env={**os.environ, "PYTHONPATH": _SRC},
        )
        try:
            assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
            assert FileSlots(tmp_path, 1).acquire(timeout=0) is None
        finally:
            assert holder.stdin is not None
            holder.stdin.close()
            holder.wait(timeout=10)
        assert FileSlots(tmp_path, 1).acquire(timeout=0) is not None

    def test_대기_시간_안에_자리가_안_나면_None(self, tmp_path: Path) -> None:
        clock = [0.0]
        slots = FileSlots(
            tmp_path, 1, clock=lambda: clock[0],
            sleep=lambda s: clock.__setitem__(0, clock[0] + s),
        )
        held = slots.acquire(timeout=0)
        assert slots.acquire(timeout=1.0) is None
        assert clock[0] >= 1.0
        slots.release(held)
