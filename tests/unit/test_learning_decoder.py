"""학습 제안 JSON 디코딩 — 엔진이 내는 자유 형식 본문에서 제안 하나를 꺼낸다.

CLI 출력 형식은 각 엔진의 parse() 가 흡수하지만 모델이 쓴 본문은 그대로
`response.body` 로 온다. 코드펜스로 감싸는지, 앞뒤에 설명을 붙이는지는
엔진마다 다르므로 이 계층이 그 차이를 흡수한다(sca-vpv).
"""

from __future__ import annotations

import pytest

from slack_cli_agent.core.result import OutcomeKind
from slack_cli_agent.learning.decoder import ProposalDecoder

디코더 = ProposalDecoder()
정상 = '{"writing_style": ["짧게"], "channel_facts": [], "corrections": [], "note": "없음"}'


class Test본문에서_꺼내기:
    @pytest.mark.parametrize(
        ("형태", "본문"),
        [
            ("맨몸", 정상),
            ("코드펜스", f"```json\n{정상}\n```"),
            ("언어표시_없는_펜스", f"```\n{정상}\n```"),
            ("앞에_설명", f"분석 결과입니다.\n\n{정상}"),
            ("뒤에_설명", f"{정상}\n\n이상입니다."),
            ("앞뒤_설명", f"결과:\n{정상}\n확인 바랍니다."),
            ("앞뒤_공백", f"\n\n  {정상}  \n\n"),
        ],
    )
    def test_어느_형태로_와도_같은_제안을_꺼낸다(self, 형태, 본문) -> None:
        결과 = 디코더.decode(본문)
        assert 결과.is_found, 형태
        assert 결과.value().writing_style == ("짧게",)

    def test_설명_안에_중괄호가_있어도_꺼낸다(self) -> None:
        """greedy 정규식이 첫 { 부터 마지막 } 까지 먹던 자리다."""
        결과 = 디코더.decode(f'형식은 {{"writing_style": [...]}} 입니다.\n\n{정상}')
        assert 결과.is_found
        assert 결과.value().writing_style == ("짧게",)

    def test_뒤에_또_다른_중괄호가_있어도_꺼낸다(self) -> None:
        결과 = 디코더.decode(f"{정상}\n\n총 {{1}} 건입니다.")
        assert 결과.is_found


class Test모호하거나_못_읽을_때:
    def test_유효한_제안이_둘이면_고르지_않고_실패한다(self) -> None:
        """임의로 하나를 고르면 어느 쪽이 쓰였는지 아무도 모른다."""
        다른 = '{"writing_style": ["길게"], "channel_facts": [], "corrections": [], "note": ""}'
        결과 = 디코더.decode(f"{정상}\n\n{다른}")
        assert 결과.kind is OutcomeKind.UNKNOWN

    @pytest.mark.parametrize("본문", ["", "   ", "그냥 문장일 뿐이다", "{망가진 json", "[1, 2, 3]"])
    def test_제안이_없으면_판정_불가다(self, 본문) -> None:
        assert 디코더.decode(본문).kind is OutcomeKind.UNKNOWN


class Test필드_타입_검증:
    """배열 자리에 다른 것이 오면 tuple() 이 조용히 다른 값으로 바꾼다.
    문자열은 한 글자씩 쪼개지고 사전은 키만 남는다.
    """

    @pytest.mark.parametrize("값", ['"문장 하나"', '{"a": 1}', "123"])
    def test_배열이_아닌_값은_판정_불가다(self, 값) -> None:
        본문 = f'{{"writing_style": {값}, "channel_facts": [], "corrections": [], "note": ""}}'
        assert 디코더.decode(본문).kind is OutcomeKind.UNKNOWN

    def test_배열_원소가_문자열이_아니면_판정_불가다(self) -> None:
        본문 = '{"writing_style": [1, 2], "channel_facts": [], "corrections": [], "note": ""}'
        assert 디코더.decode(본문).kind is OutcomeKind.UNKNOWN

    def test_note가_문자열이_아니면_판정_불가다(self) -> None:
        본문 = '{"writing_style": [], "channel_facts": [], "corrections": [], "note": {"a": 1}}'
        assert 디코더.decode(본문).kind is OutcomeKind.UNKNOWN

    def test_null_은_빈_값으로_관용한다(self) -> None:
        """모델이 빈 배열을 null 로 쓰는 것은 흔하고 뜻이 모호하지 않다.
        다른 타입과 달리 조용히 다른 값으로 바뀔 여지가 없어 실패로 보지 않는다.
        """
        본문 = '{"writing_style": null, "channel_facts": [], "corrections": [], "note": ""}'
        결과 = 디코더.decode(본문)
        assert 결과.is_found
        assert 결과.value().writing_style == ()

    def test_없는_필드는_빈_값으로_둔다(self) -> None:
        """모델이 빈 배열을 통째로 생략하는 것은 형식 위반으로 보지 않는다."""
        결과 = 디코더.decode('{"note": "제안 없음"}')
        assert 결과.is_found
        assert 결과.value().writing_style == ()
        assert 결과.value().note == "제안 없음"


class Test무관한_객체는_후보가_아니다:
    """알려진 필드가 하나도 없는 객체는 제안이 아니다. 후보로 세면 빈 제안으로
    성공하거나, 진짜 제안 앞에 있을 때 후보 둘로 실패한다.
    """

    @pytest.mark.parametrize("본문", ["{}", '{"example": true}', '{"a": 1, "b": 2}'])
    def test_알려진_필드가_없으면_판정_불가다(self, 본문) -> None:
        assert 디코더.decode(본문).kind is OutcomeKind.UNKNOWN

    def test_무관한_객체_뒤의_제안은_읽는다(self) -> None:
        결과 = 디코더.decode(f'참고 {{"example": true}} 형식입니다.\n\n{정상}')
        assert 결과.is_found
        assert 결과.value().writing_style == ("짧게",)

    def test_note만_있어도_제안으로_본다(self) -> None:
        결과 = 디코더.decode('{"note": "배울 게 없었다"}')
        assert 결과.is_found
        assert 결과.value().note == "배울 게 없었다"


class Test긴_본문:
    def test_닫히지_않은_중괄호가_많으면_포기한다(self) -> None:
        """실패한 { 마다 남은 문자열 전체를 다시 넘기면 최악 O(N^2) 이다.
        시간을 재는 대신 탐색을 포기한 사유가 남는 것으로 판정한다.
        """
        결과 = 디코더.decode("{" * 100_000 + 정상)
        assert 결과.kind is OutcomeKind.UNKNOWN
        assert "500개" in 결과.reason

    def test_길기만_한_정상_본문은_그대로_읽는다(self) -> None:
        """길이로 자르면 긴 인용이 붙은 유효한 답까지 버린다."""
        결과 = 디코더.decode("가" * 300_000 + "\n\n" + 정상)
        assert 결과.is_found
