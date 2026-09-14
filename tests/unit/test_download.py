"""HttpDownloader 단위 시험.

실제 네트워크를 부르지 않는다. `opener` 자리에 가짜를 넣어 확인한다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Self

import pytest

from slack_cli_agent.slack.attachments import AttachmentStore
from slack_cli_agent.slack.download import HttpDownloader


class _FakeResponse:
    """urlopen 이 돌려주는 컨텍스트 매니저를 흉내낸다."""

    def __init__(self, data: bytes, headers: dict[str, str] | None = None) -> None:
        self._data = data
        self.headers = headers or {}

    def read(self) -> bytes:
        return self._data

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


class _RecordingOpener:
    """opener 호출 인자를 기록해 검증에 쓴다."""

    def __init__(self, response: _FakeResponse | None = None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.calls: list[dict[str, Any]] = []

    def __call__(self, request: Any, timeout: float | None = None) -> _FakeResponse:
        self.calls.append({"request": request, "timeout": timeout})
        if self._error is not None:
            raise self._error
        assert self._response is not None
        return self._response


def test_인증_헤더에_토큰이_붙는다() -> None:
    opener = _RecordingOpener(_FakeResponse(b"data"))
    downloader = HttpDownloader(token_provider=lambda: "xoxb-token1", opener=opener)

    downloader("https://slack/files/x")

    request = opener.calls[0]["request"]
    assert request.get_header("Authorization") == "Bearer xoxb-token1"


def test_토큰을_매_호출마다_다시_얻는다() -> None:
    tokens = iter(["old-token", "new-token"])
    opener = _RecordingOpener(_FakeResponse(b"data"))
    downloader = HttpDownloader(token_provider=lambda: next(tokens), opener=opener)

    downloader("https://slack/files/x")
    downloader("https://slack/files/x")

    first, second = opener.calls
    assert first["request"].get_header("Authorization") == "Bearer old-token"
    assert second["request"].get_header("Authorization") == "Bearer new-token"


def test_본문_bytes가_그대로_담긴다() -> None:
    opener = _RecordingOpener(_FakeResponse(b"binarydata"))
    downloader = HttpDownloader(token_provider=lambda: "t", opener=opener)

    result = downloader("https://slack/files/x")

    assert result.data == b"binarydata"


def test_content_type이_담긴다() -> None:
    opener = _RecordingOpener(_FakeResponse(b"x", headers={"Content-Type": "image/png"}))
    downloader = HttpDownloader(token_provider=lambda: "t", opener=opener)

    result = downloader("https://slack/files/x")

    assert result.content_type == "image/png"


def test_파라미터가_붙은_content_type은_앞_토막만_남는다() -> None:
    opener = _RecordingOpener(_FakeResponse(b"x", headers={"Content-Type": "text/html; charset=utf-8"}))
    downloader = HttpDownloader(token_provider=lambda: "t", opener=opener)

    result = downloader("https://slack/files/x")

    assert result.content_type == "text/html"


def test_대문자가_섞인_content_type이_소문자로_정규화된다() -> None:
    opener = _RecordingOpener(_FakeResponse(b"x", headers={"Content-Type": "IMAGE/PNG"}))
    downloader = HttpDownloader(token_provider=lambda: "t", opener=opener)

    result = downloader("https://slack/files/x")

    assert result.content_type == "image/png"


def test_content_type_헤더가_없으면_빈_문자열이다() -> None:
    opener = _RecordingOpener(_FakeResponse(b"x", headers={}))
    downloader = HttpDownloader(token_provider=lambda: "t", opener=opener)

    result = downloader("https://slack/files/x")

    assert result.content_type == ""


def test_timeout이_opener에_넘어간다() -> None:
    opener = _RecordingOpener(_FakeResponse(b"x"))
    downloader = HttpDownloader(token_provider=lambda: "t", opener=opener, timeout=5.0)

    downloader("https://slack/files/x")

    assert opener.calls[0]["timeout"] == 5.0


def test_기본_timeout은_30초다() -> None:
    opener = _RecordingOpener(_FakeResponse(b"x"))
    downloader = HttpDownloader(token_provider=lambda: "t", opener=opener)

    downloader("https://slack/files/x")

    assert opener.calls[0]["timeout"] == 30.0


def test_전송_예외는_그대로_올라간다() -> None:
    opener = _RecordingOpener(error=ConnectionError("network broken"))
    downloader = HttpDownloader(token_provider=lambda: "t", opener=opener)

    with pytest.raises(ConnectionError):
        downloader("https://slack/files/x")


def test_예외_메시지에_토큰이_없다() -> None:
    opener = _RecordingOpener(error=ConnectionError("network broken"))
    downloader = HttpDownloader(token_provider=lambda: "super-secret-token", opener=opener)

    with pytest.raises(ConnectionError) as exc_info:
        downloader("https://slack/files/x")

    assert "super-secret-token" not in str(exc_info.value)


def test_AttachmentStore가_실제로_파일을_저장한다(tmp_path: Path) -> None:
    """HttpDownloader 를 AttachmentStore 의 downloader 자리에 연결해 저장까지 확인한다.

    AttachmentStore.downloader 타입은 (url, token) 두 인자를 받는다. HttpDownloader
    는 token_provider 로 자기 토큰을 직접 얻으므로, 실제 조립 코드가 그렇게 하듯
    url 만 넘기는 얇은 어댑터로 감싼다.
    """
    opener = _RecordingOpener(_FakeResponse(b"pngdata", headers={"Content-Type": "image/png"}))
    downloader = HttpDownloader(token_provider=lambda: "xoxb-token", opener=opener)

    store = AttachmentStore(
        attach_dir=tmp_path / "attach",
        token_provider=lambda: "xoxb-token",
        downloader=lambda url, token: downloader(url),
    )
    event = {
        "ts": "1700000000.0",
        "files": [{"name": "photo.png", "url_private_download": "https://slack/x", "size": 10}],
    }

    saved = store.save(event)

    assert len(saved) == 1
    assert Path(saved[0].path).read_bytes() == b"pngdata"
