"""Exception hierarchy, grouped by what the caller can recover from."""

from __future__ import annotations


class AgentError(Exception):
    pass


class ConfigError(AgentError):
    pass


class MissingPromptError(ConfigError):
    # No fallback to a default prompt — that would mean answering without the guard text.
    pass


class EngineError(AgentError):
    pass


class UsageLimitError(EngineError):
    # Signals the fallback-engine switch, not just a generic engine failure.
    pass


class SlackError(AgentError):
    pass


class HistoryUnavailable(SlackError):
    # An empty result from the history lookup, distinct from "there is no history".
    pass
