"""Application — Profile 하나에서 전체 객체 그래프를 만드는 조립 계층.

여기서 확인하는 것은 "각 부품이 제대로 동작하는가" 가 아니다. 그것은 각
부품의 시험이 이미 한다. 이 파일이 확인하는 것은 **모듈이 실제로 연결되어
있는가** 다 — 조립이 틀리면 부품 시험은 전부 통과하는데 프로세스는
아무 일도 하지 않는다.
"""

from __future__ import annotations

import json
import logging
import shlex
import sqlite3
import sys
import time
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from slack_cli_agent.auth.policy import AccessExtension
from slack_cli_agent.config.profile import Profile
from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.core.application import Application
from slack_cli_agent.core.context import RequestContext
from slack_cli_agent.core.ingress import IngressService
from slack_cli_agent.core.pipeline import RequestPipeline
from slack_cli_agent.core.spawn import ThreadTaskSpawner
from slack_cli_agent.core.timezones import KST
from slack_cli_agent.core.worker import Worker
from slack_cli_agent.engine.runner import (
    DirectInvoker,
    EngineRunner,
    FallbackEngine,
    FallbackInvoker,
)
from slack_cli_agent.engine.transcript import (
    ClaudeTranscriptReader,
    CodexTranscriptReader,
    NullTranscriptReader,
)
from slack_cli_agent.guard.base import GuardContext, GuardResult, OutputGuard
from slack_cli_agent.learning.analyzer import ProposalAnalyzer
from slack_cli_agent.learning.batch import BatchReport
from slack_cli_agent.plugin.base import BotPlugin
from slack_cli_agent.prompt.composer import SystemPromptComposer
from slack_cli_agent.prompt.sections import CompositionContext, PromptSection, RosterSection
from slack_cli_agent.reliability.connection import ConnectionKind
from slack_cli_agent.reliability.watchresult import WatchOutcome
from slack_cli_agent.review.base import ReviewProgressPort, ReviewTarget
from slack_cli_agent.slack.review_ports import ReviewProgressDisplay


class FakeSlackClient:
    """슬랙 SDK 대역. 조립이 실제 API 를 부르지 않는 것도 함께 확인한다."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        # 실제 슬랙은 conversations.open 에 DM 방 ID 를 돌려준다
        self.open_channel = "D_FAKE"
        self.channel_names: dict[str, str] = {}
        self.channel_info_error = ""

    def _record(self, name: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((name, kwargs))
        if name == "conversations_open":
            return {"ok": True, "channel": {"id": self.open_channel}}
        if name == "chat_postMessage":
            return {"ok": True, "ts": "1.1"}
        return {"ok": True}

    # 시험이 갈아 끼우는 두 개는 이름을 드러낸다. __getattr__ 로만 두면
    # 없는 이름을 갈아 끼워도 아무 표시가 없다.
    def auth_test(self, **kwargs: Any) -> dict[str, Any]:
        return self._record("auth_test", **kwargs)

    def users_info(self, **kwargs: Any) -> dict[str, Any]:
        return self._record("users_info", **kwargs)

    def conversations_info(self, **kwargs: Any) -> dict[str, Any]:
        self._record("conversations_info", **kwargs)
        if self.channel_info_error:
            raise RuntimeError(self.channel_info_error)
        return {"channel": {"name": self.channel_names.get(str(kwargs.get("channel", "")), "")}}

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
        # 판정을 여러 번 유발한다. 조립만으로는 조회가 일어나지 않는다 —
        # 신원은 처음 판정할 때 한 번 받는다.
        application.identity.is_self({"bot_id": "B_X"})
        application.identity.is_mentioned("<@U_X> 질문")
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

    def test_접수기에_스레드_실행기가_배선된다(self, app: Application) -> None:
        """InlineTaskSpawner 가 들어가면 점검이 소켓 처리기를 막는다."""
        assert isinstance(app.ingress()._spawn, ThreadTaskSpawner)

    def test_워커_서비스에_기동_캐치업이_있다(self, app: Application) -> None:
        """재기동 중에 온 멘션은 이벤트도 장애 기록도 안 남는다. 기동 때 한 번 훑지
        않으면 그 요청은 아무 절차로도 안 잡힌다."""
        group = app.worker_services(app.worker())
        assert "startup_catchup" in group.runner_names

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
            _ = Application(profile, client).engine


class TestNameWiring:
    """화자 표시 이름과 평문 호칭 이름표가 실제로 이어져 있는가.

    원본은 사용자 ID 가 아니라 표시 이름을 프롬프트에 넣었고, 평문으로 적은
    호칭을 진짜 멘션으로 바꾸려면 이름표가 있어야 한다. 연결이 빠지면
    프롬프트에 ID 가 들어가고 호칭 보정이 한 건도 동작하지 않는다.
    """

    def test_이름_조회기가_파이프라인에_들어간다(self, app: Application, client: FakeSlackClient) -> None:
        client.users_info = lambda user: {  # type: ignore[method-assign, assignment, misc]
            "user": {"profile": {"real_name": "홍길동"}, "name": "gildong"}
        }
        assert app.pipeline()._name_resolver("U_ASKER") == "홍길동"

    def test_이름표가_파이프라인에_들어간다(self, app: Application, client: FakeSlackClient) -> None:
        client.users_info = lambda user: {  # type: ignore[method-assign, assignment, misc]
            "user": {"profile": {"real_name": "홍길동"}, "name": "gildong"}
        }
        app.names.resolve("U_ASKER")
        assert app.pipeline()._mention_table() == {"홍길동": "U_ASKER"}

    def test_소유자_이름이_미리_등록된다(self, app: Application, client: FakeSlackClient) -> None:
        """소유자는 조회 전에도 이름표에 있어야 한다. 원본도 그랬다."""
        client.users_info = lambda user: {  # type: ignore[method-assign, assignment, misc]
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
        composer = application.pipeline()._composer
        assert isinstance(composer, SystemPromptComposer)
        assert section in composer._sections

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

    def test_점검에_진행_표시가_연결된다(self, app: Application) -> None:
        """부품만 만들고 조립에 안 물리면 점검은 여전히 아무 신호도 안 낸다(sca-tfd)."""
        task = app.review_tasks()["dango"]
        표시 = task._progress
        assert isinstance(표시, ReviewProgressPort), 표시
        assert isinstance(표시, ReviewProgressDisplay), 표시

    def test_점검_셋_모두에_감사가_연결된다(self, app: Application) -> None:
        """한 종류만 연결하면 그 종류 말고는 소요 기록이 안 남는다(sca-fy5)."""
        for task in app.review_tasks().values():
            assert task._audit is app.audit()

    def test_새_세션_ID_를_엔진이_발급한다(self, app: Application) -> None:
        """세션 ID 형식은 엔진마다 다르다. 밖에서 만들면 지금 세 엔진이 모두
        UUID 를 받아 우연히 맞을 뿐이고, 형식이 다른 엔진이 들어오면 일반
        대화가 sca-56y 와 같은 방식으로 깨진다(sca-k6s)."""
        from slack_cli_agent.session.ports import SessionKey

        app.engine.new_session_id = lambda: "엔진이-만든-아이디"  # type: ignore[method-assign]
        결정 = app.pipeline()._sessions.resolve(
            SessionKey(scope="thread", key="C1:1.1"), app.engine.name
        )
        assert 결정.session_id == "엔진이-만든-아이디"

    def test_워커에_감시_큐가_연결된다(self, app: Application) -> None:
        """안 꽂으면 reclaim 이 감시 중인 메시지에 대기 표식을 덧붙인다(sca-o1e)."""
        assert app.worker()._watch_jobs is app.watch_jobs()

    def test_감시_확인기에_감사가_연결된다(self, app: Application) -> None:
        """부품만 만들면 감시가 돌아 본 기록이 한 줄도 안 남는다(sca-j3d)."""
        assert app.watch_checker()._audit is app.audit()

    def test_엔진_실행기에_감사가_연결된다(self, app: Application) -> None:
        """부품만 만들면 보장 기록이 한 줄도 안 남는다(sca-dyb.15 2단계)."""
        assert app.engine_runner._audit is app.audit()

    def test_중단된_점검_보고기가_접수기_서비스에_들어간다(self, app: Application) -> None:
        """부품만 만들면 아무도 안 부른다(sca-9bq)."""
        이름들 = app.ingress_services(lambda 사유: None).runner_names
        assert "stale_review" in 이름들

    def test_서식_점검이_실재하는_교정_명령을_받는다(self, app: Application) -> None:
        """점검 프롬프트가 부르는 명령이 없으면 위반을 찾아도 원 메시지를 못
        고친다. 2026-09-18 부검까지 "리치" 라는 없는 명령이 박혀 있었다."""
        from slack_cli_agent.cli import SlackCliAgent

        명령 = app.rewrite_command()
        assert sys.executable in 명령
        assert "-m slack_cli_agent.cli rewrite" in 명령
        assert "--profile testbot" in 명령
        assert "rewrite" in SlackCliAgent().command_names()

        target = ReviewTarget(
            channel="C_ONE", ts="1.0", by_user="U_OWNER", channel_name="하나", rich=True
        )
        프롬프트 = app.review_tasks()["pencil2"].build_prompt(
            target, transcript="", flagged="답변", question=""
        )
        assert f"{명령} --channel C_ONE --update 1.0" in 프롬프트

    def test_교정_명령이_프로필을_읽은_디렉터리를_지정한다(
        self, profile: Profile, client: FakeSlackClient, tmp_path: Path
    ) -> None:
        """기본 탐색 순서는 환경변수를 보는데, 엔진 하위 프로세스가 그것을
        들고 있다는 보장이 없다."""
        loaded = replace(profile, source_file=tmp_path / "profiles" / "testbot.json")
        명령 = Application(loaded, client).rewrite_command()
        # 경로에 공백이나 비ASCII 가 들어가도 셸에서 한 인자로 남아야 한다
        assert f"--profile-dir {shlex.quote(str(tmp_path / 'profiles'))}" in 명령

    def test_보고기와_점검이_같은_원장을_본다(self, app: Application) -> None:
        """따로 만들면 중단 판정 기한이 갈려 도는 점검을 중단으로 알린다."""
        보고기 = app.stale_review_reporter()
        assert 보고기._ledger is app.review_tasks()["dango"]._ledger

    def test_모르는_이모지는_아무_점검도_부르지_않는다(
        self, app: Application, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """부작용의 부재로만 보면 검출력이 0이다. 호출 자리를 세운다."""
        불린것: list[str] = []
        for 이모지, task in app.review_tasks().items():
            monkeypatch.setattr(task, "run", lambda _t, 이름=이모지: 불린것.append(이름))
        app.on_reaction("thumbsup", "C_ONE", "1.0", "U_OWNER")
        assert 불린것 == []

    def _점검채널앱(self, profile: Profile, client: FakeSlackClient) -> Application:
        profile.paths.root.mkdir(parents=True, exist_ok=True)
        profile.paths.channels.write_text(
            json.dumps({"C_ONE": {"name": "하나", "trusted_users": ["U_TRUSTED"]}}),
            encoding="utf-8",
        )
        return Application(profile, client)

    def _부른것(
        self, app: Application, monkeypatch: pytest.MonkeyPatch, by_user: str
    ) -> list[str]:
        불린것: list[str] = []
        for 이모지, task in app.review_tasks().items():
            monkeypatch.setattr(task, "run", lambda _t, 이름=이모지: 불린것.append(이름))
        app.on_reaction("dango", "C_ONE", "1.0", by_user)
        return 불린것

    def test_채널_신뢰_사용자가_달면_점검이_돈다(
        self, profile: Profile, client: FakeSlackClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = self._점검채널앱(profile, client)
        assert self._부른것(app, monkeypatch, "U_TRUSTED") == ["dango"]

    def test_그_밖의_사람이_달면_점검이_안_돈다(
        self, profile: Profile, client: FakeSlackClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """점검은 소유자 모델과 소유자 권한으로 돌아 비용이 크다 (sca-cg9)."""
        app = self._점검채널앱(profile, client)
        assert self._부른것(app, monkeypatch, "U_STRANGER") == []

    def test_거른_사실을_로그에_남긴다(
        self,
        profile: Profile,
        client: FakeSlackClient,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """안 도는 것과 이벤트가 안 온 것이 같은 모습이면 원인을 못 가린다."""
        app = self._점검채널앱(profile, client)
        with caplog.at_level(logging.INFO):
            self._부른것(app, monkeypatch, "U_STRANGER")
        남은것 = [r.getMessage() for r in caplog.records if "점검 권한이 없다" in r.getMessage()]
        assert len(남은것) == 1, [r.getMessage() for r in caplog.records]
        assert "U_STRANGER" in 남은것[0]

    def test_점검이_예외를_내도_밖으로_내보내지_않는다(
        self, app: Application, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """리액션 하나의 실패가 이후 이벤트 처리를 막으면 안 된다."""

        def boom(target: ReviewTarget) -> None:
            raise RuntimeError("점검 실패")

        monkeypatch.setattr(app.review_tasks()["brain"], "run", boom)
        with caplog.at_level(logging.ERROR):
            app.on_reaction("brain", "C_ONE", "1.0", "U_OWNER")

        # 삼킨 실패는 아예 안 일어난 것과 구분이 안 된다.
        assert any("점검 실패" in r.getMessage() for r in caplog.records)


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
        """예외만 안 나면 통과하는 시험은 close 가 남의 핸들러를 떼도 통과한다."""
        logger = logging.getLogger("slack_sdk.socket_mode")
        남의것 = logging.NullHandler()
        logger.addHandler(남의것)
        try:
            app.close()
            assert 남의것 in logger.handlers
        finally:
            logger.removeHandler(남의것)


class TestHealth:
    def test_연결_점검기를_만든다(self, app: Application) -> None:
        reasons: list[str] = []
        monitor = app.health_monitor(reasons.append)
        assert monitor._watch is app.connection_watch()
        app.close()

    def test_슬랙이_안_닿으면_DOWN_이다(self, app: Application, client: FakeSlackClient) -> None:
        def boom(**kwargs: Any) -> dict:
            raise RuntimeError("연결 실패")

        client.auth_test = boom  # type: ignore[method-assign, assignment, misc]
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
        달라져, 정지를 요청해도 먼저 시작된 스레드가 계속 실행된다."""
        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        assert app.roster_refresher() is app.roster_refresher()

    def test_조립만으로는_슬랙을_부르지_않는다(self, tmp_path: Path) -> None:
        client = FakeSlackClient()
        app = Application.from_profile(write_profile(tmp_path), client=client)
        app.roster_refresher()
        assert not any(name == "users_list" for name, _ in client.calls)

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
        reporter = app.pipeline()._slow_reporter
        assert reporter is not None
        assert reporter._troubleshoot_channel == "C_REPORT"

    def test_보고_채널이_없어도_조립된다(self, tmp_path: Path) -> None:
        """범용 패키지라 보고 채널이 없는 프로필이 정상이다. 그때는 보고기가
        기준값 판정 전에 조용히 넘어간다."""
        profile = write_profile(tmp_path, troubleshoot_channel="")
        app = Application.from_profile(profile, client=FakeSlackClient())
        reporter = app.pipeline()._slow_reporter
        assert reporter is not None
        assert reporter._troubleshoot_channel == ""

    def test_기준값이_설정에서_온다(self, tmp_path: Path) -> None:
        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        reporter = app.pipeline()._slow_reporter
        assert reporter is not None
        assert reporter._settings.slow_report_sec == app.settings.slow_report_sec

    def test_세션_컨텍스트_계산기가_조립된다(self, tmp_path: Path) -> None:
        """계산기를 안 넣으면 사용량 표에 "세션" 행이 아예 없다. 그러면 그
        세션이 다음 요청에서 컨텍스트 부족으로 막힐지를 보고에서 미리 확인할
        수 없다."""
        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        reporter = app.pipeline()._slow_reporter
        assert reporter is not None
        assert reporter._usage_row_builder._session_context is not None

    def test_컨텍스트_한도가_설정에서_온다(self, tmp_path: Path) -> None:
        profile = write_profile(tmp_path, settings={"context_limit": {"모델A": 200000}})
        app = Application.from_profile(profile, client=FakeSlackClient())
        reporter = app.pipeline()._slow_reporter
        assert reporter is not None
        calculator = reporter._usage_row_builder._session_context
        assert calculator is not None
        assert calculator.compute(app.transcript_reader(), "없는세션", model="모델A").limit == 200000

    def test_사용량_노출_채널이_설정에서_온다(self, tmp_path: Path) -> None:
        """프로필 JSON 의 목록이 frozenset 으로 들어가야 이 행이 실제로
        나온다. 기본값은 빈 집합이라 아무 프로필에서도 안 나온다."""
        profile = write_profile(
            tmp_path, troubleshoot_channel="C_REPORT", settings={"owner_only_channels": ["C_REPORT"]},
        )
        app = Application.from_profile(profile, client=FakeSlackClient())
        reporter = app.pipeline()._slow_reporter
        assert reporter is not None
        rows = reporter._usage_row_builder.build(
            None, "C_REPORT", reader=app.transcript_reader(), session_id="없는세션", model="모델A",
        )
        assert [row[0] for row in rows] == ["토큰", "세션"]


class Test응답엔진별_기록_리더:
    """fallback 이 걸린 응답의 보고가 어느 엔진의 기록을 보는가.

    형식도 경로도 엔진마다 다르다. primary 하나로 고정하면 secondary 가 만든
    응답의 구간 분해와 사용량 행이 빈 채로 올라간다.
    """

    def _profile(self, tmp_path: Path) -> Profile:
        return write_profile(
            tmp_path,
            fallback_engine={"type": "codex", "binary": "codex", "model": "model-b"},
        )

    def test_기본값은_primary_리더다(self, tmp_path: Path) -> None:
        app = Application(self._profile(tmp_path), FakeSlackClient())
        assert isinstance(app.transcript_reader(), ClaudeTranscriptReader)

    def test_fallback_엔진_이름이면_그_엔진의_리더다(self, tmp_path: Path) -> None:
        app = Application(self._profile(tmp_path), FakeSlackClient())
        assert isinstance(app.transcript_reader("codex"), CodexTranscriptReader)

    def test_프로필에_없는_엔진이면_빈_리더다(self, tmp_path: Path) -> None:
        """예외를 내면 이미 끝난 요청의 보고가 통째로 날아간다. 그렇다고 primary
        리더로 돌리면 다른 엔진의 숫자를 이 엔진 것으로 내놓는다."""
        app = Application(self._profile(tmp_path), FakeSlackClient())
        # 등록된 이름을 쓰면 이 시험이 그 리더가 생기는 순간 조용히 뜻을 잃는다.
        # 실제로 제미나이로 적어 뒀다가 sca-ebp 에서 그렇게 됐다.
        reader = app.transcript_reader("등록되지않은엔진")
        assert isinstance(reader, NullTranscriptReader)
        assert reader.read("어떤세션") == []

    def test_같은_엔진은_리더를_다시_만들지_않는다(self, tmp_path: Path) -> None:
        """구간 분해와 사용량 행이 같은 기록을 본다. 매번 새로 만들면 같은
        파일을 두 번 읽는다."""
        app = Application(self._profile(tmp_path), FakeSlackClient())
        assert app.transcript_reader("codex") is app.transcript_reader("codex")

    def test_primary_와_fallback_리더가_섞이지_않는다(self, tmp_path: Path) -> None:
        app = Application(self._profile(tmp_path), FakeSlackClient())
        assert app.transcript_reader() is not app.transcript_reader("codex")


class Test엔진환경격리연결:
    """엔진 실행기가 환경 격리 정책을 받는가.

    안 받으면 엔진 하위 프로세스가 부모 환경을 통째로 물려받는다. 슬랙
    토큰과 다른 엔진의 자격증명이 그대로 넘어간다.
    """

    def _fallback_profile(self, tmp_path: Path) -> Profile:
        return write_profile(
            tmp_path, fallback_engine={"type": "codex", "binary": "codex", "model": "model-b"},
        )

    def test_실행기에_정책을_박지_않는다(self, tmp_path: Path) -> None:
        """실행기에 하나를 박으면 2차 엔진도 1차의 home 으로 돈다."""
        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        assert app.engine_runner._environment_policy is None

    def test_정책이_1차_엔진_종류를_따른다(self, tmp_path: Path) -> None:
        from slack_cli_agent.engine.environment import ClaudeEnvironmentPolicy

        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        assert isinstance(app.engine.environment_policy(), ClaudeEnvironmentPolicy)

    def test_2차_엔진은_자기_종류의_정책으로_돈다(self, tmp_path: Path) -> None:
        from slack_cli_agent.engine.environment import CodexEnvironmentPolicy

        app = Application.from_profile(self._fallback_profile(tmp_path), client=FakeSlackClient())
        secondary = app.engine.secondary  # type: ignore[attr-defined]
        assert isinstance(secondary.environment_policy(), CodexEnvironmentPolicy)

    def test_슬랙_토큰은_엔진에_넘어가지_않는다(self, tmp_path: Path) -> None:
        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        built = app.engine.environment_policy().build(
            {"PATH": "/usr/bin", "SLACK_BOT_TOKEN": "비밀", "ANTHROPIC_API_KEY": "비밀"}
        )
        assert "ANTHROPIC_API_KEY" not in built
        assert "SLACK_BOT_TOKEN" not in built


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

    def test_링크된_스레드_추출기가_파이프라인에_붙는다(self, tmp_path: Path) -> None:
        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        assert app.pipeline()._linked_threads is not None




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
        ), WatchOutcome.UNKNOWN)

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
        ), WatchOutcome.UNKNOWN)

        assert 보낸요청[0].resume is False
        assert 보낸요청[0].prompt.find("배포 확인") >= 0

    def test_확인실행은_배치로_표시된다(self, tmp_path: Path, client: FakeSlackClient) -> None:
        """감시 확인은 아무도 기다리지 않는다. 사람이 기다리는 요청으로
        표시하면 폴백 복구 프로브를 대신 써 버려, 그 프로브가 실패했을 때
        직후의 사람 요청이 주기 내내 복구 혜택을 못 받는다.
        """
        from slack_cli_agent.engine.base import CallOrigin
        from slack_cli_agent.reliability.watchjobs import WatchJob

        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        app._composer = lambda: _프롬프트조립대역()  # type: ignore[method-assign]
        받은: list[Any] = []

        def 호출자를_기록한다(request: Any, origin: CallOrigin = CallOrigin.INTERACTIVE) -> Any:
            받은.append(origin)
            return None

        app.engine_invoker.invoke = 호출자를_기록한다  # type: ignore[method-assign, assignment]

        app._watch_run_check(WatchJob(
            id=1, channel="C1", thread_ts="1.1", condition="배포 확인",
            created_at=0.0, last_run=None,
        ), WatchOutcome.UNKNOWN)

        assert 받은 == [CallOrigin.BACKGROUND]

    def test_확인실행의_세션_id는_엔진이_만든다(self, tmp_path: Path, monkeypatch: Any) -> None:
        """직접 만들면 그 형식이 CLI 와 어긋나도 아무도 모른다. 실제로
        uuid4().hex 를 써서 클로드가 "Invalid session ID" 로 매번 거부했고,
        감시 확인이 한 번도 성공한 적이 없었다(sca-56y).
        """
        from slack_cli_agent.reliability.watchjobs import WatchJob

        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        app._composer = lambda: _프롬프트조립대역()  # type: ignore[method-assign]
        받은요청: list[Any] = []
        app._profile.work_root.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(
            type(app.engine), "new_session_id", lambda self: "엔진이-만든-값",
        )
        def 기록하고_참을_낸다(self: Any, request: Any) -> list[str]:
            받은요청.append(request)
            return ["true"]

        monkeypatch.setattr(type(app.engine), "build_command", 기록하고_참을_낸다)

        app._watch_run_check(WatchJob(
            id=1, channel="C1", thread_ts="1.1", condition="배포 확인",
            created_at=0.0, last_run=None,
        ), WatchOutcome.UNKNOWN)

        assert 받은요청[0].session_id == "엔진이-만든-값"

    def test_확인실행에_읽기_도구가_들어간다(self, tmp_path: Path, monkeypatch: Any) -> None:
        """조회하라고 시켜 놓고 조회할 도구를 안 준 적이 있다. allowed_tools
        를 안 넘기면 기본값이 빈 튜플이고, 클로드 엔진은 그것을
        `--allowedTools ""` 로 그대로 넘겨 도구가 하나도 없는 턴이 된다
        (sca-0ab).

        도구 목록이 실제로 강제되는 것은 클로드뿐이다. codex 와 gemini 는
        allowed_tools 를 읽지 않아 이 값이 그쪽에서는 아무 효과가 없다
        (sca-dyb.11). 이 시험이 보장하는 것은 요청 객체의 내용까지다.
        """
        from slack_cli_agent.reliability.watchjobs import WatchJob

        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        app._composer = lambda: _프롬프트조립대역()  # type: ignore[method-assign]
        보낸요청: list[Any] = []
        monkeypatch.setattr(
            EngineRunner, "run",
            lambda self, engine, request: 보낸요청.append(request),
        )

        app._watch_run_check(WatchJob(
            id=1, channel="C1", thread_ts="1.1", condition="배포 확인",
            created_at=0.0, last_run=None,
        ), WatchOutcome.UNKNOWN)

        assert "Read" in 보낸요청[0].allowed_tools

    def test_확인실행에도_실행_보장_요구가_붙는다(self, tmp_path: Path, monkeypatch: Any) -> None:
        """감시 확인은 파이프라인을 안 거친다. 여기서 요구를 안 세우면 이
        경로의 도구 권한만 아무도 강제하지 않는다 (sca-98k)."""
        from slack_cli_agent.engine.capability import ToolRestriction
        from slack_cli_agent.reliability.watchjobs import WatchJob

        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        app._composer = lambda: _프롬프트조립대역()  # type: ignore[method-assign]
        보낸요청: list[Any] = []
        monkeypatch.setattr(
            EngineRunner, "run",
            lambda self, engine, request: 보낸요청.append(request),
        )

        app._watch_run_check(WatchJob(
            id=1, channel="C1", thread_ts="1.1", condition="배포 확인",
            created_at=0.0, last_run=None,
        ), WatchOutcome.UNKNOWN)

        assert 보낸요청[0].requirements.tool_restriction is ToolRestriction.EXACT_ALLOWLIST

    def test_확인실행에도_요청_상관관계_키가_붙는다(self, tmp_path: Path, monkeypatch: Any) -> None:
        """감시 확인도 엔진 실행이라 capability 기록이 남는다. 키가 없으면
        어느 감시의 확인인지 집계에서 안 갈린다 (sca-4ol)."""
        from slack_cli_agent.reliability.watchjobs import WatchJob

        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        app._composer = lambda: _프롬프트조립대역()  # type: ignore[method-assign]
        보낸요청: list[Any] = []
        monkeypatch.setattr(
            EngineRunner, "run",
            lambda self, engine, request: 보낸요청.append(request),
        )

        app._watch_run_check(WatchJob(
            id=7, channel="C1", thread_ts="1.1", condition="배포 확인",
            created_at=0.0, last_run=None,
        ), WatchOutcome.UNKNOWN)

        assert 보낸요청[0].request_id

    def test_확인실행은_소유자_추가_도구와_스킬을_안_준다(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """확인은 조회만 한다. 소유자 권한으로 등록된 감시라도 확인 턴에
        쓰기 도구가 들어가면 그 턴이 새로 일을 벌일 수 있다.
        """
        from slack_cli_agent.auth.principal import TrustLevel
        from slack_cli_agent.reliability.watchjobs import WatchJob

        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        app._composer = lambda: _프롬프트조립대역()  # type: ignore[method-assign]
        app._settings = replace(app.settings, owner_tools=("Bash",))
        보낸요청: list[Any] = []
        monkeypatch.setattr(
            EngineRunner, "run",
            lambda self, engine, request: 보낸요청.append(request),
        )

        app._watch_run_check(WatchJob(
            id=1, channel="C1", thread_ts="1.1", condition="배포 확인",
            created_at=0.0, last_run=None, trust=TrustLevel.OWNER,
        ), WatchOutcome.UNKNOWN)

        assert "Bash" not in 보낸요청[0].allowed_tools
        assert "Skill" not in 보낸요청[0].allowed_tools

    def test_확인실행이_평상시와_같은_읽기_범위를_받는다(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """readable_dirs 가 비면 엔진이 그 경로를 못 읽는다. 평상시 경로
        (pipeline)는 persona 와 prompts 를 넘긴다.
        """
        from slack_cli_agent.reliability.watchjobs import WatchJob

        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        app._composer = lambda: _프롬프트조립대역()  # type: ignore[method-assign]
        보낸요청: list[Any] = []
        monkeypatch.setattr(
            EngineRunner, "run",
            lambda self, engine, request: 보낸요청.append(request),
        )

        app._watch_run_check(WatchJob(
            id=1, channel="C1", thread_ts="1.1", condition="배포 확인",
            created_at=0.0, last_run=None,
        ), WatchOutcome.UNKNOWN)

        assert 보낸요청[0].readable_dirs == app.pipeline().readable_dirs


class _프롬프트조립대역:
    """받은 맥락을 남긴다. 무엇을 넘겼는지 보려면 결과 문자열만으로는 안 된다."""

    def __init__(self) -> None:
        self.받은맥락: list[Any] = []

    def compose(self, ctx: Any) -> str:
        self.받은맥락.append(ctx)
        return "시스템 프롬프트"

    def compose_with_report(self, ctx: Any) -> tuple[str, Any]:
        """실물과 같은 진입점을 갖는다. 파이프라인이 부르는 것이 이쪽이다."""
        from slack_cli_agent.prompt.composer import PromptBudgetReport

        text = self.compose(ctx)
        return text, PromptBudgetReport(
            budget_bytes=None,
            bytes_before=len(text.encode("utf-8")),
            bytes_after=len(text.encode("utf-8")),
            omitted_document_count=0,
            omitted_document_bytes=0,
        )


class Test자기메시지판정:
    """이 봇의 말과 다른 봇의 말을 가르는 판정.

    같은 채널에 다른 슬랙 봇이 함께 답한다. `bot_id` 가 있다는 것만으로 이
    봇의 말로 보면, 다른 봇의 답이 이 봇의 답으로 세어져 캐치업이 실제
    미응답 멘션을 복구 대상에서 뺀다. 원본 `bot.py` 의 `is_self()` 가 같은
    사고로 고쳐진 부분이다.
    """

    @staticmethod
    def _신원을준다(client: FakeSlackClient, user_id: str = "U_ME", bot_id: str = "B_ME") -> None:
        client.auth_test = lambda **kwargs: {  # type: ignore[method-assign, assignment, misc]
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

    @staticmethod
    def _조회가실패한다(client: FakeSlackClient) -> None:
        def boom(**kwargs: Any) -> dict:
            client.calls.append(("auth_test", kwargs))
            raise RuntimeError("조회 실패")

        client.auth_test = boom  # type: ignore[method-assign, assignment, misc]

    def test_신원을_모르면_어떤_봇의_말도_이봇의_말로_보지_않는다(
        self, app: Application, client: FakeSlackClient
    ) -> None:
        """판정 근거가 없을 때 `bot_id` 유무로 보면 다른 봇의 답이 이 봇의 답이 된다.

        원본 `bot.py` 는 이 경우 `bool(msg.get("bot_id"))` 로 돌아가는데, 그것이
        바로 2026-09-02 에 사고를 낸 예전 방식이다. 조회가 한 번 실패한 직후
        캐치업이 돌면 같은 오판이 그대로 재현된다.

        오판의 두 방향 중 방어가 있는 쪽으로 기운다. 이 봇의 답을 남의 것으로
        보면 캐치업이 재등록을 시도하지만 jobs 표의 `(channel, message_ts)`
        유일 제약이 그 중복을 막는다. 반대 방향은 막는 것이 없어 미응답 멘션이
        복구 대상에서 빠진 채 그대로 유실된다.
        """
        self._조회가실패한다(client)
        assert app._is_self_message({"bot_id": "B_ANY"}) is False
        assert app._is_self_message({"user": "U_HUMAN"}) is False
        app.close()

    def test_신원조회_실패를_영구히_캐시하지_않는다(
        self, profile: Profile, client: FakeSlackClient
    ) -> None:
        """한 번 실패했다고 그 결과를 계속 쓰면 일시 장애가 영구 오판이 된다."""
        시각 = [0.0]
        실패중 = [True]

        def auth_test(**kwargs: Any) -> dict:
            client.calls.append(("auth_test", kwargs))
            if 실패중[0]:
                raise RuntimeError("조회 실패")
            return {"ok": True, "user_id": "U_ME", "bot_id": "B_ME"}

        client.auth_test = auth_test  # type: ignore[method-assign, assignment, misc]
        application = Application(profile, client, clock=lambda: 시각[0])
        assert application._is_self_message({"bot_id": "B_ME"}) is False

        실패중[0] = False
        시각[0] = 999.0
        assert application._is_self_message({"bot_id": "B_ME"}) is True
        assert application._is_self_message({"bot_id": "B_OTHER"}) is False
        application.close()

    def test_실패_직후에는_재조회하지_않는다(
        self, profile: Profile, client: FakeSlackClient
    ) -> None:
        """판정마다 재조회하면 장애가 이어지는 동안 요청 수만큼 API 호출이 늘어난다."""
        self._조회가실패한다(client)
        application = Application(profile, client, clock=lambda: 0.0)
        for _ in range(3):
            application._is_self_message({"bot_id": "B_ANY"})
        assert [name for name, _ in client.calls].count("auth_test") == 1
        application.close()

    def test_신원조회는_한번만_한다(self, app: Application, client: FakeSlackClient) -> None:
        """판정마다 조회하면 요청 수만큼 API 호출이 늘어난다."""
        app._is_self_message({"bot_id": "B_X"})
        app._is_self_message({"user": "U_Y"})
        assert [name for name, _ in client.calls].count("auth_test") == 1
        app.close()


class Test신원판정연결:
    """같은 판정을 여러 부품이 각자 들고 있으면 조립이 일부에만 값을 준다.

    실제로 그랬다. `TranscriptBuilder` 는 `bot_id` 도 `bot_user_id` 도 못 받았고
    `EventListener` 는 `bot_user_id` 만 받았다. 둘 다 생성자 기본값이 빈
    문자열이라 조용히 예전 판정으로 돌아갔고, 부품 시험은 전부 통과했다.
    """

    def test_대화록_복원이_조립의_신원을_쓴다(self, app: Application) -> None:
        """다른 봇의 말이 이 봇의 말로 대화록에 적히면 화자 표시가 어긋난다."""
        assert app._transcript_builder()._identity is app.identity
        app.close()

    def test_이벤트_수신이_조립의_신원을_쓴다(self, app: Application) -> None:
        """다른 봇만 있는 스레드를 이 봇이 이미 참여한 것으로 보면 끼어든다."""
        listener = app.ingress()._listener
        assert listener._identity is app.identity
        app.close()

    def test_봇자신의_판정도_같은_신원을_쓴다(self, app: Application) -> None:
        """판정 근거가 부품마다 다르면 같은 메시지에 서로 다른 답이 나온다."""
        client = app._client
        client.auth_test = lambda **kwargs: {  # type: ignore[method-assign, assignment, misc]
            "ok": True, "user_id": "U_ME", "bot_id": "B_ME",
        }
        assert app._is_self_message({"bot_id": "B_OTHER"}) is False
        assert app.identity.is_self({"bot_id": "B_OTHER"}) is False
        assert app._is_self_message({"bot_id": "B_ME"}) is True
        app.close()

    def test_같은_신원_객체를_되풀이_쓴다(self, app: Application) -> None:
        """부품마다 새로 만들면 조회가 그 수만큼 일어나고 실패가 갈린다."""
        assert app.identity is app.identity
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
    지우는 기준 시각은 캐치업 창보다 커야 한다 — 그보다 짧게 잡으면 캐치업이
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
        queue.complete(job.id, ok=True, lease=job.lease)
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
        queue.complete(job.id, ok=True, lease=job.lease)
        application.job_purge_runner()._task()
        assert queue.counts() != {}
        application.close()

    def test_보관기간은_캐치업_최대창보다_길다(self, app: Application) -> None:
        """짧으면 이미 답한 메시지를 캐치업이 미응답으로 보고 다시 등록한다."""
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
        # 1차와 다른 경로를 쓴다. 같은 경로면 어느 엔진이 실행됐는지 명령만 보고
        # 가릴 수 없다.
        binary = tmp_path / "bin" / "fake-codex"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
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

    def test_학습_배치도_같은_부품을_쓴다(self, app: Application) -> None:
        """부품을 만든 것과 조립이 그것을 넘기는 것은 다르다. 여기서 raw
        runner 나 새 DirectInvoker 를 넘기면 학습만 폴백 밖으로 빠진다.
        """
        분석기 = app.learning_batch()._builder._analyzer
        assert isinstance(분석기, ProposalAnalyzer)
        assert 분석기._invoker is app.engine_invoker

    def test_학습_일정이_미완료_날짜를_볼_수_있다(self, app: Application) -> None:
        """안 넘기면 한도 소진으로 미완료인 날이 이틀 뒤 후보에서 사라진다."""
        일정 = app.learning_schedule()
        assert cast(Any, 일정._unsettled_days).__self__ is app._learning_progress_store()

    def test_학습_일정이_대기_여부를_볼_수_있다(self, app: Application) -> None:
        """안 넘기면 오늘이 재시도 대기 중일 때 옛 미완료 날짜가 순서를 못 받는다."""
        일정 = app.learning_schedule()
        assert cast(Any, 일정._is_waiting).__self__ is app._learning_progress_store()

    def test_학습_배치가_진행_상태_저장소를_받는다(self, app: Application) -> None:
        """부품을 만든 것과 조립이 그것을 넘기는 것은 다르다. 안 넘기면 채널별
        재시도가 전부 안 돌고 배치는 매번 처음부터 분석한다(sca-b4o).
        """
        from slack_cli_agent.learning.progress import ProgressStore

        진행 = app.learning_batch()._progress
        assert isinstance(진행, ProgressStore)
        assert 진행._dir == app._profile.paths.proposals / "progress"

    def test_학습_모델을_안_정하면_엔진이_고르게_비워_보낸다(self, app: Application) -> None:
        """공통 문자열을 넣으면 코덱스·제미나이 프로필에서 없는 모델명이 된다.
        비워 보내면 실행기가 그 엔진의 spec.model 로 채운다(sca-dyb.10).
        """
        분석기 = app.learning_batch()._builder._analyzer
        assert isinstance(분석기, ProposalAnalyzer)
        assert 분석기._model is None

    def test_학습_모델을_정하면_그대로_간다(self, tmp_path: Path, client: FakeSlackClient) -> None:
        from dataclasses import replace
        application = Application(self._폴백있는프로필(tmp_path), client)
        application._settings = replace(application._settings, learning_model="정한모델")
        분석기 = application.learning_batch()._builder._analyzer
        assert isinstance(분석기, ProposalAnalyzer)
        assert 분석기._model == "정한모델"
        application.close()

    def test_같은_부품을_되풀이_쓴다(self, app: Application) -> None:
        assert app.engine_invoker is app.engine_invoker
        app.close()

    def test_점검_리액션_경로도_같은_부품을_쓴다(
        self, tmp_path: Path, client: FakeSlackClient
    ) -> None:
        """부검·디버그 추적·서식 점검이 실행기를 직접 부르면 그 경로에서 전환이 없다.

        소유자가 점검 리액션을 달았을 때 1차 엔진이 한도에 걸려도, 전환 상태가
        기록되지 않고 계속 1차 엔진만 불린다.
        """
        application = Application(self._폴백있는프로필(tmp_path), client)
        caller = application._review_engine()
        assert caller._invoker is application.engine_invoker
        application.close()


    def test_한도소진부터_2차_실행까지_조립_전체가_이어진다(
        self, tmp_path: Path, client: FakeSlackClient
    ) -> None:
        """전환 기계는 대역 엔진으로만 확인돼 있었다(sca-75x). 프로필에서 만든
        실제 엔진 두 개와 상태 파일을 거쳐, 한도 소진이 전환 상태로 남고 승인
        뒤 요청이 2차 바이너리로 나가는지는 이 자리에서만 확인된다.
        """
        import subprocess

        from slack_cli_agent.engine.base import EngineRequest
        from slack_cli_agent.engine.switcher import EngineSwitcher

        profile = self._폴백있는프로필(tmp_path)
        assert profile.fallback_engine is not None
        일차 = str(profile.primary_engine.binary)
        이차 = str(profile.fallback_engine.binary)
        application = Application(profile, client)
        엔진 = application.engine
        실행된명령: list[list[str]] = []

        def 대역(cmd: list[str], cwd: str, timeout: float, env: Any = None) -> Any:
            실행된명령.append(cmd)
            if cmd[0] == 일차:
                # 클로드 CLI 는 한도 소진을 종료 코드 1 과 stdout JSON 으로 낸다.
                본문 = json.dumps({"result": "5-hour limit reached", "api_error_status": 429})
                return subprocess.CompletedProcess(args=cmd, returncode=1, stdout=본문, stderr="")
            본문 = "\n".join(
                [
                    json.dumps({"type": "thread.started", "thread_id": "th-1"}),
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {"type": "agent_message", "text": "2차 답변"},
                        }
                    ),
                ]
            )
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=본문, stderr="")

        엔진.runner._run = 대역  # type: ignore[attr-defined]
        요청 = EngineRequest(
            prompt="질문", system_prompt="", session_id=None, resume=False,
            model=None, effort="low", workdir=tmp_path,
        )

        application.engine_invoker.invoke(요청)

        switcher = EngineSwitcher(profile.paths.engine_state)
        assert switcher.is_switched()

        switcher.approve()
        실행된명령.clear()
        응답 = application.engine_invoker.invoke(요청)

        assert 응답.body == "2차 답변"
        assert [cmd[0] for cmd in 실행된명령] == [이차]
        application.close()


class Test학습쌓기자리연결:
    """사람이 쓰는 자리와 학습이 쌓는 자리를 갈랐다(sca-jl4.5).

    조립이 예전 자리를 그대로 넘기면 파일은 갈라 뒀는데 실제 쓰기는 사람 자리로
    계속 간다. 로더가 학습 자리를 못 받으면 반대로 쌓인 것이 프롬프트에서 빠진다.
    """

    def test_학습_적용이_학습_자리에_쓴다(self, app: Application) -> None:
        assert app.learning_batch()._applier._dir == app.profile.paths.learned

    def test_프롬프트_구성이_학습_자리도_읽는다(self, app: Application) -> None:
        composer = app._composer()
        assert isinstance(composer, SystemPromptComposer)
        assert composer._knowledge._learned_dir == app.profile.paths.learned


class Test에이전트패널연결:
    """패널을 만드는 것과 접수기가 그 이벤트를 받는 것은 다르다(sca-kos.7)."""

    def test_접수기가_패널을_받는다(self, app: Application) -> None:
        assert app.ingress()._assistant is not None

    def test_프로필의_안내_문구를_쓴다(self, tmp_path: Path, client: FakeSlackClient) -> None:
        profile = write_profile(tmp_path, agent_greeting="신지입니다. 무엇을 도와드릴까요?")
        application = Application(profile, client)
        assistant = application.ingress()._assistant
        assert assistant is not None
        assert assistant._greeting == "신지입니다. 무엇을 도와드릴까요?"
        application.close()

    def test_안내_문구가_없으면_기본_문구를_쓴다(self, app: Application) -> None:
        from slack_cli_agent.slack.assistant import DEFAULT_GREETING

        assistant = app.ingress()._assistant
        assert assistant is not None
        assert assistant._greeting == DEFAULT_GREETING

    def test_프로필의_제안_프롬프트를_쓴다(self, tmp_path: Path, client: FakeSlackClient) -> None:
        profile = write_profile(
            tmp_path, agent_prompts=[{"title": "오늘 할 일", "message": "오늘 할 일을 알려줘"}],
        )
        application = Application(profile, client)
        assistant = application.ingress()._assistant
        assert assistant is not None
        prompts = assistant._prompts
        assert [p.title for p in prompts] == ["오늘 할 일"]
        application.close()


class Test소켓지표출처연결:
    """스냅샷은 워커가 쓰고 소켓은 접수기에만 있다(sca-qi5.3).

    실측 2026-09-17 — 접수기 로그에는 세션 수립이 51회 있는데 워커가 쓴
    state.json 의 재연결 누적은 0 이었다.
    """

    def test_오류_건수를_0_대신_미계측으로_낸다(self, app: Application) -> None:
        snapshot = app._snapshot_source()
        assert snapshot.socket_error_timestamps() is None

    def test_재연결은_접수기가_적은_원장에서_읽는다(self, app: Application) -> None:
        epochs = app.connection_epochs()
        epochs.record_connection(ConnectionKind.INITIAL)
        epochs.record_connection(ConnectionKind.RECONNECT)
        assert len(app._snapshot_source().socket_reconnect_timestamps()) == 1


class Test플러그인엔진등록:
    """플러그인이 더한 엔진이 실제로 조립에 들어가는가.

    계약만 만들고 조립이 그것을 안 부르면 그 엔진은 어느 실행 경로에서도
    안 쓰인다. 이 저장소에서 반복해서 난 결함이 그 형태다.
    """

    @staticmethod
    def 엔진을더하는플러그인() -> BotPlugin:
        from slack_cli_agent.engine.base import Engine

        class 남의엔진(Engine):
            name = "mine"

            def build_command(self, request: Any) -> list[str]:
                return ["true"]

            def parse(self, stdout: str, stderr: str, returncode: int) -> Any:
                raise NotImplementedError

        class 엔진플러그인(BotPlugin):
            name = "engine-plugin"

            def engines(self) -> Sequence[type[Engine]]:
                return (남의엔진,)

        return 엔진플러그인()

    def test_플러그인이_더한_엔진을_쓸_수_있다(self, tmp_path: Path, client: FakeSlackClient) -> None:
        profile = write_profile(tmp_path)
        application = Application(profile, client, plugins=[self.엔진을더하는플러그인()])
        assert "mine" in application.engine_registry.available()

    def test_플러그인이_없으면_기본_엔진만_있다(self, profile: Profile, client: FakeSlackClient) -> None:
        assert Application(profile, client).engine_registry.available() == ["claude", "codex", "gemini"]

    def test_주입한_레지스트리에도_더한다(self, tmp_path: Path, client: FakeSlackClient) -> None:
        """레지스트리를 밖에서 넣은 경우에만 플러그인 엔진이 빠지면 그 사실이 안 드러난다."""
        from slack_cli_agent.engine.registry import EngineRegistry

        profile = write_profile(tmp_path)
        application = Application(
            profile, client, plugins=[self.엔진을더하는플러그인()], engine_registry=EngineRegistry()
        )
        assert application.engine_registry.available() == ["mine"]


class Test응답기록:
    """학습 배치가 읽을 자료를 남기는 쪽이 실제로 조립에 들어갔는지 본다.

    부품만 있고 파이프라인에 안 들어가면 기록이 한 줄도 안 생기고, 그러면
    배치는 매일 "응답 기록이 없다" 로 끝난다.
    """

    def test_파이프라인에_응답_기록이_들어간다(self, app: Application) -> None:
        assert app.pipeline()._response_archive is app.response_archive()

    def test_같은_객체를_돌려준다(self, app: Application) -> None:
        assert app.response_archive() is app.response_archive()

    def test_상태_디렉터리_아래에_남긴다(self, app: Application) -> None:
        assert app.response_archive()._root == app.profile.paths.responses

    def test_날짜를_KST_로_정한다(self, app: Application) -> None:
        """UTC 로 정하면 밤 9시 이후 기록이 다음 날 파일로 간다."""
        assert app.response_archive()._clock().utcoffset() == timedelta(hours=9)


class Test학습배치:
    """제안을 새로 만드는 배치가 조립과 워커에 실제로 연결됐는지 본다.

    원본은 launchd 가 부르는 별도 스크립트였다. 그 진입점이 없으면 표시·반영·
    되돌리기 명령은 살아 있는데 그 명령들이 읽을 제안 파일을 아무도 안 만든다.
    """

    def test_같은_객체를_돌려준다(self, app: Application) -> None:
        assert app.learning_batch() is app.learning_batch()

    def test_응답_기록을_읽는다(self, app: Application) -> None:
        assert app.learning_batch()._archives is app.response_archive()

    def test_소유자에게_알린다(self, app: Application) -> None:
        assert app.learning_batch()._notify == app._notify_owner

    def test_발송_실패는_False_로_돌려준다(self, app: Application) -> None:
        """보류에 저장하는 것과 사람에게 닿은 것은 다르다. 같은 값으로 내면
        즉시 발송 실패가 보고에서 성공으로 읽힌다."""
        def 실패(*args: object, **kwargs: object) -> str:
            raise RuntimeError("슬랙 발송 실패")

        app.publisher().post = 실패  # type: ignore[method-assign]
        assert app._notify_owner("본문") is False
        saved = json.loads(app.pending_report()._path.read_text(encoding="utf-8"))
        assert saved["text"] == "본문"

    def test_발송_성공은_True_로_돌려준다(self, app: Application) -> None:
        assert app._notify_owner("본문") is True

    def test_실행기_이름은_learning_batch(self, app: Application) -> None:
        assert app.learning_batch_runner().name == "learning_batch"

    def test_완료_표식이_있어야_그날을_끝난_것으로_본다(self, app: Application) -> None:
        """제안 파일 존재로 판정하면 저장 뒤 반영이 실패한 날이 영영 다시 실행되지 않는다."""
        proposals = app.profile.paths.proposals
        proposals.mkdir(parents=True, exist_ok=True)
        (proposals / "2026-09-14.json").write_text("{}", encoding="utf-8")
        assert app._learning_day_done("2026-09-14") is False

        (proposals / "2026-09-14.done").write_text("{}", encoding="utf-8")
        assert app._learning_day_done("2026-09-14") is True

    def test_분석할_날짜가_없으면_배치를_실행하지_않는다(
        self, tmp_path: Path, client: FakeSlackClient
    ) -> None:
        """주기 실행기는 10분마다 틱을 낸다. 판정이 없으면 그때마다 그날
        기록을 다시 분석해 엔진을 부르고 제안 파일을 덮어쓴다."""
        application = Application(write_profile(tmp_path, settings={"learning_run_hour": 23}), client)
        batch = RecordingBatch()
        application._learning_batch = batch  # type: ignore[assignment]  # 호출 여부만 본다
        proposals = application.profile.paths.proposals
        proposals.mkdir(parents=True, exist_ok=True)
        for moment in (datetime.now(KST), datetime.now(KST) - timedelta(days=1)):
            (proposals / f"{moment.strftime('%Y-%m-%d')}.done").write_text("{}", encoding="utf-8")

        application._learning_batch_tick()

        assert batch.days == []

    def test_기준_시각_설정이_범위_밖이면_기본값으로_돌린다(
        self, tmp_path: Path, client: FakeSlackClient
    ) -> None:
        """0-23 밖이면 그 값으로는 하루도 실행되지 않는다. 설정 오타로 배치가 멎는다."""
        application = Application(write_profile(tmp_path, settings={"learning_run_hour": 25}), client)
        assert application.learning_schedule()._run_hour == RuntimeSettings().learning_run_hour

    def test_분석할_날짜가_있으면_그_날짜로_배치를_실행한다(
        self, tmp_path: Path, client: FakeSlackClient
    ) -> None:
        application = Application(write_profile(tmp_path, settings={"learning_run_hour": 0}), client)
        batch = RecordingBatch()
        application._learning_batch = batch  # type: ignore[assignment]  # 호출 인자만 본다

        application._learning_batch_tick()

        assert batch.days == [datetime.now(KST).strftime("%Y-%m-%d")]


class RecordingBatch:
    """학습 배치 호출만 기록한다. 실행기 판정이 부르는지를 보는 용도다."""

    def __init__(self) -> None:
        self.days: list[str] = []

    def run(self, day: str | None = None) -> BatchReport:
        self.days.append(day or "")
        return BatchReport(day=day or "", ran=False, reason="시험용", proposal=None)


class Test발행기감사기록배선:
    """부품을 만든 것과 조립에 연결한 것은 다르다. MessagePublisher 는 audit 인자를
    받지만 Application 이 안 넘겨, 분할·게시 실패 사건이 하나도 안 남았다."""

    def test_게시_실패가_감사_기록에_남는다(self, tmp_path: Path) -> None:
        from slack_cli_agent.core.errors import SlackError

        class FailingClient(FakeSlackClient):
            def __getattr__(self, name: str) -> Any:
                if name == "chat_postMessage":
                    def fail(**kwargs: Any) -> dict[str, Any]:
                        raise RuntimeError("슬랙 게시 실패")
                    return fail
                return super().__getattr__(name)

        profile = write_profile(tmp_path)
        app = Application(profile, FailingClient())
        with pytest.raises(SlackError):
            app.publisher().post("C1", "1.1", "본문", rich=False)

        lines = profile.paths.audit_log.read_text(encoding="utf-8").splitlines()
        assert [json.loads(line)["kind"] for line in lines] == ["post_failed"]

    def test_감사_기록은_파이프라인과_같은_인스턴스다(self, tmp_path: Path) -> None:
        """따로 만들면 jsonl 핸들과 시계가 갈려 같은 구간이 두 기록으로 나뉜다."""
        app = Application(write_profile(tmp_path), FakeSlackClient())
        assert app.pipeline().audit is app.audit()


class Test게이트웨이프로필배선:
    """기동 로그에 어느 프로필인지가 들어가야 봇이 여럿일 때 누가 붙었는지
    가린다. SlackGateway 가 profile_name 을 받는데 Application 이 안 넘겼다."""

    def test_게이트웨이가_프로필_이름을_받는다(self, tmp_path: Path) -> None:
        profile = write_profile(tmp_path, name="어느봇")
        app = Application(profile, FakeSlackClient())

        assert app.gateway()._profile_name == "어느봇"


class Test소유자_DM_경로:
    """프로필에 owner_dm 을 안 적으면 빈 문자열로 발송해 channel_not_found 가
    난다. 보류 보고가 30초마다 그 실패를 반복했다(2026-09-15 실측). 소유자
    사용자 ID 는 이미 있으므로 DM 방을 열어서 쓴다."""

    def test_owner_dm_이_없으면_소유자와의_DM_을_연다(
        self, tmp_path: Path, client: FakeSlackClient
    ) -> None:
        client.open_channel = "D_OPENED"
        app = Application(write_profile(tmp_path), client=client)

        app._post_owner_dm("본문")

        opened = [kw for name, kw in client.calls if name == "conversations_open"]
        assert opened and opened[0]["users"] == "U_OWNER"
        posted = [kw for name, kw in client.calls if name == "chat_postMessage"]
        assert posted and posted[0]["channel"] == "D_OPENED"

    def test_프로필에_적힌_owner_dm_이_있으면_그대로_쓴다(
        self, tmp_path: Path, client: FakeSlackClient
    ) -> None:
        app = Application(write_profile(tmp_path, owner_dm="D_FIXED"), client=client)

        app._post_owner_dm("본문")

        assert not [name for name, _ in client.calls if name == "conversations_open"]
        posted = [kw for name, kw in client.calls if name == "chat_postMessage"]
        assert posted and posted[0]["channel"] == "D_FIXED"

    def test_한_번_연_DM_은_다시_열지_않는다(
        self, tmp_path: Path, client: FakeSlackClient
    ) -> None:
        client.open_channel = "D_OPENED"
        app = Application(write_profile(tmp_path), client=client)

        app._post_owner_dm("첫 번째")
        app._post_owner_dm("두 번째")

        assert len([name for name, _ in client.calls if name == "conversations_open"]) == 1


class Test감시확인턴의_조립맥락:
    """확인 턴은 조회만 한다. 일반 감시 안내를 함께 주면 그 턴이 새 작업을
    띄우도록 유도한다 (sca-ejy).
    """

    def test_watch_check_를_참으로_넘긴다(self, tmp_path: Path, monkeypatch: Any) -> None:
        from slack_cli_agent.auth.principal import TrustLevel
        from slack_cli_agent.reliability.watchjobs import WatchJob

        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        조립 = _프롬프트조립대역()
        app._composer = lambda: 조립  # type: ignore[method-assign]
        monkeypatch.setattr(EngineRunner, "run", lambda self, engine, request: None)

        app._watch_run_check(WatchJob(
            id=1, channel="C1", thread_ts="1.1", condition="배포 확인",
            created_at=0.0, last_run=None, trust=TrustLevel.OWNER,
        ), WatchOutcome.UNKNOWN)

        assert 조립.받은맥락 and 조립.받은맥락[0].watch_check is True

    def test_평상시_경로는_거짓이다(self, tmp_path: Path) -> None:
        """기본값이 참이면 모든 턴이 확인 턴 안내를 받아 아무도 감시를 등록하지
        못한다. 확인 턴 쪽만 보면 그 뒤집힘을 못 잡는다."""
        from slack_cli_agent.prompt.sections import CompositionContext

        assert CompositionContext(principal=_소유자()).watch_check is False


class Test감시확인턴의_실행자리:
    """확인 턴은 등록보다 한참 뒤에 돈다. 결과 파일은 등록 시점 workdir 아래에
    있으므로 지금 채널 설정으로 다시 계산하면 다른 자리를 본다 (sca-6zt).
    """

    def _요청을_잡는다(self, tmp_path: Path, monkeypatch: Any, job: Any) -> Any:
        from slack_cli_agent.reliability.watchresult import WatchOutcome

        잡은요청: list[Any] = []

        def 기록(self: Any, engine: Any, request: Any) -> None:
            잡은요청.append(request)

        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        monkeypatch.setattr(EngineRunner, "run", 기록)
        app._watch_run_check(job, WatchOutcome.UNKNOWN)
        return 잡은요청[0]

    def _작업(self, **overrides: Any) -> Any:
        from slack_cli_agent.reliability.watchjobs import WatchJob

        base: dict[str, Any] = {
            "id": 1, "channel": "C1", "thread_ts": "1.1", "condition": "배포 확인",
            "created_at": 0.0, "last_run": None,
        }
        base.update(overrides)
        return WatchJob(**base)

    def test_등록시점_자리를_그대로_쓴다(self, tmp_path: Path, monkeypatch: Any) -> None:
        자리 = tmp_path.parent / f"{tmp_path.name}-watch"
        자리.mkdir(exist_ok=True)
        요청 = self._요청을_잡는다(
            tmp_path, monkeypatch, self._작업(workdir=str(자리))
        )

        assert 요청.workdir == 자리

    def test_등록시점_자리가_없으면_기본값을_쓴다(self, tmp_path: Path, monkeypatch: Any) -> None:
        """이 컬럼이 생기기 전에 등록된 건은 값이 비어 있다. 그때 빈 경로로
        실행하면 엔진이 아무 데서나 돈다."""
        요청 = self._요청을_잡는다(tmp_path, monkeypatch, self._작업())

        assert str(요청.workdir) not in ("", ".")


class Test감시체커_배선:
    """부품을 만든 것과 조립에 연결한 것은 다르다. 확인 콜백의 시그니처가
    바뀌었는데 조립을 안 고치면 단위 시험은 전부 통과하고 운영에서만 죽는다.
    """

    def test_판정기가_주입된다(self, tmp_path: Path) -> None:
        from slack_cli_agent.reliability.watchresult import WatchResultReader

        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())

        assert isinstance(app.watch_checker()._results, WatchResultReader)

    def test_확인_콜백이_판정_결과를_받는다(self, tmp_path: Path, monkeypatch: Any) -> None:
        """조립된 콜백을 체커가 부르는 형태 그대로 부른다."""
        from slack_cli_agent.reliability.watchjobs import WatchJob
        from slack_cli_agent.reliability.watchresult import WatchOutcome

        잡은프롬프트: list[str] = []
        monkeypatch.setattr(
            EngineRunner, "run",
            lambda self, engine, request: 잡은프롬프트.append(request.prompt),
        )
        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        작업 = WatchJob(
            id=1, channel="C1", thread_ts="1.1", condition="배포 확인",
            created_at=0.0, last_run=None, workdir=str(tmp_path), run_id="r1",
        )

        app.watch_checker()._run_check(작업, WatchOutcome.FAILED)

        assert 잡은프롬프트 and "실패로 끝났다" in 잡은프롬프트[0]


def _소유자() -> Any:
    from slack_cli_agent.auth.principal import Principal, TrustLevel

    return Principal(user_id="U1", channel="D1", trust=TrustLevel.OWNER, is_direct_message=True)


class Test진행표시감사기록배선:
    """ProgressCoordinator 는 audit 인자를 받는다. Application 이 안 넘기면
    표에 없는 도구 이름이 어디에도 안 남는다 (sca-2wu)."""

    def test_표에_없는_도구_이름이_감사_기록에_남는다(self, tmp_path: Path) -> None:
        profile = write_profile(tmp_path)
        app = Application(profile, FakeSlackClient())
        # 조정자가 쥔 매퍼를 그대로 쓴다 - 세션을 돌리면 스레드 시간에 기댄다
        assert app.progress()._mapper.label_for("새로생긴도구") == "확인하는 중"
        lines = profile.paths.audit_log.read_text(encoding="utf-8").splitlines()
        기록 = [json.loads(line) for line in lines]
        assert [x["kind"] for x in 기록] == ["progress_unknown_tool"]
        assert 기록[0]["tool"] == "새로생긴도구"


class Test봇멘션가드연결:
    """다른 봇의 멘션을 지우는 가드가 조립에 들어가는가 (sca-c4m).

    만들어도 파이프라인에 안 넣으면 답에 든 <@U...> 가 그대로 나가 그 봇을
    깨우고, 그 봇의 답이 이쪽을 부르면 서로 계속 깨운다.
    """

    def test_가드가_파이프라인에_들어간다(self, tmp_path: Path) -> None:
        from slack_cli_agent.guard.mentions import BotMentionGuard

        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        assert any(isinstance(g, BotMentionGuard) for g in app._guards()._guards)

    def test_봇_멘션이_실제로_지워진다(self, tmp_path: Path) -> None:
        client = FakeSlackClient()
        client.users_info = lambda **kw: {  # type: ignore[method-assign]
            "user": {"id": kw.get("user"), "is_bot": True, "profile": {"real_name": "레이"}}
        }
        app = Application.from_profile(write_profile(tmp_path), client=client)

        result = app._guards().run("자세한 것은 <@U0REI> 에게 물어보세요", GuardContext())

        assert "<@U0REI>" not in result.body
        assert "레이" in result.body

    def test_사람_멘션은_남는다(self, tmp_path: Path) -> None:
        client = FakeSlackClient()
        client.users_info = lambda **kw: {  # type: ignore[method-assign]
            "user": {"id": kw.get("user"), "is_bot": False, "profile": {"real_name": "홍길동"}}
        }
        app = Application.from_profile(write_profile(tmp_path), client=client)

        result = app._guards().run("확인은 <@U0TAEL> 께 부탁드립니다", GuardContext())

        assert "<@U0TAEL>" in result.body


class Test점검느린보고연결:
    """점검 경로에도 느린 실행 보고가 붙는가 (sca-xck).

    안 붙이면 점검이 제한시간 근처까지 길어져도 audit 의 숫자 하나만 남는다.
    """

    def test_점검에_보고자가_주입된다(self, tmp_path: Path) -> None:
        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        for task in app.review_tasks().values():
            assert task._slow_reporter is not None

    def test_점검용_기준을_따로_쓴다(self, tmp_path: Path) -> None:
        """점검은 원래 몇 분씩 걸린다. 요청 쪽 기준을 같이 쓰면 한쪽을 바꿀 때
        다른 쪽이 딸려 온다."""
        app = Application.from_profile(
            write_profile(tmp_path, settings={"review_slow_report_sec": 111, "slow_report_sec": 222}),
            client=FakeSlackClient(),
        )
        task = next(iter(app.review_tasks().values()))
        reporter = cast(Any, task._slow_reporter)
        assert reporter._settings.slow_report_sec == 111
