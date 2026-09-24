"""Downloads and stores message attachments.

The downloader is injected so unit tests never hit the network. A
download can come back 200 with an HTML login page when the token
lacks access, so Content-Type is checked instead of trusting the
status code.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DownloadResult:
    content_type: str
    data: bytes


@dataclass(frozen=True)
class SavedAttachment:
    path: str
    name: str
    kind: str


Downloader = Callable[[str, str], DownloadResult]


class AttachmentStore:
    def __init__(
        self,
        attach_dir: Path,
        token_provider: Callable[[], str],
        downloader: Downloader,
        max_files: int = 5,
        max_bytes: int = 20 * 1024 * 1024,
        keep_hours: int = 48,
    ) -> None:
        self._attach_dir = attach_dir
        self._token_provider = token_provider
        self._downloader = downloader
        self._max_files = max_files
        self._max_bytes = max_bytes
        self._keep_hours = keep_hours

    def save(self, event: Mapping[str, Any]) -> list[SavedAttachment]:
        files = event.get("files") or []
        if not files:
            return []
        token = self._token_provider()
        if not token:
            return []

        saved: list[SavedAttachment] = []
        where = self._attach_dir / str(event.get("ts") or "misc")
        for f in files[: self._max_files]:
            url = f.get("url_private_download") or f.get("url_private")
            if not url:
                continue
            size = f.get("size") or 0
            if size > self._max_bytes:
                continue
            name = re.sub(r"[^\w.\-가-힣]+", "_", f.get("name") or f.get("id") or "file")
            dest = where / name
            try:
                result = self._downloader(url, token)
            except Exception:  # noqa: BLE001 - one attachment failing shouldn't block the rest
                log.warning("첨부 다운로드 실패 : %s", name)
                continue
            if "text/html" in (result.content_type or ""):
                # Slack returns a login page (still HTTP 200) when we lack access to the file.
                continue
            if len(result.data) > self._max_bytes:
                continue
            try:
                where.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(result.data)
            except OSError:
                # 한 파일의 쓰기 실패가 나머지 첨부까지, 나아가 catchup 이면
                # sweep() 밖 try 없이 이 메서드를 부르므로 채널 전체 처리를
                # 끊는다 (코덱스 8차 리뷰).
                log.warning("첨부 저장 실패 : %s", name)
                continue
            saved.append(
                SavedAttachment(
                    # Absolute regardless of how attach_dir was configured --
                    # the engine subprocess's cwd is workdir, not whatever
                    # this process's cwd was when a relative attach_dir got
                    # resolved (코덱스 9차 리뷰).
                    path=str(dest.resolve()),
                    name=f.get("name") or name,
                    kind=f.get("mimetype") or result.content_type,
                )
            )
        return saved

    def download(self, event: Mapping[str, Any]) -> tuple[tuple[dict[str, Any], ...], int]:
        """Downloads an event's files and merges the local path back onto
        Slack's own file fields, so callers keep whatever shape Slack sent
        plus `local_path`/`mimetype`. Shared by the live path (ingress) and
        catch-up recovery so a recovered request gets the same attachment
        handling a live one does (sca-h2dr)."""
        saved = self.save(event)
        requested = len(event.get("files") or ())
        # Missed count travels even when nothing was saved: a prompt section
        # reads it to tell the model it's answering without having seen a
        # file that was attached (sca-q45r).
        missed = max(0, requested - len(saved))
        if not saved:
            return (), missed
        originals = {f.get("name"): f for f in (event.get("files") or [])}
        merged: list[dict[str, Any]] = []
        for item in saved:
            original = dict(originals.get(item.name) or {})
            original["name"] = item.name
            original["mimetype"] = item.kind
            original["local_path"] = item.path
            merged.append(original)
        return tuple(merged), missed

    def cleanup(self, now: float | None = None) -> int:
        """지운 파일 수를 돌려준다. 건수가 없으면 0건과 아예 안 돈 것이
        로그에서 같아 보인다(sca-mf6)."""
        cut = (now if now is not None else time.time()) - self._keep_hours * 3600
        removed = 0
        try:
            for p in self._attach_dir.glob("*/*"):
                if p.is_file() and p.stat().st_mtime < cut:
                    p.unlink()
                    removed += 1
            for d in self._attach_dir.glob("*"):
                if d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
        except OSError as exc:
            # 조용히 넘기면 권한 문제로 한 번도 못 지운 상태가 정상과 같아 보인다.
            log.warning("첨부 정리 실패 : %s", exc)
        return removed
