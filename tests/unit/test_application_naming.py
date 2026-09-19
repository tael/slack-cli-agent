"""사용자 이름 판정이 조립에서 실제로 연결됐는가.

UserNamer 단위 시험은 판정 자체만 본다. 조립에 안 꽂으면 그 시험은 전부
통과하는데 운영에서는 같은 봇이 한 대화록에 두 이름으로 나온다 (sca-inw8).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from slack_cli_agent.config.profile import Profile
from slack_cli_agent.core.application import Application
from slack_cli_agent.slack.names import UserNamer

BOT_USER_ID = "U_BOT"


class 가짜클라이언트:
    """auth_test 는 봇 자신의 id 를, users_info 는 프로필과 다른 이름을 낸다.

    두 경로가 갈리는 것이 이 결함이므로 대역도 갈라 둬야 한다.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def auth_test(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("auth_test")
        return {"ok": True, "user_id": BOT_USER_ID}

    def users_info(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("users_info")
        return {"ok": True, "user": {"profile": {"real_name": "Shinji Ikari Bot"}}}

    def __getattr__(self, name: str) -> Any:
        def call(**kwargs: Any) -> dict[str, Any]:
            self.calls.append(name)
            return {"ok": True}

        return call


@pytest.fixture
def client() -> 가짜클라이언트:
    return 가짜클라이언트()


@pytest.fixture
def app(tmp_path: Path, client: 가짜클라이언트) -> Application:
    binary = tmp_path / "bin" / "fake-engine"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    profile = Profile.from_dict({
        "name": "testbot",
        "display_name": "신지",
        "primary_engine": {"type": "claude", "binary": str(binary), "model": "model-a"},
        "state_dir": str(tmp_path / "state"),
        "owner_user_id": "U_OWNER",
    })
    return Application(profile, client)


class Test이름판정주입:
    def test_판정은_UserNamer_다(self, app: Application) -> None:
        assert isinstance(app.user_namer(), UserNamer)

    def test_같은_판정_객체를_돌려준다(self, app: Application) -> None:
        assert app.user_namer() is app.user_namer()

    def test_봇_자신은_프로필_표시명으로_나온다(self, app: Application) -> None:
        판정 = app.user_namer()
        assert 판정.name_of(BOT_USER_ID) == "신지"
        assert 판정.name_of("U_SOMEONE") == "Shinji Ikari Bot"

    def test_조립만으로는_슬랙을_부르지_않는다(
        self, app: Application, client: 가짜클라이언트
    ) -> None:
        app.user_namer()
        assert client.calls == []

    def test_대화록_참가자_이번턴_본문이_같은_판정을_쓴다(self, app: Application) -> None:
        판정 = app.user_namer()
        assert app._transcript_builder()._called._name is 판정
        assert app._transcript_builder()._speaker._name_resolver is 판정
        assert app._late_addendum()._called._name is 판정
        assert app._participants()._name_resolver is 판정
        assert app.pipeline()._mentions._name is 판정
