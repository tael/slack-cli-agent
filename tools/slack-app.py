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
    tools/slack-app.py diff    <ws> <app_id> <매니페스트파일>
    tools/slack-app.py register <ws> <app_id> <봇이름> <매니페스트파일>
    tools/slack-app.py audit   [<ws>]
    tools/slack-app.py discover <ws> <봇이름> <매니페스트파일>

diff 는 저장소 정본(slack-apps/<이름>.json)과 슬랙에 실제로 올라간 설정을
대조한다. 정본을 고치고 반영을 빠뜨리면 시험은 전부 통과하는데 봇은 옛
설정으로 돈다 - sca-1v7 에서 세 봇의 DM 이 그렇게 꺼져 있었다. 부팅 경로인
preflight 에는 넣지 않는다. 슬랙 장애가 봇 기동을 막으면 안 된다.
차이가 있으면 종료코드 1 이다.

register 는 그 앱을 운영 레지스트리(~/.slack-app-config/registry.json)에
적는다. audit 은 그 레지스트리 전체를 돌며 같은 대조를 하고, 차이나 조회
실패가 하나라도 있으면 종료코드 1 이다. 그래야 launchd 일일 감사가 그것을
읽는 계기가 된다 - diff 만 있고 도는 사람이 없으면 값을 남기고 아무도 안
읽는 것과 같다(sca-4eo). ws 를 주면 그 워크스페이스만 본다.

discover 는 app ID 를 모르는 봇을 위한 것이다. 그 봇의 상태 디렉터리에서
봇 토큰을 읽어 auth.test 와 bots.info 로 app ID 를 되찾아 등록한다.

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

# 대조 규칙은 패키지 쪽에 둔다. 단위 시험이 보는 것과 이 명령이 쓰는 것이
# 같은 코드여야 한 쪽만 고쳐 어긋나지 않는다.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from slack_cli_agent.slack.app_registry import AppEntry, AppRegistry, audit, read_bot_token
from slack_cli_agent.slack.manifest import AppManifest, ManifestDiff

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


def _differences(ws: str, app_id: str, manifest: Path) -> list[str]:
    out = _post("apps.manifest.export", {"app_id": app_id}, rotate(ws))
    if not out.get("ok"):
        raise RuntimeError(str(out.get("error")))
    return list(ManifestDiff().differences(AppManifest.from_path(manifest), AppManifest(out["manifest"])))


def _audit(only_workspace: str = "") -> None:
    entries = [
        entry for entry in AppRegistry().entries()
        if not only_workspace or entry.workspace == only_workspace
    ]
    if not entries:
        registry = AppRegistry()
        if registry.is_configured():
            # 파일이 있는데 0건이면 부재가 아니라 사고다. 종료코드 0 으로 끝내면
            # 매일 도는 감사가 이상 없음과 같은 모습으로 지나간다 (sca-hr8).
            print(f"레지스트리에 읽을 항목이 없다 : {registry.path}")
            raise SystemExit(1)
        print("레지스트리에 등록된 앱이 없다. register 로 먼저 적는다")
        raise SystemExit(0)
    findings = audit(entries, lambda e: _differences(e.workspace, e.app_id, e.manifest))
    for finding in findings:
        if finding.error:
            print(f"{finding.name}: 조회 실패 - {finding.error}")
            continue
        print(f"{finding.name}: 차이 {len(finding.differences)}건")
        for line in finding.differences:
            print(f"  {line}")
    print(f"앱 {len(entries)}개 중 이상 {len(findings)}건")
    raise SystemExit(1 if findings else 0)


def _get(method: str, params: dict[str, str], token: str) -> dict:
    url = API + method + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as res:
        return json.loads(res.read().decode())


def _app_id_of(name: str) -> str:
    """도는 봇 자신의 토큰으로 app ID 를 되찾는다. auth.test 는 bot_id 만
    주므로 bots.info 를 한 번 더 부른다."""
    token = read_bot_token(Path.home() / f".{name}")
    if not token:
        raise SystemExit(f"{name} 의 봇 토큰을 찾지 못했다: ~/.{name}")
    who = _get("auth.test", {}, token)
    if not who.get("ok"):
        raise SystemExit(f"auth.test 실패: {who.get('error')}")
    info = _get("bots.info", {"bot": str(who.get("bot_id", ""))}, token)
    if not info.get("ok"):
        raise SystemExit(f"bots.info 실패: {info.get('error')}")
    return str((info.get("bot") or {}).get("app_id", ""))


def main(argv: list[str]) -> None:
    if len(argv) >= 2 and argv[1] == "audit":
        _audit(argv[2] if len(argv) > 2 else "")
        return
    if len(argv) < 3:
        print(__doc__)
        raise SystemExit(2)
    cmd, ws = argv[1], argv[2]

    if cmd == "discover":
        app_id = _app_id_of(argv[3])
        AppRegistry().register(AppEntry(argv[3], ws, app_id, Path(argv[4]).resolve()))
        print(f"{argv[3]} app_id={app_id} 를 레지스트리에 적었다")
        return
    if cmd == "register":
        AppRegistry().register(AppEntry(argv[4], ws, argv[3], Path(argv[5]).resolve()))
        print(f"{argv[4]} 를 레지스트리에 적었다")
        return
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
    if cmd == "diff":
        out = _post("apps.manifest.export", {"app_id": argv[3]}, token)
        if not out.get("ok"):
            _fail(out)
        local = AppManifest.from_path(Path(argv[4]))
        차이 = ManifestDiff().differences(local, AppManifest(out["manifest"]))
        for line in 차이:
            print(line)
        print("정본과 같다" if not 차이 else f"차이 {len(차이)}건")
        raise SystemExit(1 if 차이 else 0)
    if cmd == "update":
        manifest = Path(argv[4]).read_text(encoding="utf-8")
        out = _post("apps.manifest.update", {"app_id": argv[3], "manifest": manifest}, token)
        if not out.get("ok"):
            _fail(out)
        print("갱신했다")
        # 반영 확인을 여기서 한다. "갱신했다" 는 요청이 받아들여졌다는 뜻일
        # 뿐이고, 슬랙이 일부 키를 버려도 ok 로 돌아온다.
        차이 = ManifestDiff().differences(
            AppManifest.from_path(Path(argv[4])),
            AppManifest(_post("apps.manifest.export", {"app_id": argv[3]}, token)["manifest"]),
        )
        for line in 차이:
            print(f"  반영 안 됨: {line}")
        if 차이:
            raise SystemExit(1)
        print("정본과 같다")
        return
    raise SystemExit(f"모르는 명령: {cmd}")


if __name__ == "__main__":
    main(sys.argv)
