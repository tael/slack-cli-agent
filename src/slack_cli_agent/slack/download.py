"""Downloads Slack's private files using a Bearer token header."""

from __future__ import annotations

import urllib.request
from collections.abc import Callable
from typing import Any

from .attachments import DownloadResult


class HttpDownloader:
    def __init__(
        self,
        token_provider: Callable[[], str],
        opener: Callable[..., Any] | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._token_provider = token_provider
        self._opener = opener if opener is not None else urllib.request.urlopen
        self._timeout = timeout

    def __call__(self, url: str) -> DownloadResult:
        # Fetch the token on every call. Reading it once at construction
        # would keep using a stale value after rotation.
        token = self._token_provider()
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        # Let transport exceptions propagate. AttachmentStore.save() already
        # wraps each downloader call in its own try/except and skips just
        # that file, so catching here too would duplicate that decision and
        # only add risk of leaking the token into a new error message.
        with self._opener(request, timeout=self._timeout) as response:
            data = response.read()
            raw_content_type = response.headers.get("Content-Type") or ""
        content_type = raw_content_type.split(";", 1)[0].strip().lower()
        return DownloadResult(content_type=content_type, data=data)
