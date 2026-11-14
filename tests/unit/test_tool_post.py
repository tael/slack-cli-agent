"""파일 게시 도구가 올린 메시지에 최종 답을 합친다 (2026-11-06 답 두 개 문제)."""

from __future__ import annotations

from typing import Any

from slack_cli_agent.slack.tool_post import SlackToolPostMerger

SELF_BOT = "B_SELF"


class FakeClient:
    def __init__(self, messages: list[dict[str, Any]], fail: bool = False) -> None:
        self.messages = messages
        self.fail = fail

    def conversations_replies(self, **_: Any) -> dict[str, Any]:
        if self.fail:
            raise RuntimeError("boom")
        return {"messages": self.messages}


def _merger(messages, *, fail_read=False, fail_update=False):
    updates: list[tuple] = []
    audits: list[dict] = []

    def update(channel, ts, text, rich):
        if fail_update:
            raise RuntimeError("cant_update_message")
        updates.append((channel, ts, text, rich))

    m = SlackToolPostMerger(
        FakeClient(messages, fail=fail_read),
        is_self=lambda msg: msg.get("bot_id") == SELF_BOT,
        update=update,
        audit=lambda **kw: audits.append(kw),
    )
    return m, updates, audits


PARENT = {"ts": "1000.000001", "user": "U_ASKER", "text": "CSV로 줘"}


def test_이번_턴에_자기가_올린_파일_메시지에_답을_덮어쓴다() -> None:
    tool_post = {"ts": "1010.000100", "bot_id": SELF_BOT, "files": [{"id": "F1"}],
                 "text": "slack_thread_replies로 확인했습니다"}
    m, updates, audits = _merger([PARENT, tool_post])
    ts = m.merge("C1", "1000.000001", 1005.0, "총 163건입니다", True)
    assert ts == "1010.000100"
    assert updates == [("C1", "1010.000100", "총 163건입니다", True)]
    assert audits[0]["kind"] == "tool_post_merged"


def test_파일_없는_자기_메시지는_대상이_아니다() -> None:
    card = {"ts": "1010.000100", "bot_id": SELF_BOT, "text": "작업 중"}
    m, updates, _ = _merger([PARENT, card])
    assert m.merge("C1", "1000.000001", 1005.0, "답", True) is None
    assert updates == []


def test_다른_봇의_파일_메시지는_대상이_아니다() -> None:
    other = {"ts": "1010.000100", "bot_id": "B_OTHER", "files": [{"id": "F1"}]}
    m, updates, _ = _merger([PARENT, other])
    assert m.merge("C1", "1000.000001", 1005.0, "답", True) is None
    assert updates == []


def test_턴_시작_전에_올린_파일은_대상이_아니다() -> None:
    old = {"ts": "900.000100", "bot_id": SELF_BOT, "files": [{"id": "F1"}]}
    m, updates, _ = _merger([PARENT, old])
    assert m.merge("C1", "1000.000001", 1005.0, "답", True) is None
    assert updates == []


def test_여러_개면_가장_최근_것에_합친다() -> None:
    a = {"ts": "1010.000100", "bot_id": SELF_BOT, "files": [{"id": "F1"}]}
    b = {"ts": "1020.000100", "bot_id": SELF_BOT, "files": [{"id": "F2"}]}
    m, _, _ = _merger([PARENT, a, b])
    assert m.merge("C1", "1000.000001", 1005.0, "답", False) == "1020.000100"


def test_스레드_부모는_대상이_아니다() -> None:
    parent = {"ts": "1010.000001", "bot_id": SELF_BOT, "files": [{"id": "F1"}]}
    m, _, _ = _merger([parent])
    assert m.merge("C1", "1010.000001", 1005.0, "답", True) is None


def test_조회가_실패하면_None으로_평소대로_올리게_한다() -> None:
    m, _, _ = _merger([], fail_read=True)
    assert m.merge("C1", "1000.000001", 1005.0, "답", True) is None


def test_덮어쓰기가_실패하면_None으로_따로_올리게_하고_기록한다() -> None:
    tool_post = {"ts": "1010.000100", "bot_id": SELF_BOT, "files": [{"id": "F1"}]}
    m, _, audits = _merger([PARENT, tool_post], fail_update=True)
    assert m.merge("C1", "1000.000001", 1005.0, "답", True) is None
    assert audits[0]["kind"] == "tool_post_merge_failed"
