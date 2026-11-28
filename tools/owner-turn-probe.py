#!/usr/bin/env python3
"""소유자 턴 하나를 슬랙에 글을 남기지 않고 실제 파이프라인으로 돌린다.

    ~/.<봇>/venv/bin/python tools/owner-turn-probe.py <봇이름> <본문>
    ~/.<봇>/venv/bin/python tools/owner-turn-probe.py <봇이름> <본문> --thread /tmp/t.json

--thread 파일을 주면 그 파일에 스레드(질문과 봇 답)를 남기고, 다음 실행에서 같은
파일을 주면 같은 스레드의 다음 턴으로 이어 돌린다. 승인을 기다리는 단계에
"승인" 으로 답하는 두 턴 시험에 쓴다.

test-channel-probe.py 는 다른 봇 토큰으로 멘션하므로 그 턴은 일반 사용자
턴이다. 소유자 턴에만 붙는 지침(OWNER_NOTE, SelfConfigSection)과 도구는 그
경로로 시험되지 않는다. 소유자 세션 토큰이 없는 봇도 있어 소유자로 글을 올리는
방법도 없다 (2026-11-16 아스카 모델 변경 건).

이 도구는 실행 중인 봇과 같은 프로필, 같은 상태 디렉터리, 같은 엔진으로
RequestPipeline.handle 을 부른다. 채널은 프로필의 troubleshoot_channel,
화자는 owner_user_id 다. 슬랙 쓰기 호출(chat.*, reactions.*, files.* 업로드)은
보내지 않고 기록만 해서 출력한다. 읽기 호출은 실제 슬랙으로 간다.

주의
- 엔진 턴은 실제로 돈다. 턴이 고친 파일, 남긴 apply-request 마커는 실제다.
  마커를 남기면 supervisor 가 실행 중인 봇을 재기동한다
- audit.jsonl 과 세션 기록에 이 턴이 남는다. 슬랙에 없는 ts 라 스레드 조회는 실패로 남는다
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from slack_cli_agent.config.profile import Profile  # noqa: E402
from slack_cli_agent.core.application import Application  # noqa: E402
from slack_cli_agent.core.context import RequestContext  # noqa: E402
from slack_cli_agent.slack.app_registry import read_bot_token  # noqa: E402
from slack_cli_agent.slack.credentials import resolver_for  # noqa: E402

PROFILE_DIR = REPO / "profiles"
NAME_PATTERN = re.compile(r"\A[a-z0-9_-]+\Z")

#: 이 접두사로 시작하는 메서드는 슬랙에 상태를 남긴다. 보내지 않는다.
WRITE_PREFIXES = ("chat_", "reactions_", "files_upload", "files_complete", "files_delete", "pins_", "bookmarks_")
WRITE_API = re.compile(r"\A(chat|reactions|pins|bookmarks)\.|\Afiles\.(upload|completeUpload|delete)")


class FakeResponse(dict):
    """slack_sdk SlackResponse 대신 돌려준다. ["ts"], .get, .data 로 읽힌다."""

    @property
    def data(self) -> dict[str, Any]:
        return dict(self)

    def validate(self) -> FakeResponse:
        return self


class WriteRecordingClient:
    """읽기는 실제 클라이언트로 넘기고 쓰기는 기록만 한다."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self.writes: list[tuple[str, dict[str, Any]]] = []
        self.write_times: list[float] = []
        self._seq = 0
        # The probe thread does not exist on Slack. Thread reads for its root
        # return it, or the pre-post check reads the question as deleted and withholds.
        self.thread_ts = ""
        self.messages: list[dict[str, Any]] = []
        self.bot: dict[str, Any] = {}

    def _probe_thread(self, params: dict[str, Any]) -> FakeResponse | None:
        # The pre-post check reads replies by the question's own ts, which on a
        # later turn is a reply, not the root (pipeline origin_alive).
        asked = str(params.get("ts", ""))
        if not self.thread_ts or (asked != self.thread_ts and all(m.get("ts") != asked for m in self.messages)):
            return None
        return FakeResponse(ok=True, messages=list(self.messages), has_more=False)

    def _fake(self, method: str, params: dict[str, Any]) -> FakeResponse:
        self.writes.append((method, params))
        self.write_times.append(time.time())
        self._seq += 1
        ts = f"{time.time():.6f}"
        if method in ("chat_postMessage", "chat.postMessage") and params.get("thread_ts") == self.thread_ts:
            self.messages.append({"ts": ts, "text": params.get("text", ""), "thread_ts": self.thread_ts, **self.bot})
        if method in ("chat_update", "chat.update"):
            for message in self.messages:
                if message.get("ts") == params.get("ts"):
                    message["text"] = params.get("text", message.get("text", ""))
        return FakeResponse(ok=True, ts=ts, channel=params.get("channel", ""), message={"ts": ts})

    def api_call(self, api_method: str, *args: Any, **kwargs: Any) -> Any:
        params = dict(kwargs.get("json") or kwargs.get("params") or kwargs.get("data") or {})
        if WRITE_API.match(api_method):
            return self._fake(api_method, params)
        if api_method == "conversations.replies" and (probe := self._probe_thread(params)) is not None:
            return probe
        return self._real.api_call(api_method, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._real, name)
        if name == "conversations_replies":
            return lambda *_args, **kw: self._probe_thread(kw) or attr(*_args, **kw)
        if callable(attr) and name.startswith(WRITE_PREFIXES):
            return lambda *_args, **kw: self._fake(name, kw)
        return attr


def parse_set_options(items: list[str]) -> dict[str, Any]:
    """`key=value` pairs; the value is JSON-parsed, falling back to the raw string."""
    out: dict[str, Any] = {}
    for item in items:
        key, sep, raw = item.partition("=")
        if not sep or not key:
            raise SystemExit(f"--set 은 key=value 형식이어야 한다 : {item!r}")
        try:
            out[key] = json.loads(raw)
        except json.JSONDecodeError:
            out[key] = raw
    return out


def apply_setting_overrides(profile: Profile, overrides: dict[str, Any]) -> Profile:
    if not overrides:
        return profile
    return dataclasses.replace(profile, settings_override={**profile.settings_override, **overrides})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("bot")
    ap.add_argument("text")
    ap.add_argument("--set", dest="overrides", action="append", default=[], metavar="KEY=VALUE",
                    help="프로필 settings 키를 이번 실행에만 덮어쓴다. 값은 JSON, 아니면 문자열. 반복 가능")
    ap.add_argument("--thread", type=Path, default=None,
                    help="스레드 기록 파일. 있으면 그 스레드의 다음 턴으로 돌리고, 끝나면 갱신한다")
    a = ap.parse_args()
    if not NAME_PATTERN.match(a.bot):
        raise SystemExit(f"봇 이름이 아니다 : {a.bot!r}")

    profile = apply_setting_overrides(Profile.load(a.bot, [PROFILE_DIR]), parse_set_options(a.overrides))
    if not profile.owner_user_id or not profile.troubleshoot_channel:
        raise SystemExit("프로필에 owner_user_id 와 troubleshoot_channel 이 있어야 한다")
    resolver = resolver_for(profile)
    from slack_sdk import WebClient

    # run.sh exports the token from the state dir; a bare interpreter has no such env.
    token = read_bot_token(profile.state_dir) or resolver.bot_token()
    client = WriteRecordingClient(WebClient(token=token))
    app = Application.from_profile(profile, client=client, resolver=resolver)
    # Must parse as a float: the pipeline compares thread timestamps numerically.
    ts = f"{time.time():.6f}"
    saved: dict[str, Any] = {}
    if a.thread is not None and a.thread.exists():
        saved = json.loads(a.thread.read_text(encoding="utf-8"))
    client.thread_ts = str(saved.get("thread_ts") or ts)
    client.messages = list(saved.get("messages") or [])
    with contextlib.suppress(Exception):
        auth = client.auth_test()
        client.bot = {"user": auth.get("user_id", ""), "bot_id": auth.get("bot_id", "")}
    question = {"ts": ts, "user": profile.owner_user_id, "text": a.text}
    if client.thread_ts != ts:
        question["thread_ts"] = client.thread_ts
    client.messages.append(question)
    ctx = RequestContext(
        channel=profile.troubleshoot_channel,
        user=profile.owner_user_id,
        ts=ts,
        thread_ts=client.thread_ts,
        text=a.text,
    )
    started = time.time()
    try:
        outcome = app.pipeline().handle(ctx)
    finally:
        app.close()
    print(f"결과 : {outcome!r}  ({time.time() - started:.0f}초)")
    print(f"질문 ts : {ts}  스레드 : {client.thread_ts}")
    if a.thread is not None:
        a.thread.write_text(
            json.dumps({"thread_ts": client.thread_ts, "messages": client.messages}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
    for (method, params), at in zip(client.writes, client.write_times, strict=True):
        body = params.get("text") or params.get("name") or ""
        if not body and params.get("blocks"):
            body = json.dumps(params["blocks"], ensure_ascii=False)[:400]
        print(f"--- {method} (+{at - float(ts):.1f}초)\n{body}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
