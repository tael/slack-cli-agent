"""Renders the threads a message links to as a note appended to the request.

This goes in the user prompt, not the system prompt: codex omits the system
prompt when resuming a session, so a per-turn piece of evidence placed there
would reach the model on the first request of a thread and never again. The
original bot appends it to the tagged prompt for the same reason.
"""

from __future__ import annotations

import logging

from ..slack.linked_threads import LinkedThread, LinkedThreadReader

log = logging.getLogger(__name__)

_INTRO = (
    "아래는 지금 말에 걸린 슬랙 링크를 먼저 열어 본 것이다.\n"
    "근거로 쓴다. 여기 없는 내용을 그 스레드에 있는 것처럼 쓰지 않는다."
)

_UNREAD = (
    "이 링크를 코드가 대신 읽는 데 실패했다.\n"
    "답하기 전에 직접 열어 본다.\n"
    "열어 보지 못했으면 그 스레드에 무엇이 적혀 있는지 서술하지 않는다.\n"
    "확인하지 못했다고 밝힌다."
)

_EMPTY = (
    "이 링크는 열렸으나 옮길 메시지가 없다.\n"
    "조회 실패가 아니다. 없는 내용을 있는 것처럼 쓰지 않는다."
)


class LinkedThreadNote:
    def __init__(self, reader: LinkedThreadReader) -> None:
        self._reader = reader

    def of(self, text: str, self_channel: str = "") -> str:
        blocks = [self._block(linked) for linked in self._reader.of(text, self_channel)]
        if not blocks:
            return ""
        note = _INTRO + "\n\n" + "\n\n".join(blocks)
        log.info("링크된 스레드 %d자를 먼저 읽어 붙인다", len(note))
        return note

    def _block(self, linked: LinkedThread) -> str:
        if linked.body:
            return (
                f"----- 링크된 스레드 : {linked.name} -----\n\n{linked.body}\n\n"
                "----- 여기까지가 링크된 스레드다 -----"
            )
        if linked.read_ok:
            return f"----- 링크된 스레드 : {linked.name} (옮길 메시지가 없다) -----\n\n{_EMPTY}"
        return f"----- 링크된 스레드 : {linked.name} (읽지 못했다) -----\n\n{_UNREAD}"
