"""Contract between the worker (consumes the queue) and the pipeline (handles one
request end to end). Kept as a Protocol, not ABC, since there's no shared base
implementation to inherit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .context import RequestContext


@dataclass(frozen=True)
class HandleOutcome:
    ok: bool
    failure: str = ""
    posted_ts: str = ""
    # Not a failure — ok stays True. Kept separate from failure because the reaction
    # marker differs: silence gets a "muted" mark and counts as done, failure gets an
    # x mark and is retried.
    silent: bool = False
    # Also not a failure. Handed off to the watch queue, so the message keeps an
    # unfinished mark: a done mark would drop it from catch-up recovery while the
    # follow-up is still pending. The queue row itself completes (sca-5sb).
    watching: bool = False


@runtime_checkable
class RequestHandler(Protocol):
    def handle(self, ctx: RequestContext) -> HandleOutcome:
        # Must not raise. Any failure is reported as ok=False with a reason so the
        # worker can record it on the queue; letting the worker catch it would lose that reason.
        ...
