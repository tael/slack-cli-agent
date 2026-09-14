"""HttpDownloader — 슬랙 비공개 파일을 실제로 내려받는다.

`AttachmentStore` 의 downloader 자리에 들어가는 구현체다. 지금까지 그
자리는 단위 시험에서만 가짜로 채워져 있었고, 실제 파일을 받는 구현이
없어서 조립 계층이 첨부 저장을 연결하지 못했다.

슬랙 비공개 파일은 봇 토큰을 `Authorization: Bearer <token>` 헤더로 붙여야
받을 수 있다(원본 `bot.py` 의 `save_attachments` 참조).
"""

from __future__ import annotations

import urllib.request
from collections.abc import Callable
from typing import Any

from .attachments import DownloadResult


class HttpDownloader:
    """슬랙 비공개 파일을 내려받는다. AttachmentStore 의 downloader 자리에 들어간다."""

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
        # 토큰은 호출마다 다시 얻는다. 생성 시점에 한 번만 읽으면 토큰이
        # 갱신돼도 옛 값을 계속 쓰게 된다.
        token = self._token_provider()
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        # 전송 예외는 여기서 삼키지 않고 그대로 올린다. 파일 하나의 실패를
        # 다루는 자리는 AttachmentStore.save 이고, 그쪽이 이미 downloader
        # 호출을 개별 try/except 로 감싸 실패한 파일만 건너뛴다. 여기서
        # 다시 삼키면 그 판단을 이중으로 하는 것이고, 예외 메시지를 새로
        # 만들다 토큰을 실수로 섞어 넣을 위험만 늘어난다.
        with self._opener(request, timeout=self._timeout) as response:
            data = response.read()
            raw_content_type = response.headers.get("Content-Type") or ""
        content_type = raw_content_type.split(";", 1)[0].strip().lower()
        return DownloadResult(content_type=content_type, data=data)
