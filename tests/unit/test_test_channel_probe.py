"""tools/test-channel-probe.py — 테스트 채널에 멘션을 넣고 결과를 읽는다.

멘션은 from_app_mention 이 받고 그 경로는 bot_id 를 안 거른다. bot_id 로 거르는
MessageKind.is_human 은 from_message 쪽이다. 그래서 다른 봇의 토큰으로 넣은
멘션이 대상 봇을 깨운다(2026-09-19 실측).

대상 채널은 프로필의 troubleshoot_channel 이다. 채널 ID 는 조직 고유값이라
스크립트가 기본값으로 가지면 안 되고(2026-09-15 사용자 지시), 인자로 받으면
운영 채널로 잘못 나갈 수 있다.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "test_channel_probe", Path(__file__).resolve().parents[2] / "tools" / "test-channel-probe.py"
)
assert _SPEC and _SPEC.loader
probe = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(probe)


class Test대상_채널:
    def test_프로필의_troubleshoot_channel_을_쓴다(self) -> None:
        assert probe.target_channel({"troubleshoot_channel": "C111"}) == "C111"

    def test_비어_있으면_거부한다(self) -> None:
        """빈 값으로 게시하면 슬랙이 어디로 보낼지 모른다. 조용히 넘기면
        어느 채널에 나갔는지 모르는 채로 끝난다."""
        with pytest.raises(SystemExit):
            probe.target_channel({"troubleshoot_channel": ""})

    def test_키가_없어도_거부한다(self) -> None:
        with pytest.raises(SystemExit):
            probe.target_channel({})


class Test멘션_문구:
    def test_봇_id_를_앞에_붙인다(self) -> None:
        assert probe.mention_text("U9", "지금 시각은") == "<@U9> 지금 시각은"


class Test결과_판정:
    """봇은 리액션으로 처리 상태를 말한다. 그 표식이 결과다."""

    def test_완료(self) -> None:
        assert probe.outcome_of(["eyes", "white_check_mark"]) == probe.DONE

    def test_실패(self) -> None:
        assert probe.outcome_of(["x"]) == probe.FAILED

    def test_침묵도_완결이다(self) -> None:
        """mark_silent 의 표식이다. 답할 것이 없어 조용히 끝낸 것이지
        안 깨어난 것이 아니다."""
        assert probe.outcome_of(["zipper_mouth_face"]) == probe.SILENT

    def test_대기도_도는_중이다(self) -> None:
        """mark_waiting 의 표식이다. 이것을 모르면 정상 경로가 반응 없음으로
        읽혀 시간 초과까지 기다린다."""
        assert probe.outcome_of(["hourglass"]) == probe.WAITING

    def test_감시로_넘어간_것은_완료가_아니다(self) -> None:
        assert probe.outcome_of(["mag"]) == probe.WATCHING

    def test_아직_도는_중(self) -> None:
        assert probe.outcome_of(["eyes"]) == probe.RUNNING

    def test_표식이_없으면_안_깨어난_것이다(self) -> None:
        """이 판정이 이 도구의 존재 이유다. 멘션이 봇에 안 닿은 것과
        봇이 실패한 것은 고칠 자리가 다르다."""
        assert probe.outcome_of([]) == probe.NO_REACTION

    def test_최종_표식이_진행_표식을_이긴다(self) -> None:
        """리액션 배열에는 시간 순서가 없다. 우선순위로 정한다."""
        assert probe.outcome_of(["white_check_mark", "eyes"]) == probe.DONE

    def test_실패가_완료보다_앞선다(self) -> None:
        """최종 표식은 하나만 남는 것이 정상이라 둘이 보이면 이상 신호다.
        그 경우 나쁜 쪽을 쓴다."""
        assert probe.outcome_of(["white_check_mark", "x"]) == probe.FAILED

    def test_표식_이름을_패키지에서_읽는다(self) -> None:
        """도구가 이모지 이름을 따로 적으면 봇 쪽 상수와 어긋난다."""
        from slack_cli_agent.slack.reactions import DONE_EMOJI, SILENT_MARK_EMOJI

        assert probe.outcome_of([SILENT_MARK_EMOJI]) == probe.SILENT
        assert set(DONE_EMOJI) <= {name for name, _ in probe.OUTCOMES}


class Test봇_이름_검증:
    """이름이 그대로 경로에 붙는다. 검증이 없으면 프로필 밖 JSON 을 읽어
    임의 채널로 보낼 수 있고, 채널을 인자로 안 받는 안전장치가 무의미해진다."""

    def test_정상_이름은_통과한다(self) -> None:
        assert probe.check_name("shinji") == "shinji"

    @pytest.mark.parametrize("나쁜이름", ["../secret", "a/b", "", "이름", "a.b"])
    def test_경로가_섞인_이름은_거부한다(self, 나쁜이름: str) -> None:
        with pytest.raises(SystemExit):
            probe.check_name(나쁜이름)


class Test종료코드:
    """자동화가 원인을 가를 수 있어야 한다. 전부 1로 내면 봇 실패와
    멘션 미도달과 시간 초과가 같아 보인다."""

    def test_완결은_0이다(self) -> None:
        assert probe.exit_code(probe.DONE) == 0
        assert probe.exit_code(probe.SILENT) == 0

    def test_봇_실패는_1이다(self) -> None:
        assert probe.exit_code(probe.FAILED) == 1

    def test_반응_없음은_따로_가른다(self) -> None:
        assert probe.exit_code(probe.NO_REACTION) == 3

    def test_미완결은_따로_가른다(self) -> None:
        assert probe.exit_code(probe.WATCHING) == 4
        assert probe.exit_code(probe.RUNNING) == 4


class Test스레드_읽기:
    def test_슬랙_오류를_반응_없음으로_읽지_않는다(self, monkeypatch) -> None:
        """권한·rate limit 오류를 표식 없음으로 읽으면 원인이 안 남은 채
        시간 초과까지 기다린다."""
        monkeypatch.setattr(probe, "_get", lambda url, token, cookie: {
            "ok": False, "error": "channel_not_found"})
        with pytest.raises(SystemExit):
            probe.thread_state("C1", "1.1", "t", "c")

    def test_봇_답만_고른다(self, monkeypatch) -> None:
        """사람이 같은 스레드에 쓴 글을 봇 응답으로 내보이면 안 된다."""
        monkeypatch.setattr(probe, "_get", lambda url, token, cookie: {
            "ok": True,
            "messages": [
                {"reactions": [{"name": "white_check_mark"}]},
                {"bot_id": "B1", "text": "봇 답"},
                {"user": "U1", "text": "사람 글"},
            ],
        })
        결과, 답 = probe.thread_state("C1", "1.1", "t", "c")
        assert 결과 == probe.DONE
        assert 답 == ["봇 답"]
