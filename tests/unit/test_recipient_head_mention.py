from slack_cli_agent.guard.base import GuardContext
from slack_cli_agent.guard.mentions import (
    BotMentionGuard, RecipientResolver, leading_recipient_bot,
)

BOTS = {"A": "데이전트", "B": "마메치"}


def _run(body, recipient_tag, asker=""):
    is_bot = lambda u: u in BOTS
    guard = BotMentionGuard(is_bot=is_bot, display_name=lambda u: BOTS[u])
    full = f"{body}\n{recipient_tag}"
    rid = leading_recipient_bot(full, asker, is_bot)
    out = guard.apply(full, GuardContext(channel="C", asker_id=asker, recipient_bot_id=rid)).body
    return RecipientResolver(is_bot).resolve(out, asker)[0]


def test_same_id_head_kept_no_duplicate_name():
    assert _run("<@A> 데이전트 시험의 기준임.", "[[RECIPIENT:BOT:A]]") == "<@A> 데이전트 시험의 기준임."


def test_other_bot_head_still_swapped():
    out = _run("<@B> 시험임.", "[[RECIPIENT:BOT:A]]")
    assert out == "<@A> 마메치 시험임."


def test_no_head_mention_gets_one():
    assert _run("데이전트 시험의 기준임.", "[[RECIPIENT:BOT:A]]") == "<@A> 데이전트 시험의 기준임."


def test_mid_sentence_mention_still_swapped():
    out = _run("<@A> 결과는 <@A> 에게 전달함.", "[[RECIPIENT:BOT:A]]")
    assert out == "<@A> 결과는 데이전트 에게 전달함."
