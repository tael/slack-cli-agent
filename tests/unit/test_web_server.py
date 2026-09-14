"""웹 콘솔 HTTP 서버.

실제 소켓을 열되 포트 0 으로 띄워 시험끼리 포트를 다투지 않는다.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import quote

import pytest

from slack_cli_agent.web.api import ApiResponse, ApiRouter
from slack_cli_agent.web.server import WebServer


class FakeProfiles:
    def names(self) -> list[str]:
        return ["mametchi"]

    def read(self, name: str) -> dict[str, object]:
        return {"name": name}

    def save(self, name: str, data: Mapping[str, object]) -> list[str]:
        return []


class CountingMetrics:
    def __init__(self) -> None:
        self.calls = 0

    def collect(self, days: int) -> dict[str, object]:
        self.calls += 1
        return {"days": days, "calls": self.calls}


def make_router(metrics: CountingMetrics | None = None) -> ApiRouter:
    metrics = metrics if metrics is not None else CountingMetrics()
    return ApiRouter(
        profiles=FakeProfiles(),
        channels_for=lambda bot: (_ for _ in ()).throw(KeyError(bot)),
        prompts_for=lambda bot: (_ for _ in ()).throw(KeyError(bot)),
        knowledge_for=lambda bot: (_ for _ in ()).throw(KeyError(bot)),
        metrics_for=lambda bot: metrics,
    )


@pytest.fixture
def server():
    srv = WebServer(host="127.0.0.1", port=0, router=make_router())
    srv.start()
    yield srv
    srv.stop()


def get(port: int, path: str) -> tuple[int, bytes]:
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path)
        res = conn.getresponse()
        return res.status, res.read()
    finally:
        conn.close()


class Test바인딩:
    def test_127_0_0_1에만_묶는다(self, server: WebServer) -> None:
        assert server.host == "127.0.0.1"
        assert server.bound_address[0] == "127.0.0.1"

    def test_실제_포트가_배정된다(self, server: WebServer) -> None:
        assert server.port != 0
        assert server.bound_address[1] == server.port


class TestHTTP_1_1:
    def test_연결을_재사용한다(self, server: WebServer) -> None:
        conn = HTTPConnection("127.0.0.1", server.port, timeout=5)
        try:
            conn.request("GET", "/api/health")
            res1 = conn.getresponse()
            body1 = res1.read()
            assert res1.status == 200
            assert res1.version == 11
            # HTTP/1.1 연결이 살아 있으면 같은 소켓으로 두번째 요청이 간다.
            conn.request("GET", "/api/health")
            res2 = conn.getresponse()
            body2 = res2.read()
            assert res2.status == 200
            assert body1 == body2
        finally:
            conn.close()

    def test_모든_응답에_content_length가_있다(self, server: WebServer) -> None:
        conn = HTTPConnection("127.0.0.1", server.port, timeout=5)
        try:
            conn.request("GET", "/api/health")
            res = conn.getresponse()
            res.read()
            assert res.getheader("Content-Length") is not None
        finally:
            conn.close()


class Test정적_파일:
    def test_에셋이_없으면_안내문구_HTML을_낸다(self, server: WebServer) -> None:
        status, body = get(server.port, "/")
        assert status == 200
        assert b"<html" in body.lower()

    def test_에셋이_있으면_그_파일을_낸다(self, tmp_path: Path) -> None:
        assets = tmp_path / "assets"
        assets.mkdir()
        (assets / "index.html").write_text("<html>실제 화면</html>", encoding="utf-8")
        srv = WebServer(host="127.0.0.1", port=0, router=make_router(), assets_dir=assets)
        srv.start()
        try:
            status, body = get(srv.port, "/")
            assert status == 200
            assert "실제 화면".encode() in body
        finally:
            srv.stop()


class TestAPI_위임:
    def test_api_경로는_라우터로_넘긴다(self, server: WebServer) -> None:
        status, body = get(server.port, "/api/profiles")
        assert status == 200
        assert json.loads(body) == ["mametchi"]

    def test_없는_api_경로는_404(self, server: WebServer) -> None:
        status, _ = get(server.port, "/api/" + quote("없는것"))
        assert status == 404

    def test_PUT_본문을_JSON으로_읽어_넘긴다(self, server: WebServer) -> None:
        conn = HTTPConnection("127.0.0.1", server.port, timeout=5)
        try:
            payload = json.dumps({"model": "opus-5"}).encode("utf-8")
            conn.request(
                "PUT",
                "/api/profile/mametchi",
                body=payload,
                headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
            )
            res = conn.getresponse()
            res.read()
            assert res.status == 200
        finally:
            conn.close()

    def test_JSON이_아닌_PUT_본문은_400이다(self, server: WebServer) -> None:
        conn = HTTPConnection("127.0.0.1", server.port, timeout=5)
        try:
            payload = "이것은 JSON 이 아니다".encode("utf-8")
            conn.request(
                "PUT",
                "/api/profile/mametchi",
                body=payload,
                headers={"Content-Type": "text/plain", "Content-Length": str(len(payload))},
            )
            res = conn.getresponse()
            res.read()
            assert res.status == 400
        finally:
            conn.close()


class Test지표_캐시:
    def test_같은_봇과_창_길이면_다시_모으지_않는다(self) -> None:
        metrics = CountingMetrics()
        srv = WebServer(host="127.0.0.1", port=0, router=make_router(metrics), cache_ttl_sec=5.0)
        srv.start()
        try:
            get(srv.port, "/api/state/mametchi?days=7")
            get(srv.port, "/api/state/mametchi?days=7")
            assert metrics.calls == 1
        finally:
            srv.stop()

    def test_창_길이가_다르면_따로_모은다(self) -> None:
        metrics = CountingMetrics()
        srv = WebServer(host="127.0.0.1", port=0, router=make_router(metrics), cache_ttl_sec=5.0)
        srv.start()
        try:
            get(srv.port, "/api/state/mametchi?days=7")
            get(srv.port, "/api/state/mametchi?days=30")
            assert metrics.calls == 2
        finally:
            srv.stop()


class Test예외_격리:
    def test_핸들러_예외가_서버_스레드를_안_죽인다(self) -> None:
        class BoomRouter:
            def handle(self, method, path, query, body):  # type: ignore[no-untyped-def]
                raise RuntimeError("의도한 고장")

        srv = WebServer(host="127.0.0.1", port=0, router=BoomRouter())  # type: ignore[arg-type]
        srv.start()
        try:
            status, _ = get(srv.port, "/api/health")
            assert status == 500
            # 서버가 살아있으면 두번째 요청도 응답한다.
            status2, _ = get(srv.port, "/api/health")
            assert status2 == 500
        finally:
            srv.stop()
