# Unlike the audit log, which only records that a request was handled,
# this keeps the full response body -- the training batch uses it as
# source material for suggestions.

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path

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
        with open(path, "a") as f:
            f.write(
                f"\n## {stamp} KST · {status}\n\n"
                f"- 질문자 : {user}\n"
                f"- 스레드 : {thread_ts}\n"
                f"- 소요 : {elapsed_sec:.1f}초, {turns}턴\n\n"
                f"### 질문\n\n{question}\n\n"
                f"### 응답\n\n{body}\n"
            )
        return path

    def read_day(self, day: str) -> Mapping[str, str]:
        if not self._root.is_dir():
            return {}
        result: dict[str, str] = {}
        for path in self._root.glob(f"*/{day}.md"):
            try:
                result[path.parent.name] = path.read_text()
            except OSError:
                continue
        return result

    @staticmethod
    def thread_timestamps(text: str) -> tuple[str, ...]:
        return tuple(dict.fromkeys(_THREAD_TS_PATTERN.findall(text)))
