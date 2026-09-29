"""Tool-start hook: appends the name of the tool about to run to a log file.

Runs inside the engine's own subprocess, once per tool call, not in the
bot process. The bot only reads the file it writes (ProgressLogReader),
so the two sides share nothing but the path and the one-line format:

    {"tool": "Read"}

Standalone and import-free beyond the standard library on purpose — it is
invoked as ``<python> <this file> <log path>``, so anything it imported
from the package would have to be importable from the engine's own
environment, which is a deliberately minimal allowlist (engine/environment.py).

Never fails the tool call. A hook returning nonzero is reported to the
engine as a hook error and shows up in the answer, so every failure path
here ends in exit 0 — progress display is cosmetic and must not change
what the engine does.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

#: Absolute path to this file, for the engine adapter that builds the
#: hook command. Taken from __file__ rather than assembled from the
#: package layout so moving the module can't leave a stale path behind.
HOOK_SCRIPT = Path(__file__).resolve()


def append_tool(log_path: Path, tool_name: str) -> None:
    """Appends one tool name in the format ProgressLogReader expects.

    Also called in-process by EngineRunner for engines that stream their own
    events (engine/stream.py), so the two writers share one format definition.
    Silent on any write failure -- progress display is cosmetic.
    """
    if not tool_name:
        return
    try:
        # Append mode with one write call per line: several tool calls can
        # overlap when the engine runs them in parallel, and a read-modify-write
        # would lose lines.
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"tool": tool_name}, ensure_ascii=False) + "\n")
    except OSError:
        return


def record(log_path: Path, payload: str) -> None:
    """Appends one tool name from a hook payload. Silent on anything unreadable."""
    try:
        tool_name = json.loads(payload or "{}").get("tool_name") or ""
    except (json.JSONDecodeError, AttributeError, TypeError):
        return
    append_tool(log_path, tool_name)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        return 0
    try:
        record(Path(argv[1]), sys.stdin.read())
    except Exception:  # noqa: BLE001 - see module docstring: never fail the tool call
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
