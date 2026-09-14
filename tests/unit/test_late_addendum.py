"""LateAddendumChecker / ThreadConsumption 단위 시험.

원본 bot.py 의 `late_thread_addendum`, `late_addendum_prompt`,
`mark_thread_consumed`, `_thread_consumed` 를 포팅한 것을 검증한다.
"""

from __future__ import annotations

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.observability.notices import NoticeCatalog
from slack_cli_agent.slack.late_addendum import (
    LateAddendumChecker,
    ThreadConsumption,
    late_addendum_prompt,
)


class FakeHistory:
    """HistoryReader 계약(reliability.ports)을 만족하는 대역."""

    def __init__(self, thread_msgs=None, channel_msgs=None):
        self.thread_msgs = thread_msgs if thread_msgs is not None else []
        self.channel_msgs = channel_msgs if channel_msgs is not None else []
        self.read_history_calls = []
        self.read_thread_calls = []

    def read_history(self, channel, oldest, limit):
        self.read_history_calls.append((channel, oldest, limit))
        return self.channel_msgs

    def read_thread(self, channel, thread_ts, limit):
        self.read_thread_calls.append((channel, thread_ts, limit))
        return self.thread_msgs


def make_checker(history, owner_user_id="", owner_display_name=""):
    return LateAddendumChecker(
        history=history,
        notices=NoticeCatalog(),
        name_resolver=lambda user_id: f"이름({user_id})",
        settings=RuntimeSettings(),
        owner_user_id=owner_user_id,
        owner_display_name=owner_display_name,
    )


class TestThreadConsumption:
    def test_mark_then_read(self):
        c = ThreadConsumption()
        c.mark("T1", "100.000001")
        assert c.consumed_ts("T1") == 100.000001

    def test_unmarked_thread_defaults_to_zero(self):
        c = ThreadConsumption()
        assert c.consumed_ts("없는스레드") == 0.0

    def test_monotonic_does_not_go_backwards(self):
        c = ThreadConsumption()
        c.mark("T1", "200.0")
        c.mark("T1", "100.0")
        assert c.consumed_ts("T1") == 200.0

    def test_mark_with_falsy_ts_is_noop(self):
        c = ThreadConsumption()
        c.mark("T1", None)
        c.mark("T1", "")
        c.mark("T1", 0)
        assert c.consumed_ts("T1") == 0.0

    def test_forget_removes_entry(self):
        c = ThreadConsumption()
        c.mark("T1", "150.0")
        c.forget("T1")
        assert c.consumed_ts("T1") == 0.0

    def test_threads_are_independent(self):
        c = ThreadConsumption()
        c.mark("T1", "100.0")
        c.mark("T2", "999.0")
        assert c.consumed_ts("T1") == 100.0
        assert c.consumed_ts("T2") == 999.0


class TestLateAddendumCheckerNoNewMessages:
    def test_no_messages_returns_empty(self):
        history = FakeHistory(thread_msgs=[])
        checker = make_checker(history)
        addendum, latest = checker.check("C1", "T1", "100.0", scope="thread")
        assert addendum == ""
        assert latest is None

    def test_only_old_messages_returns_empty(self):
        history = FakeHistory(thread_msgs=[
            {"ts": "50.0", "user": "U1", "text": "그 전 말"},
        ])
        checker = make_checker(history)
        addendum, latest = checker.check("C1", "T1", "100.0", scope="thread")
        assert addendum == ""
        assert latest is None

    def test_history_unavailable_returns_empty(self):
        """read_history 가 None(판정 불가)을 주면 새 말이 없는 것처럼 조용히 끝낸다."""
        class UnavailableHistory:
            def read_history(self, channel, oldest, limit):
                return None

            def read_thread(self, channel, thread_ts, limit):
                return []

        checker = make_checker(UnavailableHistory())
        addendum, latest = checker.check("C1", "T1", "100.0", scope="channel")
        assert addendum == ""
        assert latest is None


class TestLateAddendumCheckerNewMessages:
    def test_new_message_after_ts_is_captured(self):
        history = FakeHistory(thread_msgs=[
            {"ts": "100.0", "user": "U1", "text": "원래 물음"},
            {"ts": "150.5", "user": "U2", "text": "그 사이 새로 단 말"},
        ])
        checker = make_checker(history)
        addendum, latest = checker.check("C1", "T1", "100.0", scope="thread")
        assert "그 사이 새로 단 말" in addendum
        assert "원래 물음" not in addendum
        assert latest == "150.5"

    def test_multiple_new_messages_all_captured_in_order(self):
        history = FakeHistory(thread_msgs=[
            {"ts": "100.0", "user": "U1", "text": "원래 물음"},
            {"ts": "110.0", "user": "U2", "text": "첫 번째 추가"},
            {"ts": "120.0", "user": "U3", "text": "두 번째 추가"},
        ])
        checker = make_checker(history)
        addendum, latest = checker.check("C1", "T1", "100.0", scope="thread")
        assert addendum.index("첫 번째 추가") < addendum.index("두 번째 추가")
        assert latest == "120.0"

    def test_bot_messages_are_skipped(self):
        history = FakeHistory(thread_msgs=[
            {"ts": "110.0", "bot_id": "B1", "text": "봇이 남긴 말"},
        ])
        checker = make_checker(history)
        addendum, latest = checker.check("C1", "T1", "100.0", scope="thread")
        assert addendum == ""
        assert latest is None

    def test_subtype_messages_are_skipped(self):
        history = FakeHistory(thread_msgs=[
            {"ts": "110.0", "subtype": "channel_join", "user": "U1", "text": "들어옴"},
        ])
        checker = make_checker(history)
        addendum, latest = checker.check("C1", "T1", "100.0", scope="thread")
        assert addendum == ""
        assert latest is None

    def test_notice_messages_are_skipped(self):
        notices = NoticeCatalog()
        notice_text = notices.render("busy")
        history = FakeHistory(thread_msgs=[
            {"ts": "110.0", "user": "U1", "text": notice_text},
        ])
        checker = LateAddendumChecker(
            history=history,
            notices=notices,
            name_resolver=lambda user_id: user_id,
            settings=RuntimeSettings(),
        )
        addendum, latest = checker.check("C1", "T1", "100.0", scope="thread")
        assert addendum == ""
        assert latest is None

    def test_blank_text_is_skipped(self):
        history = FakeHistory(thread_msgs=[
            {"ts": "110.0", "user": "U1", "text": "   "},
        ])
        checker = make_checker(history)
        addendum, latest = checker.check("C1", "T1", "100.0", scope="thread")
        assert addendum == ""
        assert latest is None

    def test_owner_display_name_used_for_owner(self):
        history = FakeHistory(thread_msgs=[
            {"ts": "110.0", "user": "UOWNER", "text": "주인이 남긴 말"},
        ])
        checker = make_checker(
            history, owner_user_id="UOWNER", owner_display_name="김태일"
        )
        addendum, _ = checker.check("C1", "T1", "100.0", scope="thread")
        assert "김태일" in addendum

    def test_channel_scope_uses_read_history_port(self):
        history = FakeHistory(channel_msgs=[
            {"ts": "150.0", "user": "U1", "text": "채널에 새로 단 말"},
        ])
        checker = make_checker(history)
        addendum, latest = checker.check("C1", "T1", "100.0", scope="channel")
        assert "채널에 새로 단 말" in addendum
        assert latest == "150.0"
        assert history.read_history_calls
        called_channel, called_oldest, called_limit = history.read_history_calls[0]
        assert called_channel == "C1"
        assert called_oldest == 100.0
        assert called_limit == 40

    def test_thread_scope_uses_read_thread_port(self):
        history = FakeHistory(thread_msgs=[])
        checker = make_checker(history)
        checker.check("C1", "T1", "100.0", scope="thread")
        assert history.read_thread_calls
        called_channel, called_thread_ts, called_limit = history.read_thread_calls[0]
        assert called_channel == "C1"
        assert called_thread_ts == "T1"
        assert called_limit == 40


class TestLateAddendumPrompt:
    def test_prompt_contains_addendum_text(self):
        prompt = late_addendum_prompt("[10:00:00 누군가]\n새로 물은 말")
        assert "새로 물은 말" in prompt

    def test_prompt_states_not_yet_posted(self):
        prompt = late_addendum_prompt("아무 내용")
        assert "아직" in prompt and ("올리지 않았" in prompt or "슬랙" in prompt)

    def test_prompt_instructs_to_keep_prior_answer_content(self):
        prompt = late_addendum_prompt("아무 내용")
        assert "빠짐없이" in prompt or "그대로 다시" in prompt
