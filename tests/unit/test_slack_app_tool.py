"""tools/slack-app.py 의 multipart 본문 조립.

앱 아이콘은 apps.icon.set 에 파일로 올린다. 표준 라이브러리만 쓰므로 본문을
직접 만든다 - 경계 문자열과 줄바꿈이 틀리면 슬랙이 missing_arguments 를 낸다.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "slack_app_tool", Path(__file__).resolve().parents[2] / "tools" / "slack-app.py"
)
assert _SPEC and _SPEC.loader
slack_app = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(slack_app)


class Test멀티파트_본문:
    def test_필드와_파일을_담는다(self, tmp_path: Path) -> None:
        png = tmp_path / "icon.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + "바이트".encode())

        body, content_type = slack_app.multipart_body({"app_id": "A1"}, "file", png)

        assert content_type.startswith("multipart/form-data; boundary=")
        boundary = content_type.split("boundary=")[1]
        assert boundary.encode() in body
        assert b'name="app_id"' in body
        assert b"A1" in body
        assert b'name="file"; filename="icon.png"' in body
        assert b"\x89PNG" in body

    def test_본문이_마지막_경계로_끝난다(self, tmp_path: Path) -> None:
        png = tmp_path / "icon.png"
        png.write_bytes(b"x")

        body, content_type = slack_app.multipart_body({}, "file", png)
        boundary = content_type.split("boundary=")[1]

        assert body.endswith(f"--{boundary}--\r\n".encode())

    def test_줄바꿈은_CRLF_다(self, tmp_path: Path) -> None:
        """LF 만 쓰면 슬랙이 본문을 못 읽는다."""
        png = tmp_path / "icon.png"
        png.write_bytes(b"x")

        body, _ = slack_app.multipart_body({"app_id": "A1"}, "file", png)

        assert b'name="app_id"\r\n\r\nA1\r\n' in body
