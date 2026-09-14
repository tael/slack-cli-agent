"""SpeakerNamer 단위 시험.

TranscriptBuilder 와 LateAddendumChecker 가 각각 들고 있던 화자 표시 로직을
하나로 모은 것을 검증한다. 봇 분기(자기 자신·다른 봇)를 포함한 완전한 판정을
여기서 검증하고, 두 사용처는 이 클래스에 위임만 한다.
"""

from __future__ import annotations

from slack_cli_agent.slack.speaker import SpeakerNamer


def make_namer(
    *,
    is_self=lambda msg: False,
    name_resolver=lambda user_id: "",
    bot_display_name="",
    owner_user_id="",
    owner_display_name="",
) -> SpeakerNamer:
    return SpeakerNamer(
        name_resolver=name_resolver,
        is_self=is_self,
        bot_display_name=bot_display_name,
        owner_user_id=owner_user_id,
        owner_display_name=owner_display_name,
    )


class TestSpeakerNamer:
    def test_이_봇이_보낸_말은_봇_표시_이름으로_적힌다(self) -> None:
        namer = make_namer(is_self=lambda msg: True, bot_display_name="테스트봇")
        assert namer.speaker_of({"user": "U_BOT"}) == "테스트봇"

    def test_봇_표시_이름이_없으면_봇으로_적힌다(self) -> None:
        namer = make_namer(is_self=lambda msg: True, bot_display_name="")
        assert namer.speaker_of({"user": "U_BOT"}) == "봇"

    def test_다른_봇은_이름_뒤에_다른_봇_표시를_붙인다(self) -> None:
        namer = make_namer(bot_display_name="테스트봇")
        msg = {"bot_id": "B_OTHER", "bot_profile": {"name": "잠만보"}}
        assert namer.speaker_of(msg) == "잠만보 (다른 봇)"

    def test_이름을_모르는_다른_봇은_이름_모르는_봇으로_적힌다(self) -> None:
        namer = make_namer()
        msg = {"bot_id": "B_OTHER"}
        assert namer.speaker_of(msg) == "이름 모르는 봇"

    def test_다른_봇_이름이_없으면_username으로_대신한다(self) -> None:
        namer = make_namer()
        msg = {"bot_id": "B_OTHER", "username": "웹훅이름"}
        assert namer.speaker_of(msg) == "웹훅이름 (다른 봇)"

    def test_소유자_사용자_id는_소유자_표시_이름으로_적힌다(self) -> None:
        namer = make_namer(owner_user_id="UOWNER", owner_display_name="홍길동")
        assert namer.speaker_of({"user": "UOWNER"}) == "홍길동"

    def test_소유자_표시_이름이_없으면_일반_판정을_따른다(self) -> None:
        namer = make_namer(
            owner_user_id="UOWNER",
            owner_display_name="",
            name_resolver=lambda uid: {"UOWNER": "김철수"}.get(uid, ""),
        )
        assert namer.speaker_of({"user": "UOWNER"}) == "김철수 <@UOWNER>"

    def test_일반_사용자는_이름과_멘션을_함께_적는다(self) -> None:
        namer = make_namer(name_resolver=lambda uid: {"U1": "김철수"}.get(uid, ""))
        assert namer.speaker_of({"user": "U1"}) == "김철수 <@U1>"

    def test_이름을_못_찾으면_이름_모르는_사람으로_적는다(self) -> None:
        namer = make_namer(name_resolver=lambda uid: "")
        assert namer.speaker_of({"user": "U_UNKNOWN"}) == "이름 모르는 사람 <@U_UNKNOWN>"

    def test_사용자_id가_없으면_멘션_없이_이름만_적는다(self) -> None:
        namer = make_namer(name_resolver=lambda uid: "")
        assert namer.speaker_of({}) == "이름 모르는 사람"
