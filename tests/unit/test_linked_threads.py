"""본문에 걸린 슬랙 링크의 스레드를 프롬프트에 싣는 경로."""

from __future__ import annotations

import pytest

from slack_cli_agent.prompt.linked_threads import LinkedThreadNote
from slack_cli_agent.slack.linked_threads import LinkedThread, LinkedThreadReader
from slack_cli_agent.slack.permalinks import SlackLink, parse_slack_links
from slack_cli_agent.slack.transcript import TranscriptRead

MSG = "https://example.slack.com/archives/C0EXAMPLE01/p1788253544408049"
REPLY = (
    "https://example.slack.com/archives/C0EXAMPLE01/p1788253599111222"
    "?thread_ts=1788253544.408049&cid=C0EXAMPLE01"
)


class Test링크파싱:
    def test_permalink_을_채널과_시각으로_나눈다(self):
        assert parse_slack_links(f"이것 봐 {MSG} 어때") == (
            SlackLink(channel="C0EXAMPLE01", ts="1788253544.408049", thread_ts="1788253544.408049"),
        )

    def test_답글_링크는_thread_ts_쿼리를_부모로_쓴다(self):
        link = parse_slack_links(REPLY)[0]
        assert link.ts == "1788253599.111222"
        assert link.thread_ts == "1788253544.408049"

    def test_같은_스레드를_가리키는_링크는_한_번만_낸다(self):
        assert len(parse_slack_links(f"{MSG}\n{REPLY}")) == 1

    def test_링크가_없으면_빈_결과다(self):
        assert parse_slack_links("링크 없는 본문") == ()
        assert parse_slack_links(None) == ()

    def test_DM_과_비공개_채널_링크도_읽는다(self):
        text = "https://example.slack.com/archives/D07ABC123/p1788253544408049"
        assert parse_slack_links(text)[0].channel == "D07ABC123"


class 기록대역:
    """bodies 에 없는 채널은 조회 실패로, 빈 문자열이 들어 있으면 조회 성공에
    옮길 메시지가 없는 것으로 흉내낸다."""

    def __init__(self, bodies: dict[str, str]) -> None:
        self.bodies = bodies
        self.calls: list[tuple[str, str]] = []

    def read_thread(self, channel: str, thread_ts: str, before_ts: object = None) -> TranscriptRead:
        self.calls.append((channel, thread_ts))
        if channel not in self.bodies:
            return TranscriptRead(body="", read_ok=False)
        return TranscriptRead(body=self.bodies[channel], read_ok=True)


def 리더(bodies: dict[str, str], *, max_links: int = 3) -> tuple[LinkedThreadReader, 기록대역]:
    transcript = 기록대역(bodies)
    reader = LinkedThreadReader(
        transcript=transcript,
        channel_name=lambda channel: {"C0EXAMPLE01": "테스트"}.get(channel, ""),
        max_links=max_links,
    )
    return reader, transcript


class Test링크된스레드읽기:
    def test_읽은_스레드를_채널명과_함께_낸다(self):
        reader, _ = 리더({"C0EXAMPLE01": "[10:00 홍길동]\n안녕"})
        assert reader.of(MSG, self_channel="C099") == (
            LinkedThread(name="테스트", body="[10:00 홍길동]\n안녕", read_ok=True),
        )

    def test_못_읽으면_읽기_실패로_남는다(self):
        reader, _ = 리더({})
        assert reader.of(MSG, self_channel="C099") == (
            LinkedThread(name="테스트", body="", read_ok=False),
        )

    def test_조회는_됐고_옮길_메시지가_없으면_실패가_아니다(self):
        reader, _ = 리더({"C0EXAMPLE01": ""})
        assert reader.of(MSG, self_channel="C099") == (
            LinkedThread(name="테스트", body="", read_ok=True),
        )

    def test_이름을_모르면_채널_ID_를_쓴다(self):
        reader, _ = 리더({"D07ABC123": "본문"})
        text = "https://example.slack.com/archives/D07ABC123/p1788253544408049"
        assert reader.of(text, self_channel="C099")[0].name == "D07ABC123"

    def test_지금_대화_자신을_가리키는_링크는_건너뛴다(self):
        reader, transcript = 리더({"C0EXAMPLE01": "본문"})
        assert reader.of(MSG, self_channel="C0EXAMPLE01") == ()
        assert transcript.calls == []

    def test_상한_개수까지만_읽는다(self):
        bodies = {f"C{i:010d}": "본문" for i in range(5)}
        text = " ".join(
            f"https://example.slack.com/archives/C{i:010d}/p178825354440804{i}" for i in range(5)
        )
        reader, transcript = 리더(bodies, max_links=2)
        assert len(reader.of(text, self_channel="C099")) == 2
        assert len(transcript.calls) == 2

    def test_상한은_찾은_링크에_걸리고_남긴_링크에_걸리지_않는다(self):
        """원본과 같은 순서다. 자기 채널 링크가 앞에 있어도 그 뒤 링크가
        상한 안으로 당겨 들어오지 않는다."""
        bodies = {"C0000000001": "본문", "C0000000002": "본문"}
        text = (
            "https://example.slack.com/archives/C0EXAMPLE01/p1788253544408049 "
            "https://example.slack.com/archives/C0000000001/p1788253544408041 "
            "https://example.slack.com/archives/C0000000002/p1788253544408042"
        )
        reader, transcript = 리더(bodies, max_links=2)
        assert reader.of(text, self_channel="C0EXAMPLE01") == (
            LinkedThread(name="C0000000001", body="본문", read_ok=True),
        )
        assert transcript.calls == [("C0000000001", "1788253544.408041")]

    def test_조회가_예외를_내도_요청을_막지_않는다(self):
        class 터지는대역:
            def read_thread(
                self, channel: str, thread_ts: str, before_ts: object = None
            ) -> TranscriptRead:
                raise RuntimeError("조회 실패")

        reader = LinkedThreadReader(
            transcript=터지는대역(), channel_name=lambda channel: "테스트", max_links=3
        )
        assert reader.of(MSG, self_channel="C099") == (
            LinkedThread(name="테스트", body="", read_ok=False),
        )

    def test_실시간_조회_실패는_언퍼얼_첨부로_본문을_되살린다(self):
        """봇이 초대되지 않은 채널 링크는 read_thread 가 실패한다. 그걸 붙인
        사람의 메시지에 슬랙이 이미 펼친 본문(unfurl)이 있으면 그걸 쓴다(C39)."""
        reader, _ = 리더({})  # 실시간 조회는 모두 실패
        att = [{
            "is_msg_unfurl": True,
            "from_url": MSG,
            "text": "미리보기로 복원한 본문",
        }]
        assert reader.of(MSG, self_channel="C099", attachments=att) == (
            LinkedThread(name="테스트", body="미리보기로 복원한 본문", read_ok=True),
        )

    def test_다른_링크의_첨부는_폴백으로_쓰지_않는다(self):
        reader, _ = 리더({})
        att = [{"is_msg_unfurl": True, "from_url": "https://x/archives/C9/p9", "text": "엉뚱"}]
        assert reader.of(MSG, self_channel="C099", attachments=att) == (
            LinkedThread(name="테스트", body="", read_ok=False),
        )

    def test_실시간_조회가_되면_첨부를_쓰지_않는다(self):
        reader, _ = 리더({"C0EXAMPLE01": "실시간 본문"})
        att = [{"is_msg_unfurl": True, "from_url": MSG, "text": "미리보기 본문"}]
        assert reader.of(MSG, self_channel="C099", attachments=att)[0].body == "실시간 본문"


def 안내(bodies: dict[str, str], *, max_links: int = 3) -> LinkedThreadNote:
    reader, _ = 리더(bodies, max_links=max_links)
    return LinkedThreadNote(reader)


class Test링크된스레드안내:
    def test_링크가_없으면_빈_문자열이다(self):
        assert 안내({}).of("링크 없는 본문", self_channel="C099") == ""

    def test_읽은_본문을_그대로_싣는다(self):
        note = 안내({"C0EXAMPLE01": "[10:00 홍길동]\n안녕"}).of(MSG, self_channel="C099")
        assert "링크된 스레드 : 테스트" in note
        assert "[10:00 홍길동]\n안녕" in note
        assert "읽지 못했다" not in note

    def test_못_읽은_링크는_서술하지_말라고_적는다(self):
        note = 안내({}).of(MSG, self_channel="C099")
        assert "읽지 못했다" in note
        assert "서술하지 않는다" in note

    def test_조회된_빈_스레드를_읽지_못했다고_적지_않는다(self):
        """조회는 성공했는데 옮길 메시지가 없는 경우다. 여기에 '읽지 못했다' 를
        적으면 모델에게 사실이 아닌 것을 알린다(sca-678)."""
        note = 안내({"C0EXAMPLE01": ""}).of(MSG, self_channel="C099")
        assert "읽지 못했다" not in note
        assert "옮길 메시지가 없다" in note

    def test_실시간_실패해도_언퍼얼_첨부_본문을_안내에_싣는다(self):
        """C39: 못 읽은 링크라도 붙인 사람의 미리보기 본문을 그대로 근거로 쓴다."""
        att = [{"is_msg_unfurl": True, "from_url": MSG, "text": "미리보기 본문"}]
        note = 안내({}).of(MSG, self_channel="C099", attachments=att)
        assert "미리보기 본문" in note
        assert "읽지 못했다" not in note


class Test엔진무관_전달:
    """링크된 스레드 본문이 세 엔진 모두에서, 새 세션과 이어받기 양쪽에 전달되는가.

    시스템 프롬프트 절로 두면 codex 이어받기에서 사라진다. codex 는 resume 일 때
    developer_instructions 를 안 붙이기 때문이다. 요청 프롬프트에 두어야 같다.
    """

    @pytest.mark.parametrize("engine_type", ["claude", "codex", "gemini"])
    @pytest.mark.parametrize("resume", [False, True])
    def test_링크_본문이_실행_명령에_들어간다(self, tmp_path, engine_type: str, resume: bool):
        from slack_cli_agent.auth.principal import TrustLevel
        from slack_cli_agent.config.profile import Profile
        from slack_cli_agent.config.settings import RuntimeSettings
        from slack_cli_agent.core.application import Application
        from slack_cli_agent.engine.base import EngineRequest

        마크 = "----- 링크된 스레드 : 테스트 -----"
        profile = Profile.from_dict({
            "name": f"{engine_type}-test",
            "primary_engine": {"type": engine_type, "binary": engine_type, "model": "m"},
            "state_dir": str(tmp_path / "state"),
        })
        engine = Application._default_registry().create(engine_type, profile, RuntimeSettings())
        cmd = engine.build_command(
            EngineRequest(
                prompt=f"안녕\n\n{마크}\n\n본문",
                system_prompt="시스템 지침",
                session_id="11111111-1111-1111-1111-111111111111",
                resume=resume,
                model="m",
                effort="",
                workdir=tmp_path,
                trust_level=TrustLevel.GENERAL,
            )
        )
        assert any(마크 in part for part in cmd)


class Test소유자세션읽기:
    """D5 : 봇이 초대되지 않은 비공개 대화 링크는 봇 토큰으로 읽히지 않는다.
    소유자 턴에서만 소유자 사용자 세션으로 한 번 더 읽는다(마메치
    linked_thread_context, 2026-11-03 요기요_법인영업 링크 사례).
    비소유자 턴에서는 사용자 세션을 부르지 않는다. 부르면 소유자만 볼 수 있는
    대화가 비소유자 답에 실린다."""

    def _reader(self, bot: dict[str, str], owner: dict[str, str]):
        bot_t, owner_t = 기록대역(bot), 기록대역(owner)
        reader = LinkedThreadReader(
            transcript=bot_t,
            channel_name=lambda channel: "",
            owner_transcript=owner_t,
            owner_channel_name=lambda channel: {"C0EXAMPLE01": "비공개-소통"}.get(channel, ""),
        )
        return reader, bot_t, owner_t

    def test_소유자_턴은_봇이_못_읽은_링크를_사용자_세션으로_읽는다(self):
        reader, _, owner_t = self._reader({}, {"C0EXAMPLE01": "[10:00 김]\n할증 요청"})
        assert reader.of(MSG, self_channel="C099", as_owner=True) == (
            LinkedThread(name="비공개-소통", body="[10:00 김]\n할증 요청", read_ok=True),
        )
        assert owner_t.calls == [("C0EXAMPLE01", "1788253544.408049")]

    def test_비소유자_턴은_사용자_세션을_부르지_않는다(self):
        reader, _, owner_t = self._reader({}, {"C0EXAMPLE01": "할증 요청"})
        assert reader.of(MSG, self_channel="C099") == (
            LinkedThread(name="C0EXAMPLE01", body="", read_ok=False),
        )
        assert reader.of(MSG, self_channel="C099", as_owner=False)[0].read_ok is False
        assert owner_t.calls == []

    def test_비소유자_턴은_첨부_미리보기_폴백만_쓴다(self):
        reader, _, owner_t = self._reader({}, {"C0EXAMPLE01": "전체 대화"})
        att = [{"is_msg_unfurl": True, "from_url": MSG, "text": "미리보기 한 건"}]
        assert reader.of(MSG, self_channel="C099", attachments=att)[0].body == "미리보기 한 건"
        assert owner_t.calls == []

    def test_봇이_읽으면_소유자_턴이어도_사용자_세션을_부르지_않는다(self):
        reader, _, owner_t = self._reader({"C0EXAMPLE01": "봇 본문"}, {"C0EXAMPLE01": "x"})
        assert reader.of(MSG, self_channel="C099", as_owner=True)[0].body == "봇 본문"
        assert owner_t.calls == []

    def test_사용자_세션이_없으면_소유자_턴도_종전과_같다(self):
        reader, _ = 리더({})
        assert reader.of(MSG, self_channel="C099", as_owner=True) == (
            LinkedThread(name="테스트", body="", read_ok=False),
        )

    def test_사용자_세션도_실패하면_첨부_폴백으로_간다(self):
        reader, _, _ = self._reader({}, {})
        att = [{"is_msg_unfurl": True, "from_url": MSG, "text": "미리보기"}]
        assert reader.of(MSG, self_channel="C099", attachments=att, as_owner=True)[0].body == "미리보기"

    def test_안내도_소유자_여부를_넘긴다(self):
        reader, _, owner_t = self._reader({}, {"C0EXAMPLE01": "비공개 본문"})
        note = LinkedThreadNote(reader)
        assert "비공개 본문" not in note.of(MSG, self_channel="C099")
        assert owner_t.calls == []
        assert "비공개 본문" in note.of(MSG, self_channel="C099", as_owner=True)
