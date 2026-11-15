"""WORKFLOW.md 로더·검증기, 요청 헤더용 렌더, 재개 턴 규칙.

`workflows/<id>/WORKFLOW.md` 프론트매터

    id            디렉터리 이름과 같아야 한다
    description   쓸 때와 쓰지 않을 때 (한 줄)
    capabilities  능력 이름 목록. 소스 레지스트리에 없는 이름이면 그 workflow 거부
    model         선택
    effort        선택. low|medium|high|xhigh|max
    verify        off | standard | strict (완료 검토 강도)

이 모듈은 파이프라인에 연결돼 있지 않다. 연결은 `RuntimeSettings.workflows_enabled`
가 켜진 뒤 별도 변경으로 한다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .common import (
    Rejection,
    RegistryError,
    check_capability_names,
    check_keys,
    check_name,
    reject_secret,
    require_list,
    require_text,
    scan_directory,
)
from .frontmatter import parse_document
from .sources import load_sources

WORKFLOW_FILE = "WORKFLOW.md"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
VERIFY_LEVELS = ("off", "standard", "strict")
_KEYS = frozenset({"id", "description", "capabilities", "model", "effort", "verify"})
_SUMMARY_LIMIT = 120


class WorkflowError(RegistryError):
    pass


@dataclass(frozen=True)
class Workflow:
    id: str
    description: str
    capabilities: tuple[str, ...]
    model: str
    effort: str
    verify: str
    body: str
    path: Path


def parse_workflow(text: str, dir_name: str, path: Path, known_capabilities: frozenset[str]) -> Workflow:
    try:
        reject_secret(text)
        meta, body = parse_document(text)
        check_keys(meta, _KEYS)
        wid = require_text(meta, "id")
        check_name(wid, dir_name, "id")
        caps = require_list(meta, "capabilities")
        check_capability_names(caps)
        unknown = [c for c in caps if c not in known_capabilities]
        if unknown:
            raise WorkflowError(f"레지스트리에 없는 능력: {', '.join(unknown)}")
        effort = require_text(meta, "effort", required=False)
        if effort and effort not in EFFORTS:
            raise WorkflowError(f"effort 는 {'|'.join(EFFORTS)} 중 하나여야 한다: {effort!r}")
        verify = require_text(meta, "verify")
        if verify not in VERIFY_LEVELS:
            raise WorkflowError(f"verify 는 {'|'.join(VERIFY_LEVELS)} 중 하나여야 한다: {verify!r}")
        if not body.strip():
            raise WorkflowError("본문이 비어 있다. 조회 순서·완료 조건·금지 사항을 적는다")
        return Workflow(
            id=wid,
            description=require_text(meta, "description"),
            capabilities=caps,
            model=require_text(meta, "model", required=False),
            effort=effort,
            verify=verify,
            body=body,
            path=path,
        )
    except WorkflowError:
        raise
    except RegistryError as exc:
        raise WorkflowError(str(exc)) from exc


@dataclass(frozen=True)
class WorkflowRegistry:
    """로드 결과. enabled=False 면 workflow 기능 전체가 꺼진 것이고 reason 이 사유다.

    경보는 호출 측이 낸다. 이 객체는 값만 돌려준다.
    """

    enabled: bool
    reason: str
    workflows: Mapping[str, Workflow]
    rejected: tuple[Rejection, ...]
    #: 켜진 소스가 하나도 제공하지 않는 능력. 등록은 돼 있으나 지금 쓸 수 없다.
    _unavailable: frozenset[str] = frozenset()

    def unavailable_capabilities(self, workflow_id: str) -> tuple[str, ...]:
        wf = self.workflows.get(workflow_id)
        if wf is None:
            return ()
        return tuple(c for c in wf.capabilities if c in self._unavailable)


def _disabled(reason: str) -> WorkflowRegistry:
    return WorkflowRegistry(False, reason, {}, ())


def load_workflows(workflows_root: Path, sources_root: Path) -> WorkflowRegistry:
    loaded = load_sources(sources_root)
    if loaded.error:
        return _disabled(f"소스 레지스트리 오류: {loaded.error}")
    known = frozenset(c for s in loaded.sources for c in s.capabilities)
    live = frozenset(c for s in loaded.sources if s.enabled for c in s.capabilities)

    def parse(text: str, dir_name: str, path: Path) -> Workflow:
        return parse_workflow(text, dir_name, path, known)

    items, rejected, error = scan_directory(workflows_root, WORKFLOW_FILE, parse)
    if error:
        return _disabled(f"workflows 디렉터리 오류: {error}")
    return WorkflowRegistry(True, "", {w.id: w for w in items}, tuple(rejected), known - live)


def render_workflow_block(workflow: Workflow) -> str:
    """요청 헤더(prompt/sections.render_request_header)에 한 덩어리로 붙일 블록.

    id, 요지 한 줄, 능력, 본문 파일 경로만 담는다. 본문은 싣지 않는다 - 모델이
    필요할 때 Read 로 연다. 시스템 지침이 아니라 요청 헤더에 두는 이유: claude
    --resume 은 --append-system-prompt 를 버리므로(session/manager.py) 재개
    턴에는 시스템 지침 쪽 주입이 가지 않는다. 헤더는 매 턴 사용자 프롬프트 앞에
    붙는다. 입력이 같으면 출력이 같은 순수 함수다.
    """
    summary = " ".join(workflow.description.split())
    if len(summary) > _SUMMARY_LIMIT:
        summary = summary[: _SUMMARY_LIMIT - 1] + "…"
    caps = ", ".join(workflow.capabilities) or "없음"
    return "\n".join(
        [
            f"[workflow] {workflow.id}",
            summary,
            f"능력: {caps}",
            f"본문: {workflow.path} (필요하면 Read 로 연다)",
        ]
    )


@dataclass(frozen=True)
class ResumeDecision:
    workflow_id: str
    capabilities: tuple[str, ...]
    #: 열린 workflow 의 능력보다 넓어졌는가.
    widened: bool


def resume_decision(registry: WorkflowRegistry, *, opened: str, routed: str) -> ResumeDecision | None:
    """재개 턴에 적용할 workflow 와 능력. 규칙은 "넓히기만 허용" 이다.

    opened 는 세션을 연 턴의 workflow id, routed 는 이번 턴 라우터가 고른 id
    (둘 다 없으면 ""). 반환 None 은 적용할 workflow 가 없다는 뜻이다(기능 꺼짐,
    둘 다 모르는 id).

    - 라우팅 능력이 열린 workflow 능력의 부분집합이면 열린 것을 유지한다
    - followup(스레드에서 직전 workflow 상속)은 도구를 줄이지 않는다. 능력을
      deep 과 같게 선언해 두므로, 열린 workflow 를 모르면 deep 으로, 알면 위
      규칙에 따라 열린 것이 유지된다. 상속 자체는 호출 측이 routed 를 ""
      (열린 것 유지)로 넘겨 표현할 수 있다
    - 아니면 합집합 능력으로 넓히고 id 는 이번 라우팅 결과로 바꾼다
    - 좁히는 경우는 없다

    "세션을 연 workflow 유지" 가 아니라 넓히기를 고른 근거:
    1. core/mcp_routing.py 가 재개 턴에 앞서 붙인 서버를 계속 붙이는 것과 같은
       이유다. 대화 기록의 도구 호출이 가리키는 도구가 허용 목록에서 사라지면
       이어 가는 맥락이 깨진다. 넓히기만 하면 한 번 허용된 도구는 세션 동안 남는다.
    2. 재개 턴이 61% 다(검토자 보고, 미실측). 짧은 후속 질문("그래서?")은 라우터가
       매 턴 다른 workflow 로 판정할 수 있어, 따라가면 도구 집합이 턴마다 흔들린다.
       좁히지 않으므로 흔들림은 넓어지는 방향 한 번뿐이다.
    3. 단순 유지는 웹 조회로 연 세션에서 개인 자료를 묻는 후속 질문이 도구가 없어
       실패하거나 새 세션을 강제한다. 넓히기는 그 경우를 통과시킨다.
    치르는 값: 넓어진 세션은 이후 줄어들지 않아 최소 권한이 약해진다. 세션 TTL
    (session_ttl_hours)이 끝나면 새로 열린다. 모델·effort 는 이 함수가 다루지
    않는다 - 재개 세션의 캐시와 맥락을 따라 열린 쪽을 유지하는 것이 호출 측 몫이다.
    """
    if not registry.enabled:
        return None
    opened_wf = registry.workflows.get(opened) if opened else None
    routed_wf = registry.workflows.get(routed) if routed else None
    if opened_wf is None and routed_wf is None:
        return None
    if opened_wf is None or routed_wf is None:
        only = opened_wf or routed_wf
        assert only is not None
        return ResumeDecision(only.id, only.capabilities, False)
    if set(routed_wf.capabilities) <= set(opened_wf.capabilities):
        return ResumeDecision(opened_wf.id, opened_wf.capabilities, False)
    extra = tuple(c for c in routed_wf.capabilities if c not in opened_wf.capabilities)
    return ResumeDecision(routed_wf.id, (*opened_wf.capabilities, *extra), True)
