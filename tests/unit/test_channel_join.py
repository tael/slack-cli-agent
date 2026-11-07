"""등록 채널 자동 참여 (G3).

마메치는 기동할 때 channels.json 에 `"join": true` 인 채널에 conversations.join
을 불렀다 (bot.py join_registered_channels). 봇을 채널에서 내보내도 재기동하면
다시 들어간다. 이관 전 코어에는 이 호출이 0건이라 data-support 처럼 join 으로
들어간 채널은 한 번 빠지면 돌아오지 않았다.
"""

from __future__ import annotations

from typing import Any

from slack_cli_agent.config.channel import ChannelConfig
from slack_cli_agent.observability.audit import BASELINE_KIND_VALUES, IncidentKind
from slack_cli_agent.reliability.channel_join import join_registered_channels, wants_join


class _Client:
    def __init__(self, fail: set[str] | None = None) -> None:
        self.joined: list[str] = []
        self._fail = fail or set()

    def conversations_join(self, *, channel: str) -> dict[str, Any]:
        self.joined.append(channel)
        if channel in self._fail:
            raise RuntimeError("method_not_supported_for_channel_type")
        return {"ok": True}


class _Channels:
    def __init__(self, entries: dict[str, ChannelConfig]) -> None:
        self._entries = entries

    def all(self) -> dict[str, ChannelConfig]:
        return dict(self._entries)


def _config(cid: str, **extra: Any) -> ChannelConfig:
    return ChannelConfig.from_dict(cid, {"name": cid.lower(), **extra})


class _Audit:
    def __init__(self) -> None:
        self.rows: list[tuple[str, dict[str, Any]]] = []

    def record(self, kind: str, **fields: Any) -> None:
        self.rows.append((str(kind), fields))


def test_join_참인_채널에만_참여한다() -> None:
    client = _Client()
    channels = _Channels({
        "C1": _config("C1", join=True),
        "C2": _config("C2"),
        "C3": _config("C3", join=False),
    })

    report = join_registered_channels(client, channels, audit=_Audit().record)

    assert client.joined == ["C1"]
    assert report.joined == ["C1"]
    assert report.failed == {}


def test_문자열이나_숫자는_참여로_읽지_않는다() -> None:
    """"false" 같은 문자열이 참으로 읽혀 원치 않는 채널에 들어가지 않게 한다."""
    assert wants_join(_config("C1", join=True))
    assert not wants_join(_config("C1", join="false"))
    assert not wants_join(_config("C1", join=1))
    assert not wants_join(None)


def test_한_채널이_실패해도_나머지는_참여하고_감사에_남긴다() -> None:
    client = _Client(fail={"C1"})
    audit = _Audit()
    channels = _Channels({"C1": _config("C1", join=True), "C2": _config("C2", join=True)})

    report = join_registered_channels(client, channels, audit=audit.record)

    assert client.joined == ["C1", "C2"]
    assert report.joined == ["C2"]
    assert "method_not_supported" in report.failed["C1"]
    kinds = {(kind, fields["channel"]) for kind, fields in audit.rows}
    assert kinds == {
        (IncidentKind.CHANNEL_JOIN_FAILED.value, "C1"),
        (IncidentKind.CHANNEL_JOIN.value, "C2"),
    }


def test_성공은_사고로_집계하지_않고_실패는_사고로_집계한다() -> None:
    assert IncidentKind.CHANNEL_JOIN.value in BASELINE_KIND_VALUES
    assert IncidentKind.CHANNEL_JOIN_FAILED.value not in BASELINE_KIND_VALUES


def test_DM_채널은_join_이_있어도_부르지_않는다() -> None:
    client = _Client()
    channels = _Channels({"D1": _config("D1", join=True)})

    report = join_registered_channels(client, channels, audit=_Audit().record)

    assert client.joined == []
    assert report.joined == []
