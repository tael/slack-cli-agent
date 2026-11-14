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


class Test봇_이름_복구:
    """앱 설정 변경이 봇 사용자 실명을 영문으로 덮는 것을 되돌린다 (2026-11-10 구치파치)."""

    def test_앱_ID_로_봇_사용자를_고른다(self) -> None:
        members = [
            {"id": "U1", "is_bot": True, "profile": {"api_app_id": "A_OTHER"}},
            {"id": "U2", "is_bot": False, "profile": {"api_app_id": "A1"}},
            {"id": "U3", "is_bot": True, "deleted": True, "profile": {"api_app_id": "A1"}},
            {"id": "U4", "is_bot": True, "profile": {"api_app_id": "A1"}},
        ]
        assert slack_app.bot_user_of(members, "A1")["id"] == "U4"
        assert slack_app.bot_user_of(members, "A9") is None

    def test_영문으로_덮였으면_복구_대상이다(self) -> None:
        덮임 = {"profile": {"real_name": "guchipachi", "display_name": ""}}
        정상 = {"profile": {"real_name": "구치파치", "display_name": "구치파치"}}
        assert slack_app.needs_name_restore(덮임, "구치파치")
        assert not slack_app.needs_name_restore(정상, "구치파치")
