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


# What a posted reply is scrubbed of before it reaches Slack (A17). An answer
# that quotes shell output or a config file can carry a live credential, and
# a channel message is read by everyone in it. Mametchi has no such step, so
# this is not a port; it closes the gap on both bots' shared output path.
#
# Stricter than SLACK_TOKEN_RE on purpose: a reply often *talks about*
# tokens ("xoxc-토큰", "xoxb-..."), and masking prose would garble the answer.
# Each pattern asks for the length a real credential has.
_OUTBOUND_PATTERNS: tuple[re.Pattern[str], ...] = (
    # PEM private key, header to footer, so no line of the key survives.
    re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
        re.S,
    ),
    # Slack tokens and xoxd cookies (which carry %-escapes).
    re.compile(r"(?<![A-Za-z0-9])(?:xox[abcdeprs]|xapp|xwfp)-[A-Za-z0-9%-]{10,}"),
    # AWS access key id.
    re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Z0-9])"),
    # GitHub classic and fine-grained tokens.
    re.compile(r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{30,}"),
    # Anthropic and OpenAI API keys.
    re.compile(r"(?<![A-Za-z0-9])sk-(?:ant-|proj-)?[A-Za-z0-9_-]{30,}"),
)


# Not REDACTED: "***" is bold-italic markup, and the mrkdwn conversion of a
# plain channel collapses it to a lone "*", which no longer reads as masked.
OUTBOUND_REDACTED = "[비밀값 가림]"


def redact_outbound(text: str) -> str:
    """Masks credential-shaped strings in text about to be posted."""
    for pattern in _OUTBOUND_PATTERNS:
        text = pattern.sub(OUTBOUND_REDACTED, text)
    return text


def count_outbound_secrets(text: str) -> int:
    """How many credential-shaped strings redact_outbound would mask."""
    return sum(len(pattern.findall(text)) for pattern in _OUTBOUND_PATTERNS)
