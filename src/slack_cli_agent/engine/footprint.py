"""How much text one engine call puts in front of the model.

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
    #: the adapter or the other way round. CLI-side overhead the model never
    #: sees (flag names, TOML escaping, --add-dir) is not counted: this
    #: measures what the model reads, which is what a cap would act on.
    adapter_added_bytes: int
    instruction_transport: str
    #: Whether this turn's instructions land in a resumed session's context.
    #: The one field sca-ygd is about: true means N resumes cost N times the
    #: instruction size.
    instruction_replayed_on_resume: bool
    #: 이 호출에 실제로 붙인 MCP 서버 수. None 은 엔진이 세지 않은 것이다.
    mcp_servers_attached: int | None = None
    #: 이 호출에 닫아 넘긴 내장 도구 이름 (--tools). None 은 엔진이 도구 집합을
    #: 닫지 않았거나 세지 않은 것이고, 빈 튜플은 내장 도구를 하나도 열지 않은 것이다.
    tools_builtin: tuple[str, ...] | None = None
    #: --allowedTools 에 넘긴 이름. 이름만 담고 값이나 자격증명은 담지 않는다.
    tools_allowed: tuple[str, ...] | None = None
    #: --disallowedTools 에 넘긴 이름.
    tools_disallowed: tuple[str, ...] | None = None

    @property
    def total_bytes(self) -> int:
        return self.instruction_bytes + self.user_prompt_bytes + self.adapter_added_bytes

    def as_audit_dict(self) -> dict[str, Any]:
        extra: dict[str, Any] = (
            {} if self.mcp_servers_attached is None
            else {"mcp_servers_attached": self.mcp_servers_attached}
        )
        for key, names in (
            ("tools_builtin", self.tools_builtin),
            ("tools_allowed", self.tools_allowed),
            ("tools_disallowed", self.tools_disallowed),
        ):
            if names is not None:
                extra[key] = list(names)
        return {
            "instruction_bytes": self.instruction_bytes,
            "user_prompt_bytes": self.user_prompt_bytes,
            "adapter_added_bytes": self.adapter_added_bytes,
            "total_bytes": self.total_bytes,
            "instruction_transport": self.instruction_transport,
            "instruction_replayed_on_resume": self.instruction_replayed_on_resume,
            **extra,
        }


def utf8_bytes(text: str) -> int:
    return len(text.encode("utf-8"))
