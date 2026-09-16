"""봇을 띄우는 wrapper 스크립트.

launchd 가 부르는 것은 이 셸이고, 그 종료코드를 보고 재기동할지 정한다.
게이트가 설정 오류로 78(EX_CONFIG)을 내면 여기서 차단 표식을 남겨야
`KeepAlive` 의 `PathState` 조건이 재기동을 멈춘다 (sca-y4q).

셸이라도 시험한다. 이 로직이 각 봇의 상태 디렉터리에만 있으면 봇마다 갈리고
아무도 못 본다 - 실제로 세 봇의 run.sh 가 이미 서로 달랐다.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TEMPLATE = REPO / "tools" / "templates" / "run.sh"


def _봇자리(tmp_path: Path, *, 종료코드: int, 지연초: float = 0) -> Path:
    """가짜 봇 하나. venv 자리에 종료코드를 정해 두는 CLI 대역을 둔다."""
    d = tmp_path / ".testbot"
    (d / "venv" / "bin").mkdir(parents=True)
    (d / "logs").mkdir()
    cli = d / "venv" / "bin" / "slack-cli-agent"
    cli.write_text(
        "#!/bin/bash\n"
        f"[ {지연초} != 0 ] && sleep {지연초}\n"
        f'echo "$@" > "{d}/불린인자.txt"\n'
        f"exit {종료코드}\n",
        encoding="utf-8",
    )
    cli.chmod(0o755)
    return d


def _wrapper(tmp_path: Path, 봇자리: Path) -> Path:
    """템플릿을 이 가짜 봇에 맞춰 렌더링한다."""
    run = 봇자리 / "run.sh"
    run.write_text(
        TEMPLATE.read_text(encoding="utf-8")
        .replace("__NAME__", "testbot")
        .replace("__DISPLAY__", "시험봇")
        .replace("__REPO__", str(REPO)),
        encoding="utf-8",
    )
    run.chmod(0o755)
    return run


def _실행(run: Path, tmp_path: Path, *인자: str, 대기: float = 20) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, HOME=str(tmp_path))
    return subprocess.run(  # noqa: PLW1510 - 종료코드를 시험이 직접 본다
        ["/bin/bash", str(run), *인자], capture_output=True, text=True, env=env, timeout=대기
    )


def _표식(봇자리: Path, role: str) -> Path:
    return 봇자리 / "preflight-blocked" / role


class Test차단표식:
    @pytest.mark.parametrize("role", ["worker", "ingress"])
    def test_78_이면_역할별_표식을_남긴다(self, tmp_path: Path, role: str) -> None:
        봇 = _봇자리(tmp_path, 종료코드=78)
        결과 = _실행(_wrapper(tmp_path, 봇), tmp_path, role)
        assert 결과.returncode == 78, 결과.stderr
        assert _표식(봇, role).is_file()
        # 다른 역할까지 막으면 복구 범위가 쓸데없이 커진다 (코덱스 리뷰).
        다른역할 = "ingress" if role == "worker" else "worker"
        assert not _표식(봇, 다른역할).exists()

    def test_set_e_가_78_을_삼키지_않는다(self, tmp_path: Path) -> None:
        """`set -e` 아래에서 본체를 그냥 부르면 78 에서 셸이 즉시 끝나 표식을
        남길 자리가 사라진다. 순진한 구현이 여기서 걸린다 (코덱스 리뷰)."""
        봇 = _봇자리(tmp_path, 종료코드=78)
        결과 = _실행(_wrapper(tmp_path, 봇), tmp_path, "worker")
        assert _표식(봇, "worker").is_file()
        assert 결과.returncode == 78

    @pytest.mark.parametrize("코드", [0, 1, 2])
    def test_78_이_아니면_표식을_안_남긴다(self, tmp_path: Path, 코드: int) -> None:
        """일반 런타임 실패는 계속 재기동돼야 한다. 여기서 표식을 남기면
        프로세스 충돌 한 번에 봇이 영영 안 뜬다."""
        봇 = _봇자리(tmp_path, 종료코드=코드)
        결과 = _실행(_wrapper(tmp_path, 봇), tmp_path, "worker")
        assert 결과.returncode == 코드
        assert not (봇 / "preflight-blocked").exists() or not _표식(봇, "worker").exists()

    def test_사람이_돌린_preflight_의_78_은_표식이_아니다(self, tmp_path: Path) -> None:
        """`run.sh preflight` 는 사람이 상태를 보려고 부르는 것이다. 그 78 로
        표식을 만들면 점검했다는 이유로 봇이 안 뜬다 (코덱스 리뷰)."""
        봇 = _봇자리(tmp_path, 종료코드=78)
        결과 = _실행(_wrapper(tmp_path, 봇), tmp_path, "preflight")
        assert 결과.returncode == 78
        assert not _표식(봇, "preflight").exists()

    def test_표식에_원인을_찾을_단서가_있다(self, tmp_path: Path) -> None:
        """빈 파일이어도 차단은 되지만 사람이 원인을 못 읽는다."""
        봇 = _봇자리(tmp_path, 종료코드=78)
        _실행(_wrapper(tmp_path, 봇), tmp_path, "worker")
        본문 = _표식(봇, "worker").read_text(encoding="utf-8")
        assert "78" in 본문
        assert "worker" in 본문
        assert "worker.err.log" in 본문


class Test신호전달:
    def test_SIGTERM_을_자식에게_넘긴다(self, tmp_path: Path) -> None:
        """exec 를 뺐으므로 launchd 의 SIGTERM 이 이 셸에만 온다. 직접
        넘기지 않으면 본체가 살아남아 소켓을 쥔 채로 남는다."""
        봇 = _봇자리(tmp_path, 종료코드=0, 지연초=30)
        proc = subprocess.Popen(
            ["/bin/bash", str(_wrapper(tmp_path, 봇)), "worker"],
            env=dict(os.environ, HOME=str(tmp_path)),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        time.sleep(1.5)
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)

        # 대역이 30초를 다 잤으면 신호가 안 갔다는 뜻이다.
        남은 = subprocess.run(["pgrep", "-f", str(봇 / "venv" / "bin" / "slack-cli-agent")],
                             capture_output=True, text=True, check=False)
        assert 남은.returncode != 0, f"본체가 살아 있다 : {남은.stdout}"


class Test인자전달:
    def test_역할과_프로필_인자를_본체에_넘긴다(self, tmp_path: Path) -> None:
        봇 = _봇자리(tmp_path, 종료코드=0)
        _실행(_wrapper(tmp_path, 봇), tmp_path, "worker")
        인자 = (봇 / "불린인자.txt").read_text(encoding="utf-8")
        assert "worker" in 인자
        assert "--profile testbot" in 인자
        assert "--profile-dir" in 인자


class Test생성스크립트연결:
    def test_new_bot_이_템플릿을_쓴다(self) -> None:
        """템플릿이 옳아도 new-bot.sh 가 run.sh 를 따로 쓰면 새 봇은 여전히
        exec 판으로 태어난다."""
        본문 = (REPO / "tools" / "new-bot.sh").read_text(encoding="utf-8")
        assert "templates/run.sh" in 본문
        assert "exec " not in 본문, "생성부가 다시 wrapper 를 직접 쓰고 있다"

    def test_이미_있는_봇을_갱신할_수단이_있다(self) -> None:
        """new-bot.sh 만 고치면 새 봇만 보호된다. 이미 도는 봇들은 옛 판을
        들고 있어 차단 표식이 그 봇들에는 없다 (코덱스 리뷰)."""
        동기화 = REPO / "tools" / "sync-run-sh.sh"
        assert 동기화.is_file()
        assert "templates/run.sh" in 동기화.read_text(encoding="utf-8")

    def test_복구_도구가_표식을_먼저_지운다(self) -> None:
        """kickstart 는 KeepAlive 조건을 무시하고 띄운다. 표식을 안 지우고
        띄우면 다시 78 로 끝나고 표식만 새로 생긴다 (코덱스 리뷰)."""
        본문 = (REPO / "tools" / "unblock.sh").read_text(encoding="utf-8")
        assert 본문.index("rm -f") < 본문.index("launchctl kickstart")
