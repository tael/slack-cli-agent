"""김태일 최상위 글 침묵 재실행 판정 (2026-11-08 김태일 "이 말을 무시해? 절대 용납 못해")."""

from slack_cli_agent.core.pipeline import should_rerun_owner_silence

BASE = dict(owner=True, from_bot=False, ts="1.0", thread_ts="1.0", answer_top_level=True)


def test_owner_top_level_silence_reruns():
    assert should_rerun_owner_silence("[[침묵]]", **BASE)


def test_answer_is_not_rerun():
    assert not should_rerun_owner_silence("확인했습니다.", **BASE)


def test_thread_reply_silence_is_kept():
    assert not should_rerun_owner_silence("[[침묵]]", **{**BASE, "ts": "2.0"})


def test_non_owner_silence_is_kept():
    assert not should_rerun_owner_silence("[[침묵]]", **{**BASE, "owner": False})


def test_bot_message_silence_is_kept():
    assert not should_rerun_owner_silence("[[침묵]]", **{**BASE, "from_bot": True})


def test_channel_without_top_level_duty_is_kept():
    assert not should_rerun_owner_silence("[[침묵]]", **{**BASE, "answer_top_level": False})
