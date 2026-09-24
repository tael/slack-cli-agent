"""Moves a channel's files when its slug changes.

`channel_slug` names the knowledge file and the response archive. Before a
channel is registered that name is the channel ID, so registering it later --
or renaming it -- points every reader at a name nothing was ever written under,
and what had accumulated stops being read with nothing in the log (sca-do8s).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from pathlib import Path

log = logging.getLogger(__name__)

KNOWLEDGE_SUFFIX = ".md"


class ChannelSlugMigrator:
    """`file_dirs` hold one `<slug>.md` per channel (knowledge, learned).
    `tree_roots` hold one `<slug>/` directory per channel (responses).

    Never raises: it runs while the channel file is being reread on a request
    path, and a failed move must not stop the reply. A move it cannot make
    safely is left alone and logged instead.
    """

    def __init__(self, *, file_dirs: Sequence[Path], tree_roots: Sequence[Path]) -> None:
        self._file_dirs = tuple(file_dirs)
        self._tree_roots = tuple(tree_roots)

    def migrate(self, old_slug: str, new_slug: str) -> None:
        if not old_slug or not new_slug or old_slug == new_slug:
            return
        for directory in self._file_dirs:
            self._move_file(
                directory / f"{old_slug}{KNOWLEDGE_SUFFIX}",
                directory / f"{new_slug}{KNOWLEDGE_SUFFIX}",
            )
        for root in self._tree_roots:
            self._move_tree(root / old_slug, root / new_slug)

    def _move_file(self, source: Path, target: Path) -> None:
        if not source.is_file():
            return
        if target.exists():
            log.warning(
                "채널 이름이 바뀌었으나 옮길 자리에 파일이 이미 있어 그대로 둔다 : %s 와 %s 를 사람이 합쳐야 한다",
                source, target,
            )
            return
        if not self._replace(source, target):
            return
        log.info("채널 이름이 바뀌어 지식 파일을 옮겼다 : %s -> %s", source, target)

    def _move_tree(self, source: Path, target: Path) -> None:
        if not source.is_dir():
            return
        if not target.exists():
            if self._replace(source, target):
                log.info("채널 이름이 바뀌어 응답 기록을 옮겼다 : %s -> %s", source, target)
            return
        남은것: list[str] = []
        for child in sorted(source.iterdir()):
            자리 = target / child.name
            if 자리.exists() or not self._replace(child, 자리):
                남은것.append(child.name)
        if 남은것:
            log.warning(
                "응답 기록을 옮기지 못한 파일이 있다 : %s 에 같은 이름이 있어 %s 에 남겨 둔다 : %s",
                target, source, ", ".join(남은것),
            )
            return
        try:
            source.rmdir()
        except OSError as exc:
            log.warning("빈 응답 기록 디렉터리를 지우지 못했다 : %s : %s", source, exc)
        else:
            log.info("채널 이름이 바뀌어 응답 기록을 옮겼다 : %s -> %s", source, target)

    @staticmethod
    def _replace(source: Path, target: Path) -> bool:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
        except OSError as exc:
            log.warning("채널 이름이 바뀌었으나 옮기지 못했다 : %s -> %s : %s", source, target, exc)
            return False
        return True
