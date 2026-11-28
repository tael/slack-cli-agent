"""Renders the threads a message links to as a note appended to the request.

This goes in the user prompt, not the system prompt: codex omits the system
prompt when resuming a session, so a per-turn piece of evidence placed there
would reach the model on the first request of a thread and never again. The
original bot appends it to the tagged prompt for the same reason.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

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

_TRUNCATED = (
    "\n\n----- 링크된 스레드 노트가 길어 여기서 잘렸다 -----\n"
    "뒤에 더 있을 수 있다. 잘린 뒤의 내용을 아는 것처럼 쓰지 않는다.\n"
    "필요하면 그 링크를 직접 조회한다."
)

_EMPTY = (
    "이 링크는 열렸으나 옮길 메시지가 없다.\n"
    "조회 실패가 아니다. 없는 내용을 있는 것처럼 쓰지 않는다."
)


class LinkedThreadNote:
    def __init__(self, reader: LinkedThreadReader, *, max_chars: int = 0) -> None:
        self._reader = reader
        self._max_chars = max_chars

    def of(
        self,
        text: str,
        self_channel: str = "",
        attachments: Sequence[Mapping[str, Any]] = (),
        *,
        as_owner: bool = False,
    ) -> str:
        blocks = [
            self._block(linked)
            for linked in self._reader.of(text, self_channel, attachments, as_owner=as_owner)
        ]
        if not blocks:
            return ""
        note = _INTRO + "\n\n" + "\n\n".join(blocks)
        note = self._capped(note)
        log.info("링크된 스레드 %d자를 먼저 읽어 붙인다", len(note))
        return note

    def _capped(self, note: str) -> str:
        """노트 합계가 상한을 넘으면 앞부분만 남기고 자른 사실을 적는다.

        링크 하나의 본문만 잘리고 합계는 무제한이라, 링크 여러 개가 걸린 턴은
        노트만으로 수만 자가 들어갔다. 앞부분을 남기는 것은 링크가 본문에 적힌
        순서대로 실리기 때문이다.
        """
        limit = self._max_chars
        if limit <= 0 or len(note) <= limit:
            return note
        return note[:limit] + _TRUNCATED

    def _block(self, linked: LinkedThread) -> str:
        if linked.body:
            return (
                f"----- 링크된 스레드 : {linked.name} -----\n\n{linked.body}\n\n"
                "----- 여기까지가 링크된 스레드다 -----"
            )
        if linked.read_ok:
            return f"----- 링크된 스레드 : {linked.name} (옮길 메시지가 없다) -----\n\n{_EMPTY}"
        return f"----- 링크된 스레드 : {linked.name} (읽지 못했다) -----\n\n{_UNREAD}"
