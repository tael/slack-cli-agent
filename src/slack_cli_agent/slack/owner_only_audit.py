"""owner_only_channels 선언이 실제 멤버 구성과 맞는지 주기적으로 본다.

소유자 전용 선언은 그 채널에 소유자와 봇밖에 없다는 실측에서 나왔다. 그것은
선언한 시점의 사실이고, 나중에 사람이 초대되면 조용히 어긋난다. 그 채널로
느린 요청 보고가 나가므로 어긋난 상태가 곧 유출이다(sca-d2s).

기동 점검에 넣지 않는다. 슬랙 조회가 실패하는 동안 봇이 못 뜨면, 설정이
맞는데도 장애 시간이 늘어난다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class OwnerOnlyFinding:
    """소유자 전용으로 선언된 채널 하나의 조회 결과.

    outsiders 가 비고 error 도 비면 이상 없음이다. 조회 자체가 안 된 것은
    error 로만 남긴다 — 비공개 채널에 봇이 없으면 조회가 실패하는데, 그것을
    위반으로 읽으면 정상 설정에 매번 경고가 뜬다.
    """

    channel: str
    outsiders: tuple[str, ...]
    error: str = ""


class OwnerOnlyChannelAudit:

    def __init__(
        self,
        *,
        channels: Sequence[str],
        owner_user_id: str,
        list_members: Callable[[str], Sequence[str]],
        is_bot: Callable[[str], bool],
        notify: Callable[[str], object] | None = None,
    ) -> None:
        self._channels = tuple(channels)
        self._owner_user_id = owner_user_id
        self._list_members = list_members
        self._is_bot = is_bot
        self._notify = notify
        self._last: tuple[OwnerOnlyFinding, ...] | None = None

    def findings(self) -> list[OwnerOnlyFinding]:
        found: list[OwnerOnlyFinding] = []
        for channel in self._channels:
            try:
                members = self._list_members(channel)
            except Exception as exc:  # noqa: BLE001 — 한 채널의 조회 실패가 나머지를 막으면 안 된다
                found.append(OwnerOnlyFinding(channel=channel, outsiders=(), error=str(exc)))
                continue
            outsiders = tuple(sorted(
                member for member in members
                if member != self._owner_user_id and not self._is_bot(member)
            ))
            if outsiders:
                found.append(OwnerOnlyFinding(channel=channel, outsiders=outsiders))
        return found

    def check(self) -> None:
        """한 번 돈다. 같은 상태가 이어지면 다시 알리지 않는다 — 주기마다 같은
        경고를 보내면 읽히지 않는다. 해소도 한 번 알린다."""
        current = tuple(self.findings())
        if self._last is not None and current == self._last:
            return
        resolved = self._last is not None and not current
        self._last = current

        if resolved:
            self._report("소유자 전용 채널 점검: 어긋남이 해소됐습니다")
            return
        for finding in current:
            if finding.error:
                self._report(
                    f"소유자 전용 채널 점검: {finding.channel} 의 멤버를 조회하지 "
                    f"못했습니다 ({finding.error}). 위반 여부는 판정 불가입니다"
                )
                continue
            self._report(
                f"소유자 전용으로 선언된 {finding.channel} 에 소유자와 봇이 아닌 "
                f"사람이 있습니다: {', '.join(finding.outsiders)}. "
                "이 채널로 나가는 보고가 그 사람에게도 갑니다"
            )

    def _report(self, text: str) -> None:
        log.warning("%s", text)
        if self._notify is None:
            return
        try:
            self._notify(text)
        except Exception as exc:  # noqa: BLE001 — 알림 실패가 주기 작업을 끊으면 안 된다
            log.warning("소유자 전용 채널 점검 알림 발송 실패 : %s", exc)
