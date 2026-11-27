"""계획 호출 입력에서 봇이 붙인 요청 헤더를 뺀다.

2026-11-27 rnd-pe팀-private: 헤더의 WATCH 백그라운드 안내 예시
("첫째_검사 | tail -40 || status=1") 를 계획 모델이 요청으로 읽고, 그 해석을
ack 로 스레드에 올렸다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from slack_cli_agent.guard import plan_start as ps
from slack_cli_agent.prompt.sections import REQUEST_HEADER_TITLE

HEADER = (
    REQUEST_HEADER_TITLE + "\n\n"
    "[이 대화의 좌표 : channel=C1 thread_ts=1.2]\n\n"
    "백그라운드로 돌릴 명령이 있으면 아래 세 줄을 그대로 실행한다.\n"
    "        status=0\n"
    "        첫째_검사 | tail -40 || status=1\n"
    "        exit \"$status\"\n"
)
BODY = "[13:52:28 이종선 <@U5> -> 구치파치]\n정정합니다. PR 을 올렸습니다."


def test_헤더가_있으면_대화_첫_줄부터_남긴다() -> None:
    assert ps.strip_request_header(HEADER + "\n\n" + BODY) == BODY


@pytest.mark.parametrize("prompt", [BODY, "그냥 요청", HEADER])
def test_헤더가_없거나_대화_줄이_없으면_그대로다(prompt: str) -> None:
    assert ps.strip_request_header(prompt) == prompt


def test_start_가_계획_입력에_헤더를_싣지_않는다(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ps.PLAN_GATE_ENV, "1")
    monkeypatch.delenv(ps.PLAN_SYNC_ENV, raising=False)
    transcript = str(tmp_path / "s.jsonl")
    ps.start({"transcript_path": transcript, "prompt": HEADER + "\n\n" + BODY}, spawn=lambda *a, **k: None)
    written = ps.prompt_path(transcript).read_text(encoding="utf-8")
    assert "첫째_검사" not in written
    assert written == BODY


def test_복사한_헤더_제목이_원본과_같다() -> None:
    assert ps.REQUEST_HEADER_TITLE == REQUEST_HEADER_TITLE
