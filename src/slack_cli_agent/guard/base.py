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
    # The request text as received. AddresseeGuard keeps a leading mention of
    # anyone this text itself mentions ("greet <@A> and <@B>").
    request_text: str = ""
    # Body before rewrite; RewriteLossGuard only checks for loss when set.
    previous_body: str | None = None
    # Whether this body is a rewrite from WatchPromiseGuard's rerun request.
    # If still failing on retry, the promise text gets cut and replaced.
    is_rewrite_retry: bool = False
    # Bot the reply's trailing [[RECIPIENT]] tag names. BotMentionGuard keeps
    # that bot's leading mention: swapping it for a name made RecipientResolver
    # re-add the mention in front, so the name showed twice (2026-11-10).
    recipient_bot_id: str = ""
    # The Stop hook's figurative verdict for this turn's draft
    # (guard/figurative.Precompute), read by FigurativeGuard. None runs the
    # corrector in the guard.
    figurative_precomputed: Mapping[str, Any] | None = None


class OutputGuard(ABC):
    name: ClassVar[str]

    @abstractmethod
    def apply(self, body: str, ctx: GuardContext) -> GuardResult:
        """Return changed=False with the body unmodified if there's nothing to fix."""
