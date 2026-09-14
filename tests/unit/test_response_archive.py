"""ResponseArchive 시험.

원본 `mametchi-slack-bot/bot.py` 의 `archive_response()` 를 이식한 것이다.
확인할 것:
  - 기록 형식이 원본과 글자 그대로 같다(제목·질문자·스레드·소요·질문·응답 절)
  - 같은 파일에 이어 붙인다(덮어쓰지 않는다)
  - `read_day()` 는 채널별 파일 전문을 돌려주고, 없으면 빈 매핑이다
  - 읽기 실패한 파일 하나가 나머지 채널을 못 읽게 만들지 않는다
  - `thread_timestamps()` 는 중복을 없애고 등장 순서를 지킨다
  - 실제 시계 대신 주입한 clock 만 쓴다
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from slack_cli_agent.observability.response_archive import ResponseArchive

KST = timezone(timedelta(hours=9))


def _clock_at(stamp: datetime):
    return lambda: stamp


def test_record가_원본과_같은_형식으로_기록한다(tmp_path: Path) -> None:
    archive = ResponseArchive(tmp_path, clock=_clock_at(datetime(2026, 9, 14, 15, 30, tzinfo=KST)))

    path = archive.record(
        channel_slug="dm",
        user="U123",
        thread_ts="1700000000.000100",
        question="질문 내용",
        body="응답 내용",
        ok=True,
        elapsed_sec=12.3,
        turns=4,
    )

    assert path == tmp_path / "dm" / "2026-09-14.md"
    text = path.read_text()
    assert text == (
        "\n## 15:30 KST · 성공\n\n"
        "- 질문자 : U123\n"
        "- 스레드 : 1700000000.000100\n"
        "- 소요 : 12.3초, 4턴\n\n"
        "### 질문\n\n질문 내용\n\n"
        "### 응답\n\n응답 내용\n"
    )


def test_record가_실패를_실패로_표시한다(tmp_path: Path) -> None:
    archive = ResponseArchive(tmp_path, clock=_clock_at(datetime(2026, 9, 14, 9, 5, tzinfo=KST)))

    path = archive.record(
        channel_slug="general",
        user="U999",
        thread_ts="1700000001.000200",
        question="q",
        body="b",
        ok=False,
        elapsed_sec=1.0,
        turns=None,
    )

    text = path.read_text()
    assert "· 실패" in text
    assert "- 소요 : 1.0초, None턴" in text


def test_record가_같은_날_같은_파일에_이어붙인다(tmp_path: Path) -> None:
    archive = ResponseArchive(tmp_path, clock=_clock_at(datetime(2026, 9, 14, 10, 0, tzinfo=KST)))

    archive.record(
        channel_slug="dm", user="U1", thread_ts="1.1", question="첫번째", body="첫 응답",
        ok=True, elapsed_sec=1.0, turns=1,
    )
    archive.record(
        channel_slug="dm", user="U2", thread_ts="2.2", question="두번째", body="두 응답",
        ok=True, elapsed_sec=2.0, turns=2,
    )

    text = (tmp_path / "dm" / "2026-09-14.md").read_text()
    assert text.count("### 질문") == 2
    assert "첫번째" in text
    assert "두번째" in text


def test_read_day가_채널별_전문을_돌려준다(tmp_path: Path) -> None:
    archive = ResponseArchive(tmp_path, clock=_clock_at(datetime(2026, 9, 14, 10, 0, tzinfo=KST)))
    archive.record(
        channel_slug="dm", user="U1", thread_ts="1.1", question="q1", body="b1",
        ok=True, elapsed_sec=1.0, turns=1,
    )
    archive.record(
        channel_slug="general", user="U2", thread_ts="2.2", question="q2", body="b2",
        ok=True, elapsed_sec=1.0, turns=1,
    )

    result = archive.read_day("2026-09-14")

    assert set(result) == {"dm", "general"}
    assert "q1" in result["dm"]
    assert "q2" in result["general"]


def test_read_day는_디렉터리가_없으면_빈_매핑이다(tmp_path: Path) -> None:
    archive = ResponseArchive(tmp_path, clock=_clock_at(datetime(2026, 9, 14, 10, 0, tzinfo=KST)))

    assert archive.read_day("2026-09-14") == {}


def test_read_day는_그날_파일이_없는_채널을_빈_매핑으로_돌려준다(tmp_path: Path) -> None:
    archive = ResponseArchive(tmp_path, clock=_clock_at(datetime(2026, 9, 14, 10, 0, tzinfo=KST)))
    archive.record(
        channel_slug="dm", user="U1", thread_ts="1.1", question="q1", body="b1",
        ok=True, elapsed_sec=1.0, turns=1,
    )

    assert archive.read_day("2026-09-15") == {}


def test_read_day는_읽기_실패한_파일이_있어도_나머지를_읽는다(tmp_path: Path) -> None:
    archive = ResponseArchive(tmp_path, clock=_clock_at(datetime(2026, 9, 14, 10, 0, tzinfo=KST)))
    archive.record(
        channel_slug="dm", user="U1", thread_ts="1.1", question="q1", body="b1",
        ok=True, elapsed_sec=1.0, turns=1,
    )
    # "broken" 채널의 그날 파일 자리에 디렉터리를 둬 읽기를 실패시킨다.
    broken_dir = tmp_path / "broken" / "2026-09-14.md"
    broken_dir.mkdir(parents=True)

    result = archive.read_day("2026-09-14")

    assert "dm" in result
    assert "q1" in result["dm"]
    assert "broken" not in result


def test_thread_timestamps가_중복없이_등장순서대로_뽑는다() -> None:
    text = (
        "## 10:00 KST · 성공\n\n- 스레드 : 1700000000.000100\n\n"
        "## 10:05 KST · 성공\n\n- 스레드 : 1700000001.000200\n\n"
        "## 10:10 KST · 성공\n\n- 스레드 : 1700000000.000100\n"
    )

    result = ResponseArchive.thread_timestamps(text)

    assert result == ("1700000000.000100", "1700000001.000200")


def test_thread_timestamps는_스레드_표시가_없으면_빈_튜플이다() -> None:
    assert ResponseArchive.thread_timestamps("아무 내용도 없다") == ()


def test_clock에_기본값이_없다() -> None:
    with pytest.raises(TypeError):
        ResponseArchive(Path("/tmp/whatever"))  # type: ignore[call-arg]
