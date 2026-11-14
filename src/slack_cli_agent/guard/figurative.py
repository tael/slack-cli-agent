"""Pre-post correction of figurative wording by a separate model.

Ports mametchi bot.py:7268-7357 and 8162-8176. Adding banned words to the
persona failed six times (맞닿은, 갈래, 몫, ...) and the answering model's own
self-check misses them, so a second model rewrites the reply before it is
posted. Two modes:

- chat channel: personification, metaphor, unnatural collocation, and forced
  wit. Only forced wit may delete a sentence.
- work channel: the first three only, standard document wording, never
  deletes. Skipped for short replies.

A correction is used only if it passes `figurative_accept`; any failure keeps
the original. This step must never stop a reply from going out.
"""

from __future__ import annotations

import contextlib
import difflib
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, ClassVar, Protocol

from slack_cli_agent.config.channel import ChannelConfig
from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.guard.base import GuardContext, GuardResult, OutputGuard
from slack_cli_agent.prompt.code_terms import replace_code_terms
from slack_cli_agent.prompt.sections import is_silent

FIGURATIVE_CHECK_SYSTEM = """너는 한국어 교정자다. <reply> 안의 슬랙 답글을 읽고 아래 네 가지에 해당하는 문장만 고친다.
- 의인화 : 사물, 감정, 신체 상태, 시간, 지표처럼 사람이 아닌 것을 주어로 세워 사람이 하는 동작을 시킨 문장. 예 : 졸음이 출근한다, 그래프가 평온한 척한다
- 비유 : 다른 대상에 빗대어 말한 문장. 예 : 이 코드는 시한폭탄이다
- 어색한 결합 : 한국어 모어 화자가 쓰지 않는 명사와 동사의 결합. 예 : 졸음이 붙다. 졸음은 오거나 쏟아지고 사람은 졸리다
- 억지 위트 : 상대 말이나 질문 내용과 이어지지 않는데 분위기를 띄우려고 붙인 농담, 과장된 다짐, 끝에 지어 붙인 덕담. 예 : 국악단 레퍼토리에서 영구 제명하겠습니다, 부장님 손에 야구방망이가 있잖아요
널리 굳은 일상 표현은 고치지 않는다. 시간이 가다, 졸음이 오다, 마음이 급하다 같은 것이다.
앞의 세 가지는 그 구절만 같은 뜻의 평범한 말로 바꾼다. 억지 위트는 그 문장 전체를 before 에 적고 after 를 빈 문자열로 둔다.
고친 구절 밖은 말투, 반말과 존댓말, 문장 길이, 줄바꿈을 한 글자도 바꾸지 않는다.
코드블록, 링크, 꺾쇠 멘션, 숫자, 이중 대괄호 표식은 건드리지 않는다.
출력은 JSON 하나만 쓴다. 설명을 붙이지 않는다. 답글 전체를 다시 쓰지 않는다.
before 에는 바꿀 부분만 원문 그대로 적는다. 의인화, 비유, 어색한 결합은 문장 전체가 아니라 해당 구절만 적는다. after 에는 그 부분에 들어갈 말만 적는다.
고칠 것이 없으면 {"changed": false}
고쳤으면 {"changed": true, "fixes": [{"before": "<원문에서 바꿀 구절>", "after": "<바꾼 구절>"}]}"""

FIGURATIVE_CHECK_SYSTEM_WORK = (
    FIGURATIVE_CHECK_SYSTEM
    .replace("아래 네 가지에", "아래 세 가지에")
    .replace("- 억지 위트 : 상대 말이나 질문 내용과 이어지지 않는데 분위기를 띄우려고 붙인 농담, 과장된 다짐, 끝에 지어 붙인 덕담. 예 : 국악단 레퍼토리에서 영구 제명하겠습니다, 부장님 손에 야구방망이가 있잖아요\n", "")
    .replace("앞의 세 가지는 그 구절만 같은 뜻의 평범한 말로 바꾼다. 억지 위트는 그 문장 전체를 before 에 적고 after 를 빈 문자열로 둔다.",
             "그 구절만 같은 뜻의 업무 문서 표준어로 바꾼다. 문장을 지우지 않는다. after 를 비우지 않는다. "
             "업계에서 굳은 기술 용어는 비유로 보지 않는다. 하류, 상류, 병목, 캐시 적중, 붙이다(첨부, 연결), 끊다(연결 종료) 같은 것이다. 바꾸면 뜻이 달라지는 구절은 고치지 않는다. 맞닿은, 갈래, 몫, 칸, 틀처럼 사물이나 신체를 빌린 어휘도 비유로 본다.")
)

#: Trailing `[[...]]` markers (WATCH, ASK, ...) are cut off before correction
#: and put back after, so the corrector never sees or mangles them.
_TAIL_TAG_RE = re.compile(r"(?:\n[ \t]*\[\[[^\]\n]*\]\][ \t]*)+\s*$")
#: Tokens a correction must keep exactly: whole fenced blocks (an unclosed
#: fence falls back to the bare ```), mentions, links, `[[...]]` markers
#: anywhere in the body, inline code, and digit sequences such as 1,234,
#: run ids and amounts. The prompt asks the corrector to leave these alone;
#: this enforces it.
_KEEP_TOKEN_RE = re.compile(
    r"```.*?```|```|<[@#!][^>]+>|https?://\S+|\[\[[^\]\n]*\]\]|`[^`\n]+`|\d+(?:[.,:]\d+)*",
    re.DOTALL,
)
#: A body longer than this is posted uncorrected. Sending only the head and
#: swapping the result in would drop the rest (mametchi bot.py:7339 did).
_MAX_INPUT_CHARS = 6000

log = logging.getLogger(__name__)


def is_chat_channel(config: ChannelConfig | None) -> bool:
    """Chat channel = `chat: active` or `mode: chat`, as bot.py is_chat_channel.
    An unregistered channel is a work channel."""
    if config is None:
        return False
    return config.chat == "active" or config.mode == "chat"


def figurative_accept(original: str, revised: str) -> bool:
    """Whether a correction can replace the original. Rejects an empty
    result, a length outside 0.5-1.5x, and any `_KEEP_TOKEN_RE` token
    (code block, mention, link, marker, inline code, number) that
    disappeared, changed or was added."""
    if not revised or not revised.strip():
        return False
    ratio = len(revised) / max(len(original), 1)
    if not 0.5 <= ratio <= 1.5:
        return False
    return sorted(_KEEP_TOKEN_RE.findall(original)) == sorted(_KEEP_TOKEN_RE.findall(revised))


#: A fix may grow its span by at most this factor, or by this many characters
#: for a short span. 2026-11-12: a corrector "fix" turned 젤리빈 몸 into
#: 길쭉하고 둥근 타원형 몸, adding a description the answer never had.
_FIX_GROWTH = 1.5
_FIX_GROWTH_CHARS = 4
#: Shortest span a fix may replace. Every occurrence is replaced, so a
#: one-character span would hit unrelated words.
_FIX_MIN_CHARS = 2


def fix_ok(before: str, after: str, *, work: bool) -> bool:
    """Whether one before/after pair may be applied. Keeps every
    `_KEEP_TOKEN_RE` token, refuses an empty `after` in a work channel (no
    deletion there) and a span that grows past `_FIX_GROWTH`."""
    if len(before.strip()) < _FIX_MIN_CHARS or before == after:
        return False
    if not after.strip():
        return not work
    if len(after) > max(len(before) * _FIX_GROWTH, len(before) + _FIX_GROWTH_CHARS):
        return False
    return sorted(_KEEP_TOKEN_RE.findall(before)) == sorted(_KEEP_TOKEN_RE.findall(after))


def apply_fixes(
    text: str, fixes: Sequence[Mapping[str, Any]], *, work: bool,
) -> tuple[str, list[dict[str, str]], int]:
    """(text, applied fixes, dropped count). The corrector names spans; code
    swaps them, so nothing outside a named span can change. A fix whose
    span is not in the text or fails `fix_ok` is dropped on its own."""
    applied: list[dict[str, str]] = []
    dropped = 0
    for fix in fixes[:20]:
        before = str(fix.get("before") or "")
        after = str(fix.get("after") or "")
        if not before or before not in text or not fix_ok(before, after, work=work):
            dropped += 1
            continue
        text = text.replace(before, after)
        applied.append({"before": before, "after": after})
    if applied and not work:
        text = re.sub(r"\n{3,}", "\n\n", text)
    return text, applied, dropped


def body_key(text: str) -> str:
    """Whitespace- and markup-blind key, the same shape as
    finish_review.draft_key, to match the Stop hook's draft to the body the
    output guards hand over."""
    return re.sub(r"[\s*_`>]+", "", text)


#: How close the guard's body must be to the draft the Stop hook corrected.
#: The guards before this one only touch mentions, links and markers.
_MATCH_RATIO = 0.98


def same_body(a: str, b: str) -> bool:
    if a == b:
        return True
    if not a or not b or abs(len(a) - len(b)) > max(len(a), len(b)) * (1 - _MATCH_RATIO):
        return False
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return matcher.quick_ratio() >= _MATCH_RATIO and matcher.ratio() >= _MATCH_RATIO


def prepare(body: str) -> tuple[str, str, list[dict[str, str]]]:
    """(main with code terms replaced, trailing marker tail, code-term fixes).
    Shared by the guard and the Stop hook so both correct the same text."""
    m = _TAIL_TAG_RE.search(body)
    main, tail = (body[: m.start()], body[m.start():]) if m else (body, "")
    main, term_fixes = replace_code_terms(main)
    return main, tail, term_fixes


class FigurativeCorrector(Protocol):
    def __call__(self, text: str, *, work: bool) -> Mapping[str, Any] | None:
        """The corrector's JSON verdict, or None when the check could not run."""


class ModelFigurativeCorrector:
    """Runs a nested `claude -p` with no tools, MCP servers or setting sources,
    like `ModelClaimVerifier`. The API key is dropped from the environment so
    the call uses the CLI login as bot.py did. `runner` is injectable; the
    default runs the CLI in its own process group so `cancel()` can stop it
    (the Stop hook drops a correction once verify blocks the draft)."""

    def __init__(
        self,
        *,
        model: str = "sonnet",
        timeout: float = 45,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        executable: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.model = model
        self.timeout = timeout
        self._runner = runner
        self._executable = executable
        env_map = dict(os.environ if env is None else env)
        env_map.pop("ANTHROPIC_API_KEY", None)
        env_map.pop("ANTHROPIC_AUTH_TOKEN", None)
        self._env = env_map
        self._proc: subprocess.Popen[str] | None = None
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True
        proc = self._proc
        if proc is not None and proc.poll() is None:
            with contextlib.suppress(OSError):
                os.killpg(proc.pid, signal.SIGKILL)

    def _run(self, cmd: list[str], text: str) -> str:
        if self._runner is not None:
            proc = self._runner(cmd, input=text, capture_output=True, text=True,
                                timeout=self.timeout, env=self._env, cwd="/tmp")
            return proc.stdout or ""
        if self._cancelled:
            return ""
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=self._env, cwd="/tmp", start_new_session=True,
        )
        try:
            out, _ = self._proc.communicate(input=text, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            self.cancel()
            self._proc.communicate()
            raise
        return out or ""

    def __call__(self, text: str, *, work: bool) -> Mapping[str, Any] | None:
        exe = self._executable or shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
        system = FIGURATIVE_CHECK_SYSTEM_WORK if work else FIGURATIVE_CHECK_SYSTEM
        try:
            out = self._run(
                [exe, "-p", "--model", self.model, "--tools", "", "--strict-mcp-config",
                 "--mcp-config", '{"mcpServers":{}}', "--setting-sources", "",
                 "--no-session-persistence", "--system-prompt", system],
                "<reply>\n" + text + "\n</reply>",
            )
        except Exception:  # noqa: BLE001 - a failed check never blocks the reply
            return None
        start, end = out.find("{"), out.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            verdict = json.loads(out[start : end + 1])
        except ValueError:
            return None
        return verdict if isinstance(verdict, dict) else None


# -- Stop hook precompute ---------------------------------------------------
#
# 2026-11-12: the corrector call took 6.7-9.2 s on 1,000-1,200 character
# replies it left unchanged (sonnet emits 700-1,200 hidden thinking tokens
# for an 18-character answer; MAX_THINKING_TOKENS=0, --effort low,
# alwaysThinkingEnabled=false and older models did not remove them), and
# 3.7-4.1 s on a 200-character one. Production verify blocks the first draft
# 97% of the time, and the rewrite carries most lines over. So every Stop
# starts a detached corrector on the lines no earlier draft of this turn has
# had checked; a blocked draft's check finishes while the model rewrites,
# and the final draft waits only for its new lines, or for nothing.

#: Set per turn by the pipeline: where the hook writes, the channel mode
#: (``work``/``chat``), the work-channel length floor, model and time limit.
OUT_ENV = "SCA_FIGURATIVE_OUT"
MODE_ENV = "SCA_FIGURATIVE_MODE"
MIN_CHARS_ENV = "SCA_FIGURATIVE_MIN_CHARS"
MODEL_ENV = "SCA_FIGURATIVE_MODEL"
TIMEOUT_ENV = "SCA_FIGURATIVE_TIMEOUT"
ENV_NAMES = (OUT_ENV, MODE_ENV, MIN_CHARS_ENV, MODEL_ENV, TIMEOUT_ENV)
#: New text shorter than this after a rewrite is not sent to the model.
_NEW_MIN_CHARS = 10

HOOK_SCRIPT = Path(__file__).resolve()


def _needs_check(main: str, *, chat: bool, min_chars: int) -> bool:
    if not main.strip() or is_silent(main) or len(main) > _MAX_INPUT_CHARS:
        return False
    return chat or len(main) >= min_chars


def _line_keys(text: str) -> list[str]:
    return [k for k in (body_key(line) for line in text.split("\n")) if k]


def _draft_dir(out: Path) -> Path:
    return out.with_name(out.name + ".d")


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def run_worker(job_path: Path, *, corrector: FigurativeCorrector | None = None) -> dict[str, Any]:
    """Detached body: correct one draft's new lines and write
    ``<draft>.json`` next to the job file: the line keys it checked and the
    fixes, or ``ok: false`` when the call failed."""
    job = _read_json(job_path) or {}
    text = str(job.get("text") or "")
    corrector = corrector or ModelFigurativeCorrector(
        model=str(job.get("model") or "sonnet"), timeout=float(job.get("timeout") or 45),
    )
    started = time.monotonic()
    try:
        verdict = corrector(text, work=bool(job.get("work", True)))
    except Exception:  # noqa: BLE001 - recorded as a failed check
        verdict = None
    ok = isinstance(verdict, Mapping)
    fixes = [dict(f) for f in ((verdict or {}).get("fixes") or []) if isinstance(f, Mapping)] if ok else []
    record = {"ok": ok, "lines": _line_keys(text) if ok else [], "fixes": fixes,
              "elapsed_sec": round(time.monotonic() - started, 1)}
    _write_json(job_path.with_suffix(".json"), record)
    with contextlib.suppress(OSError):
        job_path.unlink()
    return record


def _spawn_worker(job_path: Path) -> None:
    subprocess.Popen(
        [sys.executable, str(HOOK_SCRIPT), "--worker", str(job_path)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


class Precompute:
    """One Stop of a turn: starts the check of this draft's unchecked lines
    and, when the draft is final, merges every draft's fixes into the record
    the guard reads (`OUT_ENV`)."""

    def __init__(self, main: str, out: Path, mode: str, draft_id: str, started: bool) -> None:
        self.main = main
        self.out = out
        self.mode = mode
        self.draft_id = draft_id
        self.started = started
        self.key = body_key(main)

    @classmethod
    def start(cls, answer: str, env: Mapping[str, str], *,
              spawn: Callable[[Path], None] = _spawn_worker) -> Precompute | None:
        if not env.get(OUT_ENV) or not env.get(MODE_ENV):
            return None
        chat = env.get(MODE_ENV) == "chat"
        try:
            min_chars = int(env.get(MIN_CHARS_ENV) or 80)
        except ValueError:
            min_chars = 80
        main, _, _ = prepare(answer or "")
        if not _needs_check(main, chat=chat, min_chars=min_chars):
            return None
        out = Path(env[OUT_ENV])
        folder = _draft_dir(out)
        folder.mkdir(parents=True, exist_ok=True)
        checked: set[str] = set()
        for done in folder.glob("*.json"):
            record = _read_json(done)
            if record and record.get("ok"):
                checked.update(record.get("lines") or [])
        new = "\n".join(line for line in main.split("\n") if body_key(line) and body_key(line) not in checked)
        draft_id = hashlib.sha1(body_key(main).encode("utf-8")).hexdigest()[:16]
        started = len(new.strip()) >= _NEW_MIN_CHARS
        if started:
            job = folder / f"{draft_id}.job"
            _write_json(job, {"text": new, "work": not chat, "model": env.get(MODEL_ENV) or "sonnet",
                              "timeout": env.get(TIMEOUT_ENV) or 45})
            spawn(job)
        return cls(main, out, "chat" if chat else "work", draft_id, started)

    def finish(self, timeout: float = 45) -> dict[str, Any] | None:
        """Wait for this draft's check, then write the turn's record from the
        fixes of every draft that still apply. A failed check of this draft
        writes nothing: the guard then corrects the body itself."""
        folder = _draft_dir(self.out)
        mine = folder / f"{self.draft_id}.json"
        deadline = time.monotonic() + timeout
        while self.started and not mine.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        if self.started and not (_read_json(mine) or {}).get("ok"):
            return None
        fixes: list[dict[str, Any]] = []
        seen: set[str] = set()
        for done in sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime):
            for fix in (_read_json(done) or {}).get("fixes") or []:
                before = str(fix.get("before") or "")
                if before and before in self.main and before not in seen:
                    seen.add(before)
                    fixes.append(fix)
        record = {"key": self.key, "mode": self.mode,
                  "verdict": {"changed": bool(fixes), "fixes": fixes},
                  "elapsed_sec": (_read_json(mine) or {}).get("elapsed_sec") if self.started else 0.0,
                  "new_lines": self.started, "at": time.time()}
        try:
            _write_json(self.out, record)
        except OSError:
            return None
        return record


def read_precomputed(path: str | None) -> dict[str, Any] | None:
    """The Stop hook's record for this turn. The record and the per-draft
    folder are removed once read."""
    if not path:
        return None
    file = Path(path)
    record = _read_json(file)
    with contextlib.suppress(OSError):
        file.unlink()
    shutil.rmtree(_draft_dir(file), ignore_errors=True)
    return record


def main(argv: list[str]) -> int:
    if len(argv) >= 3 and argv[1] == "--worker":
        with contextlib.suppress(Exception):  # detached; nothing to report to
            run_worker(Path(argv[2]))
    return 0


class FigurativeGuard(OutputGuard):
    """Applies the corrector before posting. Off unless `figurative_check`.

    Uses the Stop hook's verdict (`GuardContext.figurative_precomputed`) when
    it was made for this body and runs the corrector itself otherwise."""

    name: ClassVar[str] = "figurative_fix"

    def __init__(
        self,
        settings: RuntimeSettings,
        *,
        is_chat: Callable[[str], bool],
        corrector: FigurativeCorrector | None = None,
    ) -> None:
        self._enabled = settings.figurative_check
        self._min_chars = settings.figurative_min_chars
        self._is_chat = is_chat
        self._corrector = corrector or ModelFigurativeCorrector(
            model=settings.figurative_model, timeout=settings.figurative_timeout_sec,
        )

    def _verdict(self, main: str, mode: str, ctx: GuardContext) -> tuple[Mapping[str, Any] | None, str]:
        pre = getattr(ctx, "figurative_precomputed", None)
        if (isinstance(pre, Mapping) and pre.get("mode") == mode
                and isinstance(pre.get("verdict"), Mapping)
                and same_body(str(pre.get("key") or ""), body_key(main))):
            return pre["verdict"], "stop_hook"
        return self._corrector(main, work=mode == "work"), "inline"

    def apply(self, body: str, ctx: GuardContext) -> GuardResult:
        unchanged = GuardResult(body=body, changed=False)
        if not self._enabled or not (body or "").strip() or is_silent(body):
            return unchanged
        chat = self._is_chat(ctx.channel)
        mode = "chat" if chat else "work"
        main, tail, term_fixes = prepare(body)
        if not main.strip():
            return unchanged
        with_terms = GuardResult(
            body=main + tail, changed=True,
            detail={"mode": mode, "fixes": term_fixes, "source": "code_terms"},
        ) if term_fixes else unchanged
        if not _needs_check(main, chat=chat, min_chars=self._min_chars):
            if len(main) > _MAX_INPUT_CHARS:
                log.info("figurative check skipped: body too long (%d chars, limit %d) channel=%s",
                         len(main), _MAX_INPUT_CHARS, ctx.channel)
            return with_terms
        verdict, source = self._verdict(main, mode, ctx)
        if not verdict:
            log.warning("figurative check could not run (corrector failed or bad JSON) mode=%s channel=%s",
                        mode, ctx.channel)
            return with_terms
        if not verdict.get("changed"):
            return with_terms
        fixes = [f for f in (verdict.get("fixes") or []) if isinstance(f, Mapping)]
        revised, applied, dropped = apply_fixes(main, fixes, work=not chat)
        if not applied or not figurative_accept(main.strip("\n"), revised.strip("\n")):
            if dropped or applied:
                log.warning("figurative correction rejected: mode=%s channel=%s dropped=%d",
                            mode, ctx.channel, dropped + len(applied))
            if term_fixes:
                return GuardResult(body=main + tail, changed=True,
                                   detail={**with_terms.detail, "rejected": dropped + len(applied)})
            return GuardResult(body=body, changed=False, detail={"rejected": True})
        return GuardResult(
            body=revised + tail, changed=True,
            detail={"mode": mode, "fixes": [*term_fixes, *applied][:10], "source": source,
                    "dropped": dropped},
        )


if __name__ == "__main__":
    sys.exit(main(sys.argv))
