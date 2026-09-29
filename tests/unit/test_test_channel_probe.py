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
                {"bot_id": "B1", "ts": "2.2", "text": "봇 답"},
                {"user": "U1", "text": "사람 글"},
            ],
        })
        결과, 답 = probe.thread_state("C1", "1.1", "t", "c")
        assert 결과 == probe.DONE
        # ts 도 함께 돌려준다 - 점검이 끝나면 그 답을 지운다 (sca-9bwx).
        assert [text for _ts, text in 답] == ["봇 답"]


class Test일시_오류_재시도:
    """2026-09-19 20:00 점검에서 asuka 가 HTTP 500 으로 종료코드 1 이었다.
    곧바로 다시 돌리자 0 이었다. 슬랙 쪽 일시 오류인데 봇 실패로 남았다
    (sca-oaty)."""

    def test_서버_오류는_다시_해_본다(self) -> None:
        import urllib.error

        시도: list[int] = []

        def 호출() -> str:
            시도.append(1)
            if len(시도) < 2:
                raise urllib.error.HTTPError("u", 500, "err", {}, None)  # type: ignore[arg-type]
            return "ok"

        assert probe.call_with_retry(호출, sleep=lambda _: None) == "ok"
        assert len(시도) == 2

    def test_연결_실패도_다시_해_본다(self) -> None:
        import urllib.error

        시도: list[int] = []

        def 호출() -> str:
            시도.append(1)
            if len(시도) < 3:
                raise urllib.error.URLError("연결 안 됨")
            return "ok"

        assert probe.call_with_retry(호출, sleep=lambda _: None) == "ok"

    def test_요청이_잘못된_것은_다시_안_한다(self) -> None:
        """400 은 다시 해도 같다. 재시도하면 원인만 늦게 드러난다."""
        import urllib.error

        시도: list[int] = []

        def 호출() -> str:
            시도.append(1)
            raise urllib.error.HTTPError("u", 400, "bad", {}, None)  # type: ignore[arg-type]

        with pytest.raises(urllib.error.HTTPError):
            probe.call_with_retry(호출, sleep=lambda _: None)
        assert len(시도) == 1

    def test_한도를_넘기면_전송_실패로_올린다(self) -> None:
        """봇 실패와 구분되는 예외여야 한다. 그대로 올리면 종료코드 1 이 돼
        봇이 답을 못 낸 것과 같아 보인다."""
        import urllib.error

        def 호출() -> str:
            raise urllib.error.HTTPError("u", 503, "err", {}, None)  # type: ignore[arg-type]

        with pytest.raises(probe.TransportFailed):
            probe.call_with_retry(호출, attempts=2, sleep=lambda _: None)

    def test_마지막_오류를_메시지에_남긴다(self) -> None:
        """무엇 때문에 못 했는지가 안 남으면 다시 재는 것 말고 할 수 있는 것이
        없다."""
        import urllib.error

        def 호출() -> str:
            raise urllib.error.HTTPError("u", 503, "서버 오류", {}, None)  # type: ignore[arg-type]

        with pytest.raises(probe.TransportFailed) as 잡힘:
            probe.call_with_retry(호출, attempts=2, sleep=lambda _: None)
        assert "503" in str(잡힘.value)

    def test_전송_실패의_종료코드는_봇_실패와_다르다(self) -> None:
        assert probe.TRANSPORT_EXIT not in (0, 1, 3, 4)


class Test게시는_다시_안_한다:
    """슬랙은 internal_error 에서 일부 작업이 이미 성공했을 수 있다고 적는다.
    500 을 '안 올라갔다' 로 읽고 다시 올리면 같은 멘션이 두 번 나가고 봇이 둘
    다 처리한다 (코덱스 리뷰)."""

    def test_쓰기는_한_번만_한다(self) -> None:
        import urllib.error

        시도: list[int] = []

        def 호출() -> str:
            시도.append(1)
            raise urllib.error.HTTPError("u", 500, "err", {}, None)  # type: ignore[arg-type]

        with pytest.raises(probe.TransportFailed):
            probe.call_with_retry(호출, attempts=1, sleep=lambda _: None)
        assert len(시도) == 1

    def test_게시_호출은_재시도_없이_보낸다(self, monkeypatch) -> None:
        본 = {}

        def 가짜(call, attempts=probe.RETRY_ATTEMPTS, delay=0.0, sleep=None):
            본["attempts"] = attempts
            return {"ok": True, "ts": "1.1"}

        monkeypatch.setattr(probe, "call_with_retry", 가짜)
        probe._post("chat.postMessage", {"channel": "C1"}, "t")
        assert 본["attempts"] == 1

    def test_조회_호출은_재시도한다(self, monkeypatch) -> None:
        본 = {}

        def 가짜(call, attempts=probe.RETRY_ATTEMPTS, delay=0.0, sleep=None):
            본["attempts"] = attempts
            return {"ok": True}

        monkeypatch.setattr(probe, "call_with_retry", 가짜)
        probe._get("https://x", "t")
        assert 본["attempts"] > 1

    def test_읽기_전용_호출은_재시도한다(self, monkeypatch) -> None:
        """auth.test 는 쓰기가 아니다. 쓰기만 골라 막는 것이지 POST 전부가
        아니다."""
        본 = {}

        def 가짜(call, attempts=probe.RETRY_ATTEMPTS, delay=0.0, sleep=None):
            본["attempts"] = attempts
            return {"ok": True, "user_id": "U1"}

        monkeypatch.setattr(probe, "call_with_retry", 가짜)
        probe._post("auth.test", {}, "t")
        assert 본["attempts"] > 1


class Test대기_시간:
    def test_429는_슬랙이_알려준_시간을_기다린다(self) -> None:
        import urllib.error

        잔: list[float] = []
        시도: list[int] = []

        def 호출() -> str:
            시도.append(1)
            if len(시도) < 2:
                raise urllib.error.HTTPError("u", 429, "slow", {"Retry-After": "7"}, None)  # type: ignore[arg-type]
            return "ok"

        assert probe.call_with_retry(호출, delay=3, sleep=잔.append) == "ok"
        assert 잔 == [7.0]

    def test_알려준_시간이_없으면_기본_간격을_쓴다(self) -> None:
        import urllib.error

        잔: list[float] = []
        시도: list[int] = []

        def 호출() -> str:
            시도.append(1)
            if len(시도) < 2:
                raise urllib.error.HTTPError("u", 500, "err", {}, None)  # type: ignore[arg-type]
            return "ok"

        probe.call_with_retry(호출, delay=3, sleep=잔.append)
        assert 잔 == [3]


class Test종료까지_이어진다:
    def test_슬랙에_못_닿으면_종료코드_5로_끝난다(self, monkeypatch) -> None:
        """상수만 보는 시험은 실제 종료 경로가 끊겨도 통과한다."""
        def 터짐(argv: list[str]) -> None:
            raise probe.TransportFailed("못 닿았다")

        monkeypatch.setattr(probe, "main", 터짐)
        with pytest.raises(SystemExit) as 잡힘:
            probe.run(["probe", "shinji", "질문"])
        assert 잡힘.value.code == probe.TRANSPORT_EXIT


class Test점검_흔적을_남기지_않는다:
    """점검이 잦아 테스트 채널이 같은 질문으로 찬다(사용자 지시 2026-09-20 -
    'UTC를 질문하는 테스트 슬랙은 너무 심하네. 작업후 제거해라'). 확인은
    그대로 하고 그 자리에 쌓이는 것만 없앤다."""

    def _지운것(self, monkeypatch) -> list[tuple[str, str]]:
        지운것: list[tuple[str, str]] = []

        def 가짜게시(method, payload, token):
            if method == "chat.delete":
                지운것.append((str(payload["channel"]), str(payload["ts"])))
            return {"ok": True}

        monkeypatch.setattr(probe, "_post", 가짜게시)
        return 지운것

    def test_완결이면_멘션과_답을_지운다(self, monkeypatch) -> None:
        지운것 = self._지운것(monkeypatch)
        probe.clear_probe("C1", "1.1", ["2.2", "3.3"], probe.DONE, "poster", "target")
        assert 지운것 == [("C1", "2.2"), ("C1", "3.3"), ("C1", "1.1")]

    def test_침묵도_완결이라_지운다(self, monkeypatch) -> None:
        지운것 = self._지운것(monkeypatch)
        probe.clear_probe("C1", "1.1", [], probe.SILENT, "poster", "target")
        assert 지운것 == [("C1", "1.1")]

    def test_실패면_남긴다(self, monkeypatch) -> None:
        """원인을 사람이 봐야 한다. 지우면 무엇이 잘못됐는지 사라진다."""
        지운것 = self._지운것(monkeypatch)
        probe.clear_probe("C1", "1.1", ["2.2"], probe.FAILED, "poster", "target")
        assert 지운것 == []

    def test_반응이_없어도_남긴다(self, monkeypatch) -> None:
        지운것 = self._지운것(monkeypatch)
        probe.clear_probe("C1", "1.1", ["2.2"], probe.NO_REACTION, "poster", "target")
        assert 지운것 == []

    def test_지우다_실패해도_점검_판정을_안_바꾼다(self, monkeypatch) -> None:
        """정리는 곁다리다. 그것 때문에 점검이 실패로 뒤집히면 안 된다."""
        def 터지는게시(method, payload, token):
            raise SystemExit("chat.delete 실패 : message_not_found")

        monkeypatch.setattr(probe, "_post", 터지는게시)
        probe.clear_probe("C1", "1.1", ["2.2"], probe.DONE, "poster", "target")

    def test_답은_그_봇의_토큰으로_지운다(self, monkeypatch) -> None:
        """봇 토큰은 자기가 올린 글만 지울 수 있다."""
        쓴토큰: list[tuple[str, str]] = []

        def 가짜게시(method, payload, token):
            if method == "chat.delete":
                쓴토큰.append((str(payload["ts"]), token))
            return {"ok": True}

        monkeypatch.setattr(probe, "_post", 가짜게시)
        probe.clear_probe("C1", "1.1", ["2.2"], probe.DONE, "poster", "target")
        assert 쓴토큰 == [("2.2", "target"), ("1.1", "poster")]
