"""절대경로 호출로 PATH 래퍼를 건너뛰면 막는지 (2026-11-23 private-taeil MB-peteam)."""

from __future__ import annotations

from pathlib import Path

from slack_cli_agent.guard.pretool.wrapper_bypass_guard import find_bypass


def _wrapper(d: Path, name: str, body: str) -> None:
    p = d / name
    p.write_text(body)
    p.chmod(0o755)


def test_래퍼가_있으면_절대경로_호출을_막는다(tmp_path: Path) -> None:
    _wrapper(tmp_path, "ssh", "#!/bin/bash\nexec /usr/bin/ssh -F cfg \"$@\"\n")
    path = f"{tmp_path}:/usr/bin:/bin"
    hit = find_bypass("/usr/bin/ssh peagent@10.5.201.183 true", path)
    assert hit and hit[0] == "/usr/bin/ssh"
    assert find_bypass("cd x && /usr/bin/ssh h", path)


def test_이름으로_부르거나_탈출구를_쓰면_통과(tmp_path: Path) -> None:
    _wrapper(tmp_path, "ssh", "#!/bin/bash\nexec /usr/bin/ssh \"$@\"\n")
    path = f"{tmp_path}:/usr/bin"
    assert find_bypass("ssh peagent@10.5.201.183 true", path) is None
    assert find_bypass("WRAPPER_BYPASS=재현 /usr/bin/ssh h", path) is None


def test_래퍼가_아닌_다른_실행파일이면_통과(tmp_path: Path) -> None:
    _wrapper(tmp_path, "python3", "#!/bin/bash\nexec /opt/homebrew/bin/python3 \"$@\"\n")
    assert find_bypass("/usr/bin/python3 -V", f"{tmp_path}:/usr/bin") is None


def test_진짜_바이너리가_먼저면_통과(tmp_path: Path) -> None:
    _wrapper(tmp_path, "ssh", "#!/bin/bash\nexec /usr/bin/ssh \"$@\"\n")
    assert find_bypass("/usr/bin/ssh h", f"/usr/bin:{tmp_path}") is None
