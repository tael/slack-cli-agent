#!/usr/bin/env python3
"""Slack 앱을 설정 토큰으로 다룬다. 브라우저 없이 되는 것만 여기서 한다.

토큰은 ~/.slack-app-config/<워크스페이스>.{access,refresh} 에 둔다.
access 토큰은 12시간이면 만료되므로 매 호출 전에 자동으로 갱신한다.

    tools/slack-app.py rotate   <ws>
    tools/slack-app.py validate <ws> <매니페스트파일>
    tools/slack-app.py create   <ws> <매니페스트파일>
    tools/slack-app.py get     <ws> <app_id>
    tools/slack-app.py icon    <ws> <app_id> <PNG파일>
    tools/slack-app.py update  <ws> <app_id> <매니페스트파일>
    tools/slack-app.py list    <ws>

브라우저가 필요한 것은 두 가지뿐이다 - 워크스페이스 설치 승인과 앱 수준
토큰(xapp) 발급. 아이콘은 apps.icon.set 으로 올라간다(512px 에서 2000px
정사각). 나머지는 전부 이 스크립트로 된다.
"""

from __future__ import annotations

import json
import mimetypes
import secrets
import sys
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path.home() / ".slack-app-config"
API = "https://slack.com/api/"


def _post(method: str, data: dict[str, str], token: str | None = None) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(API + method, data=body)
    req.add_header("Content-type", "application/x-www-form-urlencoded")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as res:
        return json.loads(res.read().decode())


def multipart_body(fields: dict[str, str], file_field: str, path: Path) -> tuple[bytes, str]:
    """multipart/form-data 본문을 만든다. 줄바꿈은 CRLF 여야 한다."""
    boundary = secrets.token_hex(16)
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode()
        )
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}";'
        f' filename="{path.name}"\r\nContent-Type: {mime}\r\n\r\n'.encode()
    )
    parts.append(path.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _post_file(method: str, fields: dict[str, str], path: Path, token: str) -> dict:
    body, content_type = multipart_body(fields, "file", path)
    req = urllib.request.Request(API + method, data=body)
    req.add_header("Content-type", content_type)
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=60) as res:
        return json.loads(res.read().decode())


def rotate(ws: str) -> str:
    """Rotates and stores both tokens. Slack invalidates the old refresh
    token on each rotation, so the new one must be written back."""
    refresh = (BASE / f"{ws}.refresh").read_text().strip()
    out = _post("tooling.tokens.rotate", {"refresh_token": refresh})
    if not out.get("ok"):
        raise SystemExit(f"토큰 갱신 실패: {out.get('error')}")
    (BASE / f"{ws}.access").write_text(out["token"])
    (BASE / f"{ws}.refresh").write_text(out["refresh_token"])
    (BASE / f"{ws}.access").chmod(0o600)
    (BASE / f"{ws}.refresh").chmod(0o600)
    return out["token"]


def _fail(out: dict) -> None:
    errors = out.get("errors")
    detail = json.dumps(errors, ensure_ascii=False) if errors else out.get("error")
    raise SystemExit(f"실패: {detail}")


def main(argv: list[str]) -> None:
    if len(argv) < 3:
        print(__doc__)
        raise SystemExit(2)
    cmd, ws = argv[1], argv[2]

    if cmd == "rotate":
        print(rotate(ws)[:12] + "…  갱신했다")
        return

    token = rotate(ws)
    if cmd == "list":
        out = _post("apps.manifest.export", {"app_id": argv[3]}, token) if len(argv) > 3 else None
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return
    if cmd == "validate":
        manifest = Path(argv[3]).read_text(encoding="utf-8")
        out = _post("apps.manifest.validate", {"manifest": manifest}, token)
        if not out.get("ok"):
            _fail(out)
        print("매니페스트 이상 없다")
        return
    if cmd == "create":
        manifest = Path(argv[3]).read_text(encoding="utf-8")
        out = _post("apps.manifest.create", {"manifest": manifest}, token)
        if not out.get("ok"):
            _fail(out)
        print(out["app_id"])
        return
    if cmd == "get":
        out = _post("apps.manifest.export", {"app_id": argv[3]}, token)
        if not out.get("ok"):
            _fail(out)
        print(json.dumps(out["manifest"], ensure_ascii=False, indent=2))
        return
    if cmd == "icon":
        out = _post_file("apps.icon.set", {"app_id": argv[3]}, Path(argv[4]), token)
        if not out.get("ok"):
            _fail(out)
        print("아이콘을 올렸다")
        return
    if cmd == "update":
        manifest = Path(argv[4]).read_text(encoding="utf-8")
        out = _post("apps.manifest.update", {"app_id": argv[3], "manifest": manifest}, token)
        if not out.get("ok"):
            _fail(out)
        print("갱신했다")
        return
    raise SystemExit(f"모르는 명령: {cmd}")


if __name__ == "__main__":
    main(sys.argv)
