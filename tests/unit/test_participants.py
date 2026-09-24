"""스레드 참여자 추출.

원본 `thread_people()` 이식. 말한 사람과 멘션으로 불려 들어온 사람을 추린다.
"""

from __future__ import annotations

from typing import Any

from slack_cli_agent.slack.participants import ThreadParticipants


class FakeHistory:
    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.messages = messages
        self.calls: list[tuple[str, str]] = []

    def read_thread(self, channel: str, thread_ts: str, limit: int) -> list[dict[str, Any]]:
        self.calls.append((channel, thread_ts))
        return self.messages


def names(user_id: str) -> str:
    return {"U1": "말한사람", "U2": "불린사람"}.get(user_id, "")


def group_names(group_id: str) -> str:
    return {"S1": "개발팀"}.get(group_id, "")


class Test그룹_호명:
    """그룹으로 불려 들어온 사람이 목록에 아예 안 잡혔다 (sca-ccyv).

    구성원을 펼치지 않고 그룹 한 줄로 넣는다 - 펼치면 50명 그룹 하나가 목록을
    통째로 채우고 이름 조회가 구성원 수만큼 늘어난다. 모델이 그 그룹을 다시
    부를 수 있으면 목적은 채워진다.
    """

    def test_그룹_멘션을_한_줄로_넣는다(self) -> None:
        history = FakeHistory([{"user": "U1", "text": "<!subteam^S1|@개발팀> 봐 주십시오"}])
        people = ThreadParticipants(
            history, names, bot_user_id="UBOT", group_resolver=group_names,
        ).of("C1", "1.0")
        assert people == (("말한사람", "<@U1>"), ("개발팀", "<!subteam^S1>"))

    def test_같은_그룹을_두_번_안_넣는다(self) -> None:
        history = FakeHistory([
            {"user": "U1", "text": "<!subteam^S1>"},
            {"user": "U1", "text": "<!subteam^S1|@개발팀>"},
        ])
        people = ThreadParticipants(
            history, names, bot_user_id="UBOT", group_resolver=group_names,
        ).of("C1", "1.0")
        assert [mention for _, mention in people] == ["<@U1>", "<!subteam^S1>"]

    def test_이름을_못_찾으면_핸들_자리를_비우지_않는다(self) -> None:
        history = FakeHistory([{"user": "U1", "text": "<!subteam^S9>"}])
        people = ThreadParticipants(
            history, names, bot_user_id="UBOT", group_resolver=group_names,
        ).of("C1", "1.0")
        assert people[-1][1] == "<!subteam^S9>"
        assert people[-1][0]

    def test_해석기가_없으면_그룹을_안_넣는다(self) -> None:
        history = FakeHistory([{"user": "U1", "text": "<!subteam^S1>"}])
        people = ThreadParticipants(history, names, bot_user_id="UBOT").of("C1", "1.0")
        assert people == (("말한사람", "<@U1>"),)


class TestThreadParticipants:
    def test_말한_사람과_멘션된_사람을_함께_센다(self) -> None:
        history = FakeHistory([{"user": "U1", "text": "<@U2> 봐 주십시오"}])
        people = ThreadParticipants(history, names, bot_user_id="UBOT").of("C1", "1.0")
        assert people == (("말한사람", "<@U1>"), ("불린사람", "<@U2>"))

    def test_같은_사람을_두_번_세지_않는다(self) -> None:
        history = FakeHistory([
            {"user": "U1", "text": "<@U2>"},
            {"user": "U2", "text": "<@U1>"},
        ])
        people = ThreadParticipants(history, names, bot_user_id="UBOT").of("C1", "1.0")
        assert [mention for _, mention in people] == ["<@U1>", "<@U2>"]

    def test_봇_자신은_넣지_않는다(self) -> None:
        history = FakeHistory([{"user": "U1", "text": "<@UBOT> 안녕"}])
        people = ThreadParticipants(history, names, bot_user_id="UBOT").of("C1", "1.0")
        assert people == (("말한사람", "<@U1>"),)

    def test_봇이_보낸_말의_발신자는_넣지_않는다(self) -> None:
        """bot_id 가 붙은 메시지의 user 는 사람이 아니다.

        원본과 같다. 멘션은 그대로 센다 — 봇이 사람을 부른 것도 그 사람이
        알림을 받아 스레드를 보고 있다는 뜻이다.
        """
        history = FakeHistory([{"user": "UOTHERBOT", "bot_id": "B1", "text": "<@U2>"}])
        people = ThreadParticipants(history, names, bot_user_id="UBOT").of("C1", "1.0")
        assert people == (("불린사람", "<@U2>"),)

    def test_이름을_모르면_그렇게_적는다(self) -> None:
        history = FakeHistory([{"user": "U9", "text": ""}])
        people = ThreadParticipants(history, names, bot_user_id="UBOT").of("C1", "1.0")
        assert people == (("이름 모르는 사람", "<@U9>"),)

    def test_디엠_채널은_읽지_않는다(self) -> None:
        """원본 `recent_thread()` 와 같다. 디엠에는 다른 참여자가 없다."""
        history = FakeHistory([{"user": "U1", "text": ""}])
        people = ThreadParticipants(history, names, bot_user_id="UBOT").of("D1", "1.0")
        assert people == ()
        assert history.calls == []
