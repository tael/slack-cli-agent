"""A14 : 모델별 토큰·비용 집계. 감사 행의 모델별 표는 세션 누적이라 행마다
직전 행과의 차이를 그 행의 사용량으로 본다."""

from __future__ import annotations

from typing import Any

import pytest

from slack_cli_agent.observability.model_cost import rollup, row_spends


def 행(at: float, session: str, table: dict[str, tuple[int, float | None]], *,
       kind: str = "request", channel: str = "C1", run_output: int | None = None) -> dict[str, Any]:
    record: dict[str, Any] = {
        "at": at, "kind": kind, "channel": channel, "session_id": session,
        "model_usage": {
            model: {"input_tokens": 0, "output_tokens": out, "cache_creation_tokens": 0,
                    "cache_read_tokens": 0, **({"cost_usd": cost} if cost is not None else {})}
            for model, (out, cost) in table.items()
        },
    }
    if run_output is not None:
        record["usage"] = {"output_tokens": run_output, "unavailable": []}
    return record


def _day(epoch: float) -> str:
    return "2026-11-07"


class Test세션_누적값의_차이:
    def test_이어받은_세션은_직전_행을_뺀다(self) -> None:
        """2026-11-07 마메치 감사 cf194f7f 와 같은 모양 : 4573 → 14775."""
        spends = row_spends([
            행(1, "s", {"sonnet": (4573, 0.92)}),
            행(2, "s", {"sonnet": (6000, 1.2), "opus": (8775, 2.2)}),
        ])
        assert spends[0].by_model["sonnet"]["output_tokens"] == 4573
        assert spends[1].by_model["sonnet"]["output_tokens"] == 1427
        assert spends[1].by_model["sonnet"]["cost_usd"] == pytest.approx(0.28)
        assert spends[1].by_model["opus"]["output_tokens"] == 8775

    def test_합계는_마지막_누적값과_같다(self) -> None:
        records = [행(i, "s", {"sonnet": (100 * i, 0.1 * i), "opus": (50 * i, 0.3 * i)})
                   for i in range(1, 6)]
        result = rollup(row_spends(records), 0, _day)
        by = {m["model"]: m for m in result["by_model"]}
        assert by["sonnet"]["output_tokens"] == 500
        assert by["opus"]["cost_usd"] == pytest.approx(1.5)
        assert result["cost_total_usd"] == pytest.approx(2.0)

    def test_값이_줄면_새로_센_것으로_본다(self) -> None:
        spends = row_spends([행(1, "s", {"m": (900, 0.9)}), 행(2, "s", {"m": (100, 0.1)})])
        assert spends[1].by_model["m"]["output_tokens"] == 100

    def test_차이가_이번_실행_출력보다_작으면_누적이_아니다(self) -> None:
        """CLI 가 세션 계수를 복원하지 못한 경우. 차이를 쓰면 과소 집계다."""
        spends = row_spends([
            행(1, "s", {"m": (1000, 1.0)}),
            행(2, "s", {"m": (1200, 1.2)}, run_output=1200),
        ])
        assert spends[1].by_model["m"]["output_tokens"] == 1200

    def test_세션이_다르면_따로_센다(self) -> None:
        spends = row_spends([행(1, "a", {"m": (10, 0.1)}), 행(2, "b", {"m": (20, 0.2)})])
        assert [s.by_model["m"]["output_tokens"] for s in spends] == [10, 20]


class Test창과_비용:
    def test_창_밖_행은_빼기에만_쓴다(self) -> None:
        records = [행(1, "s", {"m": (100, 1.0)}), 행(10, "s", {"m": (150, 1.5)})]
        result = rollup(row_spends(records), 5, _day)
        assert result["by_model"][0]["output_tokens"] == 50
        assert result["cost_total_usd"] == pytest.approx(0.5)
        assert result["sample"] == 1

    def test_비용이_없으면_합계는_None_이다(self) -> None:
        result = rollup(row_spends([행(1, "s", {"m": (10, None)})]), 0, _day)
        assert result["cost_total_usd"] is None
        assert result["by_model"][0]["cost_usd"] is None
        assert result["by_model"][0]["output_tokens"] == 10

    def test_종류와_채널별_비용(self) -> None:
        records = [
            행(1, "a", {"sonnet": (10, 0.1)}, channel="C1"),
            행(2, "b", {"opus": (10, 0.4)}, kind="review", channel="TS"),
        ]
        result = rollup(row_spends(records), 0, _day)
        assert result["cost_by_kind"] == {"request": 0.1, "review": 0.4}
        assert result["cost_by_channel"][0] == {"channel": "TS", "usd": 0.4}
        assert result["cost_median_usd"] == pytest.approx(0.1)
        assert [m["model"] for m in result["by_model"]] == ["opus", "sonnet"]
