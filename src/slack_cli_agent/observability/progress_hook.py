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


def step_label(transcript_path: str) -> str:
    """"2/5 로그인 버튼 클릭" for the step ledger's running step
    (guard/plan_steps ``<transcript>.plan.steps.json``), else ""."""
    if not transcript_path:
        return ""
    try:
        ledger = json.loads(Path(transcript_path + ".plan.steps.json").read_text(encoding="utf-8"))
        steps = [s for s in ledger.get("steps") or [] if isinstance(s, dict)]
    except (OSError, ValueError, AttributeError):
        return ""
    if ledger.get("status") != "active":
        return ""
    for step in steps:
        if step.get("status") == "running":
            return f"{step.get('n')}/{len(steps)} {step.get('do', '')}"[:120]
    return ""


def record(log_path: Path, payload: str) -> None:
    """Appends one tool name from a hook payload, with the running step's
    label when a step ledger has one. Silent on anything unreadable."""
    try:
        data = json.loads(payload or "{}")
        tool_name = data.get("tool_name") or ""
        step = step_label(str(data.get("transcript_path") or ""))
    except (json.JSONDecodeError, AttributeError, TypeError):
        return
    if not tool_name:
        return
    if not step:
        append_tool(log_path, tool_name)
        return
    try:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"tool": tool_name, "step": step}, ensure_ascii=False) + "\n")
    except OSError:
        return


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
