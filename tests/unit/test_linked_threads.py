"""본문에 걸린 슬랙 링크의 스레드를 프롬프트에 싣는 경로."""

from __future__ import annotations

import pytest

from slack_cli_agent.prompt.linked_threads import LinkedThreadNote
from slack_cli_agent.slack.linked_threads import LinkedThreadReader
from slack_cli_agent.slack.permalinks import SlackLink, parse_slack_links

MSG = "https://vroong.slack.com/archives/C0C1LNABECV/p1788253544408049"
REPLY = (
    "https://vroong.slack.com/archives/C0C1LNABECV/p1788253599111222"
    "?thread_ts=1788253544.408049&cid=C0C1LNABECV"
)


class Test링크파싱:
    def test_permalink_을_채널과_시각으로_나눈다(self):
        assert parse_slack_links(f"이것 봐 {MSG} 어때") == (
            SlackLink(channel="C0C1LNABECV", ts="1788253544.408049", thread_ts="1788253544.408049"),
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
        text = "https://vroong.slack.com/archives/D07ABC123/p1788253544408049"
        assert parse_slack_links(text)[0].channel == "D07ABC123"


class 기록대역:
    def __init__(self, bodies: dict[str, str]) -> None:
        self.bodies = bodies
        self.calls: list[tuple[str, str]] = []

    def thread_transcript(self, channel: str, thread_ts: str, before_ts: object = None) -> str:
        self.calls.append((channel, thread_ts))
        return self.bodies.get(channel, "")


def 리더(bodies: dict[str, str], *, max_links: int = 3) -> tuple[LinkedThreadReader, 기록대역]:
    transcript = 기록대역(bodies)
    reader = LinkedThreadReader(
        transcript=transcript,
        channel_name=lambda channel: {"C0C1LNABECV": "테스트"}.get(channel, ""),
        max_links=max_links,
    )
    return reader, transcript


class Test링크된스레드읽기:
    def test_읽은_스레드를_채널명과_함께_낸다(self):
        reader, _ = 리더({"C0C1LNABECV": "[10:00 김태일]\n안녕"})
        assert reader.of(MSG, self_channel="C099") == (("테스트", "[10:00 김태일]\n안녕"),)

    def test_못_읽으면_본문이_빈_항목으로_남는다(self):
        reader, _ = 리더({})
        assert reader.of(MSG, self_channel="C099") == (("테스트", ""),)

    def test_이름을_모르면_채널_ID_를_쓴다(self):
        reader, _ = 리더({"D07ABC123": "본문"})
        text = "https://vroong.slack.com/archives/D07ABC123/p1788253544408049"
        assert reader.of(text, self_channel="C099")[0][0] == "D07ABC123"

    def test_지금_대화_자신을_가리키는_링크는_건너뛴다(self):
        reader, transcript = 리더({"C0C1LNABECV": "본문"})
        assert reader.of(MSG, self_channel="C0C1LNABECV") == ()
        assert transcript.calls == []

    def test_상한_개수까지만_읽는다(self):
        bodies = {f"C{i:010d}": "본문" for i in range(5)}
        text = " ".join(
            f"https://vroong.slack.com/archives/C{i:010d}/p178825354440804{i}" for i in range(5)
        )
        reader, transcript = 리더(bodies, max_links=2)
        assert len(reader.of(text, self_channel="C099")) == 2
        assert len(transcript.calls) == 2

    def test_상한은_찾은_링크에_걸리고_남긴_링크에_걸리지_않는다(self):
        """원본과 같은 순서다. 자기 채널 링크가 앞에 있어도 그 뒤 링크가
        상한 안으로 당겨 들어오지 않는다."""
        bodies = {"C0000000001": "본문", "C0000000002": "본문"}
        text = (
            "https://vroong.slack.com/archives/C0C1LNABECV/p1788253544408049 "
            "https://vroong.slack.com/archives/C0000000001/p1788253544408041 "
            "https://vroong.slack.com/archives/C0000000002/p1788253544408042"
        )
        reader, transcript = 리더(bodies, max_links=2)
        assert reader.of(text, self_channel="C0C1LNABECV") == (("C0000000001", "본문"),)
        assert transcript.calls == [("C0000000001", "1788253544.408041")]

    def test_조회가_예외를_내도_요청을_막지_않는다(self):
        class 터지는대역:
            def thread_transcript(self, channel: str, thread_ts: str, before_ts: object = None) -> str:
                raise RuntimeError("조회 실패")

        reader = LinkedThreadReader(
            transcript=터지는대역(), channel_name=lambda channel: "테스트", max_links=3
        )
        assert reader.of(MSG, self_channel="C099") == (("테스트", ""),)


def 안내(bodies: dict[str, str], *, max_links: int = 3) -> LinkedThreadNote:
    reader, _ = 리더(bodies, max_links=max_links)
    return LinkedThreadNote(reader)


class Test링크된스레드안내:
    def test_링크가_없으면_빈_문자열이다(self):
        assert 안내({}).of("링크 없는 본문", self_channel="C099") == ""

    def test_읽은_본문을_그대로_싣는다(self):
        note = 안내({"C0C1LNABECV": "[10:00 김태일]\n안녕"}).of(MSG, self_channel="C099")
        assert "링크된 스레드 : 테스트" in note
        assert "[10:00 김태일]\n안녕" in note
        assert "읽지 못했다" not in note

    def test_못_읽은_링크는_서술하지_말라고_적는다(self):
        note = 안내({}).of(MSG, self_channel="C099")
        assert "읽지 못했다" in note
        assert "서술하지 않는다" in note


class Test엔진무관_전달:
    """링크된 스레드 본문이 세 엔진 모두에서, 새 세션과 이어받기 양쪽에 전달되는가.

    시스템 프롬프트 절로 두면 codex 이어받기에서 사라진다. codex 는 resume 일 때
    developer_instructions 를 안 붙이기 때문이다. 요청 프롬프트에 두어야 같다.
    """

    @pytest.mark.parametrize("engine_type", ["claude", "codex", "gemini"])
    @pytest.mark.parametrize("resume", [False, True])
    def test_링크_본문이_실행_명령에_들어간다(self, tmp_path, engine_type: str, resume: bool):
        from slack_cli_agent.config.profile import Profile
        from slack_cli_agent.config.settings import RuntimeSettings
        from slack_cli_agent.core.application import Application
        from slack_cli_agent.engine.base import EngineRequest, TrustLevel

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
