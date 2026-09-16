"""시험용 봇 신원. 슬랙 조회 없이 고정 값을 쓴다.

`SlackBotIdentity` 는 `auth_test` 로 신원을 받는다. 판정이 아니라 다른 것을
확인하는 시험에서는 그 조회 자체가 관심사가 아니므로, 값을 바로 돌려주는
최소 대역을 쓴다.
"""

from __future__ import annotations

from typing import Any

from slack_cli_agent.slack.identity import BotIdentity, SlackBotIdentity


class _FixedAuthClient:
    def __init__(self, user_id: str, bot_id: str, team_id: str) -> None:
        self._result = {"ok": True, "user_id": user_id, "bot_id": bot_id, "team_id": team_id}

    def auth_test(self, **kwargs: Any) -> dict[str, Any]:
        return self._result


def fake_identity(
    user_id: str = "U_BOT", bot_id: str = "B_BOT", team_id: str = "T_TEAM",
) -> BotIdentity:
    """고정 신원. 조회는 일어나지 않는다."""
    return SlackBotIdentity(_FixedAuthClient(user_id, bot_id, team_id))
