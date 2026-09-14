"""Whether a channel ID is a DM.

Lives in `core` rather than `slack` so `auth` (which doesn't depend on `slack`)
can use it without a new cross-layer dependency. Consolidates a check that used
to be duplicated across `auth/policy.py`, `slack/publisher.py`,
`slack/participants.py`, and `core/application.py`.
"""

from __future__ import annotations

# Slack channel IDs are prefixed by kind: D=DM, C=public/private channel, G=group DM.
_DM_PREFIX = "D"


def is_direct_message_channel(channel: str) -> bool:
    return channel.startswith(_DM_PREFIX)
