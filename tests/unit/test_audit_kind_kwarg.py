"""감사 기록 호출에 kind= 를 넘기면 AuditLog.record(kind, **fields) 와 겹쳐
TypeError 로 요청이 실패한다. 2026-11-08 16:10 위임 회신 기록에서 실측."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "slack_cli_agent"


def test_no_audit_record_call_passes_kind_keyword() -> None:
    bad = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "record" and node.args):
                continue
            first = ast.unparse(node.args[0])
            if "IncidentKind" not in first:
                continue
            if any(k.arg == "kind" for k in node.keywords):
                bad.append(f"{path.name}:{node.lineno}")
    assert bad == []
