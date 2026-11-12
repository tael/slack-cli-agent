"""시스템 지침이 바뀐 턴은 세션을 이어받지 않는다.

claude 2.1.319 는 --resume 턴의 --append-system-prompt 를 반영하지 않는다. 그래서
지침 파일(상태 디렉터리 prompts/prompt_chat.md 등)을 고쳐도 열린 스레드 세션은
첫 턴의 지침을 계속 쓴다 (2026-11-10 마메치 잡담 채널, 11:55 수정 뒤 12:17 답이
옛 지침으로 나갔다). 세션을 연 턴의 지침 지문과 이번 턴의 지문이 다르면 새
세션을 연다. 지문은 턴마다 바뀌는 값을 뺀 것이라 같은 지침이면 같은 값이다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_application import FakeSlackClient, write_profile
from test_pipeline import FakeComposer, build_pipeline, make_ctx, ok_response

from slack_cli_agent.auth.principal import Principal, TrustLevel
from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.core.application import Application
from slack_cli_agent.prompt.composer import SystemPromptComposer
from slack_cli_agent.prompt.sections import CompositionContext
from slack_cli_agent.session.manager import SYSTEM_PROMPT_RESET_REASON, SessionManager
from slack_cli_agent.session.ports import SessionKey, SessionRecord
from slack_cli_agent.session.store import SqliteSessionStore

KEY = SessionKey(scope="thread", key="C1:1")


def _manager(store) -> SessionManager:
    ids = iter(f"new-{i}" for i in range(1, 10))
    return SessionManager(store, RuntimeSettings(), now=lambda: 1000.0,
                          new_session_id=lambda: next(ids))


class Test판정:
    def test_지문이_같으면_이어받는다(self, database) -> None:
        mgr = _manager(SqliteSessionStore(database))
        first = mgr.resolve(KEY, engine="claude", tier="owner", fingerprint="a")
        again = mgr.resolve(KEY, engine="claude", tier="owner", fingerprint="a")
        assert again.resume is True
        assert again.session_id == first.session_id
        assert again.reset_reason == ""

    def test_지문이_다르면_새_세션으로_전체_재구성한다(self, database) -> None:
        store = SqliteSessionStore(database)
        mgr = _manager(store)
        first = mgr.resolve(KEY, engine="claude", tier="owner", fingerprint="a")
        changed = mgr.resolve(KEY, engine="claude", tier="owner", fingerprint="b")
        assert changed.resume is False
        assert changed.rebuild_full is True
        assert changed.session_id != first.session_id
        assert changed.reset_reason == SYSTEM_PROMPT_RESET_REASON
        record = store.get(KEY)
        assert record is not None and record.prompt_fingerprint == "b"
        # 새 지문으로 연 세션은 다음 턴에 이어진다.
        assert mgr.resolve(KEY, engine="claude", tier="owner", fingerprint="b").resume is True

    @staticmethod
    def _old_row(store: SqliteSessionStore) -> None:
        store.put(SessionRecord(scope=KEY.scope, key=KEY.key, session_id="old", engine="claude",
                                created_at=999.0, last_seen_ts="", updated_at=999.0,
                                tier="owner"))

    def test_지문이_없는_기존_행은_지문을_주는_턴이_이어받지_않는다(self, database) -> None:
        """배포 직후 한 번 새 세션을 연다. 11:55 이전 옛 지침으로 열린 세션이 정리된다."""
        store = SqliteSessionStore(database)
        self._old_row(store)
        mgr = _manager(store)
        decision = mgr.resolve(KEY, engine="claude", tier="owner", fingerprint="a")
        assert decision.resume is False
        assert decision.session_id != "old"
        assert decision.reset_reason == SYSTEM_PROMPT_RESET_REASON
        record = store.get(KEY)
        assert record is not None and record.prompt_fingerprint == "a"
        assert mgr.resolve(KEY, engine="claude", tier="owner", fingerprint="a").resume is True

    def test_지문을_쓰지_않는_엔진은_기존_행을_이어받는다(self, database) -> None:
        store = SqliteSessionStore(database)
        self._old_row(store)
        decision = _manager(store).resolve(KEY, engine="claude", tier="owner")
        assert decision.resume is True
        assert decision.session_id == "old"

    def test_지문을_주지_않는_호출은_비교하지_않는다(self, database) -> None:
        mgr = _manager(SqliteSessionStore(database))
        mgr.resolve(KEY, engine="claude", tier="owner", fingerprint="a")
        assert mgr.resolve(KEY, engine="claude", tier="owner").resume is True

    def test_재시도_reset도_지문을_기록한다(self, database) -> None:
        store = SqliteSessionStore(database)
        mgr = _manager(store)
        mgr.reset(KEY, engine="claude", tier="owner", fingerprint="a")
        record = store.get(KEY)
        assert record is not None and record.prompt_fingerprint == "a"


class Test지문안정성:
    """실물 조립기로 지문이 턴 사이에 흔들리지 않는지 확인한다."""

    OWNER = Principal(user_id="U_OWNER", channel="C1", trust=TrustLevel.OWNER,
                      is_direct_message=False)
    STRANGER = Principal(user_id="U_STRANGER", channel="C1", trust=TrustLevel.GENERAL,
                         is_direct_message=False)

    def _composer(self, tmp_path: Path) -> tuple[SystemPromptComposer, Path, Application]:
        profile = write_profile(tmp_path)
        app = Application(profile, FakeSlackClient())
        composer = app._composer()
        assert isinstance(composer, SystemPromptComposer)
        return composer, profile.paths.prompts, app

    def _ctx(self, principal: Principal, **kwargs: Any) -> CompositionContext:
        base: dict[str, Any] = {
            "principal": principal, "channel_mode": "chat", "channel": "C1",
            "chat_level": "normal",
            "turn_tier": "owner" if principal.trust is TrustLevel.OWNER else "read",
            "request_header": True, "watch_out_dir": "/tmp/watch-out",
        }
        base.update(kwargs)
        return CompositionContext(**base)

    def _two_turns(self, principal: Principal) -> tuple[CompositionContext, CompositionContext]:
        """같은 스레드의 두 턴. 턴마다 바뀌는 값을 전부 다르게 준다."""
        first = self._ctx(
            principal, prompt="안녕", thread_ts="1.1", watch_run_id="run-1",
            asker_name="가", asker_id="U_A", people=(("가", "<@U_A>"), ("나", "<@U_B>")),
            turn_reads_only=True,
        )
        second = self._ctx(
            principal, prompt="정산 SQL 돌려줘", thread_ts="1.1", watch_run_id="run-2",
            asker_name="나", asker_id="U_B", unaddressed=True,
            people=(("가", "<@U_A>"), ("나", "<@U_B>"), ("다", "<@U_C>")),
            files=({"name": "a.png", "mimetype": "image/png", "local_path": "/tmp/a.png"},),
            missed_files=1, turn_acting_tools=("Bash",),
        )
        return first, second

    def test_턴마다_바뀌는_값은_지문을_바꾸지_않는다(self, tmp_path: Path) -> None:
        composer, _prompts, app = self._composer(tmp_path)
        knowledge = app.profile.paths.knowledge
        knowledge.mkdir(parents=True, exist_ok=True)
        # 요청 문구로 골라지는 문서가 있어도 지문은 같다.
        (knowledge / "_settlement.md").write_text(
            "<!-- when: 정산 -->\n정산 문서\n", encoding="utf-8",
        )
        for principal in (self.OWNER, self.STRANGER):
            first, second = self._two_turns(principal)
            assert composer.compose(first) != composer.compose(second)
            assert composer.instruction_fingerprint(first) == composer.instruction_fingerprint(second)
        app.close()

    def test_지침_파일을_고치면_지문이_바뀐다(self, tmp_path: Path) -> None:
        composer, prompts, app = self._composer(tmp_path)
        ctx = self._ctx(self.OWNER, prompt="안녕", thread_ts="1.1")
        before = composer.instruction_fingerprint(ctx)
        prompts.mkdir(parents=True, exist_ok=True)
        (prompts / "prompt_chat.md").write_text("고친 잡담 지침\n", encoding="utf-8")
        assert composer.instruction_fingerprint(ctx) != before
        app.close()

    @pytest.mark.parametrize("channel_mode", ["default", "chat"])
    def test_학습_줄이_늘어도_지문이_같다(self, tmp_path: Path, channel_mode: str) -> None:
        """야간 학습기가 한 줄을 더해도 열린 세션이 리셋되면 안 된다. 업무 채널은
        학습 줄을 빼고 개수만 안내하고, 잡담 채널은 학습 줄을 그대로 싣는다
        (PR #183). 두 경우 모두 지문은 같아야 한다."""
        composer, _prompts, app = self._composer(tmp_path)
        knowledge = app.profile.paths.knowledge
        knowledge.mkdir(parents=True, exist_ok=True)
        doc = knowledge / "_rules.md"
        doc.write_text("규칙\n- 정정 1 <!-- learn:2026-11-09 -->\n", encoding="utf-8")
        ctx = self._ctx(self.OWNER, channel_mode=channel_mode)
        before_text = composer.compose(ctx)
        before = composer.instruction_fingerprint(ctx)
        doc.write_text(
            "규칙\n- 정정 1 <!-- learn:2026-11-09 -->\n- 정정 2 <!-- learn:2026-11-10 -->\n",
            encoding="utf-8",
        )
        # 2026-11-12: the deferred note no longer carries the count, so a
        # learned line added at night leaves a work-channel prompt unchanged;
        # a chat channel still carries the lines themselves.
        if channel_mode == "chat":
            assert composer.compose(ctx) != before_text
        assert composer.instruction_fingerprint(ctx) == before
        app.close()

    def test_학습_줄이_아닌_수정은_잡담_채널에서도_지문을_바꾼다(self, tmp_path: Path) -> None:
        composer, _prompts, app = self._composer(tmp_path)
        knowledge = app.profile.paths.knowledge
        knowledge.mkdir(parents=True, exist_ok=True)
        doc = knowledge / "_rules.md"
        doc.write_text("규칙 1\n- 정정 <!-- learn:2026-11-09 -->\n", encoding="utf-8")
        ctx = self._ctx(self.OWNER, channel_mode="chat")
        before = composer.instruction_fingerprint(ctx)
        doc.write_text("규칙 2\n- 정정 <!-- learn:2026-11-09 -->\n", encoding="utf-8")
        assert composer.instruction_fingerprint(ctx) != before
        app.close()

    def test_고정_지식_문서를_고쳐도_지문이_바뀐다(self, tmp_path: Path) -> None:
        composer, _prompts, app = self._composer(tmp_path)
        knowledge = app.profile.paths.knowledge
        knowledge.mkdir(parents=True, exist_ok=True)
        (knowledge / "_rules.md").write_text("규칙 1\n", encoding="utf-8")
        ctx = self._ctx(self.OWNER)
        before = composer.instruction_fingerprint(ctx)
        (knowledge / "_rules.md").write_text("규칙 2\n", encoding="utf-8")
        assert composer.instruction_fingerprint(ctx) != before
        app.close()


class _FingerprintComposer(FakeComposer):
    """instruction_fingerprint 를 가진 조립기. 값은 시험이 바꾼다."""

    def __init__(self) -> None:
        super().__init__()
        self.value = "fp-1"
        self.fingerprint_contexts: list[Any] = []

    def instruction_fingerprint(self, ctx: Any) -> str:
        self.fingerprint_contexts.append(ctx)
        return self.value


class Test파이프라인:
    def test_지침이_같으면_세션을_이어받는다(self) -> None:
        composer = _FingerprintComposer()
        pipeline, parts = build_pipeline(
            responses=[ok_response(), ok_response()], composer=composer,
        )
        pipeline.handle(make_ctx(user="UOWNER", ts="1.1", thread_ts="1.1"))
        pipeline.handle(make_ctx(user="UOWNER", ts="1.2", thread_ts="1.1"))
        second = parts["runner"].calls[1]
        assert second.resume is True
        assert second.session_id == "sess-1"
        assert parts["audit"].records[-1]["context_reset"] is False

    def test_지침이_바뀌면_새_세션을_열고_사유를_남긴다(self) -> None:
        composer = _FingerprintComposer()
        pipeline, parts = build_pipeline(
            responses=[ok_response(), ok_response(session_id="sess-2")], composer=composer,
        )
        pipeline.handle(make_ctx(user="UOWNER", ts="1.1", thread_ts="1.1"))
        composer.value = "fp-2"
        pipeline.handle(make_ctx(user="UOWNER", ts="1.2", thread_ts="1.1"))
        first, second = parts["runner"].calls
        assert second.resume is False
        assert second.session_id != first.session_id
        record = parts["audit"].records[-1]
        assert record["context_reset"] is True
        assert record["context_reset_reason"] == SYSTEM_PROMPT_RESET_REASON
        # 임계 리셋처럼 이어 가는 대화라는 안내가 붙는다.
        assert "지침이 바뀌어 새 세션으로 이어 갑니다" in second.prompt
        assert "지침이 바뀌어" not in first.prompt

    def test_지침을_다시_싣는_엔진은_지문을_비교하지_않는다(self) -> None:
        """codex, gemini 는 재개 턴에도 이번 턴 지침을 싣는다 (dedups_resumed_instructions)."""
        composer = _FingerprintComposer()
        pipeline, parts = build_pipeline(
            responses=[ok_response(), ok_response()], composer=composer,
        )
        pipeline._engine.dedups_resumed_instructions = True  # type: ignore[misc]
        pipeline.handle(make_ctx(user="UOWNER", ts="1.1", thread_ts="1.1"))
        composer.value = "fp-2"
        pipeline.handle(make_ctx(user="UOWNER", ts="1.2", thread_ts="1.1"))
        assert parts["runner"].calls[1].resume is True
        assert composer.fingerprint_contexts == []

    def test_지문용_조립은_참석자와_이름을_조회하지_않는다(self) -> None:
        composer = _FingerprintComposer()
        pipeline, _parts = build_pipeline(responses=[ok_response()], composer=composer)
        pipeline.handle(make_ctx(user="U1", ts="1.1", thread_ts="1.1"))
        ctx = composer.fingerprint_contexts[0]
        assert ctx.people == ()
        assert ctx.asker_name == ""
        assert ctx.request_header is True

    def test_지문이_없는_조립기는_비교하지_않는다(self) -> None:
        pipeline, parts = build_pipeline(responses=[ok_response(), ok_response()])
        pipeline.handle(make_ctx(user="UOWNER", ts="1.1", thread_ts="1.1"))
        pipeline.handle(make_ctx(user="UOWNER", ts="1.2", thread_ts="1.1"))
        assert parts["runner"].calls[1].resume is True


class Test엔진범위:
    def test_claude_계열만_첫_지침을_유지한다(self) -> None:
        from slack_cli_agent.engine.claude import ClaudeEngine
        from slack_cli_agent.engine.codex import CodexEngine
        from slack_cli_agent.engine.gemini import GeminiEngine

        assert ClaudeEngine.dedups_resumed_instructions is False
        assert CodexEngine.dedups_resumed_instructions is True
        assert GeminiEngine.dedups_resumed_instructions is True

    def test_대체_엔진은_이번_턴에_쓸_엔진을_따른다(self) -> None:
        from slack_cli_agent.engine.runner import FallbackEngine

        class _Primary:
            def resumes_with_opening_instructions(self) -> bool:
                return True

        class _Secondary:
            def resumes_with_opening_instructions(self) -> bool:
                return False

        fallback = FallbackEngine.__new__(FallbackEngine)
        fallback.primary = _Primary()  # type: ignore[assignment]
        fallback.secondary = _Secondary()  # type: ignore[assignment]

        class _Switch:
            def __init__(self, on: bool) -> None:
                self.on = on

            def load(self) -> bool:
                return self.on

            def is_approved(self) -> bool:
                return self.on

        fallback.switcher = _Switch(False)  # type: ignore[assignment]
        assert fallback.resumes_with_opening_instructions() is True
        fallback.switcher = _Switch(True)  # type: ignore[assignment]
        assert fallback.resumes_with_opening_instructions() is False
