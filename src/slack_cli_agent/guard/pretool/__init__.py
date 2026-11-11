"""PreToolUse Bash guards, wired into the engine as built-in defaults.

Each module here (turn_budget, restart_guard, install_guard, scan_guard, sleep_poll_guard) is a standalone,
import-free-beyond-stdlib script in the same shape as
observability/progress_hook.py: engine/claude.py invokes it as
``<sys.executable> <module>.HOOK_SCRIPT`` through claude's ``--settings``
PreToolUse hooks for the ``Bash`` matcher, in that order.

serial_read_guard is the exception on two counts: it runs for every tool
(matcher ``*``), and it imports parallel_nudge.is_read from the package so
both hooks agree on what a lookup is. plan_gate also runs for every tool,
but only does anything on a turn whose engine env carries PLAN_GATE.
"""
