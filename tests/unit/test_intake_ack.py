"""N4: 경과 시간 관찰로 계획의 ack 를 스레드에 한 번 게시한다."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_pipeline import build_pipeline, make_ctx, ok_response

from slack_cli_agent.config.channel import ChannelConfig
from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.core.intake_ack import IntakeAck, ack_record_path

ACK = "오늘 서울 기준으로 날씨를 확인해볼게요"


def _plan(**over: Any) -> dict[str, Any]:
    return {"status": "ok", "ack": ACK, "started_at": time.time(), "finished_at": time.time(), **over}


def _write(path: Path, plan: dict[str, Any]) -> None:
    path.write_text(json.dumps(plan), encoding="utf-8")


class Poster:
    def __init__(self, *, delay: float = 0.0, fail: bool = False) -> None:
        self.texts: list[str] = []
        self.finished_at: list[float] = []
        self._delay = delay
        self._fail = fail

    def __call__(self, text: str) -> None:
        if self._fail:
            raise RuntimeError("slack down")
        time.sleep(self._delay)
        self.texts.append(text)
        self.finished_at.append(time.time())


def _ack(plan_file: Path, poster: Poster, *, after: float = 0.05, resolve: Any = None) -> IntakeAck:
    return IntakeAck(
        resolve_plan=resolve or (lambda: plan_file), post=poster, after_sec=after,
        start=time.time(), poll_sec=0.01,
    )


def _hold(seconds: float) -> None:
    time.sleep(seconds)


class TestIntakeAck:
    def test_T초_뒤에도_답이_안_끝나면_한_번_게시한다(self, tmp_path: Path) -> None:
        plan_file, poster = tmp_path / "t.plan.json", Poster()
        _write(plan_file, _plan())
        with _ack(plan_file, poster) as ack:
            _hold(0.3)
        assert poster.texts == [ACK]
        assert ack.outcome.posted and ack.outcome.reason == ""
        assert ack.outcome.latency_sec is not None and ack.outcome.latency_sec >= 0.05

    def test_T초_전에_답이_끝나면_게시하지_않고_사유를_남긴다(self, tmp_path: Path) -> None:
        plan_file, poster = tmp_path / "t.plan.json", Poster()
        _write(plan_file, _plan())
        with _ack(plan_file, poster, after=5.0) as ack:
            _hold(0.05)
        assert poster.texts == [] and not ack.outcome.posted
        assert ack.outcome.reason == "answer_before_threshold"

    @pytest.mark.parametrize(("plan", "reason"), [
        (None, "no_plan_file"),
        (lambda: {"status": "pending", "started_at": time.time()}, "plan_pending"),
        (lambda: {"status": "error:cli", "started_at": time.time()}, "plan_not_ok:error:cli"),
        (lambda: _plan(ack=""), "no_ack"),
        (lambda: _plan(ack=3), "no_ack"),
        (lambda: _plan(started_at=1.0, finished_at=1.0), "stale"),
    ])
    def test_조건이_안_맞으면_게시하지_않는다(self, tmp_path: Path, plan: Any, reason: str) -> None:
        plan_file, poster = tmp_path / "t.plan.json", Poster()
        if plan is not None:
            _write(plan_file, plan())
        with _ack(plan_file, poster) as ack:
            _hold(0.2)
        assert poster.texts == [] and ack.outcome.reason == reason

    def test_계획이_T초_뒤에_완성돼도_게시한다(self, tmp_path: Path) -> None:
        plan_file, poster = tmp_path / "t.plan.json", Poster()
        _write(plan_file, {"status": "pending", "started_at": time.time()})
        with _ack(plan_file, poster):
            _hold(0.1)
            _write(plan_file, _plan())
            _hold(0.2)
        assert poster.texts == [ACK]

    def test_게시_중에_답이_끝나도_ack_는_답보다_먼저_끝난다(self, tmp_path: Path) -> None:
        plan_file, poster = tmp_path / "t.plan.json", Poster(delay=0.2)
        _write(plan_file, _plan())
        with _ack(plan_file, poster):
            _hold(0.1)
        left = time.time()
        assert poster.texts == [ACK] and poster.finished_at[0] <= left

    def test_종료_뒤에는_계획이_완성돼도_게시하지_않는다(self, tmp_path: Path) -> None:
        plan_file, poster = tmp_path / "t.plan.json", Poster()
        with _ack(plan_file, poster, after=0.0):
            pass
        _write(plan_file, _plan())
        _hold(0.2)
        assert poster.texts == []

    def test_판정_중에_답이_끝나면_게시_직전_재확인으로_막는다(self, tmp_path: Path) -> None:
        plan_file, poster = tmp_path / "t.plan.json", Poster()
        _write(plan_file, _plan())
        entered = threading.Event()

        def slow_resolve() -> Path:
            entered.set()
            _hold(0.2)
            return plan_file

        with _ack(plan_file, poster, after=0.0, resolve=slow_resolve):
            entered.wait(2.0)
        _hold(0.4)
        assert poster.texts == []

    def test_게시_실패는_요청을_막지_않고_사유만_남긴다(self, tmp_path: Path) -> None:
        plan_file = tmp_path / "t.plan.json"
        _write(plan_file, _plan())
        with _ack(plan_file, Poster(fail=True)) as ack:
            _hold(0.2)
        assert not ack.outcome.posted and ack.outcome.reason == "post_failed:RuntimeError"

    def test_계획_파일이_바뀌어도_한_번만_게시한다(self, tmp_path: Path) -> None:
        first, second, poster = tmp_path / "a.plan.json", tmp_path / "b.plan.json", Poster()
        _write(first, _plan())
        _write(second, _plan(ack="다시 확인할게요"))
        current = [first]
        with _ack(first, poster, resolve=lambda: current[0]):
            _hold(0.2)
            current[0] = second
            _hold(0.2)
        assert poster.texts == [ACK]

    def test_재시도로_경로가_바뀌면_새_경로를_읽는다(self, tmp_path: Path) -> None:
        first, second, poster = tmp_path / "a.plan.json", tmp_path / "b.plan.json", Poster()
        _write(second, _plan(ack="새 세션 계획"))
        current = [first]
        with _ack(first, poster, resolve=lambda: current[0]):
            _hold(0.1)
            current[0] = second
            _hold(0.2)
        assert poster.texts == ["새 세션 계획"]

    def test_결과를_ack_json_에_남긴다(self, tmp_path: Path) -> None:
        plan_file = tmp_path / "t.plan.json"
        _write(plan_file, _plan())
        with _ack(plan_file, Poster()):
            _hold(0.2)
        record = json.loads(ack_record_path(plan_file).read_text(encoding="utf-8"))
        assert ack_record_path(plan_file).name == "t.plan.ack.json"
        assert record["posted"] is True and record["latency_sec"] >= 0.05
        assert set(record) == {"posted", "posted_at", "latency_sec", "reason_if_not"}

    def test_미게시도_사유를_ack_json_에_남긴다(self, tmp_path: Path) -> None:
        plan_file = tmp_path / "t.plan.json"
        _write(plan_file, _plan(ack=""))
        with _ack(plan_file, Poster(), after=0.0):
            _hold(0.05)
        record = json.loads(ack_record_path(plan_file).read_text(encoding="utf-8"))
        assert record["posted"] is False and record["reason_if_not"] == "no_ack"

    def test_본문의_예외도_스레드를_멈춘다(self, tmp_path: Path) -> None:
        plan_file = tmp_path / "t.plan.json"
        with pytest.raises(ValueError), _ack(plan_file, Poster()):
            raise ValueError("engine")
        _hold(0.1)
        assert not [t for t in threading.enumerate() if t.name == "intake-ack"]


class TestPipeline:
    def _pipeline(self, tmp_path: Path, *, on: bool, channel: ChannelConfig | None = None,
                  engine_delay: float = 0.3, plan: dict[str, Any] | None = None,
                  after: float = 0.05) -> Any:
        pipeline, deps = build_pipeline(
            responses=[ok_response()], tmp_path=tmp_path,
            channels={"C1": channel} if channel else None,
        )
        plan_file = tmp_path / "s.plan.json"
        if plan is not None:
            _write(plan_file, plan)
        calls: list[tuple[str, str, Path]] = []

        def locate(engine: str, session: str, workdir: Path) -> Path:
            calls.append((engine, session, workdir))
            return plan_file

        pipeline._plan_gate_responses = 3
        pipeline._plan_intake = True
        pipeline._intake_ack = on
        pipeline._intake_ack_after_sec = after
        pipeline._intake_ack_poll_sec = 0.01
        pipeline._plan_file = locate
        runner = deps["runner"]
        original = runner.run
        order: list[str] = []

        def slow(engine: Any, request: Any) -> Any:
            _hold(engine_delay)
            order.append("engine_done")
            return original(engine, request)

        runner.run = slow
        pub = deps["publisher"]
        post = pub.post

        def traced(channel: str, thread_ts: str, text: str, rich: bool, *, flat: bool = False) -> Any:
            order.append(text)
            return post(channel, thread_ts, text, rich, flat=flat)

        pub.post = traced
        return pipeline, deps, calls, order

    def test_켜면_답보다_먼저_같은_스레드에_게시하고_행에_남긴다(self, tmp_path: Path) -> None:
        pipeline, deps, calls, order = self._pipeline(tmp_path, on=True, plan=_plan())
        pipeline.handle(make_ctx(user="UOWNER"))
        texts = [p["text"] for p in deps["publisher"].posted]
        assert texts[0] == ACK and "답변입니다" in texts[-1] and len(texts) == 2
        assert order.index(ACK) < order.index("engine_done")
        assert deps["publisher"].posted[0]["thread_ts"] == deps["publisher"].posted[-1]["thread_ts"]
        rec = deps["audit"].records[0]
        assert rec["ack_posted"] is True and rec["ack_latency_sec"] >= 0.05 and "ack_skip_reason" not in rec
        assert calls[0][2] == tmp_path.resolve()

    def test_꺼져_있으면_게시도_행_필드도_계획_조회도_없다(self, tmp_path: Path) -> None:
        pipeline, deps, calls, _ = self._pipeline(tmp_path, on=False, plan=_plan())
        pipeline.handle(make_ctx(user="UOWNER"))
        assert len(deps["publisher"].posted) == 1
        assert not [key for key in deps["audit"].records[0] if key.startswith("ack_")]
        assert calls == []

    def test_채널에서_끄면_게시하지_않는다(self, tmp_path: Path) -> None:
        pipeline, deps, calls, _ = self._pipeline(
            tmp_path, on=True, plan=_plan(), channel=ChannelConfig(channel_id="C1", intake_ack=False),
        )
        pipeline.handle(make_ctx(user="UOWNER"))
        assert len(deps["publisher"].posted) == 1 and calls == []

    def test_답이_먼저_끝나면_사유가_행에_남는다(self, tmp_path: Path) -> None:
        pipeline, deps, _, _ = self._pipeline(tmp_path, on=True, plan=_plan(), engine_delay=0.0, after=5.0)
        pipeline.handle(make_ctx(user="UOWNER"))
        rec = deps["audit"].records[0]
        assert len(deps["publisher"].posted) == 1
        assert rec["ack_posted"] is False and rec["ack_skip_reason"] == "answer_before_threshold"

    def test_계획이_없으면_게시하지_않고_사유를_남긴다(self, tmp_path: Path) -> None:
        pipeline, deps, _, _ = self._pipeline(tmp_path, on=True, plan=None)
        pipeline.handle(make_ctx(user="UOWNER"))
        assert len(deps["publisher"].posted) == 1
        assert deps["audit"].records[0]["ack_skip_reason"] == "no_plan_file"

    def test_다음_요청_행에_앞_요청의_ack_필드가_새지_않는다(self, tmp_path: Path) -> None:
        pipeline, deps, _, _ = self._pipeline(tmp_path, on=True, plan=_plan(), engine_delay=0.0, after=5.0)
        pipeline.handle(make_ctx(user="UOWNER"))
        pipeline._intake_ack = False
        deps["runner"]._responses.append(ok_response())
        pipeline.handle(make_ctx(user="UOWNER", ts="1700000002.000100", thread_ts="1700000002.000100"))
        first, second = deps["audit"].records
        assert "ack_posted" in first and not [key for key in second if key.startswith("ack_")]


def test_설정_기본값은_꺼짐이고_12초다() -> None:
    settings = RuntimeSettings()
    assert settings.intake_ack is False and settings.intake_ack_after_sec == 12
    assert ChannelConfig(channel_id="C1").intake_ack is True
    assert ChannelConfig.from_dict("C1", {"intake_ack": False}).intake_ack is False
    assert ChannelConfig.from_dict("C1", {}).intake_ack is True


class TestPlanFilePath:
    def _app(self, work_root: Path) -> Any:
        from slack_cli_agent.core.application import Application
        from slack_cli_agent.engine.transcript import TranscriptReaderRegistry

        fake = SimpleNamespace(
            _profile=SimpleNamespace(work_root=work_root), _transcript_readers=TranscriptReaderRegistry(),
            _transcript_reader_cache={}, _engine_spec_for=lambda engine: None,
        )
        fake.transcript_reader = lambda engine="": Application.transcript_reader(fake, engine)
        return Application, fake

    def test_실행_cwd_가_work_root_와_다르면_cwd_기준_경로다(self, tmp_path: Path) -> None:
        app, fake = self._app(tmp_path / "root")
        got = app._plan_file_path(fake, "claude", "sid", tmp_path / "chan")
        assert got is not None and got.name == "sid.jsonl.plan.json"
        assert str(got.parent).endswith("-chan") and "-root" not in str(got)

    def test_같거나_없으면_기존_경로와_같다(self, tmp_path: Path) -> None:
        app, fake = self._app(tmp_path / "root")
        base = app._plan_file_path(fake, "claude", "sid", None)
        assert base == app._plan_file_path(fake, "claude", "sid", tmp_path / "root")
        assert str(base.parent).endswith("-root")
