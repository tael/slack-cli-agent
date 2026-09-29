"""Slack token detection and redaction, in one place.

A token reaches a log or an error message through more than one route: a
profile key, a profile value, a file path, a URL query. Both the check that
rejects one and the redaction that keeps it out of a message read the same
pattern here, so widening it covers every caller at once (sca-jl4.4).
"""

from __future__ import annotations

import re

# Slack's own prefixes. Matched anywhere in the text, not only at the start:
# "Bearer xoxb-..." and "?access_token=xoxb-..." are the same secret.
# xoxb/xoxp/xoxa/xoxr/xoxs/xoxe user·bot·app tokens, xoxc/xoxd browser
# credentials, xapp app-level tokens, xwfp workflow tokens
# (docs.slack.dev/authentication/tokens).
SLACK_TOKEN_RE = re.compile(r"(?:xox[abcdeprs]|xapp|xwfp)-[A-Za-z0-9가-힣-]{2,}")

REDACTED = "***"


def contains_secret(text: str) -> bool:
    return SLACK_TOKEN_RE.search(text) is not None


def redact(text: str) -> str:
    """For text that must still be printed — a path, a search list."""
    return SLACK_TOKEN_RE.sub(REDACTED, text)
