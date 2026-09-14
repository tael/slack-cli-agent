"""답변 사후 점검(부검·디버그 추적·서식 점검)이 쓰는 EngineCaller 실물 구현.

`review.base.EngineCaller` 는 프롬프트와 세션 ID 만 받는 좁은 Protocol 이다.
실제로 모델을 부르려면 `EngineRequest` 를 조립해야 하는데, 그 값들은 사람
대화 요청과 다르다 — 점검은 봇 자신의 코드·지침을 읽어야 하므로 workdir 이
다르고, 소유자만 트리거하므로 trust_level 이 고정이고, 대충 보면 안 되므로
model/effort 가 소유자 하한 이상이다. 이 어댑터가 그 조립만 맡고 실행 자체는
`EngineRunner.run()` 에 그대로 위임한다.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from ..auth.policy import OWNER_EFFORT_MIN
from ..auth.principal import TrustLevel
from ..config.profile import Profile
from ..engine.base import Engine, EngineRequest, EngineResponse
from ..engine.runner import EngineRunner


class ReviewEngineCaller:
    """점검 전용 `EngineRequest` 조립 어댑터. `review.base.EngineCaller` 를 만족한다."""

    def __init__(
        self,
        *,
        engine: Engine,
        runner: EngineRunner,
        profile: Profile,
        workdir: Path,
        system_prompt: str,
        readable_dirs: Sequence[Path] = (),
        model: str | None = None,
        effort: str | None = None,
        allowed_tools: Sequence[str] = (),
    ) -> None:
        self._engine = engine
        self._runner = runner
        self._workdir = workdir
        self._system_prompt = system_prompt
        self._readable_dirs = tuple(readable_dirs)
        self._model = model or profile.primary_engine.model_for_owner()
        self._effort = effort or OWNER_EFFORT_MIN
        self._allowed_tools = tuple(allowed_tools)

    def run(self, prompt: str, session_id: str, resume: bool) -> EngineResponse:
        request = EngineRequest(
            prompt=prompt,
            system_prompt=self._system_prompt,
            session_id=session_id,
            resume=resume,
            model=self._model,
            effort=self._effort,
            workdir=self._workdir,
            readable_dirs=self._readable_dirs,
            allowed_tools=self._allowed_tools,
            trust_level=TrustLevel.OWNER,
        )
        return self._runner.run(self._engine, request)
