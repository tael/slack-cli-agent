"""Paths under the state directory, assembled in one place."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


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
