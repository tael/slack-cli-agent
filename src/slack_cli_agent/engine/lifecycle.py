"""Process-lifetime state the engine layer keeps between requests.

Only the settings ledger lives here today. It records what the settings
files held when this process first read them, so a file removed or emptied
afterwards stops the turn instead of running it without a deny list
(sca-1aji). That baseline is meant to span the whole real boot -- preflight
reads the files first, then Application is built and requests run, all in
one process -- so nothing in that path calls this. The only real caller is
test isolation (tests/conftest.py's autouse fixture): a test process reuses
the interpreter across tests, which would otherwise judge one test's files
against an earlier test's readings. A production Application constructor
used to call this too, which wiped the baseline preflight had just set in
the same process (sca-vlaj's premise was wrong; caught in codex review).
"""

from __future__ import annotations

from .claude_settings import reset_settings_ledger


def reset_engine_state() -> None:
    """Drop what the engine layer remembers from an earlier bot run."""
    reset_settings_ledger()
