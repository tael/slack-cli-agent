"""재기동 결과 보고(supervisor)를 admin 채널로 보낸다. 2026-11-13 김태일 지시."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import slack_sdk

from slack_cli_agent import cli
from slack_cli_agent.config.profile import Profile


class _Resolver:
    def bot_token(self) -> str:
        return "xoxb-test"


def _profile(tmp_path: Path, **overrides: Any) -> Profile:
    data: dict[str, Any] = {
        "name": "testbot",
        "display_name": "시험봇",
        "primary_engine": {"type": "claude", "binary": "/bin/sh", "model": "m"},
        "state_dir": str(tmp_path / "state"),
        "owner_user_id": "U_OWNER",
        "owner_dm": "D_OWNER",
        "troubleshoot_channel": "C_ADMIN",
    }
    data.update(overrides)
    return Profile.from_dict(data)


class _Sent(list[str]):
    def __init__(self) -> None:
        super().__init__()
        self.fail: set[str] = set()


@pytest.fixture
def posted(monkeypatch: pytest.MonkeyPatch) -> _Sent:
    sent = _Sent()
    fail = sent.fail

    class _Client:
        def __init__(self, token: str) -> None:
            pass

        def chat_postMessage(self, channel: str, text: str) -> None:
            if channel in fail:
                raise RuntimeError("not_in_channel")
            sent.append(channel)

    monkeypatch.setattr(slack_sdk, "WebClient", _Client)
    monkeypatch.setattr(cli, "resolver_for", lambda profile: _Resolver())
    return sent


def test_admin_채널로_보낸다(tmp_path: Path, posted: _Sent) -> None:
    cli._admin_sender(_profile(tmp_path))("재기동 완료")
    assert posted == ["C_ADMIN"]


def test_admin_채널이_비었으면_소유자_DM(tmp_path: Path, posted: _Sent) -> None:
    cli._admin_sender(_profile(tmp_path, troubleshoot_channel=""))("재기동 완료")
    assert posted == ["D_OWNER"]


def test_admin_발송이_실패하면_소유자_DM(tmp_path: Path, posted: _Sent) -> None:
    posted.fail.add("C_ADMIN")
    cli._admin_sender(_profile(tmp_path))("재기동 완료")
    assert posted == ["D_OWNER"]
