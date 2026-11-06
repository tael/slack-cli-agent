"""Stop-hook judgments that catch a turn ending without doing the work.

Ports the *common* judgments from mametchi's finish_work_hook.py (read-only
reference at ~/.mametchi/finish_work_hook.py) as an engine built-in, so a bot
built from this repo gets end-of-turn review with zero profile config:

- gave_up: said a lookup failed and never tried it again
- not_opened: said it skipped opening something it could have opened
- absence_unopened: told "it's ready now" and answered "nothing's there"
  without opening a single thing this turn
- offer: ended by offering to do the next step instead of doing it
- handoff: handed a request to someone else without a real permission wall
- denied_unretried: a tool denial that was never retried another way
- advisor_skipped: gave a decision in an advisor channel without calling
  the advisor this turn
- verify: flags fact sentences the turn's own tool results don't support

Domain-specific judgments (mametchi's lastmile enum codes, Snowflake vs.
lastmile freshness, AWS profile fan-out, 5-why causal-chain grading, ...)
are deliberately left out. They don't generalize past one organization's
systems, so they belong in a profile plugin that calls
``registry.register(name, check, guidance)`` on `default_registry` (or
builds its own `JudgmentRegistry`), the same extension shape
engine/environment.py's `EngineEnvironmentPolicyRegistry` already uses.

Every judgment is a plain function over `Turn`/`ToolCall` value objects,
not a transcript parser — building a `Turn` from a real Claude Code
transcript (JSONL rows read from the Stop hook's `transcript_path`) is a
separate, thin step (`turn_from_transcript_rows`) so the judgments
themselves are trivial to unit test without a transcript file on disk.

Wiring note for whoever wires this into engine/claude.py's hook settings
(ownership.md: B4, after this module lands): the Stop hook process should
1. read the hook JSON from stdin (`session_id`, `transcript_path`,
   `last_assistant_message`),
2. build a `Turn` via `turn_from_transcript_rows`,
3. compute `seconds_left` via `time_left(os.environ)` and `block_count`
   from wherever the per-turn block counter is persisted (mametchi keyed
   it by session_id + first row's uuid, under its own state dir — this
   module takes no position on where that file lives, since this repo
   has no state-dir convention of its own yet),
4. call `decide(turn, block_count=..., seconds_left=...)`, and
5. print `decision.to_hook_json()` (nothing when it's `{}`) to stdout.
TURN_DEADLINE/TURN_DEADLINE_MONO and POST_CHANNEL need to reach that
process; engine/environment.py's `ClaudeEnvironmentPolicy` now allowlists
them.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from slack_cli_agent.core.markers import ASK_TAG

#: Absolute path to this file. A real wiring (see module docstring) invokes
#: this module as a standalone script the same way
#: observability/progress_hook.py's HOOK_SCRIPT is invoked for PreToolUse.
HOOK_SCRIPT = Path(__file__).resolve()

#: Claude Code re-injects a blocked Stop hook's own `reason` text as a
#: synthetic user turn prefixed with this string. Without skipping it,
#: every block would look like a fresh human message and reset turn
#: boundaries / block counting on every single block.
STOP_HOOK_FEEDBACK_PREFIX = "Stop hook feedback"

#: A line that is nothing but a `[[TAG]]` marker — never part of the prose
#: a judgment reads.
_TAG_LINE = re.compile(r"^\s*\[\[[^\]]*\]\]\s*$")
#: Quoted spans are usually a reply *naming* a forbidden phrase ("the banned
#: line was X"), not using it — judgments strip these out first.
_QUOTED = re.compile(r'"[^"\n]*"|“[^”\n]*”|\'[^\'\n]*\'')


# --------------------------------------------------------------------------
# Value objects
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolCall:
    """One tool_use this turn, paired with its tool_result text if the
    transcript had one (empty string if the call is still pending)."""

    name: str
    input: Mapping[str, object] = field(default_factory=dict)
    result: str = ""


@dataclass(frozen=True)
class Turn:
    """The observable facts about one turn a judgment needs.

    Fields a given judgment doesn't need are left at their default, the
    same shape guard/base.py's GuardContext uses.
    """

    answer: str
    #: The text the person asked this turn. Only `absence_unopened` reads
    #: it (it needs to know whether *they* said something was ready).
    user_prompt: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    #: False for a transcript-less turn (e.g. a DM notice sent with
    #: --no-session-persistence). Judging a turn with no evidence to read
    #: would block every reply, including ones that actually did the work.
    has_transcript: bool = True
    #: Whether the channel this turn posts to requires an advisor call
    #: before a decision ships. Channel configuration isn't this module's
    #: concern — the caller resolves it and passes the answer in.
    is_advisor_channel: bool = False
    #: Whether this channel requires a code lookup before a doc-based answer
    #: ships (G13). Resolved by the caller from channel config, same split as
    #: `is_advisor_channel` — this module only reads the already-resolved fact.
    is_code_first_channel: bool = False


@dataclass(frozen=True)
class Finding:
    name: str
    detail: str
    guidance: str


# --------------------------------------------------------------------------
# Transcript -> Turn
# --------------------------------------------------------------------------


def _message_text(entry: Mapping[str, object]) -> str:
    message = entry.get("message")
    content = message.get("content") if isinstance(message, Mapping) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            block.get("text", "")
            for block in content
            if isinstance(block, Mapping) and block.get("type") == "text"
        )
    return ""


def rows_since_last_human_input(rows: Sequence[Mapping[str, object]]) -> list[Mapping[str, object]]:
    """Rows from (and including) the last real human text input onward.

    A Stop hook's own previous block feedback is also recorded as a `user`
    row (see `STOP_HOOK_FEEDBACK_PREFIX`) and is skipped so it doesn't look
    like a new turn boundary.
    """
    start = 0
    for i, entry in enumerate(rows):
        message = entry.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        is_text_user_row = entry.get("type") == "user" and (
            isinstance(content, str)
            or (
                isinstance(content, list)
                and any(isinstance(block, Mapping) and block.get("type") == "text" for block in content)
            )
        )
        if not is_text_user_row:
            continue
        if _message_text(entry).lstrip().startswith(STOP_HOOK_FEEDBACK_PREFIX):
            continue
        start = i
    return list(rows[start:])


def _last_assistant_text(rows: Sequence[Mapping[str, object]]) -> str:
    for entry in reversed(rows):
        message = entry.get("message")
        if not isinstance(message, Mapping) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in reversed(content):
            if isinstance(block, Mapping) and block.get("type") == "text" and (block.get("text") or "").strip():
                return str(block["text"])
    return ""


def _tool_calls_in(rows: Sequence[Mapping[str, object]]) -> tuple[ToolCall, ...]:
    calls: list[ToolCall] = []
    index_by_id: dict[object, int] = {}
    for entry in rows:
        message = entry.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, Mapping):
                continue
            if block.get("type") == "tool_use":
                calls.append(ToolCall(name=str(block.get("name") or ""), input=block.get("input") or {}))
                index_by_id[block.get("id")] = len(calls) - 1
            elif block.get("type") == "tool_result":
                idx = index_by_id.get(block.get("tool_use_id"))
                if idx is None:
                    continue
                body = block.get("content")
                if isinstance(body, list):
                    body = " ".join(part.get("text", "") for part in body if isinstance(part, Mapping))
                calls[idx] = replace(calls[idx], result=str(body or ""))
    return tuple(calls)


def turn_from_transcript_rows(
    rows: Sequence[Mapping[str, object]],
    *,
    last_assistant_message: str | None = None,
    user_prompt: str = "",
    is_advisor_channel: bool = False,
    is_code_first_channel: bool = False,
) -> Turn:
    """Builds a `Turn` from raw Claude Code transcript JSONL rows.

    `last_assistant_message` takes priority when the hook payload already
    carries one (Claude Code's Stop event provides it); otherwise this
    falls back to scanning the transcript itself, same as mametchi's hook
    does when that field is absent.
    """
    this_turn = rows_since_last_human_input(rows)
    answer = last_assistant_message if last_assistant_message is not None else _last_assistant_text(this_turn)
    return Turn(
        answer=answer or "",
        user_prompt=user_prompt,
        tool_calls=_tool_calls_in(this_turn),
        has_transcript=bool(rows),
        is_advisor_channel=is_advisor_channel,
        is_code_first_channel=is_code_first_channel,
    )


def _input_blob(input_mapping: Mapping[str, object]) -> str:
    try:
        return json.dumps(input_mapping, ensure_ascii=False)
    except TypeError:
        return str(input_mapping)


# --------------------------------------------------------------------------
# offer — ended by offering the next step instead of doing it
# --------------------------------------------------------------------------

_OFFER_RE = re.compile(
    r"(원하시면|필요하시면|필요하면|말씀 주시면|말씀해 주시면)[^\n]*?"
    r"(볼게요|드릴게요|할게요|하겠습니다|드리겠습니다|보겠습니다|이어서|확인해|조사해|파볼|말씀해\s*주세요|말씀\s*주세요)"
    r"|(더|이어서|추가로|바로)[^\n]{0,20}?(확인|조사|살펴|파|찾아|조회)\S*\s*(볼까요|드릴까요|할까요)"
    r"|(돌려|집계해|확인해|조회해|찾아|살펴|분석해|대조해|재\s?볼|모으면|모아\s?보면)"
    r"[^\n]{0,6}?(볼\s?수\s?있어요|볼\s?수\s?있습니다|잴\s?수도\s?있어요|잴\s?수\s?있어요)"
)


def offer(turn: Turn) -> str | None:
    """The offending line, if the answer's last two lines hand the next,
    reversible step to a human instead of taking it. `[[ASK]]` means this
    is a deliberate approval wait, not an offer, so it's exempt."""
    answer = turn.answer
    if not answer or ASK_TAG in answer:
        return None
    lines = [line for line in answer.splitlines() if line.strip() and not _TAG_LINE.match(line)]
    for line in lines[-2:]:
        if _OFFER_RE.search(_QUOTED.sub("", line)):
            return line.strip()
    return None


# --------------------------------------------------------------------------
# handoff — handed off to a person without a real permission wall
# --------------------------------------------------------------------------

_HANDOFF_WALL_RE = re.compile(r"권한\s?밖|이\s?턴[^.\n]{0,12}(?:할\s?수\s?없|안\s?돼|불가)")
_HANDOFF_ASK_RE = re.compile(
    r"(?:요청해\s?주세요|요청해\s?주시|문의해\s?주세요|확인해\s?주세요|확인을\s?요청해\s?주|"
    r"부탁드려요|부탁드립니다|직접\s?돌려\s?주|다시\s?불러\s?주)"
)
#: Claude Code's own tool-permission denial strings, plus the Korean
#: equivalents a bot might render them as. Not AWS- or any-vendor-specific.
_DENIED_RE = re.compile(r"has been denied|Permission to use|권한이 거부|permission denied", re.IGNORECASE)
_HANDOFF_WINDOW = 120


def handoff(turn: Turn) -> str | None:
    """The sentence, if the answer hands a request to a person by claiming
    it's out of bounds, when nothing this turn actually hit a permission
    wall. A real denial in any tool result means the block was genuine, so
    that case passes."""
    answer = turn.answer
    if not answer:
        return None
    if any(_DENIED_RE.search(call.result) for call in turn.tool_calls):
        return None
    lines = [line for line in answer.splitlines() if line.strip() and not _TAG_LINE.match(line)]
    text = _QUOTED.sub("", " ".join(line.strip() for line in lines))
    for match in _HANDOFF_WALL_RE.finditer(text):
        window = text[max(0, match.start() - _HANDOFF_WINDOW) : match.end() + _HANDOFF_WINDOW]
        if _HANDOFF_ASK_RE.search(window):
            return window.strip()[:140]
    return None


# --------------------------------------------------------------------------
# gave_up — said a lookup failed and never tried it again
# --------------------------------------------------------------------------

_GAVE_UP_RE = re.compile(r"(조회|확인|열람)(하지 못|할 수 없었)[^\n]{0,20}")


def gave_up(turn: Turn) -> str | None:
    """The offending phrase, if the answer admits a lookup failed and the
    same tool wasn't called again this turn to try a different way in."""
    answer = turn.answer
    if not answer:
        return None
    match = _GAVE_UP_RE.search(_QUOTED.sub("", answer))
    if not match:
        return None
    counts: dict[str, int] = {}
    for call in turn.tool_calls:
        counts[call.name] = counts.get(call.name, 0) + 1
    if any(n >= 2 for n in counts.values()):
        return None
    return match.group(0)


# --------------------------------------------------------------------------
# stopped_unfinished — ended a do-it task with "couldn't / no means"
# (2026-11-04 TOP40 구독 해지 : "구독 삭제는 아직 실행하지 못했습니다. Tableau에
# 로그인할 수단이 없어서예요" 로 끝났다. 실제로는 SSO 로그인이 됐고 막힘은 권한이었다)
# --------------------------------------------------------------------------

_STOPPED_RE = re.compile(
    r"(실행|처리|삭제|해지|진행|반영|적용|전환)(하지 못|할 수 없|을 못|를 못|못\s?했)[^\n]{0,20}"
    r"|(수단|방법|경로|권한)(이|은)\s?없[^\n]{0,20}"
)


def stopped_unfinished(turn: Turn) -> str | None:
    """The offending phrase, if the answer ends a do-it task by saying the
    action couldn't be done or there's no means to do it. An answer that
    explicitly waits on a human decision (`[[ASK]]`) is left alone."""
    answer = turn.answer
    if not answer or _ASK_LINE_RE.search(answer):
        return None
    match = _STOPPED_RE.search(_QUOTED.sub("", answer))
    return match.group(0) if match else None


# --------------------------------------------------------------------------
# not_opened — admitted skipping an open it could have done
# --------------------------------------------------------------------------

_NOT_OPENED_RE = re.compile(r"(열어\s?보지|읽어\s?보지|열람하지|열지|읽지)\s*(않|못)[^\n]{0,30}")
_NOT_OPENED_TARGET_RE = re.compile(r"(판단|대조|확인|본문|내용|완료조건|있는지|없는지|안내)")
#: "the server didn't read the config" has a system as subject, not the
#: model skipping a step — exempt so a legitimate system-failure
#: description isn't misread as a confession.
_NOT_OPENED_SYSTEM_RE = re.compile(r"(코드|스크립트|서비스|DAG|앱|서버|훅|봇)(가|는|이|은)[^.\n]{0,20}(읽|열)")


def not_opened(turn: Turn) -> str | None:
    """The offending phrase, if the answer says it skipped opening
    something (a thread, a doc, an attachment) that bears on judgment,
    content, or a completion check."""
    answer = turn.answer
    if not answer:
        return None
    for sentence in re.split(r"[.\n]", _QUOTED.sub("", answer)):
        match = _NOT_OPENED_RE.search(sentence)
        if not match or not _NOT_OPENED_TARGET_RE.search(sentence) or _NOT_OPENED_SYSTEM_RE.search(sentence):
            continue
        return match.group(0)
    return None


# --------------------------------------------------------------------------
# absence_unopened — told "it's ready", answered "nothing's there", opened nothing
# --------------------------------------------------------------------------

_READY_RE = re.compile(
    r"(수집\s?되었|수집\s?됐|수집했|수집한|올라왔|올려\s?뒀|올렸으니|업로드\s?됐|업로드\s?되었|받아\s?뒀|받았으니|"
    r"생겼으니|준비\s?됐|준비\s?되었|이제[^\n]{0,20}(있으니|생겼|됐으니|되었으니|있어))"
)
_ABSENCE_RE = re.compile(
    r"(수집되지 않|수집이 안|기록이 없|로그가 없|로그는 없|흔적이 (없|남지 않)|측정할 수 없|잴 수 없|"
    r"존재하지 않|남아 있지 않|남지 않|찾을 수 없|이벤트가 없|이벤트는 없|0건이라)"
)
_OPEN_ARTIFACT_RE = re.compile(
    r"(aws\s+s3|sqlite3|gsutil|s3\s+cp|s3\s+ls|s3\s+sync|curl\s[^\n]*-o\s|\.sqlite|\.csv|\.parquet|\.log\b|\.jsonl\b)"
)
#: Generic file/artifact-opening tools. A profile with its own reader tool
#: (e.g. an MCP file-read server) widens this via its own registered rule
#: rather than editing this tuple.
_ARTIFACT_OPENING_TOOLS = ("Read", "NotebookRead")


def absence_unopened(turn: Turn) -> str | None:
    """The offending sentence, if the person said some artifact just
    became available this turn and the answer concludes it's absent
    without the turn opening a single file or running an artifact-reading
    command to check."""
    answer, ask = turn.answer, turn.user_prompt
    if not answer or not ask or ask.lstrip().startswith(STOP_HOOK_FEEDBACK_PREFIX) or not _READY_RE.search(ask):
        return None
    for sentence in re.split(r"[.\n]", _QUOTED.sub("", answer)):
        match = _ABSENCE_RE.search(sentence)
        if not match:
            continue
        for call in turn.tool_calls:
            if call.name in _ARTIFACT_OPENING_TOOLS or _OPEN_ARTIFACT_RE.search(_input_blob(call.input)):
                return None
        return sentence.strip()[:120]
    return None


# --------------------------------------------------------------------------
# advisor_skipped — gave a decision in an advisor channel, skipped the advisor
# --------------------------------------------------------------------------

_DECISION_MARK_RE = re.compile(
    r"^\s*#{1,4}\s*(제안|추천|옵션|방안|비교|선택지)|^\s*\|\s*(번호|옵션|방안)\s*\|", re.MULTILINE
)
_ASK_LINE_RE = re.compile(r"^[ \t]*\[\[ASK\]\][ \t]*$", re.MULTILINE)


def advisor_skipped(turn: Turn, *, advisor_model_hint: str = "fable") -> bool:
    """True when this channel requires an advisor call before a decision
    ships, the answer reads as asking for / stating a decision, and no
    `Agent`/`Task` call this turn used the advisor model."""
    if not turn.answer or not turn.is_advisor_channel:
        return False
    if not _ASK_LINE_RE.search(turn.answer) and not _DECISION_MARK_RE.search(turn.answer):
        return False
    return not any(
        call.name in ("Agent", "Task") and advisor_model_hint in str(call.input.get("model") or "")
        for call in turn.tool_calls
    )


# --------------------------------------------------------------------------
# code_first — answered from docs alone where the channel wants code first
# --------------------------------------------------------------------------

#: Default signals for "the answer leans on a document". A doc-source MCP
#: tool (notion.*) or a doc link in the answer. These are organization-neutral
#: defaults — a profile plugin that uses a different doc store can register a
#: replacement rule rather than this module naming its tool, the same way
#: advisor_skipped's model hint is a parameter, not a hardcoded org model.
_DOC_TOOL_RE = re.compile(r"notion", re.IGNORECASE)
_DOC_LINK_RE = re.compile(r"notion\.(?:so|com)/", re.IGNORECASE)
#: Default signal for "the answer cites the code host". GitHub is the common
#: default; an org on a different host registers its own rule.
_CODE_HOST_LINK_RE = re.compile(r"github\.com/", re.IGNORECASE)
#: Default signals for "a code lookup happened this turn": a git/gh command
#: (bare or via a read wrapper like gh_read.sh), or a path under a code
#: checkout. The bare `git`/`gh`/`/Projects/` forms match any org; a wrapper
#: name an org adds is caught by the `_read.sh` tail.
_CODE_BASH_RE = re.compile(r"(?:^|[\s;&|(])(?:gh|git)\s|/Projects/|_read\.sh\b")
_CODE_PATH_RE = re.compile(r"/Projects/")
#: Doc words that, when they open the first paragraph, mark a doc-centered
#: answer even if a code-host link appears first by position
#: (mametchi code_first_hook.py DOC_WORDS, the 2026-10-23 reproduction).
_DOC_WORD_RE = re.compile(r"트래커|노션|notion|PRD|문서", re.IGNORECASE)


def _doc_basis(turn: Turn) -> bool:
    if any(_DOC_TOOL_RE.search(call.name) for call in turn.tool_calls):
        return True
    return bool(_DOC_LINK_RE.search(turn.answer))


def _code_looked_up(turn: Turn) -> bool:
    for call in turn.tool_calls:
        if call.name == "Bash" and _CODE_BASH_RE.search(str(call.input.get("command") or "")):
            return True
        if call.name in ("Read", "Grep", "Glob") and _CODE_PATH_RE.search(_input_blob(call.input)):
            return True
    return False


def code_first_doc_only(turn: Turn) -> str | None:
    """What's missing, if a code-first channel's answer leans on documents
    without the code lookup / code-host link / code-led ordering the channel
    requires — else None.

    Ports mametchi's code_first_hook.py judgment (doc / code / github_link /
    code_lead) as an organization-neutral Stop-hook rule: a doc-based answer
    (a doc tool or doc link) must also show this turn looked at the code and
    cite the code host, with the code leading over the doc rather than the
    doc framing the answer. Only fires in a channel the caller resolved as
    code-first; a bot with none configured never trips it.
    """
    if not turn.answer or not turn.is_code_first_channel:
        return None
    if not _doc_basis(turn):
        return None
    answer = turn.answer
    host_link = _CODE_HOST_LINK_RE.search(answer)
    doc_link = _DOC_LINK_RE.search(answer)
    # Code leads only if the code-host link comes before the doc link (or there
    # is no doc link), and the first paragraph doesn't open on a doc word.
    code_lead = host_link is not None and (doc_link is None or host_link.start() < doc_link.start())
    if _DOC_WORD_RE.search(answer.strip().split("\n\n", 1)[0]):
        code_lead = False
    if _code_looked_up(turn) and host_link and code_lead:
        return None
    missing: list[str] = []
    if not _code_looked_up(turn):
        missing.append("코드 조회 없음")
    if not host_link:
        missing.append("소스 저장소 링크 없음")
    elif not code_lead:
        missing.append("문서 중심 서술")
    return " / ".join(missing)


# --------------------------------------------------------------------------
# denied_unretried — a tool denial that was never retried another way
# --------------------------------------------------------------------------

_UNCONFIRMED_RE = re.compile(r"(확인하지 못|확인되지 않|확인 못|조회하지 못|막혀 있|조회가 막|권한이 없)")
#: 6+ char dotted/dashed/underscored identifiers — bucket names, resource
#: ARNs, dag ids, anything specific enough that seeing it again means the
#: same target was actually retried, not just the same tool used elsewhere.
_IDENT_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[_.\-][A-Za-z0-9]+)+")


def _call_targets(call: ToolCall) -> set[str]:
    return {token for token in _IDENT_RE.findall(_input_blob(call.input)) if len(token) >= 6}


def denied_unretried(turn: Turn) -> str | None:
    """One target identifier, if the answer carries an unconfirmed-style
    statement and a tool call naming that target was denied this turn
    with no later call retrying the same target a different way."""
    if not turn.answer or not _UNCONFIRMED_RE.search(_QUOTED.sub("", turn.answer)):
        return None
    calls = turn.tool_calls
    for i, call in enumerate(calls):
        if not _DENIED_RE.search(call.result):
            continue
        targets = _call_targets(call)
        if not targets:
            continue
        retried = any(_call_targets(other) & targets for other in calls[i + 1 :])
        if not retried:
            return next(iter(sorted(targets)))
    return None


# --------------------------------------------------------------------------
# verify — fact sentences this turn's own tool results don't support
# --------------------------------------------------------------------------

_CLAIM_MARK_RE = re.compile(
    r"원인|원천|때문|읽어|읽는|참조|수집|계산|적재|계보|합계|비율|건수|\d{2,}|[A-Za-z][A-Za-z0-9]*(?:[_.][A-Za-z0-9]+)+"
)
#: The trailing group must end on a digit, not a comma — "48291," in a
#: sentence like "건수는 48291, 원인은..." must not swallow the comma into
#: the match, or it stops matching the same number's bare form elsewhere.
_NUM_RE = re.compile(r"(?<![\w.])\d(?:[\d,]*\d)?(?:\.\d+)?")
#: Below this length, needs_verification() doesn't bother — a short answer
#: has no room for an unsupported claim worth flagging, and running the
#: check on every reply costs a model call and a rewrite round trip.
#: mametchi raised this 200 -> 400 after the median reply time tripled from
#: block-then-rewrite loops on short answers.
VERIFY_MIN_LEN = 400
#: The verify judgment blocks a turn at most this many times. 2026-11-06:
#: with 1, the rewrite after the first block was never checked, and
#: guchipachi's rewrite swapped the flagged claims for new unqueried
#: guesses ("워크플로나 예약 발송") that went out unverified. 2 checks the
#: rewrite once, matching mametchi's finish_work_hook.
VERIFY_MAX_BLOCKS = 2
#: Seconds the nested model call may take.
VERIFY_TIMEOUT = 90
#: Cap on tool-result text handed to the model.
VERIFY_EVIDENCE_CHARS = 120_000
#: Per-tool-result cap, so one huge dump can't crowd out the rest.
VERIFY_CHUNK_CHARS = 12_000
#: Cap on the user prompt included as evidence.
VERIFY_PROMPT_CHARS = 8_000
#: Default model for the nested check (settings: SCA_FINISH_VERIFY_MODEL).
VERIFY_DEFAULT_MODEL = "sonnet"
#: Set in the nested claude's env so a verify call never verifies itself.
VERIFY_NESTED_ENV = "SCA_VERIFY_NESTED"
_ANSWER_IDENT_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[_.][A-Za-z0-9]+)+")


def needs_verification(answer: str) -> bool:
    if "[[침묵]]" in (answer or ""):
        return False
    body = "\n".join(line for line in (answer or "").splitlines() if not _TAG_LINE.match(line))
    return len(body.strip()) >= VERIFY_MIN_LEN and bool(_CLAIM_MARK_RE.search(body))


#: `answer, evidence text -> unsupported sentences`. The engine's Stop hook
#: uses `ModelClaimVerifier` (a nested `claude -p` call, ported from
#: mametchi's unsupported_claims()); `heuristic_claim_verifier` is the
#: subprocess-free default of the pure `unsupported_claims()` function so
#: unit tests never spawn a model.
ClaimVerifier = Callable[[str, str], Sequence[str]]


def heuristic_claim_verifier(answer: str, evidence: str) -> list[str]:
    """Flags sentences carrying a 2+ digit number that doesn't appear
    anywhere in this turn's tool-result text."""
    numbers_seen = set(_NUM_RE.findall(evidence))
    findings: list[str] = []
    for raw_sentence in re.split(r"[.\n]", _QUOTED.sub("", answer or "")):
        sentence = raw_sentence.strip()
        if not sentence or not _CLAIM_MARK_RE.search(sentence):
            continue
        numbers = [n for n in _NUM_RE.findall(sentence) if len(n.replace(",", "")) >= 2]
        if numbers and not any(n in numbers_seen for n in numbers):
            findings.append(sentence[:160])
    return findings[:3]


def _tool_result_text(turn: Turn) -> str:
    return "\n".join(call.result for call in turn.tool_calls if call.result)


def pick_evidence(answer: str, results: Sequence[str], *, limit: int = VERIFY_EVIDENCE_CHARS) -> str:
    """Tool results that fit under `limit`, preferring ones that share
    identifiers with the answer.

    mametchi found that passing only the *last* N chars hid an earlier
    lookup the answer actually rested on, and the checker then flagged a
    grounded sentence. So when everything doesn't fit, results are ranked
    by how many of the answer's identifiers they contain (ties: later
    first) and packed greedily; the kept ones go out in session order.
    """
    chunks = [r[:VERIFY_CHUNK_CHARS] for r in results if r]
    if sum(len(c) for c in chunks) <= limit:
        return "\n".join(chunks)
    keys = {k for k in _ANSWER_IDENT_RE.findall(answer or "") if len(k) >= 4}
    keys |= set(re.findall(r"[A-Za-z_]{5,}", answer or ""))
    ranked = sorted(
        range(len(chunks)),
        key=lambda i: (sum(1 for k in keys if k in chunks[i]), i),
        reverse=True,
    )
    keep: set[int] = set()
    used = 0
    for i in ranked:
        if used + len(chunks[i]) > limit:
            continue
        keep.add(i)
        used += len(chunks[i])
    return "\n".join(chunks[i] for i in sorted(keep))


def _turn_evidence(turn: Turn) -> str:
    results = [call.result for call in turn.tool_calls if call.result]
    prompt = (turn.user_prompt or "")[:VERIFY_PROMPT_CHARS]
    return (prompt + "\n" if prompt else "") + pick_evidence(turn.answer, results)


_VERIFY_PROMPT = (
    "아래 [근거] 는 한 봇이 이번 턴에 실행한 조회 결과와 사용자 입력이다. [답] 은 그 봇이 게시하려는 답이다.\n"
    "[답] 에서 사실 주장 문장만 본다. 사실 주장은 수치, 원천과 계보, 원인, 있음과 없음, 누가 무엇을 했다는 귀속, "
    "실행했다는 보고다.\n"
    "그 중 [근거] 에서 직접 확인되지 않는 문장을 가려낸다. 결론을 좌우하는 문장부터 최대 3개만 낸다.\n"
    "확인하지 못했다는 한계 문장, 의견, 제안, 인사는 사실 주장이 아니므로 제외한다.\n"
    "[근거] 에 비슷한 단어가 있어도 문장이 말하는 관계 자체가 없으면 근거 없음이다. 예를 들어 "
    "A 정의만 조회했는데 답이 B 의 원천이 A 라고 하면 근거 없음이다.\n"
    'JSON 한 개만 답한다. 형식 {"unsupported": [{"sentence": "답의 문장 그대로", "why": "한 문장"}]}. '
    "없으면 빈 배열.\n\n"
)


class ModelClaimVerifier:
    """A `ClaimVerifier` backed by a separate model reading the evidence.

    Runs a nested `claude -p` with no tools, no MCP servers and no setting
    sources (so no hooks recurse). Any failure — missing CLI, timeout, no
    JSON — returns no findings and leaves the reason in `status`: the turn
    is let through and the failure recorded, never silently downgraded to
    the number heuristic. `runner` is injectable for tests.
    """

    def __init__(
        self,
        *,
        model: str = VERIFY_DEFAULT_MODEL,
        timeout: float = VERIFY_TIMEOUT,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        executable: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.model = model
        self.timeout = timeout
        self._runner = runner
        self._executable = executable
        self._env = dict(os.environ if env is None else env)
        self.status = "not_run"

    def __call__(self, answer: str, evidence: str) -> list[str]:
        if self._env.get(VERIFY_NESTED_ENV):
            self.status = "skip_nested"
            return []
        exe = self._executable or shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
        prompt = _VERIFY_PROMPT + "[근거]\n" + evidence + "\n\n[답]\n" + (answer or "")
        try:
            proc = self._runner(
                [exe, "-p", "--model", self.model, "--tools", "", "--strict-mcp-config",
                 "--mcp-config", '{"mcpServers":{}}', "--setting-sources", ""],
                input=prompt, capture_output=True, text=True, timeout=self.timeout,
                env={**self._env, VERIFY_NESTED_ENV: "1"},
            )
        except Exception as exc:  # noqa: BLE001 - a failed check never blocks
            self.status = "error:" + type(exc).__name__
            return []
        out = proc.stdout or ""
        start, end = out.find("{"), out.rfind("}")
        if start < 0 or end <= start:
            self.status = "error:no_json"
            return []
        try:
            items = json.loads(out[start : end + 1]).get("unsupported") or []
        except (ValueError, AttributeError):
            self.status = "error:bad_json"
            return []
        found = [
            f"{(x.get('sentence') or '')[:160]} ({(x.get('why') or '')[:80]})"
            for x in items
            if isinstance(x, dict) and (x.get("sentence") or "").strip()
        ]
        self.status = "ok"
        return found[:3]


def resolve_verify_settings(env: Mapping[str, str]) -> tuple[bool, str]:
    """(model verify enabled, model name) from env. On unless
    SCA_FINISH_VERIFY is 0/false/off — an engine default, not a profile
    opt-in. Model from SCA_FINISH_VERIFY_MODEL, default sonnet."""
    flag = (env.get("SCA_FINISH_VERIFY") or "").strip().lower()
    enabled = flag not in {"0", "false", "off", "no"}
    model = (env.get("SCA_FINISH_VERIFY_MODEL") or "").strip() or VERIFY_DEFAULT_MODEL
    return enabled, model


def unsupported_claims(turn: Turn, *, verifier: ClaimVerifier = heuristic_claim_verifier) -> list[str]:
    """Sentences in the answer that read as fact claims but aren't backed
    by this turn's own tool results (and the user prompt). Empty when the
    answer is too short to bother checking (`needs_verification`)."""
    if not needs_verification(turn.answer):
        return []
    return list(verifier(turn.answer, _turn_evidence(turn)))


def _advisor_skipped_check(turn: Turn) -> str | None:
    return "결정 요청인데 조언자 호출이 없다" if advisor_skipped(turn) else None


def _unsupported_claims_check(turn: Turn) -> str | None:
    claims = unsupported_claims(turn)
    return " | ".join(claims) if claims else None


# --------------------------------------------------------------------------
# Guidance text — what gets fed back to the model in a block
# --------------------------------------------------------------------------


def _offer_guidance(hit: str) -> str:
    return (
        f'답이 "{hit}" 로 끝난다. 조회, 분석, 로그 확인처럼 되돌릴 수 있는 다음 단계를 사람에게 넘기지 않는다. '
        "그 단계를 지금 실행한다. 결과를 반영해 답을 처음부터 다시 쓰고 제안 문장은 뺀다. 삭제, 발송, 운영 반영처럼 "
        "승인이 필요한 일이면 그 사실만 남기고 [[ASK]] 를 붙인다."
    )


def _handoff_guidance(hit: str) -> str:
    return (
        f'답에 "{hit}" 처럼 권한 밖이라며 요청자에게 다른 사람을 부르라고 넘겼다. '
        "이번 턴에 열려 있는 다른 조회 경로를 먼저 실행해 대체 경로로 어디까지 확인했는지 수치로 남긴다. "
        "그래도 남는 일만 담당자를 부르는 한 문장으로 쓴다."
    )


def _gave_up_guidance(hit: str) -> str:
    return (
        f'"{hit}" 로 조회 실패를 답에 썼는데 같은 대상을 다시 조회하지 않았다. '
        "필드나 도구를 바꿔 같은 턴에 다시 조회하고, 확인된 값으로 답을 다시 쓴다."
    )


def _stopped_unfinished_guidance(hit: str) -> str:
    return (
        f'"{hit}" 로 실행 지시를 끝내려 했다. 인증 문제인지 권한 문제인지 실제 호출로 가르고, '
        "봇이 가진 다른 경로(다른 계정·프로파일, 저장소, 스킬, 지식 파일의 절차)를 찾아 같은 턴에 끝낸다. "
        "소유자에게 비밀번호 등록이나 화면 조작을 넘기지 않는다. "
        "사람만 할 수 있는 판단이면 이유와 함께 [[ASK]] 로 묻는다."
    )


def _not_opened_guidance(hit: str) -> str:
    return (
        f'"{hit}" 로 열람을 생략했다고 답에 썼다. 열 수 있는 문서, 스레드, 첨부는 같은 턴에 열어 본문으로 판정한다. '
        "열람을 생략했다는 문장은 답에 남기지 않는다."
    )


def _absence_unopened_guidance(hit: str) -> str:
    return (
        f'"{hit}" 로 부재 결론을 냈는데, 사람이 새로 준비됐다고 한 산출물을 이번 턴에 하나도 열지 않았다. '
        "그 위치를 목록 조회해 전부 연 뒤 답을 다시 쓴다."
    )


def _denied_unretried_guidance(hit: str) -> str:
    return (
        f'"{hit}" 조회가 거부된 뒤 다른 방식으로 다시 부르지 않았다. 같은 대상을 다른 경로로 같은 턴에 다시 조회한다.'
    )


def _advisor_skipped_guidance(_hit: str) -> str:
    return (
        "결정을 구하는 답인데 이번 턴에 조언자를 부르지 않았다. 조언자를 부른 뒤 그 결과를 반영해 답을 다시 쓴다."
    )


def _code_first_guidance(hit: str) -> str:
    return (
        "이 채널은 코드 우선 채널이다. 문서(노션 등)를 근거로 답하기 전에 현재 적용된 코드를 직접 조회해 "
        f"확인한다. 부족한 점 : {hit}. 답은 코드가 1차 근거이고 문서는 대조용이다. 확인한 코드 파일과 "
        "줄을 소스 저장소 링크로 달고, 첫 문단은 코드로 확인한 사실로 연다. 문서와 코드가 다르면 그 차이를 "
        "함께 적는다."
    )


def _verify_guidance(hit: str) -> str:
    return (
        f"다음 문장은 이번 턴 조회 결과에 근거가 없다 : {hit}. "
        "같은 턴에 그 문장이 말하는 대상을 원본 메시지, 코드, 데이터, 로그로 직접 조회해 확정하고 답을 다시 쓴다. "
        "열려 있는 조회 도구로 확정할 수 있는 문장은 먼저 조회한다. "
        "주장을 줄이거나 다른 추측으로 바꾸는 것으로 조회를 대신하지 않는다. "
        "다시 쓴 답도 같은 검수를 다시 받는다. 같은 턴에 조회해도 확정되지 않는 문장만 답에서 뺀다. "
        "이 차단으로 막힌 초안은 게시되지 않았다. 다시 쓴 답에서 그 초안을 앞선 답으로 가리키거나 정정하지 않는다."
    )


# --------------------------------------------------------------------------
# Registry — the extension point profile plugins hook domain rules into
# --------------------------------------------------------------------------

#: `Turn -> detail string or None`. `None` means this rule found nothing.
JudgmentCheck = Callable[[Turn], str | None]
#: `detail string -> guidance text shown to the model`.
JudgmentGuidance = Callable[[str], str]


@dataclass(frozen=True)
class _JudgmentRule:
    name: str
    check: JudgmentCheck
    guidance: JudgmentGuidance


class JudgmentRegistry:
    """Open set of Stop-hook judgments, keyed by name.

    Same registration shape as `engine.environment.EngineEnvironmentPolicyRegistry`:
    a profile plugin adds a domain rule (mametchi's lastmile enum lookup,
    Snowflake-vs-lastmile freshness, ...) with `registry.register(...)`
    rather than this module growing an organization's regexes.
    """

    def __init__(self) -> None:
        self._rules: dict[str, _JudgmentRule] = {}

    def register(self, name: str, check: JudgmentCheck, guidance: JudgmentGuidance) -> None:
        """Registers (or overwrites) a judgment under `name`."""
        self._rules[name] = _JudgmentRule(name=name, check=check, guidance=guidance)

    def unregister(self, name: str) -> None:
        """No-op if `name` isn't registered. Tests that mutate a shared
        registry must call this in a `finally` block so one test's
        registration can't leak into the next."""
        self._rules.pop(name, None)

    def names(self) -> list[str]:
        return sorted(self._rules)

    def evaluate(self, turn: Turn, *, skip: Sequence[str] = ()) -> list[Finding]:
        findings = []
        for rule in self._rules.values():
            if rule.name in skip:
                continue
            hit = rule.check(turn)
            if hit:
                findings.append(Finding(name=rule.name, detail=hit, guidance=rule.guidance(hit)))
        return findings


def _build_common_registry() -> JudgmentRegistry:
    common = JudgmentRegistry()
    common.register("gave_up", gave_up, _gave_up_guidance)
    common.register("not_opened", not_opened, _not_opened_guidance)
    common.register("stopped_unfinished", stopped_unfinished, _stopped_unfinished_guidance)
    common.register("absence_unopened", absence_unopened, _absence_unopened_guidance)
    common.register("offer", offer, _offer_guidance)
    common.register("handoff", handoff, _handoff_guidance)
    common.register("denied_unretried", denied_unretried, _denied_unretried_guidance)
    common.register("advisor_skipped", _advisor_skipped_check, _advisor_skipped_guidance)
    common.register("code_first", code_first_doc_only, _code_first_guidance)
    common.register("verify", _unsupported_claims_check, _verify_guidance)
    return common


#: This module's default registry — the seven common judgments, built in.
#: A profile plugin extends it via `default_registry.register(name, check,
#: guidance)` instead of editing this module, the same pattern
#: engine/environment.py's module-level `registry` singleton uses.
default_registry = _build_common_registry()


# --------------------------------------------------------------------------
# Decision
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class StopDecision:
    action: str  # "block" | "allow" | "release"
    findings: tuple[Finding, ...] = ()

    def to_hook_json(self) -> dict[str, str]:
        """Claude Code's Stop hook protocol: `{"decision": "block", "reason":
        ...}` blocks the stop; anything else is silence — printing `{}` (or
        nothing) lets the turn end."""
        if self.action != "block":
            return {}
        reason = " / ".join(f.guidance for f in self.findings)
        reason += ". 답 끝의 표식과 WATCH 판정 줄은 그대로 둔다."
        return {"decision": "block", "reason": reason}


def time_left(
    env: Mapping[str, str],
    *,
    now_monotonic: float | None = None,
    now_wall: float | None = None,
) -> float | None:
    """Seconds left in the turn, from whichever deadline env var the
    engine passed down. Reads the caller-supplied `env` mapping, not
    `os.environ` directly — same reason engine/environment.py's policies
    read `source_env` instead: keeps this a pure, clock-independent
    function a test can call directly."""
    try:
        mono_deadline = float(env.get("TURN_DEADLINE_MONO") or 0)
        wall_deadline = float(env.get("TURN_DEADLINE") or 0)
    except ValueError:
        return None
    if mono_deadline:
        return mono_deadline - (time.monotonic() if now_monotonic is None else now_monotonic)
    if wall_deadline:
        return wall_deadline - (time.time() if now_wall is None else now_wall)
    return None


def resolve_advisor_channel(env: Mapping[str, str]) -> bool:
    """Whether the channel this turn posts to is one that requires an
    advisor call before a decision ships, from env the engine stamped (G16).

    The deployment puts the current channel in `POST_CHANNEL` and the set of
    advisor channels in `ADVISOR_CHANNELS` (comma- or whitespace-separated
    channel IDs). *Which* channels those are is organization config, so this
    module only reads the resolved names — the same split of concerns
    `time_left` keeps with the deadline the engine stamped. False when
    either var is empty, so a deployment that configures neither simply
    never trips the `advisor_skipped` judgment. Reads the caller-supplied
    `env` mapping, not `os.environ`, so a test can call it directly."""
    current = (env.get("POST_CHANNEL") or "").strip()
    if not current:
        return False
    configured = {c for c in re.split(r"[,\s]+", env.get("ADVISOR_CHANNELS") or "") if c}
    return current in configured


def resolve_code_first_channel(env: Mapping[str, str]) -> bool:
    """Whether the channel this turn posts to requires a code lookup before a
    doc-based answer ships, from env the engine stamped (G13).

    The deployment puts the current channel in `POST_CHANNEL` and the set of
    code-first channels in `CODE_FIRST_CHANNELS` (comma- or whitespace-separated
    channel IDs). *Which* channels those are is organization config, so this
    module only reads the resolved names — the same split `resolve_advisor_channel`
    keeps. False when either var is empty, so a deployment that configures
    neither never trips the `code_first` judgment. Reads the caller-supplied
    `env` mapping, not `os.environ`, so a test can call it directly."""
    current = (env.get("POST_CHANNEL") or "").strip()
    if not current:
        return False
    configured = {c for c in re.split(r"[,\s]+", env.get("CODE_FIRST_CHANNELS") or "") if c}
    return current in configured


def decide(
    turn: Turn,
    *,
    registry: JudgmentRegistry | None = None,
    block_count: int = 0,
    max_blocks: int = 2,
    seconds_left: float | None = None,
    release_sec: int = 240,
    claim_verifier: ClaimVerifier | None = None,
) -> StopDecision:
    """The Stop-hook decision for one turn.

    `block_count` is how many times this same turn has already been
    blocked, and `seconds_left` how much turn budget remains (see
    `time_left`) — both plain arguments rather than this function reading
    a counter file or `os.environ` itself, so it stays a pure function a
    test can call directly. Persisting `block_count` across hook
    invocations (each Stop event is a fresh process) is the wiring
    layer's job; see this module's docstring.

    A transcript-less turn (`has_transcript=False`) always allows: there's
    no evidence to judge, and judging anyway would block every such turn,
    including ones that did the work.
    """
    if not turn.has_transcript:
        return StopDecision(action="allow")
    active_registry = default_registry if registry is None else registry
    if claim_verifier is None:
        findings = tuple(active_registry.evaluate(turn))
    else:
        findings = tuple(active_registry.evaluate(turn, skip=("verify",)))
        # Model verify (mametchi's order): only when nothing cheaper already
        # flagged the turn, at most VERIFY_MAX_BLOCKS blocks per request, and
        # never so close to the deadline that the rewrite can't land.
        budget_ok = seconds_left is None or seconds_left > release_sec + VERIFY_TIMEOUT
        if not findings and budget_ok and block_count < VERIFY_MAX_BLOCKS:
            claims = unsupported_claims(turn, verifier=claim_verifier)
            if claims:
                hit = " | ".join(claims)
                findings = (Finding(name="verify", detail=hit, guidance=_verify_guidance(hit)),)
    if not findings:
        return StopDecision(action="allow")
    if seconds_left is not None and seconds_left < release_sec:
        return StopDecision(action="release", findings=findings)
    if block_count >= max_blocks:
        return StopDecision(action="release", findings=findings)
    return StopDecision(action="block", findings=findings)
