"""ChannelRegistry.register 시험.

원본 `bot.py` 의 `register_channel` 에 해당한다. 소유자가 봇을 부른 채널을
등록하고, 이미 등록돼 있으면 새로 쓰지 않는다. 다만 이전 조회 실패로 이름
자리에 채널 ID 가 남아 있으면 실제 이름이 들어온 지금 보정한다.
"""

from __future__ import annotations

import json
from pathlib import Path

from slack_cli_agent.config.channel import ChannelRegistry


def _read(path: Path, channel: str) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))[channel]


def test_새_채널은_등록하고_True를_돌려준다(tmp_path: Path) -> None:
    path = tmp_path / "channels.json"
    registry = ChannelRegistry(path)

    assert registry.register("C1", "일반") is True
    assert _read(path, "C1")["name"] == "일반"


def test_이름이_없으면_채널ID를_이름으로_쓴다(tmp_path: Path) -> None:
    path = tmp_path / "channels.json"
    registry = ChannelRegistry(path)

    assert registry.register("C1", "") is True
    assert _read(path, "C1")["name"] == "C1"


def test_이미_등록된_채널은_False를_돌려주고_덮어쓰지_않는다(tmp_path: Path) -> None:
    path = tmp_path / "channels.json"
    path.write_text(
        json.dumps({"C1": {"name": "손으로_정한이름", "mode": "default"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    registry = ChannelRegistry(path)

    assert registry.register("C1", "다른이름") is False
    assert _read(path, "C1")["name"] == "손으로_정한이름"


def test_이름자리가_채널ID면_실제이름으로_보정한다(tmp_path: Path) -> None:
    # 이전 조회 실패로 name 에 채널 ID 가 남은 상태에서 실제 이름이 들어오면 갱신.
    path = tmp_path / "channels.json"
    path.write_text(
        json.dumps({"C1": {"name": "C1", "mode": "default"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    registry = ChannelRegistry(path)

    # 새 등록이 아니므로 False (재안내 억제) 를 돌려주되 이름은 보정한다.
    assert registry.register("C1", "일반") is False
    assert _read(path, "C1")["name"] == "일반"


def test_이름자리가_비어있으면_실제이름으로_보정한다(tmp_path: Path) -> None:
    path = tmp_path / "channels.json"
    path.write_text(
        json.dumps({"C1": {"name": "", "mode": "default"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    registry = ChannelRegistry(path)

    assert registry.register("C1", "일반") is False
    assert _read(path, "C1")["name"] == "일반"


def test_보정할_이름이_여전히_채널ID면_건드리지_않는다(tmp_path: Path) -> None:
    path = tmp_path / "channels.json"
    path.write_text(
        json.dumps({"C1": {"name": "C1", "mode": "default"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    registry = ChannelRegistry(path)

    assert registry.register("C1", "C1") is False
    assert _read(path, "C1")["name"] == "C1"
