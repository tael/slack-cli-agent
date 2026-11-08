"""채널별 봇 왕복 한도 (channels.json peer_turn_limit). 2026-11-08 김태일 지시."""

from slack_cli_agent.config.channel import _peer_turn_limit
from slack_cli_agent.guard.mentions import PeerExchangeLimiter


def test_채널값이_없으면_기본_한도() -> None:
    lim = PeerExchangeLimiter(limit=2, limit_for=lambda ch: None)
    assert [lim.allowed("t", "C1") for _ in range(3)] == [True, True, False]


def test_0이면_한도를_끈다() -> None:
    lim = PeerExchangeLimiter(limit=2, limit_for=lambda ch: 0 if ch == "COFF" else None)
    assert all(lim.allowed("t", "COFF") for _ in range(50))
    assert [lim.allowed("u", "C1") for _ in range(3)] == [True, True, False]


def test_양수면_그_값이_한도() -> None:
    lim = PeerExchangeLimiter(limit=2, limit_for=lambda ch: 4)
    assert [lim.allowed("t", "C1") for _ in range(5)] == [True] * 4 + [False]


def test_채널을_모르면_기본_한도() -> None:
    lim = PeerExchangeLimiter(limit=1, limit_for=lambda ch: 0)
    assert [lim.allowed("t") for _ in range(2)] == [True, False]


def test_설정값_해석() -> None:
    assert _peer_turn_limit(None) is None
    assert _peer_turn_limit(0) == 0
    assert _peer_turn_limit(20) == 20
    assert _peer_turn_limit(-1) is None
    assert _peer_turn_limit("x") is None
    assert _peer_turn_limit(True) is None
