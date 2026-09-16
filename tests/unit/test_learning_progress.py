"""날짜별 채널 진행 상태.

배치가 채널 하나만 실패했을 때 그날을 완료로 찍으면 그 채널의 학습은 영구
유실된다(sca-b4o). 그렇다고 그날 전체를 다시 돌리면 성공한 채널까지 10분마다
재분석·중복 알림된다. 그래서 채널 단위로 무엇이 끝났는지를 남긴다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from slack_cli_agent.learning.decoder import ChannelAnalysisResult
from slack_cli_agent.learning.progress import (
    MAX_ATTEMPTS,
    ChannelFailure,
    DayProgress,
    FailureKind,
    ProgressStore,
)

NOW = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)
DAY = "2026-09-14"


def 실패(channel: str, kind: FailureKind = FailureKind.ENGINE_FAILED, attempts: int = 1) -> ChannelFailure:
    return ChannelFailure(channel=channel, kind=kind, detail="사유", attempts=attempts)


class Test다음_순회_대상:
    def test_끝난_채널은_다시_안_돈다(self) -> None:
        progress = DayProgress(day=DAY, completed={"공지": ChannelAnalysisResult()})
        assert progress.pending(["공지", "잡담"], NOW) == ("잡담",)

    def test_뽑을_것이_없어도_분석에_성공했으면_끝난_것이다(self) -> None:
        """항목이 비었다고 재시도하면 같은 답을 매 순회 다시 받는다."""
        progress = DayProgress(day=DAY, completed={"공지": ChannelAnalysisResult(note="배울 게 없다")})
        assert progress.pending(["공지"], NOW) == ()

    def test_실패한_채널은_다시_돈다(self) -> None:
        progress = DayProgress(day=DAY, failures={"잡담": 실패("잡담")})
        assert progress.pending(["공지", "잡담"], NOW) == ("공지", "잡담")

    def test_시도_상한을_넘긴_일반_실패는_더_안_돈다(self) -> None:
        progress = DayProgress(day=DAY, failures={"잡담": 실패("잡담", attempts=MAX_ATTEMPTS)})
        assert progress.pending(["잡담"], NOW) == ()

    def test_한도_소진은_상한을_넘어도_계속_돈다(self) -> None:
        """전환을 승인하면 풀리는 것이라 포기하면 승인이 무의미해진다."""
        progress = DayProgress(
            day=DAY, failures={"잡담": 실패("잡담", FailureKind.USAGE_LIMIT, attempts=MAX_ATTEMPTS + 5)})
        assert progress.pending(["잡담"], NOW) == ("잡담",)


class Test완료_판정:
    def test_전부_성공하면_끝난_것이다(self) -> None:
        progress = DayProgress(day=DAY, completed={"공지": ChannelAnalysisResult()}).with_reported()
        assert progress.settled is True

    def test_재시도가_남아_있으면_안_끝났다(self) -> None:
        assert DayProgress(day=DAY, failures={"잡담": 실패("잡담")}).settled is False

    def test_포기한_실패만_남으면_끝난_것이다(self) -> None:
        """영원히 미완료로 두면 그날이 계속 후보로 나온다."""
        progress = DayProgress(day=DAY, failures={"잡담": 실패("잡담", attempts=MAX_ATTEMPTS)})
        assert progress.with_reported().settled is True


class Test알리지_못한_날은_안_끝났다:
    """완료로 찍히면 일정기가 그 날짜를 다시 안 고른다. 분석이 다 됐어도 발송이
    실패했으면 아직 할 일이 남은 것이다(sca-b4o 리뷰).
    """

    def test_아직_안_알린_결과가_있으면_안_끝났다(self) -> None:
        progress = DayProgress(day=DAY, completed={"공지": ChannelAnalysisResult()})
        assert progress.settled is False

    def test_알리고_나면_끝난_것이다(self) -> None:
        progress = DayProgress(day=DAY, completed={"공지": ChannelAnalysisResult()}).with_reported()
        assert progress.settled is True

    def test_포기한_실패도_알려야_끝난_것이다(self) -> None:
        progress = DayProgress(day=DAY, failures={"잡담": 실패("잡담", attempts=MAX_ATTEMPTS)})
        assert progress.settled is False
        assert progress.with_reported().settled is True


class Test한_순회_결과를_합친다:
    def test_성공은_쌓이고_그_채널_실패는_지워진다(self) -> None:
        before = DayProgress(day=DAY, failures={"잡담": 실패("잡담")})
        after = before.with_round({"잡담": ChannelAnalysisResult(corrections=("고침",))}, (), NOW)
        assert after.failures == {}
        assert after.completed["잡담"].corrections == ("고침",)

    def test_같은_채널이_또_실패하면_시도_횟수가_는다(self) -> None:
        before = DayProgress(day=DAY, failures={"잡담": 실패("잡담", attempts=2)})
        after = before.with_round({}, (실패("잡담"),), NOW)
        assert after.failures["잡담"].attempts == 3

    def test_앞_순회의_성공은_안_지워진다(self) -> None:
        before = DayProgress(day=DAY, completed={"공지": ChannelAnalysisResult(corrections=("앞",))})
        after = before.with_round({"잡담": ChannelAnalysisResult()}, (), NOW)
        assert set(after.completed) == {"공지", "잡담"}


class Test한도_소진은_일반_실패_횟수를_안_깎는다:
    """한도 소진은 상한에서 면제인데 그 시도가 일반 실패 횟수에 합산되면
    한도 한 번이 일반 실패 허용치를 줄인다(sca-b4o 리뷰).
    """

    def test_한도_소진_뒤_일반_실패_둘은_아직_안_포기한다(self) -> None:
        progress = DayProgress(day=DAY).with_round({}, (실패("잡담", FailureKind.USAGE_LIMIT),), NOW)
        for _ in range(2):
            progress = progress.with_round({}, (실패("잡담"),), NOW)
        assert progress.failures["잡담"].retryable is True

    def test_일반_실패만_상한에_닿으면_포기한다(self) -> None:
        progress = DayProgress(day=DAY)
        for _ in range(MAX_ATTEMPTS):
            progress = progress.with_round({}, (실패("잡담"),), NOW)
        assert progress.failures["잡담"].retryable is False


class Test기록에서_사라진_채널:
    def test_그_채널의_실패는_정리된다(self) -> None:
        """분석할 기록이 없어진 채널을 미완료로 두면 그날이 영영 안 끝난다."""
        progress = DayProgress(day=DAY, failures={"잡담": 실패("잡담")})
        assert progress.restricted_to(["공지"]).failures == {}

    def test_남아_있는_채널의_실패는_그대로다(self) -> None:
        progress = DayProgress(day=DAY, failures={"잡담": 실패("잡담")})
        assert set(progress.restricted_to(["잡담"]).failures) == {"잡담"}


class Test같은_실패를_되풀이_안_알린다:
    def test_아직_안_알린_실패가_있으면_알릴_것이_있다(self) -> None:
        progress = DayProgress(day=DAY, failures={"잡담": 실패("잡담")})
        assert progress.unnotified_failures() == (실패("잡담"),)

    def test_이미_알린_같은_사유는_다시_안_알린다(self) -> None:
        progress = DayProgress(day=DAY, failures={"잡담": 실패("잡담")})
        assert progress.with_reported().unnotified_failures() == ()

    def test_사유가_바뀌면_다시_알린다(self) -> None:
        progress = DayProgress(day=DAY, failures={"잡담": 실패("잡담")}).with_reported()
        바뀜 = progress.with_round({}, (실패("잡담", FailureKind.USAGE_LIMIT),), NOW)
        assert 바뀜.unnotified_failures()[0].kind is FailureKind.USAGE_LIMIT


class Test파일로_남는다:
    def test_저장한_것이_그대로_돌아온다(self, tmp_path: Path) -> None:
        store = ProgressStore(tmp_path / "progress")
        progress = DayProgress(
            day=DAY,
            completed={"공지": ChannelAnalysisResult(channel_facts=("사실",), note="메모")},
            failures={"잡담": 실패("잡담", FailureKind.USAGE_LIMIT, attempts=2)},
        ).with_reported()
        store.save(progress)

        읽음 = store.load(DAY)
        assert 읽음.completed["공지"].channel_facts == ("사실",)
        assert 읽음.completed["공지"].note == "메모"
        assert 읽음.failures["잡담"].kind is FailureKind.USAGE_LIMIT
        assert 읽음.failures["잡담"].attempts == 2
        assert 읽음.unnotified_failures() == ()

    def test_시도_횟수_0이_재시작에서_1이_되지_않는다(self, tmp_path: Path) -> None:
        """한도 소진은 카운터를 안 올리는데 0 을 없는 값으로 읽으면 재시작이
        일반 실패 한 번을 미리 깎는다(sca-b4o 리뷰).
        """
        store = ProgressStore(tmp_path / "progress")
        store.save(DayProgress(day=DAY).with_round({}, (실패("잡담", FailureKind.USAGE_LIMIT),), NOW))
        assert store.load(DAY).failures["잡담"].attempts == 0

    def test_미완료로_남은_날을_찾을_수_있다(self, tmp_path: Path) -> None:
        store = ProgressStore(tmp_path / "progress")
        store.save(DayProgress(day="2026-09-14", failures={"잡담": 실패("잡담")}))
        store.save(DayProgress(day="2026-09-15", completed={"공지": ChannelAnalysisResult()}).with_reported())
        assert store.unsettled_days() == ("2026-09-14",)

    def test_진행_기록이_없으면_미완료도_없다(self, tmp_path: Path) -> None:
        assert ProgressStore(tmp_path / "progress").unsettled_days() == ()

    def test_기록이_없는_날은_빈_상태다(self, tmp_path: Path) -> None:
        progress = ProgressStore(tmp_path / "progress").load(DAY)
        assert progress.day == DAY
        assert progress.completed == {}
        assert progress.failures == {}

    def test_깨진_파일은_빈_상태로_읽는다(self, tmp_path: Path) -> None:
        """진행 상태를 못 읽는다고 그날 학습을 통째로 버릴 이유는 없다."""
        directory = tmp_path / "progress"
        directory.mkdir(parents=True)
        (directory / f"{DAY}.progress.json").write_text("{깨짐", encoding="utf-8")
        assert ProgressStore(directory).load(DAY).completed == {}

    def test_깨진_파일이_있는_날을_끝난_것으로_보지_않는다(self, tmp_path: Path) -> None:
        """빈 상태는 재시도할 것이 없다는 뜻이라 그대로 두면 그 날짜가 후보에서
        빠진다. 못 읽었으면 최악이 재분석이어야 한다(sca-b4o 리뷰).
        """
        directory = tmp_path / "progress"
        directory.mkdir(parents=True)
        (directory / f"{DAY}.progress.json").write_text("{깨짐", encoding="utf-8")
        store = ProgressStore(directory)
        assert store.load(DAY).settled is False
        assert store.unsettled_days() == (DAY,)

    def test_값이_깨진_파일도_예외를_안_낸다(self, tmp_path: Path) -> None:
        """JSON 문법은 맞는데 값이 이상하면 예외가 나 배치 틱이 멎는다."""
        directory = tmp_path / "progress"
        directory.mkdir(parents=True)
        (directory / f"{DAY}.progress.json").write_text(
            '{"failures": {"잡담": {"kind": "없는종류", "attempts": "숫자아님"}}}', encoding="utf-8")
        assert ProgressStore(directory).load(DAY).settled is False


class Test사유_종류:
    @pytest.mark.parametrize("kind", list(FailureKind))
    def test_사유마다_사람이_읽을_문구가_있다(self, kind: FailureKind) -> None:
        """종류를 늘리고 문구를 안 붙이면 알림에 코드 이름이 그대로 나간다."""
        assert kind.description


class Test재시도_간격:
    """한도 소진은 포기하지 않으므로 간격이 없으면 승인 전까지 매 순회 엔진을
    부른다(sca-b4o 리뷰).
    """

    def test_재시도_시각_전에는_후보가_아니다(self) -> None:
        progress = DayProgress(day=DAY).with_round({}, (실패("잡담", FailureKind.USAGE_LIMIT),), NOW)
        assert progress.pending(["잡담"], NOW + timedelta(minutes=1)) == ()

    def test_재시도_시각이_지나면_다시_후보다(self) -> None:
        progress = DayProgress(day=DAY).with_round({}, (실패("잡담", FailureKind.USAGE_LIMIT),), NOW)
        늦게 = NOW + FailureKind.USAGE_LIMIT.retry_delay + timedelta(seconds=1)
        assert progress.pending(["잡담"], 늦게) == ("잡담",)

    def test_한_번도_안_돈_채널은_바로_후보다(self) -> None:
        assert DayProgress(day=DAY).pending(["잡담"], NOW) == ("잡담",)

    def test_기다리는_중이어도_그날이_끝난_것은_아니다(self) -> None:
        progress = DayProgress(day=DAY).with_round({}, (실패("잡담", FailureKind.USAGE_LIMIT),), NOW)
        assert progress.settled is False

class Test깨진_파일은_계약_전체로_판정한다:
    """kind·attempts 만 검사하면 다른 항목이 깨졌을 때 빈 상태가 완료로 읽힌다
    (sca-b4o 리뷰). 진행 파일이 형식을 어기면 전부 재분석 대상으로 본다.
    """

    def 쓰기(self, tmp_path: Path, payload: str) -> ProgressStore:
        directory = tmp_path / "progress"
        directory.mkdir(parents=True)
        (directory / f"{DAY}.progress.json").write_text(payload, encoding="utf-8")
        return ProgressStore(directory)

    def test_재시도_시각_형식이_깨지면_못_읽은_것이다(self, tmp_path: Path) -> None:
        """읽을 때 안 걸러내면 pending 이 ValueError 를 내고 배치가 멎는다."""
        store = self.쓰기(tmp_path, '{"completed": {}, "notified": [], "announced": [], "failures": '
                          '{"잡담": {"kind": "usage_limit", "detail": "", "attempts": 0,'
                          ' "next_retry_at": "시각아님"}}}')
        assert store.load(DAY).readable is False
        assert store.load(DAY).pending(["잡담"], NOW) == ("잡담",)

    def test_시간대_없는_재시도_시각도_못_읽은_것이다(self, tmp_path: Path) -> None:
        """naive 와 aware 를 비교하면 TypeError 가 난다."""
        store = self.쓰기(tmp_path, '{"completed": {}, "notified": [], "announced": [], "failures": '
                          '{"잡담": {"kind": "usage_limit", "detail": "", "attempts": 0,'
                          ' "next_retry_at": "2026-09-14T10:00:00"}}}')
        assert store.load(DAY).readable is False

    def test_필수_항목이_없으면_못_읽은_것이다(self, tmp_path: Path) -> None:
        assert self.쓰기(tmp_path, "{}").load(DAY).readable is False

    def test_항목_형식이_다르면_못_읽은_것이다(self, tmp_path: Path) -> None:
        store = self.쓰기(tmp_path, '{"completed": [], "failures": {}, "notified": [], "announced": []}')
        assert store.load(DAY).readable is False

    def test_분석_결과_형식이_다르면_못_읽은_것이다(self, tmp_path: Path) -> None:
        store = self.쓰기(tmp_path, '{"completed": {"공지": {"writing_style": "목록아님",'
                          ' "channel_facts": [], "corrections": [], "note": ""}},'
                          ' "failures": {}, "notified": [], "announced": []}')
        assert store.load(DAY).readable is False


class Test대기_중인_날:
    """재시도 시각 전인 날을 계속 후보로 잡으면 이미 시각이 지난 옛 날짜가
    영원히 순서를 못 받는다(sca-b4o 리뷰).
    """

    def test_재시도_시각_전이면_대기_중이다(self) -> None:
        progress = DayProgress(day=DAY).with_round(
            {}, (실패("잡담", FailureKind.USAGE_LIMIT),), NOW).with_reported()
        assert progress.waiting(NOW + timedelta(minutes=1)) is True

    def test_재시도_시각이_지나면_대기가_아니다(self) -> None:
        progress = DayProgress(day=DAY).with_round(
            {}, (실패("잡담", FailureKind.USAGE_LIMIT),), NOW).with_reported()
        늦게 = NOW + FailureKind.USAGE_LIMIT.retry_delay + timedelta(seconds=1)
        assert progress.waiting(늦게) is False

    def test_기록이_없으면_대기가_아니다(self) -> None:
        assert DayProgress(day=DAY).waiting(NOW) is False

    def test_포기한_실패만_있으면_대기가_아니다(self) -> None:
        progress = DayProgress(
            day=DAY, failures={"잡담": 실패("잡담", attempts=MAX_ATTEMPTS)}).with_reported()
        assert progress.waiting(NOW) is False

    def test_안_알린_실패가_있으면_대기가_아니다(self) -> None:
        """대기로 두면 일정기가 그날을 건너뛰어 발송 재시도가 분석 재시도 간격
        만큼 밀린다(sca-b4o 리뷰).
        """
        progress = DayProgress(day=DAY).with_round({}, (실패("잡담", FailureKind.USAGE_LIMIT),), NOW)
        assert progress.waiting(NOW + timedelta(minutes=1)) is False

    def test_알리고_나면_다시_대기다(self) -> None:
        progress = DayProgress(day=DAY).with_round(
            {}, (실패("잡담", FailureKind.USAGE_LIMIT),), NOW).with_reported()
        assert progress.waiting(NOW + timedelta(minutes=1)) is True

    def test_못_읽은_날은_대기가_아니다(self, tmp_path: Path) -> None:
        directory = tmp_path / "progress"
        directory.mkdir(parents=True)
        (directory / f"{DAY}.progress.json").write_text("{깨짐", encoding="utf-8")
        assert ProgressStore(directory).is_waiting(DAY, NOW) is False

class Test값_타입도_계약이다:
    """str() 로 감싸면 무엇이든 통과한다. 음수 attempts 는 일반 실패가 상한에
    영영 안 닿게 만든다(sca-b4o 리뷰).
    """

    def 실패파일(self, tmp_path: Path, 항목: str) -> ProgressStore:
        directory = tmp_path / "progress"
        directory.mkdir(parents=True)
        (directory / f"{DAY}.progress.json").write_text(
            '{"completed": {}, "notified": [], "announced": [], "failures": {"잡담": ' + 항목 + "}}", encoding="utf-8")
        return ProgressStore(directory)

    def test_시도_횟수가_음수면_못_읽은_것이다(self, tmp_path: Path) -> None:
        store = self.실패파일(
            tmp_path, '{"kind": "engine_failed", "detail": "", "attempts": -100, "next_retry_at": ""}')
        assert store.load(DAY).readable is False

    def test_시도_횟수가_정수가_아니면_못_읽은_것이다(self, tmp_path: Path) -> None:
        store = self.실패파일(
            tmp_path, '{"kind": "engine_failed", "detail": "", "attempts": "2", "next_retry_at": ""}')
        assert store.load(DAY).readable is False

    def test_사유_설명이_문자열이_아니면_못_읽은_것이다(self, tmp_path: Path) -> None:
        store = self.실패파일(
            tmp_path, '{"kind": "engine_failed", "detail": 3, "attempts": 1, "next_retry_at": ""}')
        assert store.load(DAY).readable is False

    def test_재시도_시각이_문자열이_아니면_못_읽은_것이다(self, tmp_path: Path) -> None:
        store = self.실패파일(
            tmp_path, '{"kind": "engine_failed", "detail": "", "attempts": 1, "next_retry_at": false}')
        assert store.load(DAY).readable is False

    def test_알림_기록_원소가_문자열이_아니면_못_읽은_것이다(self, tmp_path: Path) -> None:
        directory = tmp_path / "progress"
        directory.mkdir(parents=True)
        (directory / f"{DAY}.progress.json").write_text(
            '{"completed": {}, "failures": {}, "notified": [1], "announced": []}', encoding="utf-8")
        assert ProgressStore(directory).load(DAY).readable is False

    def test_분석_결과_원소가_문자열이_아니면_못_읽은_것이다(self, tmp_path: Path) -> None:
        directory = tmp_path / "progress"
        directory.mkdir(parents=True)
        (directory / f"{DAY}.progress.json").write_text(
            '{"completed": {"공지": {"writing_style": [1], "channel_facts": [],'
            ' "corrections": [], "note": ""}}, "failures": {}, "notified": [], "announced": []}', encoding="utf-8")
        assert ProgressStore(directory).load(DAY).readable is False

    def test_메모가_문자열이_아니면_못_읽은_것이다(self, tmp_path: Path) -> None:
        directory = tmp_path / "progress"
        directory.mkdir(parents=True)
        (directory / f"{DAY}.progress.json").write_text(
            '{"completed": {"공지": {"writing_style": [], "channel_facts": [],'
            ' "corrections": [], "note": 5}}, "failures": {}, "notified": [], "announced": []}', encoding="utf-8")
        assert ProgressStore(directory).load(DAY).readable is False

class Test모순된_상태는_읽기_실패다:
    def test_한_채널이_완료와_실패에_함께_있으면_못_읽은_것이다(self, tmp_path: Path) -> None:
        """완료라서 재분석은 안 되고 실패가 남아 그날이 안 끝난다. 그 날짜가 매
        틱 후보로 다시 뽑히며 배치가 아무 진전 없이 돈다(sca-b4o 리뷰).
        """
        directory = tmp_path / "progress"
        directory.mkdir(parents=True)
        (directory / f"{DAY}.progress.json").write_text(
            '{"completed": {"잡담": {"writing_style": [], "channel_facts": [],'
            ' "corrections": [], "note": ""}},'
            ' "failures": {"잡담": {"kind": "engine_failed", "detail": "",'
            ' "attempts": 1, "next_retry_at": ""}}, "notified": [], "announced": []}', encoding="utf-8")
        assert ProgressStore(directory).load(DAY).readable is False

class Test알린_분석_결과를_기록한다:
    """이번 순회 결과만 알리면 앞 순회에서 발송이 실패한 채널의 학습이 영영
    안 알려진다. 무엇을 알렸는지를 진행 상태에 남긴다(sca-b4o 리뷰).
    """

    def test_아직_안_알린_결과가_나온다(self) -> None:
        progress = DayProgress(day=DAY, completed={"공지": ChannelAnalysisResult()})
        assert set(progress.unannounced_results()) == {"공지"}

    def test_알린_결과는_다시_안_나온다(self) -> None:
        progress = DayProgress(day=DAY, completed={"공지": ChannelAnalysisResult()}).with_reported()
        assert progress.unannounced_results() == {}

    def test_알린_뒤_새로_끝난_채널만_나온다(self) -> None:
        progress = DayProgress(day=DAY, completed={"공지": ChannelAnalysisResult()}).with_reported()
        나중 = progress.with_round({"잡담": ChannelAnalysisResult()}, (), NOW)
        assert set(나중.unannounced_results()) == {"잡담"}

    def test_기록은_파일로_남는다(self, tmp_path: Path) -> None:
        store = ProgressStore(tmp_path / "progress")
        store.save(DayProgress(day=DAY, completed={"공지": ChannelAnalysisResult()}).with_reported())
        assert store.load(DAY).unannounced_results() == {}
