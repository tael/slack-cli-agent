"""Value object and store for daily learning proposals.

Each day's proposal is one JSON file named `YYYY-MM-DD.json`, so lexical sort
is chronological. A missing file and a corrupt one are distinguished via
Outcome — treating a parse failure as "no proposal" would hide a proposal
that actually exists but can't be read.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..core.result import Outcome

_DEFAULT_STALE_AFTER = timedelta(hours=6)


def _str_tuple(value: object) -> tuple[str, ...]:
    # Trusts the on-disk contract that elements are strings; we write this
    # file ourselves, so no runtime check is added here.
    return tuple(value or ())  # type: ignore[arg-type]


@dataclass(frozen=True)
class LearningProposal:
    """One day's learning proposal.

    Note: the on-disk field is `channel_knowledge`, while the analyzer's
    result type (`ChannelAnalysisResult`) calls the same data `channel_facts`.
    """

    day: str
    writing_style: tuple[str, ...] = ()
    channel_knowledge: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    corrections: tuple[str, ...] = ()
    note: str = ""

    @classmethod
    def from_dict(cls, day: str, data: Mapping[str, object]) -> LearningProposal:
        raw_channels = data.get("channel_knowledge") or {}
        channels = {
            str(name): tuple(items or ())
            for name, items in raw_channels.items()
        } if isinstance(raw_channels, Mapping) else {}
        return cls(
            day=day,
            writing_style=_str_tuple(data.get("writing_style")),
            channel_knowledge=channels,
            corrections=_str_tuple(data.get("corrections")),
            note=str(data.get("note") or ""),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "writing_style": list(self.writing_style),
            "channel_knowledge": {k: list(v) for k, v in self.channel_knowledge.items()},
            "corrections": list(self.corrections),
            "note": self.note,
        }

    @property
    def has_content(self) -> bool:
        return bool(
            self.writing_style
            or any(self.channel_knowledge.values())
            or self.corrections
        )


class ProposalStore:
    """제안 파일을 읽고 쓴다. 쓰기는 임시 파일을 거쳐 os.replace 로 교체한다."""

    def __init__(self, proposal_dir: Path) -> None:
        self._dir = proposal_dir
        # Identifies which lock this instance holds, so release_lock never
        # releases a lock owned by another process.
        self._token = uuid.uuid4().hex

    def latest(self) -> Outcome[LearningProposal]:
        if not self._dir.exists():
            return Outcome.absent()
        files = sorted(self._dir.glob("*.json"))
        if not files:
            return Outcome.absent()
        path = files[-1]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            return Outcome.unknown(f"{path.name} 파싱 실패: {e}")
        if not isinstance(data, Mapping):
            return Outcome.unknown(f"{path.name} 형식이 맞지 않다")
        return Outcome.found(LearningProposal.from_dict(path.stem, data))

    def save(self, proposal: LearningProposal) -> Path:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / f"{proposal.day}.json"
        self._write_json(path, proposal.to_dict())
        return path

    def mark_applied(self, day: str, done: Mapping[str, int]) -> Path:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / f"{day}.applied"
        payload = {
            "applied_at": datetime.now(UTC).isoformat(),
            "done": dict(done),
        }
        self._write_json(path, payload)
        return path

    def mark_done(self, day: str) -> Path:
        # Separate from `.applied`: a day with nothing to apply is still
        # done, and using proposal-file existence as the marker would mean
        # a day whose apply step failed never gets retried.
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / f"{day}.done"
        self._write_json(path, {"done_at": datetime.now(UTC).isoformat()})
        return path

    def is_done(self, day: str) -> bool:
        return (self._dir / f"{day}.done").exists()

    @staticmethod
    def _write_json(path: Path, payload: Mapping[str, object]) -> None:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def acquire_lock(
        self, day: str, *, now: datetime, stale_after: timedelta = _DEFAULT_STALE_AFTER,
    ) -> bool:
        # Lock file uses O_CREAT|O_EXCL for atomic create-if-absent. A lock
        # older than stale_after is assumed left behind by a dead process
        # and reclaimed. `now` is passed in rather than read from the clock
        # so callers control time in tests.
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._lock_path(day)
        if self._try_create_lock(path):
            return True
        if not self._reclaim_stale(path, now=now, stale_after=stale_after):
            return False
        return self._try_create_lock(path)

    def _reclaim_stale(self, path: Path, *, now: datetime, stale_after: timedelta) -> bool:
        # rename() is atomic, so only one concurrent reclaimer wins. The
        # owner check guards against a race where the old lock was released
        # and a different worker grabbed a new one between our staleness
        # check and the rename.
        owner = self._read_lock_owner(path)
        if not self._is_stale(path, now=now, stale_after=stale_after):
            return False
        claimed = path.with_name(f"{path.name}.stale-{self._token}")
        try:
            os.rename(path, claimed)
        except OSError:
            return False
        return self._confirm_claim(path, claimed, owner)

    @staticmethod
    def _confirm_claim(path: Path, claimed: Path, owner: str | None) -> bool:
        if ProposalStore._read_lock_owner(claimed) != owner:
            os.replace(claimed, path)
            return False
        try:
            claimed.unlink()
        except OSError:
            return False
        return True

    @staticmethod
    def _read_lock_owner(path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return None

    @staticmethod
    def _is_stale(path: Path, *, now: datetime, stale_after: timedelta) -> bool:
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        except OSError:
            return False
        return now - mtime >= stale_after

    def release_lock(self, day: str) -> None:
        # Only releases a lock this instance owns. If the token on disk
        # doesn't match, another worker already reclaimed it after ours
        # went stale, and deleting it would let a third worker start too.
        path = self._lock_path(day)
        try:
            if path.read_text(encoding="utf-8").strip() != self._token:
                return
            path.unlink()
        except OSError:
            return

    def _lock_path(self, day: str) -> Path:
        return self._dir / f"{day}.lock"

    def _try_create_lock(self, path: Path) -> bool:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        try:
            os.write(fd, self._token.encode())
        finally:
            os.close(fd)
        return True
