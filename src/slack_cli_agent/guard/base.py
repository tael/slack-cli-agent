"""OutputGuard 계약과 파이프라인이 주고받는 값 객체.

원본 이 봇은 발신 직전 보정 로직(호칭 치환, 엉뚱한 수신자 제거, 감시 태그
확인, 재작성 손실 검출)을 `handle_request` 안에 인라인 200줄 가까이 담고
있었다. 각 보정을 `OutputGuard` 하나로 떼어 테스트 가능하게 만든다.

감사 기록은 이 패키지의 책임이 아니다. `GuardResult.detail` 로 무엇이
바뀌었는지 돌려주는 것까지만 한다 — 그것을 감사 로그에 남기는 것은 이
파이프라인을 쓰는 쪽(RequestHandler 등, 이 작업 범위 밖)의 몫이다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar


@dataclass(frozen=True)
class RerunRequest:
    """이 가드를 통과시키려면 모델에게 본문을 다시 쓰게 해야 한다는 요청.

    엔진을 실제로 다시 부르는 것은 파이프라인의 책임이 아니다. 무엇을,
    왜 다시 쓰게 해야 하는지만 여기 담아 호출부로 돌려준다.
    """

    reason: str
    rewrite_prompt: str
    guard_name: str


@dataclass
class GuardResult:
    """가드 하나를 적용한 결과."""

    body: str
    changed: bool
    detail: Mapping[str, Any] = field(default_factory=dict)
    # 값이 있으면 이 가드는 아직 끝나지 않은 것이다. 호출부가 rewrite_prompt 로
    # 엔진을 다시 불러 새 본문을 받은 뒤, 그 본문으로 파이프라인을 다시 돌려야 한다.
    rerun: RerunRequest | None = None


@dataclass(frozen=True)
class GuardContext:
    """가드가 판정에 쓰는 요청 맥락.

    가드마다 보는 필드가 다르다 — 해당 없는 필드는 기본값을 그대로 둔다.
    """

    channel: str = ""
    thread_ts: str = ""
    asker_id: str = ""
    is_owner: bool = False
    owner_user_id: str = ""
    # 평문 호칭 -> 슬랙 사용자 ID. PlainMentionGuard 가 쓴다.
    mention_names: Mapping[str, str] = field(default_factory=dict)
    # 재작성 전 본문. 있을 때만 RewriteLossGuard 가 유실을 판정한다.
    previous_body: str | None = None
    # WatchPromiseGuard 의 rerun 요청으로 다시 쓴 본문을 다시 넣는 자리인지.
    # 재시도에도 실패하면 문장을 잘라내고 대체 문구를 붙인다.
    is_rewrite_retry: bool = False


class OutputGuard(ABC):
    """발신 직전 본문을 보정하는 가드 하나의 계약."""

    name: ClassVar[str]

    @abstractmethod
    def apply(self, body: str, ctx: GuardContext) -> GuardResult:
        """본문을 보정한다. 바꿀 것이 없으면 changed=False 로 그대로 돌려준다."""
