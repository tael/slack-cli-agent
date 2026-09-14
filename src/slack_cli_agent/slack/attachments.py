"""AttachmentStore — 첨부 파일 저장.

원본 `save_attachments`, `cleanup_attach` 를 재구성했다. 슬랙 이벤트는
파일을 본문에 담아 주지 않고 주소만 준다. 내려받아 로컬에 두지 않으면
모델은 그림을 보지 못한 채 답한다.

실제 다운로드(HTTP 요청)는 `downloader` 로 주입받는다 — 단위 시험이 실제
네트워크를 부르지 않는다. 권한 없는 다운로드가 200 과 함께 HTML 로그인
화면을 주는 경우가 있어(원본 실측), `Content-Type` 을 확인해 걸러낸다.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
    """메시지에 붙은 파일을 내려받아 저장하고, 오래된 것은 지운다."""

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
        """메시지에 붙은 파일을 내려받고 그 결과 목록을 돌려준다."""
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
            except Exception:
                continue
            if "text/html" in (result.content_type or ""):
                # 권한이 없으면 슬랙이 로그인 화면을 준다. 200 이라 성공처럼 보인다.
                continue
            if len(result.data) > self._max_bytes:
                continue
            where.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(result.data)
            saved.append(
                SavedAttachment(
                    path=str(dest),
                    name=f.get("name") or name,
                    kind=f.get("mimetype") or result.content_type,
                )
            )
        return saved

    def cleanup(self, now: float | None = None) -> None:
        """오래된 첨부를 지운다. 받아 놓고 쌓아 두지 않는다."""
        cut = (now if now is not None else time.time()) - self._keep_hours * 3600
        try:
            for p in self._attach_dir.glob("*/*"):
                if p.is_file() and p.stat().st_mtime < cut:
                    p.unlink()
            for d in self._attach_dir.glob("*"):
                if d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
        except OSError:
            pass
