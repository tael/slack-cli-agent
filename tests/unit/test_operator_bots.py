"""operator_bots : 채널이 이름으로 지정한 봇만 operator 로 본다 (2026-11-10)."""
from slack_cli_agent.config.channel import ChannelConfig, is_channel_operator


def test_bot_counts_only_when_named_in_operator_bots():
    cfg = ChannelConfig.from_dict("C1", {"operators": ["*"], "operator_bots": ["UBOT"]})
    assert is_channel_operator(cfg, "UBOT", is_bot=True)
    assert not is_channel_operator(cfg, "UOTHER", is_bot=True)


def test_star_and_operators_never_cover_bots():
    cfg = ChannelConfig.from_dict("C1", {"operators": ["*", "UBOT"]})
    assert not is_channel_operator(cfg, "UBOT", is_bot=True)
    assert is_channel_operator(cfg, "UHUMAN", is_bot=False)


def test_controller_named_only_in_operator_bots_is_operator():
    # 컨트롤러 봇은 is_bot=False 로 판정된다. operators 없이 operator_bots 에만
    # 있어도 operator 다 (2026-11-15 private-taeil 구치파치 → read 등급 결함).
    cfg = ChannelConfig.from_dict("C1", {"operator_bots": ["UCTRL"], "operator_full": True})
    assert is_channel_operator(cfg, "UCTRL", is_bot=False)
    assert not is_channel_operator(cfg, "UOTHER", is_bot=False)
