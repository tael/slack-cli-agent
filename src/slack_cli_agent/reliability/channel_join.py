"""Joins the registered channels marked `"join": true` once at start (G3).

mametchi did this from its bot process at startup (bot.py
join_registered_channels, added 2026-10-14 for data-support): a turn can't
read the bot token, so only the process that holds it can call
conversations.join. A bot removed from such a channel came back on the next
restart. The core had no conversations.join call at all, so a channel joined
that way stayed lost once the bot was removed.

Slack answers ok for a channel the bot is already in, so running this on
every start is safe. DMs and private channels can't be joined this way; a DM
is skipped and a private channel's error goes to the audit log.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..config.channel import ChannelConfig
from ..core.channel_kind import is_direct_message_channel
from ..observability.audit import IncidentKind

log = logging.getLogger(__name__)

#: The channel-file key, kept in ChannelConfig.extra (config/channel.py I82).
JOIN_KEY = "join"


class _Channels(Protocol):
    def all(self) -> Mapping[str, ChannelConfig]: ...


@dataclass
class JoinReport:
    joined: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)


def wants_join(config: ChannelConfig | None) -> bool:
    # Only a real true: a hand-written "false" string must not read as yes.
    return config is not None and config.extra.get(JOIN_KEY) is True


def join_registered_channels(
    client: Any,
    channels: _Channels,
    *,
    audit: Callable[..., None],
) -> JoinReport:
    report = JoinReport()
    for cid, config in channels.all().items():
        if not wants_join(config) or is_direct_message_channel(cid):
            continue
        try:
            client.conversations_join(channel=cid)
        except Exception as error:
            reason = str(error)[:200]
            report.failed[cid] = reason
            log.warning("채널에 참여하지 못했다 : %s %s", cid, reason)
            audit(IncidentKind.CHANNEL_JOIN_FAILED, channel=cid, error=reason)
            continue
        report.joined.append(cid)
        log.info("채널에 참여했다 : %s %s", cid, config.name)
        audit(IncidentKind.CHANNEL_JOIN, channel=cid)
    return report
