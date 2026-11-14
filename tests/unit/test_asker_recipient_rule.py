"""AskerSection 이 수신자 표식 규칙과 짝 봇 안내를 싣는지 본다 (bot.py RECIPIENT_RULE 대응)."""

from slack_cli_agent.auth.principal import Principal, TrustLevel
from slack_cli_agent.prompt.sections import RECIPIENT_RULE, AskerSection


class _Lib:
    def text(self, name, keep_slots=()):
        return ""


class _Ctx:
    def __init__(self, owner, bot=False):
        self.is_owner = owner
        self.asker_is_bot = bot
        self.asker_name = "누구"
        self.asker_id = "U1"
        # The section reads principal.delegated to pick the bot note (A7).
        self.principal = Principal(user_id="U1", channel="C1", trust=TrustLevel.GENERAL,
                                   is_direct_message=False)

    def _library_or_raise(self):
        return _Lib()


def test_소유자_턴에_수신자_규칙과_짝_봇이_실린다():
    out = AskerSection(peer_bot_ids=frozenset({"U0PEER"})).render(_Ctx(True))
    assert "[[RECIPIENT:ASKER]]" in out
    assert "<@U0PEER>" in out
    assert out.endswith(RECIPIENT_RULE)


def test_다른_사람_턴에도_수신자_규칙이_실린다():
    out = AskerSection().render(_Ctx(False))
    assert "[[RECIPIENT:OWNER]]" in out
    assert "짝 봇" not in out


def test_짝_봇이_없으면_짝_봇_줄을_싣지_않는다():
    out = AskerSection().render(_Ctx(True))
    assert "짝 봇" not in out
    assert "[[RECIPIENT:BOT:" in out
