"""Plain text out of a message's Slack blocks.

A message posted as a markdown block reaches other clients with its body in
`blocks` and only a one-line notification preview in `text`. Inside those
blocks a mention is a `user` element, not the `<@ID>` string, so a check
that reads `text` alone misses a bot named mid-body — and Slack sends no
app_mention for it either. Mirrors mametchi bot.py `block_text`/`event_body`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: Child keys walked recursively. `fields` is a section's columns, `rows` a
#: table block's cells; without them a mention inside either goes unseen.
_CHILD_KEYS = ("elements", "fields", "blocks", "rows")

#: Leaf containers that end a line, so a list does not run together.
_LINE_KINDS = frozenset({"rich_text_section", "rich_text_preformatted", "rich_text_quote"})


def block_text(node: Any) -> str:
    """The text of one block (or a list of them), mentions as `<@ID>`."""
    if isinstance(node, list):
        return "".join(block_text(x) for x in node)
    if not isinstance(node, Mapping):
        return ""
    kind = node.get("type")
    if kind == "user" and node.get("user_id"):
        return f"<@{node['user_id']}>"
    if kind == "usergroup" and node.get("usergroup_id"):
        return f"<!subteam^{node['usergroup_id']}>"
    if kind == "link" and node.get("url"):
        return str(node.get("text") or node["url"])
    out: list[str] = []
    text = node.get("text")
    if isinstance(text, str):
        out.append(text)
    elif text is not None:
        out.append(block_text(text))
    for key in _CHILD_KEYS:
        if node.get(key):
            out.append(block_text(node[key]))
    joined = "".join(out)
    if kind in _LINE_KINDS:
        return joined + "\n"
    return joined


def event_body(event: Mapping[str, Any]) -> str:
    """The whole body an event carries: its blocks flattened, else `text`."""
    parts = [p for p in (block_text(b) for b in (event.get("blocks") or [])) if p]
    body = "\n".join(parts).strip()
    return body or str(event.get("text") or "")
