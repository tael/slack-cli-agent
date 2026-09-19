"""대화록 본문의 멘션 치환 시험 (sca-hkmb).

원본 bot.py:2722 readable_mentions 가 하던 일이다. 본문에 <@U...> 가 그대로
남으면 모델은 그게 누구인지 모른다. 2026-08-26 07:47 에 그 상태에서 화자와
청자가 뒤집혀 나갔다.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from identity_support import fake_identity

from slack_cli_agent.slack.mentions import MentionRenderer, SelfMentionStripper
from slack_cli_agent.slack.names import UserGroupNameResolver


def 이름표(표: dict[str, str]):
    return lambda user_id: 표.get(user_id, "")


class Test사용자_멘션:
    def test_이름으로_바꾼다(self) -> None:
        보기 = MentionRenderer(이름표({"U1": "김태일"}))
        assert 보기.render("<@U1> 확인해줘") == "김태일 확인해줘"

    def test_표시이름이_붙어_있어도_해석한_이름을_쓴다(self) -> None:
        보기 = MentionRenderer(이름표({"U1": "김태일"}))
        assert 보기.render("<@U1|taeil> 확인") == "김태일 확인"

    def test_못_풀면_붙어_있는_표시이름을_쓴다(self) -> None:
        보기 = MentionRenderer(이름표({}))
        assert 보기.render("<@U9|taeil> 확인") == "taeil 확인"

    def test_아무것도_못_풀면_원본을_남긴다(self) -> None:
        """지우면 누가 불렸는지가 사라진다."""
        보기 = MentionRenderer(이름표({}))
        assert 보기.render("<@U9> 확인") == "<@U9> 확인"

    def test_여러_사람을_한_줄에서_바꾼다(self) -> None:
        보기 = MentionRenderer(이름표({"U1": "김태일", "U2": "박종선"}))
        assert 보기.render("<@U1> 이 <@U2> 에게") == "김태일 이 박종선 에게"


class Test그룹_멘션:
    def test_그룹_이름으로_바꾼다(self) -> None:
        보기 = MentionRenderer(이름표({}), group_resolver=lambda gid: "데이터팀")
        assert 보기.render("<!subteam^S1> 봐주세요") == "@데이터팀 그룹 봐주세요"

    def test_못_풀면_붙어_있는_핸들을_쓴다(self) -> None:
        보기 = MentionRenderer(이름표({}), group_resolver=lambda gid: "")
        assert 보기.render("<!subteam^S1|@data> 봐주세요") == "@data 그룹 봐주세요"

    def test_아무것도_못_풀면_원본을_남긴다(self) -> None:
        보기 = MentionRenderer(이름표({}))
        assert 보기.render("<!subteam^S1> 봐주세요") == "<!subteam^S1> 봐주세요"


class Test특수_멘션은_건드리지_않는다:
    @pytest.mark.parametrize("원본", ["<!here> 봐주세요", "<!channel> 공지", "<#C1|일반> 에서"])
    def test_그대로_둔다(self, 원본: str) -> None:
        보기 = MentionRenderer(이름표({"U1": "김태일"}))
        assert 보기.render(원본) == 원본


class Test해석_실패는_요청을_깨지_않는다:
    def test_해석기가_예외를_내도_원본을_남긴다(self) -> None:
        def 터진다(_: str) -> str:
            raise RuntimeError("조회 실패")

        보기 = MentionRenderer(터진다, group_resolver=터진다)
        assert 보기.render("<@U1> 과 <!subteam^S1>") == "<@U1> 과 <!subteam^S1>"


class Test그룹_이름_해석기:
    class 대역:
        def __init__(self, 그룹들: list[dict[str, Any]]) -> None:
            self.그룹들 = 그룹들
            self.호출 = 0

        def usergroups_list(self) -> dict[str, Any]:
            self.호출 += 1
            return {"usergroups": self.그룹들}

    def test_핸들을_우선_쓴다(self) -> None:
        client = self.대역([{"id": "S1", "handle": "data", "name": "데이터팀"}])
        assert UserGroupNameResolver(client).resolve("S1") == "data"

    def test_핸들이_없으면_이름을_쓴다(self) -> None:
        client = self.대역([{"id": "S1", "name": "데이터팀"}])
        assert UserGroupNameResolver(client).resolve("S1") == "데이터팀"

    def test_한_번만_조회한다(self) -> None:
        """그룹은 자주 바뀌지 않는다 (bot.py:2695)."""
        client = self.대역([{"id": "S1", "handle": "data"}])
        resolver = UserGroupNameResolver(client)
        resolver.resolve("S1")
        resolver.resolve("S1")
        resolver.resolve("S2")
        assert client.호출 == 1

    def test_조회가_실패하면_빈_값이다(self) -> None:
        class 터지는대역:
            def usergroups_list(self) -> dict[str, Any]:
                raise RuntimeError("scope 없음")

        assert UserGroupNameResolver(터지는대역()).resolve("S1") == ""


class Test대화록은_본문을_그대로_두고_머리에_방향을_적는다:
    """원본 bot.py:2722 와 같다. 본문을 이름으로 바꿨더니 모델이 그 표기를
    답변에 옮겨 적었고, 그 이름은 슬랙에서 링크가 안 걸려 불러도 상대가 못
    받았다(2026-08-26 12:02, 12:05). 방향은 줄 머리에 적는다 (sca-ddkf)."""

    class 대역클라이언트:
        def __init__(self, messages: list[dict[str, Any]]) -> None:
            self._messages = messages

        def conversations_replies(self, **_: Any) -> dict[str, Any]:
            return {"messages": self._messages}

    def 대화록(self, text: str, **kwargs: Any) -> str:
        from identity_support import fake_identity

        from slack_cli_agent.config.settings import RuntimeSettings
        from slack_cli_agent.observability.notices import NoticeCatalog
        from slack_cli_agent.slack.transcript import TranscriptBuilder

        client = self.대역클라이언트([{"ts": "1700000000.000001", "user": "U1", "text": text}])
        builder = TranscriptBuilder(
            client, RuntimeSettings(), NoticeCatalog(),
            name_resolver=이름표({"U1": "김철수", "U2": "박종선"}),
            identity=fake_identity(), bot_display_name="테스트봇",
            **kwargs,
        )
        return builder.thread_transcript("C1", "1700000000.000001", before_ts=None)

    def test_본문은_슬랙_원문_그대로다(self) -> None:
        body = self.대화록("<@U2> 어제 그거 봤어?")
        assert "<@U2> 어제 그거 봤어?" in body

    def test_부른_사람을_줄_머리에_적는다(self) -> None:
        body = self.대화록("<@U2> 어제 그거 봤어?")
        assert "김철수 <@U1> -> 박종선]" in body

    def test_아무도_안_불렀으면_화자만_적는다(self) -> None:
        body = self.대화록("혼잣말")
        assert "->" not in body
        assert "김철수 <@U1>]" in body

    def test_그룹을_부른_것도_머리에_적는다(self) -> None:
        body = self.대화록("<!subteam^S1> 확인 부탁", group_resolver=lambda gid: "데이터팀")
        assert "김철수 <@U1> -> @데이터팀 그룹]" in body
        assert "<!subteam^S1> 확인 부탁" in body

    def test_방송_멘션도_머리에_적는다(self) -> None:
        body = self.대화록("<!here> 잠깐만")
        assert "김철수 <@U1> -> 이 자리 모두]" in body


class Test경계_입력:
    """리뷰 지적 2026-09-19."""

    def test_파이프_뒤가_비어_있으면_원본을_그대로_남긴다(self) -> None:
        보기 = MentionRenderer(이름표({}))
        assert 보기.render("<@U9|> 확인") == "<@U9|> 확인"

    def test_연속된_멘션을_각각_바꾼다(self) -> None:
        보기 = MentionRenderer(이름표({"U1": "김태일", "U2": "박종선"}))
        assert 보기.render("<@U1><@U2>") == "김태일박종선"

    def test_조사가_바로_붙어도_바꾼다(self) -> None:
        보기 = MentionRenderer(이름표({"U1": "김태일"}))
        assert 보기.render("<@U1>님이 말했다") == "김태일님이 말했다"

    def test_W_로_시작하는_계정도_바꾼다(self) -> None:
        """Enterprise Grid 의 사용자 ID 는 W 로 시작한다."""
        보기 = MentionRenderer(이름표({"W1": "김태일"}))
        assert 보기.render("<@W1> 확인") == "김태일 확인"

    def test_해석한_이름에_멘션_표기가_들어_있어도_다시_바꾸지_않는다(self) -> None:
        """한 번에 훑기 때문이다. 두 번 훑으면 이 자리가 또 치환된다."""
        보기 = MentionRenderer(이름표({"U1": "<!subteam^S9>"}), group_resolver=lambda gid: "데이터팀")
        assert 보기.render("<@U1> 확인") == "<!subteam^S9> 확인"


class Test그룹_조회_실패_처리:
    """리뷰 지적 2026-09-19 - 실패를 굳히지도, 매번 부르지도 않는다."""

    class 대역:
        def __init__(self, *응답들: Any) -> None:
            self.응답들 = list(응답들)
            self.호출 = 0

        def usergroups_list(self) -> Any:
            self.호출 += 1
            답 = self.응답들[min(self.호출 - 1, len(self.응답들) - 1)]
            if isinstance(답, Exception):
                raise 답
            return 답

    def test_ok_거짓_응답은_성공으로_굳히지_않는다(self) -> None:
        client = self.대역(
            {"ok": False, "error": "missing_scope"},
            {"ok": True, "usergroups": [{"id": "S1", "handle": "data"}]},
        )
        시계 = [0.0]
        resolver = UserGroupNameResolver(client, retry_interval_sec=10.0, clock=lambda: 시계[0])
        assert resolver.resolve("S1") == ""
        시계[0] = 20.0
        assert resolver.resolve("S1") == "data"

    def test_실패_뒤_간격_안에는_다시_부르지_않는다(self) -> None:
        client = self.대역(RuntimeError("missing_scope"))
        시계 = [0.0]
        resolver = UserGroupNameResolver(client, retry_interval_sec=10.0, clock=lambda: 시계[0])
        resolver.resolve("S1")
        시계[0] = 5.0
        resolver.resolve("S1")
        resolver.resolve("S2")
        assert client.호출 == 1

    def test_간격이_지나면_다시_부른다(self) -> None:
        """스코프를 나중에 붙였을 때 재기동 없이 든다."""
        client = self.대역(RuntimeError("missing_scope"))
        시계 = [0.0]
        resolver = UserGroupNameResolver(client, retry_interval_sec=10.0, clock=lambda: 시계[0])
        resolver.resolve("S1")
        시계[0] = 11.0
        resolver.resolve("S1")
        assert client.호출 == 2


class Test공지는_원본으로_판정한다:
    def test_멘션이_든_공지_문구도_걸러낸다(self) -> None:
        """치환을 먼저 하면 공지 문구와 안 맞아 대화록에 섞인다 (리뷰 지적)."""
        from identity_support import fake_identity

        from slack_cli_agent.config.settings import RuntimeSettings
        from slack_cli_agent.observability.notices import NoticeCatalog
        from slack_cli_agent.slack.transcript import TranscriptBuilder

        공지 = "<@U1> 님 답이 늦었어요."
        client = Test대화록은_본문을_그대로_두고_머리에_방향을_적는다.대역클라이언트(
            [{"ts": "1700000000.000001", "user": "U1", "text": 공지}]
        )
        builder = TranscriptBuilder(
            client, RuntimeSettings(), NoticeCatalog({"늦음": 공지}),
            name_resolver=이름표({"U1": "김철수"}),
            identity=fake_identity(), bot_display_name="테스트봇",
        )
        assert builder.thread_transcript("C1", "1700000000.000001", before_ts=None) == ""


class Test추가_메시지도_대화록과_같은_형식이다:
    """대화록과 추가 메시지가 다른 표기를 내면 모델이 같은 사람을 둘로 본다."""

    def test_본문은_원문이고_방향은_머리에_있다(self) -> None:
        from identity_support import fake_identity

        from slack_cli_agent.config.settings import RuntimeSettings
        from slack_cli_agent.observability.notices import NoticeCatalog
        from slack_cli_agent.slack.late_addendum import LateAddendumChecker

        class 대역기록:
            def read_thread(
                self, channel: str, thread_ts: str, limit: int
            ) -> list[Mapping[str, Any]]:
                return [{"ts": "1700000002.000001", "user": "U2", "text": "<@U1> 이것도 봐줘"}]

            def read_history(
                self, channel: str, oldest: float, limit: int
            ) -> list[Mapping[str, Any]] | None:
                return []

        checker = LateAddendumChecker(
            대역기록(), NoticeCatalog(), 이름표({"U1": "김철수", "U2": "박종선"}),
            RuntimeSettings(), identity=fake_identity(), bot_display_name="테스트봇",
        )
        body, _ = checker.check("C1", "1700000000.000001", "1700000001.000001")
        assert "박종선 <@U2> -> 김철수]" in body
        assert "<@U1> 이것도 봐줘" in body


class Test2차_리뷰_지적:
    """2026-09-19 2차 리뷰."""

    def test_해석한_이름에_골뱅이가_붙어_있어도_한_번만_붙인다(self) -> None:
        보기 = MentionRenderer(이름표({}), group_resolver=lambda gid: "@data")
        assert 보기.render("<!subteam^S1> 확인") == "@data 그룹 확인"

    def test_기동_뒤_생긴_그룹은_간격이_지나면_다시_조회한다(self) -> None:
        """한 번 성공하면 다시 안 보던 자리다. 새 그룹이 영영 원본으로 남았다."""
        client = Test그룹_조회_실패_처리.대역(
            {"usergroups": [{"id": "S1", "handle": "data"}]},
            {"usergroups": [{"id": "S1", "handle": "data"}, {"id": "S2", "handle": "ops"}]},
        )
        시계 = [0.0]
        resolver = UserGroupNameResolver(client, retry_interval_sec=10.0, clock=lambda: 시계[0])
        assert resolver.resolve("S1") == "data"
        assert resolver.resolve("S2") == ""
        시계[0] = 20.0
        assert resolver.resolve("S2") == "ops"

    def test_아는_그룹을_물으면_다시_조회하지_않는다(self) -> None:
        client = Test그룹_조회_실패_처리.대역({"usergroups": [{"id": "S1", "handle": "data"}]})
        시계 = [0.0]
        resolver = UserGroupNameResolver(client, retry_interval_sec=10.0, clock=lambda: 시계[0])
        resolver.resolve("S1")
        시계[0] = 20.0
        resolver.resolve("S1")
        assert client.호출 == 1

    def test_응답이_없으면_실패로_본다(self) -> None:
        client = Test그룹_조회_실패_처리.대역(None, {"usergroups": [{"id": "S1", "handle": "data"}]})
        시계 = [0.0]
        resolver = UserGroupNameResolver(client, retry_interval_sec=10.0, clock=lambda: 시계[0])
        assert resolver.resolve("S1") == ""
        시계[0] = 20.0
        assert resolver.resolve("S1") == "data"

    def test_추가_메시지도_그룹_해석기를_쓴다(self) -> None:
        from identity_support import fake_identity

        from slack_cli_agent.config.settings import RuntimeSettings
        from slack_cli_agent.observability.notices import NoticeCatalog
        from slack_cli_agent.slack.late_addendum import LateAddendumChecker

        class 대역기록:
            def read_thread(
                self, channel: str, thread_ts: str, limit: int
            ) -> list[Mapping[str, Any]]:
                return [{"ts": "1700000002.000001", "user": "U2", "text": "<!subteam^S1> 봐줘"}]

            def read_history(
                self, channel: str, oldest: float, limit: int
            ) -> list[Mapping[str, Any]] | None:
                return []

        checker = LateAddendumChecker(
            대역기록(), NoticeCatalog(), 이름표({"U2": "박종선"}),
            RuntimeSettings(), identity=fake_identity(), bot_display_name="테스트봇",
            group_resolver=lambda gid: "데이터팀",
        )
        body, _ = checker.check("C1", "1700000000.000001", "1700000001.000001")
        assert "박종선 <@U2> -> @데이터팀 그룹]" in body
        assert "<!subteam^S1> 봐줘" in body


class Test자기_멘션만_지운다:
    """받은 본문에서 멘션을 전부 지우면 누구를 불렀는지가 사라진다. 원본
    bot.py:4973 도 전부 지운다 - 그 자리를 이 봇에서 바꾼다 (sca-za2a)."""

    def _지우개(self, user_id: str = "U_BOT") -> SelfMentionStripper:
        return SelfMentionStripper(fake_identity(user_id=user_id))

    def test_봇_자신의_멘션을_지운다(self) -> None:
        assert self._지우개().remove_self("<@U_BOT> 배포 상태 알려줘") == "배포 상태 알려줘"

    def test_파이프가_붙은_형태도_지운다(self) -> None:
        assert self._지우개().remove_self("<@U_BOT|신지> 확인") == "확인"

    def test_남의_멘션은_남긴다(self) -> None:
        assert self._지우개().remove_self("<@U_BOT> <@U9> 에게 물어봐") == "<@U9> 에게 물어봐"

    def test_문장_안의_자기_멘션도_지운다(self) -> None:
        """자리에 공백이 둘 남는다. 원본과 같은 동작이고, 공백을 합치면
        코드 블록 안의 들여쓰기까지 바뀐다."""
        assert self._지우개().remove_self("아까 <@U_BOT> 가 말한 것") == "아까  가 말한 것"

    def test_신원을_모르면_선두_멘션만_지운다(self) -> None:
        """본문은 관리 명령이 맞춰 보는 대상이다. 앞에 멘션이 남으면 어느
        명령도 안 맞는다. 뒤의 멘션까지 지우면 누구를 불렀는지가 사라져,
        이 클래스가 막으려던 것을 신원 조회 실패 때마다 다시 하게 된다."""
        지우개 = SelfMentionStripper(fake_identity(user_id=""))
        assert 지우개.remove_self("<@U_BOT> <@U9> !ping") == "<@U9> !ping"

    def test_신원을_모르면_그룹_멘션은_안_건드린다(self) -> None:
        지우개 = SelfMentionStripper(fake_identity(user_id=""))
        assert 지우개.remove_self("<!subteam^S1> 봐줘") == "<!subteam^S1> 봐줘"

    def test_신원_조회가_터져도_요청이_안_깨진다(self) -> None:
        class 터지는신원:
            @property
            def user_id(self) -> str:
                raise RuntimeError("조회 실패")

        assert SelfMentionStripper(터지는신원()).remove_self("<@U_BOT> !ping") == "!ping"


class Test불린_사람_수집:
    """원본 bot.py:2722 readable_mentions 가 본문 대신 만드는 것이다.
    본문을 바꾸면 모델이 그 표기를 답변에 옮겨 적고, 그 이름은 슬랙에서
    링크가 안 걸려 불러도 상대가 못 받는다 (sca-ddkf)."""

    def 수집기(self, **kwargs: Any):
        from slack_cli_agent.slack.mentions import CalledNames

        return CalledNames(이름표({"U1": "김철수", "U2": "박종선"}), **kwargs)

    def test_사용자_멘션에서_이름을_모은다(self) -> None:
        assert self.수집기().called_in("<@U2> 어제 그거 봤어?") == ("박종선",)

    def test_같은_사람을_두_번_안_센다(self) -> None:
        assert self.수집기().called_in("<@U2> <@U2> 봐줘") == ("박종선",)

    def test_나온_차례대로_모은다(self) -> None:
        assert self.수집기().called_in("<@U2> 와 <@U1>") == ("박종선", "김철수")

    def test_못_푼_사용자는_안_넣는다(self) -> None:
        """이름을 모르면 방향을 말할 수 없다. 원본도 빈 이름은 버린다."""
        assert self.수집기().called_in("<@U9> 봐줘") == ()

    def test_그룹은_그룹_표시를_붙인다(self) -> None:
        수집 = self.수집기(group_resolver=lambda gid: "데이터팀")
        assert 수집.called_in("<!subteam^S1> 확인 부탁") == ("@데이터팀 그룹",)

    def test_그룹_해석기가_없으면_안_넣는다(self) -> None:
        assert self.수집기().called_in("<!subteam^S1> 확인") == ()

    def test_여기_모두를_부른_것도_센다(self) -> None:
        assert self.수집기().called_in("<!here> 잠깐만") == ("이 자리 모두",)

    def test_채널_모두를_부른_것도_센다(self) -> None:
        assert self.수집기().called_in("<!channel> 공지") == ("채널 모두",)

    def test_사용자를_그룹보다_앞에_둔다(self) -> None:
        """원본은 사용자, 그룹, 방송 순으로 훑는다."""
        수집 = self.수집기(group_resolver=lambda gid: "데이터팀")
        assert 수집.called_in("<!subteam^S1> 와 <@U1>") == ("김철수", "@데이터팀 그룹")

    def test_부른_사람이_없으면_빈_값이다(self) -> None:
        assert self.수집기().called_in("그냥 혼잣말") == ()

    def test_조회가_터져도_요청을_안_깬다(self) -> None:
        def 터짐(user_id: str) -> str:
            raise RuntimeError("슬랙 오류")

        from slack_cli_agent.slack.mentions import CalledNames

        assert CalledNames(터짐).called_in("<@U1> 봐줘") == ()


class Test안내문과_대화록_형식이_맞는다:
    """안내문은 처음부터 원본 형식을 적고 있었다. 형식을 만드는 쪽만 안
    따라와서, 모델은 없는 화살표를 찾고 지우라고 한 표기를 그대로 쓰라는
    말을 들었다 (sca-ddkf)."""

    def 안내문(self) -> str:
        from pathlib import Path

        import slack_cli_agent

        path = Path(slack_cli_agent.__file__).parent / "assets" / "prompts" / "direction_note.md"
        return path.read_text(encoding="utf-8")

    def test_안내문이_말하는_화살표를_대화록이_실제로_낸다(self) -> None:
        assert "[시각 화자 -> 수신자]" in self.안내문()
        본문 = Test대화록은_본문을_그대로_두고_머리에_방향을_적는다().대화록("<@U2> 봐줘")
        assert " -> " in 본문.splitlines()[0]

    def test_안내문이_쓰라는_표기가_본문에_남아_있다(self) -> None:
        """안내문은 '대화록 본문의 <@...> 표기를 그대로 쓴다' 고 한다.
        본문을 이름으로 바꾸면 쓸 것이 없다."""
        assert "대화록 본문의 <@...> 표기를 그대로 쓴다" in self.안내문()
        본문 = Test대화록은_본문을_그대로_두고_머리에_방향을_적는다().대화록("<@U2> 봐줘")
        assert "<@U2>" in 본문


class Test중복_제거는_이름이_아니라_대상으로_한다:
    """원본은 사용자를 ID 로, 방송을 낱말로 센다(bot.py:2733, 2749). 이름으로
    세면 같은 이름을 쓰는 다른 사람이 한 명으로 합쳐진다 (코덱스 리뷰)."""

    def 수집기(self, 표: dict[str, str], **kwargs: Any):
        from slack_cli_agent.slack.mentions import CalledNames

        return CalledNames(이름표(표), **kwargs)

    def test_이름이_같은_다른_사람을_둘_다_센다(self) -> None:
        수집 = self.수집기({"U1": "김민수", "U2": "김민수"})
        assert 수집.called_in("<@U1> <@U2> 봐줘") == ("김민수", "김민수")

    def test_채널과_전체는_따로_센다(self) -> None:
        """원본은 낱말로 세므로 둘 다 남는다."""
        수집 = self.수집기({})
        assert 수집.called_in("<!channel> <!everyone>") == ("채널 모두", "채널 모두")

    def test_같은_방송을_두_번_쓰면_한_번만_센다(self) -> None:
        수집 = self.수집기({})
        assert 수집.called_in("<!here> <!here>") == ("이 자리 모두",)

    def test_같은_그룹을_두_번_쓰면_한_번만_센다(self) -> None:
        수집 = self.수집기({}, group_resolver=lambda gid: "데이터팀")
        assert 수집.called_in("<!subteam^S1> <!subteam^S1>") == ("@데이터팀 그룹",)


class Test그룹_조회가_안_되면_붙어_있는_표기를_쓴다:
    """원본 group_name 은 조회 실패 시 멘션에 붙어 있는 핸들로 되돌린다
    (bot.py:2707). 이름을 못 얻었다고 방향을 통째로 버리면 안 된다."""

    def 수집기(self, **kwargs: Any):
        from slack_cli_agent.slack.mentions import CalledNames

        return CalledNames(이름표({}), **kwargs)

    def test_해석기가_없으면_붙어_있는_핸들을_쓴다(self) -> None:
        assert self.수집기().called_in("<!subteam^S1|@ops> 봐줘") == ("@ops 그룹",)

    def test_해석에_실패해도_붙어_있는_핸들을_쓴다(self) -> None:
        수집 = self.수집기(group_resolver=lambda gid: "")
        assert 수집.called_in("<!subteam^S1|@ops> 봐줘") == ("@ops 그룹",)

    def test_붙어_있는_표기도_없으면_안_넣는다(self) -> None:
        assert self.수집기().called_in("<!subteam^S1> 봐줘") == ()
