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


def _봇별대역(tmp_path: Path, 코드들: dict[str, str]) -> Path:
    """봇 이름마다 다른 코드로 끝나는 점검 대역. 섞인 결과를 재려면 필요하다."""
    path = tmp_path / "probe-mixed"
    기록 = tmp_path / "probe-mixed.기록"
    갈래 = "\n".join(f'  {이름}) exit {코드} ;;' for 이름, 코드 in 코드들.items())
    path.write_text(
        "#!/bin/bash\n"
        f'echo "$@" >> "{기록}"\n'
        'case "$1" in\n'
        f"{갈래}\n"
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _섞어돌린다(tmp_path: Path, 코드들: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "LAUNCHCTL": str(_대역(tmp_path, "launchctl")),
        "PROBE": str(_봇별대역(tmp_path, 코드들)),
        "RESTART_PROBE_WARMUP_SEC": "0",
    }
    return subprocess.run(
        ["/bin/bash", str(SCRIPT), *코드들],
        capture_output=True, text=True, env=env, timeout=30,
    )


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


class Test슬랙에_못_닿은_것:
    """점검 도구가 슬랙에 못 닿으면 종료코드 5 로 끝난다. 봇이 답을 못 낸
    것과 고칠 자리가 다르므로 결과에도 다르게 적는다 (sca-oaty)."""

    def test_봇_실패로_세지_않는다(self, tmp_path: Path) -> None:
        결과 = _돌린다(tmp_path, ["가봇"], 점검코드="5")
        assert "슬랙에 못 닿음" in 결과.stdout

    def test_그래도_종료코드는_0이_아니다(self, tmp_path: Path) -> None:
        """못 잰 것을 통과로 읽으면 재기동이 확인된 것으로 남는다."""
        결과 = _돌린다(tmp_path, ["가봇"], 점검코드="5")
        assert 결과.returncode != 0

    def test_봇_실패와_종료코드를_가른다(self, tmp_path: Path) -> None:
        못닿음 = _돌린다(tmp_path, ["가봇"], 점검코드="5")
        봇실패 = _돌린다(tmp_path, ["가봇"], 점검코드="1")
        assert 못닿음.returncode != 봇실패.returncode


class Test결과가_섞였을_때:
    """봇마다 결과가 다를 수 있다. 하나로 뭉개면 무엇을 고칠지가 사라진다
    (코덱스 리뷰)."""

    def test_봇_실패가_못_닿음을_이긴다(self, tmp_path: Path) -> None:
        결과 = _섞어돌린다(tmp_path, {"가봇": "1", "나봇": "5"})
        assert 결과.returncode == 1

    def test_반응_없음은_그대로_나온다(self, tmp_path: Path) -> None:
        """3 을 1 로 바꾸면 멘션 미도달이 봇 실패로 보인다."""
        결과 = _섞어돌린다(tmp_path, {"가봇": "3", "나봇": "5"})
        assert 결과.returncode == 3

    def test_미완결도_그대로_나온다(self, tmp_path: Path) -> None:
        결과 = _섞어돌린다(tmp_path, {"가봇": "4", "나봇": "5"})
        assert 결과.returncode == 4

    def test_정상과_못_닿음만_있으면_못_닿음이다(self, tmp_path: Path) -> None:
        결과 = _섞어돌린다(tmp_path, {"가봇": "0", "나봇": "5"})
        assert 결과.returncode == 5

    def test_먼저_실패한_코드를_쓴다(self, tmp_path: Path) -> None:
        """뒤엣것으로 덮으면 먼저 난 원인이 사라진다."""
        결과 = _섞어돌린다(tmp_path, {"가봇": "3", "나봇": "1"})
        assert 결과.returncode == 3


class Test못_닿음_코드는_한_곳에서_온다:
    """셸과 파이썬이 각각 5 를 적으면 한쪽만 고쳐 조용히 어긋난다."""

    def test_두_파일의_값이_같다(self) -> None:
        import re

        셸 = re.search(r"^TRANSPORT_EXIT=(\d+)", SCRIPT.read_text(encoding="utf-8"), re.MULTILINE)
        파이썬 = re.search(
            r"^TRANSPORT_EXIT = (\d+)",
            (REPO / "tools" / "test-channel-probe.py").read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        assert 셸 and 파이썬
        assert 셸.group(1) == 파이썬.group(1)
