"""Protocol the reliability layer expects for reading Slack history.

`CatchupService` depends only on this Protocol, so it can be tested with a
stand-in and wired to a real implementation separately.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from .catchup import CatchupReport, RetryStatus


@runtime_checkable
class HistoryReader(Protocol):
    def read_history(
        self, channel: str, oldest: float, limit: int
    ) -> list[Mapping[str, Any]] | None:
        """Reads top-level messages in `channel` since `oldest`, up to `limit`.

        Slack can return an empty list with `ok: true` as a normal, non-error
        response — treating that at face value would read as "nothing missed".
        Returns None when repeated reads can't confirm whether it's really
        empty; that must not be interpreted as "no messages".
        """

    def read_thread(
        self, channel: str, thread_ts: str, limit: int
    ) -> list[Mapping[str, Any]]:
        """Reads a thread's messages (including the parent) in order.

        Returns at most `limit` messages from the first page, and an empty
        list on failure.
        """

    def read_full_thread(
        self, channel: str, thread_ts: str, page_size: int
    ) -> list[Mapping[str, Any]] | None:
        """Reads every page of a thread (including the parent) in order.

        Returns None when any page fails or the thread runs past the page
        cap. A truncated thread can be missing the bot's latest replies,
        and an empty list would read as "nothing missed", so the caller
        must treat None as "couldn't tell".
        """


@runtime_checkable
class CatchupPort(Protocol):
    """What Worker needs from the catch-up service.

    `CatchupService` satisfies it. Declared here so the worker doesn't depend
    on that class directly.
    """

    def sweep(self, channels: list[str], window: float) -> CatchupReport: ...

    def retry_pending(self) -> list[RetryStatus]: ...
