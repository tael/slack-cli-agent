"""Application — Profile 하나에서 전체 객체 그래프를 만드는 조립 계층.

여기서 확인하는 것은 "각 부품이 제대로 동작하는가" 가 아니다. 그것은 각
부품의 시험이 이미 한다. 이 파일이 확인하는 것은 **모듈이 실제로 연결되어
있는가** 다 — 조립이 틀리면 부품 시험은 전부 통과하는데 프로세스는
아무 일도 하지 않는다.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from slack_cli_agent.auth.policy import AccessExtension
from slack_cli_agent.config.profile import Profile
from slack_cli_agent.core.application import Application
from slack_cli_agent.core.context import RequestContext
from slack_cli_agent.core.ingress import IngressService
from slack_cli_agent.core.pipeline import RequestPipeline
from slack_cli_agent.core.worker import Worker
from slack_cli_agent.engine.runner import DirectInvoker, EngineRunner, FallbackEngine, FallbackInvoker
from slack_cli_agent.guard.base import GuardContext, GuardResult, OutputGuard
from slack_cli_agent.plugin.base import BotPlugin
from slack_cli_agent.prompt.sections import CompositionContext, PromptSection, RosterSection
from slack_cli_agent.review.base import ReviewTarget


class FakeSlackClient:
    """슬랙 SDK 대역. 조립이 실제 API 를 부르지 않는 것도 함께 확인한다."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _record(self, name: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((name, kwargs))
        return {"ok": True}

    def __getattr__(self, name: str) -> Any:
        def call(**kwargs: Any) -> dict[str, Any]:
            return self._record(name, **kwargs)

        return call


def write_profile(tmp_path: Path, **overrides: Any) -> Profile:
    binary = tmp_path / "bin" / "fake-engine"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    data: dict[str, Any] = {
        "name": "testbot",
        "display_name": "시험봇",
        "primary_engine": {"type": "claude", "binary": str(binary), "model": "model-a"},
        "state_dir": str(tmp_path / "state"),
        "owner_user_id": "U_OWNER",
        "troubleshoot_channel": "C_TROUBLE",
    }
    data.update(overrides)
    return Profile.from_dict(data)


@pytest.fixture
def client() -> FakeSlackClient:
    return FakeSlackClient()


@pytest.fixture
def profile(tmp_path: Path) -> Profile:
    return write_profile(tmp_path)


@pytest.fixture
def app(profile: Profile, client: FakeSlackClient) -> Application:
    return Application(profile, client)


class RecordingGuard(OutputGuard):
    name = "recording"

    def __init__(self) -> None:
        self.seen: list[GuardContext] = []

    def apply(self, body: str, ctx: GuardContext) -> GuardResult:
        self.seen.append(ctx)
        return GuardResult(body=body, changed=False)


class MarkerSection(PromptSection):
    def applies_to(self, ctx: CompositionContext) -> bool:
        return True

    def render(self, ctx: CompositionContext) -> str:
        return "플러그인 절"


class MarkerExtension(AccessExtension):
    def decide(self, *args: Any, **kwargs: Any) -> Any:
        return None


class MarkerPlugin(BotPlugin):
    name = "marker"

    def __init__(self, guard: OutputGuard, section: PromptSection, extension: AccessExtension) -> None:
        self._guard = guard
        self._section = section
        self._extension = extension

    def output_guards(self):
        return (self._guard,)

    def prompt_sections(self):
        return (self._section,)

    def access_extensions(self):
        return (self._extension,)


class TestStateSetup:
    def test_조립이_상태_디렉터리를_만든다(self, profile: Profile, client: FakeSlackClient) -> None:
        Application(profile, client)
        assert profile.paths.root.is_dir()
        assert profile.paths.prompts.is_dir()

    def test_생성자만으로는_슬랙을_부르지_않는다(self, profile: Profile, client: FakeSlackClient) -> None:
        """조립이 네트워크에 매이면 기동 전 점검도 시험도 슬랙 없이는 못 돈다."""
        Application(profile, client)
        assert client.calls == []

    def test_파이프라인_조립도_슬랙을_부르지_않는다(
        self, profile: Profile, client: FakeSlackClient
    ) -> None:
        Application(profile, client).pipeline()
        assert client.calls == []

    def test_봇_사용자_ID_를_한_번만_조회한다(self, profile: Profile, client: FakeSlackClient) -> None:
        """접수기와 워커가 같은 값을 쓴다. 매번 조회하면 API 호출이 늘고,

        한 번이라도 실패하면 그 부품만 빈 값을 갖게 돼 자기 메시지 판정이
        부품마다 달라진다.
        """
        application = Application(profile, client)
        application.ingress()
        application.worker()
        assert [name for name, _ in client.calls].count("auth_test") == 1

    def test_settings_는_프로필_설정으로_덮인다(self, tmp_path: Path, client: FakeSlackClient) -> None:
        profile = write_profile(tmp_path, settings={"max_concurrent": 3})
        application = Application(profile, client)
        assert application.settings.max_concurrent == 3

    def test_모르는_설정_키는_무시한다(self, tmp_path: Path, client: FakeSlackClient) -> None:
        profile = write_profile(tmp_path, settings={"없는키": 1})
        assert Application(profile, client).settings.max_concurrent == 10

    def test_DB_스키마를_적용한다(self, app: Application) -> None:
        version = app.database.connect().execute("PRAGMA user_version").fetchone()[0]
        assert int(version) > 0


class TestSingletons:
    """같은 것을 두 번 요구하면 같은 객체여야 한다.

    게이트웨이가 매번 새로 만들어지면 핸들러를 등록한 것과 연결을 맺는 것이
    서로 다른 객체가 돼, 이벤트가 들어와도 아무 핸들러도 불리지 않는다.
    """

    def test_게이트웨이는_같은_객체다(self, app: Application) -> None:
        assert app.gateway() is app.gateway()

    def test_접수기는_같은_객체다(self, app: Application) -> None:
        assert app.ingress() is app.ingress()

    def test_파이프라인은_같은_객체다(self, app: Application) -> None:
        assert app.pipeline() is app.pipeline()

    def test_이름_조회기는_같은_객체다(self, app: Application) -> None:
        assert app.names is app.names

    def test_워커는_부를_때마다_새로_만든다(self, app: Application) -> None:
        assert app.worker("w1") is not app.worker("w2")


class TestWiring:
    def test_접수기가_게이트웨이에_핸들러_세_종류를_등록한다(self, app: Application) -> None:
        gateway = app.gateway()
        app.ingress().register(gateway)
        assert gateway.handler_count("app_mention") == 1
        assert gateway.handler_count("message") == 1
        assert gateway.handler_count("reaction_added") == 1

    def test_접수기와_워커가_같은_큐를_쓴다(self, app: Application) -> None:
        ingress = app.ingress()
        worker = app.worker()
        assert ingress._queue is worker._queue

    def test_워커의_처리기는_파이프라인이다(self, app: Application) -> None:
        worker = app.worker()
        assert worker._handler is app.pipeline()

    def test_만들어진_것들의_타입(self, app: Application) -> None:
        assert isinstance(app.ingress(), IngressService)
        assert isinstance(app.pipeline(), RequestPipeline)
        assert isinstance(app.worker(), Worker)

    def test_워커_아이디가_전달된다(self, app: Application) -> None:
        assert app.worker("worker-2")._worker_id == "worker-2"

    def test_파이프라인에_리액션_표식이_들어간다(self, app: Application) -> None:
        """빠지면 처리 중·완료 표식이 아무것도 안 달린다."""
        assert app.pipeline()._reactions is not None

    def test_파이프라인이_소유자_ID_를_안다(self, app: Application) -> None:
        assert app.pipeline()._owner_user_id == "U_OWNER"


class TestEngine:
    def test_폴백이_없으면_1차_엔진만_쓴다(self, app: Application) -> None:
        assert not isinstance(app.engine, FallbackEngine)
        assert app.engine.spec.model == "model-a"

    def test_폴백이_있으면_FallbackEngine_으로_감싼다(self, tmp_path: Path, client: FakeSlackClient) -> None:
        binary = tmp_path / "bin" / "fake-engine"
        profile = write_profile(
            tmp_path,
            fallback_engine={"type": "codex", "binary": str(binary), "model": "model-b"},
        )
        assert isinstance(Application(profile, client).engine, FallbackEngine)

    def test_모르는_엔진_종류는_설정_오류다(self, tmp_path: Path, client: FakeSlackClient) -> None:
        from slack_cli_agent.core.errors import ConfigError

        binary = tmp_path / "bin" / "fake-engine"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("", encoding="utf-8")
        profile = write_profile(
            tmp_path, primary_engine={"type": "없는엔진", "binary": str(binary), "model": "m"}
        )
        with pytest.raises(ConfigError):
            Application(profile, client).engine


class TestNameWiring:
    """화자 표시 이름과 평문 호칭 이름표가 실제로 이어져 있는가.

    원본은 사용자 ID 가 아니라 표시 이름을 프롬프트에 넣었고, 평문으로 적은
    호칭을 진짜 멘션으로 바꾸려면 이름표가 있어야 한다. 연결이 빠지면
    프롬프트에 ID 가 들어가고 호칭 보정이 한 건도 동작하지 않는다.
    """

    def test_이름_조회기가_파이프라인에_들어간다(self, app: Application, client: FakeSlackClient) -> None:
        client.users_info = lambda user: {  # type: ignore[method-assign]
            "user": {"profile": {"real_name": "홍길동"}, "name": "gildong"}
        }
        assert app.pipeline()._name_resolver("U_ASKER") == "홍길동"

    def test_이름표가_파이프라인에_들어간다(self, app: Application, client: FakeSlackClient) -> None:
        client.users_info = lambda user: {  # type: ignore[method-assign]
            "user": {"profile": {"real_name": "홍길동"}, "name": "gildong"}
        }
        app.names.resolve("U_ASKER")
        assert app.pipeline()._mention_table() == {"홍길동": "U_ASKER"}

    def test_소유자_이름이_미리_등록된다(self, app: Application, client: FakeSlackClient) -> None:
        """소유자는 조회 전에도 이름표에 있어야 한다. 원본도 그랬다."""
        client.users_info = lambda user: {  # type: ignore[method-assign]
            "user": {"profile": {"real_name": "주인"}, "name": "owner"}
        }
        assert app.names.resolve("U_OWNER") == "주인"


class TestPlugins:
    def test_플러그인_가드가_파이프라인에_들어간다(self, profile: Profile, client: FakeSlackClient) -> None:
        guard = RecordingGuard()
        plugin = MarkerPlugin(guard, MarkerSection(), MarkerExtension())
        application = Application(profile, client, plugins=[plugin])
        assert guard in application.pipeline()._guards._guards

    def test_플러그인_프롬프트_절이_구성기에_들어간다(self, profile: Profile, client: FakeSlackClient) -> None:
        section = MarkerSection()
        plugin = MarkerPlugin(RecordingGuard(), section, MarkerExtension())
        application = Application(profile, client, plugins=[plugin])
        assert section in application.pipeline()._composer._sections

    def test_플러그인_권한_확장이_정책에_들어간다(self, profile: Profile, client: FakeSlackClient) -> None:
        extension = MarkerExtension()
        plugin = MarkerPlugin(RecordingGuard(), MarkerSection(), extension)
        application = Application(profile, client, plugins=[plugin])
        assert extension in application.access_policy._extensions

    def test_플러그인이_없어도_기본_가드가_있다(self, app: Application) -> None:
        assert app.pipeline()._guards._guards


class TestChannels:
    def test_채널_목록이_비어_있으면_빈_목록이다(self, app: Application) -> None:
        assert app.channel_ids() == []

    def test_등록된_채널_ID_를_돌려준다(self, profile: Profile, client: FakeSlackClient) -> None:
        profile.paths.root.mkdir(parents=True, exist_ok=True)
        profile.paths.channels.write_text(
            json.dumps({"C_ONE": {"name": "하나"}, "C_TWO": {"name": "둘"}}), encoding="utf-8"
        )
        assert sorted(Application(profile, client).channel_ids()) == ["C_ONE", "C_TWO"]


class TestReviewReactions:
    """리액션으로 부검·디버그 추적·서식 점검을 부르는 경로.

    연결이 빠지면 그 세 점검은 코드에만 있고 아무도 부르지 못한다.
    """

    def test_허용_리액션에_점검_이모지_셋이_들어간다(self, app: Application) -> None:
        assert {"dango", "brain", "pencil2"} <= app.ingress()._allowed_reactions

    def test_점검_이모지가_해당_점검을_부른다(self, app: Application, monkeypatch: pytest.MonkeyPatch) -> None:
        called: list[ReviewTarget] = []
        task = app.review_tasks()["dango"]
        monkeypatch.setattr(task, "run", called.append)
        app.on_reaction("dango", "C_ONE", "1.0", "U_OWNER")
        assert len(called) == 1
        assert called[0].channel == "C_ONE"
        assert called[0].ts == "1.0"
        assert called[0].by_user == "U_OWNER"

    def test_모르는_이모지는_아무_점검도_부르지_않는다(self, app: Application) -> None:
        app.on_reaction("thumbsup", "C_ONE", "1.0", "U_OWNER")

    def test_점검이_예외를_내도_밖으로_내보내지_않는다(
        self, app: Application, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """리액션 하나의 실패가 이후 이벤트 처리를 막으면 안 된다."""

        def boom(target: ReviewTarget) -> None:
            raise RuntimeError("점검 실패")

        monkeypatch.setattr(app.review_tasks()["brain"], "run", boom)
        app.on_reaction("brain", "C_ONE", "1.0", "U_OWNER")


class TestConnectionWatch:
    """연결 감시가 실제로 로그를 보고 있는가.

    `SocketErrorWatch` 는 `logging.Handler` 다. 어느 로거에 붙이는지는 조립의
    몫이고, 안 붙이면 소켓이 끊겨도 아무것도 세지 않는다 — 감시가 있는데
    미발동인 것과 감시가 없는 것이 같은 모습이 된다.
    """

    def test_소켓_라이브러리_로거에_감시를_붙인다(self, app: Application) -> None:
        watch = app.connection_watch()
        for name in ("slack_sdk.socket_mode", "slack_bolt"):
            assert watch in logging.getLogger(name).handlers
        app.close()

    def test_감시는_같은_객체다(self, app: Application) -> None:
        assert app.connection_watch() is app.connection_watch()
        app.close()

    def test_붙인_감시가_재연결_로그를_센다(self, app: Application) -> None:
        watch = app.connection_watch()
        logging.getLogger("slack_sdk.socket_mode").error("Failed to connect")
        assert watch.recent_errors() >= 1
        app.close()

    def test_close_가_감시를_로거에서_뗀다(self, app: Application) -> None:
        """떼지 않으면 프로세스가 여럿 뜨고 지는 동안 핸들러가 계속 늘어난다."""
        watch = app.connection_watch()
        app.close()
        assert watch not in logging.getLogger("slack_sdk.socket_mode").handlers

    def test_감시를_안_만들었으면_close_가_아무것도_안_한다(self, app: Application) -> None:
        app.close()


class TestHealth:
    def test_연결_점검기를_만든다(self, app: Application) -> None:
        reasons: list[str] = []
        monitor = app.health_monitor(reasons.append)
        assert monitor._watch is app.connection_watch()
        app.close()

    def test_슬랙이_안_닿으면_DOWN_이다(self, app: Application, client: FakeSlackClient) -> None:
        def boom(**kwargs: Any) -> dict:
            raise RuntimeError("연결 실패")

        client.auth_test = boom  # type: ignore[method-assign]
        monitor = app.health_monitor(lambda reason: None)
        assert monitor.check().kind.name == "DOWN"
        app.close()


class TestClose:
    def test_close_가_DB_연결을_닫는다(self, app: Application) -> None:
        """`connect()` 를 다시 부르면 새 연결이 열리므로 그것으로는 못 본다.

        닫혔는지는 앞서 얻은 커넥션 객체로 확인한다.
        """
        conn = app.database.connect()
        app.close()
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")

    def test_close_를_두_번_불러도_예외가_없다(self, app: Application) -> None:
        app.close()
        app.close()


class TestRoster:
    """계정 핸들과 실명을 잇는 명부가 조립되고 프롬프트에 알려지는가.

    명부 갱신기를 만들기만 하고 어디서도 돌리지 않으면 파일이 생기지 않고,
    프롬프트 절은 늘 빈 문자열을 낸다. 만든 것과 연결된 것은 다르다.
    """

    def test_프롬프트_절에_명부가_들어_있다(self, tmp_path: Path) -> None:
        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        assert any(isinstance(s, RosterSection) for s in app._prompt_sections())

    def test_명부_갱신기가_프로필의_명부_경로에_쓴다(self, tmp_path: Path) -> None:
        profile = write_profile(tmp_path)
        app = Application.from_profile(profile, client=FakeSlackClient())
        assert app.roster_builder()._output_path == profile.roster_file

    def test_명부_갱신기를_매번_새로_만들지_않는다(self, tmp_path: Path) -> None:
        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        assert app.roster_builder() is app.roster_builder()

    def test_갱신_주기가_설정값이다(self, tmp_path: Path) -> None:
        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        assert app.roster_refresher()._interval_sec == app.settings.roster_refresh_sec

    def test_갱신기를_매번_새로_만들지_않는다(self, tmp_path: Path) -> None:
        """주기 실행기가 매번 새로 만들어지면 시작한 객체와 정지시키는 객체가
        달라져, 정지를 요청해도 먼저 시작된 스레드가 계속 돈다."""
        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        assert app.roster_refresher() is app.roster_refresher()

    def test_조립만으로는_슬랙을_부르지_않는다(self, tmp_path: Path) -> None:
        client = FakeSlackClient()
        app = Application.from_profile(write_profile(tmp_path), client=client)
        app.roster_refresher()
        assert not any(name == "users_list" for name, _, _ in client.calls)

    def test_close가_갱신기를_정지시킨다(self, tmp_path: Path) -> None:
        """정지시키지 않으면 데몬 스레드가 프로세스 종료까지 슬랙을 계속 호출한다."""
        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        refresher = app.roster_refresher()
        refresher.start()
        app.close()
        refresher.join(timeout=5)
        assert not refresher.is_running()


class TestSlowReport:
    """느린 요청 보고기가 조립돼 파이프라인에 들어가는가.

    만들기만 하고 파이프라인에 안 넣으면 어떤 요청도 그 경로를 지나지 않는다.
    소요 시간이 기준값을 넘어도 원인 분해가 올라가지 않고, 나중에는 소요
    시간 숫자만 남는다.
    """

    def test_파이프라인이_보고기를_받는다(self, tmp_path: Path) -> None:
        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        assert app.pipeline()._slow_reporter is not None

    def test_보고_채널은_프로필에서_온다(self, tmp_path: Path) -> None:
        profile = write_profile(tmp_path, troubleshoot_channel="C_REPORT")
        app = Application.from_profile(profile, client=FakeSlackClient())
        assert app.pipeline()._slow_reporter._troubleshoot_channel == "C_REPORT"

    def test_보고_채널이_없어도_조립된다(self, tmp_path: Path) -> None:
        """범용 패키지라 보고 채널이 없는 프로필이 정상이다. 그때는 보고기가
        기준값 판정 전에 조용히 넘어간다."""
        profile = write_profile(tmp_path, troubleshoot_channel="")
        app = Application.from_profile(profile, client=FakeSlackClient())
        assert app.pipeline()._slow_reporter._troubleshoot_channel == ""

    def test_기준값이_설정에서_온다(self, tmp_path: Path) -> None:
        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        reporter = app.pipeline()._slow_reporter
        assert reporter._settings.slow_report_sec == app.settings.slow_report_sec

    def test_세션_컨텍스트_계산기가_조립된다(self, tmp_path: Path) -> None:
        """계산기를 안 넣으면 사용량 표에 "세션" 행이 아예 없다. 그러면 그
        세션이 다음 요청에서 컨텍스트 부족으로 막힐지를 보고에서 미리 확인할
        수 없다."""
        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        builder = app.pipeline()._slow_reporter._usage_row_builder
        assert builder._session_context is not None

    def test_컨텍스트_한도가_설정에서_온다(self, tmp_path: Path) -> None:
        profile = write_profile(tmp_path, settings={"context_limit": {"모델A": 200000}})
        app = Application.from_profile(profile, client=FakeSlackClient())
        calculator = app.pipeline()._slow_reporter._usage_row_builder._session_context
        assert calculator.compute("없는세션", model="모델A").limit == 200000

    def test_사용량_노출_채널이_설정에서_온다(self, tmp_path: Path) -> None:
        """프로필 JSON 의 목록이 frozenset 으로 들어가야 이 행이 실제로
        나온다. 기본값은 빈 집합이라 아무 프로필에서도 안 나온다."""
        profile = write_profile(
            tmp_path, troubleshoot_channel="C_REPORT", settings={"owner_only_channels": ["C_REPORT"]},
        )
        app = Application.from_profile(profile, client=FakeSlackClient())
        builder = app.pipeline()._slow_reporter._usage_row_builder
        rows = builder.build(None, "C_REPORT", session_id="없는세션", model="모델A")
        assert [row[0] for row in rows] == ["토큰", "세션"]


class Test엔진환경격리연결:
    """엔진 실행기가 환경 격리 정책을 받는가.

    안 받으면 엔진 하위 프로세스가 부모 환경을 통째로 물려받는다. 슬랙
    토큰과 다른 엔진의 자격증명이 그대로 넘어간다.
    """

    def test_실행기가_정책을_받는다(self, tmp_path: Path) -> None:
        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        assert app.engine_runner._environment_policy is not None

    def test_정책이_1차_엔진_종류를_따른다(self, tmp_path: Path) -> None:
        from slack_cli_agent.engine.environment import ClaudeEnvironmentPolicy

        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        assert isinstance(app.engine_runner._environment_policy, ClaudeEnvironmentPolicy)

    def test_슬랙_토큰은_엔진에_넘어가지_않는다(self, tmp_path: Path) -> None:
        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        built = app.engine_runner._environment_policy.build(
            {"PATH": "/usr/bin", "SLACK_BOT_TOKEN": "비밀", "ANTHROPIC_API_KEY": "비밀"}
        )
        assert "ANTHROPIC_API_KEY" not in built


class Test지울문구가드연결:
    """설정한 문구로 시작하는 줄을 지우는 가드가 조립에 들어가는가.

    가드를 만들어도 파이프라인에 안 넣으면 그 문구가 답변에 그대로 나간다.
    """

    def test_가드가_파이프라인에_들어간다(self, tmp_path: Path) -> None:
        from slack_cli_agent.guard.dropline import ConfiguredLineDropGuard

        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        assert any(isinstance(g, ConfiguredLineDropGuard) for g in app._guards()._guards)

    def test_설정한_문구가_실제로_지워진다(self, tmp_path: Path) -> None:
        app = Application.from_profile(
            write_profile(tmp_path, settings={"dropped_line_heads": ["안내가 켜져 있습니다"]}),
            client=FakeSlackClient(),
        )
        result = app._guards().run("본문입니다\n— 안내가 켜져 있습니다", GuardContext())
        assert "안내가 켜져" not in result.body


class Test함께있는사람연결:
    """참여자 추출기와 프롬프트 섹션이 조립에 들어가는가."""

    def test_섹션이_조립에_들어간다(self, tmp_path: Path) -> None:
        from slack_cli_agent.prompt.sections import PresentPeopleSection

        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        assert any(isinstance(s, PresentPeopleSection) for s in app._prompt_sections())

    def test_파이프라인이_추출기를_받는다(self, tmp_path: Path) -> None:
        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        assert app.pipeline()._participants is not None


class Test발송전재확인연결:
    """발송 직전 스레드 재확인이 조립에 들어가는가.

    안 들어가면 답을 만드는 사이 달린 말을 못 보고 그대로 올린다.
    """

    def test_파이프라인이_재확인기를_받는다(self, tmp_path: Path) -> None:
        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        assert app.pipeline()._late_addendum is not None

    def test_소화_기록을_함께_받는다(self, tmp_path: Path) -> None:
        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        assert app.pipeline()._consumption is not None

    def test_소화_기록은_같은_객체를_공유한다(self, tmp_path: Path) -> None:
        """대기줄과 발송 전 재확인이 같은 기록을 봐야 한다.

        따로 만들면 재확인이 흡수한 말을 대기줄이 또 돌려 같은 답이 두 번
        올라간다.
        """
        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        assert app.consumption is app.pipeline()._consumption


class Test상태기록연결:
    """프로세스 상태를 파일로 내려 적는 경로가 조립에 있는가.

    없으면 밖에서 지금 몇 건이 진행 중인지, 종료 중인지 볼 방법이 없다.
    """

    def test_기록기가_상태_파일에_쓴다(self, tmp_path: Path) -> None:
        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        app.state_snapshot_writer().write()
        written = json.loads(app.profile.paths.state_snapshot.read_text(encoding="utf-8"))
        assert written["inflight"] == 0
        assert "uptime_sec" in written

    def test_진행중_건수가_워커와_같은_카운터다(self, tmp_path: Path) -> None:
        """워커가 따로 카운터를 만들면 상태 파일이 늘 0 으로 남는다."""
        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        assert app.worker().inflight is app.inflight

    def test_주기_실행기를_만든다(self, tmp_path: Path) -> None:
        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        runner = app.state_snapshot_runner()
        assert runner is not None


class Test감시큐연결:
    """지켜보겠다는 약속을 등록할 큐가 파이프라인에 들어가는가.

    안 들어가면 가드가 `[[WATCH:]]` 태그를 뽑아내도 넣을 곳이 없어, 지켜보겠다는
    답만 나가고 실제 확인은 아무도 하지 않는다.
    """

    def test_파이프라인이_감시큐를_받는다(self, tmp_path: Path) -> None:
        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        assert app.pipeline()._watch_queue is not None

    def test_상태기록과_같은_큐를_본다(self, tmp_path: Path) -> None:
        """따로 만들면 등록한 건수와 상태 파일에 적히는 건수가 어긋난다."""
        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        assert app.pipeline()._watch_queue is app.watch_jobs()


class Test감시확인연결:
    """등록된 감시 건을 실제로 확인하는 경로가 조립에 있는가.

    등록만 되고 확인이 없으면 그 큐는 계속 늘어나기만 하고 아무도 보고하지 않는다.
    """

    def test_확인기가_등록과_같은_큐를_본다(self, tmp_path: Path) -> None:
        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        assert app.watch_checker()._queue is app.watch_jobs()

    def test_확인기_조립이_슬랙을_부르지_않는다(
        self, profile: Profile, client: FakeSlackClient
    ) -> None:
        Application(profile, client).watch_checker()
        assert client.calls == []

    def test_주기실행기가_설정한_간격으로_돈다(self, tmp_path: Path) -> None:
        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        runner = app.watch_runner()
        assert runner._interval_sec == app.settings.watch_check_interval_sec

    def test_확인실행은_등록시점_권한을_이어받는다(self, tmp_path: Path, monkeypatch: Any) -> None:
        """확인 프롬프트를 어느 권한으로 돌릴지는 등록할 때 정해진 값이다.

        여기서 일반 권한으로 낮추면 소유자만 쓸 수 있는 도구로 등록된 건이
        확인 단계에서 조회에 실패한다.
        """
        from slack_cli_agent.auth.principal import TrustLevel
        from slack_cli_agent.reliability.watchjobs import WatchJob

        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        # 시스템 프롬프트 본문은 이 시험의 대상이 아니다. 프롬프트 파일 준비가
        # 없으면 조립이 아니라 파일 읽기에서 끝나므로 조립기를 대역으로 바꾼다.
        app._composer = lambda: _프롬프트조립대역()  # type: ignore[method-assign]
        # `engine_runner` 는 호출할 때마다 새 실행기를 만드는 속성이라 그 객체에
        # 대입해도 다음 호출에는 남지 않는다. 클래스 쪽을 바꾼다.
        보낸요청: list[Any] = []
        monkeypatch.setattr(
            EngineRunner, "run",
            lambda self, engine, request: 보낸요청.append(request),
        )

        app._watch_run_check(WatchJob(
            id=1, channel="C1", thread_ts="1.1", condition="배포 확인",
            created_at=0.0, last_run=None, trust=TrustLevel.OWNER,
        ))

        assert 보낸요청[0].trust_level is TrustLevel.OWNER

    def test_확인실행은_새_세션으로_돈다(self, tmp_path: Path, monkeypatch: Any) -> None:
        """확인은 등록보다 한참 뒤에 일어난다. 그 사이 원래 대화 세션은
        만료됐을 수 있어, 이어받기를 시도하면 그 실행 자체가 실패한다."""
        from slack_cli_agent.reliability.watchjobs import WatchJob

        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        # 시스템 프롬프트 본문은 이 시험의 대상이 아니다. 프롬프트 파일 준비가
        # 없으면 조립이 아니라 파일 읽기에서 끝나므로 조립기를 대역으로 바꾼다.
        app._composer = lambda: _프롬프트조립대역()  # type: ignore[method-assign]
        # `engine_runner` 는 호출할 때마다 새 실행기를 만드는 속성이라 그 객체에
        # 대입해도 다음 호출에는 남지 않는다. 클래스 쪽을 바꾼다.
        보낸요청: list[Any] = []
        monkeypatch.setattr(
            EngineRunner, "run",
            lambda self, engine, request: 보낸요청.append(request),
        )

        app._watch_run_check(WatchJob(
            id=1, channel="C1", thread_ts="1.1", condition="배포 확인",
            created_at=0.0, last_run=None,
        ))

        assert 보낸요청[0].resume is False
        assert 보낸요청[0].prompt.find("배포 확인") >= 0


class _프롬프트조립대역:
    def compose(self, ctx: Any) -> str:
        return "시스템 프롬프트"


class Test자기메시지판정:
    """이 봇의 말과 다른 봇의 말을 가르는 판정.

    같은 채널에 다른 슬랙 봇이 함께 답한다. `bot_id` 가 있다는 것만으로 이
    봇의 말로 보면, 다른 봇의 답이 이 봇의 답으로 세어져 되짚기가 실제
    미응답 멘션을 복구 대상에서 뺀다. 원본 `bot.py` 의 `is_self()` 가 같은
    사고로 고쳐진 부분이다.
    """

    @staticmethod
    def _신원을준다(client: FakeSlackClient, user_id: str = "U_ME", bot_id: str = "B_ME") -> None:
        client.auth_test = lambda **kwargs: {  # type: ignore[method-assign]
            "ok": True, "user_id": user_id, "bot_id": bot_id,
        }

    def test_다른봇의_말은_이봇의_말이_아니다(self, app: Application, client: FakeSlackClient) -> None:
        self._신원을준다(client)
        assert app._is_self_message({"bot_id": "B_OTHER", "text": "다른 봇의 답"}) is False
        app.close()

    def test_자기_bot_id_면_이봇의_말이다(self, app: Application, client: FakeSlackClient) -> None:
        self._신원을준다(client)
        assert app._is_self_message({"bot_id": "B_ME", "text": "내 답"}) is True
        app.close()

    def test_bot_id_가_없으면_사용자ID로_판정한다(self, app: Application, client: FakeSlackClient) -> None:
        self._신원을준다(client)
        assert app._is_self_message({"user": "U_ME", "text": "내 말"}) is True
        assert app._is_self_message({"user": "U_HUMAN", "text": "사람 말"}) is False
        app.close()

    def test_신원조회가_실패하면_bot_id_유무로_본다(self, app: Application, client: FakeSlackClient) -> None:
        """판정 근거가 없을 때는 원본과 같이 예전 방식으로 돌아간다."""
        def boom(**kwargs: Any) -> dict:
            raise RuntimeError("조회 실패")

        client.auth_test = boom  # type: ignore[method-assign]
        assert app._is_self_message({"bot_id": "B_ANY"}) is True
        assert app._is_self_message({"user": "U_HUMAN"}) is False
        app.close()

    def test_신원조회는_한번만_한다(self, app: Application, client: FakeSlackClient) -> None:
        """판정마다 조회하면 요청 수만큼 API 호출이 늘어난다."""
        app._is_self_message({"bot_id": "B_X"})
        app._is_self_message({"user": "U_Y"})
        assert [name for name, _ in client.calls].count("auth_test") == 1
        app.close()


class Test연결감시연결:
    """소켓 오류를 세는 것과 그 값을 보고 판정하는 것은 다르다.

    `SocketErrorWatch` 를 로거에 붙여도 `HealthMonitor.check()` 를 부르는
    경로가 없으면 상한을 넘어도 재기동이 발화하지 않는다.
    """

    def test_감시_주기실행기를_만든다(self, app: Application) -> None:
        runner = app.health_runner(lambda reason: None)
        assert runner.thread is None
        app.close()

    def test_주기는_설정값을_따른다(self, tmp_path: Path, client: FakeSlackClient) -> None:
        profile = write_profile(tmp_path, settings={"health_interval_sec": 11})
        application = Application(profile, client)
        runner = application.health_runner(lambda reason: None)
        assert runner._interval_sec == 11
        application.close()

    def test_같은_점검기를_되풀이_쓴다(self, app: Application) -> None:
        """회차마다 새로 만들면 끊김 시작 시각을 잃어 복구 판정이 안 나온다."""
        runner = app.health_runner(lambda reason: None)
        assert app.health_runner(lambda reason: None) is runner
        app.close()

    def test_한_회차가_재기동까지_이어진다(self, tmp_path: Path, client: FakeSlackClient) -> None:
        """실행기를 만드는 것과 그것이 판정과 재기동을 부르는 것은 다르다."""
        profile = write_profile(tmp_path, settings={"socket_error_limit": 1})
        application = Application(profile, client)
        사유: list[str] = []
        runner = application.health_runner(사유.append)
        # 소켓 오류를 상한만큼 기록해 둔다. 감시 핸들러는 로거에 붙어 있다.
        watch = application.connection_watch()
        watch.emit(logging.LogRecord("x", logging.ERROR, "f", 1, "on_error invoked", None, None))
        runner._task()
        assert 사유 and "소켓 오류" in 사유[0]
        application.close()

    def test_재기동은_처리중인_요청이_끝나기를_기다린다(self, tmp_path: Path, client: FakeSlackClient) -> None:
        """진행 중인 건을 버리고 나가면 그 요청은 답 없이 사라진다."""
        profile = write_profile(tmp_path, settings={"shutdown_grace_sec": 1})
        application = Application(profile, client)
        application.inflight.enter()
        나간코드: list[int] = []
        started = time.monotonic()
        application.self_restarter(exit_process=나간코드.append)("소켓 오류 9건")
        assert time.monotonic() - started >= 1.0
        assert 나간코드 == [1]
        application.close()

    def test_처리중인_건이_없으면_바로_나간다(self, app: Application) -> None:
        나간코드: list[int] = []
        started = time.monotonic()
        app.self_restarter(exit_process=나간코드.append)("소켓 오류 9건")
        assert time.monotonic() - started < 0.5
        assert 나간코드 == [1]
        app.close()

    def test_재기동_전에_종료중임을_상태기록에_남긴다(self, app: Application) -> None:
        """상태 파일만 보는 쪽이 멎은 프로세스와 재기동 중인 것을 구분해야 한다."""
        app.self_restarter(exit_process=lambda code: None)("소켓 오류 9건")
        assert app._shutting_down is True
        app.close()


class Test실패건재등록상한연결:
    """접수와 워커가 재시도 상한을 실제로 받는가.

    모듈에 상한을 받는 자리를 만든 것과 조립이 그 값을 넘기는 것은 다르다.
    안 넘기면 계속 실패하는 요청이 재전달마다 되살아난다.
    """

    def test_접수가_설정된_상한을_받는다(self, tmp_path: Path, client: FakeSlackClient) -> None:
        profile = write_profile(tmp_path, settings={"job_max_attempts": 5})
        application = Application(profile, client)
        assert application.ingress()._job_max_attempts == 5
        application.close()


class Test끝난작업정리연결:
    """끝난 작업을 지우는 경로가 있는가.

    `purge_finished()` 는 있지만 부르는 곳이 없으면 jobs 표가 계속 커진다.
    지우는 기준 시각은 되짚기 창보다 커야 한다 — 그보다 짧게 잡으면 되짚기가
    이미 답한 메시지를 미응답으로 보고 다시 등록해 같은 답이 두 번 나간다.
    """

    def test_정리_주기실행기를_만든다(self, app: Application) -> None:
        runner = app.job_purge_runner()
        assert runner.thread is None
        app.close()

    def test_한_회차가_실제로_지운다(self, tmp_path: Path, client: FakeSlackClient) -> None:
        app = Application(write_profile(tmp_path, settings={"job_retention_sec": 0}), client)
        queue = app.queue()
        queue.enqueue(RequestContext(channel="C1", user="U1", ts="1.0", thread_ts="1.0", text="x"))
        job = queue.claim_next("w")
        assert job is not None
        queue.complete(job.id, ok=True)
        app.job_purge_runner()._task()
        assert queue.counts() == {}
        app.close()

    def test_최근에_끝난것은_안_지운다(self, tmp_path: Path, client: FakeSlackClient) -> None:
        profile = write_profile(tmp_path, settings={"job_retention_sec": 86400})
        application = Application(profile, client)
        queue = application.queue()
        queue.enqueue(RequestContext(channel="C1", user="U1", ts="1.0", thread_ts="1.0", text="x"))
        job = queue.claim_next("w")
        assert job is not None
        queue.complete(job.id, ok=True)
        application.job_purge_runner()._task()
        assert queue.counts() != {}
        application.close()

    def test_보관기간은_되짚기_최대창보다_길다(self, app: Application) -> None:
        """짧으면 이미 답한 메시지를 되짚기가 미응답으로 보고 다시 등록한다."""
        assert app.settings.job_retention_sec > app.settings.catchup_max_window_sec
        app.close()


class Test폴백엔진연결:
    """폴백이 실제 요청 경로에서 도는가.

    `FallbackEngine` 을 만드는 것과 요청이 그 `run()` 을 거치는 것은 다르다.
    실행기를 파이프라인에 그대로 넘기면 감싼 의미가 없어, 한도 소진 때 대체
    엔진 전환과 상태 기록이 일어나지 않는다.
    """

    @staticmethod
    def _폴백있는프로필(tmp_path: Path) -> Profile:
        binary = tmp_path / "bin" / "fake-engine"
        return write_profile(
            tmp_path,
            fallback_engine={"type": "codex", "binary": str(binary), "model": "model-b"},
        )

    def test_폴백이_없으면_직접실행을_쓴다(self, app: Application) -> None:
        assert isinstance(app.engine_invoker, DirectInvoker)
        app.close()

    def test_폴백이_있으면_전환판정을_거친다(self, tmp_path: Path, client: FakeSlackClient) -> None:
        application = Application(self._폴백있는프로필(tmp_path), client)
        assert isinstance(application.engine_invoker, FallbackInvoker)
        application.close()

    def test_파이프라인이_그_부품을_받는다(self, tmp_path: Path, client: FakeSlackClient) -> None:
        """만드는 것과 요청 경로에 연결하는 것은 다르다."""
        application = Application(self._폴백있는프로필(tmp_path), client)
        assert application.pipeline()._invoker is application.engine_invoker
        application.close()

    def test_감시확인도_같은_부품을_쓴다(self, tmp_path: Path, client: FakeSlackClient) -> None:
        """확인 실행만 실행기를 직접 부르면 그 경로에서 전환이 안 일어난다."""
        import inspect

        from slack_cli_agent.core import application as 조립모듈

        본문 = inspect.getsource(조립모듈.Application._watch_run_check)
        assert "engine_runner.run(" not in 본문
        assert "engine_invoker.invoke(" in 본문

    def test_같은_부품을_되풀이_쓴다(self, app: Application) -> None:
        assert app.engine_invoker is app.engine_invoker
        app.close()
