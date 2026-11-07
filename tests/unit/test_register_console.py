"""웹 콘솔 launchd 등록 스크립트 (H8).

마메치 대시보드는 ~/.mametchi 만 읽어 구치파치 지표를 내지 못한다. 대체는
sca 웹 콘솔이고, 등록은 tools/register-console.sh 가 한다. 이 스크립트가
조용히 실패하면 대체가 된 것처럼 보인다. 확인한 실패는 셋이다.

- 8787 을 마메치 대시보드가 이미 쓰고 있다. 전에는 그래도 등록하고
  launchctl list 에 이름이 보이면 끝냈다. 콘솔은 bind 실패로 재기동만 반복한다
- 프로필 디렉터리에 봇 프로필이 없어도 등록했다. 작업 트리 사본에서 돌리면
  profiles/ 에 example 하나만 있어 구치파치가 콘솔 목록에 없다
- 기동 확인을 HTTP 로 하지 않았다

launchctl, lsof, curl 은 환경변수로 갈아 끼운다. HOME 도 임시 디렉터리라
실제 LaunchAgents 와 ~/.console 을 건드리지 않는다.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "tools" / "register-console.sh"


def _대역(path: Path, 본문: str) -> Path:
    path.write_text("#!/bin/bash\n" + 본문, encoding="utf-8")
    path.chmod(0o755)
    return path


def _준비(tmp_path: Path, *, 포트점유: str = "", 건강: bool = True) -> dict[str, str]:
    home = tmp_path / "home"
    venv_bin = home / ".console" / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    _대역(venv_bin / "slack-cli-agent", "exit 0\n")
    # Already installed from this repo, so the script must not run pip.
    (home / ".console" / "repo").write_text(str(REPO) + "\n", encoding="utf-8")
    # Stubs also sit first on PATH: a script that ignores the env override
    # still must not reach the real launchctl (a red run registered a live job).
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    기록 = tmp_path / "launchctl.기록"
    launchctl = _대역(bin_dir / "launchctl", f'echo "$@" >> "{기록}"\nexit 0\n')
    lsof = _대역(
        bin_dir / "lsof",
        f'printf "%s" "{포트점유}"\n[ -n "{포트점유}" ]\n',
    )
    bots = json.dumps({"bots": [{"name": "guchipachi"}]})
    health = "echo '{\"ok\": true}'" if 건강 else "exit 7"
    curl = _대역(
        bin_dir / "curl",
        f'case "$*" in\n  *health*) {health} ;;\n  *bots*) echo \'{bots}\' ;;\nesac\n',
    )
    return {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(home),
        "LAUNCHCTL": str(launchctl),
        "LSOF": str(lsof),
        "CURL": str(curl),
        "CONSOLE_HEALTH_TIMEOUT_SEC": "1",
    }


def _프로필(tmp_path: Path, *이름: str) -> Path:
    d = tmp_path / "profiles"
    d.mkdir()
    (d / "example.example.json").write_text("{}", encoding="utf-8")
    for n in 이름:
        (d / f"{n}.json").write_text(json.dumps({"name": n}), encoding="utf-8")
    return d


def _돌린다(env: dict[str, str], *인자: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(SCRIPT), *인자],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _launchctl_기록(tmp_path: Path) -> str:
    path = tmp_path / "launchctl.기록"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_다른_프로세스가_포트를_쓰면_등록하지_않는다(tmp_path: Path) -> None:
    env = _준비(tmp_path, 포트점유="1160")
    env["CONSOLE_PROFILE_DIRS"] = str(_프로필(tmp_path, "guchipachi"))

    done = _돌린다(env)

    assert done.returncode != 0
    assert "8787" in done.stderr and "1160" in done.stderr
    assert "bootstrap" not in _launchctl_기록(tmp_path)


def test_봇_프로필이_없는_디렉터리면_등록하지_않는다(tmp_path: Path) -> None:
    env = _준비(tmp_path)
    env["CONSOLE_PROFILE_DIRS"] = str(_프로필(tmp_path))

    done = _돌린다(env)

    assert done.returncode != 0
    assert "프로필" in done.stderr
    assert "bootstrap" not in _launchctl_기록(tmp_path)


def test_health_응답이_없으면_실패로_끝낸다(tmp_path: Path) -> None:
    env = _준비(tmp_path, 건강=False)
    env["CONSOLE_PROFILE_DIRS"] = str(_프로필(tmp_path, "guchipachi"))

    done = _돌린다(env, "8790")

    assert done.returncode != 0
    assert "기동 확인 실패" in done.stderr


def test_정상이면_프로필_디렉터리를_넘기고_봇_목록을_낸다(tmp_path: Path) -> None:
    env = _준비(tmp_path)
    profiles = _프로필(tmp_path, "guchipachi")
    extra = tmp_path / "more"
    extra.mkdir()
    (extra / "deagent.json").write_text("{}", encoding="utf-8")
    env["CONSOLE_PROFILE_DIRS"] = f"{profiles}:{extra}"

    done = _돌린다(env, "8790")

    assert done.returncode == 0, done.stderr
    assert "guchipachi" in done.stdout
    assert "http://127.0.0.1:8790/" in done.stdout
    run_sh = (Path(env["HOME"]) / ".console" / "run.sh").read_text(encoding="utf-8")
    assert f'--profile-dir "{profiles}"' in run_sh
    assert f'--profile-dir "{extra}"' in run_sh
    assert "bootstrap" in _launchctl_기록(tmp_path)
