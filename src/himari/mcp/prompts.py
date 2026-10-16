"""A server prompt as text the user reviews and sends (V3-P60 §4).

``prompts/get`` answers with messages; HIMARI turns them into one text for
the user's composer, never into the conversation itself: what reaches the
model is what the user then sends. The text starts with a line saying where
it came from, then each ``user`` message's content in order: text as it is,
an embedded text resource under ``[resource <uri>]``, a resource link as a
line. Anything HIMARI cannot pass on as text — an ``assistant`` message
(O2: assistant turns come only from model Runs), an image, audio, a blob
resource, a content type it does not know — refuses the whole prompt, so
nothing is filled in that differs from what the server meant.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

MAX_PROMPT_TEXT_CHARS = 32_000  # what one user message may hold
MAX_HEADER_VALUE_CHARS = 80


class PromptContentError(ValueError):
    """The prompt cannot be given as text; the message says why."""


def _shown(value: str) -> str:
    one_line = " ".join("".join(c if c.isprintable() else " " for c in value).split())
    return one_line if len(one_line) <= MAX_HEADER_VALUE_CHARS else one_line[: MAX_HEADER_VALUE_CHARS - 1] + "…"


def _token(value: str) -> str:
    """A name in the source line: whatever it holds, printable on one line."""
    return "".join(c if c.isprintable() else "?" for c in value)


def header(source: str, name: str, arguments: Sequence[tuple[str, str]]) -> str:
    """``[MCP prompt calliope/calliope_commentary · game=g-1 ply=10]``: always
    one line, whatever the names and values hold (PR #43 review B2)."""
    given = " ".join(f"{_token(key)}={_shown(value)}" for key, value in arguments if value != "")
    return f"[MCP prompt {_token(source)}/{_token(name)}" + (f" · {given}" if given else "") + "]"


def _content(content: object) -> str:
    if not isinstance(content, dict):
        raise PromptContentError("a message has no readable content")
    kind = content.get("type")
    if kind == "text" and isinstance(content.get("text"), str):
        return content["text"]  # type: ignore[no-any-return]
    if kind == "resource" and isinstance(content.get("resource"), dict):
        resource = content["resource"]
        if isinstance(resource.get("text"), str) and "blob" not in resource:
            uri = resource.get("uri")
            return f"[resource {uri if isinstance(uri, str) else ''}]\n{resource['text']}"
        raise PromptContentError("it contains a binary resource, which HIMARI cannot send yet")
    if kind == "resource_link" and isinstance(content.get("uri"), str):
        name = content.get("name")
        return f"[resource link: {name if isinstance(name, str) else ''} {content['uri']}]"
    if kind in ("image", "audio"):
        raise PromptContentError(f"it contains {kind}, which HIMARI cannot send yet")
    raise PromptContentError("it contains content HIMARI cannot send")


def prompt_text(source: str, name: str, arguments: Sequence[tuple[str, str]], result: Mapping[str, Any]) -> str:
    """The text to fill in; PromptContentError when it cannot be given."""
    messages = result.get("messages")
    if not isinstance(messages, list) or not messages:
        raise PromptContentError("the server gave no messages")
    parts: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            raise PromptContentError("a message is not readable")
        role = message.get("role")
        if role == "assistant":
            raise PromptContentError("it contains an assistant message, which HIMARI does not put in a conversation")
        if role != "user":
            raise PromptContentError("a message has no user role")
        contents = message.get("content")
        for content in contents if isinstance(contents, list) else [contents]:  # one block, or a list
            parts.append(_content(content))
    text = header(source, name, arguments) + "\n\n" + "\n\n".join(parts)
    if len(text) > MAX_PROMPT_TEXT_CHARS:
        raise PromptContentError(f"it is longer than a message may be ({MAX_PROMPT_TEXT_CHARS} characters)")
    return text
