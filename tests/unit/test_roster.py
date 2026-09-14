"""RosterBuilder, RosterSection 시험.

원본 `bot.py` 의 `build_people()`, `people_loop()` 을 옮긴 것의 기대 동작을
고정한다. 실제 인물 예시는 전부 가상값(alice.kim, bob.lee 등)이다.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from slack_cli_agent.auth.principal import Principal, TrustLevel
from slack_cli_agent.prompt.sections import CompositionContext, RosterSection
from slack_cli_agent.slack.roster import RosterBuilder, RosterEntry


class _FakeSlackClient:
    """`users_list` 를 커서 페이지네이션으로 흉내낸다."""

    def __init__(self, pages: list[dict[str, Any]] | None = None, error: Exception | None = None) -> None:
        self._pages = pages or []
        self._error = error
        self.calls: list[dict[str, Any]] = []

    def users_list(self, limit: int, cursor: str | None = None) -> dict[str, Any]:
        self.calls.append({"limit": limit, "cursor": cursor})
        if self._error is not None:
            raise self._error
        index = len(self.calls) - 1
        if index >= len(self._pages):
            return {"members": [], "response_metadata": {"next_cursor": ""}}
        return self._pages[index]


def _member(
    *,
    handle: str = "alice.kim",
    real_name: str = "김앨리스",
    is_bot: bool = False,
    is_app_user: bool = False,
    deleted: bool = False,
) -> dict[str, Any]:
    return {
        "name": handle,
        "profile": {"real_name": real_name},
        "is_bot": is_bot,
        "is_app_user": is_app_user,
        "deleted": deleted,
    }


def _page(members: list[dict[str, Any]], next_cursor: str = "") -> dict[str, Any]:
    return {"members": members, "response_metadata": {"next_cursor": next_cursor}}


class TestRosterBuilderFetch:
    def test_커서_페이지네이션으로_여러_페이지를_전부_조회한다(self, tmp_path: Path) -> None:
        client = _FakeSlackClient(
            pages=[
                _page([_member(handle="alice.kim", real_name="김앨리스")], next_cursor="c1"),
                _page([_member(handle="bob.lee", real_name="이밥")], next_cursor=""),
            ]
        )
        builder = RosterBuilder(client, tmp_path / "roster.md", now=lambda: 1_700_000_000.0)

        count = builder.refresh()

        assert count == 2
        assert client.calls[0]["cursor"] is None
        assert client.calls[1]["cursor"] == "c1"
        text = (tmp_path / "roster.md").read_text(encoding="utf-8")
        assert "alice.kim" in text
        assert "bob.lee" in text


class TestRosterBuilderFilters:
    def test_봇_사용자는_제외된다(self, tmp_path: Path) -> None:
        client = _FakeSlackClient(
            pages=[
                _page(
                    [
                        _member(handle="alice.kim", is_bot=True),
                        _member(handle="bob.lee", real_name="이밥"),
                    ]
                )
            ]
        )
        builder = RosterBuilder(client, tmp_path / "roster.md", now=lambda: 1_700_000_000.0)

        count = builder.refresh()

        assert count == 1
        text = (tmp_path / "roster.md").read_text(encoding="utf-8")
        assert "alice.kim" not in text
        assert "bob.lee" in text

    def test_앱_사용자는_제외된다(self, tmp_path: Path) -> None:
        client = _FakeSlackClient(
            pages=[
                _page(
                    [
                        _member(handle="alice.kim", is_app_user=True),
                        _member(handle="bob.lee", real_name="이밥"),
                    ]
                )
            ]
        )
        builder = RosterBuilder(client, tmp_path / "roster.md", now=lambda: 1_700_000_000.0)

        builder.refresh()

        text = (tmp_path / "roster.md").read_text(encoding="utf-8")
        assert "alice.kim" not in text

    def test_핸들이_비어_있으면_제외된다(self, tmp_path: Path) -> None:
        client = _FakeSlackClient(
            pages=[
                _page(
                    [
                        _member(handle="", real_name="김앨리스"),
                        _member(handle="bob.lee", real_name="이밥"),
                    ]
                )
            ]
        )
        builder = RosterBuilder(client, tmp_path / "roster.md", now=lambda: 1_700_000_000.0)

        count = builder.refresh()

        assert count == 1

    def test_실명이_비어_있으면_제외된다(self, tmp_path: Path) -> None:
        client = _FakeSlackClient(
            pages=[
                _page(
                    [
                        _member(handle="alice.kim", real_name=""),
                        _member(handle="bob.lee", real_name="이밥"),
                    ]
                )
            ]
        )
        builder = RosterBuilder(client, tmp_path / "roster.md", now=lambda: 1_700_000_000.0)

        count = builder.refresh()

        assert count == 1

    def test_핸들과_실명이_같으면_제외된다(self, tmp_path: Path) -> None:
        client = _FakeSlackClient(
            pages=[
                _page(
                    [
                        _member(handle="alice.kim", real_name="alice.kim"),
                        _member(handle="bob.lee", real_name="이밥"),
                    ]
                )
            ]
        )
        builder = RosterBuilder(client, tmp_path / "roster.md", now=lambda: 1_700_000_000.0)

        count = builder.refresh()

        assert count == 1

    def test_핸들에_점이_없으면_제외된다(self, tmp_path: Path) -> None:
        client = _FakeSlackClient(
            pages=[
                _page(
                    [
                        _member(handle="alice", real_name="김앨리스"),
                        _member(handle="bob.lee", real_name="이밥"),
                    ]
                )
            ]
        )
        builder = RosterBuilder(client, tmp_path / "roster.md", now=lambda: 1_700_000_000.0)

        count = builder.refresh()

        assert count == 1

    def test_퇴사자는_제외되지_않고_상태가_표시된다(self, tmp_path: Path) -> None:
        client = _FakeSlackClient(
            pages=[_page([_member(handle="bob.lee", real_name="이밥", deleted=True)])]
        )
        builder = RosterBuilder(client, tmp_path / "roster.md", now=lambda: 1_700_000_000.0)

        count = builder.refresh()

        assert count == 1
        text = (tmp_path / "roster.md").read_text(encoding="utf-8")
        assert "bob.lee" in text
        assert "퇴사" in text
        lines = [line for line in text.splitlines() if line.startswith("| bob.lee")]
        assert len(lines) == 1
        assert "퇴사" in lines[0]


class TestRosterBuilderFailurePolicy:
    def test_조회가_예외를_내면_기존_파일이_그대로_남는다(self, tmp_path: Path) -> None:
        output = tmp_path / "roster.md"
        output.write_text("기존 내용", encoding="utf-8")
        client = _FakeSlackClient(error=RuntimeError("조회 실패"))
        builder = RosterBuilder(client, output, now=lambda: 1_700_000_000.0)

        count = builder.refresh()

        assert count == 0
        assert output.read_text(encoding="utf-8") == "기존 내용"

    def test_결과가_비면_기존_파일이_그대로_남는다(self, tmp_path: Path) -> None:
        output = tmp_path / "roster.md"
        output.write_text("기존 내용", encoding="utf-8")
        client = _FakeSlackClient(pages=[_page([])])
        builder = RosterBuilder(client, output, now=lambda: 1_700_000_000.0)

        count = builder.refresh()

        assert count == 0
        assert output.read_text(encoding="utf-8") == "기존 내용"

    def test_출력_디렉터리가_없으면_스스로_만든다(self, tmp_path: Path) -> None:
        output = tmp_path / "nested" / "roster.md"
        client = _FakeSlackClient(pages=[_page([_member(handle="bob.lee", real_name="이밥")])])
        builder = RosterBuilder(client, output, now=lambda: 1_700_000_000.0)

        count = builder.refresh()

        assert count == 1
        assert output.exists()


class TestRosterBuilderRendering:
    def test_표가_핸들_기준으로_정렬된다(self, tmp_path: Path) -> None:
        client = _FakeSlackClient(
            pages=[
                _page(
                    [
                        _member(handle="zoe.park", real_name="박조이"),
                        _member(handle="alice.kim", real_name="김앨리스"),
                    ]
                )
            ]
        )
        builder = RosterBuilder(client, tmp_path / "roster.md", now=lambda: 1_700_000_000.0)

        builder.refresh()

        text = (tmp_path / "roster.md").read_text(encoding="utf-8")
        assert text.index("alice.kim") < text.index("zoe.park")

    def test_시각_주입이_반영된다(self, tmp_path: Path) -> None:
        client = _FakeSlackClient(pages=[_page([_member(handle="bob.lee", real_name="이밥")])])
        builder = RosterBuilder(client, tmp_path / "roster.md", now=lambda: 1_700_000_000.0)

        builder.refresh()

        text = (tmp_path / "roster.md").read_text(encoding="utf-8")
        assert "2023-11-15" in text


class TestRosterSection:
    def _ctx(self) -> CompositionContext:
        principal = Principal(
            user_id="U1", channel="C1", trust=TrustLevel.GENERAL, is_direct_message=False
        )
        return CompositionContext(principal=principal)

    def test_파일이_없으면_아무것도_내지_않는다(self, tmp_path: Path) -> None:
        section = RosterSection(tmp_path / "roster.md")

        assert section.applies_to(self._ctx()) is True
        assert section.render(self._ctx()) == ""

    def test_파일이_있으면_경로를_알린다(self, tmp_path: Path) -> None:
        roster_path = tmp_path / "roster.md"
        roster_path.write_text("| 계정 핸들 | 이름 | 상태 |\n", encoding="utf-8")
        section = RosterSection(roster_path)

        rendered = section.render(self._ctx())

        assert str(roster_path) in rendered
        # 내용 전체를 프롬프트에 싣지 않는다. 표 헤더가 그대로 들어가면 안 된다.
        assert "계정 핸들 | 이름 | 상태" not in rendered
