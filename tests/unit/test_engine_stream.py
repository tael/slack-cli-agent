"""엔진 출력 스트리밍과 제한 시각 강제 종료.

2026-10-16 레이에서 agy 한 턴이 900초 제한에 27,558초를 돌았다. 그때
Popen.wait(timeout=) 이 돌아오지 않아 엔진을 아무도 죽이지 않았고, 회수된
작업이 같은 요청에 새 엔진을 계속 붙였다(sca-rei-spin). 이 시험은 제한을
넘긴 프로세스가 실제로 죽는지, 그 프로세스가 띄운 자식까지 닿는지를 본다.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

from slack_cli_agent.engine.stream import run_streaming


def _살아있다(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Test정상종료:
    def test_표준출력을_줄_단위로_넘기고_결과를_모은다(self, tmp_path) -> None:
        줄: list[str] = []

        결과 = run_streaming(
            [sys.executable, "-c", "print('첫줄'); print('둘째줄')"],
            cwd=str(tmp_path),
            timeout=30,
            env=None,
            on_stdout_line=줄.append,
        )

        assert 결과.returncode == 0
        assert [받은.strip() for 받은 in 줄] == ["첫줄", "둘째줄"]
        assert "둘째줄" in 결과.stdout


class Test콜백없는실행:
    """진행 표시가 꺼진 채널은 줄 콜백을 안 넘긴다. 그 경로가 subprocess.run
    이던 동안은 제한을 강제하는 것이 wait(timeout=) 하나였다 (sca-rei-spin)."""

    def test_콜백이_없어도_출력을_모은다(self, tmp_path) -> None:
        본문 = "import sys; print('본문'); print('오류', file=sys.stderr)"
        결과 = run_streaming(
            [sys.executable, "-c", 본문], cwd=str(tmp_path), timeout=30, env=None,
        )

        assert 결과.returncode == 0
        assert "본문" in 결과.stdout
        assert "오류" in 결과.stderr

    def test_콜백이_없어도_제한을_넘기면_죽인다(self, tmp_path) -> None:
        시작 = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            run_streaming(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                cwd=str(tmp_path),
                timeout=0.3,
                env=None,
            )

        assert time.monotonic() - 시작 < 10


class Test제한초과:
    def test_제한을_넘기면_프로세스를_죽이고_TimeoutExpired_를_낸다(self, tmp_path) -> None:
        시작 = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            run_streaming(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                cwd=str(tmp_path),
                timeout=0.3,
                env=None,
                on_stdout_line=lambda line: None,
            )

        # wait() 가 깨어나기를 기다리지 않는다. 제한 직후에 끝나야 한다.
        assert time.monotonic() - 시작 < 10

    def test_제한_직전까지_받은_출력은_예외에_담긴다(self, tmp_path) -> None:
        본문 = "import time; print('받은줄', flush=True); time.sleep(30)"
        with pytest.raises(subprocess.TimeoutExpired) as 예외:
            run_streaming(
                [sys.executable, "-c", 본문],
                cwd=str(tmp_path),
                timeout=1.0,
                env=None,
                on_stdout_line=lambda line: None,
            )

        assert "받은줄" in (예외.value.output or "")

    def test_엔진이_띄운_자식까지_같이_죽는다(self, tmp_path) -> None:
        """도구 실행이 자식으로 떠 있으면 엔진만 죽여도 그 자식이 남는다.
        프로세스 그룹째 죽이는지를 손자 pid 로 확인한다."""
        본문 = (
            "import subprocess, sys, time;"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']);"
            "print(child.pid, flush=True);"
            "time.sleep(30)"
        )
        손자: list[int] = []
        with pytest.raises(subprocess.TimeoutExpired):
            run_streaming(
                [sys.executable, "-c", 본문],
                cwd=str(tmp_path),
                timeout=1.0,
                env=None,
                on_stdout_line=lambda line: 손자.append(int(line.strip())),
            )

        assert 손자, "자식 pid 를 못 받았다"
        죽은 = False
        끝 = time.monotonic() + 5
        while time.monotonic() < 끝:
            if not _살아있다(손자[0]):
                죽은 = True
                break
            time.sleep(0.05)
        if not 죽은:
            os.kill(손자[0], signal.SIGKILL)
        assert 죽은 is True
