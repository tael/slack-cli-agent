"""ResponseGate 특성화 시험.

기대 출력은 원본 `bot.py` 의 `worth_answering`/`asked_back` 을 AST 로 뽑아
실제로 실행해 얻었다(손으로 짐작하지 않았다). 아래 각 케이스 옆 주석의 값이
그 실행 결과다.
"""

from __future__ import annotations

import pytest

from slack_cli_agent.slack.gate import AWAITING_DECISION, ResponseGate


@pytest.fixture
def gate() -> ResponseGate:
    return ResponseGate()


class TestWorthAnswering:
    """길이가 아니라 어휘로 판정한다.

    2026-08-25 에 길이 규칙이 승인 3건을 버렸다. "올려" 같은 15자 이하
    지시문도 답변 대상이어야 한다.
    """

    @pytest.mark.parametrize(
        "text,bot_asked,expected",
        [
            # 맞장구 어휘만으로 이뤄진 말은 답이 아니다.
            ("네넵", False, False),
            ("음", False, False),
            # 이모지·문장부호만 남으면 답이 아니다.
            ("😊", False, False),
            ("ㅋㅋㅋ", False, False),
            # 짧은 한국어 지시문은 답변 대상이다 — 길이로 거르지 않는다.
            ("올려", False, True),
            # 물음표가 있는 되물음은 봇이 안 물어도 답변 대상이다.
            ("배포해도 될까요?", False, True),
            # 맺음 인사는 물음표가 없으면 답이 아니다.
            ("잘 부탁드립니다", False, False),
            ("잘 부탁드립니다 감사합니다", False, False),
            # 괄호로 감싼 혼잣말은 봇이 되물은 자리라도 답이 아니다.
            ("(하품)", True, False),
            # 봇이 되물은 자리에서는 짧아도, 이모지·자모만 있어도 답이다.
            (":+1:", True, True),
            ("이거", True, True),
            ("네", True, True),
        ],
    )
    def test_matches_original_output(
        self, gate: ResponseGate, text: str, bot_asked: bool, expected: bool
    ) -> None:
        assert gate.worth_answering(text, bot_asked) is expected

    def test_empty_text_is_not_worth_answering(self, gate: ResponseGate) -> None:
        assert gate.worth_answering("") is False
        assert gate.worth_answering(None) is False  # type: ignore[arg-type]

    def test_mutter_only_beats_bot_asked(self, gate: ResponseGate) -> None:
        """괄호 혼잣말 판정이 되물음 판정보다 먼저 걸린다."""
        assert gate.worth_answering("(딴생각)", bot_asked=True) is False


class TestAskedBack:
    """봇이 되물었는지 판정한다. 걸린 시간 줄은 판정 전에 걷어낸다."""

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("지금 반영할까요?", True),
            # 걸린 시간 줄이 끝에 붙어 있어도 되물음 판정은 그대로다.
            # 2026-09-02 : 이 줄 때문에 "네" 가 맞장구로 걸러진 사고의 재발방지.
            ("지금 반영할까요.\n\n> 걸린 시간 : 42초", True),
            ("네 알겠습니다.", False),
            ("확인해볼까요", True),
        ],
    )
    def test_matches_original_output(
        self, gate: ResponseGate, text: str, expected: bool
    ) -> None:
        assert gate.asked_back(text) is expected


class TestAwaitingDecision:
    """평서형 승인 대기 — bot.py:8251-8264, 2026-09-19 추가분.

    "진행할지 말씀 주시면 그때 실행하겠습니다." 뒤에 온 "좋아" 가 물음표가
    없다는 이유로 맞장구로 버려졌던 사고(C01EWFYL077:1789783495.105289)의
    재발방지. ASKED_BACK(종결어미)만으로는 안 걸리는 평서형 승인 요청이다.
    """

    def test_정규식_원문이_원본과_같다(self) -> None:
        assert AWAITING_DECISION.pattern == (
            r"(?:말씀\s*(?:주시면|주세요|주시는\s*대로|부탁)"
            r"|알려\s*주(?:시면|세요)"
            r"|지시\s*(?:주시면|주세요|해\s*주세요)"
            r"|결정(?:해)?\s*주(?:시면|세요)"
            r"|(?:진행|반영|적용|실행|추가|삭제|정리)할지"
            r"|(?:확인|검토|판단|결정)\s*부탁)"
            r"[^\n]{0,40}$"
        )

    @pytest.mark.parametrize(
        "text,expected",
        [
            # 2026-09-19 사고 재현 문구 — 물음표 없이 승인을 구한다.
            ("진행할지 말씀 주시면 그때 실행하겠습니다.", True),
            ("반영할지 말씀 주세요.", True),
            ("배포 여부는 지시해 주세요.", True),
            ("삭제할지 결정해 주시면 진행하겠습니다.", True),
            # 종결어미로도 안 걸리고 평서형 패턴도 없으면 되물음이 아니다.
            ("네 알겠습니다.", False),
            # 이미 종결어미(ASKED_BACK)로 걸리던 문장은 그대로 걸린다.
            ("지금 반영할까요?", True),
        ],
    )
    def test_asked_back이_평서형_승인요청도_잡는다(
        self, gate: ResponseGate, text: str, expected: bool
    ) -> None:
        assert gate.asked_back(text) is expected
