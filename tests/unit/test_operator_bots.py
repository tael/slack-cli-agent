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
