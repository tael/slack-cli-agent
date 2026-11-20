"""레지스트리 1회 점검. 소스마다 health 를 돌리고 workflow 로드 결과를 함께 낸다.

루트 디렉터리 구조: `<root>/sources/<이름>/SOURCE.md`, `<root>/workflows/<id>/WORKFLOW.md`.
`workflows/` 가 없으면 소스만 점검한다.
실패(fail)나 거부(reject)가 하나라도 있으면 종료코드 1. skip 은 실패가 아니다.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .health import DEFAULT_TIMEOUT_SEC, check_source
from .sources import load_sources
from .workflows import load_workflows


@dataclass(frozen=True)
class CheckRow:
    kind: str  # source | workflow
    name: str
    status: str  # ok | fail | skip | reject
    detail: str


def run_check(root: Path, *, timeout: float = DEFAULT_TIMEOUT_SEC) -> tuple[list[CheckRow], int]:
    rows: list[CheckRow] = []
    loaded = load_sources(root / "sources")
    if loaded.error:
        return [CheckRow("source", "(전체)", "fail", loaded.error)], 1
    for source in loaded.sources:
        result = check_source(source, timeout=timeout)
        rows.append(CheckRow("source", source.name, result.status, result.detail))
    rows.extend(CheckRow("source", r.name, "reject", r.reason) for r in loaded.rejected)
    workflows_root = root / "workflows"
    if workflows_root.is_dir():
        registry = load_workflows(workflows_root, root / "sources")
        if not registry.enabled:
            rows.append(CheckRow("workflow", "(전체)", "fail", registry.reason))
        for wid in registry.workflows:
            unavailable = registry.unavailable_capabilities(wid)
            detail = f"사용 불가 능력(꺼진 소스만 제공): {', '.join(unavailable)}" if unavailable else ""
            rows.append(CheckRow("workflow", wid, "ok", detail))
        rows.extend(CheckRow("workflow", r.name, "reject", r.reason) for r in registry.rejected)
    failed = any(r.status in ("fail", "reject") for r in rows)
    return rows, 1 if failed else 0


def format_table(rows: list[CheckRow]) -> str:
    header = ("구분", "이름", "상태", "사유")
    body = [(r.kind, r.name, r.status, r.detail) for r in rows]
    widths = [max(len(row[i]) for row in [header, *body]) for i in range(3)]
    lines = ["  ".join(c.ljust(w) for c, w in zip(row[:3], widths, strict=True)) + "  " + row[3]
             for row in [header, *body]]
    return "\n".join(line.rstrip() for line in lines)
