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


# mentions.py — BotMentionGuard (sca-c4m)


class _가짜봇판정:
    """user ID -> 봇인가. 조회 횟수를 센다."""

    def __init__(self, bots: set[str], names: dict[str, str] | None = None) -> None:
        self._bots = bots
        self._names = names or {}
        self.calls: list[str] = []

    def is_bot(self, user_id: str) -> bool:
        self.calls.append(user_id)
        return user_id in self._bots

    def display_name(self, user_id: str) -> str:
        return self._names.get(user_id, "")


class Test봇_멘션을_지운다:
    """엔진 답에 다른 봇의 <@U...> 가 들어가면 그 봇이 깨어난다.

    app_mention 은 bot_id 를 안 거르므로(sca-3ee) 수신에서 막을 수 없고,
    막는 자리는 발신측이다. 답 A 가 봇 B 를 부르고 B 의 답이 A 를 부르면
    둘이 서로를 계속 깨운다.
    """

    def _guard(self, bots: set[str], names: dict[str, str] | None = None):
        from slack_cli_agent.guard.mentions import BotMentionGuard

        판정 = _가짜봇판정(bots, names)
        return BotMentionGuard(is_bot=판정.is_bot, display_name=판정.display_name), 판정

    def test_봇_멘션은_표시_이름_평문으로_바뀐다(self) -> None:
        guard, _ = self._guard({"U0REI"}, {"U0REI": "레이"})

        result = guard.apply("<@U0REI> 에게 물어보세요", GuardContext())

        assert result.body == "레이 에게 물어보세요"
        assert result.changed is True
        assert result.detail == {"targets": ["U0REI"]}

    def test_사람_멘션은_그대로_둔다(self) -> None:
        """사람을 부르는 것은 이 봇의 기능이다. PlainMentionGuard 가 일부러
        @이름 을 멘션으로 바꾸는데 여기서 되돌리면 그 기능이 죽는다."""
        guard, _ = self._guard({"U0REI"})

        result = guard.apply("<@U0HUMAN> 확인 부탁드립니다", GuardContext())

        assert result.body == "<@U0HUMAN> 확인 부탁드립니다"
        assert result.changed is False

    def test_표시_이름을_모르면_멘션만_지운다(self) -> None:
        guard, _ = self._guard({"U0REI"})

        result = guard.apply("먼저 <@U0REI> 를 부르세요", GuardContext())

        assert "<@" not in result.body
        assert result.changed is True

    def test_이름이_붙은_멘션_형식도_바꾼다(self) -> None:
        """슬랙은 <@U123|표시이름> 형태로도 보낸다. 이 형태를 놓치면
        루프가 그대로 난다."""
        guard, _ = self._guard({"U0REI"}, {"U0REI": "레이"})

        result = guard.apply("<@U0REI|rei> 확인", GuardContext())

        assert result.body == "레이 확인"

    def test_코드_안의_멘션은_안_건드린다(self) -> None:
        """예시로 적은 것은 실제로 부르지 않는다. PlainMentionGuard 가 같은
        이유로 코드 구간을 비켜 간다."""
        guard, _ = self._guard({"U0REI"}, {"U0REI": "레이"})

        body = "이렇게 씁니다 : `<@U0REI>`"
        result = guard.apply(body, GuardContext())

        assert result.body == body
        assert result.changed is False

    def test_같은_봇을_여러_번_불러도_조회는_한_번이다(self) -> None:
        guard, 판정 = self._guard({"U0REI"}, {"U0REI": "레이"})

        guard.apply("<@U0REI> 와 <@U0REI>", GuardContext())

        assert 판정.calls == ["U0REI"]

    def test_봇이_없으면_본문이_그대로다(self) -> None:
        guard, _ = self._guard(set())

        result = guard.apply("그냥 답", GuardContext())

        assert result.changed is False


class Test봇_호출을_허용한_채널:
    """bot_mentions 를 켠 채널은 다른 봇에게 일을 넘길 수 있어야 한다.

    평문 이름은 링크도 알림도 안 되므로 넘기려면 진짜 멘션이 필요하다.
    루프는 답이 물어본 봇을 되부를 때 닫히므로 그 갈래만 자른다.
    """

    def _guard(self, allowed: bool):
        from slack_cli_agent.guard.mentions import BotMentionGuard

        return BotMentionGuard(
            is_bot=lambda uid: uid.startswith("UB"),
            display_name=lambda uid: {"UBASKER": "아스카", "UBOTHER": "레이"}[uid],
            allowed_in=lambda channel: allowed,
        )

    def test_다른_봇_멘션은_남는다(self) -> None:
        guard = self._guard(allowed=True)

        result = guard.apply(
            "<@UBOTHER> 조사 부탁드립니다",
            GuardContext(channel="C1", asker_id="U0HUMAN"),
        )

        assert result.body == "<@UBOTHER> 조사 부탁드립니다"
        assert result.changed is False

    def test_물어본_봇_멘션은_지운다(self) -> None:
        """이 갈래를 남기면 그 봇이 다시 깨어나 서로를 계속 깨운다."""
        guard = self._guard(allowed=True)

        result = guard.apply(
            "<@UBASKER> 확인했습니다",
            GuardContext(channel="C1", asker_id="UBASKER"),
        )

        assert result.body == "아스카 확인했습니다"
        assert result.detail == {"targets": ["UBASKER"]}

    def test_끄면_모든_봇_멘션을_지운다(self) -> None:
        guard = self._guard(allowed=False)

        result = guard.apply(
            "<@UBOTHER> 부탁드립니다",
            GuardContext(channel="C1", asker_id="U0HUMAN"),
        )

        assert result.body == "레이 부탁드립니다"

    def test_설정_조회가_실패하면_지운다(self) -> None:
        """읽을 수 없는 설정은 허용이 아니라 기존 동작으로 떨어진다."""
        from slack_cli_agent.guard.mentions import BotMentionGuard

        def raises(channel: str) -> bool:
            raise RuntimeError("설정 파일 없음")

        guard = BotMentionGuard(
            is_bot=lambda uid: True, display_name=lambda uid: "레이", allowed_in=raises
        )

        result = guard.apply("<@UBOTHER> 부탁", GuardContext(channel="C1"))

        assert result.body == "레이 부탁"


class Test코드_구간_보호가_안_샌다:
    """가드가 코드 구간을 비켜 가는 절차의 결함 2건 (sca-9qv, 코덱스 리뷰).

    예시로 적은 멘션을 진짜 멘션으로 바꾸면 엉뚱한 사람이나 봇을 부른다.
    """

    def _guard(self):
        from slack_cli_agent.guard.mentions import BotMentionGuard

        return BotMentionGuard(is_bot=lambda uid: True, display_name=lambda uid: "레이")

    @pytest.mark.parametrize(
        "본문",
        [
            "`<@U0REI>`",
            "``<@U0REI>``",
            "```<@U0REI>```",
            "```\n<@U0REI>\n```",
            "````<@U0REI>````",
        ],
    )
    def test_백틱_개수와_무관하게_보호된다(self, 본문: str) -> None:
        """마크다운은 백틱을 몇 개든 같은 수로 닫으면 코드 구간이다. 홑겹과
        세겹만 보면 겹백틱 예시의 안쪽이 바뀐다."""
        result = self._guard().apply(본문, GuardContext())

        assert result.body == 본문
        assert result.changed is False

    def test_여는_백틱과_닫는_백틱_수가_다르면_코드_구간이_아니다(self) -> None:
        """길이가 다른 백틱 run 은 코드 구간을 열지 않는다. 그런데도 구간으로
        읽으면 그 안의 진짜 멘션이 가려져 가드를 통째로 건너뛴다(코덱스 리뷰)."""
        result = self._guard().apply("````<@U0REI> ```", GuardContext())

        assert "레이" in result.body
        assert "<@U0REI>" not in result.body

    def test_본문에_구분자로_쓰는_문자가_있어도_안_깨진다(self) -> None:
        """코드 구간을 빼 둘 때 쓰는 표식이 본문에 이미 있으면 되돌리는
        단계가 그것을 구간 번호로 읽는다. 구간이 없으면 예외가 나고 있으면
        본문이 다른 조각으로 바뀐다."""
        본문 = "표식 \x000\x00 과 `코드` 와 <@U0REI>"

        result = self._guard().apply(본문, GuardContext())

        assert "\x000\x00" in result.body
        assert "`코드`" in result.body
        assert "레이" in result.body

    def test_평문_멘션_가드도_같은_보호를_받는다(self) -> None:
        """두 가드가 같은 절차를 쓴다. 한쪽만 고치면 다른 쪽이 그대로 샌다."""
        guard = PlainMentionGuard()

        result = guard.apply("``@레이``", GuardContext(mention_names={"레이": "U0REI"}))

        assert result.body == "``@레이``"
