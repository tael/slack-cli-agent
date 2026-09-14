"""BotIdentity — 이 봇 자신의 슬랙 신원 판정.

자기 말과 다른 봇의 말을 가르는 근거를 한 곳에 모은다. 같은 판정을 여러
클래스가 각자 들고 있으면, 조립이 그중 일부에만 값을 주입해도 부품 시험은
전부 통과한다. 실제로 그렇게 됐다 — `Application`·`TranscriptBuilder`·
`EventListener` 가 같은 `_is_self` 를 각각 구현했고, 조립은 그중 하나에만
`bot_id` 를 넘겼다.
"""

from __future__ import annotations

from typing import Any

from slack_cli_agent.slack.identity import SlackBotIdentity


class FakeAuthClient:
    """`auth_test` 만 있는 최소 대역. 호출 횟수를 센다."""

    def __init__(self, result: dict[str, Any] | None = None, exc: Exception | None = None) -> None:
        self.result = result if result is not None else {"ok": True, "user_id": "U_ME", "bot_id": "B_ME"}
        self.exc = exc
        self.calls = 0

    def auth_test(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return self.result


class Test신원조회:
    def test_한번만_조회한다(self) -> None:
        """판정마다 조회하면 요청 수만큼 API 호출이 늘어난다."""
        client = FakeAuthClient()
        identity = SlackBotIdentity(client)
        identity.is_self({"bot_id": "B_ME"})
        identity.is_self({"user": "U_ME"})
        assert identity.user_id == "U_ME"
        assert identity.bot_id == "B_ME"
        assert client.calls == 1

    def test_조회_전에는_신원을_모른다(self) -> None:
        client = FakeAuthClient()
        identity = SlackBotIdentity(client)
        assert client.calls == 0
        assert identity.known is True
        assert client.calls == 1


class Test판정:
    def test_자기_bot_id_면_이봇의_말이다(self) -> None:
        identity = SlackBotIdentity(FakeAuthClient())
        assert identity.is_self({"bot_id": "B_ME"}) is True

    def test_다른봇의_말은_이봇의_말이_아니다(self) -> None:
        identity = SlackBotIdentity(FakeAuthClient())
        assert identity.is_self({"bot_id": "B_OTHER"}) is False

    def test_bot_id_가_없으면_사용자ID로_본다(self) -> None:
        identity = SlackBotIdentity(FakeAuthClient())
        assert identity.is_self({"user": "U_ME"}) is True
        assert identity.is_self({"user": "U_HUMAN"}) is False


class Test신원미확정:
    def test_신원을_모르면_어떤_봇의_말도_이봇의_말로_보지_않는다(self) -> None:
        """`bot_id` 유무로 보면 다른 봇의 답이 이 봇의 답으로 세어진다.

        오판의 두 방향 중 방어가 있는 쪽으로 기운다. 이 봇의 답을 남의 것으로
        보면 되짚기가 재등록을 시도해도 jobs 표의 유일 제약이 중복을 막지만,
        반대 방향은 막는 것이 없어 미응답 멘션이 그대로 유실된다.
        """
        identity = SlackBotIdentity(FakeAuthClient(exc=RuntimeError("조회 실패")))
        assert identity.is_self({"bot_id": "B_ANY"}) is False
        assert identity.is_self({"user": "U_HUMAN"}) is False
        assert identity.known is False

    def test_조회가_성공해도_값이_비면_모르는_것이다(self) -> None:
        """빈 값으로는 어떤 메시지도 대조할 수 없다."""
        identity = SlackBotIdentity(FakeAuthClient({"ok": True}))
        assert identity.known is False
        assert identity.is_self({"bot_id": "B_ANY"}) is False

    def test_실패_직후에는_재조회하지_않는다(self) -> None:
        """장애가 이어지는 동안 요청 수만큼 API 호출이 늘어나면 안 된다."""
        client = FakeAuthClient(exc=RuntimeError("조회 실패"))
        identity = SlackBotIdentity(client, clock=lambda: 0.0, retry_interval_sec=60.0)
        for _ in range(3):
            identity.is_self({"bot_id": "B_ANY"})
        assert client.calls == 1

    def test_실패를_영구히_캐시하지_않는다(self) -> None:
        """한 번 실패했다고 그 결과를 계속 쓰면 일시 장애가 영구 오판이 된다."""
        시각 = [0.0]
        client = FakeAuthClient(exc=RuntimeError("조회 실패"))
        identity = SlackBotIdentity(client, clock=lambda: 시각[0], retry_interval_sec=60.0)
        assert identity.is_self({"bot_id": "B_ME"}) is False

        client.exc = None
        시각[0] = 61.0
        assert identity.is_self({"bot_id": "B_ME"}) is True
        assert identity.is_self({"bot_id": "B_OTHER"}) is False
        assert client.calls == 2

    def test_성공한_뒤에는_다시_조회하지_않는다(self) -> None:
        시각 = [0.0]
        client = FakeAuthClient()
        identity = SlackBotIdentity(client, clock=lambda: 시각[0], retry_interval_sec=60.0)
        identity.is_self({"bot_id": "B_ME"})
        시각[0] = 9999.0
        identity.is_self({"bot_id": "B_ME"})
        assert client.calls == 1
