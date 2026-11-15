"""소스 레지스트리 로더·검증기와 1회 점검."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

import pytest

from slack_cli_agent.registry.check import run_check
from slack_cli_agent.registry.health import check_source
from slack_cli_agent.registry.sources import load_sources, parse_source

PY = shlex.quote(sys.executable)


def _text(**over: str) -> str:
    fields = {
        "name": "web",
        "description": "웹 검색. 최신 정보가 필요할 때 뒤진다",
        "access": "builtin:WebSearch",
        "kind": "read",
        "capabilities": "[web.search, web.fetch]",
        "enabled": "true",
    }
    fields.update(over)
    lines = [f"{k}: {v}" for k, v in fields.items() if v != "<없음>"]
    return "---\n" + "\n".join(lines) + "\n---\n본문\n"


def _put(root: Path, name: str, text: str) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SOURCE.md").write_text(text, encoding="utf-8")


def test_정상_항목을_읽는다():
    s = parse_source(_text(health="true"), "web", Path("x"))
    assert (s.name, s.kind, s.enabled, s.health) == ("web", "read", True, "true")
    assert s.capabilities == ("web.search", "web.fetch")


def test_enabled_를_생략하면_켜짐이다():
    assert parse_source(_text(enabled="<없음>"), "web", Path("x")).enabled is True


@pytest.mark.parametrize(
    ("over", "reason"),
    [
        ({"name": "other"}, "디렉터리 이름"),
        ({"name": "<없음>"}, "name"),
        ({"description": "<없음>"}, "description"),
        ({"access": "<없음>"}, "access"),
        ({"kind": "write2"}, "kind"),
        ({"kind": "<없음>"}, "kind"),
        ({"capabilities": "[]"}, "capabilities"),
        ({"capabilities": "[Web Search]"}, "능력 이름"),
        ({"capabilities": "web.search"}, "목록"),
        ({"enabled": "yes"}, "enabled"),
        ({"health": "[a]"}, "health"),
        ({"typo_key": "1"}, "알 수 없는 키"),
        ({"access": "gh --token ghp_abcdefghijklmnopqrstuvwxyz0123456789"}, "비밀"),
        ({"health": "curl -H xoxb-1234567890-abcdef"}, "비밀"),
    ],
)
def test_잘못된_항목은_사유와_함께_거부한다(over, reason):
    from slack_cli_agent.registry.sources import SourceError

    with pytest.raises(SourceError, match=reason):
        parse_source(_text(**over), "web", Path("x"))


def test_본문의_비밀도_거부한다():
    from slack_cli_agent.registry.sources import SourceError

    with pytest.raises(SourceError, match="비밀"):
        parse_source(_text() + "sk-ant-abcdefghijklmnopqrstuv\n", "web", Path("x"))


def test_잘못된_항목만_거부하고_나머지는_싣는다(tmp_path):
    _put(tmp_path, "web", _text())
    _put(tmp_path, "bad", _text(name="bad", kind="nope"))
    (tmp_path / "nofile").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "README.md").write_text("x")
    load = load_sources(tmp_path)
    assert load.error == ""
    assert [s.name for s in load.sources] == ["web"]
    assert {r.name: r.reason for r in load.rejected}.keys() == {"bad", "nofile"}
    assert "SOURCE.md 가 없다" in {r.name: r.reason for r in load.rejected}["nofile"]


def test_루트가_없으면_전체_오류다(tmp_path):
    load = load_sources(tmp_path / "없음")
    assert load.error and not load.sources


def test_읽을_수_없는_파일은_그_항목만_거부한다(tmp_path):
    _put(tmp_path, "web", _text())
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "SOURCE.md").write_bytes(b"\xff\xfe\x00bad")
    load = load_sources(tmp_path)
    assert [s.name for s in load.sources] == ["web"]
    assert [r.name for r in load.rejected] == ["bin"]


# ---- health

def _src(**over: str):
    return parse_source(_text(**over), "web", Path("x"))


def test_health_종료코드_0은_ok_아니면_fail():
    ok = check_source(_src(health=f"{PY} -c 'import sys; sys.exit(0)'"))
    bad = check_source(_src(health=f"{PY} -c 'import sys; sys.exit(3)'"))
    assert ok.status == "ok"
    assert bad.status == "fail" and "3" in bad.detail


def test_health_stderr_마지막줄을_사유에_남기고_길이를_자른다():
    r = check_source(_src(health=f"{PY} -c 'import sys; sys.stderr.write(\"x\"*500); sys.exit(1)'"))
    assert r.status == "fail" and len(r.detail) < 200


def test_health_명령이_없으면_fail():
    r = check_source(_src(health="no-such-cli-zzz --version"))
    assert r.status == "fail" and "찾지 못" in r.detail


def test_health_시간초과는_fail():
    r = check_source(_src(health=f"{PY} -c 'import time; time.sleep(5)'"), timeout=0.3)
    assert r.status == "fail" and "시간 초과" in r.detail


def test_health_인자의_틸드를_홈으로_푼다(tmp_path):
    (tmp_path / "f").write_text("x")
    r = check_source(_src(health=f"{PY} -c 'import sys,os; sys.exit(0 if os.path.exists(sys.argv[1]) else 4)' ~/f"))
    assert r.status == "ok"


def test_health_가_없으면_경로_존재를_본다(tmp_path):
    (tmp_path / "d").mkdir()
    assert check_source(_src(access=str(tmp_path / "d"))).status == "ok"
    assert check_source(_src(access="~/d")).status == "ok"
    miss = check_source(_src(access=str(tmp_path / "없는경로")))
    assert miss.status == "fail" and "없는경로" in miss.detail


def test_health_가_없는_cli_는_실행파일_존재를_본다():
    assert check_source(_src(access=f"{PY} --version")).status == "ok"
    assert check_source(_src(access="no-such-cli-zzz run")).status == "fail"


def test_내장_도구는_skip_이고_실패가_아니다():
    r = check_source(_src(access="builtin:WebSearch,WebFetch"))
    assert r.status == "skip"


def test_꺼진_소스는_점검하지_않는다():
    r = check_source(_src(enabled="false", health="false"))
    assert r.status == "skip" and "enabled" in r.detail


# ---- 1회 점검 (유도 시험 포함)

def test_점검은_실패가_있으면_exit_1(tmp_path):
    _put(tmp_path / "sources", "web", _text())
    _put(tmp_path / "sources", "ghost", _text(name="ghost", access=str(tmp_path / "없는경로")))
    rows, code = run_check(tmp_path)
    by = {(r.kind, r.name): r for r in rows}
    assert code == 1
    assert by[("source", "web")].status == "skip"
    assert by[("source", "ghost")].status == "fail"


def test_점검은_모두_ok_또는_skip_이면_exit_0(tmp_path):
    _put(tmp_path / "sources", "web", _text())
    (tmp_path / "d").mkdir()
    _put(tmp_path / "sources", "d", _text(name="d", access=str(tmp_path / "d")))
    _, code = run_check(tmp_path)
    assert code == 0


def test_거부된_항목도_exit_1_이고_행으로_남는다(tmp_path):
    _put(tmp_path / "sources", "bad", _text(name="bad", kind="x"))
    rows, code = run_check(tmp_path)
    assert code == 1 and rows[0].status == "reject"


def test_루트가_없으면_exit_1(tmp_path):
    rows, code = run_check(tmp_path / "없음")
    assert code == 1 and rows[0].status == "fail"


def test_workflows_디렉터리가_있으면_함께_점검한다(tmp_path):
    _put(tmp_path / "sources", "web", _text())
    wf = tmp_path / "workflows" / "w"
    wf.mkdir(parents=True)
    (wf / "WORKFLOW.md").write_text(
        "---\nid: w\ndescription: d\ncapabilities: [web.search]\nverify: off\n---\n본문\n", encoding="utf-8"
    )
    (tmp_path / "workflows" / "x").mkdir()
    (tmp_path / "workflows" / "x" / "WORKFLOW.md").write_text(
        "---\nid: x\ndescription: d\ncapabilities: [없는.능력]\nverify: off\n---\n본문\n", encoding="utf-8"
    )
    rows, code = run_check(tmp_path)
    by = {(r.kind, r.name): r.status for r in rows}
    assert by[("workflow", "w")] == "ok" and by[("workflow", "x")] == "reject"
    assert code == 1


def test_main_은_표를_출력하고_종료코드를_돌려준다(tmp_path, capsys):
    from slack_cli_agent.registry.__main__ import main

    _put(tmp_path / "sources", "ghost", _text(name="ghost", access=str(tmp_path / "없음")))
    code = main([str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 1 and "ghost" in out and "fail" in out
