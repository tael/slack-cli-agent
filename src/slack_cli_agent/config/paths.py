"""Paths under the state directory, assembled in one place."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

PROFILE_DIR_ENV = "SLACK_CLI_AGENT_PROFILE_DIR"
_APP_DIR_NAME = "slack-cli-agent"


def _env_dirs(env: Mapping[str, str]) -> tuple[Path, ...]:
    raw = env.get(PROFILE_DIR_ENV, "")
    return tuple(Path(part).expanduser() for part in raw.split(os.pathsep) if part)


def user_profile_dir(env: Mapping[str, str] | None = None, home: Path | None = None) -> Path:
    """Where an installed bot keeps its profiles.

    XDG says a relative XDG_CONFIG_HOME must be ignored, so it falls back to
    ~/.config in that case. The `profiles/` leaf matches the layout the repo
    already uses for profile files.
    """
    env = os.environ if env is None else env
    home = home or Path.home()
    configured = Path(env.get("XDG_CONFIG_HOME", "")).expanduser()
    base = configured if configured.is_absolute() else home / ".config"
    return base / _APP_DIR_NAME / "profiles"


def default_profile_dirs(
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    cwd: Path | None = None,
) -> tuple[Path, ...]:
    """Search order used by every command that takes --profile-dir.

    The environment wins so one machine can run several bots, the user config
    directory is what an installed bot uses, and the current directory stays
    last so running from a checkout keeps working (sca-jl4.3).
    """
    env = os.environ if env is None else env
    ordered = (*_env_dirs(env), user_profile_dir(env, home), cwd or Path.cwd())
    seen: list[Path] = []
    for path in ordered:
        if path not in seen:
            seen.append(path)
    return tuple(seen)


def default_profile_write_dir(
    env: Mapping[str, str] | None = None, home: Path | None = None
) -> Path:
    """Where `init` puts a new profile: the first place the search will look."""
    env = os.environ if env is None else env
    configured = _env_dirs(env)
    return configured[0] if configured else user_profile_dir(env, home)


@dataclass(frozen=True)
class StatePaths:
    root: Path

    @classmethod
    def for_bot(cls, name: str, home: Path | None = None) -> StatePaths:
        return cls((home or Path.home()) / f".{name}")

    @property
    def profile(self) -> Path:
        return self.root / "profile.json"

    @property
    def credentials(self) -> Path:
        """Slack tokens. Operator-owned, never part of the installed package."""
        return self.root / "credentials.json"

    @property
    def channels(self) -> Path:
        return self.root / "channels.json"

    @property
    def prompts(self) -> Path:
        return self.root / "prompts"

    @property
    def persona(self) -> Path:
        return self.root / "persona"

    @property
    def responses(self) -> Path:
        """Bot replies, logged per channel into per-day files."""
        return self.root / "responses"

    @property
    def proposals(self) -> Path:
        """Learning proposal files, one set per day."""
        return self.root / "proposals"

    @property
    def knowledge(self) -> Path:
        """Per-channel knowledge files; applying a learning proposal appends a line here."""
        return self.persona / "knowledge"

    @property
    def skills(self) -> Path:
        """Bot-owned skill files, kept out of any engine's shared home directory.

        Layout expected inside varies by engine (see engine/claude.py,
        engine/codex.py, engine/gemini.py for what each one can actually see).
        """
        return self.root / "skills"

    @property
    def engine_dir(self) -> Path:
        return self.root / "engine"

    @property
    def engine_state(self) -> Path:
        return self.root / "engine_state.json"

    @property
    def database(self) -> Path:
        """All machine state: queue, sessions, audit trail, postmortems, watch queue."""
        return self.root / "state.db"

    @property
    def audit_log(self) -> Path:
        """Audit log copy, kept alongside the DB since external tools read this format."""
        return self.root / "audit.jsonl"

    @property
    def state_snapshot(self) -> Path:
        """Process state snapshot, rewritten wholesale on a timer."""
        return self.root / "state.json"

    @property
    def pid_file(self) -> Path:
        return self.root / "bot.pid"

    @property
    def mcp_config(self) -> Path:
        return self.engine_dir / "mcp.json"

    def engine_settings(self, engine_type: str) -> Path:
        return self.engine_dir / f"settings-{engine_type}.json"

    def ensure(self) -> None:
        for path in (self.root, self.prompts, self.persona, self.engine_dir):
            path.mkdir(parents=True, exist_ok=True)
