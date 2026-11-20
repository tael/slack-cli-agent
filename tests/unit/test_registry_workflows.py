"""WORKFLOW.md 로더·검증기, 요청 헤더 렌더, 재개 규칙, 설정 키."""

from __future__ import annotations

from pathlib import Path

import pytest

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.registry.workflows import (
    WorkflowError,
    load_workflows,
    parse_workflow,
    render_workflow_block,
    resume_decision,
)


def _wf(**over: str) -> str:
    fields = {
        "id": "web-lookup",
        "description": "날씨·시세 같은 단순 조회. 코드 수정에는 쓰지 않는다",
        "capabilities": "[web.search, web.fetch]",
        "model": "sonnet",
        "effort": "medium",
        "verify": "standard",
    }
    fields.update(over)
    lines = [f"{k}: {v}" for k, v in fields.items() if v != "<없음>"]
    return "---\n" + "\n".join(lines) + "\n---\n1. 검색한다\n2. 출처를 붙인다\n"


def _src(root: Path, name: str, caps: str, enabled: str = "true") -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SOURCE.md").write_text(
        f"---\nname: {name}\ndescription: d\naccess: builtin:X\nkind: read\ncapabilities: {caps}\n"
        f"enabled: {enabled}\n---\n",
        encoding="utf-8",
    )


def _put(root: Path, id_: str, text: str) -> None:
    d = root / id_
    d.mkdir(parents=True, exist_ok=True)
    (d / "WORKFLOW.md").write_text(text, encoding="utf-8")


KNOWN = frozenset({"web.search", "web.fetch", "local.files"})


def test_정상_workflow_를_읽는다():
    w = parse_workflow(_wf(), "web-lookup", Path("p"), KNOWN)
    assert (w.id, w.model, w.effort, w.verify) == ("web-lookup", "sonnet", "medium", "standard")
    assert w.capabilities == ("web.search", "web.fetch")
    assert w.body.startswith("1. 검색한다")


def test_model_effort_는_선택이다():
    w = parse_workflow(_wf(model="<없음>", effort="<없음>"), "web-lookup", Path("p"), KNOWN)
    assert (w.model, w.effort) == ("", "")


@pytest.mark.parametrize(
    ("over", "reason"),
    [
        ({"id": "other"}, "디렉터리 이름"),
        ({"id": "다른"}, "id 형식"),
        ({"id": "<없음>"}, "id"),
        ({"description": "<없음>"}, "description"),
        ({"capabilities": "<없음>"}, "capabilities"),
        ({"capabilities": "[web.search, nope.x]"}, "레지스트리에 없는 능력: nope.x"),
        ({"effort": "huge"}, "effort"),
        ({"verify": "<없음>"}, "verify"),
        ({"verify": "maybe"}, "verify"),
        ({"model": "[a]"}, "model"),
        ({"typo": "1"}, "알 수 없는 키"),
    ],
)
def test_잘못된_workflow_는_사유와_함께_거부한다(over, reason):
    with pytest.raises(WorkflowError, match=reason):
        parse_workflow(_wf(**over), "web-lookup", Path("p"), KNOWN)


def test_본문이_비면_거부한다():
    text = _wf().split("---\n")[1]
    with pytest.raises(WorkflowError, match="본문"):
        parse_workflow(f"---\n{text}---\n\n", "web-lookup", Path("p"), KNOWN)


def test_빈_capabilities_는_허용한다():
    w = parse_workflow(_wf(capabilities="[]"), "web-lookup", Path("p"), KNOWN)
    assert w.capabilities == ()


def test_잘못된_항목만_거부하고_나머지는_켜진_상태로_싣는다(tmp_path):
    _src(tmp_path / "s", "web", "[web.search, web.fetch]")
    _put(tmp_path / "w", "web-lookup", _wf())
    _put(tmp_path / "w", "bad", _wf(id="bad", capabilities="[ghost.cap]"))
    (tmp_path / "w" / "empty").mkdir()
    reg = load_workflows(tmp_path / "w", tmp_path / "s")
    assert reg.enabled and reg.reason == ""
    assert set(reg.workflows) == {"web-lookup"}
    assert {r.name for r in reg.rejected} == {"bad", "empty"}


def test_소스_레지스트리_전체_오류면_workflow_전체를_끈다(tmp_path):
    _put(tmp_path / "w", "web-lookup", _wf())
    reg = load_workflows(tmp_path / "w", tmp_path / "없는소스")
    assert not reg.enabled and "소스" in reg.reason and not reg.workflows


def test_workflows_루트가_없으면_전체를_끈다(tmp_path):
    _src(tmp_path / "s", "web", "[web.search]")
    reg = load_workflows(tmp_path / "없음", tmp_path / "s")
    assert not reg.enabled and "workflows" in reg.reason


def test_꺼진_소스의_능력은_등록은_되고_사용불가로_표시한다(tmp_path):
    _src(tmp_path / "s", "web", "[web.search, web.fetch]", enabled="false")
    _put(tmp_path / "w", "web-lookup", _wf())
    reg = load_workflows(tmp_path / "w", tmp_path / "s")
    assert "web-lookup" in reg.workflows
    assert reg.unavailable_capabilities("web-lookup") == ("web.search", "web.fetch")
    assert reg.unavailable_capabilities("없음") == ()


def test_켜진_소스가_있으면_사용불가가_아니다(tmp_path):
    _src(tmp_path / "s", "web", "[web.search, web.fetch]")
    _src(tmp_path / "s", "off", "[web.search]", enabled="false")
    _put(tmp_path / "w", "web-lookup", _wf())
    reg = load_workflows(tmp_path / "w", tmp_path / "s")
    assert reg.unavailable_capabilities("web-lookup") == ()


# ---- 렌더

def test_렌더_블록은_id_요지_능력_본문경로를_담는다():
    w = parse_workflow(_wf(), "web-lookup", Path("/x/workflows/web-lookup/WORKFLOW.md"), KNOWN)
    block = render_workflow_block(w)
    lines = block.splitlines()
    assert lines[0] == "[workflow] web-lookup"
    assert "단순 조회" in lines[1]
    assert "web.search, web.fetch" in block
    assert "/x/workflows/web-lookup/WORKFLOW.md" in block
    assert "1. 검색한다" not in block
    assert len(lines) <= 4


def test_렌더는_긴_요지를_자른다():
    w = parse_workflow(_wf(description="가" * 500), "web-lookup", Path("p"), KNOWN)
    assert len(render_workflow_block(w).splitlines()[1]) <= 130


def test_렌더_결과는_요청헤더_제목_아래에_붙일_수_있는_순수_문자열이다():
    w = parse_workflow(_wf(), "web-lookup", Path("p"), KNOWN)
    assert render_workflow_block(w) == render_workflow_block(w)
    assert "**" not in render_workflow_block(w)


# ---- 재개 규칙

def _reg(tmp_path):
    _src(tmp_path / "s", "web", "[web.search, web.fetch]")
    _src(tmp_path / "s", "loc", "[local.files]")
    _put(tmp_path / "w", "web-lookup", _wf())
    _put(tmp_path / "w", "deep", _wf(id="deep", capabilities="[web.search, web.fetch, local.files]"))
    _put(tmp_path / "w", "files", _wf(id="files", capabilities="[local.files]"))
    return load_workflows(tmp_path / "w", tmp_path / "s")


def test_첫_턴은_라우팅_결과_그대로(tmp_path):
    d = resume_decision(_reg(tmp_path), opened="", routed="web-lookup")
    assert (d.workflow_id, d.capabilities, d.widened) == ("web-lookup", ("web.search", "web.fetch"), False)


def test_재개_턴에_더_넓은_workflow_로_라우팅되면_넓어진다(tmp_path):
    d = resume_decision(_reg(tmp_path), opened="web-lookup", routed="deep")
    assert d.workflow_id == "deep" and d.widened
    assert set(d.capabilities) == {"web.search", "web.fetch", "local.files"}


def test_재개_턴에_더_좁은_workflow_로_라우팅돼도_좁히지_않는다(tmp_path):
    d = resume_decision(_reg(tmp_path), opened="deep", routed="web-lookup")
    assert d.workflow_id == "deep" and not d.widened
    assert set(d.capabilities) == {"web.search", "web.fetch", "local.files"}


def test_서로_포함하지_않으면_합집합이고_id_는_라우팅_쪽(tmp_path):
    d = resume_decision(_reg(tmp_path), opened="web-lookup", routed="files")
    assert d.workflow_id == "files" and d.widened
    assert set(d.capabilities) == {"web.search", "web.fetch", "local.files"}


def test_같은_workflow_면_변화_없음(tmp_path):
    d = resume_decision(_reg(tmp_path), opened="deep", routed="deep")
    assert d.workflow_id == "deep" and not d.widened


def test_열린_workflow_가_사라졌으면_라우팅_결과만_쓴다(tmp_path):
    d = resume_decision(_reg(tmp_path), opened="삭제됨", routed="files")
    assert d.workflow_id == "files" and d.capabilities == ("local.files",) and not d.widened


def test_라우팅_결과가_없으면_열린_workflow_를_유지한다(tmp_path):
    d = resume_decision(_reg(tmp_path), opened="deep", routed="")
    assert d.workflow_id == "deep"


def test_둘_다_모르면_None_이다(tmp_path):
    assert resume_decision(_reg(tmp_path), opened="x", routed="y") is None
    assert resume_decision(_reg(tmp_path), opened="", routed="") is None


def test_꺼진_레지스트리는_None_이다(tmp_path):
    reg = load_workflows(tmp_path / "없음", tmp_path / "없음2")
    assert resume_decision(reg, opened="a", routed="b") is None


# ---- 설정 키

def test_workflows_enabled_기본은_꺼짐이고_override_로_켠다():
    assert RuntimeSettings().workflows_enabled is False
    assert RuntimeSettings().override({"workflows_enabled": True}).workflows_enabled is True


def test_꺼진_레지스트리는_workflow_가_들어_있어도_None_이다(tmp_path):
    from dataclasses import replace

    live = _reg(tmp_path)
    off = replace(live, enabled=False, reason="x")
    assert resume_decision(off, opened="deep", routed="deep") is None
