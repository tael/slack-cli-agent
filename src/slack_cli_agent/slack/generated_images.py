"""Posts the pictures Codex made this turn but never posted (D2).

Codex's built-in image generation writes a file under
CODEX_HOME/generated_images/<thread_id>/ and nothing else. Posting it is up to
the model calling image_post_from_file, and it doesn't always: 2026-10-25 the
same request posted at 21:58 and at 22:40 only answered "방 이미지예요" with
the picture left on disk. Instructions couldn't make the call reliable, so the
code uploads it. Ported from mametchi bot.py new_generated_images and
generated_image_autopost.

The upload lands in the thread as a file message of its own. The pipeline then
hands the answer to the tool post merger, which writes it over that message,
so one reply holds both the picture and the answer, as it does when the model
posts the file itself. Slack shares the file to the thread after
files.completeUploadExternal returns, so the message is not yet in
conversations_replies when the pipeline merges a moment later. upload therefore
waits on files.info until the share shows up and hands back its ts, and the
merger writes into that ts directly. Mametchi avoided the gap by sending the
answer as initial_comment; that path can't carry the rich blocks the answer
is posted with here.

A resumed thread keeps the same folder, so only files modified since the turn
started count. Only codex turns are read: other engines have no such folder.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..core.unposted_render import succeeded_tool_names
from ..engine.base import EngineResponse
from ..observability.audit import IncidentKind

log = logging.getLogger(__name__)

GEN_IMAGE_EXTS: tuple[str, ...] = (".png", ".jpg", ".jpeg", ".webp")

#: A turn that called one of these already put a file in the thread.
FILE_POST_TOOLS: tuple[str, ...] = (
    "mermaid_render_and_post", "image_post_from_file", "image_post_from_url", "csv_post_from_sql",
)

#: File mtime and the turn clock are both local, but the turn clock is read
#: before the engine starts; a small slack keeps an early file from being missed.
CLOCK_SKEW_SEC = 2.0

NOTICE_IMAGE_UPLOAD_FAILED = "그림은 만들었지만 이 스레드에 올리지 못했습니다."

#: How long upload waits for Slack to share the file into the thread. The
#: share usually shows within a second; past this the merger falls back to
#: reading the thread.
SHARE_WAIT_TRIES = 10
SHARE_WAIT_SEC = 0.5


@dataclass(frozen=True)
class ImageUpload:
    ok: bool
    #: ts of the file message in the thread; None when Slack had not shared
    #: it yet when the wait ran out.
    ts: str | None = None


def share_ts(file_obj: Mapping[str, Any], channel: str, thread_ts: str | None) -> str | None:
    """ts of the message that shared ``file_obj`` into ``channel``/``thread_ts``."""
    shares = file_obj.get("shares") or {}
    for scope in ("public", "private"):
        for share in (shares.get(scope) or {}).get(channel) or []:
            if thread_ts is None or str(share.get("thread_ts") or "") == thread_ts:
                ts = str(share.get("ts") or "")
                if ts:
                    return ts
    return None


def new_generated_images(codex_home: Path | None, thread_id: str, since: float) -> list[Path]:
    """Images Codex wrote for ``thread_id`` at or after ``since``, oldest first."""
    if codex_home is None or not thread_id:
        return []
    d = Path(codex_home) / "generated_images" / str(thread_id)
    if not d.is_dir():
        return []
    files = [
        f for f in d.iterdir()
        if f.is_file() and f.suffix.lower() in GEN_IMAGE_EXTS and f.stat().st_mtime >= since
    ]
    return sorted(files, key=lambda f: f.stat().st_mtime)


@runtime_checkable
class GeneratedImagePoster(Protocol):
    def pending(self, since: float, responses: Sequence[EngineResponse]) -> list[Path]:
        """Pictures this turn made and did not post; [] when there are none."""
        ...

    def upload(self, channel: str, thread_ts: str | None, images: Sequence[Path]) -> ImageUpload:
        """ok when Slack took them, with the file message ts once it is shared."""
        ...


class SlackGeneratedImagePoster:
    def __init__(
        self,
        client: Any,
        codex_home_for: Callable[[str], Path | None],
        audit: Callable[..., None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        share_wait_tries: int = SHARE_WAIT_TRIES,
    ) -> None:
        # codex_home_for maps the engine name that answered to its CODEX_HOME,
        # None for a non-codex engine. A fallback codex turn runs under the
        # fallback slot's home, not the primary's.
        self._client = client
        self._codex_home_for = codex_home_for
        self._audit = audit or (lambda **_: None)
        self._sleep = sleep
        self._share_wait_tries = share_wait_tries

    def pending(self, since: float, responses: Sequence[EngineResponse]) -> list[Path]:
        names = [n for r in responses for n in succeeded_tool_names(r)]
        if any(n in FILE_POST_TOOLS for n in names):
            return []
        found: list[Path] = []
        seen: set[Path] = set()
        for resp in responses:
            if not resp.ok or not resp.session_id:
                continue
            home = self._codex_home_for(resp.engine or "")
            for f in new_generated_images(home, resp.session_id, since - CLOCK_SKEW_SEC):
                if f not in seen:
                    seen.add(f)
                    found.append(f)
        return found

    def upload(self, channel: str, thread_ts: str | None, images: Sequence[Path]) -> ImageUpload:
        kwargs: dict[str, Any] = {
            "channel": channel,
            "file_uploads": [{"file": str(f), "filename": f.name, "title": f.name} for f in images],
        }
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        try:
            res = self._client.files_upload_v2(**kwargs)
        except Exception as exc:  # noqa: BLE001 - a failed upload is told in the body, not raised
            log.warning("만든 그림을 올리지 못했다 channel=%s ts=%s : %s", channel, thread_ts, exc)
            self._audit(
                kind=IncidentKind.GENERATED_IMAGE_AUTOPOST_FAILED.value, channel=channel,
                thread_ts=thread_ts or "", error=str(exc)[:300],
            )
            return ImageUpload(ok=False)
        files = (res or {}).get("files") or []
        file_id = str(files[0].get("id") or "") if files else ""
        ts = self._wait_for_share(file_id, channel, thread_ts)
        log.info("만들고 올리지 않은 그림 %d장을 코드가 올렸다 : %s", len(images), thread_ts)
        self._audit(
            kind=IncidentKind.GENERATED_IMAGE_AUTOPOST.value, channel=channel,
            thread_ts=thread_ts or "", files=[f.name for f in images], target_ts=ts or "",
        )
        return ImageUpload(ok=True, ts=ts)

    def _wait_for_share(self, file_id: str, channel: str, thread_ts: str | None) -> str | None:
        # The files go up in one call and share as one message, so the first
        # file's share is the message the answer belongs in.
        if not file_id:
            return None
        for attempt in range(self._share_wait_tries):
            if attempt:
                self._sleep(SHARE_WAIT_SEC)
            try:
                info = self._client.files_info(file=file_id)
            except Exception as exc:  # noqa: BLE001 - unknown ts means the merger reads the thread
                log.warning("올린 그림의 게시 위치를 확인하지 못했다 file=%s : %s", file_id, exc)
                return None
            ts = share_ts((info or {}).get("file") or {}, channel, thread_ts)
            if ts:
                return ts
        log.warning("올린 그림이 %d회 확인 동안 스레드에 공유되지 않았다 file=%s",
                    self._share_wait_tries, file_id)
        return None
