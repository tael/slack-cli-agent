"""종료 검수(finish-review) Stop 훅 판정 시험.

mametchi finish_work_hook.py 의 공통 판정(도메인 문구 제외)을 엔진 기본값으로
이식한 것을 검증한다. 각 판정 함수는 transcript 를 직접 파싱하지 않고
Turn/ToolCall 값 객체를 받는 순수 함수라, 여기서는 그 값 객체를 손으로
만들어 넣는다.
"""

from __future__ import annotations

import json

import pytest

from slack_cli_agent.guard import finish_review
from slack_cli_agent.guard.finish_review import JudgmentRegistry, StopDecision, ToolCall, Turn


def _turn(answer: str, **kwargs) -> Turn:
    return Turn(answer=answer, **kwargs)


class TestOffer:
    def test_blocks_when_answer_ends_with_offer_to_help(self):
        turn = _turn("지금까지 로그 두 건을 봤다.\n원하시면 나머지도 이어서 확인해 볼게요")

        hit = finish_review.offer(turn)

        assert hit is not None
        assert finish_review.decide(turn).action == "block"

    def test_allows_when_ask_tag_present(self):
        turn = _turn("삭제는 승인이 필요하다.\n원하시면 지금 바로 진행할게요\n[[ASK]]")

        assert finish_review.offer(turn) is None

    def test_allows_when_offer_phrase_is_only_inside_a_quote(self):
        turn = _turn('지난 턴 금지 문구 "원하시면 이어서 볼게요" 를 다시 쓰지 않았다는 보고다.')

        assert finish_review.offer(turn) is None


class TestHandoff:
    def test_blocks_when_answer_hands_off_without_a_real_permission_wall(self):
        turn = _turn("이 턴에서는 권한 밖이라 할 수 없다. <@U000> 님께 요청해 주세요.")

        hit = finish_review.handoff(turn)

        assert hit is not None
        assert finish_review.decide(turn).action == "block"

    def test_allows_when_a_tool_call_actually_denied_permission(self):
        turn = _turn(
            "이 턴에서는 권한 밖이라 할 수 없다. <@U000> 님께 요청해 주세요.",
            tool_calls=(
                ToolCall(name="Bash", input={"command": "gh repo clone x"}, result="Permission to use denied"),
            ),
        )

        assert finish_review.handoff(turn) is None


class TestGaveUp:
    def test_blocks_when_lookup_failed_and_was_not_retried(self):
        turn = _turn(
            "오더 상태를 조회하지 못했다.",
            tool_calls=(ToolCall(name="Bash", input={"command": "mbs.sh sql 1 'select 1'"}, result="error"),),
        )

        hit = finish_review.gave_up(turn)

        assert hit is not None
        assert finish_review.decide(turn).action == "block"

    def test_allows_when_same_tool_was_called_again(self):
        turn = _turn(
            "오더 상태를 조회하지 못했다.",
            tool_calls=(
                ToolCall(name="Bash", input={"command": "a"}, result="error"),
                ToolCall(name="Bash", input={"command": "b"}, result="ok"),
            ),
        )

        assert finish_review.gave_up(turn) is None


class TestStoppedUnfinished:
    def test_blocks_the_top40_answer(self):
        turn = _turn(
            "티켓은 만들었습니다. 다만 구독 삭제는 아직 실행하지 못했습니다. "
            "Tableau에 로그인할 수단이 없어서예요."
        )

        assert finish_review.stopped_unfinished(turn) is not None
        assert finish_review.decide(turn).action == "block"

    def test_allows_when_waiting_on_ask(self):
        turn = _turn("운영 반영은 진행하지 못했습니다. 승인 주시면 진행합니다.\n[[ASK]]\n[[NOWATCH]]")

        assert finish_review.stopped_unfinished(turn) is None

    def test_allows_finished_answer(self):
        turn = _turn("구독 28건을 해지했고 재조회 잔존은 0건입니다.")

        assert finish_review.stopped_unfinished(turn) is None


class TestNotOpened:
    def test_blocks_when_answer_admits_skipping_the_open(self):
        turn = _turn("본문을 열어 보지 않아 완료조건 판단을 하지 못했다.")

        hit = finish_review.not_opened(turn)

        assert hit is not None
        assert finish_review.decide(turn).action == "block"

    def test_allows_when_subject_is_the_system_not_the_model(self):
        turn = _turn("그 서버가 설정 파일을 읽지 못해 500 을 돌려줬다.")

        assert finish_review.not_opened(turn) is None

    def test_blocks_when_unopened_original_leaves_a_guess(self):
        turn = _turn("어떤 워크플로인지는 원본 메시지를 열어 보지 않아 이름까지는 모릅니다.")

        assert finish_review.not_opened(turn) is not None

    def test_allows_reported_speech_about_the_rule(self):
        turn = _turn("원문을 열지 않았다고 쓰거나 그래서 모른다고 쓴 문장을 막도록 판정을 넓혔다.")

        assert finish_review.not_opened(turn) is None

    def test_allows_policy_sentence_about_not_reading_files(self):
        turn = _turn("마메치는 이 파일을 자동으로 읽지 않으므로 동작에는 영향이 없습니다.")

        assert finish_review.not_opened(turn) is None


class TestAbsenceUnopened:
    def test_blocks_when_ready_but_nothing_was_opened(self):
        turn = _turn(
            "로그가 수집되지 않아 규모를 측정할 수 없다.",
            user_prompt="단말 로그가 이제 수집되었으니 확인해보세요.",
        )

        hit = finish_review.absence_unopened(turn)

        assert hit is not None
        assert finish_review.decide(turn).action == "block"

    def test_allows_when_an_artifact_was_actually_opened(self):
        turn = _turn(
            "로그가 수집되지 않아 규모를 측정할 수 없다.",
            user_prompt="단말 로그가 이제 수집되었으니 확인해보세요.",
            tool_calls=(ToolCall(name="Read", input={"file_path": "/tmp/a.log"}, result="(empty)"),),
        )

        assert finish_review.absence_unopened(turn) is None

    def test_allows_when_user_did_not_say_anything_is_ready(self):
        turn = _turn("로그가 수집되지 않아 규모를 측정할 수 없다.", user_prompt="오늘 장애 원인이 뭐야?")

        assert finish_review.absence_unopened(turn) is None


class TestAdvisorSkipped:
    def test_blocks_decision_request_in_advisor_channel_without_fable_call(self):
        turn = _turn("### 방안\n1. A안\n2. B안\n[[ASK]]", is_advisor_channel=True)

        assert finish_review.advisor_skipped(turn) is True
        assert finish_review.decide(turn).action == "block"

    def test_allows_when_fable_was_called_this_turn(self):
        turn = _turn(
            "### 방안\n1. A안\n2. B안\n[[ASK]]",
            is_advisor_channel=True,
            tool_calls=(ToolCall(name="Agent", input={"model": "fable"}, result="ok"),),
        )

        assert finish_review.advisor_skipped(turn) is False

    def test_allows_outside_advisor_channel(self):
        turn = _turn("### 방안\n1. A안\n2. B안\n[[ASK]]", is_advisor_channel=False)

        assert finish_review.advisor_skipped(turn) is False


class TestResolveAdvisorChannel:
    """G16 배선: POST_CHANNEL 이 ADVISOR_CHANNELS 에 들면 자문 채널로 본다."""

    def test_true_when_current_channel_is_in_the_configured_set(self):
        env = {"POST_CHANNEL": "C002", "ADVISOR_CHANNELS": "C001,C002,C003"}

        assert finish_review.resolve_advisor_channel(env) is True

    def test_false_when_current_channel_is_not_configured(self):
        env = {"POST_CHANNEL": "C009", "ADVISOR_CHANNELS": "C001,C002"}

        assert finish_review.resolve_advisor_channel(env) is False

    def test_false_when_no_current_channel(self):
        assert finish_review.resolve_advisor_channel({"ADVISOR_CHANNELS": "C001"}) is False

    def test_false_when_set_unconfigured(self):
        assert finish_review.resolve_advisor_channel({"POST_CHANNEL": "C001"}) is False

    def test_accepts_whitespace_separated_ids(self):
        env = {"POST_CHANNEL": "C002", "ADVISOR_CHANNELS": "C001 C002\tC003"}

        assert finish_review.resolve_advisor_channel(env) is True

    def test_empty_env_is_false(self):
        assert finish_review.resolve_advisor_channel({}) is False


class TestCodeFirstDocOnly:
    """G13: 코드 우선 채널에서 문서만 근거로 답을 끝내지 못하게 막는다.

    mametchi code_first_hook.py 의 판정(doc / code / github_link / code_lead)을
    조직 중립 기본값으로 이식한 것을 검증한다.
    """

    _doc_only = "트래커에서 cm_v1 도 재무리포트로 분류하고 있어요. https://notion.so/abc 참고."

    def test_blocks_doc_only_answer_in_code_first_channel(self):
        turn = _turn(self._doc_only, is_code_first_channel=True)

        assert finish_review.code_first_doc_only(turn) is not None
        assert finish_review.decide(turn).action == "block"

    def test_allows_outside_code_first_channel(self):
        turn = _turn(self._doc_only, is_code_first_channel=False)

        assert finish_review.code_first_doc_only(turn) is None

    def test_allows_when_no_doc_basis(self):
        turn = _turn("배차 로직은 이렇게 동작합니다.", is_code_first_channel=True)

        assert finish_review.code_first_doc_only(turn) is None

    def test_allows_code_led_answer_with_code_lookup_and_repo_link(self):
        turn = _turn(
            "assign.py:42 의 _pick() 이 최근접 기사를 고릅니다. "
            "https://github.com/org/repo/blob/main/assign.py#L42\n\n"
            "대조로 https://notion.so/abc 와 같습니다.",
            is_code_first_channel=True,
            tool_calls=(
                ToolCall(
                    name="Read",
                    input={"file_path": "/Users/x/Projects/repo/assign.py"},
                    result="def _pick(): ...",
                ),
            ),
        )

        assert finish_review.code_first_doc_only(turn) is None

    def test_blocks_when_repo_link_present_but_doc_leads(self):
        turn = _turn(
            "트래커 기준으로는 cm_v1 이 재무리포트입니다. "
            "https://notion.so/abc 코드도 같습니다 "
            "https://github.com/org/repo/blob/main/x.py",
            is_code_first_channel=True,
            tool_calls=(
                ToolCall(name="Bash", input={"command": "git log -1"}, result="ok"),
            ),
        )

        # 코드 조회·저장소 링크는 있으나 노션 링크가 먼저라 문서 중심 답이다.
        assert finish_review.code_first_doc_only(turn) is not None

    def test_blocks_when_code_looked_up_but_no_repo_link(self):
        turn = _turn(
            "코드를 봤는데 notion 트래커와 같습니다 https://notion.so/abc",
            is_code_first_channel=True,
            tool_calls=(
                ToolCall(name="Bash", input={"command": "git show HEAD"}, result="ok"),
            ),
        )

        assert finish_review.code_first_doc_only(turn) is not None


class TestResolveCodeFirstChannel:
    """G13 배선: POST_CHANNEL 이 CODE_FIRST_CHANNELS 에 들면 코드 우선 채널로 본다."""

    def test_true_when_current_channel_is_in_the_configured_set(self):
        env = {"POST_CHANNEL": "C002", "CODE_FIRST_CHANNELS": "C001,C002,C003"}

        assert finish_review.resolve_code_first_channel(env) is True

    def test_false_when_current_channel_is_not_configured(self):
        env = {"POST_CHANNEL": "C009", "CODE_FIRST_CHANNELS": "C001,C002"}

        assert finish_review.resolve_code_first_channel(env) is False

    def test_false_when_no_current_channel(self):
        assert finish_review.resolve_code_first_channel({"CODE_FIRST_CHANNELS": "C001"}) is False

    def test_accepts_whitespace_separated_ids(self):
        env = {"POST_CHANNEL": "C002", "CODE_FIRST_CHANNELS": "C001 C002\tC003"}

        assert finish_review.resolve_code_first_channel(env) is True

    def test_empty_env_is_false(self):
        assert finish_review.resolve_code_first_channel({}) is False


class TestDeniedUnretried:
    def test_blocks_when_a_denied_target_was_never_retried(self):
        turn = _turn(
            "권한이 없어 확인하지 못했다.",
            tool_calls=(
                ToolCall(
                    name="Bash",
                    input={"command": "aws s3 ls s3://my-long-bucket-name-x"},
                    result="permission denied for my-long-bucket-name-x",
                ),
            ),
        )

        hit = finish_review.denied_unretried(turn)

        assert hit is not None
        assert finish_review.decide(turn).action == "block"

    def test_allows_when_the_same_target_was_retried_another_way(self):
        turn = _turn(
            "권한이 없어 확인하지 못했다.",
            tool_calls=(
                ToolCall(
                    name="Bash",
                    input={"command": "aws s3 ls s3://my-long-bucket-name-x --profile a"},
                    result="permission denied for my-long-bucket-name-x",
                ),
                ToolCall(
                    name="Bash",
                    input={"command": "aws s3 ls s3://my-long-bucket-name-x --profile b"},
                    result="ok: file.csv",
                ),
            ),
        )

        assert finish_review.denied_unretried(turn) is None


class TestVerify:
    def test_flags_a_fact_sentence_with_a_number_no_tool_result_supports(self):
        answer = (
            "이번 턴 조회와 원인 분석 결과를 처음부터 끝까지 자세히 정리해서 답한다. " * 10
            + "오늘 발생한 주문 지연 건수는 48291건이고 원인은 큐 적체 때문이다. "
            + "자세한 내용은 위 조회 결과를 참고한다."
        )
        turn = _turn(answer, tool_calls=(ToolCall(name="Bash", input={}, result="큐 상태는 정상이었다"),))

        hits = finish_review.unsupported_claims(turn)

        assert hits
        assert finish_review.decide(turn).action == "block"

    def test_allows_when_the_number_is_grounded_in_a_tool_result(self):
        answer = (
            "이번 턴 조회와 원인 분석 결과를 처음부터 끝까지 자세히 정리해서 답한다. " * 10
            + "오늘 발생한 주문 지연 건수는 48291건이고 원인은 큐 적체 때문이다. "
            + "자세한 내용은 위 조회 결과를 참고한다."
        )
        turn = _turn(answer, tool_calls=(ToolCall(name="Bash", input={}, result="지연 건수 48291, 큐 적체 확인"),))

        assert finish_review.unsupported_claims(turn) == []

    def test_allows_short_answers_without_running_the_check(self):
        turn = _turn("확인했고 특이사항 없다.")

        assert finish_review.unsupported_claims(turn) == []


_LONG_PREFIX = "이번 턴 조회 결과를 처음부터 끝까지 자세히 정리해서 답한다. " * 12


def _fake_runner(stdout: str = "", exc: Exception | None = None):
    calls: list[dict] = []

    def run(cmd, **kwargs):
        calls.append({"cmd": cmd, **kwargs})
        if exc is not None:
            raise exc
        import subprocess as _sp
        return _sp.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    run.calls = calls  # type: ignore[attr-defined]
    return run


class TestModelVerify:
    ANSWER = _LONG_PREFIX + "실패한 검증 코드는 모두 제가 낸 검증 코드이고 원인은 배포 설정 누락이다."

    def _turn(self):
        return _turn(self.ANSWER, tool_calls=(ToolCall(name="Bash", input={}, result="검증 실패 3건, 작성자 미상"),))

    def test_flags_a_numberless_claim_the_model_finds_unsupported(self):
        payload = json.dumps({"unsupported": [
            {"sentence": "실패한 검증 코드는 모두 제가 낸 검증 코드이고", "why": "작성자 근거 없음"}]},
            ensure_ascii=False)
        runner = _fake_runner(stdout="결과: " + payload)
        verifier = finish_review.ModelClaimVerifier(runner=runner, executable="claude", env={})
        turn = self._turn()

        assert finish_review.heuristic_claim_verifier(turn.answer, "검증 실패 3건") == []
        decision = finish_review.decide(turn, claim_verifier=verifier)

        assert decision.action == "block"
        assert decision.findings[0].name == "verify"
        assert "제가 낸 검증 코드" in decision.findings[0].detail
        assert verifier.status == "ok"
        call = runner.calls[0]
        assert call["cmd"][:4] == ["claude", "-p", "--model", "sonnet"]
        assert "검증 실패 3건" in call["input"] and call["env"][finish_review.VERIFY_NESTED_ENV] == "1"

    def test_model_failure_passes_and_records_status(self):
        import subprocess as _sp
        verifier = finish_review.ModelClaimVerifier(
            runner=_fake_runner(exc=_sp.TimeoutExpired("claude", 90)), executable="claude", env={})

        decision = finish_review.decide(self._turn(), claim_verifier=verifier)

        assert decision.action == "allow"
        assert verifier.status == "error:TimeoutExpired"

    def test_no_json_passes_without_heuristic_fallback(self):
        answer = _LONG_PREFIX + "오늘 지연 건수는 48291건이고 원인은 큐 적체 때문이다."
        turn = _turn(answer, tool_calls=(ToolCall(name="Bash", input={}, result="큐 정상"),))
        verifier = finish_review.ModelClaimVerifier(runner=_fake_runner(stdout="모름"), executable="claude", env={})

        assert finish_review.decide(turn, claim_verifier=verifier).action == "allow"
        assert verifier.status == "error:no_json"

    def test_stops_blocking_after_max_blocks(self):
        payload = json.dumps({"unsupported": [{"sentence": "모두 제가 낸 검증 코드", "why": "x"}]}, ensure_ascii=False)
        runner = _fake_runner(stdout=payload)
        verifier = finish_review.ModelClaimVerifier(runner=runner, executable="claude", env={})

        decision = finish_review.decide(
            self._turn(), claim_verifier=verifier, block_count=finish_review.VERIFY_MAX_BLOCKS)

        assert decision.action == "allow"
        assert runner.calls == []

    def test_rewrite_after_first_block_is_verified_again(self):
        payload = json.dumps({"unsupported": [{"sentence": "모두 제가 낸 검증 코드", "why": "x"}]}, ensure_ascii=False)
        runner = _fake_runner(stdout=payload)
        verifier = finish_review.ModelClaimVerifier(runner=runner, executable="claude", env={})

        decision = finish_review.decide(self._turn(), claim_verifier=verifier, block_count=1)

        assert decision.action == "block"
        assert len(runner.calls) == 1

    def test_short_answer_skips_the_model(self):
        runner = _fake_runner(stdout='{"unsupported": [{"sentence": "a", "why": "b"}]}')
        verifier = finish_review.ModelClaimVerifier(runner=runner, executable="claude", env={})
        turn = _turn("원인은 배포 설정 누락이고 모두 제가 낸 코드다.")

        assert finish_review.decide(turn, claim_verifier=verifier).action == "allow"
        assert runner.calls == []

    def test_skips_near_deadline(self):
        runner = _fake_runner(stdout='{"unsupported": [{"sentence": "a", "why": "b"}]}')
        verifier = finish_review.ModelClaimVerifier(runner=runner, executable="claude", env={})

        decision = finish_review.decide(self._turn(), claim_verifier=verifier, seconds_left=300)

        assert decision.action == "allow" and runner.calls == []

    def test_nested_call_does_not_verify_itself(self):
        runner = _fake_runner(stdout='{"unsupported": [{"sentence": "a", "why": "b"}]}')
        verifier = finish_review.ModelClaimVerifier(
            runner=runner, executable="claude", env={finish_review.VERIFY_NESTED_ENV: "1"})

        assert finish_review.decide(self._turn(), claim_verifier=verifier).action == "allow"
        assert verifier.status == "skip_nested" and runner.calls == []

    def test_settings_default_on_and_can_be_turned_off(self):
        assert finish_review.resolve_verify_settings({}) == (True, "sonnet")
        assert finish_review.resolve_verify_settings({"SCA_FINISH_VERIFY": "0"})[0] is False
        assert finish_review.resolve_verify_settings({"SCA_FINISH_VERIFY_MODEL": "opus"}) == (True, "opus")

    def test_pick_evidence_prefers_results_sharing_answer_identifiers(self):
        filler = ["x" * 12000 for _ in range(12)]
        relevant = "billable_fee 갱신은 settle_job 이 한다"
        picked = finish_review.pick_evidence(
            "billable_fee 는 settle_job 이 갱신한다", [relevant, *filler], limit=50_000)

        assert relevant in picked
        assert len(picked) <= 50_000 + 20


class TestAllow:
    def test_decide_allows_a_clean_completed_turn(self):
        turn = _turn(
            "조회를 마쳤다. 오늘 지연 건수는 12건이고 모두 큐 적체 때문이었다. "
            "원인은 재시작 직후 몰린 재처리 큐였다.",
            tool_calls=(ToolCall(name="Bash", input={}, result="지연 12건, 재처리 큐 적체"),),
        )

        decision = finish_review.decide(turn)

        assert decision.action == "allow"
        assert decision.findings == ()
        assert decision.to_hook_json() == {}


class TestDecideReleaseRules:
    def test_releases_when_turn_deadline_is_too_close(self):
        turn = _turn("원하시면 이어서 확인해 볼게요")

        decision = finish_review.decide(turn, seconds_left=10, release_sec=240)

        assert decision.action == "release"

    def test_releases_after_max_blocks_reached(self):
        turn = _turn("원하시면 이어서 확인해 볼게요")

        decision = finish_review.decide(turn, block_count=2, max_blocks=2)

        assert decision.action == "release"

    def test_blocks_within_budget(self):
        turn = _turn("원하시면 이어서 확인해 볼게요")

        decision = finish_review.decide(turn, block_count=0, max_blocks=2, seconds_left=1000)

        assert decision.action == "block"
        reason = decision.to_hook_json()["reason"]
        assert "원하시면" in reason

    def test_allows_when_transcript_is_missing(self):
        """DM 안내처럼 --no-session-persistence 로 돈 턴은 transcript 가 없다.
        근거를 못 읽는 채로 검수하면 조회에 성공한 답도 전부 막히므로 건너뛴다."""
        turn = _turn("원하시면 이어서 확인해 볼게요", has_transcript=False)

        assert finish_review.decide(turn).action == "allow"


class TestTimeLeft:
    def test_reads_monotonic_deadline_first(self):
        left = finish_review.time_left({"TURN_DEADLINE_MONO": "100", "TURN_DEADLINE": "9999999999"}, now_monotonic=40)

        assert left == 60

    def test_falls_back_to_wall_clock_deadline(self):
        left = finish_review.time_left({"TURN_DEADLINE": "1000"}, now_wall=400)

        assert left == 600

    def test_none_when_neither_deadline_is_set(self):
        assert finish_review.time_left({}) is None


class TestExtensionPoint:
    """프로필 플러그인이 lastmile 코드/Snowflake 같은 도메인 판정을 더하는 지점.

    이 모듈은 공통 판정만 들고 있고, 도메인 규칙은 JudgmentRegistry.register()
    로 별도 모듈(프로필 플러그인)이 덧붙인다.
    """

    def test_a_plugin_can_register_a_domain_rule_without_editing_this_module(self):
        registry = JudgmentRegistry()
        registry.register(
            "lastmile_code_unchecked",
            check=lambda turn: "코드 158" if "158" in turn.answer else None,
            guidance=lambda hit: f"{hit} 를 lastmile enum 과 대조한다.",
        )
        turn = _turn("코드 158 의 뜻은 확인하지 못했다.")

        findings = registry.evaluate(turn)

        assert len(findings) == 1
        assert findings[0].name == "lastmile_code_unchecked"

    def test_default_registry_has_exactly_the_common_judgments(self):
        assert set(finish_review.default_registry.names()) == {
            "gave_up",
            "not_opened",
            "stopped_unfinished",
            "absence_unopened",
            "offer",
            "handoff",
            "denied_unretried",
            "advisor_skipped",
            "code_first",
            "verify",
        }

    def test_registering_on_default_registry_is_seen_by_decide(self):
        try:
            finish_review.default_registry.register(
                "always_fires", check=lambda turn: "x", guidance=lambda hit: "항상 막힘",
            )
            turn = _turn("아무 문제 없는 평범한 답이다.")

            assert finish_review.decide(turn).action == "block"
        finally:
            finish_review.default_registry.unregister("always_fires")

        turn = _turn("아무 문제 없는 평범한 답이다.")
        assert finish_review.decide(turn).action == "allow"


class TestStopDecisionHookJson:
    def test_block_emits_claude_code_stop_hook_protocol(self):
        finding = finish_review.Finding(name="offer", detail="x", guidance="고쳐라")
        decision = StopDecision(action="block", findings=(finding,))

        assert decision.to_hook_json() == {
            "decision": "block",
            "reason": "고쳐라. 답 끝의 표식과 WATCH 판정 줄은 그대로 둔다.",
        }

    @pytest.mark.parametrize("action", ["allow", "release"])
    def test_allow_and_release_emit_nothing(self, action):
        decision = StopDecision(action=action)

        assert decision.to_hook_json() == {}
