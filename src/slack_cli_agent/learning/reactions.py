"""응답 스레드에 달린 사람의 반응을 모은다.

원본 `mametchi-slack-bot/learn.py` 의 `collect_reactions()` 를 이식한 것이다.
원본은 파일 시스템(`RESPONSES.glob()`)과 슬랙 API(`slack("conversations.replies")`)
를 함수 안에서 직접 불렀다. 여기서는 그 두 자료를 `learning/ports.py` 의
`ResponseArchiveReader` 와 `reliability/ports.py` 의 `HistoryReader` 로 받아,
`ReactionCollector` 자체는 판정 규칙만 담당한다.

판정 규칙(원본과 동일)
  - 채널마다 그날 기록 본문에서 스레드 ts 를 뽑아 각 스레드를 읽는다
  - `bot_id` 가 있는 메시지는 반응이 아니다
  - 그 메시지보다 앞에 봇 메시지가 하나라도 있어야 반응으로 센다 —
    봇이 답한 뒤 사람이 남긴 말이 피드백이라는 것이 원본 근거다
  - 본문이 공백뿐이면 제외한다
  - 담는 형태는 ``{"channel": 채널이름, "thread": ts, "text": 본문[:1000]}`` 다

원본과 다른 점
  - 반환을 채널별 매핑으로 낸다. 원본은 평평한 목록을 내고 호출부가 다시
    걸렀다. 반응이 하나도 없는 채널은 키를 넣지 않는다
  - 채널 ID 를 모르는 채널은 건너뛴다. 스레드 조회가 예외를 내도 그 스레드만
    건너뛰고 나머지는 계속한다 — 채널 하나의 실패로 배치 전체가 멈추면 안 된다
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from slack_cli_agent.learning.ports import ResponseArchiveReader
from slack_cli_agent.reliability.ports import HistoryReader


class ReactionCollector:
    """응답 기록에서 스레드 ts 를 뽑아 각 스레드의 사람 반응을 모은다."""

    def __init__(
        self,
        archive: ResponseArchiveReader,
        *,
        threads: HistoryReader,
        thread_timestamps: Callable[[str], tuple[str, ...]],
        channel_id_of: Callable[[str], str | None],
        limit: int,
    ) -> None:
        self._archive = archive
        self._threads = threads
        self._thread_timestamps = thread_timestamps
        self._channel_id_of = channel_id_of
        self._limit = limit

    def collect(self, day: str) -> Mapping[str, tuple[Mapping[str, object], ...]]:
        result: dict[str, tuple[Mapping[str, object], ...]] = {}
        for channel_name, text in self._archive.read_day(day).items():
            channel_id = self._channel_id_of(channel_name)
            if channel_id is None:
                continue
            reactions = self._reactions_in_channel(channel_id, channel_name, text)
            if reactions:
                result[channel_name] = tuple(reactions)
        return result

    def _reactions_in_channel(
        self, channel_id: str, channel_name: str, text: str
    ) -> list[Mapping[str, object]]:
        reactions: list[Mapping[str, object]] = []
        for ts in self._thread_timestamps(text):
            try:
                messages = self._threads.read_thread(channel_id, ts, self._limit)
            except Exception:  # noqa: BLE001, S112 — 스레드 하나의 조회 실패로 배치 전체가 멎으면 안 된다
                continue
            reactions.extend(self._reactions_in_thread(channel_name, ts, messages))
        return reactions

    def _reactions_in_thread(
        self, channel_name: str, ts: str, messages: list[Mapping[str, Any]]
    ) -> list[Mapping[str, object]]:
        out: list[Mapping[str, object]] = []
        for i, message in enumerate(messages):
            if message.get("bot_id"):
                continue
            prev_bot = any(m.get("bot_id") for m in messages[:i])
            if prev_bot and (message.get("text") or "").strip():
                out.append(
                    {
                        "channel": channel_name,
                        "thread": ts,
                        "text": str(message["text"])[:1000],
                    }
                )
        return out
