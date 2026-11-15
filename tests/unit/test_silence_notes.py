"""침묵 재실행 문구. 2026-11-15 휴가 위임 거짓 보고."""

from __future__ import annotations

import pytest

from slack_cli_agent.core.pipeline import MENTIONED_SILENCE_NOTE, OWNER_TOP_LEVEL_SILENCE_NOTE


@pytest.mark.parametrize("note", [OWNER_TOP_LEVEL_SILENCE_NOTE, MENTIONED_SILENCE_NOTE])
def test_재실행_문구는_앞_초안이_게시되지_않았음을_말한다(note: str) -> None:
    assert "앞서 쓴 초안은 게시되지 않았다" in note
    assert "초안 내용을 이미 한 일로 보고하지 않는다" in note
