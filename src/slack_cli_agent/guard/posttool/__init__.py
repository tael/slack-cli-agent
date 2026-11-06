"""PostToolUse hooks, wired into the engine as built-in defaults.

Same shape as guard/pretool/: each module is a standalone,
import-free-beyond-stdlib script that engine/claude.py invokes as
``<sys.executable> <module>.HOOK_SCRIPT`` through claude's ``--settings``.
These never block a tool call; they only add context after one ran.
"""
