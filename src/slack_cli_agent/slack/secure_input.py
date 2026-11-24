"""Secure input: a button opens a modal, and the submitted value goes to the
keychain instead of the thread.

A password or one-time code typed as a reply stays in the channel history.
A modal submission reaches only the app (Socket Mode `interactive` request),
so the value never becomes a message. The button carries the target name and
an expiry and nothing secret; the value is held in memory just long enough
to hand to `security`, and is never logged, audited or passed on.

Optional: runs only when the profile sets `secure_input` true.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

log = logging.getLogger(__name__)

ACTION_OPEN = "secure_input_open"
CALLBACK_ID = "secure_input_submit"
BLOCK_ID = "secure_input_block"
INPUT_ACTION_ID = "secure_input_value"
KEYCHAIN_ACCOUNT = "value"
DEFAULT_TTL_SEC = 600

_SECURITY = "/usr/bin/security"
_TARGET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def valid_target(target: str) -> bool:
    return bool(_TARGET_RE.match(target))


def keychain_service(target: str) -> str:
    return f"shinji/{target}"


def save_to_keychain(target: str, value: str) -> None:
    """Stores `value` as the keychain item shinji/<target>.

    The value is sent on the stdin of `security -i`, so it is in no process
    argument list (`ps` shows arguments). `-X` (hex) would hide it from the
    shell too, but `find-generic-password -w` then reads it back as hex, so
    the value is quoted instead.
    """
    if not valid_target(target):
        raise ValueError("대상 이름 형식이 맞지 않는다")
    quoted = '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    command = (
        f"add-generic-password -s {keychain_service(target)} -a {KEYCHAIN_ACCOUNT} "
        f"-w {quoted} -U\n"
    )
    done = subprocess.run(
        [_SECURITY, "-i"], input=command, capture_output=True, text=True, timeout=15
    )
    # `security -i` exits 0 even when a command fails and prints "returned N".
    reply = (done.stdout or "") + (done.stderr or "")
    if done.returncode != 0 or "returned" in reply:
        raise RuntimeError("키체인 저장 실패")


def read_from_keychain(target: str) -> str:
    """Reads shinji/<target> back as it was typed.

    `find-generic-password -w` prints data that is not ASCII as hex, and a
    plain value such as "123456" looks the same as hex, so `-w` cannot tell
    them apart. `-g` prints `password: 0x<hex>  "<text>"` for non-ASCII data
    and `password: "<text>"` otherwise; that form is parsed here.
    """
    if not valid_target(target):
        raise ValueError("대상 이름 형식이 맞지 않는다")
    done = subprocess.run(
        [_SECURITY, "find-generic-password", "-s", keychain_service(target),
         "-a", KEYCHAIN_ACCOUNT, "-g"],
        capture_output=True, text=True, timeout=15,
    )
    if done.returncode != 0:
        raise RuntimeError("키체인 항목을 읽지 못했다")
    for line in (done.stderr or "").splitlines():
        if line.startswith("password: "):
            body = line[len("password: "):]
            if body.startswith("0x"):
                return bytes.fromhex(body[2:].split()[0]).decode("utf-8")
            return body[1:-1] if body.startswith('"') and body.endswith('"') else body
    raise RuntimeError("키체인 항목에 값이 없다")


def request_blocks(target: str, expires_at: float) -> list[dict[str, Any]]:
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*보안 입력 요청* : `{target}`\n버튼을 누르면 입력창이 열림. 입력 값은 채팅에 남지 않음",
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": ACTION_OPEN,
                    "text": {"type": "plain_text", "text": "입력창 열기"},
                    "style": "primary",
                    "value": f"{target}|{int(expires_at)}",
                }
            ],
        },
    ]


def post_request(
    client: Any,
    channel: str,
    thread_ts: str,
    target: str,
    *,
    now: Callable[[], float] = time.time,
    ttl_sec: int = DEFAULT_TTL_SEC,
) -> str:
    """Posts the button message and returns its ts."""
    if not valid_target(target):
        raise ValueError("대상 이름은 영문·숫자·점·밑줄·하이픈 64자 이하")
    expires_at = now() + ttl_sec
    kwargs: dict[str, Any] = {
        "channel": channel,
        "text": f"보안 입력 요청 : {target}",
        "blocks": request_blocks(target, expires_at),
    }
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    response = client.chat_postMessage(**kwargs)
    return str(response.get("ts") or "")


class SecureInput:
    def __init__(
        self,
        client: Any,
        owner_user_id: str,
        *,
        save: Callable[[str, str], None] = save_to_keychain,
        now: Callable[[], float] = time.time,
        on_saved: Callable[[dict[str, Any]], None] | None = None,
        bot_user_id: Callable[[], str] = lambda: "",
    ) -> None:
        self._client = client
        self._owner = owner_user_id
        self._save = save
        self._now = now
        # Called with a synthetic app_mention event (the owner asking the bot
        # to carry on) once a value is saved. Without it the bot stays silent
        # after a submission, because a modal submit is not a message.
        self._on_saved = on_saved
        self._bot_user_id = bot_user_id

    def handle(self, payload: Mapping[str, Any]) -> dict[str, Any] | None:
        """One interactive payload. Returns an ack payload for view_submission
        (validation errors), otherwise None. Never raises, and never logs
        the payload: it carries the submitted value."""
        kind = str(payload.get("type") or "")
        try:
            if kind == "block_actions":
                self._open_modal(payload)
                return None
            if kind == "view_submission":
                return self._submit(payload)
        except Exception:
            log.exception("보안 입력 처리 실패 : type=%s", kind)
            if kind == "view_submission":
                return self._error("처리 중 오류가 났음. 다시 시도 필요")
        return None

    def _is_owner(self, payload: Mapping[str, Any]) -> bool:
        user = str((payload.get("user") or {}).get("id") or "")
        return bool(self._owner) and user == self._owner

    @staticmethod
    def _error(message: str) -> dict[str, Any]:
        return {"response_action": "errors", "errors": {BLOCK_ID: message}}

    def _open_modal(self, payload: Mapping[str, Any]) -> None:
        action = next(
            (a for a in payload.get("actions") or [] if a.get("action_id") == ACTION_OPEN), None
        )
        if action is None:
            return
        if not self._is_owner(payload):
            log.info("보안 입력 버튼 : 소유자 아님 user=%s", (payload.get("user") or {}).get("id"))
            return
        target, _, expires = str(action.get("value") or "").partition("|")
        channel = str((payload.get("channel") or {}).get("id") or "")
        message = payload.get("message") or {}
        user = str((payload.get("user") or {}).get("id") or "")
        if not valid_target(target) or not expires.isdigit() or self._now() > int(expires):
            if channel and user:
                self._client.chat_postEphemeral(
                    channel=channel, user=user,
                    text="보안 입력 요청이 만료됐음. 새로 요청 필요",
                )
            return
        meta = {
            "target": target,
            "exp": int(expires),
            "channel": channel,
            "ts": str(message.get("ts") or ""),
            "thread_ts": str(message.get("thread_ts") or message.get("ts") or ""),
        }
        self._client.views_open(
            trigger_id=payload.get("trigger_id"),
            view={
                "type": "modal",
                "callback_id": CALLBACK_ID,
                "private_metadata": json.dumps(meta, ensure_ascii=False),
                "title": {"type": "plain_text", "text": "보안 입력"},
                "submit": {"type": "plain_text", "text": "저장"},
                "close": {"type": "plain_text", "text": "취소"},
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"대상 : `{target}`\n저장 위치 : 키체인. 입력 중 글자는 화면에 그대로 보임",
                        },
                    },
                    {
                        "type": "input",
                        "block_id": BLOCK_ID,
                        "label": {"type": "plain_text", "text": "값"},
                        "element": {
                            "type": "plain_text_input",
                            "action_id": INPUT_ACTION_ID,
                            "focus_on_load": True,
                            "max_length": 3000,
                        },
                    },
                ],
            },
        )

    def _submit(self, payload: Mapping[str, Any]) -> dict[str, Any] | None:
        view = payload.get("view") or {}
        if view.get("callback_id") != CALLBACK_ID:
            return None
        if not self._is_owner(payload):
            return self._error("소유자만 입력할 수 있음")
        try:
            meta = json.loads(str(view.get("private_metadata") or "{}"))
        except ValueError:
            return self._error("요청 정보를 읽지 못했음. 새로 요청 필요")
        target = str(meta.get("target") or "")
        if not valid_target(target) or self._now() > float(meta.get("exp") or 0):
            return self._error("요청이 만료됐음. 새로 요청 필요")
        field = ((view.get("state") or {}).get("values") or {}).get(BLOCK_ID, {})
        value = str((field.get(INPUT_ACTION_ID) or {}).get("value") or "")
        if not value or "\n" in value or "\r" in value:
            return self._error("한 줄 값이 필요함")
        try:
            self._save(target, value)
        except Exception:
            log.exception("보안 입력 저장 실패 : target=%s", target)
            return self._error("키체인 저장에 실패했음")
        finally:
            value = ""
        log.info("보안 입력 저장 : target=%s", target)
        self._notify_saved(meta, target)
        return None

    def _notify_saved(self, meta: Mapping[str, Any], target: str) -> None:
        channel = str(meta.get("channel") or "")
        if not channel:
            return
        text = f"입력 받음 : `{target}` (키체인 {keychain_service(target)})"
        thread_ts = str(meta.get("thread_ts") or "")
        posted_ts = ""
        try:
            if meta.get("ts"):
                self._client.chat_update(
                    channel=channel, ts=meta["ts"], text=text,
                    blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
                )
            response = self._client.chat_postMessage(
                channel=channel, thread_ts=thread_ts or None, text=text
            )
            posted_ts = str(response.get("ts") or "")
        except Exception:
            log.exception("보안 입력 완료 알림 실패 : target=%s", target)
        if self._on_saved is not None and posted_ts:
            event = {
                "type": "app_mention",
                "user": self._owner,
                "channel": channel,
                "ts": posted_ts,
                "event_ts": posted_ts,
                "thread_ts": thread_ts or posted_ts,
                "text": (
                    f"<@{self._bot_user_id()}> 보안 입력 `{target}` 저장 완료. "
                    f"키체인 {keychain_service(target)} 값을 확인하고 하던 작업을 이어서 진행. "
                    "값은 출력하지 않음"
                ),
            }
            # Off the socket thread: the submit ack is due within 3 seconds.
            threading.Thread(
                target=self._run_on_saved, args=(event,), name="secure-input-saved", daemon=True
            ).start()

    def _run_on_saved(self, event: dict[str, Any]) -> None:
        try:
            assert self._on_saved is not None
            self._on_saved(event)
        except Exception:
            log.exception("보안 입력 후속 처리 실패")
