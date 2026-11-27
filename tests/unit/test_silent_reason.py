"""침묵 사유별 이모지 (2026-11-27 김태일 지시).

[[침묵]] 뒤에 사유 단어를 붙이면 그 사유의 이모지를 단다. 모르는 사유나
사유 없음은 zipper_mouth_face 그대로다. 새 이모지도 처리 완료로 세야
재기동 catchup 이 침묵한 메시지를 다시 집지 않는다.
"""

from __future__ import annotations

from slack_cli_agent.prompt.sections import SILENT_REASONS, is_silent, silent_reason
from slack_cli_agent.reliability import catchup
from slack_cli_agent.slack import reactions
from slack_cli_agent.slack.reactions import (
    DONE_EMOJI,
    SILENT_MARK_EMOJI,
    SILENT_REASON_EMOJI,
    ReactionMarker,
    silent_emoji,
)


class FakeClient:
    def __init__(self) -> None:
        self.added: list[tuple[str, str, str]] = []

    def reactions_add(self, *, channel: str, timestamp: str, name: str) -> None:
        self.added.append((channel, timestamp, name))

    def reactions_remove(self, *, channel: str, timestamp: str, name: str) -> None:
        raise RuntimeError("no_reaction")


def test_사유를_읽는다() -> None:
    assert silent_reason("[[침묵]] 감사") == "감사"
    assert silent_reason("[[침묵]] 동의") == "동의"
    assert silent_reason("[[침묵]] 타인\n") == "타인"


def test_사유가_없으면_빈값() -> None:
    assert silent_reason("[[침묵]]") == ""
    assert silent_reason("답변입니다") == ""


def test_사유를_붙여도_침묵이다() -> None:
    assert is_silent("[[침묵]] 마무리")


def test_사유별_이모지() -> None:
    assert silent_emoji("감사") == "bow"
    assert silent_emoji("동의") == "+1"
    assert silent_emoji("확인") == "ok_hand"
    assert silent_emoji("타인") == "speak_no_evil"
    assert silent_emoji("마무리") == "wave"


def test_모르는_사유는_기본_이모지() -> None:
    assert silent_emoji("") == SILENT_MARK_EMOJI
    assert silent_emoji("아무말") == SILENT_MARK_EMOJI


def test_새_이모지도_완료로_센다() -> None:
    for name in SILENT_REASON_EMOJI.values():
        assert name in DONE_EMOJI
        msg = {"reactions": [{"name": name, "users": ["UBOT"]}]}
        assert catchup.already_handled(msg, "UBOT")


def test_사람이_단_같은_이모지는_완료가_아니다() -> None:
    for name in SILENT_REASON_EMOJI.values():
        msg = {"reactions": [{"name": name, "users": ["UHUMAN"]}]}
        assert not catchup.already_handled(msg, "UBOT")
        assert not catchup.already_handled(msg)


def test_사유_목록이_양쪽에서_같다() -> None:
    assert set(SILENT_REASONS) == set(SILENT_REASON_EMOJI)


def test_본문_중간의_사유_붙은_침묵도_침묵이다() -> None:
    assert is_silent("설명입니다.\n\n[[침묵]] 감사")
    assert is_silent("[[RECIPIENT:ASKER]]\n[[침묵]] 마무리")
    assert not is_silent("설명입니다.\n[[침묵]] 표식을 쓰면 된다")


def test_mark_silent에_사유를_주면_그_이모지를_단다() -> None:
    client = FakeClient()
    ReactionMarker(client).mark_silent("C1", "1.0", reason="동의")
    assert client.added == [("C1", "1.0", "+1")]


def test_mark_silent_사유_없으면_zipper() -> None:
    client = FakeClient()
    ReactionMarker(client).mark_silent("C1", "1.0")
    assert client.added == [("C1", "1.0", reactions.SILENT_MARK_EMOJI)]
