"""UserPromptSubmit hook: start the engine's plan call alongside the turn.

2026-11-10 W5: plan_gate asked the model for a `[계획]` block and denied the
third tool-calling response without one. All 15 runs ended in two denials
and gave_up. Claude Code 2.1.321 writes a response's text and tool_use lines
to the transcript 50-110 ms after its PreToolUse hook starts, so a plan
written in the same response as the call is invisible to the hook, and the
model wrote no text with its first call in 15 of 15 runs anyway
(tasks/guchipachi-orchestration/plan_gate_rca.md). The plan is now made by
code instead of being asked of the model.

On each request of a turn the pipeline set PLAN_GATE for, this hook removes
the previous request's plan files and starts a detached planner process,
then returns at once. The planner runs a tool-less nested ``claude -p``
(default haiku) with a JSON schema and writes ``<transcript>.plan.json``.
guard/pretool/plan_gate injects the finished plan once as PreToolUse
additionalContext. It never denies, so a plan that arrives late, fails or
says "short" only means nothing is injected.

The plan file is also the record: status is ``ok``, ``timeout``,
``error:<kind>`` or ``bad_output``, never a substitute plan.

Workflow classification (record only). Where the pipeline sets
SCA_PLAN_WORKFLOWS (JSON ``{id: description}``), the same call also picks the
workflow that fits the request's meaning and writes ``workflow``,
``workflow_confidence`` and ``workflow_reason`` into the plan file. No length
or keyword rule: the description of each candidate carries example utterances.
plan_gate never reads these fields, so the injected text and the turn's
behavior are unchanged. On a resumed turn the tail of the earlier conversation
(transcript) is added after the request, because short follow-ups cannot be
read without it.

Standard library only. Never fails the prompt: every path ends in exit 0
with nothing on stdout (UserPromptSubmit stdout would be added to context).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

HOOK_SCRIPT = Path(__file__).resolve()

#: Same switch as plan_gate: set by the pipeline on turns the flow applies to.
PLAN_GATE_ENV = "PLAN_GATE"
#: Set in the planner's own environment so nothing it starts plans again.
PLAN_NESTED_ENV = "SCA_PLAN_NESTED"
#: Set inside the finish-review verify claude (finish_review.VERIFY_NESTED_ENV).
VERIFY_NESTED_ENV = "SCA_VERIFY_NESTED"
#: Set by the pipeline when this turn's tool list has Agent or Task. Only then
#: may the planner name a worker; otherwise every branch's delegate is "none".
AGENT_DELEGATION_ENV = "AGENT_DELEGATION"
#: Planner model override. haiku measured 4.3-6.6 s for four plans (RCA 재현 6).
PLAN_MODEL_ENV = "SCA_PLAN_MODEL"
DEFAULT_MODEL = "haiku"
#: Planner call limit. Past this the turn is usually well under way.
TIMEOUT_SEC = 20.0
#: Tail of the request handed to the planner. The pipeline's prompt carries
#: thread history before the request itself, so the end matters most.
PROMPT_TAIL_CHARS = 12_000

#: Set by the pipeline: JSON object ``{workflow id: description}`` of the
#: candidates the planner may choose from. Absent or invalid means the planner
#: classifies nothing and the plan file has no workflow fields.
WORKFLOWS_ENV = "SCA_PLAN_WORKFLOWS"
#: Set by the pipeline to "1" when the profile's plan_intake is on: the plan
#: also records the request's interpretation, assumed and missing inputs, an
#: ack for the user, the expected output and checks. Absent or "0" means off.
INTAKE_ENV = "SCA_PLAN_INTAKE"
INTAKE_TEXT_FIELDS = ("intent", "ask", "expected_output", "ack")
INTAKE_LIST_FIELDS = ("assumed", "missing")
INTAKE_FIELDS = ("intent", "assumed", "missing", "ask", "expected_output", "ack", "checks")
#: Set by the pipeline to "2" (with intake on) for the v2 intake fields: every
#: input with a kind, and the output's form and items.
INTAKE_SCHEMA_ENV = "SCA_PLAN_INTAKE_SCHEMA"
INTAKE_FIELDS_V2 = ("intent", "inputs", "ask", "output", "ack")
#: Optional intake field (both schema versions, never required): "none" when
#: the request needs no lookup, write or send. core/intake_ack skips the ack
#: then, so an acknowledgement-only message gets one answer, not two.
WORK_VALUES = ("none", "some")
INTAKE_V1_ONLY = ("assumed", "missing", "expected_output", "checks")
#: Copies of guard/pretool/plan_gate's (that file is a standalone hook script).
INPUT_KINDS = ("given", "derived", "lookup", "guess", "ask")
OUTPUT_FORMS = ("한두줄", "문단", "목록", "표", "긴글")
INPUTS_MAX = 6
OUTPUT_ITEMS_MAX = 6
OUTPUT_ITEMS_SHORT_MAX = 2
#: Set by the pipeline from the profile's plan_refine: "separate" runs one
#: extra tool-less call that rewrites the request before the planner,
#: "combined" asks the planner itself to refine first. Anything else is off.
REFINE_ENV = "SCA_PLAN_REFINE"
REFINE_MODES = ("separate", "combined")
REFINE_FIELDS = ("refined", "resolved", "unresolved")
#: Refine call limit in separate mode; on a miss the raw request is planned.
REFINE_TIMEOUT_SEC = 15.0
#: Set by the pipeline (with intake on): JSON list of the tool names this turn
#: can use. The planner then picks `source` only from it.
SOURCES_ENV = "SCA_PLAN_SOURCES"
SOURCES_MAX = 60
SOURCE_NAME_CHARS = 80
KST = timezone(timedelta(hours=9), "KST")
WEEKDAYS = "월화수목금토일"
#: The fallback workflow when the planner is not confident.
FALLBACK_WORKFLOW = "deep"
CONFIDENCES = ("low", "medium", "high")
#: Resumed-turn context: the last N text messages of the transcript, each cut
#: to a limit, and a limit on the whole block.
CONTEXT_MESSAGES = 4
CONTEXT_MESSAGE_CHARS = 300
CONTEXT_TOTAL_CHARS = 1_200
#: Only this much of the transcript's end is read to find them.
CONTEXT_READ_BYTES = 400_000

#: Agent worker names the flow note delegates to (profiles/agents/*.md).
WORKERS = ("lookup", "analyst")

def delegation_enabled(env: dict[str, str] | os._Environ[str] | None = None) -> bool:
    """AGENT_DELEGATION on: set and not "0". Absent or empty means off."""
    raw = (env if env is not None else os.environ).get(AGENT_DELEGATION_ENV, "")
    return str(raw).strip() not in ("", "0")


def workflow_candidates(env: dict[str, str] | os._Environ[str] | None = None) -> dict[str, str]:
    """``{id: description}`` from SCA_PLAN_WORKFLOWS; empty when absent or invalid."""
    raw = (env if env is not None else os.environ).get(WORKFLOWS_ENV, "")
    if not str(raw).strip():
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        k: " ".join(v.split()) for k, v in value.items()
        if isinstance(k, str) and k and isinstance(v, str) and v.strip()
    }


def intake_enabled(env: dict[str, str] | os._Environ[str] | None = None) -> bool:
    """SCA_PLAN_INTAKE on: set and not "0". Absent or empty means off."""
    raw = (env if env is not None else os.environ).get(INTAKE_ENV, "")
    return str(raw).strip() not in ("", "0")


def intake_schema(env: dict[str, str] | os._Environ[str] | None = None) -> int:
    """SCA_PLAN_INTAKE_SCHEMA as 2 or 1 (the default for anything else)."""
    raw = (env if env is not None else os.environ).get(INTAKE_SCHEMA_ENV, "")
    return 2 if str(raw).strip() == "2" else 1


def refine_mode(env: dict[str, str] | os._Environ[str] | None = None) -> str:
    """"separate", "combined" or "" (off, also for any other value)."""
    raw = str((env if env is not None else os.environ).get(REFINE_ENV, "")).strip()
    return raw if raw in REFINE_MODES else ""


def sources_list(env: dict[str, str] | os._Environ[str] | None = None) -> list[str]:
    """Tool names from SCA_PLAN_SOURCES; empty when absent or invalid."""
    raw = (env if env is not None else os.environ).get(SOURCES_ENV, "")
    try:
        value = json.loads(raw) if str(raw).strip() else []
    except ValueError:
        return []
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip() and item.strip()[:SOURCE_NAME_CHARS] not in names:
            names.append(item.strip()[:SOURCE_NAME_CHARS])
    return names[:SOURCES_MAX]


def _refine_properties() -> dict[str, Any]:
    text = {"type": "string"}
    return {
        "refined": text,
        "resolved": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"from": text, "to": text, "basis": text},
                "required": ["from", "to", "basis"],
            },
        },
        "unresolved": {"type": "array", "items": text},
    }


def refine_schema() -> dict[str, Any]:
    return {"type": "object", "properties": _refine_properties(), "required": list(REFINE_FIELDS)}


def _intake_properties_v2() -> dict[str, Any]:
    text = {"type": "string"}
    return {
        "intent": text,
        "inputs": {
            "type": "array",
            "maxItems": INPUTS_MAX,
            "items": {
                "type": "object",
                "properties": {
                    "name": text, "value": text, "kind": {"type": "string", "enum": list(INPUT_KINDS)},
                    "reason": text,
                },
                "required": ["name", "value", "kind", "reason"],
            },
        },
        "ask": text,
        "output": {
            "type": "object",
            "properties": {
                "form": {"type": "string", "enum": list(OUTPUT_FORMS)},
                "items": {"type": "array", "maxItems": OUTPUT_ITEMS_MAX, "items": text},
            },
            "required": ["form", "items"],
        },
        "ack": text,
        "work": {"type": "string", "enum": list(WORK_VALUES)},
    }


def schema(
    delegation: bool = False, workflows: Any = None, intake: bool = False, refine: str = "",
    intake_version: int = 1,
) -> dict[str, Any]:
    """The planner's JSON schema. Workers are enum values only with delegation.
    Workflow fields are there only when candidate ids are given, intake fields
    only with intake, refine fields only in combined mode."""
    result = _base_schema(delegation)
    if refine == "combined":
        result["properties"].update(_refine_properties())
        result["required"] = [*result["required"], *REFINE_FIELDS]
    if intake and intake_version == 2:
        result["properties"].update(_intake_properties_v2())
        result["required"] = [*result["required"], *INTAKE_FIELDS_V2]
    elif intake:
        text = {"type": "string"}
        texts = {"type": "array", "items": {"type": "string"}}
        result["properties"].update({
            "intent": text, "assumed": texts, "missing": texts, "ask": text,
            "expected_output": text, "ack": text,
            "work": {"type": "string", "enum": list(WORK_VALUES)},
            "checks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"what": text, "how": text},
                    "required": ["what", "how"],
                },
            },
        })
        result["required"] = [*result["required"], *INTAKE_FIELDS]
    if workflows:
        result["properties"]["workflow"] = {"type": "string", "enum": list(workflows)}
        result["properties"]["workflow_confidence"] = {"type": "string", "enum": list(CONFIDENCES)}
        result["properties"]["workflow_reason"] = {"type": "string"}
        result["required"] = [*result["required"], "workflow", "workflow_confidence", "workflow_reason"]
    return result


def _base_schema(delegation: bool) -> dict[str, Any]:
    workers = list(WORKERS) if delegation else []
    return {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["short", "branch", "explore"]},
            "done_when": {"type": "array", "maxItems": 3, "items": {"type": "string"}},
            "branches": {
                "type": "array",
                "maxItems": 5,
                "items": {
                    "type": "object",
                    "properties": {
                        "goal": {"type": "string"},
                        "source": {"type": "string"},
                        "depends_on": {"type": "array", "items": {"type": "integer"}},
                        "delegate": {"type": "string", "enum": ["none", *workers]},
                        "returns": {"type": "string"},
                    },
                    "required": ["goal", "source", "depends_on", "delegate", "returns"],
                },
            },
        },
        "required": ["type", "branches"],
    }


_PLANNER_HEAD = (
    "너는 슬랙 업무 봇의 작업 계획만 세운다. 도구는 없다. 아래 [요청]을 읽고 JSON 으로만 답한다.\n"
    "1. type 을 위에서 아래 순서로 정한다\n"
    "- explore : 원인이나 대상을 모르고 앞 결과가 다음 조회를 정한다. 5why, 부검, RCA, 근본원인, "
    "장애·오류 원인 분석, 재발방지 요청은 항상 explore 이고 첫 갈래를 원천별(로그, 메트릭, k8s 이벤트, "
    "DB, 코드, 슬랙)로 2-3개 적는다\n"
    "- branch : 답에 필요한 조회나 작업 단위가 둘 이상이다. 순서 의존은 depends_on 으로 적는다. branches 는 2-5개다\n"
    "- short : 그 밖의 요청이다. branches 는 빈 배열이다\n"
    "2. done_when 에 이 요청이 끝났다고 볼 조건을 1-3개 적는다. 요청에 나온 것만 쓴다. short 이면 빈 배열이다\n"
    "3. 갈래마다 적는다\n"
    "- goal : 알아낼 것 한 문장\n"
    "- source : 볼 원천. 테이블, 파일, API, 채널 중 요청에 나온 것. 모르면 '확인 필요'\n"
    "- depends_on : 먼저 끝나야 하는 갈래 번호(1부터). 없으면 빈 배열\n"
)
_DELEGATE_WITH_WORKERS = (
    "- delegate : 도구 호출이 3회 이상 들 갈래만 작업자에게 맡긴다. 조회만이면 lookup, "
    "집계 스크립트 실행이 필요하면 analyst, 그보다 작으면 none\n"
)
_DELEGATE_NONE = "- delegate : 항상 none 으로 적는다. 이 턴에는 작업자가 없다\n"
_PLANNER_TAIL = (
    "- returns : 돌려받을 것. 수치, 목록, 판정 중 하나와 그 대상\n"
    "4. 요청에 없는 작업을 만들지 않는다. 쓰기, 게시, 삭제가 요청에 없으면 갈래에 넣지 않는다\n\n"
    "[요청]\n"
)


_WORKFLOW_HEAD = (
    "4. workflow 를 고른다. 아래 후보 중 요청의 의미에 맞는 id 하나다\n"
    "- 슬랙 질문은 대부분 짧다. 글자 수나 낱말로 정하지 않는다. 무엇을 알고 싶은지 추정해 고른다\n"
    "- 짧은 질문이 가벼운 경우는 드물다. 대상이 모호하거나 직전 대화에 기대는 후속 발화는 "
    "[직전 대화]가 있으면 그 맥락으로 읽는다\n"
    "- workflow_confidence 는 low, medium, high 중 하나다. 확신이 낮으면 workflow 는 "
    f"{FALLBACK_WORKFLOW} 로 하고 low 로 적는다\n"
    "- workflow_reason 은 고른 근거 한 문장이며 40자 이내다. 길게 쓰면 계획이 늦어진다\n"
    "[workflow 후보]\n"
)


def workflow_section(workflows: dict[str, str]) -> str:
    if not workflows:
        return ""
    return _WORKFLOW_HEAD + "".join(f"- {wid} : {desc}\n" for wid, desc in workflows.items())


_WORK_FIELD_LINE = (
    "- work : 조회·수정·전송·계산할 일이 하나라도 있으면 some 이다. 확인·동의·인사·감탄처럼 "
    "앞선 답을 받아들이는 말이고 새로 할 일이 없으면 none 이고 그때는 ack 를 빈 문자열로 둔다\n"
)


_INTAKE_SECTION = (
    "[입력 점검] 아래 필드도 적는다. 길게 쓰면 계획이 늦어진다\n"
    "- intent : 요청을 어떻게 이해했는지 한 문장\n"
    "- assumed : 요청에 없어 추정으로 채운 입력 목록. 없으면 빈 배열\n"
    "- missing : 추정할 수 없는 입력 목록. 없으면 빈 배열. "
    "파일 위치·설정 값·기록처럼 도구로 찾을 수 있는 값은 missing 이 아니다\n"
    "- ask : missing 이 있을 때만 되묻는 질문 한 문장. 아니면 빈 문자열. 쓰기·전송·삭제 요청에서 "
    "대상이나 필수 값을 정할 수 없으면 missing 에 적고 ask 는 비우지 않고 질문으로 적는다\n"
    "- expected_output : 사용자가 받을 결과의 형태와 대상\n"
    "- ack : 사용자에게 바로 보낼 한두 문장. 추정한 해석과 확인하거나 할 일만 적는다. "
    "공감·위로·상담 문구와 요청에 없는 내용은 넣지 않는다. ack 에는 질문을 넣지 않는다. 되물을 것은 ask 에만 적는다. "
    "예: \"X 기준으로 Y 를 확인해볼게요\"\n"
    "ack 는 이 단계에서 확인한 사실이 없다. 추정을 사실로 쓰지 않고 \"X 는 Y 가 아니다\", \"X 는 없다\" 처럼 대상의 존재·소속을 부정하지 않는다. "
    "제품·모델·서비스·회사 이름의 정체를 모르면 \"X 가 무엇인지 웹에서 먼저 확인할게요\" 로 쓴다. 학습 시점 이후에 나온 것일 수 있다\n"
    "- checks : 답하기 전에 만족해야 할 조건 목록. what 은 조건, how 는 확인할 도구나 원천이다. "
    "도구 결과로 확인되는 조건만 적고 의견은 적지 않는다\n"
    + _WORK_FIELD_LINE
)


_INTAKE_SECTION_V2 = (
    "[입력 점검] 아래 필드도 적는다. 짧게 쓴다. 길게 쓰면 계획이 늦어진다\n"
    "- intent : 요청을 어떻게 이해했는지 한 문장\n"
    "- inputs : 이 일에 필요한 직접 입력을 모두 나열한다. 고르는 기준·포함 범위처럼 해석이 갈리는 값도 입력이다. "
    "항목은 name, value, kind, reason 이다. reason 은 짧게 쓰고 given 은 빈 문자열이다. kind 는 다음 중 하나다\n"
    "    - given : 요청에 적혀 있다\n"
    "    - derived : 현재 시각이나 직전 대화에서 계산된다\n"
    "    - lookup : 도구로 찾을 수 있다. 파일 위치·설정 값·기록, 제품·모델·서비스 이름의 정체(웹검색)는 lookup 이고 절대 묻지 않는다. 기억에 없거나 기억과 다른 조합이면 요청에 적힌 수식어를 그대로 두고 lookup 으로 적는다. 요청에 적힌 낱말을 틀렸다고 고치지 않는다\n"
    "    - guess : 요청에 없지만 가장 가능성 높은 값을 근거(언어·화자·시각·직전 대화)와 함께 적는다. "
    "사람은 짧게 쓰므로 되묻기보다 추정을 택한다. 단 쓰기·전송·삭제의 대상은 guess 로 채우지 않는다. "
    "요청이나 도구로 정할 수 없으면 ask 다\n"
    "    - ask : 근거 있는 추정이 없거나 쓰기·전송·삭제의 대상을 정할 수 없을 때만 쓴다\n"
    "- ask : kind 가 ask 인 입력이 있을 때만 되묻는 질문 한 문장. 아니면 빈 문자열\n"
    "- output : 답이 담아야 할 결과. form 은 한두줄·문단·목록·표·긴글 중 하나다. items 는 요청이 직접 물은 것만 "
    "짧은 명사구로 적는다. 한두줄이면 2개 이하, 그 밖은 최대 6개다. "
    "'있으면' 같은 조건부 항목과 추정 공개는 넣지 않는다\n"
    "- ack : 사용자에게 바로 보낼 한두 문장. \"X 기준으로 Y 를 찾아볼게요\" 꼴로 추정한 값(guess)을 밝힌다. "
    "추정을 사실로 쓰지 않고 대상의 존재·소속을 부정하지 않는다(\"X 는 Y 가 아니라서\" 금지). 정체를 모르는 이름은 \"X 가 무엇인지 웹에서 먼저 확인할게요\" 로 쓴다. "
    "질문은 넣지 않는다. 공감·위로·상담 문구와 요청에 없는 내용은 넣지 않는다\n"
    + _WORK_FIELD_LINE
)


_COMBINED_REFINE_SECTION = (
    "[입력 정제] 먼저 요청을 정제하고 그 결과로 계획한다\n"
    "- refined : 지시어·상대 날짜·줄임말을 구체 값으로 바꿔 그 자체로 읽히게 다시 쓴 요청. "
    "범위를 넓히거나 줄이지 않고, 대상어(예: \"치과 캘린더에 넣어줘\" 의 치과)를 다른 뜻으로 바꾸지 않는다. "
    "원문에 없는 대상·지역·원천·기준·출력 형식·작업 범위를 더하지 않는다. 근거가 없는 표현은 원문 그대로 두고 "
    "unresolved 에 적는다. unresolved 에 적은 것을 refined 에 확정해 쓰지 않는다. "
    "추정으로 채울 값은 assumed 에만 적는다. 언어·화자·시각처럼 근거가 있는 확률 높은 추정은 assumed 에 적고 "
    "ack 에 밝힌 뒤 진행한다\n"
    "- resolved : 바꾼 것. from, to, basis. basis 는 직전 대화나 현재 시각처럼 명시된 근거만 적는다. "
    "추측은 적지 않는다\n"
    "- unresolved : 근거가 없어 못 바꾼 것\n"
)


def sources_section(sources: list[str]) -> str:
    if not sources:
        return ""
    return (
        "[쓸 수 있는 원천] " + ", ".join(sources) + "\n"
        "- source 는 이 목록에서 고르거나 '확인 필요' 로 적는다. 목록에 없는 시스템을 만들지 않는다\n"
    )


def planner_prompt(
    delegation: bool = False, workflows: dict[str, str] | None = None, intake: bool = False,
    sources: list[str] | None = None, refine: str = "", intake_version: int = 1,
) -> str:
    """The instruction placed before the request. The workflow section goes
    after item 3 and before the [요청] marker, then the intake section (with
    the source list); absent candidates, an off intake and an off refine leave
    the text exactly as it was. Sources count only with intake."""
    tail = _PLANNER_TAIL
    combined = refine == "combined"
    if workflows or intake or combined:
        marker = "\n[요청]\n"
        head, _, rest = tail.partition(marker)
        section = _INTAKE_SECTION_V2 if intake_version == 2 else _INTAKE_SECTION
        intake_text = (section + sources_section(sources or [])) if intake else ""
        tail = (
            head + workflow_section(workflows or {}) + (_COMBINED_REFINE_SECTION if combined else "")
            + intake_text + marker + rest
        )
    return _PLANNER_HEAD + (_DELEGATE_WITH_WORKERS if delegation else _DELEGATE_NONE) + tail


#: Without delegation, the default.
SCHEMA: dict[str, Any] = schema(False)
PLANNER_PROMPT = planner_prompt(False)


#: Root-cause requests are never "short". On 2026-11-13 haiku planned a
#: "서버 부하 관점으로 5why" request as short with no branches, so no plan was
#: injected and the turn ran without workers (오너 지시 "5why를 short 단답형
#: 으로 간주하면 안됨"). The prompt rule above asks for it; this pattern is the
#: final check: a short verdict on such a request gets one more planner call.
RCA_PATTERN = re.compile(
    r"5\s*-?\s*why|부검|rca\b|근본\s*원인|재발\s*방지|원인\s*(분석|규명|조사)",
    re.IGNORECASE,
)
RCA_RETRY_NOTE = (
    "\n\n[계획 재요청] 이 요청은 원인 분석이다. short 로 답하지 말고 "
    "원천별 첫 갈래 2-3개를 explore 로 적는다.\n"
)


def is_rca(prompt: str) -> bool:
    return bool(RCA_PATTERN.search(prompt))


def plan_path(transcript_path: str) -> Path:
    return Path(transcript_path + ".plan.json")


def injected_path(transcript_path: str) -> Path:
    return Path(transcript_path + ".plan.injected")


def prompt_path(transcript_path: str) -> Path:
    return Path(transcript_path + ".plan.prompt")


def context_path(transcript_path: str) -> Path:
    return Path(transcript_path + ".plan.context")


def _message_text(message: Any, role: str) -> str:
    """The person-readable text of one transcript message, "" for tool traffic.

    A user message carries the pipeline's header before the request, so its
    end is kept; an assistant message keeps its start.
    """
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        content = "\n".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
        )
    if not isinstance(content, str):
        return ""
    text = " ".join(content.split())
    if text.startswith("Stop hook feedback:"):
        return ""
    if len(text) > CONTEXT_MESSAGE_CHARS:
        text = "…" + text[-CONTEXT_MESSAGE_CHARS:] if role == "user" else text[:CONTEXT_MESSAGE_CHARS] + "…"
    return text


def recent_context(transcript_path: str, prompt: str) -> str:
    """The tail of the earlier conversation, or "" on a first turn.

    Reads the transcript's last bytes and keeps the last CONTEXT_MESSAGES text
    messages of the user and the bot, oldest first. A user message that is the
    current prompt (the CLI may have written it already) is left out.
    """
    try:
        with open(transcript_path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - CONTEXT_READ_BYTES))
            raw = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    lines = raw.splitlines()
    if size > CONTEXT_READ_BYTES:
        lines = lines[1:]  # the first line is cut mid-way
    current = " ".join(prompt.split())[-80:]
    found: list[tuple[str, str]] = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("isSidechain"):
            continue
        role = event.get("type")
        if role not in ("user", "assistant"):
            continue
        text = _message_text(event.get("message"), role)
        if not text:
            continue
        if role == "user" and current and text.endswith(current[-60:]):
            continue
        found.append((role, text))
    chosen = found[-CONTEXT_MESSAGES:]
    if not chosen:
        return ""
    label = {"user": "사용자", "assistant": "봇"}
    block = "\n".join(f"{label[role]}: {text}" for role, text in chosen)
    return block[-CONTEXT_TOTAL_CHARS:]


def enabled(env: dict[str, str] | os._Environ[str] | None = None) -> bool:
    source = env if env is not None else os.environ
    if source.get(PLAN_NESTED_ENV) or source.get(VERIFY_NESTED_ENV):
        return False
    try:
        return int(str(source.get(PLAN_GATE_ENV, "")).strip() or 0) > 0
    except ValueError:
        return False


def _write_json(path: Path, value: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


#: Env var that makes start() plan inside the hook and return the plan as the
#: prompt's additionalContext, before the first model response. Absent, empty
#: or "0" keeps the detached planner. In A' replay (2026-11-16) the detached
#: plan arrived after 2-5 direct tool calls and 2 of 9 runs called Agent twice
#: in one response (tasks/shinji-workflow/h8-force-parallel/RESULT.md).
PLAN_SYNC_ENV = "SCA_PLAN_SYNC"
#: Per planner call in sync mode. The prompt waits for it; planner elapsed was
#: at most 13.5 s over 279 plan files (2026-11-16).
SYNC_TIMEOUT_SEC = 15.0
#: guard/pretool/plan_gate.py, loaded by path: both files run as scripts.
PLAN_GATE_SCRIPT = Path(__file__).resolve().parent / "pretool" / "plan_gate.py"


def sync_enabled(env: dict[str, str] | os._Environ[str] | None = None) -> bool:
    raw = (env if env is not None else os.environ).get(PLAN_SYNC_ENV, "")
    return str(raw).strip() not in ("", "0")


def _gate_context(transcript: str) -> str:
    """plan_gate.decide on the finished plan: the injected text, or "" when it
    does not inject. decide also writes the injected flag, so the PreToolUse
    gate does not inject a second time."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_plan_gate_sync", PLAN_GATE_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    verdict, context = module.decide({"transcript_path": transcript})
    return context if verdict == "inject" else ""


def start(data: dict[str, Any], *, spawn: Any = subprocess.Popen, runner: Any = None) -> str:
    """Reset this request's plan files and launch the planner. Returns what
    it did, for tests and the hook's silence."""
    if not enabled():
        return "off"
    if data.get("agent_id"):
        return "subagent"
    transcript = data.get("transcript_path")
    prompt = data.get("prompt")
    if not transcript or not isinstance(prompt, str) or not prompt.strip():
        return "no_input"
    for stale in (plan_path(transcript), injected_path(transcript), context_path(transcript)):
        stale.unlink(missing_ok=True)
    Path(transcript).parent.mkdir(parents=True, exist_ok=True)
    prompt_path(transcript).write_text(prompt[-PROMPT_TAIL_CHARS:], encoding="utf-8")
    if workflow_candidates() or refine_mode() or intake_enabled():  # context readers only
        context = recent_context(transcript, prompt)
        if context:
            context_path(transcript).write_text(context, encoding="utf-8")
    _write_json(plan_path(transcript), {"status": "pending", "started_at": time.time()})
    if sync_enabled():
        (runner or run)(transcript, timeout=SYNC_TIMEOUT_SEC)
        return "sync"
    spawn(
        [sys.executable, str(HOOK_SCRIPT), "--run", transcript],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, env={**os.environ, PLAN_NESTED_ENV: "1"},
    )
    return "started"


def _call_claude(
    prompt: str, model: str, timeout: float, json_schema: dict[str, Any],
) -> subprocess.CompletedProcess[str]:
    exe = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
    cmd = [
        exe, "-p", "--model", model, "--tools", "", "--strict-mcp-config",
        "--mcp-config", '{"mcpServers":{}}', "--setting-sources", "",
        "--output-format", "json", "--json-schema", json.dumps(json_schema),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        out, err = proc.communicate(input=prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def _call_planner(prompt: str, model: str, timeout: float) -> subprocess.CompletedProcess[str]:
    delegation = delegation_enabled()
    workflows = workflow_candidates()
    intake = intake_enabled()
    refine = refine_mode()
    version = intake_schema()
    text = planner_prompt(delegation, workflows, intake, sources_list(), refine, version) + prompt
    return _call_claude(text, model, timeout, schema(delegation, workflows, intake, refine, version))


def _call_refiner(prompt: str, model: str, timeout: float) -> subprocess.CompletedProcess[str]:
    return _call_claude(prompt, model, timeout, refine_schema())


_REFINE_HEAD = (
    "너는 슬랙 업무 봇의 입력 정제 단계다. 도구는 없다. JSON 으로만 답한다.\n"
    "[현재 시각]과 [직전 대화]로 [요청]을 그 자체로 읽히게 다시 쓴다\n"
    "- 지시어(그거), 상대 날짜(어제, 담주 화욜), 줄임말·오타를 구체 값으로 바꾼다\n"
    "- 바꾼 것은 resolved 에 from, to, basis 로 적는다. basis 는 직전 대화 몇 번째 줄이나 현재 시각처럼 "
    "명시된 근거만 쓴다\n"
    "- 근거 없이 짐작해야 하는 해석(예: 3시를 오후로)은 바꾸지 않고 unresolved 에 적는다. 추측해 채우지 않는다\n"
    "- 요청의 의도·범위를 넓히거나 줄이지 않는다\n"
    "- 대상어를 다른 뜻으로 바꾸지 않는다. \"치과 캘린더에 넣어줘\" 는 치과 일정을 캘린더에 넣는 뜻이지 "
    "\"치과 캘린더\" 라는 캘린더가 아니다\n\n"
)


def kst_now_text(now: datetime | None = None) -> str:
    moment = (now or datetime.now(UTC)).astimezone(KST)
    return f"{moment:%Y-%m-%d} ({WEEKDAYS[moment.weekday()]}) {moment:%H:%M} KST"


def refine_prompt(request: str = "", context: str = "", now: datetime | None = None) -> str:
    return (
        _REFINE_HEAD + f"[현재 시각] {kst_now_text(now)}\n[직전 대화]\n{context or '(없음)'}\n[요청]\n{request}\n"
    )


def _clean_refine(value: Any) -> dict[str, Any] | None:
    """The refine fields in canonical form, or None when malformed. A resolved
    item without a basis is a guess: it moves to unresolved."""
    if not isinstance(value, dict):
        return None
    refined, resolved, unresolved = (value.get(key) for key in REFINE_FIELDS)
    if not isinstance(refined, str) or not refined.strip():
        return None
    if not isinstance(resolved, list) or not isinstance(unresolved, list):
        return None
    if not all(isinstance(item, str) for item in unresolved):
        return None
    kept: list[dict[str, str]] = []
    moved: list[str] = []
    for item in resolved:
        if not isinstance(item, dict) or not all(isinstance(item.get(key), str) for key in ("from", "to", "basis")):
            return None
        if item["basis"].strip():
            kept.append({key: item[key] for key in ("from", "to", "basis")})
        else:
            moved.append(item["from"])
    return {"refined": refined.strip(), "resolved": kept, "unresolved": [*unresolved, *moved]}


def refine_request(
    request: str, context: str, model: str, *, refiner: Any, now: datetime | None,
) -> dict[str, Any]:
    """One tool-less refine call. Returns the record for the plan file: status
    ok with the fields, or the failure status alone, so the caller can go on
    with the raw request."""
    started = time.monotonic()
    record: dict[str, Any] = {"mode": "separate"}
    try:
        proc = refiner(refine_prompt(request, context, now), model, REFINE_TIMEOUT_SEC)
        try:
            result = json.loads(proc.stdout or "")
        except ValueError:
            result = None
        if isinstance(result, list):
            result = next((e for e in reversed(result) if isinstance(e, dict) and e.get("type") == "result"), None)
        if not isinstance(result, dict):
            record["status"] = "bad_output"
        elif result.get("is_error"):
            record["status"] = "error:cli"
        else:
            cleaned = _clean_refine(result.get("structured_output"))
            if cleaned is None:
                record["status"] = "bad_output"
            else:
                record.update(cleaned)
                record["status"] = "ok"
    except subprocess.TimeoutExpired:
        record["status"] = "timeout"
    except Exception as exc:  # noqa: BLE001 - the plan file records the failure
        record["status"] = "error:" + type(exc).__name__
    record["elapsed_sec"] = round(time.monotonic() - started, 1)
    return record


def refined_planner_input(request: str, refine: dict[str, Any]) -> str:
    lines = [f"[정제된 요청]\n{refine['refined']}"]
    if refine["resolved"]:
        resolved = "\n".join(f"- {r['from']} → {r['to']} ({r['basis']})" for r in refine["resolved"])
        lines.append("[확정한 해석]\n" + resolved)
    if refine["unresolved"]:
        lines.append(
            "[미확정 : 추정으로 채우면 assumed 에, 쓰기 요청에 꼭 필요하면 missing 과 ask 에 적는다]\n"
            + "\n".join(f"- {item}" for item in refine["unresolved"])
        )
    return "\n\n".join(lines) + f"\n\n[원문 요청]\n{request}"


def result_event(stdout: str) -> dict[str, Any] | None:
    """The result object of a CLI ``--output-format json`` run.

    The CLI emits either a single result object or an array of events ending
    in one (engine/claude.py _result_event has the same rule). 2026-11-16: the
    operator's verbose setting reached the plan call despite
    --setting-sources "", and all 26 recorded plans were error:cli. The
    2.1.325 CLI prints the array by default (N9, 2026-11-19).
    """
    try:
        result = json.loads(stdout)
    except ValueError:
        return None
    if isinstance(result, list):
        result = next(
            (e for e in reversed(result) if isinstance(e, dict) and e.get("type") == "result"),
            None,
        )
    return result if isinstance(result, dict) else None


def parse(stdout: str) -> tuple[str, dict[str, Any] | None]:
    """(status, plan) from the CLI's ``--output-format json`` result."""
    result = result_event(stdout)
    if result is None:
        return "bad_output", None
    if result.get("is_error"):
        return "error:cli", None
    plan = result.get("structured_output")
    if not isinstance(plan, dict) or plan.get("type") not in ("short", "branch", "explore"):
        return "bad_output", None
    branches = plan.get("branches")
    if not isinstance(branches, list):
        return "bad_output", None
    kind, count = plan["type"], len(branches)
    if (kind == "short" and count) or (kind == "branch" and not 2 <= count <= 5) or (
        kind == "explore" and not 1 <= count <= 3
    ):
        plan["count_note"] = f"{kind}:{count}"  # recorded, plan kept as given
    plan["cost_usd"] = result.get("total_cost_usd")
    return "ok", plan


WORKFLOW_FIELDS = ("workflow", "workflow_confidence", "workflow_reason")


def check_workflow(plan: dict[str, Any], candidates: dict[str, str]) -> str:
    """Keep the workflow fields only where they are valid; return a note.

    "" means they are valid, "off" means no candidates were given (fields
    removed), "invalid" means the id or confidence is outside what was offered
    (fields removed, so a wrong value is never recorded as a classification).
    """
    if not candidates:
        for key in WORKFLOW_FIELDS:
            plan.pop(key, None)
        return "off"
    reason = plan.get("workflow_reason")
    if (
        plan.get("workflow") in candidates
        and plan.get("workflow_confidence") in CONFIDENCES
        and isinstance(reason, str)
    ):
        return ""
    for key in WORKFLOW_FIELDS:
        plan.pop(key, None)
    return "invalid"


def _intake_field_valid(key: str, value: Any) -> bool:
    if key in INTAKE_TEXT_FIELDS:
        return isinstance(value, str)
    if key in INTAKE_LIST_FIELDS:
        return isinstance(value, list) and all(isinstance(item, str) for item in value)
    return isinstance(value, list) and all(
        isinstance(item, dict) and isinstance(item.get("what"), str) and isinstance(item.get("how"), str)
        for item in value
    )


def _clean_inputs_v2(value: Any) -> list[dict[str, str]] | None:
    if not isinstance(value, list):
        return None
    kept = []
    for item in value:
        if not isinstance(item, dict) or item.get("kind") not in INPUT_KINDS:
            continue
        name, val, reason = item.get("name"), item.get("value"), item.get("reason", "")
        if isinstance(name, str) and name.strip() and isinstance(val, str) and isinstance(reason, str):
            kept.append({"name": name, "value": val, "kind": item["kind"], "reason": reason})
    return kept[:INPUTS_MAX]


def _clean_output_v2(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or value.get("form") not in OUTPUT_FORMS or not isinstance(value.get("items"), list):
        return None
    items = [i for i in value["items"] if isinstance(i, str) and i.strip()]
    limit = OUTPUT_ITEMS_SHORT_MAX if value["form"] == "한두줄" else OUTPUT_ITEMS_MAX
    return {"form": value["form"], "items": items[:limit]}


def _clean_work(plan: dict[str, Any], dropped: list[str], enabled: bool = True) -> None:
    """Keep ``work`` only as one of WORK_VALUES; an off intake removes it."""
    if "work" not in plan:
        return
    if enabled and str(plan["work"]).strip() in WORK_VALUES:
        plan["work"] = str(plan["work"]).strip()
        return
    plan.pop("work")
    if enabled:
        dropped.append("work")


def check_intake_v2(plan: dict[str, Any]) -> tuple[list[str], list[str]]:
    """v2 counterpart of check_intake: (dropped, fixed). Malformed input
    entries are removed one by one; ``ask`` is forced empty when no input
    has kind ask."""
    dropped: list[str] = []
    fixed: list[str] = []
    for key in INTAKE_V1_ONLY:
        plan.pop(key, None)
    for key in INTAKE_FIELDS_V2:
        if key not in plan:
            continue
        value = plan[key]
        if key == "inputs":
            value = _clean_inputs_v2(value)
        elif key == "output":
            value = _clean_output_v2(value)
        elif not isinstance(value, str):
            value = None
        if value is None:
            plan.pop(key)
            dropped.append(key)
        else:
            plan[key] = value
    _clean_work(plan, dropped)
    has_ask = any(i["kind"] == "ask" for i in plan.get("inputs", []))
    if plan.get("ask") and not has_ask:
        plan["ask"] = ""
        fixed.append("ask")
    return dropped, fixed


def check_intake(plan: dict[str, Any], enabled: bool, version: int = 1) -> list[str]:
    """Keep the intake fields only where they are well formed; return the
    names dropped. Off removes all of them. Nothing here touches the rest of
    the plan, so a bad intake field never costs the plan injection."""
    if enabled and version == 2:
        return check_intake_v2(plan)[0]
    dropped: list[str] = []
    for key in INTAKE_FIELDS:
        if key not in plan:
            continue
        if not enabled or not _intake_field_valid(key, plan[key]):
            plan.pop(key)
            if enabled:
                dropped.append(key)
    _clean_work(plan, dropped, enabled)
    return dropped


def check_refine(plan: dict[str, Any], mode: str) -> dict[str, Any] | None:
    """Move combined-mode refine fields out of the plan into the record under
    `refine`. Malformed fields are dropped and the status says so; the rest of
    the plan is untouched."""
    values = {key: plan.pop(key) for key in REFINE_FIELDS if key in plan}
    if mode != "combined":
        return None
    cleaned = _clean_refine(values)
    if cleaned is None:
        return {"mode": "combined", "status": "bad_output"}
    return {"mode": "combined", "status": "ok", **cleaned}


def run(
    transcript: str, *, caller: Any = _call_planner, timeout: float = TIMEOUT_SEC,
    refiner: Any = _call_refiner, now: datetime | None = None,
) -> dict[str, Any]:
    """Planner process body: call the model and write the plan file."""
    model = os.environ.get(PLAN_MODEL_ENV) or DEFAULT_MODEL
    started = time.monotonic()
    record: dict[str, Any] = {"model": model}
    mode = refine_mode()
    try:
        request = prompt_path(transcript).read_text(encoding="utf-8")
        candidates = workflow_candidates()
        prompt = request
        context = ""
        intake = intake_enabled()
        if (candidates or mode or intake) and context_path(transcript).exists():
            context = context_path(transcript).read_text(encoding="utf-8")
        if (candidates or intake or mode == "combined") and context:
            prompt = f"{request}\n\n[직전 대화 : 참고용이며 요청이 아니다]\n{context}\n"
        if mode == "separate":
            record["refine"] = refine_request(request, context, model, refiner=refiner, now=now)
            if record["refine"]["status"] == "ok":
                prompt = refined_planner_input(prompt, record["refine"])
        proc = caller(prompt, model, timeout)
        status, plan = parse(proc.stdout or "")
        if plan is not None and plan.get("type") == "short" and is_rca(request):
            record["rca_retry"] = True
            proc = caller(prompt + RCA_RETRY_NOTE, model, timeout)
            status, plan = parse(proc.stdout or "")
        if plan is not None:
            combined = check_refine(plan, mode)
            if combined is not None:
                record["refine"] = combined
            note = check_workflow(plan, candidates)
            if note == "invalid":
                record["workflow_error"] = "invalid"
            if intake and intake_schema() == 2:
                dropped, fixed = check_intake_v2(plan)
                if fixed:
                    record["intake_fixed"] = fixed
            else:
                dropped = check_intake(plan, intake)
            if dropped:
                record["intake_dropped"] = dropped
            record.update(plan)
    except subprocess.TimeoutExpired:
        status = "timeout"
    except Exception as exc:  # noqa: BLE001 - the plan file records the failure
        status = "error:" + type(exc).__name__
    record["status"] = status
    record["elapsed_sec"] = round(time.monotonic() - started, 1)
    record["finished_at"] = time.time()
    try:
        _write_json(plan_path(transcript), record)
    finally:
        prompt_path(transcript).unlink(missing_ok=True)
        context_path(transcript).unlink(missing_ok=True)
    return record


def main(argv: list[str]) -> int:
    if len(argv) >= 3 and argv[1] == "--run":
        with contextlib.suppress(Exception):  # detached; nothing to report to
            run(argv[2])
        return 0
    with contextlib.suppress(Exception):  # never fail the prompt
        data = json.load(sys.stdin)
        if start(data) == "sync":
            context = _gate_context(data["transcript_path"])
            if context:
                print(json.dumps({
                    "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context},
                }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
