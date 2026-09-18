"""재기동 뒤 멘션-응답 경로를 재는 스크립트 (sca-mxa).

preflight 게이트는 설정과 자격만 본다. 슬랙 이벤트를 받아 답까지 내는
경로는 재기동이 성공해도 확인되지 않는다. 이 스크립트가 그 자리다.

launchctl 과 점검 도구는 환경변수로 갈아 끼운다. 그렇게 안 하면 이 시험이
실제 봇을 재기동하고 테스트 채널에 글을 남긴다.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "tools" / "restart-and-probe.sh"


def _대역(tmp_path: Path, 이름: str, 종료코드: str = "0") -> Path:
    """부른 인자를 기록하고 정해진 코드로 끝나는 명령."""
    path = tmp_path / 이름
    기록 = tmp_path / f"{이름}.기록"
    path.write_text(
        "#!/bin/bash\n"
        f'echo "$@" >> "{기록}"\n'
        f"exit {종료코드}\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _돌린다(tmp_path: Path, 봇들: list[str], 점검코드: str = "0") -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "LAUNCHCTL": str(_대역(tmp_path, "launchctl")),
        "PROBE": str(_대역(tmp_path, "probe", 점검코드)),
        # 접수기가 다시 뜨기를 기다리는 시간. 대역은 기다릴 것이 없다
        "RESTART_PROBE_WARMUP_SEC": "0",
    }
    return subprocess.run(
        ["/bin/bash", str(SCRIPT), *봇들],
        capture_output=True, text=True, env=env, timeout=30,
    )


def _기록(tmp_path: Path, 이름: str) -> list[str]:
    path = tmp_path / f"{이름}.기록"
    return path.read_text(encoding="utf-8").strip().splitlines() if path.exists() else []


class Test재기동:
    def test_봇마다_접수기와_워커를_둘_다_올린다(self, tmp_path: Path) -> None:
        _돌린다(tmp_path, ["아무봇"])
        불린 = _기록(tmp_path, "launchctl")
        assert any("아무봇.ingress" in line for line in 불린)
        assert any("아무봇.worker" in line for line in 불린)

    def test_받은_봇만_돈다(self, tmp_path: Path) -> None:
        """기본값으로 세 봇을 다 도는 것과 인자를 준 것은 다르다."""
        _돌린다(tmp_path, ["가봇"])
        assert not any("나봇" in line for line in _기록(tmp_path, "launchctl"))


class Test점검:
    def test_재기동한_봇마다_한_번씩_잰다(self, tmp_path: Path) -> None:
        _돌린다(tmp_path, ["가봇", "나봇"])
        불린 = _기록(tmp_path, "probe")
        assert len(불린) == 2
        assert 불린[0].startswith("가봇 ") and 불린[1].startswith("나봇 ")

    def test_점검이_실패하면_종료코드가_0이_아니다(self, tmp_path: Path) -> None:
        """0 으로 끝나면 자동화가 실패를 못 본다. 이 스크립트의 존재 이유다."""
        결과 = _돌린다(tmp_path, ["가봇"], 점검코드="3")
        assert 결과.returncode != 0

    def test_한_봇이_실패해도_나머지를_계속_잰다(self, tmp_path: Path) -> None:
        """먼저 실패한 봇에서 멈추면 나머지 봇의 상태를 모른 채 끝난다."""
        결과 = _돌린다(tmp_path, ["가봇", "나봇"], 점검코드="1")
        assert len(_기록(tmp_path, "probe")) == 2
        assert 결과.returncode != 0

    def test_봇별_결과를_출력한다(self, tmp_path: Path) -> None:
        결과 = _돌린다(tmp_path, ["가봇"], 점검코드="4")
        assert "가봇" in 결과.stdout
        assert "4" in 결과.stdout
