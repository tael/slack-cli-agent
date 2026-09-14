"""DisplayNameResolver 시험.

원본 `bot.py` 의 `asker_display_name` 과 전역 `_asker_cache`, `_name_to_id` 를
클래스 하나로 옮긴 것의 기대 동작을 고정한다.
"""

from __future__ import annotations

from typing import Any

from slack_cli_agent.slack.names import DisplayNameResolver


class _FakeSlackClient:
    """가짜 슬랙 client. users_info 호출 횟수를 센다."""

    def __init__(self, profiles: dict[str, dict[str, Any]] | None = None) -> None:
        self._profiles = profiles or {}
        self.call_count = 0

    def users_info(self, user: str) -> dict[str, Any]:
        self.call_count += 1
        if user not in self._profiles:
            raise RuntimeError("사용자 정보 조회 실패")
        return {"user": self._profiles[user]}


def _profile(*, real_name: str = "", display_name: str = "", name: str = "") -> dict[str, Any]:
    return {"profile": {"real_name": real_name, "display_name": display_name}, "name": name}


def test_resolve_empty_user_id_returns_empty_without_calling_client() -> None:
    client = _FakeSlackClient()
    resolver = DisplayNameResolver(client)

    assert resolver.resolve("") == ""
    assert client.call_count == 0


def test_resolve_prefers_real_name_over_display_name_and_name() -> None:
    client = _FakeSlackClient(
        {
            "U_ALICE": _profile(real_name="홍길동", display_name="길동이", name="alice"),
        }
    )
    resolver = DisplayNameResolver(client)

    assert resolver.resolve("U_ALICE") == "홍길동"


def test_resolve_falls_back_to_display_name_when_real_name_empty() -> None:
    client = _FakeSlackClient(
        {
            "U_ALICE": _profile(real_name="", display_name="길동이", name="alice"),
        }
    )
    resolver = DisplayNameResolver(client)

    assert resolver.resolve("U_ALICE") == "길동이"


def test_resolve_falls_back_to_name_when_profile_fields_empty() -> None:
    client = _FakeSlackClient(
        {
            "U_ALICE": _profile(real_name="", display_name="", name="alice"),
        }
    )
    resolver = DisplayNameResolver(client)

    assert resolver.resolve("U_ALICE") == "alice"


def test_resolve_caches_lookup_across_calls() -> None:
    client = _FakeSlackClient(
        {
            "U_ALICE": _profile(real_name="홍길동"),
        }
    )
    resolver = DisplayNameResolver(client)

    assert resolver.resolve("U_ALICE") == "홍길동"
    assert resolver.resolve("U_ALICE") == "홍길동"
    assert client.call_count == 1


def test_resolve_failure_returns_empty_and_is_cached() -> None:
    client = _FakeSlackClient({})
    resolver = DisplayNameResolver(client)

    assert resolver.resolve("U_UNKNOWN") == ""
    assert resolver.resolve("U_UNKNOWN") == ""
    assert client.call_count == 1


def test_successful_resolve_populates_name_table() -> None:
    client = _FakeSlackClient(
        {
            "U_ALICE": _profile(real_name="홍길동"),
        }
    )
    resolver = DisplayNameResolver(client)
    resolver.resolve("U_ALICE")

    assert resolver.name_table()["홍길동"] == "U_ALICE"


def test_display_name_with_space_registers_head_token_too() -> None:
    client = _FakeSlackClient(
        {
            "U_ALICE": _profile(real_name="홍길동 개발팀"),
        }
    )
    resolver = DisplayNameResolver(client)
    resolver.resolve("U_ALICE")

    table = resolver.name_table()
    assert table["홍길동 개발팀"] == "U_ALICE"
    assert table["홍길동"] == "U_ALICE"


def test_name_table_does_not_overwrite_existing_mapping() -> None:
    client = _FakeSlackClient(
        {
            "U_ALICE": _profile(real_name="홍길동"),
            "U_BOB": _profile(real_name="홍길동"),
        }
    )
    resolver = DisplayNameResolver(client)
    resolver.resolve("U_ALICE")
    resolver.resolve("U_BOB")

    assert resolver.name_table()["홍길동"] == "U_ALICE"


def test_register_adds_external_mapping() -> None:
    client = _FakeSlackClient()
    resolver = DisplayNameResolver(client)

    resolver.register("홍길동", "U_ALICE")

    assert resolver.name_table()["홍길동"] == "U_ALICE"


def test_register_does_not_overwrite_existing_mapping() -> None:
    client = _FakeSlackClient(
        {
            "U_ALICE": _profile(real_name="홍길동"),
        }
    )
    resolver = DisplayNameResolver(client)
    resolver.resolve("U_ALICE")

    resolver.register("홍길동", "U_BOB")

    assert resolver.name_table()["홍길동"] == "U_ALICE"


def test_name_table_returns_copy_not_internal_state() -> None:
    client = _FakeSlackClient(
        {
            "U_ALICE": _profile(real_name="홍길동"),
        }
    )
    resolver = DisplayNameResolver(client)
    resolver.resolve("U_ALICE")

    table = resolver.name_table()
    table["침입"] = "U_MALLORY"

    assert "침입" not in resolver.name_table()


def test_call_matches_resolve() -> None:
    client = _FakeSlackClient(
        {
            "U_ALICE": _profile(real_name="홍길동"),
        }
    )
    resolver = DisplayNameResolver(client)

    assert resolver("U_ALICE") == resolver.resolve("U_ALICE")
    assert client.call_count == 1
