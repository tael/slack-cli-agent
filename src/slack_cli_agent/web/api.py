"""웹 콘솔 API 라우팅. HTTP 서버와 분리해 소켓 없이 시험한다.

경로 존재·타입 검증은 이 계층이 직접 한다. 주입된 편집기·수집기가 예외를
내면 그 원인을 알 수 없으므로 전부 500 으로 옮긴다 -- 이 파일이 문서화한
404/400 판정만 명시적으로 앞서 걸러낸다.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

DEFAULT_DAYS = 7
MIN_DAYS = 1
MAX_DAYS = 90


@dataclass(frozen=True)
class ApiResponse:
    status: int
    body: object


class ProfilesPort(Protocol):
    def names(self) -> list[str]: ...
    def read(self, name: str) -> dict[str, object]: ...
    def save(self, name: str, data: Mapping[str, object]) -> list[str]: ...


class ChannelsPort(Protocol):
    def list(self) -> list[dict[str, object]]: ...
    def update(self, channel_id: str, changes: Mapping[str, object]) -> dict[str, object]: ...


class FilesPort(Protocol):
    def names(self) -> list[str]: ...
    def read(self, name: str) -> str: ...
    def write(self, name: str, text: str) -> None: ...


class RosterPort(Protocol):
    def rows(self) -> list[dict[str, object]]: ...


class MetricsPort(Protocol):
    def collect(self, days: int) -> dict[str, object]: ...


_NOT_FOUND = ApiResponse(404, {"error": "찾을 수 없다"})
_METHOD_NOT_ALLOWED = ApiResponse(405, {"error": "허용하지 않는 메서드다"})
_BAD_BODY = ApiResponse(400, {"error": "잘못된 요청 본문이다"})


def _is_object(body: object) -> bool:
    return isinstance(body, Mapping)


class ApiRouter:
    def __init__(
        self,
        *,
        profiles: ProfilesPort,
        channels_for: Callable[[str], ChannelsPort],
        prompts_for: Callable[[str], FilesPort],
        knowledge_for: Callable[[str], FilesPort],
        learned_for: Callable[[str], FilesPort],
        metrics_for: Callable[[str], MetricsPort],
        roster: RosterPort,
    ) -> None:
        self._profiles = profiles
        self._channels_for = channels_for
        self._prompts_for = prompts_for
        self._knowledge_for = knowledge_for
        # Separate from knowledge: the learning batch appends here and a human
        # only ever removes a wrong line (sca-jl4.5).
        self._learned_for = learned_for
        self._metrics_for = metrics_for
        self._roster = roster

    def handle(self, method: str, path: str, query: Mapping[str, str], body: object | None) -> ApiResponse:
        segments = self._segments(path)
        if segments is None:
            return _NOT_FOUND
        try:
            return self._dispatch(method, segments, query, body)
        except Exception as exc:  # noqa: BLE001 - 핸들러 예외를 밖으로 내지 않는다
            return ApiResponse(500, {"error": f"{type(exc).__name__}: {exc}"})

    @staticmethod
    def _segments(path: str) -> list[str] | None:
        if not path.startswith("/api"):
            return None
        rest = path[len("/api"):].strip("/")
        return rest.split("/") if rest else []

    def _dispatch(
        self, method: str, segments: Sequence[str], query: Mapping[str, str], body: object | None
    ) -> ApiResponse:
        head = segments[0] if segments else ""
        rest = segments[1:]

        if head == "health" and not rest:
            return self._require(method, "GET", lambda: ApiResponse(200, {"ok": True}))
        if head == "bots" and not rest:
            return self._require(method, "GET", self._get_bots)
        if head == "profiles" and not rest:
            return self._require(method, "GET", self._get_profiles)
        if head == "profile" and len(rest) == 1:
            (name,) = rest
            if method == "GET":
                return self._get_profile(name)
            if method == "PUT":
                return self._put_profile(name, body)
            return _METHOD_NOT_ALLOWED
        if head == "channels" and len(rest) == 1:
            (bot,) = rest
            return self._require(method, "GET", lambda: self._get_channels(bot))
        if head == "channels" and len(rest) == 2:
            bot, channel_id = rest
            return self._require(method, "PUT", lambda: self._put_channel(bot, channel_id, body))
        if head == "prompts" and len(rest) == 1:
            (bot,) = rest
            return self._require(method, "GET", lambda: self._get_names(bot, self._prompts_for))
        if head == "prompt" and len(rest) == 2:
            bot, name = rest
            if method == "GET":
                return self._get_file(bot, name, self._prompts_for)
            if method == "PUT":
                return self._put_file(bot, name, body, self._prompts_for)
            return _METHOD_NOT_ALLOWED
        if head == "knowledge" and len(rest) == 1:
            (bot,) = rest
            return self._require(method, "GET", lambda: self._get_names(bot, self._knowledge_for))
        if head == "knowledge" and len(rest) == 2:
            bot, name = rest
            if method == "GET":
                return self._get_file(bot, name, self._knowledge_for)
            if method == "PUT":
                return self._put_file(bot, name, body, self._knowledge_for)
            return _METHOD_NOT_ALLOWED
        if head == "learned" and len(rest) == 1:
            (bot,) = rest
            return self._require(method, "GET", lambda: self._get_names(bot, self._learned_for))
        if head == "learned" and len(rest) == 2:
            bot, name = rest
            if method == "GET":
                return self._get_file(bot, name, self._learned_for)
            if method == "PUT":
                return self._put_file(bot, name, body, self._learned_for)
            return _METHOD_NOT_ALLOWED
        if head == "state" and len(rest) == 1:
            (bot,) = rest
            return self._require(method, "GET", lambda: self._get_state(bot, query))

        return _NOT_FOUND

    @staticmethod
    def _require(method: str, expected: str, handler: Callable[[], ApiResponse]) -> ApiResponse:
        if method != expected:
            return _METHOD_NOT_ALLOWED
        return handler()

    def _bot_exists(self, bot: str) -> bool:
        return bot in self._profiles.names()

    def _get_bots(self) -> ApiResponse:
        return ApiResponse(200, self._roster.rows())

    def _get_profiles(self) -> ApiResponse:
        return ApiResponse(200, self._profiles.names())

    def _get_profile(self, name: str) -> ApiResponse:
        if not self._bot_exists(name):
            return _NOT_FOUND
        return ApiResponse(200, self._profiles.read(name))

    def _put_profile(self, name: str, body: object | None) -> ApiResponse:
        if not _is_object(body):
            return _BAD_BODY
        assert isinstance(body, Mapping)
        errors = self._profiles.save(name, body)
        if errors:
            return ApiResponse(400, {"errors": errors})
        return ApiResponse(200, {"ok": True})

    def _get_channels(self, bot: str) -> ApiResponse:
        if not self._bot_exists(bot):
            return _NOT_FOUND
        return ApiResponse(200, self._channels_for(bot).list())

    def _put_channel(self, bot: str, channel_id: str, body: object | None) -> ApiResponse:
        if not self._bot_exists(bot):
            return _NOT_FOUND
        if not _is_object(body):
            return _BAD_BODY
        assert isinstance(body, Mapping)
        updated = self._channels_for(bot).update(channel_id, body)
        return ApiResponse(200, updated)

    def _get_names(self, bot: str, factory: Callable[[str], FilesPort]) -> ApiResponse:
        if not self._bot_exists(bot):
            return _NOT_FOUND
        return ApiResponse(200, factory(bot).names())

    def _get_file(self, bot: str, name: str, factory: Callable[[str], FilesPort]) -> ApiResponse:
        if not self._bot_exists(bot):
            return _NOT_FOUND
        editor = factory(bot)
        if name not in editor.names():
            return _NOT_FOUND
        return ApiResponse(200, {"name": name, "text": editor.read(name)})

    def _put_file(
        self, bot: str, name: str, body: object | None, factory: Callable[[str], FilesPort]
    ) -> ApiResponse:
        if not self._bot_exists(bot):
            return _NOT_FOUND
        if not _is_object(body):
            return _BAD_BODY
        assert isinstance(body, Mapping)
        text = body.get("text")
        if not isinstance(text, str):
            return _BAD_BODY
        try:
            factory(bot).write(name, text)
        except ValueError as exc:
            # Rejecting empty content is a user input error, not a server fault;
            # the page shows this list next to the field.
            return ApiResponse(400, {"errors": [str(exc)]})
        return ApiResponse(200, {"ok": True})

    def _get_state(self, bot: str, query: Mapping[str, str]) -> ApiResponse:
        if not self._bot_exists(bot):
            return _NOT_FOUND
        days = self._parse_days(query.get("days"))
        return ApiResponse(200, self._metrics_for(bot).collect(days))

    @staticmethod
    def _parse_days(raw: str | None) -> int:
        if raw is None or not raw.isdigit():
            return DEFAULT_DAYS
        return max(MIN_DAYS, min(MAX_DAYS, int(raw)))
