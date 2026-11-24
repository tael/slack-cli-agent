"""보안 입력: 소유자만, 만료 거절, 값이 로그·알림 어디에도 없음, 게이트웨이 분기."""

from __future__ import annotations

import json
import logging
from typing import Any

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.slack import secure_input as si
from slack_cli_agent.slack.gateway import SlackGateway

OWNER = "U_OWNER"
SECRET = "pw-Z9!x7#Q2"


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        def call(**kwargs: Any) -> dict[str, Any]:
            self.calls.append((name, kwargs))
            return {"ok": True, "ts": "1.1"}

        return call

    def named(self, name: str) -> list[dict[str, Any]]:
        return [kw for n, kw in self.calls if n == name]


def make(now: float = 1000.0):
    client, saved = FakeClient(), []
    service = si.SecureInput(
        client, OWNER, save=lambda t, v: saved.append((t, v)), now=lambda: now
    )
    return service, client, saved


def click(user: str = OWNER, value: str = "otp|1600") -> dict[str, Any]:
    return {
        "type": "block_actions",
        "user": {"id": user},
        "trigger_id": "T1",
        "channel": {"id": "D1"},
        "message": {"ts": "5.5", "thread_ts": "4.4"},
        "actions": [{"action_id": si.ACTION_OPEN, "value": value}],
    }


def submit(user: str = OWNER, value: str = SECRET, exp: int = 1600) -> dict[str, Any]:
    meta = {"target": "otp", "exp": exp, "channel": "D1", "ts": "5.5", "thread_ts": "4.4"}
    return {
        "type": "view_submission",
        "user": {"id": user},
        "view": {
            "callback_id": si.CALLBACK_ID,
            "private_metadata": json.dumps(meta),
            "state": {"values": {si.BLOCK_ID: {si.INPUT_ACTION_ID: {"value": value}}}},
        },
    }


def test_소유자_클릭은_모달을_연다() -> None:
    service, client, _ = make()
    assert service.handle(click()) is None
    opened = client.named("views_open")
    assert len(opened) == 1 and opened[0]["trigger_id"] == "T1"
    meta = json.loads(opened[0]["view"]["private_metadata"])
    assert meta["target"] == "otp" and SECRET not in json.dumps(opened[0])


def test_소유자_아닌_클릭은_모달을_열지_않는다() -> None:
    service, client, _ = make()
    service.handle(click(user="U_OTHER"))
    assert client.named("views_open") == []


def test_만료된_클릭은_모달_대신_안내만() -> None:
    service, client, _ = make(now=2000.0)
    service.handle(click())
    assert client.named("views_open") == []
    assert client.named("chat_postEphemeral")


def test_제출_값은_저장소로만_가고_알림에_없다() -> None:
    service, client, saved = make()
    assert service.handle(submit()) is None
    assert saved == [("otp", SECRET)]
    assert SECRET not in json.dumps(client.calls)
    assert client.named("chat_update") and client.named("chat_postMessage")


def test_소유자_아닌_제출은_저장하지_않는다() -> None:
    service, _, saved = make()
    answer = service.handle(submit(user="U_OTHER"))
    assert saved == [] and answer["response_action"] == "errors"


def test_만료된_제출은_저장하지_않는다() -> None:
    service, _, saved = make(now=2000.0)
    answer = service.handle(submit())
    assert saved == [] and answer["response_action"] == "errors"


def test_빈_값과_여러_줄은_오류로_돌려준다() -> None:
    service, _, saved = make()
    assert service.handle(submit(value=""))["response_action"] == "errors"
    assert service.handle(submit(value="a\nb"))["response_action"] == "errors"
    assert saved == []


def test_한글_값도_저장한다() -> None:
    service, _, saved = make()
    assert service.handle(submit(value="비밀번호-테스트")) is None
    assert saved == [("otp", "비밀번호-테스트")]


def test_저장_뒤_소유자_명의_멘션_이벤트로_봇을_깨운다() -> None:
    import threading

    got: list[dict[str, Any]] = []
    done = threading.Event()

    def on_saved(event: dict[str, Any]) -> None:
        got.append(event)
        done.set()

    client, saved = FakeClient(), []
    service = si.SecureInput(
        client, OWNER, save=lambda t, v: saved.append((t, v)), now=lambda: 1000.0,
        on_saved=on_saved, bot_user_id=lambda: "UBOT",
    )
    service.handle(submit())
    assert done.wait(2)
    event = got[0]
    assert event["type"] == "app_mention" and event["user"] == OWNER
    assert event["channel"] == "D1" and event["thread_ts"] == "4.4" and event["ts"] == "1.1"
    assert "<@UBOT>" in event["text"] and SECRET not in json.dumps(event)
    assert "bot_id" not in event


def test_제출이_실패하면_봇을_깨우지_않는다() -> None:
    got: list[Any] = []
    client = FakeClient()

    def boom(target: str, value: str) -> None:
        raise RuntimeError("x")

    service = si.SecureInput(client, OWNER, save=boom, now=lambda: 1000.0, on_saved=got.append)
    service.handle(submit())
    assert got == []


def test_저장_실패는_오류로_돌려주고_값을_로그에_남기지_않는다(caplog) -> None:
    client = FakeClient()

    def boom(target: str, value: str) -> None:
        raise RuntimeError("키체인 저장 실패")

    service = si.SecureInput(client, OWNER, save=boom, now=lambda: 1000.0)
    with caplog.at_level(logging.DEBUG):
        answer = service.handle(submit())
    assert answer["response_action"] == "errors"
    assert SECRET not in caplog.text


def test_대상_이름_검증() -> None:
    assert si.valid_target("peagent-google.2fa")
    assert not si.valid_target("a b")
    assert not si.valid_target("../x")
    assert not si.valid_target("")


def test_게이트웨이는_핸들러가_없으면_interactive_를_처리하지_않는다() -> None:
    gateway = SlackGateway(FakeClient())
    assert not gateway.has_interactive_handler()
    assert gateway.handle_interactive({"type": "block_actions"}) is None


def test_게이트웨이는_핸들러_예외를_삼키고_로거를_INFO_로_고정한다(caplog) -> None:
    gateway = SlackGateway(FakeClient())

    def bad(payload: Any) -> None:
        raise RuntimeError("x")

    logging.getLogger("slack_sdk").setLevel(logging.DEBUG)
    gateway.set_interactive_handler(bad)
    assert gateway.handle_interactive({"type": "view_submission"}) is None
    assert logging.getLogger("slack_sdk").level == logging.INFO


def test_기능은_기본으로_꺼져_있다() -> None:
    assert RuntimeSettings().secure_input is False
    assert RuntimeSettings().override({"secure_input": True}).secure_input is True


def test_요청_문구를_바꿀_수_있다() -> None:
    blocks = si.request_blocks("otp", 1600.0, prompt="인증문자를 확인하고 입력해 주세요")
    assert "인증문자를 확인하고 입력해 주세요" in blocks[0]["text"]["text"]
    assert "otp" in blocks[0]["text"]["text"]
