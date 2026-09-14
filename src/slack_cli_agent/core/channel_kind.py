"""채널 ID 로 DM 여부를 판정한다.

같은 판정(`channel.startswith("D")`)이 `auth/policy.py`, `slack/publisher.py`,
`slack/participants.py`, `core/application.py` 네 곳에 따로 적혀 있었다.
슬랙 채널 ID 규칙(선행 문자로 종류를 가른다)을 코드 곳곳이 직접 아는 셈이라,
규칙이 바뀌면 네 곳을 다 찾아 고쳐야 했다. 판정을 여기 하나로 모은다.

`auth` 는 지금 `slack` 을 import 하지 않는다 — 권한 계층이 발신 계층에
얽매일 이유가 없다. 그 경계를 새로 엮지 않으려고 이 판정을 `slack` 이 아니라
`core` 에 둔다. `core.markers` 가 가드·게이트·발신 세 계층이 공유하는 정규식을
같은 이유로 `core` 에 둔 것과 같은 자리다. `slack` 은 이미 `core` 에
의존하므로(`publisher.py` 가 `core.markers` 를 쓴다) 이 방향의 의존은 새로
생기지 않는다.

인자 하나를 받아 참거짓을 내는 순수 판정이라 클래스로 감쌀 상태가 없다.
`slack/message_kind.py` 의 `MessageKind` 는 받아들이는 subtype 목록을 주입받는
설정이 있어 클래스가 맞지만, 여기는 그런 설정이 없어 함수로 둔다.
"""

from __future__ import annotations

# 슬랙 채널 ID 는 선행 문자로 종류를 가른다. D 는 DM, C 는 공개/비공개 채널,
# G 는 그룹 DM(멀티파티)이다. 원본은 D 만 DM 으로 본다.
_DM_PREFIX = "D"


def is_direct_message_channel(channel: str) -> bool:
    """이 채널이 DM(1:1 대화)인가."""
    return channel.startswith(_DM_PREFIX)
