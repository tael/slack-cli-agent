"""G7: listener, welcome, publisher and review write the mametchi kinds
they used to log only to the process log."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from identity_support import fake_identity
from test_bot_interaction import _is_bot, _registry, _ThreadClient
from test_publisher_rewrite_backup import FakeClient, make_publisher
from test_review_base import _ok_response, make_rig
from test_welcome import CH, _Channels, _Client, _Publisher, _response

from slack_cli_agent.config.channel import ChannelRegistry
from slack_cli_agent.guard.mentions import PeerExchangeLimiter
from slack_cli_agent.review.base import ReviewTarget
from slack_cli_agent.slack.gate import ResponseGate
from slack_cli_agent.slack.listener import EventListener
from slack_cli_agent.storage.database import Database
from slack_cli_agent.welcome.service import WelcomeService
from slack_cli_agent.welcome.store import WelcomeStore


class _Rows:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def __call__(self, kind: str, **fields: Any) -> None:
        self.rows.append({"kind": kind, **fields})

    def kinds(self) -> list[str]:
        return [r["kind"] for r in self.rows]


def test_peer_cap_is_recorded() -> None:
    rows = _Rows()
    listener = EventListener(
        _ThreadClient(), ChannelRegistry(Path("/nonexistent.json")), ResponseGate(),
        identity=fake_identity(), peer_limiter=PeerExchangeLimiter(limit=1), audit=rows,
    )
    event = {"channel": "C1", "bot_id": "B_OTHER", "ts": "1.0", "thread_ts": "1.0",
             "text": "<@U_BOT> 다시 봐줘"}
    listener.from_app_mention(event)
    assert listener.from_app_mention(event) is None
    assert rows.kinds() == ["peer_turn_limit"]


def test_message_left_to_the_peer_bot_is_recorded(tmp_path: Path) -> None:
    rows = _Rows()
    client = _ThreadClient([
        {"ts": "1.0", "user": "U1", "text": "<@U0OTHERBOT> 검토해줘"},
        {"ts": "1.1", "bot_id": "B_BOT", "text": "확인할게요"},
    ])
    listener = EventListener(
        client, _registry(tmp_path, {"C1": {"answer_unaddressed": True}}), ResponseGate(),
        identity=fake_identity(), is_bot=_is_bot({"U0OTHERBOT"}), audit=rows,
    )
    event = {"channel": "C1", "user": "U1", "ts": "2.0", "thread_ts": "1.0", "text": "그거 어떻게 됐어?"}
    assert listener.from_message(event) is None
    assert rows.kinds() == ["peer_skipped"]
    assert rows.rows[0]["message_ts"] == "2.0"


def test_gate_skip_in_a_joined_thread_is_recorded(tmp_path: Path) -> None:
    rows = _Rows()
    client = _ThreadClient([
        {"ts": "1.0", "user": "U1", "text": "<@U_BOT> 봐줘"},
        {"ts": "1.1", "bot_id": "B_BOT", "text": "확인했습니다."},
    ])
    listener = EventListener(
        client, _registry(tmp_path, {"C1": {"answer_unaddressed": True}}), ResponseGate(),
        identity=fake_identity(), audit=rows,
    )
    event = {"channel": "C1", "user": "U2", "ts": "2.0", "thread_ts": "1.0", "text": "ㅋㅋ"}
    assert listener.from_message(event) is None
    assert rows.kinds() == ["gate_skipped"]


def _listener_with_marks(tmp_path: Path, messages: list[dict[str, Any]], marks: list[Any], **kw: Any) -> EventListener:
    order = kw.pop("order", marks)
    return EventListener(
        _ThreadClient(messages), _registry(tmp_path, {"C1": {"answer_unaddressed": True}}), ResponseGate(),
        identity=fake_identity(), mark_silent=lambda c, t: order.append(("mark", c, t)), **kw,
    )


_JOINED = [
    {"ts": "1.0", "user": "U1", "text": "<@U_BOT> 봐줘"},
    {"ts": "1.1", "bot_id": "B_BOT", "text": "확인했습니다."},
]
_PEER_THREAD = [
    {"ts": "1.0", "user": "U1", "text": "<@U0OTHERBOT> 검토해줘"},
    {"ts": "1.1", "bot_id": "B_BOT", "text": "확인할게요"},
]


def test_peer_skip_in_a_joined_thread_is_audited_then_marked(tmp_path: Path) -> None:
    log: list[Any] = []

    def audit(kind: str, **_f: Any) -> None:
        log.append(("audit", kind))

    listener = _listener_with_marks(
        tmp_path, _PEER_THREAD, log, is_bot=_is_bot({"U0OTHERBOT"}), audit=audit)
    event = {"channel": "C1", "user": "U1", "ts": "2.0", "thread_ts": "1.0", "text": "그거 어떻게 됐어?"}
    assert listener.from_message(event) is None
    assert log == [("audit", "peer_skipped"), ("mark", "C1", "2.0")]


def test_gate_skip_in_a_joined_thread_is_audited_then_marked(tmp_path: Path) -> None:
    log: list[Any] = []

    def audit(kind: str, **_f: Any) -> None:
        log.append(("audit", kind))

    listener = _listener_with_marks(tmp_path, _JOINED, log, audit=audit)
    event = {"channel": "C1", "user": "U2", "ts": "3.0", "thread_ts": "1.0", "text": "ㅋㅋ"}
    assert listener.from_message(event) is None
    assert log == [("audit", "gate_skipped"), ("mark", "C1", "3.0")]


def test_no_mark_in_a_thread_the_bot_never_joined(tmp_path: Path) -> None:
    marks: list[Any] = []
    listener = _listener_with_marks(
        tmp_path, [{"ts": "1.0", "user": "U1", "text": "저희끼리 얘기"}], marks)
    event = {"channel": "C1", "user": "U2", "ts": "3.0", "thread_ts": "1.0", "text": "ㅋㅋ"}
    assert listener.from_message(event) is None
    assert marks == []


def test_no_peer_mark_when_the_bot_asked_last(tmp_path: Path) -> None:
    marks: list[Any] = []
    thread = [
        {"ts": "1.0", "user": "U1", "text": "<@U0OTHERBOT> 검토해줘"},
        {"ts": "1.1", "bot_id": "B_BOT", "text": "어느 쪽으로 할까요?"},
    ]
    listener = _listener_with_marks(tmp_path, thread, marks, is_bot=_is_bot({"U0OTHERBOT"}))
    listener._thread_state = lambda _c, _t: (True, True, True, False)  # type: ignore[method-assign]
    event = {"channel": "C1", "user": "U1", "ts": "2.0", "thread_ts": "1.0", "text": "ㅇㅇ"}
    assert listener.from_message(event) is not None
    assert marks == []


def test_no_mark_when_the_old_bot_would_have_left_it_to_the_model(tmp_path: Path) -> None:
    """A leading mention of a person is refused by the policy but passes the
    old bot's vocabulary filter; the old bot sent it to the model."""
    marks: list[Any] = []
    listener = _listener_with_marks(tmp_path, _JOINED, marks)
    event = {"channel": "C1", "user": "U2", "ts": "3.0", "thread_ts": "1.0",
             "text": "<@U9PERSON> 이거 확인해줘"}
    assert listener.from_message(event) is None
    assert marks == []


def test_application_wires_one_add_off_the_handler_thread() -> None:
    import tempfile
    import time

    from test_application import FakeSlackClient, write_profile

    from slack_cli_agent.core.application import Application

    class _Client(FakeSlackClient):
        # The fake's catch-all records through a `name` parameter that
        # collides with reactions_add's own `name` kwarg.
        def reactions_add(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(("reactions_add", kwargs))
            return {"ok": True}

    with tempfile.TemporaryDirectory() as d:
        client = _Client()
        app = Application(write_profile(Path(d)), client)
        app.ingress()._listener._silent_mark({"channel": "C1", "ts": "2.0"})
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not any(n == "reactions_add" for n, _ in client.calls):
            time.sleep(0.02)
        time.sleep(0.1)
    reactions = [(n, k) for n, k in client.calls if n.startswith("reactions_")]
    assert reactions == [
        ("reactions_add", {"channel": "C1", "timestamp": "2.0", "name": "zipper_mouth_face"})
    ]


def test_a_failing_silent_mark_does_not_lose_the_skip_row(tmp_path: Path) -> None:
    rows = _Rows()

    def boom(_c: str, _t: str) -> None:
        raise RuntimeError("slack down")

    client = _ThreadClient([
        {"ts": "1.0", "user": "U1", "text": "<@U_BOT> 봐줘"},
        {"ts": "1.1", "bot_id": "B_BOT", "text": "확인했습니다."},
    ])
    listener = EventListener(
        client, _registry(tmp_path, {"C1": {"answer_unaddressed": True}}), ResponseGate(),
        identity=fake_identity(), audit=rows, mark_silent=boom,
    )
    event = {"channel": "C1", "user": "U2", "ts": "2.0", "thread_ts": "1.0", "text": "ㅋㅋ"}
    assert listener.from_message(event) is None
    assert rows.kinds() == ["gate_skipped"]


def _welcome(tmp_path: Path, body: Any, rows: _Rows, publisher: Any = None) -> WelcomeService:
    return WelcomeService(
        store=WelcomeStore(Database(tmp_path / "state.db")), client=_Client(),
        channels=_Channels({CH: {"name": "잡담", "mode": "chat", "welcome": True}}),
        publisher=publisher or _Publisher(), run=lambda _p: body, template=lambda: "<<USER>>",
        self_user_id=lambda: "U_BOT", audit=rows,
    )


def test_welcome_posted_and_failed_are_recorded(tmp_path: Path) -> None:
    from contextlib import nullcontext

    rows = _Rows()
    _welcome(tmp_path, _response("<@UNEW1> 님 반갑습니다."), rows).welcome(CH, "UNEW1", nullcontext)
    _welcome(tmp_path / "b", _response("", ok=False), rows).welcome(CH, "UNEW2", nullcontext)
    assert rows.kinds() == ["welcome_posted", "welcome_failed"]


class _PostingClient(FakeClient):
    def chat_postMessage(self, **kwargs: Any) -> dict:
        self.calls.append(("chat_postMessage", kwargs))
        return {"ok": True, "ts": "1.2"}


def test_notion_link_fix_is_recorded(tmp_path: Path) -> None:
    rows: list[dict[str, Any]] = []
    publisher = make_publisher(_PostingClient(), tmp_path, audit=rows)
    publisher.post("C1", "1.0", "https://app.notion.com/3e2cf5759dc08058a5f9e91c2a3193fe", rich=False)
    assert [r["kind"] for r in rows] == ["notion_link_fixed"]


def test_review_without_split_marker_is_recorded(database) -> None:
    rig = make_rig(database, engine_responses=[_ok_response("구분선없는본문")])
    rig.task.run(ReviewTarget(channel="C1", ts="1.1", by_user="U2", channel_name="채널", rich=True))
    assert [k for k, _ in rig.audit.rows if k == "review_format"] == ["review_format"]
