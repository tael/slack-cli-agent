"""2차 엔진이 실제로 답하는지 본다 (sca-75x).

    ~/.<봇>/venv/bin/python tools/fallback-probe.py shinji

폴백 전환을 기다리지 않고 2차만 직접 부른다. 읽기만 한다 - 전환 상태도
세션도 건드리지 않는다.

판정 함정 - 새로 만든 엔진 홈의 첫 호출은 초기화 때문에 몇 분이 걸린다.
2026-09-18 에 shinji 의 codex 홈이 첫 회 180초를 넘겼고 두 번째는 6초였다.
timeout 을 짧게 잡고 실패로 읽지 않는다.
"""
import sys
from pathlib import Path

from slack_cli_agent.cli import resolver_for
from slack_cli_agent.config.profile import Profile
from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.engine.base import EngineRequest
from slack_cli_agent.engine.registry import default_registry
from slack_cli_agent.engine.runner import EngineRunner

PROFILE_DIR = Path(__file__).resolve().parent.parent / "profiles"

이름 = sys.argv[1]
프로필 = Profile.load(이름, [PROFILE_DIR])
resolver_for(프로필)
spec = 프로필.fallback_engine
assert spec is not None
설정 = RuntimeSettings()
엔진 = default_registry().create(spec.type, 프로필, 설정)
runner = EngineRunner(설정)
요청 = EngineRequest(
    prompt="1 더하기 1은? 숫자만 답해라.",
    system_prompt="너는 계산기다. 숫자만 답한다.",
    session_id=None, resume=False, model=None, effort="low",
    workdir=프로필.work_root,
)
응답 = runner.run(엔진, 요청, timeout_sec=600)
print(f"[{이름}/2차 {spec.type} {spec.model}] ok={응답.ok} "
      f"model={응답.model_actual} 본문={응답.body.strip()[:120]!r} "
      f"사유={응답.failure_reason}")
