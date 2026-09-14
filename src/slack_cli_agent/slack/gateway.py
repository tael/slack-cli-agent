"""SlackGateway — Socket Mode 연결과 이벤트 분배.

원본은 `slack_bolt.App` 의 `@app.event(...)` 데코레이터로 핸들러를 등록하고,
`SocketModeHandler` 가 연결을 맺는다. 여기서는 핸들러 등록·분배와 연결을
나눈다. 연결을 맺는 부분은 `connector` 로 주입받아, 단위 시험이 실제
네트워크를 부르지 않게 한다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from ..core.errors import ConfigError

log = logging.getLogger(__name__)

# 연결을 맺는 함수. 게이트웨이 자신과 앱 토큰을 받는다.
Connector = Callable[["SlackGateway", str], Any]


class SlackGateway:
    """이벤트 타입별 핸들러를 등록하고 들어온 이벤트를 분배한다."""

    def __init__(self, client: Any, connector: Connector | None = None) -> None:
        self._client = client
        self._connector = connector or _socket_mode_connect
        self._handlers: dict[str, list[Callable[[Mapping[str, Any]], None]]] = {}

    @property
    def client(self) -> Any:
        return self._client

    def on(self, event_type: str, handler: Callable[[Mapping[str, Any]], None]) -> None:
        """이 이벤트 타입이 들어올 때 부를 핸들러를 등록한다.

        같은 타입에 여러 핸들러를 등록할 수 있다. 등록한 순서대로 부른다.
        """
        self._handlers.setdefault(event_type, []).append(handler)

    def dispatch(self, event_type: str, event: Mapping[str, Any]) -> None:
        """등록된 핸들러 전부에게 이벤트를 넘긴다.

        등록된 핸들러가 없는 이벤트 타입은 조용히 넘어간다.
        """
        for handler in self._handlers.get(event_type, ()):
            handler(event)

    def handler_count(self, event_type: str) -> int:
        return len(self._handlers.get(event_type, ()))

    def handle_events_api(self, payload: Mapping[str, Any]) -> None:
        """Socket Mode 로 온 events_api payload 하나를 분배한다.

        핸들러에서 난 예외를 밖으로 내지 않는다. 여기서 예외가 올라가면
        소켓 연결이 끊기고, 그 뒤에 온 요청이 전부 사라진다. 다만 삼키되
        기록은 남긴다.
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
        """Socket Mode 연결을 맺는다. 연결이 끊길 때까지 돌아온다.

        토큰이 비어 있으면 연결하지 않는다. 빈 토큰으로 연결을 시도하면
        슬랙이 인증 오류를 내는데, 그 시점에는 설정이 빠진 것인지 토큰이
        만료된 것인지 구분되지 않는다.
        """
        if not app_token:
            raise ConfigError("Socket Mode 앱 토큰이 없다")
        self._connector(self, app_token)


def _socket_mode_connect(gateway: SlackGateway, app_token: str) -> None:
    """기본 연결기. `slack_sdk` 의 Socket Mode 구현을 쓴다.

    import 를 함수 안에 둔다 — 이 모듈을 읽는 것만으로 SDK 가 딸려 오면
    연결과 무관한 하위 명령까지 그 의존에 매인다.
    """
    from slack_sdk.socket_mode import SocketModeClient
    from slack_sdk.socket_mode.response import SocketModeResponse

    socket = SocketModeClient(app_token=app_token, web_client=gateway.client)

    def on_request(client: Any, request: Any) -> None:
        # 먼저 응답한다. 슬랙은 3초 안에 답을 못 받으면 같은 요청을 다시 보낸다 —
        # 처리를 마친 뒤에 답하면 오래 걸리는 요청이 중복 접수된다.
        client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
        if request.type == "events_api":
            gateway.handle_events_api(request.payload or {})

    socket.socket_mode_request_listeners.append(on_request)
    socket.connect()

    # 연결을 유지한다. `connect()` 는 곧바로 돌아오므로 여기서 막지 않으면
    # 프로세스가 그대로 끝나 이벤트를 하나도 받지 못한다.
    from threading import Event

    Event().wait()
