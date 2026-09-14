"""학습 배치가 읽는 두 자료의 계약.

배치(`learning/batch.py`)는 응답 기록과 사람 반응을 읽어 제안을 만든다. 그
두 자료의 실물은 각각 파일 시스템과 슬랙 API 에 있는데, 배치가 그것을 직접
알면 배치 시험이 파일과 API 를 함께 세워야 돈다. 여기 계약만 알게 해 실물을
밖에서 조립한다.

원본 learn.py 는 `RESPONSES.glob()` 과 `slack("conversations.replies")` 를
`main()` 안에서 직접 불렀다. 그래서 배치 흐름 자체가 한 번도 시험되지 않았다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol


class ResponseArchiveReader(Protocol):
    """하루치 응답 기록을 채널별로 읽는다."""

    def read_day(self, day: str) -> Mapping[str, str]:
        """채널 이름 → 그 채널의 그날 응답 기록 전문. 없으면 빈 매핑이다."""
        ...


class ReactionSource(Protocol):
    """응답 스레드에 사람이 남긴 말을 채널별로 모은다."""

    def collect(self, day: str) -> Mapping[str, Sequence[Mapping[str, object]]]:
        """채널 이름 → 그 채널에서 모은 반응 목록. 없으면 빈 매핑이다."""
        ...
