"""반응 수집 — 응답 스레드에서 사람의 말을 모은다.

원본 learn.py 의 collect_reactions() 를 이식한 것이다. 판정 규칙은 원본과
같다: 채널마다 그날 기록 본문에서 스레드 ts 를 뽑아 각 스레드를 읽고,
`bot_id` 가 없고 그 앞에 봇 메시지가 하나라도 있는 메시지만 반응으로 센다.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from slack_cli_agent.learning.reactions import ReactionCollector


class FakeThreads:
    """HistoryReader 대역. read_thread 호출을 기록하고 정해 둔 결과를 낸다.

    key 는 (channel_id, thread_ts). 그 key 가 raise_on 에 있으면 예외를 낸다.
    """

    def __init__(
        self,
        by_key: Mapping[tuple[str, str], list[Mapping[str, Any]]],
        raise_on: frozenset[tuple[str, str]] = frozenset(),
    ) -> None:
        self._by_key = by_key
        self._raise_on = raise_on
        self.calls: list[tuple[str, str, int]] = []

    def read_history(
        self, channel: str, oldest: float, limit: int
    ) -> list[Mapping[str, Any]] | None:
        raise NotImplementedError("이 시험에서는 안 쓴다")

    def read_thread(
        self, channel: str, thread_ts: str, limit: int
    ) -> list[Mapping[str, Any]]:
        self.calls.append((channel, thread_ts, limit))
        if (channel, thread_ts) in self._raise_on:
            raise RuntimeError("조회 실패")
        return list(self._by_key.get((channel, thread_ts), []))


def make_collector(
    day_map: Mapping[str, str],
    thread_map: Mapping[tuple[str, str], list[Mapping[str, Any]]],
    *,
    raise_on: frozenset[tuple[str, str]] = frozenset(),
    channel_ids: Mapping[str, str] | None = None,
    limit: int = 50,
    ts_by_text: Mapping[str, Sequence[str]] | None = None,
) -> tuple[Callable[[], Mapping[str, object]], FakeThreads]:
    channel_ids = channel_ids or {}
    ts_by_text = ts_by_text or {}
    threads = FakeThreads(thread_map, raise_on=raise_on)
    collector = ReactionCollector(
        threads=threads,
        thread_timestamps=lambda text: tuple(ts_by_text.get(text, ())),
        channel_id_of=lambda name: channel_ids.get(name),
        limit=limit,
    )
    # 그날 본문은 배치가 이미 읽어 넘긴다. 시험에서도 같은 자료를 묶어 둔다.
    return lambda: collector.collect(day_map), threads


class TestReactionCollector:
    def test_봇_응답_뒤_사람_메시지를_반응으로_담는다(self) -> None:
        collect, _ = make_collector(
            day_map={"공지": "- 스레드 : 111.222"},
            thread_map={
                ("C1", "111.222"): [
                    {"bot_id": "B1", "text": "봇 응답"},
                    {"text": "고마워요"},
                ]
            },
            channel_ids={"공지": "C1"},
            ts_by_text={"- 스레드 : 111.222": ("111.222",)},
        )
        result = collect()
        assert result == {
            "공지": ({"channel": "공지", "thread": "111.222", "text": "고마워요"},)
        }

    def test_봇_메시지보다_앞에_봇_메시지가_없으면_제외한다(self) -> None:
        collect, _ = make_collector(
            day_map={"공지": "본문"},
            thread_map={
                ("C1", "111.222"): [
                    {"text": "봇이 답하기 전에 남긴 말"},
                    {"bot_id": "B1", "text": "봇 응답"},
                ]
            },
            channel_ids={"공지": "C1"},
            ts_by_text={"본문": ("111.222",)},
        )
        result = collect()
        assert result == {}

    def test_bot_id_있는_메시지는_반응이_아니다(self) -> None:
        collect, _ = make_collector(
            day_map={"공지": "본문"},
            thread_map={
                ("C1", "111.222"): [
                    {"bot_id": "B1", "text": "첫 응답"},
                    {"bot_id": "B2", "text": "다른 봇도 말했다"},
                ]
            },
            channel_ids={"공지": "C1"},
            ts_by_text={"본문": ("111.222",)},
        )
        result = collect()
        assert result == {}

    def test_공백만_있는_본문은_제외한다(self) -> None:
        collect, _ = make_collector(
            day_map={"공지": "본문"},
            thread_map={
                ("C1", "111.222"): [
                    {"bot_id": "B1", "text": "봇 응답"},
                    {"text": "   "},
                ]
            },
            channel_ids={"공지": "C1"},
            ts_by_text={"본문": ("111.222",)},
        )
        result = collect()
        assert result == {}

    def test_본문을_천_자로_자른다(self) -> None:
        long_text = "가" * 1500
        collect, _ = make_collector(
            day_map={"공지": "본문"},
            thread_map={
                ("C1", "111.222"): [
                    {"bot_id": "B1", "text": "봇 응답"},
                    {"text": long_text},
                ]
            },
            channel_ids={"공지": "C1"},
            ts_by_text={"본문": ("111.222",)},
        )
        result = collect()
        assert len(result["공지"][0]["text"]) == 1000

    def test_채널_ID_를_모르면_건너뛴다(self) -> None:
        collect, threads = make_collector(
            day_map={"모르는채널": "본문"},
            thread_map={},
            channel_ids={},
            ts_by_text={"본문": ("111.222",)},
        )
        result = collect()
        assert result == {}
        assert threads.calls == []

    def test_스레드_조회_실패는_그_스레드만_건너뛰고_계속한다(self) -> None:
        collect, _ = make_collector(
            day_map={"공지": "본문"},
            thread_map={
                ("C1", "222.222"): [
                    {"bot_id": "B1", "text": "봇 응답"},
                    {"text": "이건 잡힌다"},
                ]
            },
            raise_on=frozenset({("C1", "111.222")}),
            channel_ids={"공지": "C1"},
            ts_by_text={"본문": ("111.222", "222.222")},
        )
        result = collect()
        assert result == {
            "공지": ({"channel": "공지", "thread": "222.222", "text": "이건 잡힌다"},)
        }

    def test_반응이_없는_채널은_키를_안_넣는다(self) -> None:
        collect, _ = make_collector(
            day_map={"공지": "본문", "잡담": "본문2"},
            thread_map={
                ("C1", "111.222"): [{"bot_id": "B1", "text": "봇 응답"}],
                ("C2", "333.333"): [
                    {"bot_id": "B1", "text": "봇 응답"},
                    {"text": "반응"},
                ],
            },
            channel_ids={"공지": "C1", "잡담": "C2"},
            ts_by_text={"본문": ("111.222",), "본문2": ("333.333",)},
        )
        result = collect()
        assert set(result.keys()) == {"잡담"}

    def test_limit_이_스레드_조회에_전달된다(self) -> None:
        collect, threads = make_collector(
            day_map={"공지": "본문"},
            thread_map={("C1", "111.222"): []},
            channel_ids={"공지": "C1"},
            ts_by_text={"본문": ("111.222",)},
            limit=7,
        )
        collect()
        assert threads.calls == [("C1", "111.222", 7)]
