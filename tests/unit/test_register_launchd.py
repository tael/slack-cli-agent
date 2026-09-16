"""launchd 등록기가 만드는 plist.

`KeepAlive=true` 인 서비스가 설정 오류로 못 뜨면 ThrottleInterval 주기로
영원히 재기동한다. 조용한 실패를 재기동 폭주로 바꾸는 것이라, 기동 게이트
(sca-xay)보다 이 조건이 먼저 있어야 한다 (sca-y4q).

생성기는 셸로 둔다. bootout/bootstrap 이라는 운영 부작용이 있는 배포
도구라, 시험 편의만으로 전부 파이썬으로 옮기면 위험 범위가 커진다
(코덱스 리뷰). 대신 실제 셸을 가짜 launchctl 과 임시 HOME 으로 돌려
나온 plist 를 구조로 검사한다.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
등록기 = REPO / "tools" / "register-launchd.sh"


@pytest.fixture
def 생성된plist(tmp_path: Path) -> dict[str, Any]:
    """임시 HOME 에서 등록기를 돌리고 나온 plist 두 개를 읽는다."""
    (tmp_path / "Library" / "LaunchAgents").mkdir(parents=True)
    (tmp_path / ".testbot").mkdir()

    # launchctl 을 실제로 부르면 이 기기의 서비스를 건드린다. 가짜로 가린다.
    bin_dir = tmp_path / "가짜bin"
    bin_dir.mkdir()
    가짜 = bin_dir / "launchctl"
    # print 는 113(서비스 없음)을 낸다. bootout 뒤 사라짐을 기다리는 쪽이
    # 여기서 멈추지 않게 한다.
    가짜.write_text(
        "#!/bin/bash\n"
        'echo "launchctl $*" >> "$HOME/launchctl.log"\n'
        '[ "$1" = print ] && exit 113\n'
        "exit 0\n",
        encoding="utf-8",
    )
    가짜.chmod(0o755)

    결과 = subprocess.run(  # noqa: PLW1510 - 종료코드를 시험이 직접 본다
        ["/bin/bash", str(등록기), "testbot"],
        capture_output=True,
        text=True,
        timeout=60,
        env=dict(
            os.environ,
            HOME=str(tmp_path),
            PATH=f"{bin_dir}:{os.environ['PATH']}",
            REGISTER_SETTLE_SEC="0",
        ),
    )
    assert 결과.returncode == 0, 결과.stderr

    나온것 = {}
    for role in ("worker", "ingress"):
        path = tmp_path / "Library" / "LaunchAgents" / f"local.testbot.{role}.plist"
        assert path.is_file(), f"{role} plist 가 없다"
        # 문자열 grep 이 아니라 구조로 본다. 형태가 깨진 plist 도 grep 은 통과한다.
        assert (
            subprocess.run(
                ["plutil", "-lint", str(path)], capture_output=True, check=False
            ).returncode
            == 0
        )
        나온것[role] = plistlib.loads(path.read_bytes())
    나온것["_home"] = str(tmp_path)  # type: ignore[assignment]
    return 나온것


class TestKeepAlive조건:
    @pytest.mark.parametrize("role", ["worker", "ingress"])
    def test_차단_표식이_없을_때만_살려_둔다(self, 생성된plist: dict[str, Any], role: str) -> None:
        home = 생성된plist["_home"]
        keep = 생성된plist[role]["KeepAlive"]
        # true 그대로면 표식이 있어도 재기동한다. 조건 자체가 없어야 잡힌다.
        assert isinstance(keep, dict), f"KeepAlive 가 아직 {keep!r} 이다"
        기대경로 = f"{home}/.testbot/preflight-blocked/{role}"
        assert keep["PathState"] == {기대경로: False}

    def test_역할마다_다른_표식을_본다(self, 생성된plist: dict[str, Any]) -> None:
        """한 표식을 둘이 나눠 보면 ingress 만 막혀도 worker 까지 멈춘다."""
        worker = set(생성된plist["worker"]["KeepAlive"]["PathState"])
        ingress = set(생성된plist["ingress"]["KeepAlive"]["PathState"])
        assert worker != ingress

    @pytest.mark.parametrize("role", ["worker", "ingress"])
    def test_표식_경로가_절대경로다(self, 생성된plist: dict[str, Any], role: str) -> None:
        """launchd 는 상대경로를 자기 기준으로 푼다. 그러면 조건이 영영
        거짓이 되어 아무것도 안 막는다."""
        for 경로 in 생성된plist[role]["KeepAlive"]["PathState"]:
            assert 경로.startswith("/"), 경로


class Test기존설정유지:
    @pytest.mark.parametrize("role", ["worker", "ingress"])
    def test_역할을_인자로_넘긴다(self, 생성된plist: dict[str, Any], role: str) -> None:
        인자 = 생성된plist[role]["ProgramArguments"]
        assert 인자[-1] == role
        assert 인자[1].endswith("/.testbot/run.sh")

    @pytest.mark.parametrize("role", ["worker", "ingress"])
    def test_재기동_간격과_로그_자리가_그대로다(
        self, 생성된plist: dict[str, Any], role: str
    ) -> None:
        """이번 변경은 KeepAlive 조건만 바꾼다. 나머지가 함께 바뀌면 무엇이
        무엇을 만들었는지 못 가린다."""
        plist = 생성된plist[role]
        assert plist["ThrottleInterval"] == 10
        assert plist["StandardErrorPath"].endswith(f"/logs/{role}.err.log")
        assert plist["StandardOutPath"].endswith(f"/logs/{role}.out.log")
        assert plist["WorkingDirectory"].endswith("/.testbot")


class Test재등록:
    """표식을 둔 채 재등록하면 방금 고친 설정으로도 봇이 안 뜬다. 그런데
    지우는 시점이 중요하다 - 둘을 한꺼번에 미리 지우면 재등록이 중간에
    실패했을 때 다른 역할이 의도치 않게 살아난다 (코덱스 리뷰).
    """

    @staticmethod
    def _돌린다(tmp_path: Path) -> tuple[subprocess.CompletedProcess[str], Path]:
        """가짜 launchctl 이 호출 순서를 기록하게 해서 돌린다.

        표식이 사라졌다는 것만 보면 bootstrap 보다 먼저 지웠는지, 아예 순서가
        뒤바뀌었는지 구별되지 않는다. 그래서 삭제 자취도 같은 기록에 남긴다.
        """
        (tmp_path / "Library" / "LaunchAgents").mkdir(parents=True)
        표식자리 = tmp_path / ".testbot" / "preflight-blocked"
        표식자리.mkdir(parents=True)
        for role in ("worker", "ingress"):
            (표식자리 / role).write_text("옛 차단", encoding="utf-8")

        bin_dir = tmp_path / "가짜bin"
        bin_dir.mkdir()
        기록 = tmp_path / "호출.log"
        가짜 = bin_dir / "launchctl"
        가짜.write_text(
            "#!/bin/bash\n"
            # bootstrap 이 불릴 때 그 역할의 표식이 이미 없어야 한다. 그
            # 사실을 호출 시점에 그대로 기록한다.
            f'if [ "$1" = "bootstrap" ]; then\n'
            f'  role=$(basename "$3" .plist)\n'
            f"  role=${{role##*.}}\n"
            # 셸 변수 이름은 ASCII 여야 한다. 한글 이름은 bash 가 명령으로 읽는다.
            f'  if [ -f "{표식자리}/$role" ]; then state=표식있음; else state=표식없음; fi\n'
            f'  echo "bootstrap $role $state" >> "{기록}"\n'
            f"else\n"
            f'  echo "$1 $2" >> "{기록}"\n'
            f"fi\n"
            # print 는 113(서비스 없음). bootout 완료를 기다리는 쪽이 여기서
            # 멈추지 않게 한다.
            '[ "$1" = print ] && exit 113\n'
            "exit 0\n",
            encoding="utf-8",
        )
        가짜.chmod(0o755)

        결과 = subprocess.run(  # noqa: PLW1510 - 종료코드를 시험이 직접 본다
            ["/bin/bash", str(등록기), "testbot"],
            capture_output=True,
            text=True,
            timeout=60,
            env=dict(
                os.environ,
                HOME=str(tmp_path),
                PATH=f"{bin_dir}:{os.environ['PATH']}",
                REGISTER_SETTLE_SEC="0",
            ),
        )
        return 결과, 기록

    def test_등록기가_성공으로_끝난다(self, tmp_path: Path) -> None:
        """실패한 실행의 부작용을 보고 통과를 선언하지 않는다."""
        결과, _ = self._돌린다(tmp_path)
        assert 결과.returncode == 0, 결과.stderr

    def test_bootstrap_시점에_그_역할의_표식이_이미_없다(self, tmp_path: Path) -> None:
        _, 기록 = self._돌린다(tmp_path)
        줄들 = [
            l for l in 기록.read_text(encoding="utf-8").splitlines() if l.startswith("bootstrap")
        ]
        assert 줄들 == ["bootstrap ingress 표식없음", "bootstrap worker 표식없음"]

    def test_두_역할_모두_bootstrap_까지_간다(self, tmp_path: Path) -> None:
        _, 기록 = self._돌린다(tmp_path)
        본문 = 기록.read_text(encoding="utf-8")
        assert "bootstrap worker" in 본문
        assert "bootstrap ingress" in 본문


class Test기존서비스가_사라질_때까지_기다린다:
    """`bootout` 은 비동기다. 프로세스가 아직 남은 채 같은 레이블로
    `bootstrap` 하면 `5: Input/output error` 로 실패하고, bootout 은 이미
    끝났으므로 그 역할이 도메인에서 사라진 채로 남는다.

    2026-09-16 에 shinji.worker 가 실제로 그렇게 사라졌다 (sca-fca).
    """

    def _돌린다(self, tmp_path: Path, *, 남아있는_횟수: int) -> subprocess.CompletedProcess[str]:
        (tmp_path / "Library" / "LaunchAgents").mkdir(parents=True)
        (tmp_path / ".testbot").mkdir()
        bin_dir = tmp_path / "가짜bin"
        bin_dir.mkdir()
        가짜 = bin_dir / "launchctl"
        # bootout 뒤 print 가 몇 번은 "아직 있다" 를 돌려준다. 그 사이에
        # bootstrap 을 부르면 기록에 남아 시험이 검출한다.
        가짜.write_text(
            f"""#!/bin/bash
LOG="$HOME/launchctl.log"
CNT="$HOME/print.count"
echo "$1" >> "$LOG"
case "$1" in
  print)
    n=$(cat "$CNT" 2>/dev/null || echo 0)
    echo $((n + 1)) > "$CNT"
    [ "$n" -lt {남아있는_횟수} ] && exit 0
    exit 113
    ;;
  bootout) echo 0 > "$CNT" ;;
esac
exit 0
""",
            encoding="utf-8",
        )
        가짜.chmod(0o755)
        return subprocess.run(  # noqa: PLW1510 - 종료코드를 시험이 직접 본다
            ["/bin/bash", str(등록기), "testbot"],
            capture_output=True,
            text=True,
            timeout=60,
            env=dict(
                os.environ,
                HOME=str(tmp_path),
                PATH=f"{bin_dir}:{os.environ['PATH']}",
                REGISTER_SETTLE_SEC="0",
                BOOTOUT_POLL_SEC="0.05",
            ),
        )

    def test_아직_남아있으면_bootstrap_을_부르지_않는다(self, tmp_path: Path) -> None:
        결과 = self._돌린다(tmp_path, 남아있는_횟수=3)
        기록 = (tmp_path / "launchctl.log").read_text(encoding="utf-8").split()
        # bootout -> print 가 사라짐을 확인 -> bootstrap 순서여야 한다.
        assert 결과.returncode == 0, 결과.stderr
        첫bootstrap = 기록.index("bootstrap")
        확인횟수 = 기록[:첫bootstrap].count("print")
        assert 확인횟수 >= 3, 기록

    def test_바로_사라지면_기다리지_않는다(self, tmp_path: Path) -> None:
        결과 = self._돌린다(tmp_path, 남아있는_횟수=0)
        기록 = (tmp_path / "launchctl.log").read_text(encoding="utf-8").split()
        assert 결과.returncode == 0, 결과.stderr
        assert 기록[:3] == ["bootout", "print", "bootstrap"], 기록


class Test등록이_실패하면_그렇게_끝난다:
    """bootstrap 실패를 0 으로 삼키면 등록되지 않은 서비스를 등록됐다고
    읽는다. 이번 사고에서 실제로 그 상태가 됐다 (sca-fca).
    """

    def _돌린다(self, tmp_path: Path) -> subprocess.CompletedProcess[str]:
        (tmp_path / "Library" / "LaunchAgents").mkdir(parents=True)
        (tmp_path / ".testbot").mkdir()
        bin_dir = tmp_path / "가짜bin"
        bin_dir.mkdir()
        가짜 = bin_dir / "launchctl"
        가짜.write_text(
            "#!/bin/bash\n"
            'echo "$1" >> "$HOME/launchctl.log"\n'
            '[ "$1" = print ] && exit 113\n'
            # launchd 가 내는 것과 같은 실패.
            '[ "$1" = bootstrap ] && { echo "Bootstrap failed: 5: Input/output error" >&2; exit 5; }\n'
            "exit 0\n",
            encoding="utf-8",
        )
        가짜.chmod(0o755)
        return subprocess.run(  # noqa: PLW1510 - 종료코드를 시험이 직접 본다
            ["/bin/bash", str(등록기), "testbot"],
            capture_output=True,
            text=True,
            timeout=60,
            env=dict(
                os.environ,
                HOME=str(tmp_path),
                PATH=f"{bin_dir}:{os.environ['PATH']}",
                REGISTER_SETTLE_SEC="0",
            ),
        )

    def test_종료코드가_0_이_아니다(self, tmp_path: Path) -> None:
        assert self._돌린다(tmp_path).returncode != 0

    def test_어느_역할이_등록되지_않았는지_말한다(self, tmp_path: Path) -> None:
        결과 = self._돌린다(tmp_path)
        assert "등록되지 않았다" in 결과.stderr
        assert "testbot.ingress" in 결과.stderr
