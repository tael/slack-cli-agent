"""게시 전 비유 교정 가드 (B9, bot.py:7268-7357, 8162-8176 이관)."""

from __future__ import annotations

import json
import subprocess
from typing import Any

from slack_cli_agent.config.channel import ChannelConfig
from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.guard.base import GuardContext
from slack_cli_agent.guard.figurative import (
    FIGURATIVE_CHECK_SYSTEM,
    FIGURATIVE_CHECK_SYSTEM_WORK,
    FigurativeGuard,
    ModelFigurativeCorrector,
    figurative_accept,
    is_chat_channel,
)

ON = RuntimeSettings().override({"figurative_check": True})
WORK_BODY = "이번 배치는 정산 파이프라인과 맞닿은 지점에서 실패했습니다. " * 3


class FakeCorrector:
    def __init__(self, verdict: dict[str, Any] | None) -> None:
        self.verdict = verdict
        self.calls: list[tuple[str, bool]] = []

    def __call__(self, text: str, *, work: bool) -> dict[str, Any] | None:
        self.calls.append((text, work))
        return self.verdict


def _guard(corrector: FakeCorrector, *, chat: bool = False, settings: RuntimeSettings = ON) -> FigurativeGuard:
    return FigurativeGuard(settings, is_chat=lambda _c: chat, corrector=corrector)


class Test잡담채널판정:
    def test_chat_active_는_잡담(self) -> None:
        assert is_chat_channel(ChannelConfig(channel_id="C1", chat="active"))

    def test_mode_chat_은_잡담(self) -> None:
        assert is_chat_channel(ChannelConfig(channel_id="C1", mode="chat"))

    def test_기본_채널과_미등록은_업무(self) -> None:
        assert not is_chat_channel(ChannelConfig(channel_id="C1"))
        assert not is_chat_channel(None)


class Test수용검사:
    def test_멘션이_사라지면_거부(self) -> None:
        assert not figurative_accept("<@U1> 안녕하세요 반갑습니다", "안녕하세요 반갑습니다 여러분")

    def test_링크가_바뀌면_거부(self) -> None:
        assert not figurative_accept("자료 https://a.example/x 참고", "자료 https://a.example/y 참고")

    def test_길이가_절반_미만이면_거부(self) -> None:
        assert not figurative_accept("가" * 100, "가" * 40)

    def test_멘션_링크_유지하고_길이_비슷하면_수용(self) -> None:
        assert figurative_accept("<@U1> 이 코드는 시한폭탄이다 https://a.b",
                                 "<@U1> 이 코드는 장애 위험이 크다 https://a.b")


class Test가드:
    def test_꺼져_있으면_교정을_부르지_않는다(self) -> None:
        corrector = FakeCorrector({"changed": True, "text": "x"})
        result = _guard(corrector, settings=RuntimeSettings()).apply(WORK_BODY, GuardContext(channel="C1"))
        assert not result.changed and corrector.calls == []

    def test_업무_채널_비유가_표준어로_바뀐다(self) -> None:
        fixed = WORK_BODY.replace("맞닿은", "연결된")
        corrector = FakeCorrector({"changed": True, "text": fixed,
                                   "fixes": [{"before": "맞닿은", "after": "연결된"}]})
        result = _guard(corrector).apply(WORK_BODY, GuardContext(channel="C1"))
        assert result.changed and result.body == fixed
        assert corrector.calls[0][1] is True
        assert result.detail["mode"] == "work"
        assert result.detail["fixes"] == [{"before": "맞닿은", "after": "연결된"}]

    def test_업무_채널_짧은_답은_건너뛴다(self) -> None:
        corrector = FakeCorrector({"changed": True, "text": "x"})
        result = _guard(corrector).apply("네 확인했습니다", GuardContext(channel="C1"))
        assert not result.changed and corrector.calls == []

    def test_잡담_채널은_짧은_답도_잡담_모드로_고친다(self) -> None:
        corrector = FakeCorrector({"changed": True, "text": "졸음이 오네요"})
        result = _guard(corrector, chat=True).apply("졸음이 출근하네요", GuardContext(channel="C1"))
        assert result.body == "졸음이 오네요"
        assert corrector.calls[0][1] is False
        assert result.detail["mode"] == "chat"

    def test_멘션_보존_실패면_원문_유지(self) -> None:
        body = "<@U1> " + WORK_BODY
        corrector = FakeCorrector({"changed": True, "text": WORK_BODY.replace("맞닿은", "연결된")})
        result = _guard(corrector).apply(body, GuardContext(channel="C1"))
        assert not result.changed and result.body == body
        assert result.detail == {"rejected": True}

    def test_교정_실패면_원문_유지(self) -> None:
        result = _guard(FakeCorrector(None)).apply(WORK_BODY, GuardContext(channel="C1"))
        assert not result.changed and result.body == WORK_BODY

    def test_끝_이중대괄호_표식은_떼고_맡긴_뒤_다시_붙인다(self) -> None:
        body = WORK_BODY + "\n[[WATCH: 배치 완료]]"
        fixed = WORK_BODY.replace("맞닿은", "연결된")
        corrector = FakeCorrector({"changed": True, "text": fixed})
        result = _guard(corrector).apply(body, GuardContext(channel="C1"))
        assert "[[WATCH" not in corrector.calls[0][0]
        assert result.body.endswith("[[WATCH: 배치 완료]]")
        assert "연결된" in result.body

    def test_침묵_답은_건너뛴다(self) -> None:
        corrector = FakeCorrector({"changed": True, "text": "x"})
        result = _guard(corrector, chat=True).apply("[[침묵]]", GuardContext(channel="C1"))
        assert not result.changed and corrector.calls == []


class Test모델교정기:
    def _runner(self, stdout: str, seen: list[Any]):
        def run(cmd, **kwargs):
            seen.append((cmd, kwargs))
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
        return run

    def test_업무_모드는_업무_지침과_도구_없이_실행한다(self) -> None:
        seen: list[Any] = []
        out = json.dumps({"changed": True, "text": "고친 답"}, ensure_ascii=False)
        corrector = ModelFigurativeCorrector(
            runner=self._runner(out, seen), executable="claude",
            env={"ANTHROPIC_API_KEY": "k", "PATH": "/bin"})
        assert corrector("원문", work=True) == {"changed": True, "text": "고친 답"}
        cmd, kwargs = seen[0]
        assert cmd[cmd.index("--system-prompt") + 1] == FIGURATIVE_CHECK_SYSTEM_WORK
        assert cmd[cmd.index("--tools") + 1] == ""
        assert "<reply>\n원문\n</reply>" == kwargs["input"]
        assert "ANTHROPIC_API_KEY" not in kwargs["env"]

    def test_잡담_모드는_억지_위트_항목을_쓴다(self) -> None:
        seen: list[Any] = []
        ModelFigurativeCorrector(runner=self._runner("{}", seen), executable="claude")("x", work=False)
        assert seen[0][0][seen[0][0].index("--system-prompt") + 1] == FIGURATIVE_CHECK_SYSTEM
        assert "억지 위트" in FIGURATIVE_CHECK_SYSTEM
        assert "억지 위트" not in FIGURATIVE_CHECK_SYSTEM_WORK

    def test_JSON이_없거나_예외면_None(self) -> None:
        assert ModelFigurativeCorrector(runner=self._runner("설명뿐", []), executable="c")("x", work=True) is None

        def boom(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 1)
        assert ModelFigurativeCorrector(runner=boom, executable="c")("x", work=True) is None


class Test검토보완:
    def test_상한_초과_본문은_교정을_건너뛰어_뒷부분을_잃지_않는다(self, caplog) -> None:
        body = WORK_BODY * 120
        assert len(body) > 6000
        corrector = FakeCorrector({"changed": True, "text": body[:6000].replace("맞닿은", "연결된")})
        with caplog.at_level("INFO", logger="slack_cli_agent.guard.figurative"):
            result = _guard(corrector).apply(body, GuardContext(channel="C1"))
        assert not result.changed and result.body == body
        assert corrector.calls == []
        assert "too long" in caplog.text

    def test_모델_교정기는_입력을_자르지_않는다(self) -> None:
        seen: list[Any] = []

        def run(cmd, **kwargs):
            seen.append(kwargs)
            return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")
        ModelFigurativeCorrector(runner=run, executable="c")("가" * 7000, work=True)
        assert seen[0]["input"] == "<reply>\n" + "가" * 7000 + "\n</reply>"

    def test_거부되면_경고를_남긴다(self, caplog) -> None:
        body = "<@U1> " + WORK_BODY
        corrector = FakeCorrector({"changed": True, "text": WORK_BODY.replace("맞닿은", "연결된")})
        with caplog.at_level("WARNING", logger="slack_cli_agent.guard.figurative"):
            _guard(corrector).apply(body, GuardContext(channel="C1"))
        assert "rejected" in caplog.text

    def test_교정기가_실패하면_경고를_남긴다(self, caplog) -> None:
        with caplog.at_level("WARNING", logger="slack_cli_agent.guard.figurative"):
            result = _guard(FakeCorrector(None)).apply(WORK_BODY, GuardContext(channel="C1"))
        assert result.body == WORK_BODY
        assert "could not run" in caplog.text

    def test_숫자가_바뀌면_거부(self) -> None:
        assert not figurative_accept("실패 1,234건이 맞닿은 지점에서 났다", "실패 1,243건이 연결된 지점에서 났다")

    def test_코드블록_안이_바뀌면_거부(self) -> None:
        original = "쿼리가 맞닿은 부분\n```sql\nSELECT a FROM t\n```\n입니다"
        revised = "쿼리가 연결된 부분\n```sql\nSELECT b FROM t\n```\n입니다"
        assert not figurative_accept(original, revised)

    def test_본문_중간_이중대괄호_표식이_사라지면_거부(self) -> None:
        original = "확인 부탁드립니다\n[[ASK]]\n이 결과가 맞닿은 지점입니다"
        revised = "확인 부탁드립니다\n이 결과가 연결된 지점입니다 여기"
        assert not figurative_accept(original, revised)

    def test_숫자_코드_표식이_그대로면_수용(self) -> None:
        original = "1,234건이 맞닿은 부분\n```\nSELECT 1\n```\n[[ASK]]\n끝"
        revised = "1,234건이 연결된 부분\n```\nSELECT 1\n```\n[[ASK]]\n끝"
        assert figurative_accept(original, revised)
