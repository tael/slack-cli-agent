"""학습 배치 흐름 시험.

원본 learn.py 의 main() 을 옮긴 LearningBatch 를 시험한다. 엔진과 슬랙
API 실물을 부르지 않는다 — 분석은 시험용 분석기를 주입한 ProposalBuilder
로, 자료 조회는 ports.py 의 계약을 흉내 낸 페이크로 대신한다. 시계도
주입한 것만 쓴다.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from slack_cli_agent.learning.analyzer import (
    ChannelAnalysis,
    ChannelAnalysisResult,
    ProposalBuilder,
)
from slack_cli_agent.learning.apply import LearningApplier
from slack_cli_agent.learning.batch import LearningBatch
from slack_cli_agent.learning.ports import DayArchives
from slack_cli_agent.learning.progress import (
    MAX_ATTEMPTS,
    ChannelFailure,
    FailureKind,
    ProgressStore,
)
from slack_cli_agent.learning.proposal import ProposalStore
from slack_cli_agent.learning.render import ProposalRenderer

DAY = "2026-09-14"


class FakeArchives:
    """ResponseArchiveReader 를 흉내 낸다."""

    def __init__(self, data: Mapping[str, str] | None = None) -> None:
        self._data = {"공지": "오늘 응답 기록 전문"} if data is None else data
        self.unreadable: set[str] = set()

    def read_day(self, day: str) -> DayArchives:
        return DayArchives(texts=self._data, unreadable=frozenset(self.unreadable))


class FakeReactions:
    """ReactionSource 를 흉내 낸다."""

    def __init__(
        self,
        data: Mapping[str, Sequence[Mapping[str, object]]] | None = None,
        *,
        raise_error: bool = False,
    ) -> None:
        self._data = data or {}
        self._raise = raise_error

    def collect(self, texts: Mapping[str, str]) -> Mapping[str, Sequence[Mapping[str, object]]]:
        if self._raise:
            raise RuntimeError("슬랙 conversations.replies 호출 실패")
        return self._data


class FakeAnalyzer:
    """ProposalAnalyzer 를 흉내 낸다. analyze_channel() 만 쓰인다."""

    def __init__(
        self,
        result: ChannelAnalysisResult | None = None,
        *,
        fail_reason: str | None = None,
        fail_channels: set[str] | None = None,
        fail_kind: FailureKind = FailureKind.ENGINE_FAILED,
    ) -> None:
        self._result = result if result is not None else ChannelAnalysisResult(
            channel_facts=("9월 회의는 매주 화요일이다",),
        )
        self._fail_reason = fail_reason
        self.fail_channels = fail_channels if fail_channels is not None else set()
        self.fail_kind = fail_kind
        self.calls: list[str] = []

    def analyze_channel(
        self, day: str, channel_name: str, archive_text: str,
        reactions: Sequence[Mapping[str, object]],
    ) -> ChannelAnalysis:
        self.calls.append(channel_name)
        if self._fail_reason is not None or channel_name in self.fail_channels:
            return ChannelAnalysis.failed(ChannelFailure(
                channel=channel_name, kind=self.fail_kind,
                detail=self._fail_reason or "사유",
            ))
        return ChannelAnalysis.succeeded(self._result)


class Recorder:
    """notify 로 넘어온 문자열을 받아 둔다."""

    def __init__(self, *, fail: bool = False, result: bool = True) -> None:
        self.calls: list[str] = []
        self._fail = fail
        # 발송기가 예외를 내지 않고 실패를 값으로 알리는 경우를 만든다.
        self._result = result

    def __call__(self, text: str) -> bool:
        if self._fail:
            raise RuntimeError("슬랙 chat.postMessage 실패")
        self.calls.append(text)
        return self._result


def make_clock(moment: datetime) -> object:
    return lambda: moment


class 가변시계:
    """순회 사이에 시간을 넘긴다. 실패 재시도에는 간격이 있어서 같은 시각으로
    두 번 돌리면 두 번째가 아무것도 안 한다."""

    def __init__(self, moment: datetime) -> None:
        self.now = moment

    def __call__(self) -> datetime:
        return self.now

    def 다음_재시도까지(self) -> None:
        self.now += timedelta(hours=1)


def make_batch(
    tmp_path: Path,
    *,
    archives: FakeArchives | None = None,
    reactions: FakeReactions | None = None,
    analyzer_result: ChannelAnalysisResult | None = None,
    analyzer_fail: str | None = None,
    analyzer: FakeAnalyzer | None = None,
    notify: Recorder | None = None,
    clock: object | None = None,
    applier: object | None = None,
) -> tuple[LearningBatch, ProposalStore, Recorder]:
    store = ProposalStore(tmp_path / "proposals")
    applier = applier if applier is not None else LearningApplier(tmp_path / "knowledge", "테스트봇")
    renderer = ProposalRenderer()
    analyzer = analyzer if analyzer is not None else FakeAnalyzer(
        analyzer_result, fail_reason=analyzer_fail)
    builder = ProposalBuilder(analyzer)
    rec = notify if notify is not None else Recorder()
    fixed_clock = clock if clock is not None else make_clock(datetime(2026, 9, 14, 10, 0, tzinfo=UTC))
    batch = LearningBatch(
        archives=archives or FakeArchives(),
        reactions=reactions or FakeReactions(),
        builder=builder,
        store=store,
        progress=ProgressStore(tmp_path / "progress"),
        applier=applier,  # type: ignore[arg-type]  # 대역을 끼울 수 있게 둔다
        renderer=renderer,
        notify=rec,
        clock=fixed_clock,  # type: ignore[arg-type]
    )
    return batch, store, rec


class TestLearningBatch:
    def test_응답_기록이_없으면_ran_False(self, tmp_path: Path) -> None:
        batch, _store, rec = make_batch(tmp_path, archives=FakeArchives({}))
        report = batch.run(DAY)
        assert report.ran is False
        assert report.reason == "응답 기록이 없다"
        assert report.proposal is None
        assert rec.calls == []

    def test_정상_흐름은_저장하고_반영하고_알린다(self, tmp_path: Path) -> None:
        batch, store, rec = make_batch(tmp_path)
        report = batch.run(DAY)

        assert report.ran is True
        assert report.proposal is not None
        assert report.proposal.channel_knowledge == {"공지": ("9월 회의는 매주 화요일이다",)}
        assert report.applied == {"공지": 1}
        assert report.notified is True

        # 제안 파일이 저장됐다.
        outcome = store.latest()
        assert outcome.is_found
        assert outcome.value().day == DAY

        # 지식 파일에 반영됐다.
        knowledge = (tmp_path / "knowledge" / "공지.md").read_text(encoding="utf-8")
        assert "9월 회의는 매주 화요일이다" in knowledge

        # applied 기록이 남았다 (mark_applied 호출 확인).
        applied_payload = json.loads((tmp_path / "proposals" / f"{DAY}.applied").read_text())
        assert applied_payload["done"] == {"공지": 1}

        # 알림 문구가 원본 형식과 같다.
        assert len(rec.calls) == 1
        text = rec.calls[0]
        assert text.startswith(f"*{DAY} 학습*\n\n")
        assert "지식에 반영했습니다. 공지 1건" in text
        assert f"학습 되돌리기 {DAY}" in text

    def test_반응_조회가_실패해도_배치는_계속한다(self, tmp_path: Path) -> None:
        batch, _store, rec = make_batch(tmp_path, reactions=FakeReactions(raise_error=True))
        report = batch.run(DAY)
        assert report.ran is True
        assert report.proposal is not None
        assert report.proposal.has_content is True
        assert rec.calls  # 알림까지 정상적으로 갔다

    def test_반영할_내용이_없으면_머리말과_꼬리말이_없다(self, tmp_path: Path) -> None:
        empty_result = ChannelAnalysisResult(note="특별히 배울 게 없었다")
        batch, _store, rec = make_batch(tmp_path, analyzer_result=empty_result)
        report = batch.run(DAY)

        assert report.ran is True
        assert report.proposal is not None
        assert report.proposal.has_content is False
        assert report.applied == {}
        text = rec.calls[0]
        assert not text.startswith(f"*{DAY} 학습*")
        assert "지식에 반영했습니다" not in text
        assert "학습 되돌리기" not in text

    def test_분석_실패도_ran_True다_응답_기록은_있었다(self, tmp_path: Path) -> None:
        batch, _store, rec = make_batch(tmp_path, analyzer_fail="분석 실행에 실패했다")
        report = batch.run(DAY)
        assert report.ran is True
        assert report.proposal is not None
        assert report.proposal.has_content is False
        assert report.applied == {}
        assert rec.calls  # 실패해도 "배울 게 없다" 로 알림은 간다

    def test_알림_발송이_실패해도_반영은_ran_True로_남는다(self, tmp_path: Path) -> None:
        rec = Recorder(fail=True)
        batch, store, _ = make_batch(tmp_path, notify=rec)
        report = batch.run(DAY)  # 예외가 밖으로 안 나온다

        assert report.ran is True
        assert report.notified is False
        # 알림이 실패해도 반영은 이미 끝나 있다.
        assert report.applied == {"공지": 1}
        outcome = store.latest()
        assert outcome.is_found

    def test_day를_생략하면_clock에서_만든다(self, tmp_path: Path) -> None:
        moment = datetime(2026, 9, 1, 3, 0, tzinfo=UTC)
        batch, store, _rec = make_batch(
            tmp_path, archives=FakeArchives({"공지": "글"}), clock=make_clock(moment),
        )
        report = batch.run()
        assert report.day == "2026-09-01"
        assert store.latest().value().day == "2026-09-01"

    def test_같은_날짜가_이미_잠겨_있으면_건너뛴다(self, tmp_path: Path) -> None:
        batch, store, rec = make_batch(tmp_path)
        now = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)
        # 다른 워커가 그날 배치를 이미 돌리고 있다고 가정한다.
        assert store.acquire_lock(DAY, now=now) is True

        report = batch.run(DAY)

        assert report.ran is False
        assert report.proposal is None
        assert rec.calls == []  # 알림도, 분석도 전혀 안 돌았다
        # 잠금을 쥔 쪽이 안 풀었으니 배치가 그걸 풀어 버리면 안 된다.
        assert store.acquire_lock(DAY, now=now) is False

    def test_잠금을_풀면_다시_돌_수_있다(self, tmp_path: Path) -> None:
        batch, store, rec = make_batch(tmp_path)
        now = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)
        store.acquire_lock(DAY, now=now)
        store.release_lock(DAY)

        report = batch.run(DAY)
        assert report.ran is True
        assert rec.calls


class TestProposalStoreLock:
    """배치의 중복 방지가 기대는 ProposalStore 잠금 메서드 자체를 시험한다."""

    def test_두번째_잠금_시도는_실패한다(self, tmp_path: Path) -> None:
        store = ProposalStore(tmp_path)
        now = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)
        assert store.acquire_lock(DAY, now=now) is True
        assert store.acquire_lock(DAY, now=now) is False

    def test_release_후_다시_잡을_수_있다(self, tmp_path: Path) -> None:
        store = ProposalStore(tmp_path)
        now = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)
        assert store.acquire_lock(DAY, now=now) is True
        store.release_lock(DAY)
        assert store.acquire_lock(DAY, now=now) is True

    def test_release는_없는_잠금에도_예외를_안_낸다(self, tmp_path: Path) -> None:
        store = ProposalStore(tmp_path)
        store.release_lock("2026-01-01")  # 예외 없이 넘어간다

    def test_오래된_잠금은_죽은_프로세스로_보고_다시_가져온다(self, tmp_path: Path) -> None:
        store = ProposalStore(tmp_path)
        old_now = datetime(2026, 9, 14, 0, 0, tzinfo=UTC)
        assert store.acquire_lock(DAY, now=old_now, stale_after=timedelta(hours=6)) is True

        # 잠금 파일의 mtime 을 6시간보다 더 이전으로 되돌린다.
        lock_path = tmp_path / f"{DAY}.lock"
        stale_epoch = old_now.timestamp() - timedelta(hours=7).total_seconds()
        import os

        os.utime(lock_path, (stale_epoch, stale_epoch))

        later = old_now + timedelta(hours=7)
        assert store.acquire_lock(DAY, now=later, stale_after=timedelta(hours=6)) is True

    def test_최근_잠금은_다시_가져오지_못한다(self, tmp_path: Path) -> None:
        store = ProposalStore(tmp_path)
        now = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)
        assert store.acquire_lock(DAY, now=now, stale_after=timedelta(hours=6)) is True
        soon = now + timedelta(hours=1)
        assert store.acquire_lock(DAY, now=soon, stale_after=timedelta(hours=6)) is False

    def test_완료_표식은_잠금과_별개다(self, tmp_path: Path) -> None:
        store = ProposalStore(tmp_path)
        assert store.is_done(DAY) is False
        store.mark_done(DAY)
        assert store.is_done(DAY) is True

    def test_남이_쥔_잠금은_풀지_않는다(self, tmp_path: Path) -> None:
        """워커가 여럿일 때 먼저 끝난 쪽이 남의 잠금을 지우면 배치가 두 번 돈다."""
        holder = ProposalStore(tmp_path)
        other = ProposalStore(tmp_path)
        now = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)
        assert holder.acquire_lock(DAY, now=now) is True

        other.release_lock(DAY)

        assert (tmp_path / f"{DAY}.lock").exists()
        assert other.acquire_lock(DAY, now=now) is False

    def test_집고_보니_다른_잠금이면_되돌리고_포기한다(self, tmp_path: Path) -> None:
        """오래된 것으로 보고 집는 사이 다른 워커가 새 잠금을 만들 수 있다.

        되돌리지 않으면 그 워커의 잠금을 빼앗아 같은 날 배치가 두 번 돈다.
        """
        store = ProposalStore(tmp_path)
        lock_path = tmp_path / f"{DAY}.lock"
        claimed = tmp_path / f"{DAY}.lock.stale-시험"
        claimed.write_text("새워커", encoding="utf-8")

        confirmed = store._confirm_claim(lock_path, claimed, "죽은워커")

        assert confirmed is False
        assert lock_path.read_text(encoding="utf-8") == "새워커"
        assert not claimed.exists()

    def test_집은_것이_집으려던_잠금이면_치운다(self, tmp_path: Path) -> None:
        store = ProposalStore(tmp_path)
        lock_path = tmp_path / f"{DAY}.lock"
        claimed = tmp_path / f"{DAY}.lock.stale-시험"
        claimed.write_text("죽은워커", encoding="utf-8")

        assert store._confirm_claim(lock_path, claimed, "죽은워커") is True
        assert not claimed.exists()
        assert not lock_path.exists()


class FailingApplier:
    """지식 파일 쓰기가 실패하는 상황을 만든다."""

    def apply(self, proposal: object) -> Mapping[str, int]:
        raise OSError("지식 파일을 쓸 수 없다")


class Test알림상태:
    def test_발송기가_실패를_값으로_알리면_notified_False(self, tmp_path: Path) -> None:
        """조립의 발송기는 실패를 예외가 아니라 보류 저장으로 처리한다.

        예외만 보면 즉시 발송 실패가 보고에서 성공으로 읽힌다.
        """
        batch, _store, rec = make_batch(tmp_path, notify=Recorder(result=False))
        report = batch.run(DAY)
        assert report.ran is True
        assert report.notified is False
        assert rec.calls != []


class Test완료표식:
    """다음 틱이 그날을 다시 실행할지 판정하는 근거다.

    제안 파일 존재로 판정하면 저장 뒤 반영이 실패한 날이 영영 다시 실행되지 않는다.
    """

    def test_정상_흐름은_완료로_표시한다(self, tmp_path: Path) -> None:
        batch, store, _ = make_batch(tmp_path)
        batch.run(DAY)
        assert store.is_done(DAY) is True

    def test_응답_기록이_없어도_완료로_표시한다(self, tmp_path: Path) -> None:
        """표시하지 않으면 기록이 없던 날을 주기마다 다시 집는다."""
        batch, store, _ = make_batch(tmp_path, archives=FakeArchives({}))
        report = batch.run(DAY)
        assert report.ran is False
        assert store.is_done(DAY) is True

    def test_반영이_실패하면_완료로_표시하지_않는다(self, tmp_path: Path) -> None:
        batch, store, _ = make_batch(tmp_path, applier=FailingApplier())
        with pytest.raises(OSError):
            batch.run(DAY)
        assert store.is_done(DAY) is False

if __name__ == "__main__":
    pytest.main([__file__])


class Test한_채널만_실패한_날:
    """A 성공 B 실패면 A 만 반영되고 B 의 실패는 알림에서 사라졌다. 완료 표식
    때문에 B 는 다음 날에도 재시도되지 않아 그날 B 의 학습이 유실됐다(sca-b4o).
    """

    def _배치(self, tmp_path: Path, analyzer: FakeAnalyzer, rec: Recorder | None = None):
        self.시계 = 가변시계(datetime(2026, 9, 14, 10, 0, tzinfo=UTC))
        return make_batch(
            tmp_path,
            archives=FakeArchives({"공지": "기록1", "잡담": "기록2"}),
            analyzer=analyzer,
            notify=rec,
            clock=self.시계,
        )

    def test_성공한_채널은_반영된다(self, tmp_path: Path) -> None:
        batch, store, _rec = self._배치(tmp_path, FakeAnalyzer(fail_channels={"잡담"}))
        batch.run(DAY)
        assert "공지" in store.latest().value().channel_knowledge

    def test_실패한_채널이_알림에_실린다(self, tmp_path: Path) -> None:
        batch, _store, rec = self._배치(tmp_path, FakeAnalyzer(fail_channels={"잡담"}))
        batch.run(DAY)
        assert "잡담" in rec.calls[0]

    def test_그날을_완료로_안_찍는다(self, tmp_path: Path) -> None:
        batch, store, _rec = self._배치(tmp_path, FakeAnalyzer(fail_channels={"잡담"}))
        batch.run(DAY)
        assert store.is_done(DAY) is False

    def test_다음_순회는_실패한_채널만_다시_분석한다(self, tmp_path: Path) -> None:
        """성공한 채널을 다시 분석하면 10분마다 중복 반영과 중복 알림이 난다."""
        analyzer = FakeAnalyzer(fail_channels={"잡담"})
        batch, _store, _rec = self._배치(tmp_path, analyzer)
        batch.run(DAY)
        analyzer.calls.clear()
        self.시계.다음_재시도까지()
        batch.run(DAY)
        assert analyzer.calls == ["잡담"]

    def test_실패가_풀리면_두_채널이_다_남는다(self, tmp_path: Path) -> None:
        analyzer = FakeAnalyzer(fail_channels={"잡담"})
        batch, store, _rec = self._배치(tmp_path, analyzer)
        batch.run(DAY)
        analyzer.fail_channels.clear()
        self.시계.다음_재시도까지()
        batch.run(DAY)

        proposal = store.latest().value()
        assert set(proposal.channel_knowledge) == {"공지", "잡담"}
        assert store.is_done(DAY) is True

    def test_복구_알림에_앞서_알린_채널_내용을_안_싣는다(self, tmp_path: Path) -> None:
        """제안을 저장된 완료 결과 전체로 다시 조립하므로 그대로 알리면 앞
        순회에서 이미 알린 채널이 한 번 더 나간다(sca-b4o 리뷰).
        """
        analyzer = FakeAnalyzer(fail_channels={"잡담"})
        batch, _store, rec = self._배치(tmp_path, analyzer)
        batch.run(DAY)
        analyzer.fail_channels.clear()
        self.시계.다음_재시도까지()
        batch.run(DAY)

        assert "잡담" in rec.calls[-1]
        assert "공지" not in rec.calls[-1]

    def test_발송이_실패하면_다음_순회가_다시_알린다(self, tmp_path: Path) -> None:
        """이번 순회 결과만 알리면 발송이 실패한 채널의 학습이 영영 안 알려진다
        (sca-b4o 리뷰).
        """
        analyzer = FakeAnalyzer()
        rec = Recorder(result=False)
        batch, _store, _rec = self._배치(tmp_path, analyzer, rec)
        batch.run(DAY)
        보낸_수 = len(rec.calls)
        rec._result = True
        self.시계.다음_재시도까지()
        batch.run(DAY)
        assert len(rec.calls) == 보낸_수 + 1
        assert "공지" in rec.calls[-1]

    def test_반영이_늦게_성공하면_그것도_알린다(self, tmp_path: Path) -> None:
        """앞 순회에서 반영이 실패했다가 다음 순회에 들어가면, 알린 채널이라는
        이유로 조용히 완료되어 반영 사실이 안 알려진다(sca-b4o 리뷰).
        """
        class 늦는반영기:
            def __init__(self) -> None:
                self.works = False

            def apply(self, proposal):
                return {name: 1 for name in proposal.channel_knowledge} if self.works else {}

        반영기 = 늦는반영기()
        self.시계 = 가변시계(datetime(2026, 9, 14, 10, 0, tzinfo=UTC))
        batch, _store, rec = make_batch(
            tmp_path,
            archives=FakeArchives({"공지": "기록1", "잡담": "기록2"}),
            analyzer=FakeAnalyzer(fail_channels={"잡담"}),
            clock=self.시계,
            applier=반영기,
        )
        batch.run(DAY)
        보낸_수 = len(rec.calls)
        반영기.works = True
        self.시계.다음_재시도까지()
        batch.run(DAY)

        assert len(rec.calls) == 보낸_수 + 1
        assert "반영했습니다" in rec.calls[-1]
        assert "공지" in rec.calls[-1]

    def test_발송이_실패한_날을_완료로_안_찍는다(self, tmp_path: Path) -> None:
        """완료로 찍히면 일정기가 그 날짜를 다시 안 골라 재발송 경로가 막힌다
        (sca-b4o 리뷰).
        """
        rec = Recorder(result=False)
        batch, store, _rec = make_batch(
            tmp_path,
            archives=FakeArchives({"공지": "기록1"}),
            analyzer=FakeAnalyzer(),
            notify=rec,
        )
        batch.run(DAY)
        assert store.is_done(DAY) is False

    def test_기록이_비어도_안_알린_것이_있으면_완료로_안_찍는다(self, tmp_path: Path) -> None:
        """기록 조회 실패와 기록 없음이 같은 모습이다. 조기 반환이 진행 상태를
        안 보면 안 나간 알림이 그 자리에서 사라진다(sca-b4o 리뷰).
        """
        archives = FakeArchives({"공지": "기록1"})
        rec = Recorder(result=False)
        batch, store, _rec = make_batch(tmp_path, archives=archives, analyzer=FakeAnalyzer(), notify=rec)
        batch.run(DAY)

        archives._data = {}
        rec._result = True
        보낸_수 = len(rec.calls)
        batch.run(DAY)
        assert len(rec.calls) == 보낸_수 + 1
        assert store.is_done(DAY) is True

    def test_읽기가_실패한_채널의_진행_상태를_안_지운다(self, tmp_path: Path) -> None:
        """건너뛴 채널은 기록이 없는 채널과 같은 모습이다. 그 차이로 지우면
        남은 상태가 완료가 되고 그 채널은 영영 재시도에서 빠진다(sca-b4o 리뷰).
        """
        archives = FakeArchives({"공지": "기록1", "잡담": "기록2"})
        analyzer = FakeAnalyzer(fail_channels={"잡담"})
        self.시계 = 가변시계(datetime(2026, 9, 14, 10, 0, tzinfo=UTC))
        batch, store, _rec = make_batch(
            tmp_path, archives=archives, analyzer=analyzer, clock=self.시계)
        batch.run(DAY)

        archives._data = {"공지": "기록1"}
        archives.unreadable = {"잡담"}
        self.시계.다음_재시도까지()
        batch.run(DAY)
        assert store.is_done(DAY) is False

    def test_같은_실패를_되풀이_알리지_않는다(self, tmp_path: Path) -> None:
        analyzer = FakeAnalyzer(fail_channels={"잡담"}, fail_kind=FailureKind.USAGE_LIMIT)
        batch, _store, rec = self._배치(tmp_path, analyzer)
        batch.run(DAY)
        before = len(rec.calls)
        self.시계.다음_재시도까지()
        batch.run(DAY)
        assert len(rec.calls) == before

    def test_시도_상한을_넘기면_실패를_남긴_채_완료로_찍는다(self, tmp_path: Path) -> None:
        """영원히 미완료로 두면 그날이 매 순회 후보로 나온다."""
        analyzer = FakeAnalyzer(fail_channels={"잡담"})
        batch, store, _rec = self._배치(tmp_path, analyzer)
        for _ in range(MAX_ATTEMPTS):
            batch.run(DAY)
            self.시계.다음_재시도까지()
        assert store.is_done(DAY) is True

    def test_한도_소진은_상한을_넘어도_안_포기한다(self, tmp_path: Path) -> None:
        analyzer = FakeAnalyzer(fail_channels={"잡담"}, fail_kind=FailureKind.USAGE_LIMIT)
        batch, store, _rec = self._배치(tmp_path, analyzer)
        for _ in range(MAX_ATTEMPTS + 2):
            batch.run(DAY)
            self.시계.다음_재시도까지()
        assert store.is_done(DAY) is False


class Test상태_기록_순서와_알림_보존:
    """완료 표식과 진행 상태는 서로 다른 파일이라 그 사이에서 죽으면 어긋난다.
    진행 상태를 먼저 남기면 표식만 남고 상태가 뒤처지는 구간이 없어진다(sca-b4o 리뷰).
    """

    def test_진행_상태를_완료_표식보다_먼저_남긴다(self, tmp_path: Path) -> None:
        순서: list[str] = []

        class 기록제안저장소(ProposalStore):
            def mark_done(self, day: str):
                순서.append("done")
                return super().mark_done(day)

        class 기록진행저장소(ProgressStore):
            def save(self, progress):
                순서.append("progress")
                return super().save(progress)

        store = 기록제안저장소(tmp_path / "proposals")
        batch = LearningBatch(
            archives=FakeArchives({"공지": "기록1"}),
            reactions=FakeReactions(),
            builder=ProposalBuilder(FakeAnalyzer()),
            store=store,
            progress=기록진행저장소(tmp_path / "progress"),
            applier=LearningApplier(tmp_path / "knowledge", "테스트봇"),
            renderer=ProposalRenderer(),
            notify=Recorder(),
            clock=make_clock(datetime(2026, 9, 14, 10, 0, tzinfo=UTC)),
        )
        batch.run(DAY)
        assert 순서[-1] == "done"
        assert "progress" in 순서[:-1]

    def test_알림_전송이_실패하면_다음_순회에_다시_알린다(self, tmp_path: Path) -> None:
        """보냈다고 기록해 버리면 그 실패는 영원히 소유자에게 안 닿는다."""
        rec = Recorder(result=False)
        analyzer = FakeAnalyzer(fail_channels={"잡담"}, fail_kind=FailureKind.USAGE_LIMIT)
        시계 = 가변시계(datetime(2026, 9, 14, 10, 0, tzinfo=UTC))
        batch, _store, _ = make_batch(
            tmp_path, archives=FakeArchives({"공지": "기록1", "잡담": "기록2"}),
            analyzer=analyzer, notify=rec, clock=시계,
        )
        batch.run(DAY)
        시계.다음_재시도까지()
        batch.run(DAY)
        assert len(rec.calls) == 2

    def test_기록에서_사라진_채널_때문에_그날이_안_끝나는_일은_없다(self, tmp_path: Path) -> None:
        archives = FakeArchives({"공지": "기록1", "잡담": "기록2"})
        analyzer = FakeAnalyzer(fail_channels={"잡담"}, fail_kind=FailureKind.USAGE_LIMIT)
        batch, store, _rec = make_batch(tmp_path, archives=archives, analyzer=analyzer)
        batch.run(DAY)
        archives._data = {"공지": "기록1"}

        batch.run(DAY)
        assert store.is_done(DAY) is True


class Test반영보다_진행_상태를_먼저_남긴다:
    """반영 직후에 죽으면 그다음 순회가 성공한 채널을 다시 분석한다. 문구가
    조금만 달라지면 같은 날짜 지식이 또 쌓인다(sca-b4o 리뷰).
    """

    def _순서기록배치(self, tmp_path: Path, 순서: list[str]):
        class 기록반영기(LearningApplier):
            def apply(self, proposal):
                순서.append("apply")
                return super().apply(proposal)

        class 기록진행저장소(ProgressStore):
            def save(self, progress):
                순서.append("progress")
                return super().save(progress)

        return LearningBatch(
            archives=FakeArchives({"공지": "기록1"}),
            reactions=FakeReactions(),
            builder=ProposalBuilder(FakeAnalyzer()),
            store=ProposalStore(tmp_path / "proposals"),
            progress=기록진행저장소(tmp_path / "progress"),
            applier=기록반영기(tmp_path / "knowledge", "테스트봇"),
            renderer=ProposalRenderer(),
            notify=Recorder(),
            clock=make_clock(datetime(2026, 9, 14, 10, 0, tzinfo=UTC)),
        )

    def test_진행_상태가_반영보다_앞선다(self, tmp_path: Path) -> None:
        순서: list[str] = []
        self._순서기록배치(tmp_path, 순서).run(DAY)
        assert 순서.index("progress") < 순서.index("apply")

    def test_반영_뒤_중단돼도_다시_분석하지_않는다(self, tmp_path: Path) -> None:
        """진행 상태만 남아 있으면 다음 순회는 저장된 결과로 반영만 마무리한다."""
        analyzer = FakeAnalyzer()
        batch, store, _rec = make_batch(
            tmp_path, archives=FakeArchives({"공지": "기록1"}), analyzer=analyzer)
        batch.run(DAY)
        # 완료 표식이 없는 상태를 만든다 — 표식 직전에 죽은 것과 같다.
        (tmp_path / "proposals" / f"{DAY}.done").unlink()
        analyzer.calls.clear()

        report = batch.run(DAY)

        assert analyzer.calls == []
        assert store.is_done(DAY) is True
        assert report.proposal is not None
        assert report.proposal.has_content is True


class Test읽기가_실패한_채널:
    """건너뛴 채널은 기록이 없는 채널과 같은 모습이다. 진행 상태에 남기지 않으면
    그날이 완료로 찍히고 그 채널의 학습이 영영 유실된다(sca-b4o 리뷰).
    """

    def _배치(self, tmp_path: Path, archives: FakeArchives):
        self.시계 = 가변시계(datetime(2026, 9, 14, 10, 0, tzinfo=UTC))
        return make_batch(tmp_path, archives=archives, analyzer=FakeAnalyzer(), clock=self.시계)

    def test_처음부터_못_읽은_채널이_있으면_완료로_안_찍는다(self, tmp_path: Path) -> None:
        archives = FakeArchives({"공지": "기록1"})
        archives.unreadable = {"잡담"}
        batch, store, _rec = self._배치(tmp_path, archives)
        batch.run(DAY)
        assert store.is_done(DAY) is False

    def test_못_읽은_채널이_알림에_실린다(self, tmp_path: Path) -> None:
        archives = FakeArchives({"공지": "기록1"})
        archives.unreadable = {"잡담"}
        batch, _store, rec = self._배치(tmp_path, archives)
        batch.run(DAY)
        assert "잡담" in rec.calls[0]

    def test_전부_못_읽어도_완료로_안_찍는다(self, tmp_path: Path) -> None:
        archives = FakeArchives({})
        archives.unreadable = {"잡담"}
        batch, store, _rec = self._배치(tmp_path, archives)
        batch.run(DAY)
        assert store.is_done(DAY) is False

    def test_다시_읽히면_그때_분석한다(self, tmp_path: Path) -> None:
        archives = FakeArchives({"공지": "기록1"})
        archives.unreadable = {"잡담"}
        batch, store, _rec = self._배치(tmp_path, archives)
        batch.run(DAY)

        archives._data = {"공지": "기록1", "잡담": "기록2"}
        archives.unreadable = set()
        self.시계.다음_재시도까지()
        batch.run(DAY)
        assert set(store.latest().value().channel_knowledge) == {"공지", "잡담"}
        assert store.is_done(DAY) is True

    def test_끝난_채널이_안_읽히게_돼도_결과가_남는다(self, tmp_path: Path) -> None:
        """읽은 채널만으로 정리하면 그 결과가 지워지고 제안에서 사라진다."""
        archives = FakeArchives({"공지": "기록1", "잡담": "기록2"})
        batch, store, _rec = self._배치(tmp_path, archives)
        batch.run(DAY)

        archives._data = {"공지": "기록1"}
        archives.unreadable = {"잡담"}
        self.시계.다음_재시도까지()
        batch.run(DAY)
        assert set(store.latest().value().channel_knowledge) == {"공지", "잡담"}

    def test_읽기_실패는_재시도_간격을_지킨다(self, tmp_path: Path) -> None:
        """간격을 안 지키면 알림 재발송으로 도는 순회마다 시도 횟수가 올라
        파일이 그대로인데 상한을 소진한다(sca-b4o 리뷰).
        """
        archives = FakeArchives({"공지": "기록1"})
        archives.unreadable = {"잡담"}
        rec = Recorder(result=False)
        self.시계 = 가변시계(datetime(2026, 9, 14, 10, 0, tzinfo=UTC))
        batch, _store, _rec = make_batch(
            tmp_path, archives=archives, analyzer=FakeAnalyzer(), clock=self.시계, notify=rec)
        for _ in range(5):
            batch.run(DAY)
        진행 = ProgressStore(tmp_path / "progress").load(DAY)
        assert 진행.failures["잡담"].attempts == 1
