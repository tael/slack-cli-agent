"""Moves a channel's files when its slug changes.

`channel_slug` names the knowledge file and the response archive. Before a
channel is registered that name is the channel ID, so registering it later --
or renaming it -- points every reader at a name nothing was ever written under,
and what had accumulated stops being read with nothing in the log (sca-do8s).
"""

from __future__ import annotations

import fcntl
import logging
import os
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import IO

log = logging.getLogger(__name__)

KNOWLEDGE_SUFFIX = ".md"

SLUG_MIGRATION_LOCK = "slug-migration.lock"
"""Lock file name under the state root. Named once because the worker and the
web console both build a migrator and must take the same lock (sca-uk55)."""

DEFAULT_LOCK_TIMEOUT = 5.0
_LOCK_POLL_SEC = 0.02


class ChannelSlugMigrator:
    """`file_dirs` hold one `<slug>.md` per channel (knowledge, learned).
    `tree_roots` hold one `<slug>/` directory per channel (responses).

    Never raises: it runs while the channel file is being reread on a request
    path, and a failed move must not stop the reply. A move it cannot make
    safely is left alone and logged instead.

    `migrate` returns False when something it could not move is still sitting
    under the old slug, so the caller can leave the old slug recorded and come
    back to it. A name collision returns True instead -- that one needs a
    person, and retrying it every reparse would only repeat the warning.
    """

    def __init__(
        self,
        *,
        file_dirs: Sequence[Path],
        tree_roots: Sequence[Path],
        lock_path: Path | None = None,
        lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
    ) -> None:
        self._file_dirs = tuple(file_dirs)
        self._tree_roots = tuple(tree_roots)
        # Worker and ingress reparse the channel file independently, so the
        # in-process lock in ChannelRegistry does not keep two migrations apart.
        self._lock_path = lock_path
        self._lock_timeout = lock_timeout

    def migrate(self, old_slug: str, new_slug: str) -> bool:
        if not old_slug or not new_slug or old_slug == new_slug:
            return True
        with self._exclusive() as acquired:
            if not acquired:
                log.warning(
                    "다른 프로세스가 채널 파일을 옮기는 중이라 이번에는 건너뛴다 : %s -> %s",
                    old_slug, new_slug,
                )
                return False
            done = True
            for directory in self._file_dirs:
                done = self._move_file(
                    directory / f"{old_slug}{KNOWLEDGE_SUFFIX}",
                    directory / f"{new_slug}{KNOWLEDGE_SUFFIX}",
                ) and done
            for root in self._tree_roots:
                done = self._move_tree(root / old_slug, root / new_slug) and done
            return done

    @contextmanager
    def _exclusive(self) -> Iterator[bool]:
        if self._lock_path is None:
            yield True
            return
        handle = self._open_lock(self._lock_path)
        if handle is None:
            yield True
            return
        try:
            yield self._acquire(handle)
        finally:
            with suppress(OSError):
                fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()

    @staticmethod
    def _open_lock(path: Path) -> IO[str] | None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            return open(path, "a+")
        except OSError as exc:
            # A lock we cannot create must not stop the move; one process
            # moving is still better than none.
            log.warning("이사 잠금 파일을 열지 못해 잠금 없이 옮긴다 : %s : %s", path, exc)
            return None

    def _acquire(self, handle: IO[str]) -> bool:
        deadline = time.monotonic() + self._lock_timeout
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(_LOCK_POLL_SEC)

    def _move_file(self, source: Path, target: Path) -> bool:
        if not source.is_file():
            return True
        if target.exists():
            log.warning(
                "채널 이름이 바뀌었으나 옮길 자리에 파일이 이미 있어 그대로 둔다 : %s 와 %s 를 사람이 합쳐야 한다",
                source, target,
            )
            return True
        if not self._replace(source, target):
            return False
        log.info("채널 이름이 바뀌어 지식 파일을 옮겼다 : %s -> %s", source, target)
        return True

    def _move_tree(self, source: Path, target: Path) -> bool:
        if not source.is_dir():
            return True
        if not target.exists():
            if not self._replace(source, target):
                return False
            log.info("채널 이름이 바뀌어 응답 기록을 옮겼다 : %s -> %s", source, target)
            return True
        try:
            children = sorted(source.iterdir())
        except OSError as exc:
            # Another process moving the same channel deletes this directory
            # out from under the listing.
            log.warning("응답 기록을 훑지 못해 다음에 다시 시도한다 : %s : %s", source, exc)
            return False
        겹친것: list[str] = []
        못옮긴것: list[str] = []
        for child in children:
            자리 = target / child.name
            if 자리.exists():
                겹친것.append(child.name)
            elif not self._replace(child, 자리):
                못옮긴것.append(child.name)
        if 겹친것:
            log.warning(
                "응답 기록을 옮기지 못한 파일이 있다 : %s 에 같은 이름이 있어 %s 에 남겨 둔다 : %s",
                target, source, ", ".join(겹친것),
            )
        if 겹친것 or 못옮긴것:
            return not 못옮긴것
        try:
            source.rmdir()
        except OSError as exc:
            log.warning("빈 응답 기록 디렉터리를 지우지 못했다 : %s : %s", source, exc)
        else:
            log.info("채널 이름이 바뀌어 응답 기록을 옮겼다 : %s -> %s", source, target)
        return True

    @staticmethod
    def _replace(source: Path, target: Path) -> bool:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
        except OSError as exc:
            log.warning("채널 이름이 바뀌었으나 옮기지 못했다 : %s -> %s : %s", source, target, exc)
            return False
        return True
