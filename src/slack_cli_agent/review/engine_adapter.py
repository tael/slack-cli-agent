"""답변 사후 점검(부검·디버그 추적·서식 점검)이 쓰는 EngineCaller 실물 구현.

`review.base.EngineCaller` 는 프롬프트와 세션 ID 만 받는 좁은 Protocol 이다.
실제로 모델을 부르려면 `EngineRequest` 를 조립해야 하는데, 그 값들은 사람
대화 요청과 다르다 — 점검은 봇 자신의 코드·지침을 읽어야 하므로 workdir 이
다르고, 소유자만 트리거하므로 trust_level 이 고정이고, 대충 보면 안 되므로
model/effort 가 소유자 하한 이상이다. 이 어댑터가 그 조립만 맡고 실행 자체는
`EngineInvoker.invoke()` 에 그대로 위임한다.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from ..auth.policy import OWNER_EFFORT_MIN
from ..auth.principal import TrustLevel
from ..config.profile import Profile
from ..engine.base import EngineRequest, EngineResponse
from ..engine.runner import EngineInvoker


class ReviewEngineCaller:
    """점검 전용 `EngineRequest` 조립 어댑터. `review.base.EngineCaller` 를 만족한다.

    실행은 `EngineInvoker` 에 맡긴다. 실행기와 엔진을 직접 받아
    `EngineRunner.run()` 을 부르면, 폴백이 설정돼 있어도 `FallbackEngine.run()`
    의 한도 감지와 전환 상태 기록이 건너뛰어진다. 그러면 소유자가 부검·디버그
    추적·서식 점검 리액션을 달았을 때 1차 엔진이 한도에 걸려도 전환 없이 계속
    1차 엔진만 불린다.
    """

    def __init__(
        self,
        *,
        invoker: EngineInvoker,
        profile: Profile,
        workdir: Path,
        system_prompt: str,
        readable_dirs: Sequence[Path] = (),
        model: str | None = None,
        effort: str | None = None,
        allowed_tools: Sequence[str] = (),
    ) -> None:
        self._invoker = invoker
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
        return self._invoker.invoke(request)
