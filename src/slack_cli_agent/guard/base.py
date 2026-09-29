"""OutputGuard contract and the value objects the pipeline passes around.

Audit logging isn't this package's responsibility — it only reports what
changed via `GuardResult.detail`; writing that to the audit log is up to
the caller (RequestHandler etc.).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar


@dataclass(frozen=True)
class RerunRequest:
    """Signals that the model needs to rewrite its answer before this guard
    passes. Calling the engine again is the caller's job, not the pipeline's.
    """

    reason: str
    rewrite_prompt: str
    guard_name: str


@dataclass
class GuardResult:
    body: str
    changed: bool
    detail: Mapping[str, Any] = field(default_factory=dict)
    # Non-None means this guard isn't done: caller must re-invoke the engine
    # with rewrite_prompt and run the pipeline again on the new body.
    rerun: RerunRequest | None = None


@dataclass(frozen=True)
class GuardContext:
    """Request context guards use for their checks. Fields a given guard
    doesn't need are left at their default.
    """

    channel: str = ""
    thread_ts: str = ""
    asker_id: str = ""
    is_owner: bool = False
    owner_user_id: str = ""
    # Plain-text name -> Slack user ID, used by PlainMentionGuard.
    mention_names: Mapping[str, str] = field(default_factory=dict)
    # Body before rewrite; RewriteLossGuard only checks for loss when set.
    previous_body: str | None = None
    # Whether this body is a rewrite from WatchPromiseGuard's rerun request.
    # If still failing on retry, the promise text gets cut and replaced.
    is_rewrite_retry: bool = False


class OutputGuard(ABC):
    name: ClassVar[str]

    @abstractmethod
    def apply(self, body: str, ctx: GuardContext) -> GuardResult:
        """Return changed=False with the body unmodified if there's nothing to fix."""
