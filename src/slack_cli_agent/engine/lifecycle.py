"""Process-lifetime state the engine layer keeps between requests.

Only the settings ledger lives here today. It records what the settings
files held when this process first read them, so a file removed or emptied
afterwards stops the turn instead of running it without a deny list
(sca-1aji). That baseline belongs to one bot run, not to the interpreter: a
process that builds a second Application -- tests, and the in-process
restart path -- would otherwise judge the new bot's files against the old
one's readings (sca-vlaj).

core/application.py has to call reset_engine_state() when it builds an
Application. This module is the entry point it calls; nothing here can
trigger itself.
"""

from __future__ import annotations

from .claude_settings import reset_settings_ledger


def reset_engine_state() -> None:
    """Drop what the engine layer remembers from an earlier bot run."""
    reset_settings_ledger()
