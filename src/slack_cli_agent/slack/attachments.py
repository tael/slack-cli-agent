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
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .permalinks import parse_slack_links
from .transcribe import TranscribeFn, is_audio

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
        transcriber: TranscribeFn | None = None,
    ) -> None:
        self._attach_dir = attach_dir
        self._token_provider = token_provider
        self._downloader = downloader
        self._max_files = max_files
        self._max_bytes = max_bytes
        self._keep_hours = keep_hours
        self._transcriber = transcriber

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
            if is_audio(item.kind):
                self._transcribe_into(original, item.path)
            merged.append(original)
        return tuple(merged), missed

    def _transcribe_into(self, file: dict[str, Any], path: str) -> None:
        """음성 첨부를 받아써 `transcript` 또는 `transcript_error` 를 붙인다.

        모델은 소리를 듣지 못하므로 받아쓰지 않으면 경로만 보고 내용을
        짐작한다. 실패도 사유를 그대로 실어 프롬프트가 실패를 밝히게 한다(G6).
        """
        if self._transcriber is None:
            file["transcript_error"] = "받아쓰기 엔진이 설정되지 않았다"
            return
        t0 = time.time()
        try:
            words, why = self._transcriber(path)
        except Exception as exc:  # noqa: BLE001 - 한 클립 실패가 요청 전체를 막지 않는다
            words, why = None, f"받아쓰기를 실행하지 못했다 : {exc}"
        log.info("음성을 받아썼다 : %s %.1f초 %s", file.get("name"),
                 time.time() - t0, "성공" if words else why)
        if words:
            file["transcript"] = words
        else:
            file["transcript_error"] = why or "받아쓰기 실패"

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


# ---------------------------------------------------------------------------
# attachments 본문 재구성 — slack 조회 MCP 서버(mcp/slack_readonly.py)와
# LinkedThreadReader 의 실시간 조회 실패 폴백이 함께 쓴다(gaps.md C39/C40).
#
# Slack 의 `attachments` 필드는 두 가지 전혀 다른 용도로 쓰인다.
#
# 1. 사람이 링크를 붙여넣으면 슬랙이 그 메시지를 미리보기로 "펼친다"(unfurl).
#    `is_msg_unfurl` 또는 `from_url` 이 있고, 그 안의 `text`/`fallback` 은
#    "링크가 가리키는 메시지"의 본문이다.
# 2. 알림 앱(Grafana 등)은 최상위 `text` 를 비워 두고 본문 전체를
#    `attachments` 의 pretext/title/text/fields 에 싣는다. 2026-11-03
#    모니터링-부릉-내부 grafana 알림이 이 형태였는데 text 만 보고 "본문 없음"
#    으로 읽었다.
#
# 둘을 섞으면 알림 본문에 "링크 미리보기" 라벨이 붙거나, 링크 미리보기가
# 알림인 것처럼 섞여 나온다 — 그래서 함수를 나눈다.
# ---------------------------------------------------------------------------


def is_unfurl_attachment(attachment: Mapping[str, Any]) -> bool:
    """링크를 붙여넣어 슬랙이 직접 펼친 미리보기인가.

    알림 앱의 경보성 첨부는 이 둘을 달지 않는다 — 참이면 그 메시지가
    "링크 미리보기" 쪽, 거짓이면 "경보 본문" 쪽이다.
    """
    return bool(attachment.get("is_msg_unfurl") or attachment.get("from_url"))


def unfurled_messages(
    attachments: Iterable[Mapping[str, Any]] | None, limit: int = 2000
) -> tuple[dict[str, Any], ...]:
    """링크 미리보기 첨부에서 그 링크가 가리키는 메시지의 본문을 되살린다.

    읽을 수 없는 채널의 링크라도 그걸 붙인 사람의 메시지에는 슬랙이 이미
    펼친 본문이 실려 있다. 2026-10-29 sandbox-rnd 에서 text 만 넘겨 허지우님
    요청 본문이 통째로 빠진 사례가 기준이다.
    """
    out: list[dict[str, Any]] = []
    for a in attachments or ():
        if not is_unfurl_attachment(a):
            continue
        body = a.get("text") or a.get("fallback") or ""
        if not body:
            continue
        out.append({
            "author": a.get("author_subname") or a.get("author_name") or "",
            "ts": a.get("ts") or "",
            "from_url": a.get("from_url") or "",
            "text": body[:limit],
        })
    return tuple(out)


def attachment_body_texts(
    attachments: Iterable[Mapping[str, Any]] | None, limit: int = 12000
) -> tuple[str, ...]:
    """경보성(비(非) 언퍼얼) 첨부에서 본문을 되살린다.

    알림 앱은 최상위 text 를 비우고 본문 전체를 여기에 싣는다. 비워 두면
    "본문이 없다" 고 잘못 읽는다(2026-11-03 grafana 사례).
    """
    out: list[str] = []
    for a in attachments or ():
        if is_unfurl_attachment(a):
            continue
        parts = [a.get("pretext"), a.get("title"), a.get("text")]
        parts += [f"{f.get('title', '')}: {f.get('value', '')}" for f in a.get("fields") or ()]
        body = "\n".join(p for p in parts if p) or a.get("fallback") or ""
        if body:
            out.append(body[:limit])
    return tuple(out)


def file_metadata(files: Iterable[Mapping[str, Any]] | None) -> tuple[dict[str, Any], ...]:
    """첨부 파일의 존재를 알리는 최소 정보만 추린다.

    내용은 여기서 받지 않는다 — files 가 있다는 사실 자체를 빼면 본문에
    글자가 없는 것을 내용이 없는 것으로 읽는다(2026-10-22 식단표 이미지 사례).
    """
    return tuple(
        {"id": f.get("id"), "name": f.get("name"), "mimetype": f.get("mimetype")}
        for f in files or ()
    )


def unfurl_matches(attachment: Mapping[str, Any], channel: str, thread_ts: str) -> bool:
    """이 언퍼얼 첨부가 정확히 이 링크(channel+thread_ts)의 것인가.

    `from_url` 에 박힌 permalink 로 대조한다. 문자열 동등 비교가 아니라
    `parse_slack_links` 로 같은 규칙을 한 번 더 태워, 쿼리 문자열의
    thread_ts 유무 같은 표기 차이에 흔들리지 않게 한다. 실시간 스레드 조회가
    실패한 링크를 이 첨부로 복원하는 LinkedThreadReader 의 폴백이 쓴다
    (gaps.md C39).
    """
    if not is_unfurl_attachment(attachment):
        return False
    found = parse_slack_links(attachment.get("from_url"))
    return bool(found) and found[0].channel == channel and found[0].thread_ts == thread_ts
