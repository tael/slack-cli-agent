"""응답 아카이브.

원본 `mametchi-slack-bot/bot.py` 의 `archive_response()` 와 `RESPONSE_DIR` 을
이식한 것이다. 감사 로그가 처리 사실만 남기는 것과 달리 여기에는 응답 본문을
그대로 둔다. 학습 배치가 이 기록을 근거로 제안을 만든다.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path

_THREAD_TS_PATTERN = re.compile(r"- 스레드 : (\d+\.\d+)")


class ResponseArchive:
    """봇이 낸 응답을 채널별 날짜 파일에 남기고 되읽는다."""

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
        """응답 하나를 `<root>/<channel_slug>/<YYYY-MM-DD>.md` 에 이어 붙인다."""
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
        """그날 채널별 기록 전문. 읽기 실패한 채널 하나가 나머지를 막지 않는다."""
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
        """기록 본문에서 스레드 타임스탬프를 중복 없이 등장 순서대로 뽑는다."""
        return tuple(dict.fromkeys(_THREAD_TS_PATTERN.findall(text)))
