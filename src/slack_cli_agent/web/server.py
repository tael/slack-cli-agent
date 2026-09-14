"""웹 콘솔 HTTP 서버. http.server 위에 얇게 얹는다.

127.0.0.1 에만 묶는다 -- 지표에 질문·답변 원문과 토큰·비용이 들어간다.
HTTP/1.1 로 응답한다. 기본값 HTTP/1.0 은 응답마다 연결을 닫아 TIME_WAIT 가
쌓인다 -- 원본은 5초 폴링 나흘에 5,621개가 남아 새 연결이 막혔다.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Protocol, Self
from urllib.parse import parse_qsl, urlsplit

from .api import ApiResponse

_LOGGER = logging.getLogger(__name__)

DEFAULT_ASSETS_DIR = Path(__file__).resolve().parent / "assets"

_FALLBACK_HTML = (
    "<html><head><meta charset=\"utf-8\"></head>"
    "<body><h1>웹 콘솔 화면을 아직 설치하지 못했다</h1>"
    "<p>패키지에 assets/index.html 이 없다.</p></body></html>"
)


class _ApiRouterLike(Protocol):
    def handle(self, method: str, path: str, query: Mapping[str, str], body: object | None) -> ApiResponse: ...


class _MetricsCache:
    def __init__(self, ttl_sec: float) -> None:
        self._ttl_sec = ttl_sec
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, str], tuple[float, ApiResponse]] = {}

    def get(self, key: tuple[str, str]) -> ApiResponse | None:
        with self._lock:
            hit = self._entries.get(key)
            if hit is None:
                return None
            stored_at, response = hit
            if time.time() - stored_at >= self._ttl_sec:
                return None
            return response

    def put(self, key: tuple[str, str], response: ApiResponse) -> None:
        with self._lock:
            self._entries[key] = (time.time(), response)


def _make_handler_class(
    router: _ApiRouterLike, assets_dir: Path, cache: _MetricsCache
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            self._safely(self._handle_get)

        def do_PUT(self) -> None:
            self._safely(self._handle_put)

        def _handle_get(self) -> None:
            parts = urlsplit(self.path)
            path = parts.path
            if path in ("", "/"):
                self._serve_index()
                return
            if path.startswith("/api/"):
                query = dict(parse_qsl(parts.query))
                self._respond_via_cache(path, query)
                return
            self._send_json(ApiResponse(404, {"error": "찾을 수 없다"}))

        def _handle_put(self) -> None:
            parts = urlsplit(self.path)
            path = parts.path
            if not path.startswith("/api/"):
                self._send_json(ApiResponse(404, {"error": "찾을 수 없다"}))
                return
            query = dict(parse_qsl(parts.query))
            body = self._read_json_body()
            response = router.handle("PUT", path, query, body)
            self._send_json(response)

        def _respond_via_cache(self, path: str, query: Mapping[str, str]) -> None:
            cache_key = self._state_cache_key(path, query)
            if cache_key is not None:
                cached = cache.get(cache_key)
                if cached is not None:
                    self._send_json(cached)
                    return
            response = router.handle("GET", path, query, None)
            if cache_key is not None and response.status == 200:
                cache.put(cache_key, response)
            self._send_json(response)

        @staticmethod
        def _state_cache_key(path: str, query: Mapping[str, str]) -> tuple[str, str] | None:
            prefix = "/api/state/"
            if not path.startswith(prefix):
                return None
            bot = path[len(prefix):]
            return (bot, query.get("days", ""))

        def _read_json_body(self) -> object | None:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length == 0:
                return None
            raw = self.rfile.read(length)
            try:
                parsed: object = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None
            return parsed

        def _serve_index(self) -> None:
            index_path = assets_dir / "index.html"
            if index_path.is_file():
                content = index_path.read_bytes()
            else:
                content = _FALLBACK_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def _send_json(self, response: ApiResponse) -> None:
            body = json.dumps(response.body, ensure_ascii=False).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _safely(self, action: Callable[[], None]) -> None:
            try:
                action()
            except Exception as exc:  # noqa: BLE001 - 요청 스레드가 죽으면 화면 전체가 멎는다
                try:
                    self._send_json(ApiResponse(500, {"error": f"{type(exc).__name__}: {exc}"}))
                except Exception:
                    _LOGGER.exception("500 응답 전송에 실패했다")

    return Handler


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class WebServer:
    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8787,
        router: _ApiRouterLike,
        assets_dir: Path | None = None,
        cache_ttl_sec: float = 2.0,
    ) -> None:
        self._host = host
        self._requested_port = port
        self._router = router
        self._assets_dir = assets_dir if assets_dir is not None else DEFAULT_ASSETS_DIR
        self._cache = _MetricsCache(cache_ttl_sec)
        self._httpd: _Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        if self._httpd is None:
            raise RuntimeError("서버가 아직 시작되지 않았다")
        return int(self._httpd.server_address[1])

    @property
    def bound_address(self) -> tuple[str, int]:
        if self._httpd is None:
            raise RuntimeError("서버가 아직 시작되지 않았다")
        sock_name = self._httpd.socket.getsockname()
        return (str(sock_name[0]), int(sock_name[1]))

    def start(self) -> None:
        if self._httpd is not None:
            raise RuntimeError("서버가 이미 실행 중이다")
        handler_class = _make_handler_class(self._router, self._assets_dir, self._cache)
        self._httpd = _Server((self._host, self._requested_port), handler_class)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def serve_forever(self) -> None:
        """Blocks until stop() is called. start() runs the loop on a thread."""
        if self._thread is None:
            raise RuntimeError("서버가 아직 시작되지 않았다")
        while self._thread.is_alive():
            self._thread.join(timeout=0.5)

    def stop(self) -> None:
        if self._httpd is None:
            return
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._httpd = None
        self._thread = None

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()
