"""OutputGuard 파이프라인 시험.

mentions.py·watch.py 이식 대상의 기대 출력은 원본 `bot.py` 의
`fix_plain_mentions`, `guard_wrong_addressee` 를 AST 로 뽑아 실제로 실행해
얻었다(손으로 짐작하지 않았다).
"""

from __future__ import annotations

import pytest

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.guard.base import GuardContext, GuardResult, OutputGuard, RerunRequest
from slack_cli_agent.guard.mentions import AddresseeGuard, PlainMentionGuard
from slack_cli_agent.guard.pipeline import GuardPipeline, PipelineResult
from slack_cli_agent.guard.rewrite import RewriteLossGuard
from slack_cli_agent.guard.watch import PROMISE_WITHOUT_WATCH_RE, WATCH_RE, WatchPromiseGuard

# ---------------------------------------------------------------------------
# base.py — 계약 자체


class _AlwaysChanges(OutputGuard):
    name = "always_changes"

    def apply(self, body: str, ctx: GuardContext) -> GuardResult:
        return GuardResult(body=body + "!", changed=True, detail={"added": "!"})


class _NeverChanges(OutputGuard):
    name = "never_changes"

    def apply(self, body: str, ctx: GuardContext) -> GuardResult:
        return GuardResult(body=body, changed=False)


class _RequestsRerun(OutputGuard):
    name = "requests_rerun"

    def apply(self, body: str, ctx: GuardContext) -> GuardResult:
        return GuardResult(
            body=body,
            changed=False,
            rerun=RerunRequest(reason="test", rewrite_prompt="다시 써라", guard_name=self.name),
        )


class TestGuardContext:
    def test_defaults_are_empty(self) -> None:
        ctx = GuardContext()
        assert ctx.mention_names == {}
        assert ctx.previous_body is None
        assert ctx.is_rewrite_retry is False


# ---------------------------------------------------------------------------
# mentions.py — PlainMentionGuard (원본 fix_plain_mentions 이식)


class TestPlainMentionGuard:
    @pytest.fixture
    def names(self) -> dict[str, str]:
        return {"김서준": "U0EXAMPLE01", "김서준 개발팀": "U0EXAMPLE02"}

    def test_replaces_longest_name_first(self, names: dict[str, str]) -> None:
        """이름이 긴 것부터 바꾼다. 짧은 이름을 먼저 바꾸면 뒤 토막이 남는다."""
        guard = PlainMentionGuard()
        ctx = GuardContext(mention_names=names)
        result = guard.apply("@김서준 개발팀 님 확인 부탁드립니다", ctx)
        assert result.changed is True
        assert result.body == "<@U0EXAMPLE02> 확인 부탁드립니다"
        assert result.detail["names"] == ["김서준 개발팀"]

    def test_code_span_is_untouched(self, names: dict[str, str]) -> None:
        guard = PlainMentionGuard()
        ctx = GuardContext(mention_names=names)
        body = "코드에 `@김서준` 있음"
        result = guard.apply(body, ctx)
        assert result.changed is False
        assert result.body == body

    def test_fenced_code_block_is_untouched(self, names: dict[str, str]) -> None:
        guard = PlainMentionGuard()
        ctx = GuardContext(mention_names=names)
        body = "```\n@김서준 예시\n```"
        result = guard.apply(body, ctx)
        assert result.changed is False
        assert result.body == body

    def test_unknown_name_is_untouched(self, names: dict[str, str]) -> None:
        guard = PlainMentionGuard()
        ctx = GuardContext(mention_names=names)
        body = "@모름 님"
        result = guard.apply(body, ctx)
        assert result.changed is False
        assert result.body == body

    def test_empty_table_is_untouched(self) -> None:
        guard = PlainMentionGuard()
        ctx = GuardContext()
        body = "@김서준 님"
        result = guard.apply(body, ctx)
        assert result.changed is False
        assert result.body == body


# ---------------------------------------------------------------------------
# mentions.py — AddresseeGuard (원본 guard_wrong_addressee 이식 + 발신 문구 결합)


class TestAddresseeGuard:
    def test_addressing_the_asker_is_untouched(self) -> None:
        guard = AddresseeGuard()
        ctx = GuardContext(asker_id="U123", is_owner=False, owner_user_id="UOWNER")
        body = "<@U123> 님 확인했습니다"
        result = guard.apply(body, ctx)
        assert result.changed is False
        assert result.body == body

    def test_wrong_target_is_stripped_and_disclosed(self) -> None:
        """지운 것을 조용히 넘기지 않는다 — 본문에 무엇을 지웠는지 남긴다."""
        guard = AddresseeGuard()
        ctx = GuardContext(asker_id="U123", is_owner=False, owner_user_id="UOWNER")
        result = guard.apply("<@U999> 님 확인했습니다", ctx)
        assert result.changed is True
        assert result.detail["wrong_target"] == "U999"
        assert result.body == "확인했습니다\n\n(다른 분을 부르는 첫머리를 지웠습니다.)"

    def test_owner_calling_owner_id_is_untouched(self) -> None:
        """소유자가 자기 자신을 부르는 것은 예외로 허용된다."""
        guard = AddresseeGuard()
        ctx = GuardContext(asker_id="U123", is_owner=True, owner_user_id="UOWNER")
        body = "<@UOWNER> 님 확인했습니다"
        result = guard.apply(body, ctx)
        assert result.changed is False
        assert result.body == body

    def test_no_leading_mention_is_untouched(self) -> None:
        guard = AddresseeGuard()
        ctx = GuardContext(asker_id="U123", owner_user_id="UOWNER")
        body = "그냥 본문"
        result = guard.apply(body, ctx)
        assert result.changed is False
        assert result.body == body


# ---------------------------------------------------------------------------
# rewrite.py — RewriteLossGuard (원본 late_rewrite_lost_content 이식 + 병합)


class TestRewriteLossGuard:
    def _settings(self) -> RuntimeSettings:
        return RuntimeSettings()

    def test_no_previous_body_means_no_judgement(self) -> None:
        guard = RewriteLossGuard(self._settings())
        result = guard.apply("아무 답", GuardContext())
        assert result.changed is False

    def test_short_before_is_not_judged_by_ratio(self) -> None:
        """LATE_REWRITE_MIN_CHARS 아래는 애초에 짧은 답이라 비율로 재지 않는다."""
        guard = RewriteLossGuard(self._settings())
        ctx = GuardContext(previous_body="짧다")
        result = guard.apply("더 짧", ctx)
        assert result.changed is False

    def test_shrunk_past_ratio_keeps_both(self) -> None:
        """40% 이상 짧아지면 유실로 보고, 앞 답을 버리지 않고 뒤에 잇는다."""
        guard = RewriteLossGuard(self._settings())
        before = "가" * 250
        after = "가" * 100
        ctx = GuardContext(previous_body=before)
        result = guard.apply(after, ctx)
        assert result.changed is True
        assert result.detail["lost"] is True
        assert result.body == f"{before}\n\n{after}"

    def test_within_ratio_is_untouched(self) -> None:
        guard = RewriteLossGuard(self._settings())
        before = "가" * 250
        after = "가" * 200
        ctx = GuardContext(previous_body=before)
        result = guard.apply(after, ctx)
        assert result.changed is False
        assert result.body == after


# ---------------------------------------------------------------------------
# watch.py — WatchPromiseGuard (원본 WATCH_RE·PROMISE_WITHOUT_WATCH_RE 이식)


class TestWatchPromiseGuard:
    def test_watch_tag_is_extracted_and_stripped(self) -> None:
        guard = WatchPromiseGuard()
        body = "확인했습니다.\n\n[[WATCH: dag_id=foo run_id=bar]]"
        result = guard.apply(body, GuardContext())
        assert result.changed is True
        assert result.body == "확인했습니다."
        assert result.detail["watch_desc"] == "dag_id=foo run_id=bar"
        assert result.rerun is None

    def test_plain_answer_is_untouched(self) -> None:
        guard = WatchPromiseGuard()
        body = "그냥 답변입니다"
        result = guard.apply(body, GuardContext())
        assert result.changed is False
        assert result.rerun is None

    def test_promise_without_tag_requests_rerun_without_calling_engine(self) -> None:
        """엔진 재호출은 이 작업의 범위 밖이다 — rerun 요청만 돌려준다."""
        guard = WatchPromiseGuard()
        body = "지켜보다가 끝나면 보고하겠습니다."
        result = guard.apply(body, GuardContext())
        assert result.changed is False
        assert result.body == body  # 직접 고치지 않는다
        assert result.detail["promise_without_watch"] is True
        assert result.rerun is not None
        assert result.rerun.guard_name == "watch_promise"
        assert "[[WATCH:" in result.rerun.rewrite_prompt

    def test_retry_still_failing_cuts_sentence_and_appends_fallback(self) -> None:
        """재시도에도 실패하면 그 문장을 잘라내고 대체 문구를 붙인다."""
        guard = WatchPromiseGuard()
        body = "지켜보다가 끝나면 보고하겠습니다."
        ctx = GuardContext(is_rewrite_retry=True)
        result = guard.apply(body, ctx)
        assert result.changed is True
        assert result.rerun is None
        assert result.body == "확인이 더 필요하면 다시 말씀해 주세요."
        assert result.detail["promise_without_watch_retry_failed"] is True

    def test_regexes_match_original(self) -> None:
        """정규식 자체가 원본과 같은 문자열에 일치하는지 본다."""
        assert WATCH_RE.search("그냥 답변입니다") is None
        assert PROMISE_WITHOUT_WATCH_RE.search("확인했습니다. 추가 조치는 없습니다.") is None
        assert PROMISE_WITHOUT_WATCH_RE.search("배포 후 반영되면 말씀드릴게요.") is not None


# ---------------------------------------------------------------------------
# pipeline.py — GuardPipeline


class TestGuardPipeline:
    def test_applies_guards_in_order_and_records_details(self) -> None:
        pipeline = GuardPipeline([_NeverChanges(), _AlwaysChanges()])
        result = pipeline.run("본문", GuardContext())
        assert isinstance(result, PipelineResult)
        assert result.body == "본문!"
        assert result.changed is True
        assert result.applied == ("always_changes",)
        assert result.details == {"always_changes": {"added": "!"}}
        assert result.rerun is None

    def test_no_guard_changes_anything(self) -> None:
        pipeline = GuardPipeline([_NeverChanges()])
        result = pipeline.run("본문", GuardContext())
        assert result.body == "본문"
        assert result.changed is False
        assert result.applied == ()

    def test_rerun_stops_pipeline_without_calling_engine(self) -> None:
        """rerun 을 받으면 그 자리에서 멈추고 이후 가드는 적용하지 않는다.

        엔진을 실제로 다시 부르는 것은 파이프라인의 책임이 아니다 — 여기서는
        rerun 요청이 그대로 호출부로 전달되는지만 본다.
        """
        after_rerun = _AlwaysChanges()
        pipeline = GuardPipeline([_RequestsRerun(), after_rerun])
        result = pipeline.run("본문", GuardContext())
        assert result.rerun is not None
        assert result.rerun.reason == "test"
        assert result.rerun.rewrite_prompt == "다시 써라"
        # 뒤에 놓인 가드는 실행되지 않았다 — 본문이 바뀌지 않았다.
        assert result.body == "본문"
        assert result.changed is False

    def test_watch_promise_guard_rerun_travels_through_pipeline(self) -> None:
        """실제 WatchPromiseGuard 를 넣어도 같은 방식으로 멈춘다."""
        pipeline = GuardPipeline([WatchPromiseGuard()])
        result = pipeline.run("지켜보다가 끝나면 보고하겠습니다.", GuardContext())
        assert result.rerun is not None
        assert result.rerun.guard_name == "watch_promise"


# ---------------------------------------------------------------------------
# dropline.py — ConfiguredLineDropGuard (원본 drop_vooster 이식, 문구는 설정으로 받는다)


class TestConfiguredLineDropGuard:
    def test_기본값_빈_목록이면_아무것도_지우지_않는다(self) -> None:
        from slack_cli_agent.guard.dropline import ConfiguredLineDropGuard

        guard = ConfiguredLineDropGuard(RuntimeSettings())
        body = "본문\n\n테스트 안내가 켜져 있습니다\n"
        result = guard.apply(body, GuardContext())
        assert result.changed is False
        assert result.body == body

    def test_설정된_문구로_시작하는_줄을_지운다(self) -> None:
        from slack_cli_agent.guard.dropline import ConfiguredLineDropGuard

        settings = RuntimeSettings().override(
            {"dropped_line_heads": ["테스트 안내가 켜져 있습니다"]}
        )
        guard = ConfiguredLineDropGuard(settings)
        body = "본문입니다.\n\n테스트 안내가 켜져 있습니다 — 자세한 내용은 여기.\n"
        result = guard.apply(body, GuardContext())
        assert result.changed is True
        assert result.body == "본문입니다."
        assert result.detail["dropped"] == ["테스트 안내가 켜져 있습니다"]

    def test_앞에_붙는_대시를_함께_지운다(self) -> None:
        from slack_cli_agent.guard.dropline import ConfiguredLineDropGuard

        settings = RuntimeSettings().override(
            {"dropped_line_heads": ["테스트 안내가 켜져 있습니다"]}
        )
        guard = ConfiguredLineDropGuard(settings)
        body = "본문입니다.\n\n— 테스트 안내가 켜져 있습니다\n"
        result = guard.apply(body, GuardContext())
        assert result.changed is True
        assert result.body == "본문입니다."

    def test_문장_중간에_섞인_경우는_지우지_않는다(self) -> None:
        """줄 단위로만 판단한다 — 그 줄이 인사 형태로 시작할 때만 지운다."""
        from slack_cli_agent.guard.dropline import ConfiguredLineDropGuard

        settings = RuntimeSettings().override(
            {"dropped_line_heads": ["테스트 안내가 켜져 있습니다"]}
        )
        guard = ConfiguredLineDropGuard(settings)
        body = "누군가 테스트 안내가 켜져 있습니다 라고 인용했습니다."
        result = guard.apply(body, GuardContext())
        assert result.changed is False
        assert result.body == body

    def test_여러_문구_중_하나만_있어도_지운다(self) -> None:
        from slack_cli_agent.guard.dropline import ConfiguredLineDropGuard

        settings = RuntimeSettings().override(
            {"dropped_line_heads": ["문구1", "문구2"]}
        )
        guard = ConfiguredLineDropGuard(settings)
        body = "본문.\n\n문구2 — 부가 설명\n"
        result = guard.apply(body, GuardContext())
        assert result.changed is True
        assert result.body == "본문."
        assert result.detail["dropped"] == ["문구2"]
