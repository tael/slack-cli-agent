"""owner_only_channels 선언이 실제 멤버와 맞는지 본다(sca-d2s).

sca-39c 에서 트러블슈팅 채널을 소유자 전용으로 선언한 근거는 실측이었다 —
그 워크스페이스에 사람이 소유자 한 명뿐이었다. 그것은 시점 사실이다. 나중에
누가 초대돼 그 채널에 들어오면 선언은 소유자 전용인데 실제로는 아니고, 그
상태로 느린 요청 보고가 그 채널에 나간다.
"""

from __future__ import annotations

import logging

import pytest

from slack_cli_agent.slack.owner_only_audit import (
    OwnerOnlyChannelAudit,
    OwnerOnlyFinding,
)

OWNER = "U-소유자"


def _봇여부(user_id: str) -> bool:
    return user_id.startswith("B")


class Test소견:
    def _감사(self, 멤버: dict[str, list[str]], channels: tuple[str, ...] = ("C1",)) -> OwnerOnlyChannelAudit:
        def 조회(channel: str) -> list[str]:
            if channel not in 멤버:
                raise RuntimeError("채널을 못 읽는다")
            return 멤버[channel]

        return OwnerOnlyChannelAudit(
            channels=channels, owner_user_id=OWNER, list_members=조회, is_bot=_봇여부,
        )

    def test_소유자와_봇만_있으면_소견이_없다(self) -> None:
        assert self._감사({"C1": [OWNER, "B봇1", "B봇2"]}).findings() == []

    def test_다른_사람이_있으면_그_사람을_낸다(self) -> None:
        소견 = self._감사({"C1": [OWNER, "B봇1", "U-남"]}).findings()
        assert 소견 == [OwnerOnlyFinding(channel="C1", outsiders=("U-남",), error="")]

    def test_여러_명이면_정렬해서_낸다(self) -> None:
        """순서가 실행마다 달라지면 같은 상태가 다른 소견으로 보인다."""
        소견 = self._감사({"C1": [OWNER, "U-나", "U-가"]}).findings()
        assert 소견[0].outsiders == ("U-가", "U-나")

    def test_조회_실패는_위반이_아니다(self) -> None:
        """비공개 채널이고 봇이 멤버가 아니면 조회가 실패한다. 그것을 위반으로
        읽으면 정상 설정에 매번 경고가 뜬다."""
        소견 = self._감사({}).findings()
        assert len(소견) == 1
        assert 소견[0].outsiders == ()
        assert 소견[0].error

    def test_한_채널의_실패가_나머지_조회를_막지_않는다(self) -> None:
        소견 = self._감사({"C2": [OWNER, "U-남"]}, channels=("C1", "C2")).findings()
        assert [(f.channel, bool(f.error)) for f in 소견] == [("C1", True), ("C2", False)]

    def test_선언이_비어_있으면_아무것도_조회하지_않는다(self) -> None:
        조회내역: list[str] = []

        def 조회(channel: str) -> list[str]:
            조회내역.append(channel)
            return []

        감사 = OwnerOnlyChannelAudit(
            channels=(), owner_user_id=OWNER, list_members=조회, is_bot=_봇여부,
        )
        assert 감사.findings() == []
        assert 조회내역 == []


class Test알림:
    def _감사(self, 멤버: dict[str, list[str]], notify: object = None) -> OwnerOnlyChannelAudit:
        return OwnerOnlyChannelAudit(
            channels=("C1",), owner_user_id=OWNER,
            list_members=lambda channel: 멤버[channel], is_bot=_봇여부,
            notify=notify,  # type: ignore[arg-type]
        )

    def test_위반을_찾으면_소유자에게_알린다(self) -> None:
        보낸것: list[str] = []
        self._감사({"C1": [OWNER, "U-남"]}, notify=보낸것.append).check()
        assert len(보낸것) == 1
        assert "C1" in 보낸것[0] and "U-남" in 보낸것[0]

    def test_같은_상태가_이어지면_다시_안_알린다(self) -> None:
        """주기마다 같은 경고를 보내면 읽히지 않는다."""
        보낸것: list[str] = []
        감사 = self._감사({"C1": [OWNER, "U-남"]}, notify=보낸것.append)
        감사.check()
        감사.check()
        assert len(보낸것) == 1

    def test_상태가_바뀌면_다시_알린다(self) -> None:
        보낸것: list[str] = []
        멤버 = {"C1": [OWNER, "U-남"]}
        감사 = self._감사(멤버, notify=보낸것.append)
        감사.check()
        멤버["C1"] = [OWNER, "U-남", "U-또다른"]
        감사.check()
        assert len(보낸것) == 2

    def test_해소되면_그것도_알린다(self) -> None:
        """경고만 보내고 해소를 안 알리면 아직 위반 중인지 사람이 다시 조회해야
        한다."""
        보낸것: list[str] = []
        멤버 = {"C1": [OWNER, "U-남"]}
        감사 = self._감사(멤버, notify=보낸것.append)
        감사.check()
        멤버["C1"] = [OWNER]
        감사.check()
        assert len(보낸것) == 2
        assert "해소" in 보낸것[1]

    def test_알림_경로가_없어도_로그에는_남는다(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            self._감사({"C1": [OWNER, "U-남"]}).check()
        기록 = [r.getMessage() for r in caplog.records]
        assert any("C1" in m for m in 기록)
