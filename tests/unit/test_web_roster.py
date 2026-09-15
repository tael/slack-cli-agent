"""봇 명부 — 모든 봇의 한 줄 상태.

콘솔 선택줄이 이것을 쓴다. 지표 수집기는 봇 하나만 보므로 선택줄이 지표
응답을 쓰면 봇이 1개만 나온다(2026-09-15 에 실제로 그랬다).
"""

from __future__ import annotations

import json
from pathlib import Path

from slack_cli_agent.web.roster import BotRoster

NOW = 1_000_000.0


def write_profile(base: Path, name: str, state: Path, *, engine: str = "claude") -> None:
    base.mkdir(parents=True, exist_ok=True)
    (base / f"{name}.json").write_text(
        json.dumps(
            {
                "name": name,
                "display_name": f"{name} 표시명",
                "primary_engine": {"type": engine, "binary": "python3", "model": "m"},
                "owner_user_id": "U1",
                "troubleshoot_channel": "C1",
                "state_dir": str(state),
            }
        ),
        encoding="utf-8",
    )


def write_snapshot(state: Path, **fields: object) -> None:
    state.mkdir(parents=True, exist_ok=True)
    (state / "state.json").write_text(json.dumps(fields), encoding="utf-8")


class Test명부:
    def test_검색_경로의_모든_봇을_낸다(self, tmp_path: Path) -> None:
        base = tmp_path / "profiles"
        write_profile(base, "asuka", tmp_path / "a", engine="codex")
        write_profile(base, "rei", tmp_path / "r", engine="gemini")

        rows = BotRoster([base], now=lambda: NOW).rows()

        assert [r["name"] for r in rows] == ["asuka", "rei"]
        assert [r["engine"] for r in rows] == ["codex", "gemini"]

    def test_예시_프로필은_안_낸다(self, tmp_path: Path) -> None:
        base = tmp_path / "profiles"
        write_profile(base, "asuka", tmp_path / "a")
        (base / "example.example.json").write_text("{}", encoding="utf-8")

        assert [r["name"] for r in BotRoster([base], now=lambda: NOW).rows()] == ["asuka"]

    def test_최근_스냅샷이_있으면_살아있는_것으로_낸다(self, tmp_path: Path) -> None:
        base = tmp_path / "profiles"
        state = tmp_path / "a"
        write_profile(base, "asuka", state)
        write_snapshot(state, written_at=NOW - 1, pid=42, inflight=2, queued_total=3)

        (row,) = BotRoster([base], now=lambda: NOW).rows()

        assert row["available"] is True
        assert row["pid"] == 42
        assert row["inflight"] == 2

    def test_한_주기를_막_넘긴_스냅샷은_아직_살아있는_것으로_낸다(self, tmp_path: Path) -> None:
        """봇은 health_interval_sec(기본 30초)마다 스냅샷을 쓴다. 그보다
        짧은 기준을 쓰면 매 주기마다 10초씩 '상태 모름' 으로 보인다
        (2026-09-15 에 실제로 그랬다). 두 번 연속 못 쓴 것만 이상으로 본다."""
        base = tmp_path / "profiles"
        state = tmp_path / "a"
        write_profile(base, "asuka", state)
        write_snapshot(state, written_at=NOW - 35, pid=42)

        (row,) = BotRoster([base], now=lambda: NOW).rows()

        assert row["available"] is True

    def test_두_주기를_넘게_안_쓰면_상태를_모르는_것으로_낸다(self, tmp_path: Path) -> None:
        base = tmp_path / "profiles"
        state = tmp_path / "a"
        write_profile(base, "asuka", state)
        write_snapshot(state, written_at=NOW - 200, pid=42)

        (row,) = BotRoster([base], now=lambda: NOW).rows()

        assert row["available"] is False
        assert "지났다" in str(row["reason"])

    def test_한_봇의_프로필이_깨져도_나머지를_낸다(self, tmp_path: Path) -> None:
        """봇 하나가 고장 나서 콘솔 전체가 빈 화면이 되면 안 된다."""
        base = tmp_path / "profiles"
        write_profile(base, "rei", tmp_path / "r")
        (base / "asuka.json").write_text("{ 깨진 JSON", encoding="utf-8")

        rows = BotRoster([base], now=lambda: NOW).rows()

        assert [r["name"] for r in rows] == ["asuka", "rei"]
        assert rows[0]["available"] is False
        assert rows[1]["available"] is False  # 스냅샷이 없다
        assert "프로필을 읽지 못했다" in str(rows[0]["reason"])
