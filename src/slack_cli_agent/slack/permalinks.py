"""Parses Slack permalinks out of message text."""

from __future__ import annotations

import re
from dataclasses import dataclass

# p1788253544408049 is 10 digits of seconds followed by 6 of microseconds.
# A reply link carries the parent ts separately in the thread_ts query.
SLACK_PERMALINK_RE = re.compile(
    r"https://[a-z0-9-]+\.slack\.com/archives/([CDG][A-Z0-9]+)/p(\d{10})(\d{6})"
    r"(?:\?[^\s>|]*thread_ts=(\d+\.\d+))?"
)


@dataclass(frozen=True)
class SlackLink:
    channel: str
    ts: str
    thread_ts: str


def parse_slack_links(text: str | None) -> tuple[SlackLink, ...]:
    links: list[SlackLink] = []
    seen: set[tuple[str, str]] = set()
    for match in SLACK_PERMALINK_RE.finditer(text or ""):
        channel, seconds, micros, thread_ts = match.groups()
        ts = f"{seconds}.{micros}"
        key = (channel, thread_ts or ts)
        if key in seen:
            continue
        seen.add(key)
        links.append(SlackLink(channel=channel, ts=ts, thread_ts=thread_ts or ts))
    return tuple(links)
