"""운영 작업 트리 고정 점검.

구치파치는 운영 작업 트리를 editable 설치로 실행한다. 그 트리에서 브랜치를
바꾸거나 커밋하지 않은 수정이 남으면, 리뷰를 거치지 않은 코드가 다음 기동에
그대로 실린다. 점검은 HEAD 가 origin/main 의 조상인지와 작업 트리가 깨끗한지를
보고, 위반은 경고로만 남긴다(기동은 막지 않는다).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from slack_cli_agent.config.profile import Profile
from slack_cli_agent.preflight.check import PreflightContext
from slack_cli_agent.preflight.checks import SourceTreeCheck
from slack_cli_agent.preflight.suite import PreflightSuite


def make_profile(tmp_path: Path) -> Profile:
    return Profile.from_dict(
        {
            "name": "example",
            "primary_engine": {"type": "claude", "binary": str(tmp_path / "claude_bin"), "model": "m"},
            "owner_user_id": "U1",
            "troubleshoot_channel": "C1",
            "state_dir": str(tmp_path / "state"),
        }
    )


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


@pytest.fixture
def repos(tmp_path: Path) -> tuple[Path, Path]:
    """원격 역할의 bare 저장소와 그것을 clone 한 운영 트리."""
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    tree = tmp_path / "tree"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    git(tmp_path, "init", "-q", "-b", "main", str(seed))
    (seed / "a.py").write_text("x = 1\n", encoding="utf-8")
    git(seed, "add", "a.py")
    git(seed, "commit", "-q", "-m", "init")
    git(seed, "remote", "add", "origin", str(remote))
    git(seed, "push", "-q", "origin", "main")
    git(tmp_path, "clone", "-q", str(remote), str(tree))
    return remote, tree


def run(check: SourceTreeCheck, tmp_path: Path):
    return check.run(PreflightContext(profile=make_profile(tmp_path)))


def test_깨끗하고_origin_main_과_같으면_통과(repos: tuple[Path, Path], tmp_path: Path) -> None:
    _, tree = repos
    result = run(SourceTreeCheck(root=tree), tmp_path)
    assert result.ok is True, result.detail


def test_원격이_앞서_있어도_조상이면_통과(repos: tuple[Path, Path], tmp_path: Path) -> None:
    remote, tree = repos
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(remote), str(other))
    (other / "b.py").write_text("y = 2\n", encoding="utf-8")
    git(other, "add", "b.py")
    git(other, "commit", "-q", "-m", "next")
    git(other, "push", "-q", "origin", "main")
    result = run(SourceTreeCheck(root=tree), tmp_path)
    assert result.ok is True, result.detail


def test_커밋하지_않은_수정은_경고(repos: tuple[Path, Path], tmp_path: Path) -> None:
    _, tree = repos
    (tree / "a.py").write_text("x = 2\n", encoding="utf-8")
    result = run(SourceTreeCheck(root=tree), tmp_path)
    assert result.ok is False
    assert result.fatal is False
    assert "a.py" in result.detail


def test_추적하지_않는_파일도_경고(repos: tuple[Path, Path], tmp_path: Path) -> None:
    _, tree = repos
    (tree / "new.py").write_text("z = 3\n", encoding="utf-8")
    result = run(SourceTreeCheck(root=tree), tmp_path)
    assert result.ok is False
    assert result.fatal is False


def test_origin_main_에_없는_커밋은_경고(repos: tuple[Path, Path], tmp_path: Path) -> None:
    _, tree = repos
    git(tree, "checkout", "-q", "-b", "local-work")
    (tree / "a.py").write_text("x = 9\n", encoding="utf-8")
    git(tree, "commit", "-q", "-am", "local")
    result = run(SourceTreeCheck(root=tree), tmp_path)
    assert result.ok is False
    assert result.fatal is False
    assert "origin/main" in result.detail


def test_원격_조회_실패는_경고이고_기동을_막지_않는다(repos: tuple[Path, Path], tmp_path: Path) -> None:
    _, tree = repos
    git(tree, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    result = run(SourceTreeCheck(root=tree), tmp_path)
    assert result.ok is False
    assert result.fatal is False
    assert "원격 조회 실패" in result.detail


def test_git_작업_트리가_아니면_통과(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    result = run(SourceTreeCheck(root=plain), tmp_path)
    assert result.ok is True, result.detail


def test_표준_점검_목록에_있고_실패해도_기동_가능(repos: tuple[Path, Path], tmp_path: Path) -> None:
    _, tree = repos
    (tree / "a.py").write_text("x = 2\n", encoding="utf-8")
    suite = PreflightSuite()
    found = [c for c in suite.checks if isinstance(c, SourceTreeCheck)]
    assert found, "표준 점검 목록에 운영 트리 점검이 없다"
    report = PreflightSuite(checks=[SourceTreeCheck(root=tree)]).run(make_profile(tmp_path))
    assert report.bootable is True
    lines = [line for _, line in PreflightSuite(checks=[SourceTreeCheck(root=tree)]).messages(report)]
    assert lines[0].startswith("[경고] source_tree")
