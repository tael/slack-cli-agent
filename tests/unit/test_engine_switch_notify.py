"""엔진 전환·복귀 시 소유자 DM 알림 (B24·B25·B26).

마메치 bot.py 의 engine_switch_notice / engine_recovered 와 같은 동작을 리포
FallbackEngine 에 둔다. 전환이 시작되면(한도·로그인 만료) 소유자 DM 으로
사유·시각·확인결과가 담긴 안내가 자동 발송되고(push, 조회 없이), 기본 엔진이
복귀하면 되돌렸다는 DM 이 발송된다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from test_engine import (  # 기존 대역 재사용
    SETTINGS,
    EngineRunner,
    FakeCompleted,
    RecordingEngine,
    codex_profile,
    named,
    request,
    통과정책,
)

from slack_cli_agent.engine.base import EngineResponse
from slack_cli_agent.engine.runner import (
    FallbackEngine,
    recovery_dm_body,
    switch_dm_body,
)
from slack_cli_agent.engine.switcher import EngineSwitcher
from slack_cli_agent.session.manager import AUTH_FAILURE_REASON, USAGE_LIMIT_REASON


def _resp(**over: Any) -> EngineResponse:
    base: dict[str, Any] = {
        "ok": True, "body": "", "session_id": None, "model_actual": None,
        "elapsed": 0.0, "turns": None, "usage": None,
    }
    base.update(over)
    return EngineResponse(**base)


# -- B24: DM 본문 생성 (순수 함수)


class TestSwitchDmBody:
    def test_한도_전환은_사유_시각_확인결과를_담는다(self) -> None:
        body = switch_dm_body(
            bot_name="구치파치", from_engine="codex", to_engine="claude",
            reason=USAGE_LIMIT_REASON, detail="weekly limit · resets 3am",
            probe_ok=True, probe_detail="OK", switched_at=1_700_000_000.0,
        )
        assert "구치파치 실행기를 claude 로 바꿨습니다" in body
        assert "구독 한도 소진" in body  # 사유
        assert "KST" in body            # 시각
        assert "weekly limit · resets 3am" in body  # 원문
        assert "전환 확인 : claude 가 응답합니다" in body  # 확인결과
        assert "엔진 승인" in body and "엔진 거부" in body

    def test_로그인_만료_전환은_다른_사유를_쓴다(self) -> None:
        body = switch_dm_body(
            bot_name="구치파치", from_engine="codex", to_engine="claude",
            reason=AUTH_FAILURE_REASON, detail="", probe_ok=True,
            probe_detail="OK", switched_at=1_700_000_000.0,
        )
        assert "로그인 만료" in body
        assert "구독 한도 소진" not in body

    def test_확인_실패면_대체도_못_쓴다고_알린다(self) -> None:
        body = switch_dm_body(
            bot_name="구치파치", from_engine="codex", to_engine="claude",
            reason=USAGE_LIMIT_REASON, detail="limit", probe_ok=False,
            probe_detail="연결 거부", switched_at=1_700_000_000.0,
        )
        assert "전환 확인 실패 : 연결 거부" in body
        assert "엔진 승인" not in body

    def test_복귀_DM_은_되돌렸다고_알린다(self) -> None:
        body = recovery_dm_body(
            bot_name="구치파치", primary_engine="codex", recovered_at=1_700_000_000.0,
        )
        assert "구치파치 실행기를 codex 로 되돌렸습니다" in body
        assert "KST" in body


# -- B25·B26: FallbackEngine 배선


class _Notifier:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[str] = []
        self._fail = fail

    def __call__(self, text: str) -> bool:
        if self._fail:
            raise RuntimeError("DM 발송 실패")
        self.sent.append(text)
        return True


def _fallback(tmp_path: Path, *, primary_response=None, secondary_response=None,
              notify=None, clock=None):
    profile = codex_profile(tmp_path)
    primary = named(RecordingEngine, "codex")(profile, SETTINGS, response=primary_response)
    secondary = named(RecordingEngine, "claude")(profile, SETTINGS, response=secondary_response)
    switcher = EngineSwitcher(tmp_path / "engine_state.json")

    def fake_subprocess(cmd, cwd, timeout, env=None):
        return FakeCompleted(stdout="", returncode=0)

    runner = EngineRunner(SETTINGS, subprocess_runner=fake_subprocess, environment_policy=통과정책())
    kwargs: dict[str, Any] = {}
    if notify is not None:
        kwargs["notify"] = notify
    if clock is not None:
        kwargs["clock"] = clock
    engine = FallbackEngine(primary, secondary, switcher, runner, **kwargs)
    return engine, primary, secondary, switcher


class TestSwitchNotifyWiring:
    def _limit_primary(self):
        return _resp(ok=False, body="한도 소진", failure_reason=USAGE_LIMIT_REASON,
                     user_facing=True)

    def test_한도_전환_시작시_소유자에게_DM_이_자동_발송된다(self, tmp_path: Path) -> None:
        notifier = _Notifier()
        probe = _resp(ok=True, body="OK")
        fallback, _p, _s, switcher = _fallback(
            tmp_path, primary_response=self._limit_primary(),
            secondary_response=probe, notify=notifier)

        fallback.run(request())

        assert switcher.is_switched() is True
        assert len(notifier.sent) == 1
        assert "실행기를 claude 로 바꿨습니다" in notifier.sent[0]
        assert "전환 확인 : claude 가 응답합니다" in notifier.sent[0]

    def test_notify_없으면_전환은_되고_발송만_생략된다(self, tmp_path: Path) -> None:
        probe = _resp(ok=True, body="OK")
        fallback, _p, _s, switcher = _fallback(
            tmp_path, primary_response=self._limit_primary(), secondary_response=probe)
        fallback.run(request())
        assert switcher.is_switched() is True  # 알림 없이도 전환은 일어난다

    def test_DM_발송_예외는_전환을_막지_않는다(self, tmp_path: Path) -> None:
        notifier = _Notifier(fail=True)
        probe = _resp(ok=True, body="OK")
        fallback, _p, _s, switcher = _fallback(
            tmp_path, primary_response=self._limit_primary(),
            secondary_response=probe, notify=notifier)
        fallback.run(request())  # 예외가 새어나오면 안 된다
        assert switcher.is_switched() is True

    def test_복귀시_되돌림_DM_이_발송된다(self, tmp_path: Path) -> None:
        notifier = _Notifier()
        recovered = _resp(ok=True, body="다시 됩니다")
        fallback, _p, _s, switcher = _fallback(
            tmp_path, primary_response=recovered, notify=notifier)
        switcher.begin_switch("weekly limit", engine_name="claude")
        switcher.mark_probed(switcher.load()["switched_at"] - 700)

        resp = fallback.run(request())

        assert resp.body == "다시 됩니다"
        assert switcher.is_switched() is False
        assert len(notifier.sent) == 1
        assert "실행기를 codex 로 되돌렸습니다" in notifier.sent[0]
