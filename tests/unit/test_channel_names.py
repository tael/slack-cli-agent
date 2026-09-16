"""ChannelNameResolver 시험.

원본 `bot.py` 의 `fetch_channel_name` 에 해당한다. 채널 설정에 이름이 있으면
그것을 쓰고, 없을 때만 슬랙에 조회하며, 조회 결과는 캐시한다.
"""

from __future__ import annotations

from typing import Any

from slack_cli_agent.slack.channel_names import ChannelNameResolver


class _FakeSlackClient:
    def __init__(self, names: dict[str, str] | None = None) -> None:
        self._names = names or {}
        self.call_count = 0

    def conversations_info(self, *, channel: str) -> dict[str, Any]:
        self.call_count += 1
        if channel not in self._names:
            raise RuntimeError("채널 조회 실패")
        return {"channel": {"name": self._names[channel]}}


def _configured(table: dict[str, str]):
    def lookup(channel: str) -> str:
        return table.get(channel, "")

    return lookup


def test_설정에_이름이_있으면_슬랙에_조회하지_않는다() -> None:
    client = _FakeSlackClient({"C1": "슬랙쪽"})
    resolver = ChannelNameResolver(client, _configured({"C1": "설정쪽"}))

    assert resolver.resolve("C1") == "설정쪽"
    assert client.call_count == 0


def test_설정에_없으면_슬랙에서_이름을_가져온다() -> None:
    client = _FakeSlackClient({"C1": "일반"})
    resolver = ChannelNameResolver(client, _configured({}))

    assert resolver.resolve("C1") == "일반"


def test_두_번째_조회는_캐시에서_나온다() -> None:
    client = _FakeSlackClient({"C1": "일반"})
    resolver = ChannelNameResolver(client, _configured({}))

    assert resolver.resolve("C1") == "일반"
    assert resolver.resolve("C1") == "일반"
    assert client.call_count == 1


def test_조회에_실패하면_빈_문자열이고_다시_조회하지_않는다() -> None:
    client = _FakeSlackClient({})
    resolver = ChannelNameResolver(client, _configured({}))

    assert resolver.resolve("C1") == ""
    assert resolver.resolve("C1") == ""
    assert client.call_count == 1


def test_이름_없는_응답은_빈_문자열로_본다() -> None:
    class _NoName:
        call_count = 0

        def conversations_info(self, *, channel: str) -> dict[str, Any]:
            return {"channel": {}}

    resolver = ChannelNameResolver(_NoName(), _configured({}))

    assert resolver.resolve("C1") == ""


def test_빈_채널_ID_는_조회하지_않는다() -> None:
    client = _FakeSlackClient({"C1": "일반"})
    resolver = ChannelNameResolver(client, _configured({}))

    assert resolver.resolve("") == ""
    assert client.call_count == 0


def test_호출_가능하다() -> None:
    client = _FakeSlackClient({"C1": "일반"})
    resolver = ChannelNameResolver(client, _configured({}))

    assert resolver("C1") == "일반"
