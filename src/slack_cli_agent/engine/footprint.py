"""What one engine call actually puts on the wire.

Kept apart from EngineCapabilities: that says what an engine guarantees, this
says what it costs. The two move for different reasons and a reader of one
should not have to load the other.

Only sizes and kinds, never the text itself -- the audit file is readable by
people who are not allowed to read the prompts (sca-ygd).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Instructions ride their own CLI argument, outside the conversation.
INSTRUCTION_TRANSPORT_NATIVE = "native_system"
#: Instructions are folded into the user prompt, in the same layer as the
#: Slack input. On a resumed session they join that session's context.
INSTRUCTION_TRANSPORT_USER_PROMPT = "user_prompt"


@dataclass(frozen=True)
class PayloadFootprint:
    """Sizes in UTF-8 bytes. Character counts read Korean at a third of its cost."""

    instruction_bytes: int
    user_prompt_bytes: int
    #: What the engine adapter adds on its own -- readable-path notes, the
    #: untrusted-input mark. Separated so a growing prompt isn't blamed on
    #: the adapter or the other way round.
    adapter_added_bytes: int
    instruction_transport: str
    #: Whether this turn's instructions land in a resumed session's context.
    #: The one field sca-ygd is about: true means N resumes cost N times the
    #: instruction size.
    instruction_replayed_on_resume: bool

    @property
    def total_bytes(self) -> int:
        return self.instruction_bytes + self.user_prompt_bytes + self.adapter_added_bytes

    def as_audit_dict(self) -> dict[str, Any]:
        return {
            "instruction_bytes": self.instruction_bytes,
            "user_prompt_bytes": self.user_prompt_bytes,
            "adapter_added_bytes": self.adapter_added_bytes,
            "total_bytes": self.total_bytes,
            "instruction_transport": self.instruction_transport,
            "instruction_replayed_on_resume": self.instruction_replayed_on_resume,
        }


def utf8_bytes(text: str) -> int:
    return len(text.encode("utf-8"))
