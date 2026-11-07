# The values needed to build an `EngineRequest` for a review differ from a
# normal chat request: workdir points at the bot's own code/docs, trust
# level is fixed to owner-only, and model/effort are floored at the owner
# minimum since these reviews shouldn't be done half-heartedly.

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from ..auth.execution_policy import ExecutionPolicy
from ..auth.policy import OWNER_EFFORT_MIN
from ..auth.principal import TrustLevel
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
        model: str | None = None,
        effort: str | None = None,
        tools: ToolSelection | None = None,
    ) -> None:
        self._invoker = invoker
        self._workdir = workdir
        self._system_prompt = system_prompt
        self._readable_dirs = tuple(readable_dirs)
        # 소유자에게 큰 모델을 강제하지 않는다 (A13). 점검별 모델은 호출부가 정한다.
        self._model = model or profile.primary_engine.model
        self._effort = effort or OWNER_EFFORT_MIN
        self._tools = tools or ToolSelection.unrestricted()
        self._execution_policy = ExecutionPolicy()

    @property
    def model(self) -> str:
        return self._model

    @property
    def effort(self) -> str:
        return self._effort

    def run(
        self, prompt: str, session_id: str | None, resume: bool, progress_log: Path | None = None,
        request_id: str = "",
    ) -> EngineResponse:
        # A callable is composed per call, so an edit to the persona or the
        # prompt files reaches the next review without a restart, as it does
        # for a normal request. It gets the review prompt so topic knowledge
        # is chosen by the reviewed conversation's words.
        system_prompt = (
            self._system_prompt(prompt) if callable(self._system_prompt)
            else self._system_prompt
        )
        request = EngineRequest(
            prompt=prompt,
            system_prompt=system_prompt,
            session_id=session_id,
            resume=resume,
            model=self._model,
            effort=self._effort,
            workdir=self._workdir,
            readable_dirs=self._readable_dirs,
            tools=self._tools,
            requirements=self._execution_policy.requirements_for(
                config=None, tools=self._tools,
            ),
            trust_level=TrustLevel.OWNER,
            progress_log=progress_log,
            # One review calls the engine twice (main and split retry). Same
            # key so the audit reads them as one review (sca-4ol).
            request_id=request_id,
        )
        return self._invoker.invoke(request)
