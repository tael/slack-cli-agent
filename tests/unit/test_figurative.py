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
    MODE_ENV,
    OUT_ENV,
    FigurativeGuard,
    ModelFigurativeCorrector,
    Precompute,
    apply_fixes,
    body_key,
    figurative_accept,
    fix_ok,
    is_chat_channel,
    read_precomputed,
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
        corrector = FakeCorrector({"changed": True, "fixes": [{"before": "맞닿은", "after": "연결된"}]})
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
        corrector = FakeCorrector({"changed": True, "fixes": [{"before": "출근하네요", "after": "오네요"}]})
        result = _guard(corrector, chat=True).apply("졸음이 출근하네요", GuardContext(channel="C1"))
        assert result.body == "졸음이 오네요"
        assert corrector.calls[0][1] is False
        assert result.detail["mode"] == "chat"

    def test_멘션_보존_실패면_원문_유지(self) -> None:
        body = "<@U1> " + WORK_BODY
        corrector = FakeCorrector({"changed": True, "fixes": [{"before": "<@U1> 이번", "after": "이번"}]})
        result = _guard(corrector).apply(body, GuardContext(channel="C1"))
        assert not result.changed and result.body == body
        assert result.detail == {"rejected": True}

    def test_교정_실패면_원문_유지(self) -> None:
        result = _guard(FakeCorrector(None)).apply(WORK_BODY, GuardContext(channel="C1"))
        assert not result.changed and result.body == WORK_BODY

    def test_끝_이중대괄호_표식은_떼고_맡긴_뒤_다시_붙인다(self) -> None:
        body = WORK_BODY + "\n[[WATCH: 배치 완료]]"
        corrector = FakeCorrector({"changed": True, "fixes": [{"before": "맞닿은", "after": "연결된"}]})
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
        corrector = FakeCorrector({"changed": True, "fixes": [{"before": "<@U1> 이번", "after": "이번"}]})
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


class Test구절교정:
    """2026-11-12: the corrector names spans and code swaps them."""

    def test_구절만_바꾸고_나머지는_그대로(self) -> None:
        text, applied, dropped = apply_fixes("A 맞닿은 B. C 맞닿은 D.", [{"before": "맞닿은", "after": "연결된"}],
                                             work=True)
        assert text == "A 연결된 B. C 연결된 D." and dropped == 0 and len(applied) == 1

    def test_본문에_없는_구절은_버린다(self) -> None:
        text, applied, dropped = apply_fixes("원문", [{"before": "없는 말", "after": "다른 말"}], work=True)
        assert text == "원문" and applied == [] and dropped == 1

    def test_내용을_덧붙인_교정은_버린다(self) -> None:
        assert not fix_ok("젤리빈 몸", "길쭉하고 둥근 타원형 몸", work=True)
        assert fix_ok("붙어 있음", "연결되어 있음", work=True)

    def test_업무_채널은_삭제를_받지_않는다(self) -> None:
        assert not fix_ok("농담입니다", "", work=True)
        assert fix_ok("농담입니다", "", work=False)

    def test_한_글자_구절과_숫자_변경은_버린다(self) -> None:
        assert not fix_ok("칸", "열", work=True)
        assert not fix_ok("3건이 붙음", "4건이 연결됨", work=True)

    def test_일부만_버려도_나머지는_반영한다(self) -> None:
        body = "배치는 정산 파이프라인과 맞닿은 지점에서 실패했고 졸음이 출근했습니다. " * 3
        corrector = FakeCorrector({"changed": True, "fixes": [
            {"before": "맞닿은", "after": "연결된"},
            {"before": "졸음이", "after": "아주 길게 덧붙인 전혀 다른 설명이"},
        ]})
        result = _guard(corrector).apply(body, GuardContext(channel="C1"))
        assert "연결된" in result.body and "졸음이" in result.body
        assert result.detail["dropped"] == 1


class Test코드용어:
    def test_모델_없이_치환한다(self) -> None:
        corrector = FakeCorrector({"changed": False})
        result = _guard(corrector).apply("탈출구 1 : 형제 호출이 있으면 통과함", GuardContext(channel="C1"))
        assert result.body == "예외 1 : 같은 응답의 다른 호출이 있으면 통과함"
        assert corrector.calls == []
        assert result.detail["source"] == "code_terms"

    def test_교정기에는_치환한_본문을_넘긴다(self) -> None:
        corrector = FakeCorrector({"changed": False})
        _guard(corrector).apply(WORK_BODY + " 탈출구", GuardContext(channel="C1"))
        assert "예외" in corrector.calls[0][0] and "탈출구" not in corrector.calls[0][0]


class Test미리계산:
    def _pre(self, body: str, verdict: dict[str, Any], mode: str = "work") -> dict[str, Any]:
        return {"key": body_key(body), "mode": mode, "verdict": verdict}

    def test_같은_본문이면_Stop_훅_판정을_쓰고_교정기를_부르지_않는다(self) -> None:
        corrector = FakeCorrector({"changed": False})
        pre = self._pre(WORK_BODY, {"changed": True, "fixes": [{"before": "맞닿은", "after": "연결된"}]})
        result = _guard(corrector).apply(WORK_BODY, GuardContext(channel="C1", figurative_precomputed=pre))
        assert corrector.calls == [] and "연결된" in result.body
        assert result.detail["source"] == "stop_hook"

    def test_고칠것_없음_판정도_그대로_쓴다(self) -> None:
        corrector = FakeCorrector({"changed": True, "fixes": [{"before": "맞닿은", "after": "연결된"}]})
        pre = self._pre(WORK_BODY, {"changed": False})
        result = _guard(corrector).apply(WORK_BODY, GuardContext(channel="C1", figurative_precomputed=pre))
        assert corrector.calls == [] and not result.changed

    def test_본문이_다르면_직접_교정한다(self) -> None:
        corrector = FakeCorrector({"changed": False})
        pre = self._pre("전혀 다른 초안 " * 20, {"changed": False})
        _guard(corrector).apply(WORK_BODY, GuardContext(channel="C1", figurative_precomputed=pre))
        assert len(corrector.calls) == 1

    def test_모드가_다르면_직접_교정한다(self) -> None:
        corrector = FakeCorrector({"changed": False})
        pre = self._pre(WORK_BODY, {"changed": False}, mode="chat")
        _guard(corrector).apply(WORK_BODY, GuardContext(channel="C1", figurative_precomputed=pre))
        assert len(corrector.calls) == 1

    def test_멘션만_바뀐_본문은_같은_본문으로_본다(self) -> None:
        corrector = FakeCorrector({"changed": False})
        long_body = WORK_BODY * 4
        pre = self._pre(long_body + " 김태일", {"changed": False})
        _guard(corrector).apply(long_body + " <@U1>", GuardContext(channel="C1", figurative_precomputed=pre))
        assert corrector.calls == []


class TestStop훅계산:
    """2026-11-12: each Stop checks only the lines no earlier draft had checked."""

    def _env(self, tmp_path) -> dict[str, str]:
        return {OUT_ENV: str(tmp_path / "f.json"), MODE_ENV: "work"}

    def _inline(self, corrector):
        from slack_cli_agent.guard.figurative import run_worker
        seen: list[str] = []

        def spawn(job):
            seen.append(json.loads(job.read_text(encoding="utf-8"))["text"])
            run_worker(job, corrector=corrector)
        return spawn, seen

    def test_판정을_파일로_남기고_읽으면_폴더까지_지운다(self, tmp_path) -> None:
        spawn, _ = self._inline(FakeCorrector({"changed": False}))
        job = Precompute.start(WORK_BODY + "\n[[WATCH: x]]", self._env(tmp_path), spawn=spawn)
        assert job is not None
        record = job.finish(5)
        assert record is not None and record["key"] == body_key(WORK_BODY)
        assert read_precomputed(str(tmp_path / "f.json"))["verdict"]["changed"] is False
        assert not (tmp_path / "f.json").exists() and not (tmp_path / "f.json.d").exists()

    def test_재작성본은_새_줄만_교정기에_넘기고_앞_초안의_교정을_이어_쓴다(self, tmp_path) -> None:
        first = "첫 줄은 정산 파이프라인과 맞닿은 지점입니다 " * 2 + "\n둘째 줄은 그대로 둡니다 아주 길게 씁니다 " * 2
        spawn, seen = self._inline(FakeCorrector({"changed": True, "fixes": [{"before": "맞닿은", "after": "연결된"}]}))
        Precompute.start(first, self._env(tmp_path), spawn=spawn)
        final = first + "\n셋째 줄은 재작성에서 새로 생긴 문장입니다"
        spawn2, seen2 = self._inline(FakeCorrector({"changed": False}))
        job = Precompute.start(final, self._env(tmp_path), spawn=spawn2)
        record = job.finish(5)
        assert seen2 == ["셋째 줄은 재작성에서 새로 생긴 문장입니다"]
        assert record["verdict"]["fixes"] == [{"before": "맞닿은", "after": "연결된"}]

    def test_새_줄이_없으면_모델을_부르지_않는다(self, tmp_path) -> None:
        spawn, _ = self._inline(FakeCorrector({"changed": False}))
        Precompute.start(WORK_BODY, self._env(tmp_path), spawn=spawn)
        spawn2, seen2 = self._inline(FakeCorrector({"changed": False}))
        job = Precompute.start(WORK_BODY + " ", self._env(tmp_path), spawn=spawn2)
        assert seen2 == [] and not job.started
        assert job.finish(5)["verdict"] == {"changed": False, "fixes": []}

    def test_사라진_구절의_교정은_버린다(self, tmp_path) -> None:
        spawn, _ = self._inline(FakeCorrector({"changed": True, "fixes": [{"before": "맞닿은", "after": "연결된"}]}))
        Precompute.start(WORK_BODY, self._env(tmp_path), spawn=spawn)
        spawn2, _ = self._inline(FakeCorrector({"changed": False}))
        job = Precompute.start(WORK_BODY.replace("맞닿은", "이어진"), self._env(tmp_path), spawn=spawn2)
        assert job.finish(5)["verdict"]["fixes"] == []

    def test_짧은_업무_답과_설정_없음은_시작하지_않는다(self, tmp_path) -> None:
        spawn, _ = self._inline(FakeCorrector({}))
        assert Precompute.start("네", self._env(tmp_path), spawn=spawn) is None
        assert Precompute.start(WORK_BODY, {}, spawn=spawn) is None

    def test_교정기_실패면_파일을_남기지_않는다(self, tmp_path) -> None:
        spawn, _ = self._inline(FakeCorrector(None))
        job = Precompute.start(WORK_BODY, self._env(tmp_path), spawn=spawn)
        assert job is not None and job.finish(5) is None and not (tmp_path / "f.json").exists()

    def test_없는_파일은_None(self, tmp_path) -> None:
        assert read_precomputed(str(tmp_path / "none.json")) is None
        assert read_precomputed(None) is None
