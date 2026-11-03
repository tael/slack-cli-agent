"""PreToolUse Bash guards, wired into the engine as built-in defaults.

Each module here (turn_budget, install_guard, scan_guard) is a standalone,
import-free-beyond-stdlib script in the same shape as
observability/progress_hook.py: engine/claude.py invokes it as
``<sys.executable> <module>.HOOK_SCRIPT`` through claude's ``--settings``
PreToolUse hooks for the ``Bash`` matcher, in that order.
"""
