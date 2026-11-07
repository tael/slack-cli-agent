"""미등록 채널에서 소유자가 아닌 사람이 봇을 부르거나 초대하면 소유자를 불러
등록을 유도한다. 원본 bot.py 의 notify_unregistered_channel, handle_self_join
(2026-11-05) 을 옮긴 것이다.
"""

from __future__ import annotations

import json
from pathlib import Path

from slack_cli_agent.auth.policy import AccessPolicy
from slack_cli_agent.config.channel import ChannelRegistry
from slack_cli_agent.config.profile import Profile
from slack_cli_agent.core.access import RequestAccess
from slack_cli_agent.core.unregistered import UnregisteredNoticeLog
from slack_cli_agent.observability.audit import BASELINE_KIND_VALUES, IncidentKind
from slack_cli_agent.observability.notices import NoticeCatalog

OWNER = "U_OWNER"
CONTROLLER = "U_CTRL"
BOT = "U_BOT"


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def make_profile(tmp_path: Path, owner: str = OWNER) -> Profile:
    return Profile.from_dict(
        {
            "name": "testbot",
            "display_name": "테스트봇",
            "owner_user_id": owner,
            "controller_user_ids": [CONTROLLER],
            "primary_engine": {"type": "claude", "binary": "claude", "model": "m"},
            "troubleshoot_channel": "C1",
            "state_dir": str(tmp_path / "state"),
        }
    )


def make_access(
    tmp_path: Path,
    replies: list[tuple[str, str, str]],
    clock: Clock | None = None,
    owner: str = OWNER,
    registered: dict | None = None,
    dms: list[str] | None = None,
    permalink=None,
    audits: list[dict] | None = None,
) -> RequestAccess:
    profile = make_profile(tmp_path, owner)
    path = tmp_path / "channels.json"
    path.write_text(json.dumps(registered or {"CREG": {"name": "등록"}}), encoding="utf-8")
    channels = ChannelRegistry(path)

    def reply(channel: str, thread_ts: str, body: str) -> None:
        replies.append((channel, thread_ts, body))

    return RequestAccess(
        policy=AccessPolicy(profile, channels),
        channels=channels,
        channel_name=lambda c: f"이름-{c}",
        notices=NoticeCatalog(),
        reply=reply,
        owner_user_id=owner,
        bot_name="테스트봇",
        self_user_id=lambda: BOT,
        notice_log=UnregisteredNoticeLog(tmp_path / "unreg.json", clock=clock or Clock()),
        notify_owner=(lambda text: dms.append(text) or True) if dms is not None else None,
        permalink=permalink,
        audit=(lambda kind, **f: audits.append({"kind": str(kind), **f})) if audits is not None else None,
    )


class Test소유자_호출:
    def test_미등록_채널_멘션이면_그_스레드에_소유자를_부른다(self, tmp_path: Path) -> None:
        replies: list[tuple[str, str, str]] = []
        access = make_access(tmp_path, replies)

        access.call_owner("C1", "U1", "111.1", "mention")

        assert len(replies) == 1
        channel, thread_ts, body = replies[0]
        assert (channel, thread_ts) == ("C1", "111.1")
        assert f"<@{OWNER}>" in body
        assert "<@U1>님" in body
        assert "테스트봇" in body
        assert "담당자 <@U_OWNER>님이 이 채널에서" in body

    def test_같은_채널은_간격_안에_다시_부르지_않는다(self, tmp_path: Path) -> None:
        replies: list[tuple[str, str, str]] = []
        clock = Clock()
        access = make_access(tmp_path, replies, clock)

        access.call_owner("C1", "U1", "111.1", "mention")
        clock.now += 6 * 3600 - 1
        access.call_owner("C1", "U2", "111.2", "invite")
        assert len(replies) == 1

        clock.now += 1
        access.call_owner("C1", "U2", "111.3", "mention")
        assert len(replies) == 2

    def test_간격은_채널마다_따로_센다(self, tmp_path: Path) -> None:
        replies: list[tuple[str, str, str]] = []
        access = make_access(tmp_path, replies)

        access.call_owner("C1", "U1", "1.0", "mention")
        access.call_owner("C2", "U1", "2.0", "mention")

        assert [r[0] for r in replies] == ["C1", "C2"]

    def test_간격은_재기동해도_유지된다(self, tmp_path: Path) -> None:
        replies: list[tuple[str, str, str]] = []
        clock = Clock()
        make_access(tmp_path, replies, clock).call_owner("C1", "U1", "1.0", "mention")
        make_access(tmp_path, replies, clock).call_owner("C1", "U1", "2.0", "mention")

        assert len(replies) == 1

    def test_등록된_채널과_DM_은_부르지_않는다(self, tmp_path: Path) -> None:
        replies: list[tuple[str, str, str]] = []
        access = make_access(tmp_path, replies)

        access.call_owner("CREG", "U1", "1.0", "mention")
        access.call_owner("D1", "U1", "1.0", "mention")
        access.call_owner("", "U1", "1.0", "mention")

        assert replies == []

    def test_소유자와_제어자_본인은_부르지_않는다(self, tmp_path: Path) -> None:
        replies: list[tuple[str, str, str]] = []
        access = make_access(tmp_path, replies)

        access.call_owner("C1", OWNER, "1.0", "invite")
        access.call_owner("C2", CONTROLLER, "1.0", "mention")

        assert replies == []

    def test_소유자가_없으면_부르지_않는다(self, tmp_path: Path) -> None:
        replies: list[tuple[str, str, str]] = []
        access = make_access(tmp_path, replies, owner="")

        access.call_owner("C1", "U1", "1.0", "mention")

        assert replies == []

    def test_초대_안내에_초대한_사람을_적는다(self, tmp_path: Path) -> None:
        replies: list[tuple[str, str, str]] = []
        access = make_access(tmp_path, replies)

        access.call_owner("C1", "U9", "", "invite")

        channel, thread_ts, body = replies[0]
        assert (channel, thread_ts) == ("C1", "")
        assert "<@U9>님이" in body and "초대했지만" in body

    def test_초대한_사람을_모르면_누군가로_적는다(self, tmp_path: Path) -> None:
        replies: list[tuple[str, str, str]] = []
        access = make_access(tmp_path, replies)

        access.call_owner("C1", "", "", "invite")

        assert "누군가" in replies[0][2]

    def test_게시가_실패해도_예외를_내지_않는다(self, tmp_path: Path) -> None:
        access = make_access(tmp_path, [])

        def broken(channel: str, thread_ts: str, body: str) -> None:
            raise RuntimeError("슬랙 장애")

        access._reply = broken
        access.call_owner("C1", "U1", "1.0", "mention")


class Test봇_입장_알림:
    def test_봇_자신이_초대되면_초대자를_넘긴다(self, tmp_path: Path) -> None:
        access = make_access(tmp_path, [])
        event = {"subtype": "channel_join", "user": BOT, "inviter": "U9", "channel": "C3"}

        assert access.self_join(event) == ("C3", "U9")

    def test_다른_사람_입장은_넘기지_않는다(self, tmp_path: Path) -> None:
        access = make_access(tmp_path, [])
        event = {"subtype": "channel_join", "user": "U7", "channel": "C3"}

        assert access.self_join(event) is None

    def test_입장_알림이_아니면_넘기지_않는다(self, tmp_path: Path) -> None:
        access = make_access(tmp_path, [])

        assert access.self_join({"user": BOT, "channel": "C3", "text": "안녕"}) is None


class Test기록_파일:
    def test_깨진_기록은_빈_기록으로_본다(self, tmp_path: Path) -> None:
        path = tmp_path / "unreg.json"
        path.write_text("{깨짐", encoding="utf-8")
        log = UnregisteredNoticeLog(path, clock=Clock())

        assert log.due("C1") is True
        assert log.due("C1") is False


class TestOwnerDirectMessage:
    """Slack does not notify a non-member mentioned in a private channel, so the
    in-channel call alone may never reach the owner."""

    def test_소유자_개인_대화로도_채널을_알린다(self, tmp_path: Path) -> None:
        replies: list[tuple[str, str, str]] = []
        dms: list[str] = []
        access = make_access(tmp_path, replies, dms=dms)
        access.call_owner("CNEW", "U_X", "1.1", "mention")
        assert len(dms) == 1
        assert "<#CNEW>" in dms[0] and "<@U_X>" in dms[0] and "이름-CNEW" in dms[0]

    def test_초대도_개인_대화로_알린다(self, tmp_path: Path) -> None:
        dms: list[str] = []
        access = make_access(tmp_path, [], dms=dms)
        access.call_owner("CNEW", "U_X", "", "invite")
        assert len(dms) == 1 and "초대" in dms[0]

    def test_간격_안에서는_개인_대화도_보내지_않는다(self, tmp_path: Path) -> None:
        dms: list[str] = []
        access = make_access(tmp_path, [], dms=dms)
        access.call_owner("CNEW", "U_X", "1.1", "mention")
        access.call_owner("CNEW", "U_X", "1.2", "mention")
        assert len(dms) == 1

    def test_채널_게시가_실패해도_개인_대화는_보낸다(self, tmp_path: Path) -> None:
        dms: list[str] = []
        access = make_access(tmp_path, [], dms=dms)
        access._reply = lambda *a: (_ for _ in ()).throw(RuntimeError("not_in_channel"))
        access.call_owner("CNEW", "U_X", "1.1", "mention")
        assert len(dms) == 1


class TestOwnerDirectMessagePermalink:
    """The owner's DM carries a link to the thread where the bot was called, so
    the owner does not have to hunt for it (bot.py notify_unregistered_channel)."""

    def test_개인_대화에_스레드_링크를_붙인다(self, tmp_path: Path) -> None:
        dms: list[str] = []
        asked: list[tuple[str, str]] = []

        def permalink(channel: str, ts: str) -> str:
            asked.append((channel, ts))
            return "https://x.slack.com/archives/CNEW/p11"

        access = make_access(tmp_path, [], dms=dms, permalink=permalink)
        access.call_owner("CNEW", "U_X", "1.1", "mention")
        assert asked == [("CNEW", "1.1")]
        assert "스레드 : https://x.slack.com/archives/CNEW/p11" in dms[0]

    def test_스레드가_없으면_링크를_묻지_않는다(self, tmp_path: Path) -> None:
        dms: list[str] = []
        asked: list[tuple[str, str]] = []
        access = make_access(
            tmp_path, [], dms=dms, permalink=lambda c, t: asked.append((c, t)) or "L"
        )
        access.call_owner("CNEW", "U_X", "", "invite")
        assert asked == [] and "스레드 :" not in dms[0]

    def test_링크_조회가_실패해도_개인_대화는_보낸다(self, tmp_path: Path) -> None:
        dms: list[str] = []

        def broken(channel: str, ts: str) -> str:
            raise RuntimeError("슬랙 장애")

        access = make_access(tmp_path, [], dms=dms, permalink=broken)
        access.call_owner("CNEW", "U_X", "1.1", "mention")
        assert len(dms) == 1 and "스레드 :" not in dms[0]

    def test_링크가_비면_줄을_넣지_않는다(self, tmp_path: Path) -> None:
        dms: list[str] = []
        access = make_access(tmp_path, [], dms=dms, permalink=lambda c, t: "")
        access.call_owner("CNEW", "U_X", "1.1", "mention")
        assert "스레드 :" not in dms[0]
        assert "\n\n" not in dms[0]


class Test사람별_시도_기록:
    """원본 record_unregistered_attempt (bot.py:7385) 와 unregistered_context
    (bot.py:7404). DM 답 생성이 어느 채널 이야기인지 알도록 사람별로 남긴다."""

    def test_시도를_사람별로_채널_이름과_함께_남긴다(self, tmp_path: Path) -> None:
        access = make_access(tmp_path, [])
        access.call_owner("C1", "U1", "1.0", "mention")
        access.call_owner("C2", "U2", "2.0", "invite")

        data = json.loads((tmp_path / "unreg.json").read_text(encoding="utf-8"))
        assert [r["channel"] for r in data["attempts"]["U1"]] == ["C1"]
        assert data["attempts"]["U1"][0]["name"] == "이름-C1"
        assert data["attempts"]["U2"][0]["kind"] == "invite"
        assert set(data["notified"]) == {"C1", "C2"}

    def test_간격_안의_시도도_기록한다(self, tmp_path: Path) -> None:
        access = make_access(tmp_path, [])
        access.call_owner("C1", "U1", "1.0", "mention")
        access.call_owner("C1", "U2", "1.1", "mention")

        data = json.loads((tmp_path / "unreg.json").read_text(encoding="utf-8"))
        assert "U2" in data["attempts"]

    def test_같은_채널은_최근_시도_하나만_남긴다(self, tmp_path: Path) -> None:
        clock = Clock()
        access = make_access(tmp_path, [], clock)
        access.call_owner("C1", "U1", "1.0", "mention")
        clock.now += 60
        access.call_owner("C1", "U1", "1.1", "invite")

        rows = json.loads((tmp_path / "unreg.json").read_text(encoding="utf-8"))["attempts"]["U1"]
        assert [(r["channel"], r["kind"]) for r in rows] == [("C1", "invite")]

    def test_사흘이_지난_시도와_열_건_넘는_시도는_버린다(self, tmp_path: Path) -> None:
        clock = Clock()
        access = make_access(tmp_path, [], clock)
        access.call_owner("COLD", "U1", "0.1", "mention")
        clock.now += 3 * 86400
        for i in range(12):
            access.call_owner(f"C{i}", "U1", "1.0", "mention")

        rows = json.loads((tmp_path / "unreg.json").read_text(encoding="utf-8"))["attempts"]["U1"]
        assert [r["channel"] for r in rows] == [f"C{i}" for i in range(2, 12)]

    def test_기존_파일의_시도_기록을_지우지_않는다(self, tmp_path: Path) -> None:
        path = tmp_path / "unreg.json"
        path.write_text(json.dumps({"notified": {}, "attempts": {"U9": [
            {"channel": "C9", "name": "옛채널", "kind": "mention", "at": 999_000.0}]}}), encoding="utf-8")
        log = UnregisteredNoticeLog(path, clock=Clock())

        assert log.due("C1") is True
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["attempts"]["U9"][0]["channel"] == "C9"

    def test_DM_답_생성에_붙일_최근_채널_목록을_만든다(self, tmp_path: Path) -> None:
        access = make_access(tmp_path, [])
        access.call_owner("C1", "U1", "1.0", "mention")

        text = access.recent_unregistered_context("U1")
        assert text.startswith("<recent_channels>\n")
        assert "<#C1> 이름 이름-C1" in text and "멘션" in text
        assert text.endswith("</recent_channels>\n")
        assert access.recent_unregistered_context("U_NONE") == ""
        assert access.recent_unregistered_context("") == ""

    def test_그새_등록된_채널은_목록에서_뺀다(self, tmp_path: Path) -> None:
        access = make_access(tmp_path, [])
        access.call_owner("C1", "U1", "1.0", "mention")
        access._channels.register("C1", "이름-C1")

        assert access.recent_unregistered_context("U1") == ""

    def test_사흘_지난_시도는_목록에서_뺀다(self, tmp_path: Path) -> None:
        clock = Clock()
        access = make_access(tmp_path, [], clock)
        access.call_owner("C1", "U1", "1.0", "mention")
        clock.now += 3 * 86400

        assert access.recent_unregistered_context("U1") == ""


class Test시도_감사_기록:
    def test_시도마다_kind_와_호출_여부를_감사에_남긴다(self, tmp_path: Path) -> None:
        audits: list[dict] = []
        access = make_access(tmp_path, [], audits=audits)
        access.call_owner("C1", "U1", "1.0", "mention")
        access.call_owner("C1", "U2", "1.1", "invite")

        assert audits == [
            {"kind": "unregistered_mention", "channel": "C1", "user": "U1", "notified": True},
            {"kind": "unregistered_invite", "channel": "C1", "user": "U2", "notified": False},
        ]

    def test_등록_채널과_소유자_시도는_감사하지_않는다(self, tmp_path: Path) -> None:
        audits: list[dict] = []
        access = make_access(tmp_path, [], audits=audits)
        access.call_owner("CREG", "U1", "1.0", "mention")
        access.call_owner("C1", OWNER, "1.0", "mention")
        access.call_owner("D1", "U1", "1.0", "mention")

        assert audits == []

    def test_감사_실패가_안내를_막지_않는다(self, tmp_path: Path) -> None:
        replies: list[tuple[str, str, str]] = []
        access = make_access(tmp_path, replies, audits=[])
        access._audit = lambda kind, **f: (_ for _ in ()).throw(RuntimeError("db"))
        access.call_owner("C1", "U1", "1.0", "mention")

        assert len(replies) == 1

    def test_시도_kind_는_장애_집계에서_뺀다(self) -> None:
        assert IncidentKind.UNREGISTERED_MENTION.value == "unregistered_mention"
        assert IncidentKind.UNREGISTERED_INVITE.value == "unregistered_invite"
        assert {"unregistered_mention", "unregistered_invite"} <= BASELINE_KIND_VALUES
