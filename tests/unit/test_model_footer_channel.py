"""채널별 모델 푸터 끄기 (channels.json model_footer). 2026-11-17 오너 지시."""

from slack_cli_agent.config.channel import ChannelConfig


def test_기본값은_푸터를_붙인다() -> None:
    assert ChannelConfig.from_dict("C1", {"name": "a"}).model_footer is True


def test_false면_끈다() -> None:
    assert ChannelConfig.from_dict("C1", {"name": "a", "model_footer": False}).model_footer is False
