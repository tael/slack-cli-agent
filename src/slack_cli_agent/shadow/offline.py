"""Offline shadow evaluation (G5, mametchi shadow_eval.py).

Runs the shadow answer for past source questions with the same prompt and
channel guidance as the live loop, then judges it against the real thread
answer. Results go to a file only: the evaluator has no post callable, and
the source API it gets is wrapped by `read_only`, so a test run can never
write to Slack.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from typing import Any

from ..core.markers import take_recipient_tag
from ..core.timezones import KST
from ..plugin.hooks import EngineSlot
from ..slack.transcript import message_text
from .prompts import answer_prompt, offline_judge_prompt
from .service import TurnResult, blind_violations, is_question

READ_METHODS = frozenset({
    "conversations.history", "conversations.replies", "conversations.info", "chat.getPermalink",
})


def read_only(api: Callable[..., Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    """Refuses every Slack method outside READ_METHODS before calling it."""

    def call(method: str, **params: Any) -> Mapping[str, Any]:
        if method not in READ_METHODS:
            raise PermissionError(f"오프라인 평가기는 읽기 메서드만 부른다 : {method}")
        return api(method, **params)

    return call


class OfflineEvaluator:
    def __init__(
        self,
        *,
        source_api: Callable[..., Mapping[str, Any]],
        run_turn: Callable[[str, str, str], TurnResult],
        name_of: Callable[[str], str],
        bot_name: str,
        source: str,
        target: str,
        engine: EngineSlot = nullcontext,
        is_self: Callable[[Mapping[str, Any]], bool] = lambda m: False,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._api = source_api
        self._run_turn = run_turn
        self._name_of = name_of
        self._bot = bot_name
        self._source = source
        self._target = target
        self._engine = engine
        self._is_self = is_self
        self._now = now

    def questions(self, *, days: float, limit: int, only_ts: Sequence[str] = ()) -> list[Mapping[str, Any]]:
        if only_ts:
            msgs: list[Mapping[str, Any]] = []
            for ts in only_ts:
                resp = self._api("conversations.history", channel=self._source, latest=ts,
                                 inclusive="true", limit=1)
                msgs += [m for m in resp.get("messages") or [] if m.get("ts") == ts]
            return msgs
        # Only posts older than a day: the real answer needs time to arrive.
        params: dict[str, Any] = {
            "channel": self._source, "oldest": f"{self._now() - days * 86400:.6f}",
            "latest": f"{self._now() - 86400:.6f}", "limit": 200,
        }
        found: list[Mapping[str, Any]] = []
        while True:
            resp = self._api("conversations.history", **params)
            found += [m for m in resp.get("messages") or [] if is_question(m) and m.get("reply_count")]
            cursor = (resp.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
            params["cursor"] = cursor
        found.sort(key=lambda m: float(m["ts"]), reverse=True)
        return found[:limit]

    def _source_name(self) -> str:
        try:
            return str(self._api("conversations.info", channel=self._source)["channel"]["name"])
        except Exception:  # noqa: BLE001 - the id stands in for the name on any failure
            return self._source

    def _replies(self, ts: str) -> str:
        msgs = self._api("conversations.replies", channel=self._source, ts=ts, limit=100).get("messages") or []
        lines = []
        for m in msgs:
            if m.get("ts") == ts or self._is_self(m):
                continue
            who = self._name_of(str(m["user"])) if m.get("user") else m.get("username") or "봇"
            lines.append(f"[{who}] {message_text(m)}")
        return "\n".join(lines)

    def _turn(self, prompt: str, request_id: str) -> TurnResult:
        with self._engine():
            return self._run_turn(prompt, self._target, request_id)

    def evaluate(self, msg: Mapping[str, Any]) -> dict[str, Any]:
        ts = str(msg["ts"])
        asked_at = datetime.fromtimestamp(float(ts), KST)
        question = message_text(msg)
        prompt = answer_prompt(
            bot=self._bot, source_id=self._source, source_name=self._source_name(), question=question,
            author=self._name_of(str(msg["user"])) if msg.get("user") else "확인 불가",
            asked_at=asked_at, files=msg.get("files") or (),
        )
        t0 = time.time()
        result = self._turn(prompt, f"shadow-eval-{self._source}-{ts}-{uuid.uuid4().hex[:8]}")
        answer, _ = take_recipient_tag(result.body or "")
        ok = result.ok and bool(answer.strip())
        violations = blind_violations(result.tool_calls or (), self._source, ts) if ok else []
        actual = self._replies(ts)
        verdict: dict[str, Any] = {}
        if ok:
            judged = self._turn(offline_judge_prompt(bot=self._bot, question=question, answer=answer,
                                                     replies=actual[:12000]),
                                f"shadow-eval-judge-{self._source}-{ts}-{uuid.uuid4().hex[:8]}")
            raw, _ = take_recipient_tag(judged.body or "")
            try:
                parsed = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
                verdict = parsed if isinstance(parsed, dict) else {"content": "판정 실패", "raw": raw[:500]}
            except ValueError:
                verdict = {"content": "판정 실패", "raw": raw[:500]}
        return {
            "ts": ts, "asked_at": f"{asked_at:%m/%d %H:%M}", "question": question, "answer": answer,
            "ok": ok, "violations": violations, "blind_checked": result.tool_calls is not None,
            "actual": actual, "verdict": verdict, "elapsed": round(time.time() - t0),
        }

    def run(self, msgs: Sequence[Mapping[str, Any]], *, workers: int) -> list[dict[str, Any]]:
        if not msgs:
            return []
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(msgs)))) as pool:
            return list(pool.map(self.evaluate, msgs))


def write_results(results: Sequence[Mapping[str, Any]], out_dir: Path, run_id: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{run_id}.json"
    path.write_text(json.dumps(list(results), ensure_ascii=False, indent=2), encoding="utf-8")
    return path
