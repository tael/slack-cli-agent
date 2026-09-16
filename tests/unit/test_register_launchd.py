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
    가짜.write_text("#!/bin/bash\necho \"launchctl $*\" >> \"$HOME/launchctl.log\"\nexit 0\n", encoding="utf-8")
    가짜.chmod(0o755)

    결과 = subprocess.run(  # noqa: PLW1510 - 종료코드를 시험이 직접 본다
        ["/bin/bash", str(등록기), "testbot"],
        capture_output=True, text=True, timeout=60,
        env=dict(os.environ, HOME=str(tmp_path), PATH=f"{bin_dir}:{os.environ['PATH']}",
                 REGISTER_SETTLE_SEC="0"),
    )
    assert 결과.returncode == 0, 결과.stderr

    나온것 = {}
    for role in ("worker", "ingress"):
        path = tmp_path / "Library" / "LaunchAgents" / f"local.testbot.{role}.plist"
        assert path.is_file(), f"{role} plist 가 없다"
        # 문자열 grep 이 아니라 구조로 본다. 형태가 깨진 plist 도 grep 은 통과한다.
        assert subprocess.run(["plutil", "-lint", str(path)], capture_output=True, check=False).returncode == 0
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
    def test_bootstrap_전에_그_역할의_표식을_지운다(self, tmp_path: Path) -> None:
        """표식을 둔 채 재등록하면 방금 고친 설정으로도 봇이 안 뜬다.
        역할별로 각각 지운다 - 둘을 한꺼번에 미리 지우면 재등록이 중간에
        실패했을 때 다른 역할이 의도치 않게 살아난다 (코덱스 리뷰)."""
        (tmp_path / "Library" / "LaunchAgents").mkdir(parents=True)
        표식자리 = tmp_path / ".testbot" / "preflight-blocked"
        표식자리.mkdir(parents=True)
        for role in ("worker", "ingress"):
            (표식자리 / role).write_text("옛 차단", encoding="utf-8")

        bin_dir = tmp_path / "가짜bin"
        bin_dir.mkdir()
        가짜 = bin_dir / "launchctl"
        가짜.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
        가짜.chmod(0o755)

        subprocess.run(  # noqa: PLW1510 - 종료코드를 시험이 직접 본다
            ["/bin/bash", str(등록기), "testbot"], capture_output=True, text=True, timeout=60,
            env=dict(os.environ, HOME=str(tmp_path), PATH=f"{bin_dir}:{os.environ['PATH']}",
                 REGISTER_SETTLE_SEC="0"),
        )

        assert not (표식자리 / "worker").exists()
        assert not (표식자리 / "ingress").exists()
