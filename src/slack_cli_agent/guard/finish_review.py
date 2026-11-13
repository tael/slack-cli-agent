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
import signal
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
    #: The knowledge-file text this turn's system prompt actually carried
    #: (KnowledgeSection's rendered output, handed over by the engine through
    #: KNOWLEDGE_FILE_ENV). Only `verify` reads it: a code table or mapping
    #: the bot answered from is in the prompt, not in any tool result, and
    #: without it the check flagged those sentences as unsupported
    #: (2026-11-10: dispatch_policy 4 = LEGACY_AI, status 4 = 배송완료).
    knowledge: str = ""


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
    r"[^\n]{0,6}?(볼\s?수\s?있어요|볼\s?수\s?있습니다|잴\s?수도\s?있어요|잴\s?수\s?있어요"
    # 2026-11-10 : 명사형 요약체 답("~볼 수 있음")도 같은 제안형 마무리로 본다
    r"|볼\s?수\s?있음|잴\s?수\s?있음)"
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
# named_turn — a turn's role described by a person's name
# --------------------------------------------------------------------------

#: "<이름>님 턴" names the person instead of the role. The owner is profile
#: config, not a fixed person, so turn tiers are written as 오너 턴 / 요청자 턴
#: (2026-11-09 private-taeil: the owner was named in a turn-tier explanation).
_NAMED_TURN_RE = re.compile(r"[가-힣A-Za-z]{2,10}님\s?(?:의\s?)?턴")


def _owner_turn_re(env: Mapping[str, str]) -> re.Pattern[str] | None:
    """"<오너 이름> 턴", "<오너 이름> 본인 턴", "<오너 이름>이 아닌 사람의 턴"
    -- the owner named within a few characters before 턴, with or without 님.
    The name comes from OWNER_NAME (pipeline._engine_env), never hardcoded."""
    name = (env.get("OWNER_NAME") or "").strip()
    if len(name) < 2:
        return None
    return re.compile(re.escape(name) + r"[^.\n,]{0,10}?턴")


def named_turn(turn: Turn, env: Mapping[str, str] | None = None) -> str | None:
    """The first "<이름>님 턴" (or "<오너 이름> 턴") outside quotes, if any."""
    answer = turn.answer
    if not answer:
        return None
    owner_re = _owner_turn_re(os.environ if env is None else env)
    for line in answer.splitlines():
        if _TAG_LINE.match(line):
            continue
        bare = _QUOTED.sub("", line)
        for pattern in (_NAMED_TURN_RE, owner_re):
            hit = pattern.search(bare) if pattern else None
            if hit:
                return hit.group(0)
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
# 2026-11-06: Codex reports a sandbox refusal as ``patch rejected: writing
# is blocked by read-only sandbox; rejected by user approval settings``.
# It contains neither the old ``permission denied`` phrase nor a successful
# tool result, so the refusal used to pass the end-of-turn review.
_DENIED_RE = re.compile(
    r"has been denied|Permission to use|권한이 거부|permission denied|"
    r"writing is blocked by [^\n]*sandbox|rejected by user approval settings",
    re.IGNORECASE,
)
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

#: gave_up blocks a turn at most this many times; see gave_up().
GAVE_UP_MAX_BLOCKS = 1
_GAVE_UP_RE = re.compile(r"(조회|확인|열람)(하지 못|할 수 없었|\s?불가)[^\n]{0,20}")


def gave_up(turn: Turn) -> str | None:
    """The offending phrase, if the answer admits a lookup failed and the
    same tool wasn't called again this turn to try a different way in."""
    answer = turn.answer
    if not answer:
        return None
    match = _GAVE_UP_RE.search(_QUOTED.sub("", answer))
    if not match:
        return None
    # 2026-11-09: the old exemption ("any tool called twice") let every turn
    # with two Bash calls through. guchipachi ran four ssh lookups on the same
    # stale session file and shipped "현재 사용량은 확인하지 못했습니다"; one
    # more lookup 18 seconds later found the live value. Bash is too generic
    # for a call count to mean "tried another way", so the phrase now always
    # flags, and decide() blocks it once only (GAVE_UP_MAX_BLOCKS): the rewrite
    # either retries and drops the phrase, or keeps it and is let through.
    return match.group(0)


# --------------------------------------------------------------------------
# stopped_unfinished — ended a do-it task with "couldn't / no means"
# (2026-11-04 TOP40 구독 해지 : "구독 삭제는 아직 실행하지 못했습니다. Tableau에
# 로그인할 수단이 없어서예요" 로 끝났다. 실제로는 SSO 로그인이 됐고 막힘은 권한이었다)
# --------------------------------------------------------------------------

# 2026-11-06: "마메치 실제 경로 시험은 끝내지 못했어요" 가 위 동사 목록에 없어
# 통과했다. 시험, 검증, 점검, 완료와 "끝내지 못", "마치지 못" 문형을 더한다.
_STOPPED_RE = re.compile(
    r"(실행|처리|삭제|해지|진행|반영|적용|전환|시험|테스트|검증|점검|완료)"
    r"(하지 못|할 수 없|을 못|를 못|못\s?했)[^\n]{0,20}"
    r"|(시험|테스트|검증|점검|작업|확인)[^\n.]{0,12}(끝내지|마치지|끝나지)\s?(못|않)[^\n]{0,20}"
    r"|(수단|방법|경로|권한)(이|은)\s?없[^\n]{0,20}|(수단|방법|경로|권한)\s?없음[^\n]{0,20}"
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
# unable_unlooked — claimed "can't see / can't tell" with zero tool calls
# (2026-11-10 sandbox-rnd 19:01 : "새 이미지는 직접 볼 수 없어 평가는 못 함" 을
# 도구 호출 0회로 냈다. users.info → image_512 → curl → Read 로 열 수 있었다.
# 2026-11-11 재생 시험에서 기존 검사 8종 모두 None 이었다)
# --------------------------------------------------------------------------

#: An inability claim. The judgment's weight is on "zero tool calls this
#: turn", not on this list: any turn that called a tool at all passes, so the
#: list only has to catch how an inability is phrased, not every variant of
#: a wrong answer.
_UNABLE_RE = re.compile(
    r"(볼|알|열|읽을|확인할|조회할|판단할|평가할|파악할|접근할|열람할|비교할|확인될)\s?수(가|는|도)?\s?없"
    r"|(보|알|열|읽|확인하|조회하|열람하|파악하|평가하|판단하)지\s?못"
    r"|(확인|조회|열람|평가|판단|파악)\s?불가"
    r"|(평가|판단|확인|조회|열람|파악|비교|검토|분석)(는|은|도)?\s?못\s?(함|했|해|합니다|해요)"
    r"|못\s?(봄|봤|봐서|봅니다|봐요)"
)


def unable_unlooked(turn: Turn) -> str | None:
    """The inability phrase, if the answer claims it can't see, open, or
    tell something and this turn called no tool at all.

    One tool call of any kind passes: whether that lookup was the right one
    is other judgments' concern. A silent reply is exempt."""
    answer = turn.answer
    if not answer or turn.tool_calls or "[[침묵]]" in answer:
        return None
    for line in answer.splitlines():
        if _TAG_LINE.match(line):
            continue
        match = _UNABLE_RE.search(_QUOTED.sub("", line))
        if match:
            return match.group(0)
    return None


# --------------------------------------------------------------------------
# not_opened — admitted skipping an open it could have done
# --------------------------------------------------------------------------

_NOT_OPENED_RE = re.compile(r"(열어\s?보지|읽어\s?보지|열람하지|열지|읽지)\s*(않|못)[^\n]{0,30}")
_NOT_OPENED_TARGET_RE = re.compile(r"(판단|대조|확인|본문|내용|완료조건|있는지|없는지|안내)")
#: 2026-11-06 : "원본 메시지를 열어 보지 않아 이름까지는 모릅니다" 는 위 대상 단어가 없어
#: 통과했고, 구치파치가 원본을 열지 않은 채 "워크플로나 예약 발송" 추정을 게시했다.
#: 원본, 원문, 첨부를 열지 않았다고 과거형으로 자백하거나 그래서 모른다고 쓴 문장도 잡는다.
#: 두 봇 감사 로그 답 5,342건 재생에서 추가로 잡힌 11건이 모두 실제 미열람 자백이었다.
_NOT_OPENED_PAST_RE = re.compile(
    r"(열어\s?보지|읽어\s?보지|열람하지|열지|읽지)\s*(않았(?!다고|다는)|않아서|않아\s|못했(?!다고|다는)|못해|못한|않음|못함)[^\n]{0,30}"
)
_NOT_OPENED_SOURCE_RE = re.compile(r"(모르|모릅|알 수 없|알지 못|원본|원문|첨부|이름까지)")
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
        if match and not _NOT_OPENED_TARGET_RE.search(sentence):
            past = _NOT_OPENED_PAST_RE.search(sentence)
            match = past if past and _NOT_OPENED_SOURCE_RE.search(sentence) else None
        if not match or _NOT_OPENED_SYSTEM_RE.search(sentence):
            continue
        return match.group(0)
    return None


# --------------------------------------------------------------------------
# absence_unopened — told "it's ready", answered "nothing's there", opened nothing
# --------------------------------------------------------------------------

_READY_RE = re.compile(
    r"(수집\s?되었|수집\s?됐|수집했|수집한|올라왔|올려\s?뒀|올렸으니|업로드\s?됐|업로드\s?되었|받아\s?뒀|받았으니|"
    r"생겼으니|준비\s?됐|준비\s?되었|이제[^\n]{0,20}(있으니|생겼|됐으니|되었으니|있어)|수집됨|업로드됨|준비됨|올라옴)"
)
_ABSENCE_RE = re.compile(
    r"(수집되지 않|수집이 안|기록이 없|로그가 없|로그는 없|흔적이 (없|남지 않)|측정할 수 없|잴 수 없|"
    r"존재하지 않|남아 있지 않|남지 않|찾을 수 없|이벤트가 없|이벤트는 없|0건이라|"
    r"(로그|기록|이벤트|흔적)\s?없음|측정\s?불가)"
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


#: A Bash call that ran the claude CLI with `--model <...advisor...>`. Some
#: sessions run with the Agent tool disabled (2026-11-06 private-taeil: "Agent
#: is disabled for this session"), so the advisor was called through the CLI
#: and the old Agent/Task-only check blocked the answer three times with no
#: way to pass. A CLI call with the advisor model is the same advisor call.
_CLI_ADVISOR_RE = re.compile(r"\bclaude\b[^\n|;&]*--model[ =]\S*")


def _is_advisor_call(call: ToolCall, hint: str) -> bool:
    if call.name in ("Agent", "Task"):
        return hint in str(call.input.get("model") or "")
    if call.name == "Bash":
        m = _CLI_ADVISOR_RE.search(str(call.input.get("command") or ""))
        return bool(m and hint in m.group(0))
    return False


def advisor_skipped(turn: Turn, *, advisor_model_hint: str = "fable") -> bool:
    """True when this channel requires an advisor call before a decision
    ships, the answer reads as asking for / stating a decision, and no call
    this turn used the advisor model — an `Agent`/`Task` call, or a Bash call
    running the claude CLI with `--model` set to the advisor model."""
    if not turn.answer or not turn.is_advisor_channel:
        return False
    if not _ASK_LINE_RE.search(turn.answer) and not _DECISION_MARK_RE.search(turn.answer):
        return False
    return not any(_is_advisor_call(call, advisor_model_hint) for call in turn.tool_calls)


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

_UNCONFIRMED_RE = re.compile(
    r"(확인하지 못|확인되지 않|확인 못|조회하지 못|막혀 있|조회가 막|권한이 없|"
    r"반영되지 않|계속 차단|쓰기가 [^.\n]{0,12}차단|확인\s?불가|조회\s?불가)"
)
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
        # A second call that is denied in the same way is not recovery.  The
        # old check counted the call itself and let "retried twice, still
        # blocked" ship as a finished answer.  Require a later non-denied
        # result for the same target before treating the refusal as handled.
        recovered = any(
            (_call_targets(other) & targets)
            and bool(other.result.strip())
            and not _DENIED_RE.search(other.result)
            for other in calls[i + 1 :]
        )
        if not recovered:
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
#: The verify judgment blocks a turn at most this many times before the
#: `prior_flagged` filter starts. 2026-11-06:
#: with 1, the rewrite after the first block was never checked, and
#: guchipachi's rewrite swapped the flagged claims for new unqueried
#: guesses ("워크플로나 예약 발송") that went out unverified. 2 checks the
#: rewrite once, matching mametchi's finish_work_hook.
VERIFY_MAX_BLOCKS = 1
#: 2026-11-11 (orchestration D): lowered 2 -> 1. From 11-10 21:00 to 11-11
#: 18:00 the model check blocked 146 of 150 runs; 75 checks of a rewrite
#: blocked again, and 182 of the 278 sentences they flagged were already in
#: the blocked draft (the check returns at most three per run, so the rest
#: surfaced one rewrite later). Past this count the check now blocks only on
#: sentences that are new in the rewrite (`prior_drafts`) and were not
#: flagged before (`prior_flagged`).
#: 2026-11-10: past VERIFY_MAX_BLOCKS the model check used to be skipped
#: outright, and a rewrite carrying a stale knowledge-file line ("사무실 IP
#: 는 API허브에서 차단 중") went out unchecked. Now the check keeps running up
#: to this hard cap, but past VERIFY_MAX_BLOCKS it blocks only on sentences
#: no earlier block in the same turn flagged (`prior_flagged`), so a rewrite
#: can't keep bouncing on the same claim.
#: 2026-11-10: lowered 4 -> 2. 76% of one day's 368 checks blocked, and each
#: block made the rewrite run more lookups and add new derived sentences that
#: drew the next block; finish_review took 30% of 7 days' turn time.
#: SCA_FINISH_VERIFY_MAX_BLOCKS=4 restores the old cap
#: (resolve_verify_max_blocks).
VERIFY_HARD_MAX_BLOCKS = 2
#: Env var that overrides VERIFY_HARD_MAX_BLOCKS.
VERIFY_MAX_BLOCKS_ENV = "SCA_FINISH_VERIFY_MAX_BLOCKS"
#: Seconds the nested model call may take.
VERIFY_TIMEOUT = 90
#: Cap on tool-result text handed to the model.
VERIFY_EVIDENCE_CHARS = 120_000
#: Per-tool-result cap, so one huge dump can't crowd out the rest.
VERIFY_CHUNK_CHARS = 12_000
#: Cap on the user prompt included as evidence.
VERIFY_PROMPT_CHARS = 8_000
#: Cap on knowledge-file text included as evidence. The system prompt budget
#: is 64KiB (prompt/composer.py), so a full knowledge load can exceed this;
#: then paragraphs sharing identifiers with the answer are kept first, the
#: same packing pick_evidence does for tool results.
VERIFY_KNOWLEDGE_CHARS = 40_000
#: Env var naming the file that holds this turn's loaded knowledge text. The
#: engine runner writes it for the life of one engine call
#: (engine/runner.py) and the Stop hook reads it (guard/finish_hook.py).
KNOWLEDGE_FILE_ENV = "SCA_FINISH_KNOWLEDGE_FILE"
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


def pick_knowledge(answer: str, knowledge: str, *, limit: int = VERIFY_KNOWLEDGE_CHARS) -> str:
    """Knowledge text under `limit`: all of it when it fits, otherwise the
    blank-line-separated paragraphs that share the most identifiers with the
    answer, in their original order."""
    text = (knowledge or "").strip()
    if len(text) <= limit:
        return text
    paragraphs = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    return pick_evidence(answer, paragraphs, limit=limit)


#: Input fields that say what a call ran or opened, in the order shown.
_INPUT_FIELDS = ("command", "file_path", "notebook_path", "path", "pattern", "url", "query",
                 "sql", "subagent_type", "description", "old_string", "new_string", "content",
                 "prompt")
#: Per-field and total caps for the tool-input section.
VERIFY_INPUT_FIELD_CHARS = 400
VERIFY_INPUT_CHARS = 20_000


def tool_inputs_text(turn: Turn, *, limit: int = VERIFY_INPUT_CHARS) -> str:
    """One line per tool call: name and the input fields that say what it
    ran, opened or wrote. 2026-11-11 (orchestration D): about a third of the
    sentences verify flagged named a path, a line or a command the bot had
    itself run or edited; the check saw only results, so "edited X and
    restarted" had nothing to stand on."""
    lines: list[str] = []
    used = 0
    for i, call in enumerate(turn.tool_calls, start=1):
        parts = []
        for key in _INPUT_FIELDS:
            value = call.input.get(key) if isinstance(call.input, Mapping) else None
            if value in (None, ""):
                continue
            text = " ".join(str(value).split())
            if len(text) > VERIFY_INPUT_FIELD_CHARS:
                text = text[:VERIFY_INPUT_FIELD_CHARS] + " ..."
            parts.append(f"{key}={text}")
        if not parts:
            parts.append(_input_blob(call.input)[:VERIFY_INPUT_FIELD_CHARS])
        line = f"{i}. {call.name} " + " / ".join(parts)
        if used + len(line) > limit:
            lines.append(f"... 호출 {len(turn.tool_calls) - i + 1}건 생략")
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


def _turn_evidence(turn: Turn) -> str:
    results = [call.result for call in turn.tool_calls if call.result]
    prompt = (turn.user_prompt or "")[:VERIFY_PROMPT_CHARS]
    evidence = (prompt + "\n" if prompt else "") + pick_evidence(turn.answer, results)
    inputs = tool_inputs_text(turn)
    if inputs:
        evidence += "\n\n[이번 턴 도구 호출 입력]\n" + inputs
    knowledge = pick_knowledge(turn.answer, turn.knowledge)
    if knowledge:
        evidence += "\n\n[이번 턴 지침에 실린 지식 파일]\n" + knowledge
    return evidence


_VERIFY_PROMPT = (
    "아래 [근거] 는 한 봇이 이번 턴에 실행한 조회 결과와 사용자 입력이고, 끝에 붙은 "
    "[이번 턴 지침에 실린 지식 파일] 은 그 봇이 이번 턴 시스템 지침으로 받은 지식 파일 내용이다. "
    "[답] 은 그 봇이 게시하려는 답이다.\n"
    "[답] 에서 사실 주장 문장만 본다. 사실 주장은 수치, 원천과 계보, 원인, 있음과 없음, 누가 무엇을 했다는 귀속, "
    "실행했다는 보고다.\n"
    "그 중 [근거] 에서 직접 확인되지 않는 문장을 가려낸다. 결론을 좌우하는 문장부터 최대 3개만 낸다.\n"
    "확인하지 못했다는 한계 문장, 의견, 제안, 인사는 사실 주장이 아니므로 제외한다.\n"
    "[근거] 에 비슷한 단어가 있어도 문장이 말하는 관계 자체가 없으면 근거 없음이다. 예를 들어 "
    "A 정의만 조회했는데 답이 B 의 원천이 A 라고 하면 근거 없음이다.\n"
    # 2026-11-10: code-table readings (dispatch_policy 4 = LEGACY_AI) were
    # flagged four times in one turn although the mapping sat in a knowledge
    # file loaded that turn; the check never saw it.
    "지식 파일에 적힌 코드표, 값과 이름의 대응, 용어 정의, 절차, 위치 같은 고정된 정의는 근거로 인정한다. "
    "답의 문장이 지식 파일의 그 정의를 옮겨 쓴 것이면 근거 있음이다.\n"
    # 2026-11-10: a knowledge-file note dated 11/08 ("API허브 TCP 차단") went
    # out as "차단 중임" with no lookup this turn; the check ranked it below
    # three other findings. Stale-state claims now go first.
    "차단, 장애, 진행, 정지, 실패 같은 상태를 '~중', '현재', '지금' 처럼 지금 상태로 단정한 문장은 "
    "[근거] 의 조회 결과가 지금 그 상태임을 직접 보여 주지 않으면 근거 없음이다. 메모, 지식, 과거 기록에 "
    "날짜와 함께 적힌 상태는 지금 상태의 근거가 아니다. 이런 문장은 다른 문장보다 먼저 낸다.\n"
    # 2026-11-11 (orchestration D): "edited X, restarted at 22:35" was flagged
    # because the check saw results only, not the calls that made them.
    "[이번 턴 도구 호출 입력] 은 봇이 이번 턴에 실행한 명령, 연 파일, 고친 내용이다. 무엇을 실행했다, "
    "어느 파일을 열었다, 어느 파일을 어떻게 고쳤다는 보고 문장은 이 입력으로 확인되면 근거 있음이다. "
    "그 실행의 결과, 수치, 상태는 조회 결과로만 판정한다.\n"
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
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        executable: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.model = model
        self.timeout = timeout
        self._runner = runner or _run_killable
        self._executable = executable
        self._env = dict(os.environ if env is None else env)
        self.status = "not_run"
        #: Seconds the nested claude call took, so a slow Stop hook can be
        #: split into the model call and everything else.
        self.elapsed: float | None = None

    def __call__(self, answer: str, evidence: str) -> list[str]:
        if self._env.get(VERIFY_NESTED_ENV):
            self.status = "skip_nested"
            return []
        exe = self._executable or shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
        prompt = _VERIFY_PROMPT + "[근거]\n" + evidence + "\n\n[답]\n" + (answer or "")
        started = time.monotonic()
        try:
            proc = self._runner(
                [exe, "-p", "--model", self.model, "--tools", "", "--strict-mcp-config",
                 "--mcp-config", '{"mcpServers":{}}', "--setting-sources", ""],
                input=prompt, capture_output=True, text=True, timeout=self.timeout,
                env={**self._env, VERIFY_NESTED_ENV: "1"},
            )
        except Exception as exc:  # noqa: BLE001 - a failed check never blocks
            self.status = "error:" + type(exc).__name__
            self.elapsed = round(time.monotonic() - started, 1)
            return []
        self.elapsed = round(time.monotonic() - started, 1)
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


def _run_killable(cmd, *, input, capture_output, text, timeout, env):  # noqa: A002 - subprocess.run's name
    """subprocess.run that kills the whole process group on timeout.

    2026-11-09: a verify call hit the 90 s timeout but the Stop hook took
    196 s; where the other 106 s went is not known (finish-hook-verify.jsonl
    now records elapsed_sec). subprocess.run kills only the direct child and
    leaves claude's own children running, so this kills the whole group.
    """
    del capture_output  # always captured
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=text, env=env, start_new_session=True)
    try:
        out, err = proc.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.communicate(timeout=5)
        except Exception:  # noqa: BLE001 - already timed out; just stop waiting
            pass
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def resolve_verify_settings(env: Mapping[str, str]) -> tuple[bool, str]:
    """(model verify enabled, model name) from env. On unless
    SCA_FINISH_VERIFY is 0/false/off — an engine default, not a profile
    opt-in. Model from SCA_FINISH_VERIFY_MODEL, default sonnet."""
    flag = (env.get("SCA_FINISH_VERIFY") or "").strip().lower()
    enabled = flag not in {"0", "false", "off", "no"}
    model = (env.get("SCA_FINISH_VERIFY_MODEL") or "").strip() or VERIFY_DEFAULT_MODEL
    return enabled, model


def resolve_verify_max_blocks(env: Mapping[str, str]) -> int:
    """The verify block cap from SCA_FINISH_VERIFY_MAX_BLOCKS, or
    VERIFY_HARD_MAX_BLOCKS when unset or not a non-negative integer."""
    raw = (env.get(VERIFY_MAX_BLOCKS_ENV) or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return VERIFY_HARD_MAX_BLOCKS
    return value if value >= 0 else VERIFY_HARD_MAX_BLOCKS


def read_knowledge_file(env: Mapping[str, str], *, limit: int = 4 * VERIFY_KNOWLEDGE_CHARS) -> str:
    """This turn's loaded knowledge text from the file KNOWLEDGE_FILE_ENV
    names, or "" when unset or unreadable. A missing file only means the
    check runs without knowledge, as it did before."""
    path = (env.get(KNOWLEDGE_FILE_ENV) or "").strip()
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read(limit)
    except (OSError, UnicodeDecodeError):
        return ""


def unsupported_claims(turn: Turn, *, verifier: ClaimVerifier = heuristic_claim_verifier) -> list[str]:
    """Sentences in the answer that read as fact claims but aren't backed
    by this turn's own tool results, the user prompt or the knowledge files
    this turn's system prompt carried (`Turn.knowledge`). Empty when the
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


def _named_turn_guidance(hit: str) -> str:
    return (
        f'답에 "{hit}" 처럼 턴의 주체를 사람 이름으로 특정했다. 턴은 역할로 쓴다. '
        "오너가 말한 턴은 오너 턴, 그 밖의 사람은 요청자 턴이나 다른 사람 턴으로 고쳐 답을 다시 쓴다."
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


def _unable_unlooked_guidance(hit: str) -> str:
    return (
        f'"{hit}" 로 볼 수 없다, 알 수 없다고 답했는데 이번 턴에 도구를 한 번도 부르지 않았다. '
        "그 대상을 열 수 있는 경로(슬랙 API, 파일 내려받기 후 Read, 조회 도구)로 같은 턴에 실제로 조회하고, "
        "조회한 결과로 답을 다시 쓴다. 실제로 막히면 그 호출의 오류 문구를 그대로 적는다."
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
    # 2026-11-10: the old text forbade dropping a claim instead of querying
    # it, so the rewrite ran more lookups and added new derived sentences that
    # drew the next block. A sentence the question does not need may now be
    # dropped; a sentence that answers the question still has to be confirmed.
    return (
        f"다음 문장은 이번 턴 조회 결과와 지식 파일에 근거가 없다 : {hit}. "
        "그 문장이 질문에 답하는 데 필요 없는 해석이나 부연이면 조회하지 않고 지운다. "
        "질문의 답을 이루는 사실 문장이면 같은 턴에 원본 메시지, 코드, 데이터, 로그로 직접 조회해 확정한다. "
        "조회로 확정하지 못한 사실 문장은 답에서 빼고, 다른 추측으로 바꾸어 남기지 않는다. "
        "지워도 된다는 것은 근거 없는 사실 단정을 남겨도 된다는 뜻이 아니다. 남기는 사실 문장은 모두 "
        "이번 턴 조회 결과나 지식 파일에 근거가 있어야 한다. "
        "다시 쓸 때 질문에 필요 없는 새 해석 문장을 덧붙이지 않는다. 다시 쓴 답도 같은 검수를 다시 받는다. "
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


# --------------------------------------------------------------------------
# causal_chain — a "- 왜" bullet chain whose steps aren't backed by this
# turn's tool results
# --------------------------------------------------------------------------
#
# 2026-11-13 sandbox-rnd 14:48 "서버 부하 관점으로 5why" 답이 남의 슬랙 보고만
# 옮기고 k8s, 로그 조회를 한 번도 하지 않은 채 게시됐다. mametchi 의
# finish_work_hook.py causal_problems(2026-10-25) 가 막던 유형인데, sca 로
# 옮길 때 review/five_why.py 의 표 형식 검사만 넘어와 불릿 형식 사슬은 아무
# 검사도 받지 않았다. 그 판정을 이식하고 "확인 필요" 로 넘긴 검증 항목을 더한다.

_CC_WHY_LINE_RE = re.compile(r"^-\s*(왜|why)\b", re.I)
_CC_HEDGE_RE = re.compile(
    r"(못했|하지 못|확인되지 않|확인 못|알 수 없|모르겠|모르는 상태|추정|가설|가능성이\s*(커|크|높|있)"
    r"|로 보여요|로 보입니다|로 봐요|로 봄|것 같|듯해|듯합|아닐까|미확인|여지가|배제할 수 없|단정할 수 없|단정하기 어렵)"
)
_CC_UNVERIFIED_HEAD_RE = re.compile(r"^#+\s*[^\n]*(확인하지 못|미확인|확인 못|모르는 것)", re.M)
_CC_NUM_RE = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")
_CC_DATE_LIKE_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}|\d{1,2}:\d{2}|\d{1,2}-\d{1,2}\s*시|\d+\s*why", re.I
)
_CC_IDENT_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[_.\-][A-Za-z0-9]+)+")
#: 바이트→MB·GB, 천 단위, 비율→%.
_CC_SCALES = (1, 1000, 1024, 1024**2, 1024**3, 1000**2, 1000**3, 0.01)
#: "k8s 이벤트로 확인 필요" 처럼 사람 지목 없이 넘긴 검증 항목. 사람이나 팀이
#: 할 일("서비스개발1팀 확인 대상")은 넘겨도 된다.
_CC_DEFERRED_RE = re.compile(r"(확인|비교|조회|대조|측정)\s?필요")
_CC_OWNER_RE = re.compile(r"(님|팀|담당)")


_CC_SECONDARY_TOOLS = ("mcp__slack__", "ToolSearch", "Skill", "mcp__notion__notion-search")


def _cc_is_primary(name: str) -> bool:
    return not name.startswith(_CC_SECONDARY_TOOLS)


def _cc_tool_numbers(turn: Turn) -> set[float]:
    out: set[float] = set()
    for call in turn.tool_calls:
        for n in _CC_NUM_RE.findall(call.result or ""):
            try:
                out.add(float(n.replace(",", "")))
            except ValueError:
                pass
    return out


def _cc_grounded(text: str, nums: set[float]) -> bool:
    """True if any number in text matches a tool-result number, allowing
    unit scaling and rounding. Single-digit integers don't count."""
    for n in _CC_NUM_RE.findall(_CC_DATE_LIKE_RE.sub(" ", text)):
        raw = n.replace(",", "")
        if len(raw) < 2 and "." not in raw:
            continue
        x = float(raw)
        if "." in raw:
            tol = 10 ** -len(raw.split(".")[1])
        elif raw.endswith("0"):
            tol = max(1.0, 0.03 * x)
        else:
            tol = 1.0
        if any(abs(t / s - x) <= tol for t in nums for s in _CC_SCALES):
            return True
    return False


def _cc_why_blocks(answer: str) -> list[str]:
    blocks: list[list[str]] = []
    cur: list[str] | None = None
    for line in answer.splitlines():
        if _CC_WHY_LINE_RE.match(line):
            if cur:
                blocks.append(cur)
            cur = [line]
        elif cur is not None and (line.startswith("#") or line.startswith("- ")):
            blocks.append(cur)
            cur = None
        elif cur is not None:
            cur.append(line)
    if cur:
        blocks.append(cur)
    return ["\n".join(b) for b in blocks]


def _cc_refute_block(answer: str) -> str | None:
    lines = answer.splitlines()
    for i, line in enumerate(lines):
        if "반증" in line and not line.startswith("#"):
            out = [line]
            for nxt in lines[i + 1 :]:
                if nxt.startswith(("  ", "\t")):
                    out.append(nxt)
                else:
                    break
            return "\n".join(out)
    return None


def causal_chain(turn: Turn) -> str | None:
    """Problems of a '- 왜' chain answer, joined; None if not a chain or clean."""
    answer = turn.answer
    if not answer or "[[침묵]]" in answer:
        return None
    body = _QUOTED.sub("", answer)
    blocks = _cc_why_blocks(body)
    if len(blocks) < 2:
        return None
    nums = _cc_tool_numbers(turn)
    corpus = "\n".join(call.result or "" for call in turn.tool_calls)
    probs: list[str] = []
    # 슬랙 보고만 읽은 사슬은 남의 분석을 옮긴 것이다. 14:48 답의 72,001건,
    # 4-7초 같은 수치는 전부 장애 채널 보고에서 왔고 원천 조회는 0회였다.
    if not any(_cc_is_primary(call.name) for call in turn.tool_calls):
        probs.append("원천 조회 0회. 슬랙 보고만 읽고 사슬을 세움")
    for b in blocks:
        head = b.splitlines()[0].strip()[:40]
        h = _CC_HEDGE_RE.search(b)
        if h:
            probs.append(f'단계 "{head}" 에 추정 표현 "{h.group(0)}"')
        if not _cc_grounded(b, nums) and not any(
            len(t) >= 8 and t in corpus for t in _CC_IDENT_RE.findall(b)
        ):
            probs.append(f'단계 "{head}" 에 이번 턴 도구 결과로 뒷받침되는 수치나 식별자가 없음')
    rb = _cc_refute_block(body)
    if rb is None:
        probs.append("반증 확인 줄이 없음")
    else:
        if not _cc_grounded(rb, nums):
            probs.append("반증 줄에 도구 결과 수치가 없음")
        # 반증 줄에서 "가설은 깨짐" 은 정상 서술이라 가설 단어만 빼고 본다
        h = _CC_HEDGE_RE.search(rb.replace("가설", ""))
        if h:
            probs.append(f'반증 줄에 추정 표현 "{h.group(0)}"')
    u = _CC_UNVERIFIED_HEAD_RE.search(body)
    if u:
        probs.append(f'미확인 섹션 제목 "{u.group(0).strip()}"')
    for line in body.splitlines():
        if _CC_DEFERRED_RE.search(line) and not _CC_OWNER_RE.search(line):
            probs.append(f'직접 할 수 있는 검증을 넘김 "{line.strip()[:60]}"')
    return "; ".join(probs) if probs else None


def _causal_chain_guidance(hit: str) -> str:
    return (
        "인과 사슬 답에 결함이 있다 : " + hit + ". "
        "각 단계는 이번 턴 도구 결과의 수치로 뒷받침한다. 다른 사람의 슬랙 보고를 옮긴 것은 근거가 아니다. "
        "원천(k8s 이벤트, 로그, 지표, 코드, DB)을 이번 턴에 조회해 확정하고, '확인 필요' 로 남긴 항목은 지금 조회한다. "
        "조회 수단이 정말 없는 것만 사슬에서 빼고 '누가 무엇을 실행' 한 줄로 남긴다. "
        "'- 반증 확인' 줄에 반대 가설을 깨는 수치를 쓴다. 반증에서 인과가 깨지면 사슬과 결론을 고친다."
    )


def _build_common_registry() -> JudgmentRegistry:
    common = JudgmentRegistry()
    common.register("gave_up", gave_up, _gave_up_guidance)
    common.register("not_opened", not_opened, _not_opened_guidance)
    common.register("unable_unlooked", unable_unlooked, _unable_unlooked_guidance)
    common.register("stopped_unfinished", stopped_unfinished, _stopped_unfinished_guidance)
    common.register("absence_unopened", absence_unopened, _absence_unopened_guidance)
    common.register("offer", offer, _offer_guidance)
    common.register("handoff", handoff, _handoff_guidance)
    common.register("denied_unretried", denied_unretried, _denied_unretried_guidance)
    # advisor_skipped 는 등록하지 않는다. 2026-11-10 김태일 지시 "조언자 강제 훅을 제거하세요".
    # 판정 함수(advisor_skipped)는 남겨 두고 종료 검수에서만 뺀다.
    common.register("code_first", code_first_doc_only, _code_first_guidance)
    common.register("named_turn", named_turn, _named_turn_guidance)
    common.register("verify", _unsupported_claims_check, _verify_guidance)
    common.register("causal_chain", causal_chain, _causal_chain_guidance)
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


def _drop_spent_gave_up(findings: tuple[Finding, ...], block_count: int) -> tuple[Finding, ...]:
    """gave_up blocks once; after that it is dropped before the verify gate,
    so the rewrite still gets the model verify instead of skipping it."""
    if block_count >= GAVE_UP_MAX_BLOCKS:
        return tuple(f for f in findings if f.name != "gave_up")
    return findings


def decide(
    turn: Turn,
    *,
    registry: JudgmentRegistry | None = None,
    block_count: int = 0,
    max_blocks: int = 2,
    seconds_left: float | None = None,
    release_sec: int = 240,
    claim_verifier: ClaimVerifier | None = None,
    prior_flagged: frozenset[str] = frozenset(),
    verify_hard_max_blocks: int = VERIFY_HARD_MAX_BLOCKS,
    prior_drafts: Sequence[str] = (),
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

    `verify_hard_max_blocks` is the most times the model verify may block
    one turn (resolve_verify_max_blocks reads the env override).

    `prior_drafts` are the answers this turn already had blocked. Past
    VERIFY_MAX_BLOCKS the check blocks only on sentences that are not in any
    of them (`carried_over`).
    """
    if not turn.has_transcript:
        return StopDecision(action="allow")
    active_registry = default_registry if registry is None else registry
    if claim_verifier is None:
        findings = _drop_spent_gave_up(tuple(active_registry.evaluate(turn)), block_count)
    else:
        findings = _drop_spent_gave_up(
            tuple(active_registry.evaluate(turn, skip=("verify",))), block_count
        )
        # Model verify (mametchi's order): only when nothing cheaper already
        # flagged the turn, at most VERIFY_MAX_BLOCKS blocks per request, and
        # never so close to the deadline that the rewrite can't land.
        budget_ok = seconds_left is None or seconds_left > release_sec + VERIFY_TIMEOUT
        if not findings and budget_ok and block_count < verify_hard_max_blocks:
            claims = unsupported_claims(turn, verifier=claim_verifier)
            if block_count >= VERIFY_MAX_BLOCKS:
                claims = [
                    c for c in claims
                    if claim_key(c) not in prior_flagged and not carried_over(c, prior_drafts)
                ]
            if claims:
                hit = " | ".join(claims)
                findings = (Finding(name="verify", detail=hit, guidance=_verify_guidance(hit)),)
    if not findings:
        return StopDecision(action="allow")
    if seconds_left is not None and seconds_left < release_sec:
        return StopDecision(action="release", findings=findings)
    only_verify = all(f.name == "verify" for f in findings)
    if block_count >= max_blocks and not (only_verify and block_count < verify_hard_max_blocks):
        return StopDecision(action="release", findings=findings)
    return StopDecision(action="block", findings=findings)


def claim_key(finding_text: str) -> str:
    """The flagged sentence of one verify finding ("sentence (why)"),
    whitespace-normalized, so a later block can tell a claim it already
    flagged from a new one."""
    text = finding_text or ""
    cut = text.rfind(" (")
    sentence = text[:cut] if cut > 0 else text
    return " ".join(sentence.split())[:160]


def draft_key(text: str) -> str:
    """An answer with all whitespace and markdown emphasis removed, so a
    sentence can be looked up in it however the rewrite re-wrapped it."""
    return re.sub(r"[\s*_`>]+", "", text or "")


#: Shortest flagged-sentence key `carried_over` looks up. Shorter ones
#: ("0", a bare date) match almost any draft.
_CARRY_MIN_CHARS = 8


def carried_over(finding_text: str, prior_drafts: Sequence[str]) -> bool:
    """Whether the sentence a verify finding flagged was already in an
    answer blocked earlier in this turn (`prior_drafts` hold draft_key
    forms). A rewrite that keeps an earlier sentence is not re-blocked for
    it: that sentence already had one check and one chance to be fixed."""
    key = draft_key(claim_key(finding_text).lstrip("-• "))
    if len(key) < _CARRY_MIN_CHARS:
        return False
    return any(key in draft for draft in prior_drafts)


def verify_claim_keys(decision: StopDecision) -> set[str]:
    """claim_key of every sentence a verify finding in `decision` flagged."""
    keys: set[str] = set()
    for f in decision.findings:
        if f.name == "verify":
            keys |= {claim_key(part) for part in f.detail.split(" | ") if part.strip()}
    return keys
