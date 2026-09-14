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

from slack_cli_agent.core.result import Outcome
from slack_cli_agent.learning.analyzer import ChannelAnalysisResult, ProposalBuilder
from slack_cli_agent.learning.apply import LearningApplier
from slack_cli_agent.learning.batch import LearningBatch
from slack_cli_agent.learning.proposal import ProposalStore
from slack_cli_agent.learning.render import ProposalRenderer

DAY = "2026-09-14"


class FakeArchives:
    """ResponseArchiveReader 를 흉내 낸다."""

    def __init__(self, data: Mapping[str, str] | None = None) -> None:
        self._data = {"공지": "오늘 응답 기록 전문"} if data is None else data

    def read_day(self, day: str) -> Mapping[str, str]:
        return self._data


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

    def collect(self, day: str) -> Mapping[str, Sequence[Mapping[str, object]]]:
        if self._raise:
            raise RuntimeError("슬랙 conversations.replies 호출 실패")
        return self._data


class FakeAnalyzer:
    """ProposalAnalyzer 를 흉내 낸다. analyze_channel() 만 쓰인다."""

    def __init__(
        self, result: ChannelAnalysisResult | None = None, *, fail_reason: str | None = None,
    ) -> None:
        self._result = result if result is not None else ChannelAnalysisResult(
            channel_facts=("9월 회의는 매주 화요일이다",),
        )
        self._fail_reason = fail_reason

    def analyze_channel(
        self, day: str, channel_name: str, archive_text: str,
        reactions: Sequence[Mapping[str, object]],
    ) -> Outcome[ChannelAnalysisResult]:
        if self._fail_reason is not None:
            return Outcome.unknown(self._fail_reason)
        return Outcome.found(self._result)


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


def make_batch(
    tmp_path: Path,
    *,
    archives: FakeArchives | None = None,
    reactions: FakeReactions | None = None,
    analyzer_result: ChannelAnalysisResult | None = None,
    analyzer_fail: str | None = None,
    notify: Recorder | None = None,
    clock: object | None = None,
    applier: object | None = None,
) -> tuple[LearningBatch, ProposalStore, Recorder]:
    store = ProposalStore(tmp_path / "proposals")
    applier = applier if applier is not None else LearningApplier(tmp_path / "knowledge", "테스트봇")
    renderer = ProposalRenderer()
    builder = ProposalBuilder(FakeAnalyzer(analyzer_result, fail_reason=analyzer_fail))
    rec = notify if notify is not None else Recorder()
    fixed_clock = clock if clock is not None else make_clock(datetime(2026, 9, 14, 10, 0, tzinfo=UTC))
    batch = LearningBatch(
        archives=archives or FakeArchives(),
        reactions=reactions or FakeReactions(),
        builder=builder,
        store=store,
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
