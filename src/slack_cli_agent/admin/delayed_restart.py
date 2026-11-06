"""Restart explicit launchd services after the Slack acknowledgement returns."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def _save_status(path: Path, status: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(status, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def main(args: list[str]) -> int:
    profile_path, status_path, expected_model, *labels = args
    status: dict[str, object] = {"model": expected_model, "services": {}}
    time.sleep(2)
    try:
        profile = json.loads(Path(profile_path).read_text(encoding="utf-8"))
        engine = profile["primary_engine"]
        status["profile_verified"] = (
            engine.get("model") == expected_model and engine.get("model_owner") == expected_model
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        status["profile_verified"] = False
        status["error"] = str(exc)
        _save_status(Path(status_path), status)
        return 1
    if not status["profile_verified"]:
        _save_status(Path(status_path), status)
        return 1
    services = status["services"]
    assert isinstance(services, dict)
    for label in labels:
        result = subprocess.run(
            ["/bin/launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"], check=False
        )
        running = subprocess.run(
            ["/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"],
            capture_output=True, text=True, check=False,
        )
        services[label] = result.returncode == 0 and running.returncode == 0
    status["complete"] = all(services.values())
    _save_status(Path(status_path), status)
    if not status["complete"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
