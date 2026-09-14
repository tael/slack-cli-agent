"""요청 처리 파이프라인.

세션 판정부터 슬랙 발신까지 요청 하나를 끝까지 처리한다. 원본 bot.py 의
``handle_request`` 에 해당한다. ``core.ports.RequestHandler`` 계약을
구현해 워커와 분리한다 — 워커는 이 계약(``HandleOutcome``)만 알면 되고,
화자 판정·세션·프롬프트 조립·엔진 실행·가드 보정·발신의 실제 조립은
이 클래스가 맡는다.

예외를 밖으로 내지 않는다. 어떤 단계에서 실패해도 ``HandleOutcome(ok=False)``
로 돌려준다 — 워커가 예외 처리를 대신하면 그 사유가 큐에 남지 않는다.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from ..auth.policy import AccessPolicy
from ..auth.principal import Principal, TrustLevel
from ..config.channel import ChannelRegistry
from ..engine.base import Engine, EngineRequest, EngineResponse, Usage
from ..engine.runner import EngineInvoker
from ..guard.base import GuardContext
from ..guard.pipeline import GuardPipeline
from ..guard.watch import WatchPromiseGuard
from ..observability.audit import AuditLog
from ..observability.response_archive import ResponseArchive
from ..observability.slow_report import SlowRequestMeta, SlowRequestReporter, tail_output
from ..prompt.composer import SystemPromptComposer
from ..prompt.sections import SILENT_MARK, CompositionContext
from ..reliability.watchjobs import WatchJobPort
from ..session.manager import SessionDecision, SessionManager
from ..session.ports import SessionKey, SessionScope
from ..slack.late_addendum import LateAddendumChecker, ThreadConsumption, late_addendum_prompt
from ..slack.publisher import MessagePublisher
from ..slack.reactions import ReactionMarker
from ..slack.transcript import TranscriptBuilder
from .context import RequestContext
from .ports import HandleOutcome

log = logging.getLogger(__name__)

class RequestPipeline:
    """요청 하나를 끝까지 처리한다. ``core.ports.RequestHandler`` 를 구현한다.

    ``reactions`` 는 처리 상태 리액션(``slack.reactions.ReactionMarker`` 상당)을
    다는 대상이다. ``None`` 이면 표식을 안 단다 — 시험이나 표식이 필요 없는
    자리에서 선택으로 뺄 수 있게 한다.
    """

    def __init__(
        self,
        *,
        access_policy: AccessPolicy,
        transcript_builder: TranscriptBuilder,
        prompt_composer: SystemPromptComposer,
        session_manager: SessionManager,
        engine: Engine,
        # 엔진 실행 한 걸음. 실행기를 직접 받지 않는다 — 폴백이 설정돼
        # 있어도 `EngineRunner.run()` 을 직접 부르면 `FallbackEngine.run()`
        # 이 안 불려 한도 소진 때 전환과 상태 기록이 건너뛰어진다.
        invoker: EngineInvoker,
        guard_pipeline: GuardPipeline,
        publisher: MessagePublisher,
        audit: AuditLog,
        channels: ChannelRegistry,
        default_workdir: Path,
        owner_user_id: str = "",
        reactions: ReactionMarker | None = None,
        name_resolver: Callable[[str], str] = lambda user_id: user_id,
        mention_table: Callable[[], Mapping[str, str]] = dict,
        slow_reporter: SlowRequestReporter | None = None,
        # 스레드에 함께 있는 사람을 추리는 함수. 안 주면 그 대목을 프롬프트에
        # 안 붙인다 — 슬랙 스레드 조회가 한 번 더 나가므로 기본은 끈 상태다.
        participants: Callable[[str, str], tuple[tuple[str, str], ...]] | None = None,
        # 발송 직전 스레드 재확인. 안 주면 그 확인을 아예 안 한다 — 슬랙
        # 조회가 한 번 더 나가므로 조립 코드에서 선택한다.
        late_addendum: LateAddendumChecker | None = None,
        # 재확인이 흡수한 말을 대기줄이 또 실행하지 않게 적어 두는 자리.
        # 두 경로가 같은 기록을 봐야 하므로 밖에서 하나를 만들어 공유한다.
        consumption: ThreadConsumption | None = None,
        # 지켜보겠다는 약속을 등록할 감시 큐. 안 주면 등록을 안 한다 —
        # 가드가 태그를 뽑아내도 그 값을 넣을 곳이 없으면 아무도 다시 확인하지
        # 않는다. 원본 `register_watch_job()` 호출 위치에 대응한다.
        watch_queue: WatchJobPort | None = None,
        # 올린 응답을 채널별 날짜 파일로 남기는 곳. 안 주면 안 남긴다 —
        # 학습 배치가 읽는 자료가 이것뿐이라, 여기서 빠지면 그 배치는 매일
        # "응답 기록이 없다" 로 끝난다. 원본 `archive_response()` 자리다.
        response_archive: ResponseArchive | None = None,
        now: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._access = access_policy
        self._transcript = transcript_builder
        self._composer = prompt_composer
        self._sessions = session_manager
        self._engine = engine
        self._invoker = invoker
        self._guards = guard_pipeline
        self._publisher = publisher
        self._audit = audit
        self._channels = channels
        self._default_workdir = default_workdir
        self._owner_user_id = owner_user_id
        self._reactions = reactions
        self._name_resolver = name_resolver
        self._mention_table = mention_table
        self._slow_reporter = slow_reporter
        self._participants = participants
        self._late_addendum = late_addendum
        self._consumption = consumption
        self._watch_queue = watch_queue
        self._response_archive = response_archive
        self._now = now
        self._monotonic = monotonic

    def handle(self, ctx: RequestContext) -> HandleOutcome:
        try:
            return self._handle(ctx)
        except Exception as exc:  # 예외를 밖으로 내지 않는다  # noqa: BLE001 — 요청 처리기 예외를 밖으로 내지 않는다 — 워커 루프가 이 한 건 실패로 멎으면 안 된다
            failure = str(exc) or exc.__class__.__name__
            self._mark_failed(ctx)
            self._record_best_effort(ctx, failure)
            return HandleOutcome(ok=False, failure=failure)


    def _report_if_slow(
        self,
        ctx: RequestContext,
        decision: Any,
        model: str,
        effort: str,
        elapsed: float,
        mono_elapsed: float,
        response: EngineResponse,
        started: float,
    ) -> None:
        """느린 요청을 보고 경로로 넘긴다. 기준값 판정은 보고기가 한다.

        예외를 밖으로 내지 않는다. 보고는 이미 끝난 요청의 부가 기록이고,
        그것이 실패했다고 성공한 응답을 실패로 뒤집으면 안 된다.
        """
        if self._slow_reporter is None:
            return
        config = self._channels.get(ctx.channel)
        try:
            self._slow_reporter.maybe_report(
                SlowRequestMeta(
                    elapsed_wall=elapsed,
                    mono_elapsed=mono_elapsed,
                    # 세션 기록에는 여러 요청의 이벤트가 누적된다. 이 값을 안 넘기면
                    # 이번 요청이 아니라 세션 시작부터의 전체 경과가 분해 대상이 된다.
                    started=started,
                    model=model,
                    model_actual=response.model_actual,
                    effort=effort,
                    num_turns=response.turns,
                    reason=response.failure_reason,
                    session_id=decision.session_id or "",
                    resume=decision.resume,
                    channel=ctx.channel,
                    channel_name=config.name if config else ctx.channel,
                    text=ctx.text,
                    usage=response.usage,
                    # 정상 종료 경로에는 이 키가 없다. 빈 문자열을 채워 넣으면
                    # 보고에 내용 없는 블록이 나간다.
                    stdout_tail=tail_output(response.raw.get("stdout")) or None,
                    stderr_tail=tail_output(response.raw.get("stderr")) or None,
                )
            )
        except Exception:
            log.exception("느린 요청 보고에 실패했다")

    # -- 본 흐름 -------------------------------------------------------

    def _handle(self, ctx: RequestContext) -> HandleOutcome:
        start = self._now()
        # 벽시계와 단조시계를 함께 잰다. 벽시계만으로는 실제로 느린 것과
        # 기기가 절전에 들어갔던 것이 같은 값으로 나온다.
        mono_start = self._monotonic()
        self._mark_processing(ctx)

        principal = self._access.principal_for(ctx.channel, ctx.user)
        model = self._access.model_for(principal)
        effort = self._access.effort_for(principal, ctx.text)

        config = self._channels.get(ctx.channel)
        workdir = config.workdir if (config and config.workdir) else self._default_workdir
        rich = bool(config and config.rich)
        channel_mode = config.mode if config else "default"
        channel_slug = config.name if config else ctx.channel
        chat_level = config.chat if config else "normal"
        scope = config.session_scope if config else SessionScope.THREAD.value

        key = self._session_key(ctx, scope)
        decision = self._sessions.resolve(key, engine=self._engine.name)

        prompt = self._build_prompt(ctx, scope, decision)
        system_prompt = self._compose_system_prompt(
            ctx, principal, channel_mode, channel_slug, rich, chat_level
        )

        request = EngineRequest(
            prompt=prompt,
            system_prompt=system_prompt,
            session_id=decision.session_id,
            resume=decision.resume,
            model=model,
            effort=effort,
            workdir=workdir,
            trust_level=principal.trust,
        )
        response = self._invoker.invoke(request)

        if not response.ok and self._sessions.should_retry_with_new_session(
            response.failure_reason or ""
        ):
            decision = self._sessions.reset(key, engine=self._engine.name)
            prompt = self._build_prompt(ctx, scope, decision)
            request = replace(request, session_id=decision.session_id, resume=False, prompt=prompt)
            response = self._invoker.invoke(request)

        elapsed = self._now() - start
        # 성공·실패·침묵 어느 경로로 갈라지기 전에 한 번만 부른다. 분기마다
        # 넣으면 한 요청이 여러 번 보고된다. 실패한 요청이 오히려 더 느리다 —
        # 타임아웃으로 끝난 것이 가장 긴 소요다.
        self._report_if_slow(ctx, decision, model, effort, elapsed, self._monotonic() - mono_start, response, start)

        if not response.ok:
            failure = response.failure_reason or "엔진 실행 실패"
            self._record(ctx, decision, model, effort, elapsed, ok=False, usage=None, failure=failure)
            self._mark_failed(ctx)
            return HandleOutcome(ok=False, failure=failure)

        # 엔진이 자기 세션 ID 를 발급했으면 매핑에 반영한다. 성공한 뒤에만
        # 한다 — 실패한 실행이 낸 ID 를 다음 요청의 이어받기에 쓰면 그 요청도
        # 함께 깨진다. 원본 `persist_runner_session()` 호출 위치와 같다.
        if response.session_id and self._sessions.adopt_engine_session(
            key, decision.session_id, self._engine.name, response.session_id
        ):
            decision = replace(decision, session_id=response.session_id)

        self._sessions.touch(key, ctx.ts)

        if response.body.strip() == SILENT_MARK:
            self._record(ctx, decision, model, effort, elapsed, ok=True, usage=response.usage)
            self._mark_silent(ctx)
            return HandleOutcome(ok=True, silent=True)

        body, previous_body = self._absorb_late_addendum(ctx, scope, decision, request, response)
        body, watch_desc = self._apply_guards(body, ctx, principal, request, decision, previous_body)
        body = self._publisher.apply_elapsed_model_line(body, model, rich)
        posted_ts = self._publisher.post(ctx.channel, ctx.thread_ts, body, rich) or ""
        self._archive_response(ctx, channel_slug, body, response)

        self._record(ctx, decision, model, effort, elapsed, ok=True, usage=response.usage)
        if watch_desc and self._register_watch(ctx, principal, watch_desc):
            # 감시로 넘어간 건은 완료가 아니다. 완료 표식을 달면 미완료 복구
            # 대상에서 빠져 되짚기가 다시 보지 않는다.
            self._mark_watch(ctx)
        else:
            self._mark_done(ctx)
        return HandleOutcome(ok=True, posted_ts=posted_ts)

    def _archive_response(
        self, ctx: RequestContext, channel_slug: str, body: str, response: EngineResponse
    ) -> None:
        """올린 응답 본문을 그대로 남긴다. 학습 배치가 이 기록을 근거로 쓴다.

        예외를 밖으로 내지 않는다. 이미 발송까지 끝난 요청이고, 부가 기록이
        실패했다고 성공한 응답을 실패로 뒤집으면 안 된다.
        """
        if self._response_archive is None:
            return
        try:
            self._response_archive.record(
                channel_slug=channel_slug,
                user=ctx.user,
                thread_ts=ctx.thread_ts,
                question=ctx.text,
                body=body,
                ok=True,
                elapsed_sec=response.elapsed,
                turns=response.turns,
            )
        except Exception:
            log.exception("응답 기록에 실패했다")

    # -- 조립 도움 -------------------------------------------------------

    def _session_key(self, ctx: RequestContext, scope: str) -> SessionKey:
        key_value = ctx.channel if scope == SessionScope.CHANNEL else f"{ctx.channel}:{ctx.thread_ts}"
        return SessionKey(scope=scope, key=key_value)

    def _build_prompt(self, ctx: RequestContext, scope: str, decision: SessionDecision) -> str:
        if decision.resume:
            transcript = self._transcript.thread_transcript(
                ctx.channel, ctx.thread_ts, ctx.ts, scope=scope, after_ts=decision.after_ts,
            )
        else:
            transcript = self._transcript.thread_transcript(
                ctx.channel, ctx.thread_ts, ctx.ts, scope=scope,
            )
        return self._transcript.with_history(transcript, ctx.text)

    def _compose_system_prompt(
        self,
        ctx: RequestContext,
        principal: Principal,
        channel_mode: str,
        channel_slug: str,
        rich: bool,
        chat_level: str,
    ) -> str:
        is_owner = principal.trust is TrustLevel.OWNER
        asker_name = "" if is_owner else (self._name_resolver(ctx.user) or ctx.user)
        composition_ctx = CompositionContext(
            principal=principal,
            prompt=ctx.text,
            channel_mode=channel_mode,
            channel_slug=channel_slug,
            is_rich=rich,
            asker_name=asker_name,
            asker_id="" if is_owner else ctx.user,
            unaddressed=ctx.unaddressed,
            chat_level=chat_level,
            people=self._present_people(ctx),
        )
        return self._composer.compose(composition_ctx)

    def _present_people(self, ctx: RequestContext) -> tuple[tuple[str, str], ...]:
        """스레드에 함께 있는 사람. 못 세면 빈 목록이다.

        여기서 실패해도 요청은 그대로 처리한다 — 이 값이 없으면 프롬프트에
        그 대목이 안 붙을 뿐이고, 조회 실패로 답변 자체를 막을 이유가 없다.
        """
        if self._participants is None:
            return ()
        try:
            return self._participants(ctx.channel, ctx.thread_ts)
        except Exception as exc:  # noqa: BLE001 — 참가자 조회 실패로 답변 자체를 막지 않는다 — 프롬프트에 그 대목만 빠진다
            log.warning("함께 있는 사람을 세지 못했다 : %s", exc)
            return ()

    def _absorb_late_addendum(
        self,
        ctx: RequestContext,
        scope: str,
        decision: SessionDecision,
        request: EngineRequest,
        response: EngineResponse,
    ) -> tuple[str, str | None]:
        """발송 직전에 스레드 아래로 새로 달린 말을 담아 다시 낸다.

        (올릴 본문, 앞 답) 을 돌려준다. 다시 내지 않았으면 앞 답은 None 이다 —
        그 값이 있을 때만 유실 판정 가드가 실행된다.

        다시 내기가 실패했으면 앞 답을 그대로 올리고 소화 기록도 안 남긴다.
        그 말은 아직 답을 못 받은 것이라 대기줄이 처리해야 한다.
        """
        body = response.body
        if self._late_addendum is None:
            return body, None
        try:
            addendum, addendum_ts = self._late_addendum.check(
                ctx.channel, ctx.thread_ts, ctx.ts, scope
            )
        except Exception as exc:  # noqa: BLE001 — 발송 전 재확인 실패로 이미 만든 답을 버리지 않는다 — 그대로 올리고 대기줄에 맡긴다
            log.warning("발송 전 스레드 재확인 실패 : %s", exc)
            return body, None
        if not addendum:
            return body, None

        rerun = self._invoker.invoke(replace(
            request,
            prompt=late_addendum_prompt(addendum),
            resume=True,
            session_id=decision.session_id,
        ))
        new_body = (rerun.body or "").strip()
        if not rerun.ok or not new_body or new_body == SILENT_MARK:
            return body, None

        if self._consumption is not None:
            self._consumption.mark(ctx.thread_ts, addendum_ts)
        return rerun.body, body

    def _apply_guards(
        self,
        body: str,
        ctx: RequestContext,
        principal: Principal,
        request: EngineRequest,
        decision: SessionDecision,
        previous_body: str | None = None,
    ) -> tuple[str, str]:
        """가드를 적용한다. 보정한 본문과 감시 대상 설명을 함께 돌려준다.

        감시 대상 설명은 `WatchPromiseGuard` 가 `[[WATCH: ...]]` 태그에서 뽑은
        값이다. 없으면 빈 문자열이다. 이 값을 버리면 가드가 태그를 지우기만
        하고 등록은 아무도 안 해, 지켜보겠다는 답만 나가고 실제 확인은 없다.

        rerun 요청은 한 번까지만 엔진 재호출로 잇는다. 두 번째 가드 실행은
        ``is_rewrite_retry=True`` 로 실행된다 — 그 결과의 ``rerun`` 은 보지 않는다.
        여기서 그 값을 무시하는 것 자체가 무한 재시도를 막는 구조다.
        """
        is_owner = principal.trust is TrustLevel.OWNER
        guard_ctx = GuardContext(
            channel=ctx.channel,
            thread_ts=ctx.thread_ts,
            asker_id=ctx.user,
            is_owner=is_owner,
            owner_user_id=self._owner_user_id,
            mention_names=self._mention_table(),
            previous_body=previous_body,
        )
        result = self._guards.run(body, guard_ctx)
        if result.rerun is None:
            return result.body, _watch_desc_of(result)

        rerun_request = replace(
            request, prompt=result.rerun.rewrite_prompt, resume=True, session_id=decision.session_id,
        )
        rerun_response = self._invoker.invoke(rerun_request)
        if not rerun_response.ok:
            # 다시 쓰기 자체가 실패했다. 앞서 가드를 거친 본문을 그대로 쓴다.
            return result.body, _watch_desc_of(result)

        retry_ctx = replace(guard_ctx, previous_body=result.body, is_rewrite_retry=True)
        final_result = self._guards.run(rerun_response.body, retry_ctx)
        # 다시 쓴 답에 태그가 붙었으면 그것도 등록 대상이다. 원본도 재작성
        # 결과에서 태그를 다시 찾아 등록한다.
        return final_result.body, _watch_desc_of(final_result)

    def _register_watch(self, ctx: RequestContext, principal: Principal, description: str) -> bool:
        """감시 큐에 등록한다. 실제로 등록했으면 True.

        예외를 밖으로 내지 않는다. 답은 이미 나간 뒤라, 등록 실패로 요청
        전체를 실패로 적으면 워커가 재시도해 같은 답이 두 번 올라간다.
        """
        if self._watch_queue is None:
            return False
        try:
            self._watch_queue.enqueue(
                ctx.channel, ctx.thread_ts, description,
                msg_ts=ctx.ts, trust=principal.trust,
            )
        except Exception as exc:  # noqa: BLE001 — 감시 등록 실패를 요청 실패로 적지 않는다 — 답은 이미 나갔고 재시도하면 같은 답이 중복 발송된다
            log.warning("감시 등록 실패 : %s", exc)
            return False
        log.info("감시 등록 : %s", description[:80])
        return True

    # -- 감사 -------------------------------------------------------

    def _record(
        self,
        ctx: RequestContext,
        decision: SessionDecision,
        model: str,
        effort: str,
        elapsed: float,
        *,
        ok: bool,
        usage: Usage | None,
        failure: str = "",
    ) -> None:
        extra: dict[str, Any] = {}
        if failure:
            extra["failure"] = failure
        first_reaction_sec = self._first_reaction_sec(ctx)
        queue_wait_sec = self._queue_wait_sec(ctx)
        self._audit.record_request(
            channel=ctx.channel,
            thread_ts=ctx.thread_ts,
            message_ts=ctx.ts,
            session_id=decision.session_id,
            resumed=decision.resume,
            model=model,
            effort=effort,
            elapsed=elapsed,
            ok=ok,
            first_reaction_sec=first_reaction_sec,
            queue_wait_sec=queue_wait_sec,
            usage=asdict(usage) if usage is not None else None,
            **extra,
        )

    def _record_best_effort(self, ctx: RequestContext, failure: str) -> None:
        """예외 경로에서 남기는 최소 감사 기록. 이것마저 실패해도 삼킨다 —
        감사 기록 실패가 원래 처리 실패를 덮어써서는 안 된다."""
        try:
            self._audit.record_request(
                channel=ctx.channel,
                thread_ts=ctx.thread_ts,
                message_ts=ctx.ts,
                session_id="",
                resumed=False,
                model="",
                effort="",
                elapsed=0.0,
                ok=False,
                failure=failure,
            )
        except Exception as exc:  # noqa: BLE001 — 감사 기록 실패가 원래 처리 실패를 덮어쓰면 안 된다
            log.warning("최소 감사 기록 실패 : %s", exc)

    @staticmethod
    def _first_reaction_sec(ctx: RequestContext) -> float | None:
        if ctx.first_reaction_at is None:
            return None
        try:
            return ctx.first_reaction_at - float(ctx.ts)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _queue_wait_sec(ctx: RequestContext) -> float | None:
        if not ctx.requeued or ctx.queued_at is None:
            return None
        try:
            return time.time() - ctx.queued_at
        except TypeError:
            return None

    # -- 표식 -------------------------------------------------------

    def _mark_processing(self, ctx: RequestContext) -> None:
        if self._reactions is not None:
            self._reactions.mark_processing(ctx.channel, ctx.ts)

    def _mark_done(self, ctx: RequestContext) -> None:
        if self._reactions is not None:
            self._reactions.mark_done(ctx.channel, ctx.ts)

    def _mark_failed(self, ctx: RequestContext) -> None:
        if self._reactions is not None:
            self._reactions.mark_failed(ctx.channel, ctx.ts)

    def _mark_silent(self, ctx: RequestContext) -> None:
        if self._reactions is not None:
            self._reactions.mark_silent(ctx.channel, ctx.ts)

    def _mark_watch(self, ctx: RequestContext) -> None:
        if self._reactions is not None:
            self._reactions.mark_watch(ctx.channel, ctx.ts)


def _watch_desc_of(result: Any) -> str:
    """가드 실행 결과에서 감시 대상 설명을 꺼낸다. 없으면 빈 문자열."""
    detail = result.details.get(WatchPromiseGuard.name) or {}
    return str(detail.get("watch_desc") or "")
