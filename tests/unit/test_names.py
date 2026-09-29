"""DisplayNameResolver 시험.

원본 `bot.py` 의 `asker_display_name` 과 전역 `_asker_cache`, `_name_to_id` 를
클래스 하나로 옮긴 것의 기대 동작을 고정한다.
"""

from __future__ import annotations

from typing import Any

from slack_cli_agent.slack.names import DisplayNameResolver, UserNamer


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


# BotUserResolver (sca-c4m)


def _bot_profile(*, is_bot: bool = False, user_id: str = "U1") -> dict[str, Any]:
    return {"id": user_id, "is_bot": is_bot, "profile": {}, "name": ""}


def test_봇_계정은_봇으로_본다() -> None:
    from slack_cli_agent.slack.names import BotUserResolver

    client = _FakeSlackClient({"U0REI": _bot_profile(is_bot=True, user_id="U0REI")})

    assert BotUserResolver(client).is_bot("U0REI") is True


def test_사람_계정은_봇이_아니다() -> None:
    from slack_cli_agent.slack.names import BotUserResolver

    client = _FakeSlackClient({"U0TAEL": _bot_profile(user_id="U0TAEL")})

    assert BotUserResolver(client).is_bot("U0TAEL") is False


def test_슬랙봇도_봇으로_본다() -> None:
    from slack_cli_agent.slack.names import BotUserResolver

    client = _FakeSlackClient({"USLACKBOT": _bot_profile(user_id="USLACKBOT")})

    assert BotUserResolver(client).is_bot("USLACKBOT") is True


def test_조회가_안_되면_사람으로_본다() -> None:
    """이 판정의 쓰임은 멘션을 지우는 것이다. 조회 실패를 봇으로 읽으면
    일시적 장애에 사람 멘션이 지워진다. 소유자 전용 채널 점검은 반대로
    실패를 봇으로 보는데, 거기서는 사람으로 읽는 쪽이 헛경보를 낸다."""
    from slack_cli_agent.slack.names import BotUserResolver

    assert BotUserResolver(_FakeSlackClient()).is_bot("U0GONE") is False


def test_같은_사용자를_두_번_물어도_조회는_한_번이다() -> None:
    from slack_cli_agent.slack.names import BotUserResolver

    client = _FakeSlackClient({"U0REI": _bot_profile(is_bot=True, user_id="U0REI")})
    resolver = BotUserResolver(client)

    resolver.is_bot("U0REI")
    resolver.is_bot("U0REI")

    assert client.call_count == 1


def test_조회_실패는_간격을_두고_다시_묻는다() -> None:
    """실패를 사람으로 굳혀 캐시하면 그 봇의 멘션이 프로세스가 사는 내내
    안 지워져 막으려던 루프가 그대로 난다. 그렇다고 매번 다시 물으면 답
    하나에 멘션 수만큼 조회가 나간다. 그래서 간격을 둔다(코덱스 리뷰)."""
    from slack_cli_agent.slack.names import BotUserResolver

    client = _FakeSlackClient()
    시각 = {"값": 0.0}
    resolver = BotUserResolver(client, clock=lambda: 시각["값"], retry_interval_sec=60.0)

    resolver.is_bot("U0GONE")
    resolver.is_bot("U0GONE")
    assert client.call_count == 1

    시각["값"] = 61.0
    resolver.is_bot("U0GONE")
    assert client.call_count == 2


def test_실패_뒤_되살아나면_봇으로_바뀐다() -> None:
    from slack_cli_agent.slack.names import BotUserResolver

    client = _FakeSlackClient()
    시각 = {"값": 0.0}
    resolver = BotUserResolver(client, clock=lambda: 시각["값"], retry_interval_sec=60.0)

    assert resolver.is_bot("U0REI") is False

    client._profiles["U0REI"] = _bot_profile(is_bot=True, user_id="U0REI")
    시각["값"] = 61.0

    assert resolver.is_bot("U0REI") is True


def test_성공한_판정은_다시_묻지_않는다() -> None:
    from slack_cli_agent.slack.names import BotUserResolver

    client = _FakeSlackClient({"U0REI": _bot_profile(is_bot=True, user_id="U0REI")})
    시각 = {"값": 0.0}
    resolver = BotUserResolver(client, clock=lambda: 시각["값"], retry_interval_sec=60.0)

    resolver.is_bot("U0REI")
    시각["값"] = 6000.0
    resolver.is_bot("U0REI")

    assert client.call_count == 1


def test_빈_user_id_는_조회하지_않는다() -> None:
    from slack_cli_agent.slack.names import BotUserResolver

    client = _FakeSlackClient()

    assert BotUserResolver(client).is_bot("") is False
    assert client.call_count == 0


class _가짜신원:
    def __init__(self, user_id: str = "") -> None:
        self.user_id = user_id


class TestUserNamer:
    """봇 자신이 화자 자리와 멘션 자리에서 같은 이름으로 나와야 한다.

    화자 표기는 프로필의 display_name 을 쓰고 멘션은 users.info 의 real_name 을
    써서, 같은 봇이 대화록 머리와 본문에서 다른 이름으로 나왔다 (sca-inw8).
    """

    def test_봇_자신은_프로필_표시명으로_부른다(self) -> None:
        namer = UserNamer(
            lambda uid: "Shinji Ikari Bot",
            identity=_가짜신원("UBOT"),
            bot_display_name="신지",
        )
        assert namer.name_of("UBOT") == "신지"

    def test_다른_사람은_resolver_결과를_그대로_쓴다(self) -> None:
        namer = UserNamer(
            lambda uid: "홍길동", identity=_가짜신원("UBOT"), bot_display_name="신지"
        )
        assert namer.name_of("U1") == "홍길동"

    def test_소유자_표시명이_있으면_그것을_쓴다(self) -> None:
        namer = UserNamer(
            lambda uid: "Gildong Hong",
            identity=_가짜신원("UBOT"),
            bot_display_name="신지",
            owner_user_id="UOWNER",
            owner_display_name="대표",
        )
        assert namer.name_of("UOWNER") == "대표"

    def test_표시명이_비어_있으면_resolver_로_떨어진다(self) -> None:
        namer = UserNamer(lambda uid: "Shinji Ikari Bot", identity=_가짜신원("UBOT"))
        assert namer.name_of("UBOT") == "Shinji Ikari Bot"

    def test_신원_조회가_터져도_resolver_로_떨어진다(self) -> None:
        """신원은 슬랙 조회라 실패할 수 있다. 그때 이름이 안 나오면
        대화록 전체가 빈 이름이 된다."""

        class 터지는신원:
            @property
            def user_id(self) -> str:
                raise RuntimeError("auth_test 실패")

        namer = UserNamer(
            lambda uid: "홍길동", identity=터지는신원(), bot_display_name="신지"
        )
        assert namer.name_of("U1") == "홍길동"

    def test_resolver_자리에_그대로_넣을_수_있다(self) -> None:
        """MentionRenderer·CalledNames 가 Callable[[str], str] 을 받는다."""
        namer = UserNamer(
            lambda uid: "Shinji Ikari Bot",
            identity=_가짜신원("UBOT"),
            bot_display_name="신지",
        )
        assert namer("UBOT") == "신지"
