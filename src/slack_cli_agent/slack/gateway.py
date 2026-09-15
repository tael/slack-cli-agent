"""SlackGateway — Socket Mode connection and event dispatch.

Connection setup is injected via `connector` so unit tests never open
a real socket.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from ..core.errors import ConfigError
from ..core.periodic import PeriodicRunner
from ..reliability.connection import ConnectionEpochRecorder, ConnectionKind

log = logging.getLogger(__name__)

Connector = Callable[["SlackGateway", str], Any]

# How often the background watch checks whether the socket is actually up.
# Not the process's own heartbeat — apps.connections.open plus the
# websocket handshake can take a couple of seconds, so this only needs to
# be short enough that the first "connected" log doesn't lag noticeably.
_CONNECTION_POLL_SEC = 2.0


class ConnectionEdgeDetector:
    """Turns repeated `is_connected()` polls into "just became connected"
    events, and tells the first connection apart from a reconnect.

    Without this, a poll loop that logs on every `True` would log once per
    poll interval for the entire time the socket stays up — the opposite of
    the quiet-means-fine signal this is meant to produce.
    """

    def __init__(self) -> None:
        self._connected = False
        self._ever_connected = False

    def on_poll(self, connected: bool) -> str | None:
        if connected and not self._connected:
            kind = "reconnect" if self._ever_connected else "initial"
            self._connected = True
            self._ever_connected = True
            return kind
        if not connected:
            self._connected = False
        return None


class SlackGateway:
    def __init__(
        self,
        client: Any,
        connector: Connector | None = None,
        profile_name: str = "",
        epoch_recorder: ConnectionEpochRecorder | None = None,
    ) -> None:
        self._client = client
        self._connector = connector or _socket_mode_connect
        self._profile_name = profile_name
        self._handlers: dict[str, list[Callable[[Mapping[str, Any]], None]]] = {}
        self._epoch_recorder = epoch_recorder
        self._edges = ConnectionEdgeDetector()

    def observe_connection(self, connected: bool) -> None:
        """Feeds one socket-state observation to the log and the epoch ledger.

        The worker that runs catch-up is a separate process, so the ledger is
        the only path this observation can take.
        """
        kind = self._edges.on_poll(connected)
        if kind is not None:
            self.log_connected(reconnect=(kind == "reconnect"))
            self._record_connection(kind)
        elif connected:
            self._note_alive()

    def _record_connection(self, kind: str) -> None:
        if self._epoch_recorder is None:
            return
        try:
            self._epoch_recorder.record_connection(
                ConnectionKind.RECONNECT if kind == "reconnect" else ConnectionKind.INITIAL
            )
        except Exception:
            # Dropping the socket over a failed write would lose every later
            # event; a late catch-up is the cheaper failure.
            log.exception("소켓 연결 세대 기록 실패")

    def _note_alive(self) -> None:
        if self._epoch_recorder is None:
            return
        try:
            self._epoch_recorder.note_alive()
        except Exception:
            log.exception("소켓 생존 기록 실패")

    @property
    def client(self) -> Any:
        return self._client

    def on(self, event_type: str, handler: Callable[[Mapping[str, Any]], None]) -> None:
        """Registers a handler for an event type. Multiple handlers run in registration order."""
        self._handlers.setdefault(event_type, []).append(handler)

    def dispatch(self, event_type: str, event: Mapping[str, Any]) -> None:
        for handler in self._handlers.get(event_type, ()):
            handler(event)

    def handler_count(self, event_type: str) -> int:
        return len(self._handlers.get(event_type, ()))

    def handle_events_api(self, payload: Mapping[str, Any]) -> None:
        """Dispatches one events_api payload.

        Handler exceptions are caught, not re-raised — letting one
        escape would drop the socket connection and lose every event
        after it.
        """
        event = payload.get("event") or {}
        event_type = event.get("type") or ""
        if not event_type:
            return
        try:
            self.dispatch(event_type, event)
        except Exception:
            log.exception("이벤트 처리 실패: %s", event_type)

    def log_connected(self, *, reconnect: bool = False) -> None:
        """Logs one line for an actual Socket Mode connection, not just the
        process having started. `auth_test` failing doesn't block the log —
        it just leaves the workspace/bot fields as "확인 안 됨" instead of
        losing the connection event entirely.
        """
        team = "확인 안 됨"
        bot_user = "확인 안 됨"
        try:
            info = self._client.auth_test()
            team = str(info.get("team") or team)
            bot_user = str(info.get("user") or info.get("user_id") or bot_user)
        except Exception:  # noqa: BLE001, S110 — identifying the workspace is best-effort; the connection event itself must still be logged
            pass
        log.info(
            "슬랙 소켓 %s : 워크스페이스=%s 봇=%s 프로필=%s",
            "재연결" if reconnect else "연결",
            team,
            bot_user,
            self._profile_name or "확인 안 됨",
        )

    def start(self, app_token: str) -> None:
        """Opens the Socket Mode connection. Blocks until it drops.

        Refuses an empty app token outright, rather than letting
        Slack's auth error obscure whether the config is missing or
        the token expired.
        """
        if not app_token:
            raise ConfigError("Socket Mode 앱 토큰이 없다")
        self._connector(self, app_token)


def _socket_mode_connect(gateway: SlackGateway, app_token: str) -> None:
    """Default connector, backed by slack_sdk's Socket Mode client.

    Import is local to this function so reading this module doesn't
    pull in the SDK for subcommands unrelated to connecting.
    """
    from slack_sdk.socket_mode import SocketModeClient
    from slack_sdk.socket_mode.response import SocketModeResponse

    socket = SocketModeClient(app_token=app_token, web_client=gateway.client)

    def on_request(client: Any, request: Any) -> None:
        # Ack first. Slack retries any request it doesn't hear back
        # from within 3 seconds, so acking after processing would
        # duplicate slow requests.
        client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
        if request.type == "events_api":
            gateway.handle_events_api(request.payload or {})

    socket.socket_mode_request_listeners.append(on_request)
    socket.connect()

    # connect() returns as soon as it kicks off the handshake, not once the
    # socket is actually up — is_connected() is the real observation point.
    # Polling (rather than a one-shot check) also catches any later
    # reconnect after a drop, which would otherwise go unlogged.
    def poll_connection() -> None:
        gateway.observe_connection(bool(socket.is_connected()))

    PeriodicRunner(poll_connection, _CONNECTION_POLL_SEC, name="socket_mode_watch").start()

    # Block here — connect() returns immediately, so without this the
    # process would exit before receiving any events.
    from threading import Event

    Event().wait()
