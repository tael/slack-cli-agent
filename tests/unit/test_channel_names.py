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


class Test캐시만료:
    """실패를 프로세스 수명 내내 들고 있으면 잠깐의 rate limit 이 그 채널을
    ID 표기로 굳힌다. 이름이 바뀐 경우도 같다 (sca-ops).
    """

    def _resolver(self, client: _FakeSlackClient, 시각: list[float]) -> ChannelNameResolver:
        return ChannelNameResolver(
            client,
            _configured({}),
            now=lambda: 시각[0],
            ttl_sec=3600.0,
            failure_ttl_sec=60.0,
        )

    def test_실패는_짧은_만료_뒤_다시_조회한다(self) -> None:
        시각 = [0.0]
        client = _FakeSlackClient({})
        resolver = self._resolver(client, 시각)

        assert resolver.resolve("C1") == ""
        시각[0] = 61.0
        client._names["C1"] = "일반"

        assert resolver.resolve("C1") == "일반"
        assert client.call_count == 2

    def test_실패는_만료_전에는_다시_조회하지_않는다(self) -> None:
        시각 = [0.0]
        client = _FakeSlackClient({})
        resolver = self._resolver(client, 시각)

        resolver.resolve("C1")
        시각[0] = 59.0
        resolver.resolve("C1")

        assert client.call_count == 1

    def test_성공은_긴_만료_뒤_다시_조회한다(self) -> None:
        시각 = [0.0]
        client = _FakeSlackClient({"C1": "옛이름"})
        resolver = self._resolver(client, 시각)

        assert resolver.resolve("C1") == "옛이름"
        시각[0] = 3601.0
        client._names["C1"] = "새이름"

        assert resolver.resolve("C1") == "새이름"

    def test_성공은_만료_전에는_다시_조회하지_않는다(self) -> None:
        시각 = [0.0]
        client = _FakeSlackClient({"C1": "일반"})
        resolver = self._resolver(client, 시각)

        resolver.resolve("C1")
        시각[0] = 3599.0
        resolver.resolve("C1")

        assert client.call_count == 1
