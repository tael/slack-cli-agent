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

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from typing import Any, ClassVar, Protocol

from slack_cli_agent.config.channel import ChannelConfig
from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.guard.base import GuardContext, GuardResult, OutputGuard
from slack_cli_agent.prompt.sections import is_silent

FIGURATIVE_CHECK_SYSTEM = """너는 한국어 교정자다. <reply> 안의 슬랙 답글을 읽고 아래 네 가지에 해당하는 문장만 고친다.
- 의인화 : 사물, 감정, 신체 상태, 시간, 지표처럼 사람이 아닌 것을 주어로 세워 사람이 하는 동작을 시킨 문장. 예 : 졸음이 출근한다, 그래프가 평온한 척한다
- 비유 : 다른 대상에 빗대어 말한 문장. 예 : 이 코드는 시한폭탄이다
- 어색한 결합 : 한국어 모어 화자가 쓰지 않는 명사와 동사의 결합. 예 : 졸음이 붙다. 졸음은 오거나 쏟아지고 사람은 졸리다
- 억지 위트 : 상대 말이나 질문 내용과 이어지지 않는데 분위기를 띄우려고 붙인 농담, 과장된 다짐, 끝에 지어 붙인 덕담. 예 : 국악단 레퍼토리에서 영구 제명하겠습니다, 김태일님 손에 야구방망이가 있잖아요
널리 굳은 일상 표현은 고치지 않는다. 시간이 가다, 졸음이 오다, 마음이 급하다 같은 것이다.
앞의 세 가지는 그 문장만 같은 뜻의 평범한 문장으로 바꾼다. 억지 위트는 그 문장을 지운다. 지울 때 앞뒤 문장이 어색해지면 짧게 이어 준다.
고친 문장 밖은 말투, 반말과 존댓말, 문장 길이, 줄바꿈을 한 글자도 바꾸지 않는다.
코드블록, 링크, 꺾쇠 멘션, 숫자, 이중 대괄호 표식은 건드리지 않는다.
출력은 JSON 하나만 쓴다. 설명을 붙이지 않는다.
고칠 것이 없으면 {"changed": false}
고쳤으면 {"changed": true, "text": "<고친 전체 답글>", "fixes": [{"before": "<원래 문장>", "after": "<바꾼 문장>"}]}"""

FIGURATIVE_CHECK_SYSTEM_WORK = (
    FIGURATIVE_CHECK_SYSTEM
    .replace("아래 네 가지에", "아래 세 가지에")
    .replace("- 억지 위트 : 상대 말이나 질문 내용과 이어지지 않는데 분위기를 띄우려고 붙인 농담, 과장된 다짐, 끝에 지어 붙인 덕담. 예 : 국악단 레퍼토리에서 영구 제명하겠습니다, 김태일님 손에 야구방망이가 있잖아요\n", "")
    .replace("앞의 세 가지는 그 문장만 같은 뜻의 평범한 문장으로 바꾼다. 억지 위트는 그 문장을 지운다. 지울 때 앞뒤 문장이 어색해지면 짧게 이어 준다.",
             "그 문장만 같은 뜻의 업무 문서 표준어 문장으로 바꾼다. 문장을 지우지 않는다. 맞닿은, 갈래, 몫, 칸, 틀처럼 사물이나 신체를 빌린 어휘도 비유로 본다.")
)

#: Trailing `[[...]]` markers (WATCH, ASK, ...) are cut off before correction
#: and put back after, so the corrector never sees or mangles them.
_TAIL_TAG_RE = re.compile(r"(?:\n[ \t]*\[\[[^\]\n]*\]\][ \t]*)+\s*$")
_KEEP_TOKEN_RE = re.compile(r"<[@#!][^>]+>|https?://\S+|```")
_MAX_INPUT_CHARS = 6000


def is_chat_channel(config: ChannelConfig | None) -> bool:
    """Chat channel = `chat: active` or `mode: chat`, as bot.py is_chat_channel.
    An unregistered channel is a work channel."""
    if config is None:
        return False
    return config.chat == "active" or config.mode == "chat"


def figurative_accept(original: str, revised: str) -> bool:
    """Whether a correction can replace the original. Rejects an empty
    result, a length outside 0.5-1.5x, and any mention, link or code fence
    that disappeared, changed or was added."""
    if not revised or not revised.strip():
        return False
    ratio = len(revised) / max(len(original), 1)
    if not 0.5 <= ratio <= 1.5:
        return False
    return sorted(_KEEP_TOKEN_RE.findall(original)) == sorted(_KEEP_TOKEN_RE.findall(revised))


class FigurativeCorrector(Protocol):
    def __call__(self, text: str, *, work: bool) -> Mapping[str, Any] | None:
        """The corrector's JSON verdict, or None when the check could not run."""


class ModelFigurativeCorrector:
    """Runs a nested `claude -p` with no tools, MCP servers or setting sources,
    like `ModelClaimVerifier`. The API key is dropped from the environment so
    the call uses the CLI login as bot.py did. `runner` is injectable."""

    def __init__(
        self,
        *,
        model: str = "sonnet",
        timeout: float = 45,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
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

    def __call__(self, text: str, *, work: bool) -> Mapping[str, Any] | None:
        exe = self._executable or shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
        system = FIGURATIVE_CHECK_SYSTEM_WORK if work else FIGURATIVE_CHECK_SYSTEM
        try:
            proc = self._runner(
                [exe, "-p", "--model", self.model, "--tools", "", "--strict-mcp-config",
                 "--mcp-config", '{"mcpServers":{}}', "--setting-sources", "",
                 "--no-session-persistence", "--system-prompt", system],
                input="<reply>\n" + text[:_MAX_INPUT_CHARS] + "\n</reply>",
                capture_output=True, text=True, timeout=self.timeout, env=self._env, cwd="/tmp",
            )
        except Exception:  # noqa: BLE001 - a failed check never blocks the reply
            return None
        out = proc.stdout or ""
        start, end = out.find("{"), out.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            verdict = json.loads(out[start : end + 1])
        except ValueError:
            return None
        return verdict if isinstance(verdict, dict) else None


class FigurativeGuard(OutputGuard):
    """Applies the corrector before posting. Off unless `figurative_check`."""

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

    def apply(self, body: str, ctx: GuardContext) -> GuardResult:
        unchanged = GuardResult(body=body, changed=False)
        if not self._enabled or not (body or "").strip() or is_silent(body):
            return unchanged
        chat = self._is_chat(ctx.channel)
        if not chat and len(body) < self._min_chars:
            return unchanged
        m = _TAIL_TAG_RE.search(body)
        main, tail = (body[: m.start()], body[m.start():]) if m else (body, "")
        if not main.strip():
            return unchanged
        verdict = self._corrector(main, work=not chat)
        if not verdict or not verdict.get("changed"):
            return unchanged
        revised = str(verdict.get("text") or "").strip("\n")
        if not figurative_accept(main.strip("\n"), revised):
            return GuardResult(body=body, changed=False, detail={"rejected": True})
        fixes = [f for f in (verdict.get("fixes") or []) if isinstance(f, dict)][:10]
        return GuardResult(
            body=revised + tail, changed=True,
            detail={"mode": "chat" if chat else "work", "fixes": fixes},
        )
