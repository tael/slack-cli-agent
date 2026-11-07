# The values needed to build an `EngineRequest` for a review differ from a
# normal chat request: workdir points at the bot's own code/docs, and
# model/effort are floored at the owner minimum since these reviews shouldn't
# be done half-heartedly. Trust follows whoever reacted (A18): the owner gets
# the owner turn as before, anyone else gets the turn `scoped` builds.

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from ..auth.execution_policy import ExecutionPolicy
from ..auth.policy import OWNER_EFFORT_MIN
from ..auth.principal import Principal, TrustLevel
from ..config.profile import Profile
from ..engine.base import EngineRequest, EngineResponse
from ..engine.runner import EngineInvoker
from ..engine.tool_selection import ToolSelection


class ReviewEngineCaller:
    # Delegates execution to `EngineInvoker` rather than calling
    # `EngineRunner.run()` directly, so fallback-engine limit detection and
    # switchover still apply to review requests.

    def __init__(
        self,
        *,
        invoker: EngineInvoker,
        profile: Profile,
        workdir: Path,
        system_prompt: str | Callable[[str], str],
        readable_dirs: Sequence[Path] = (),
        model: str | Callable[[], str] | None = None,
        effort: str | None = None,
        tools: ToolSelection | None = None,
        scoped: Callable[[Principal, str], tuple[ToolSelection, str]] | None = None,
        owner_tools: Callable[[str], ToolSelection] | None = None,
        #: A callable gets the requester (None for the owner turn) and the
        #: tools the turn got, so a scoped non-owner turn records its own tier.
        turn_tier: str | Callable[[Principal | None, ToolSelection], str] = "",
    ) -> None:
        self._invoker = invoker
        self._workdir = workdir
        self._system_prompt = system_prompt
        self._readable_dirs = tuple(readable_dirs)
        # 소유자에게 큰 모델을 강제하지 않는다 (A13). 점검별 모델은 호출부가 정한다.
        # callable 이면 실행할 때마다 다시 고른다. 점검 객체는 캐시되므로
        # 생성 시점 값으로 두면 channels.json 수정이 재기동 전까지 반영되지 않는다.
        self._model_source = model
        self._default_model = profile.primary_engine.model
        self._effort = effort or OWNER_EFFORT_MIN
        self._tools = tools or ToolSelection.unrestricted()
        # A non-owner's tools and system prompt, given the requester and the
        # review prompt. None means this caller has no non-owner turn at all.
        self._scoped = scoped
        # The owner turn's tools, given the review prompt. None keeps `tools`.
        # Unrestricted names nothing to claude, and its dontAsk mode then
        # refuses every call that needs approval: Write, Bash and the Slack
        # lookups all came back denied on an owner's format review (B6).
        self._owner_tools = owner_tools
        self._execution_policy = ExecutionPolicy()
        # Recorded on the capability row: a review writes no request row, so
        # without it the audit cannot say what the turn ran as (A12 review).
        self._turn_tier = turn_tier

    @property
    def model(self) -> str:
        source = self._model_source
        chosen = source() if callable(source) else source
        return chosen or self._default_model

    @property
    def effort(self) -> str:
        return self._effort

    def run(
        self, prompt: str, session_id: str | None, resume: bool, progress_log: Path | None = None,
        request_id: str = "",
        requester: Principal | None = None,
    ) -> EngineResponse:
        if requester is None or requester.trust is TrustLevel.OWNER:
            tools = self._owner_tools(prompt) if self._owner_tools is not None else self._tools
            trust = TrustLevel.OWNER
            scoped_requester: Principal | None = None
            # A callable is composed per call, so an edit to the persona or the
            # prompt files reaches the next review without a restart, as it does
            # for a normal request. It gets the review prompt so topic knowledge
            # is chosen by the reviewed conversation's words.
            system_prompt = (
                self._system_prompt(prompt) if callable(self._system_prompt)
                else self._system_prompt
            )
        else:
            # Falling back to the owner turn here is what A18 removed: anyone a
            # channel trusts ran the owner's tools by adding an emoji.
            if self._scoped is None:
                raise RuntimeError("비소유자 점검 범위가 연결되지 않아 점검을 돌리지 않는다")
            tools, system_prompt = self._scoped(requester, prompt)
            trust = requester.trust
            scoped_requester = requester
        request = EngineRequest(
            prompt=prompt,
            system_prompt=system_prompt,
            session_id=session_id,
            resume=resume,
            model=self.model,
            effort=self._effort,
            workdir=self._workdir,
            readable_dirs=self._readable_dirs,
            tools=tools,
            requirements=self._execution_policy.requirements_for(
                config=None, tools=tools,
            ),
            trust_level=trust,
            progress_log=progress_log,
            # One review calls the engine twice (main and split retry). Same
            # key so the audit reads them as one review (sca-4ol).
            request_id=request_id,
            turn_tier=(
                self._turn_tier(scoped_requester, tools) if callable(self._turn_tier)
                else self._turn_tier
            ),
        )
        return self._invoker.invoke(request)
