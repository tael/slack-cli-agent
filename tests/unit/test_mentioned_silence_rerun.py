"""직접 멘션 받은 말의 침묵 재실행과 현재 줄 머리 수신자 (2026-11-10)."""

from slack_cli_agent.core.pipeline import should_rerun_mentioned_silence
from slack_cli_agent.slack.mentions import CalledNames

BASE = dict(from_bot=False, unaddressed=False, direct_message=False)


def test_mentioned_silence_reruns():
    assert should_rerun_mentioned_silence("[[침묵]]", **BASE)


def test_answer_is_not_rerun():
    assert not should_rerun_mentioned_silence("확인함.", **BASE)


def test_unaddressed_silence_is_kept():
    assert not should_rerun_mentioned_silence("[[침묵]]", **{**BASE, "unaddressed": True})


def test_bot_silence_is_kept():
    assert not should_rerun_mentioned_silence("[[침묵]]", **{**BASE, "from_bot": True})


def test_dm_silence_is_kept():
    assert not should_rerun_mentioned_silence("[[침묵]]", **{**BASE, "direct_message": True})


def test_head_names_self_from_raw_text():
    names = {"USELF": "구치파치"}
    called = CalledNames(names.get, None).called_in("원래 말투가 사라졌다. <@USELF>\n조사")
    assert called == ("구치파치",)
