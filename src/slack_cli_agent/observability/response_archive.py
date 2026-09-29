# Unlike the audit log, which only records that a request was handled,
# this keeps the full response body -- the training batch uses it as
# source material for suggestions.

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from ..learning.ports import DayArchives

_THREAD_TS_PATTERN = re.compile(r"- 스레드 : (\d+\.\d+)")


class ResponseArchive:

    def __init__(self, root: Path, *, clock: Callable[[], datetime]) -> None:
        self._root = root
        self._clock = clock

    def record(
        self,
        *,
        channel_slug: str,
        user: str,
        thread_ts: str,
        question: str,
        body: str,
        ok: bool,
        elapsed_sec: float,
        turns: int | None,
    ) -> Path:
        now = self._clock()
        day = now.strftime("%Y-%m-%d")
        path = self._root / channel_slug / f"{day}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = now.strftime("%H:%M")
        status = "성공" if ok else "실패"
        turns_display = f"{turns}턴" if turns is not None else "턴 정보 없음"
        with open(path, "a") as f:
            f.write(
                f"\n## {stamp} KST · {status}\n\n"
                f"- 질문자 : {user}\n"
                f"- 스레드 : {thread_ts}\n"
                f"- 소요 : {elapsed_sec:.1f}초, {turns_display}\n\n"
                f"### 질문\n\n{question}\n\n"
                f"### 응답\n\n{body}\n"
            )
        return path

    def read_day(self, day: str) -> DayArchives:
        if not self._root.is_dir():
            return DayArchives()
        texts: dict[str, str] = {}
        unreadable: set[str] = set()
        for path in self._root.glob(f"*/{day}.md"):
            try:
                texts[path.parent.name] = path.read_text()
            except (OSError, UnicodeDecodeError):
                # A channel that exists but can't be read right now is not a
                # channel without history, and callers act on that difference.
                unreadable.add(path.parent.name)
        return DayArchives(texts=texts, unreadable=frozenset(unreadable))

    @staticmethod
    def thread_timestamps(text: str) -> tuple[str, ...]:
        return tuple(dict.fromkeys(_THREAD_TS_PATTERN.findall(text)))
