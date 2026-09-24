"""조립 시험 — 프로필 검색 경로 하나로 콘솔 전체가 서는가.

각 부품의 동작은 그 부품의 시험이 본다. 여기서는 부품이 실제로 이어졌는지만
본다. 부품을 다 만들어 놓고 조립에서 빠뜨리면 화면이 빈 값만 보여준다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from slack_cli_agent.web.console import WebConsole

PROFILE = {
    "name": "example",
    "display_name": "예시봇",
    "primary_engine": {"type": "claude", "binary": "python3", "model": "m"},
    "owner_user_id": "U1",
    "troubleshoot_channel": "C1",
}


def write_profile(profiles: Path, state: Path) -> None:
    profiles.mkdir(parents=True, exist_ok=True)
    (profiles / "example.json").write_text(
        json.dumps({**PROFILE, "state_dir": str(state)}), encoding="utf-8"
    )


def make_console(tmp_path: Path) -> WebConsole:
    write_profile(tmp_path / "profiles", tmp_path / "state")
    return WebConsole([tmp_path / "profiles"])


class Test조립:
    def test_프로필_목록이_검색_경로에서_나온다(self, tmp_path: Path) -> None:
        console = make_console(tmp_path)
        res = console.router().handle("GET", "/api/profiles", {}, None)
        assert res.status == 200
        assert res.body == ["example"]

    def test_프롬프트_편집기가_그_봇의_상태_디렉터리를_본다(self, tmp_path: Path) -> None:
        console = make_console(tmp_path)
        prompts = tmp_path / "state" / "prompts"
        prompts.mkdir(parents=True)
        (prompts / "persona.md").write_text("페르소나", encoding="utf-8")

        res = console.router().handle("GET", "/api/prompts/example", {}, None)

        assert res.status == 200
        assert res.body == ["persona"]

    def test_학습이_쌓은_파일은_다른_자리에서_읽는다(self, tmp_path: Path) -> None:
        """사람 자리만 열면 학습이 잘못 쌓은 줄을 지울 수 없다(sca-jl4.5)."""
        console = make_console(tmp_path)
        learned = tmp_path / "state" / "persona" / "learned"
        learned.mkdir(parents=True)
        (learned / "잡담.md").write_text("쌓인 것", encoding="utf-8")

        res = console.router().handle("GET", "/api/learned/example", {}, None)

        assert res.body == ["잡담"]
        assert console.router().handle("GET", "/api/knowledge/example", {}, None).body == []

    def test_지식_편집기는_페르소나_아래를_본다(self, tmp_path: Path) -> None:
        console = make_console(tmp_path)
        knowledge = tmp_path / "state" / "persona" / "knowledge"
        knowledge.mkdir(parents=True)
        (knowledge / "잡담.md").write_text("확정 사실", encoding="utf-8")

        res = console.router().handle("GET", "/api/knowledge/example", {}, None)

        assert res.body == ["잡담"]

    def test_채널_목록을_그_봇의_channels_json_에서_읽는다(self, tmp_path: Path) -> None:
        console = make_console(tmp_path)
        state = tmp_path / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "channels.json").write_text(
            json.dumps({"C9": {"name": "잡담", "mode": "default"}}), encoding="utf-8"
        )

        res = console.router().handle("GET", "/api/channels/example", {}, None)

        assert res.status == 200
        rows = cast(list[dict[str, Any]], res.body)
        assert [row["channel_id"] for row in rows] == ["C9"]

    def test_지표를_그_봇의_프로필로_모은다(self, tmp_path: Path) -> None:
        console = make_console(tmp_path)
        res = console.router().handle("GET", "/api/state/example", {"days": "1"}, None)
        assert res.status == 200
        assert cast(dict[str, Any], res.body)["bot"]["name"] == "example"

    def test_봇_명부가_모든_프로필을_낸다(self, tmp_path: Path) -> None:
        """봇 선택줄이 이것을 쓴다. 지표 응답은 선택된 봇 하나만 담는다."""
        console = make_console(tmp_path)
        profiles = tmp_path / "profiles"
        (profiles / "another.json").write_text(
            json.dumps({**PROFILE, "name": "another", "state_dir": str(tmp_path / "state2")}),
            encoding="utf-8",
        )

        res = console.router().handle("GET", "/api/bots", {}, None)

        assert res.status == 200
        assert isinstance(res.body, list)
        assert [row["name"] for row in res.body] == ["another", "example"]

    def test_프로필을_저장하면_파일이_바뀐다(self, tmp_path: Path) -> None:
        console = make_console(tmp_path)
        data = {**PROFILE, "state_dir": str(tmp_path / "state"), "display_name": "새이름"}

        res = console.router().handle("PUT", "/api/profile/example", {}, data)

        assert res.status == 200
        saved = json.loads((tmp_path / "profiles" / "example.json").read_text(encoding="utf-8"))
        assert saved["display_name"] == "새이름"

    def test_서버는_127_0_0_1_에만_묶는다(self, tmp_path: Path) -> None:
        """지표에 질문·답변 원문과 비용이 들어간다. 다른 기기에서 닿으면 안 된다."""
        console = make_console(tmp_path)
        server = console.server(port=0)
        server.start()
        try:
            assert server.bound_address[0] == "127.0.0.1"
        finally:
            server.stop()


class Test슬러그_이사가_웹에도_걸린다:
    """콜백을 application.py 에서만 걸면 웹에서 이름을 바꿨을 때 파일이 옛
    이름에 남는다(sca-a26x)."""

    def test_웹에서_이름을_바꾸면_배운_것이_따라_옮겨진다(self, tmp_path: Path) -> None:
        console = make_console(tmp_path)
        state = tmp_path / "state"
        learned = state / "persona" / "learned"
        learned.mkdir(parents=True)
        (learned / "C9.md").write_text("배운 것", encoding="utf-8")
        state.mkdir(parents=True, exist_ok=True)
        (state / "channels.json").write_text(
            json.dumps({"C9": {"name": "C9", "mode": "default"}}), encoding="utf-8"
        )

        res = console.router().handle("PUT", "/api/channels/example/C9", {}, {"name": "잡담"})

        assert res.status == 200
        assert (learned / "잡담.md").read_text(encoding="utf-8") == "배운 것"
        assert not (learned / "C9.md").exists()
