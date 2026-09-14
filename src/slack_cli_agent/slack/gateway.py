"""SlackGateway — Socket Mode connection and event dispatch.

Connection setup is injected via `connector` so unit tests never open
a real socket.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from ..core.errors import ConfigError

log = logging.getLogger(__name__)

Connector = Callable[["SlackGateway", str], Any]


class SlackGateway:
    def __init__(self, client: Any, connector: Connector | None = None) -> None:
        self._client = client
        self._connector = connector or _socket_mode_connect
        self._handlers: dict[str, list[Callable[[Mapping[str, Any]], None]]] = {}

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

    # Block here — connect() returns immediately, so without this the
    # process would exit before receiving any events.
    from threading import Event

    Event().wait()
