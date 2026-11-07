"""신규 멤버 환영 (G2, 마메치 bot.py:9653-9827).

channels.json 에 "welcome": true 인 채널에서만 입장 알림에 환영 글을 채널
본문으로 한 번 올린다. 슬랙이 같은 입장을 두 번 보내도 한 번만 올린다.
입장 알림을 놓친 사람은 소유자의 "환영 @사람" 명령으로 소급 환영한다.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from test_application import FakeSlackClient, write_profile

from slack_cli_agent.admin.command import AdminContext
from slack_cli_agent.auth.principal import Principal, TrustLevel
from slack_cli_agent.config.channel import ChannelConfig
from slack_cli_agent.core.application import Application
from slack_cli_agent.core.slots import ThreadSlots
from slack_cli_agent.core.spawn import InlineTaskSpawner
from slack_cli_agent.engine.base import EngineResponse
from slack_cli_agent.plugin.hooks import SlotUnavailable, event_callbacks
from slack_cli_agent.storage.database import Database
from slack_cli_agent.welcome.plugin import WelcomeCommand, WelcomePlugin
from slack_cli_agent.welcome.service import WelcomeService, example_line
from slack_cli_agent.welcome.store import WelcomeStore

CH = "C_WELCOME"
JOIN = {"type": "message", "subtype": "channel_join", "channel": CH, "user": "UNEW1", "ts": "1.1"}


class _Channels:
    def __init__(self, configs: dict[str, dict[str, Any]]) -> None:
        self._configs = {k: ChannelConfig.from_dict(k, v) for k, v in configs.items()}

    def get(self, channel_id: str) -> ChannelConfig | None:
        return self._configs.get(channel_id)


class _Client:
    def __init__(self, is_bot: bool = False) -> None:
        self._is_bot = is_bot

    def users_info(self, user: str) -> dict[str, Any]:
        return {"user": {"is_bot": self._is_bot, "profile": {"real_name": "새 사람", "title": "엔지니어"}}}


class _Publisher:
    def __init__(self) -> None:
        self.posts: list[tuple[str, str, str, bool]] = []

    def post(self, channel: str, thread_ts: str, text: str, rich: bool, *, flat: bool = False) -> str | None:
        self.posts.append((channel, thread_ts, text, flat))
        return "9.9"


@contextmanager
def _free() -> Iterator[None]:
    yield


@contextmanager
def _busy() -> Iterator[None]:
    raise SlotUnavailable("no slot")
    yield


def _response(body: str, ok: bool = True) -> EngineResponse:
    return EngineResponse(ok=ok, body=body, session_id=None, model_actual=None, elapsed=0.0, turns=None, usage=None)


def _service(tmp_path: Path, *, bodies: list[EngineResponse] | None = None, is_bot: bool = False,
             configs: dict[str, dict[str, Any]] | None = None) -> tuple[WelcomeService, _Publisher, list[str]]:
    prompts: list[str] = []
    replies = list(bodies or [_response("<@UNEW1> 님 반갑습니다. 와 주셔서 고맙습니다.")] * 5)

    def run(prompt: str) -> EngineResponse:
        prompts.append(prompt)
        return replies.pop(0)

    publisher = _Publisher()
    service = WelcomeService(
        store=WelcomeStore(Database(tmp_path / "state.db")),
        client=_Client(is_bot),
        channels=_Channels(configs or {CH: {"name": "잡담", "mode": "chat", "welcome": True}}),
        publisher=publisher,
        run=run,
        template=lambda: "채널 <<CHANNEL_NAME>> <<CHANNEL_INTRO>> <@<<USER>>> <<PROFILE>>\n<<RECENT>>",
        self_user_id=lambda: "U_BOT",
    )
    return service, publisher, prompts


def _plugin(service: WelcomeService, configs: dict[str, dict[str, Any]] | None = None) -> WelcomePlugin:
    return WelcomePlugin(_Channels(configs or {CH: {"welcome": True}}), lambda: service)


class _Policy:
    def principal_for(self, channel: str, user: str) -> Principal:
        return Principal(user_id=user, channel=channel, trust=TrustLevel.GENERAL, is_direct_message=False)

    def accepts_request(self, principal: Principal) -> bool:
        return False


def _dispatch(plugin: WelcomePlugin, event: dict[str, Any], slots: ThreadSlots | None = None) -> None:
    for event_type, callback in event_callbacks(
        [plugin], InlineTaskSpawner(), slots or ThreadSlots(1), 0.01, policy=_Policy(),
        self_user_id=lambda: "U_BOT",
    ):
        assert event_type == "message"
        callback(event)


def test_입장_알림에_채널_본문으로_한_번_환영한다(tmp_path: Path) -> None:
    service, publisher, prompts = _service(tmp_path)
    _dispatch(_plugin(service), JOIN)

    assert len(publisher.posts) == 1
    channel, thread_ts, text, flat = publisher.posts[0]
    assert (channel, thread_ts, flat) == (CH, "", True)
    assert "UNEW1" in text
    # 채널 이름과 성격, 프로필이 프롬프트에 실린다 (bot.py:9692)
    assert "잡담" in prompts[0] and "수다" in prompts[0] and "엔지니어" in prompts[0]


def test_같은_입장이_두_번_와도_한_번만_올린다(tmp_path: Path) -> None:
    service, publisher, _ = _service(tmp_path)
    plugin = _plugin(service)
    _dispatch(plugin, JOIN)
    # 슬랙 재전송은 ts 가 같고, 다른 소켓 연결의 같은 입장은 ts 가 다를 수 있다
    _dispatch(plugin, {**JOIN, "ts": "1.2"})
    assert len(publisher.posts) == 1


def test_환영을_켜지_않은_채널과_다른_메시지는_넘긴다(tmp_path: Path) -> None:
    service, publisher, _ = _service(tmp_path)
    _dispatch(_plugin(service, {CH: {"welcome": "yes"}}), JOIN)
    _dispatch(_plugin(service), {**JOIN, "subtype": None, "text": "안녕"})
    _dispatch(_plugin(service), {**JOIN, "channel": "C_OTHER"})
    assert publisher.posts == []


def test_봇과_자기_자신의_입장은_환영하지_않는다(tmp_path: Path) -> None:
    service, publisher, _ = _service(tmp_path, is_bot=True)
    _dispatch(_plugin(service), JOIN)
    _dispatch(_plugin(service), {**JOIN, "user": "U_BOT"})
    assert publisher.posts == []


def test_글을_만들지_못하면_표시를_지워_다음에_다시_환영한다(tmp_path: Path) -> None:
    service, publisher, _ = _service(
        tmp_path, bodies=[_response("", ok=False), _response("[[침묵]]"), _response("<@UNEW1> 반가워요")],
    )
    plugin = _plugin(service)
    for ts in ("1.1", "1.2", "1.3"):
        _dispatch(plugin, {**JOIN, "ts": ts})
    assert len(publisher.posts) == 1


def test_최근_환영_첫머리를_채널별로_다음_프롬프트에_싣는다(tmp_path: Path) -> None:
    service, _, prompts = _service(tmp_path, bodies=[
        _response("<@UNEW1> 님 어서 오세요 #잡담 에!"), _response("두 번째"),
    ])
    service.welcome(CH, "UNEW1", _free)
    service.welcome(CH, "UTWO2", _free)
    assert "어서 오세요 이 채널 에!" in prompts[1]
    assert "UNEW1" not in prompts[1].split("\n", 1)[1]
    assert example_line("<@U1> 안녕 <#C1|잡담>\n둘째 줄", "잡담") == "안녕 이 채널"


def test_자리가_없으면_소급_대기열에_넣고_작업이_다시_환영한다(tmp_path: Path) -> None:
    service, publisher, _ = _service(tmp_path)
    plugin = _plugin(service)
    slots = ThreadSlots(1)
    held = slots.acquire(timeout=0)
    _dispatch(plugin, JOIN, slots)
    assert publisher.posts == []
    slots.release(held)

    (job,) = plugin.background_jobs()
    with pytest.raises(SlotUnavailable):
        job.run(_busy)
    assert publisher.posts == []
    job.run(_free)
    assert len(publisher.posts) == 1
    job.run(_free)
    assert len(publisher.posts) == 1


def test_소급_환영_명령은_소유자_전용이고_대기열에_넣는다(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path)
    (command,) = _plugin(service).admin_commands()
    assert isinstance(command, WelcomeCommand)
    assert command.required_trust is TrustLevel.OWNER
    assert command.matches("환영 <@UAAA1> <@UBBB2|비>")
    assert command.matches("환영 <#CXXX1|잡담> <@UAAA1>")
    assert not command.matches("환영 인사 좀 써 줘 <@UAAA1>")
    assert not command.matches("환영")

    owner = Principal(user_id="U_OWNER", channel=CH, trust=TrustLevel.OWNER, is_direct_message=False)
    ctx = AdminContext(principal=owner, channel=CH, thread_ts="", channels=None, profile=None,  # type: ignore[arg-type]
                       text="환영 <#CXXX1|잡담> <@UAAA1> <@UBBB2>")
    result = command.execute(ctx)
    assert result.applied and "2명" in result.message
    assert service.store.take_request() == ("CXXX1", "UAAA1")
    assert service.store.take_request() == ("CXXX1", "UBBB2")
    assert service.store.take_request() is None


def test_애플리케이션이_환영을_훅과_관리_명령으로_건다(tmp_path: Path) -> None:
    app = Application(write_profile(tmp_path), FakeSlackClient(), plugins=[])
    assert app.plugins == ()
    names = {c.name for c in app._admin_router()._commands}
    assert "welcome" in names
    assert any(r.name == "welcome:retro" for r in app.plugin_job_runners("worker"))
    assert app.plugin_job_runners("ingress") == []
    # 환영 턴은 읽기 전용 도구만 받는다. 쓰기 도구가 끼면 입장자 조회가 행동이 된다
    caller_tools = app.tool_policy().tool_list_for(
        Principal(user_id="UOWNER", channel="", trust=TrustLevel.OWNER, is_direct_message=False), readonly=True,
    )
    assert not {"Bash", "Edit", "Write"} & set(caller_tools)
    assert app.welcome_service() is app.welcome_service()
    assert "<<USER>>" in (Path(__file__).parents[2] / "src/slack_cli_agent/assets/prompts/welcome.md").read_text()
