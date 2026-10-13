"""Small public value types shared by several module contracts.

Only identifiers and plain immutable values live here — no behavior, no
module-specific DTOs. Anything owned by one module belongs in that module's
``api.py`` (V3-046). Every ``himari.<module>.api`` may import this module.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import NewType

# --- Opaque identifiers ------------------------------------------------------
# IDs are references, never authorization (Data v0.2 §3).

PrincipalId = NewType("PrincipalId", str)
ConversationId = NewType("ConversationId", str)
EventId = NewType("EventId", str)
RunId = NewType("RunId", str)
CallId = NewType("CallId", str)
ArtifactId = NewType("ArtifactId", str)
WorkspaceId = NewType("WorkspaceId", str)
EnvironmentInstanceId = NewType("EnvironmentInstanceId", str)
BindingId = NewType("BindingId", str)
ToolId = NewType("ToolId", str)
EnvironmentKind = NewType("EnvironmentKind", str)

type JsonValue = (
    None | bool | int | float | str | Sequence[JsonValue] | Mapping[str, JsonValue]
)


@dataclass(frozen=True)
class TrustedScope:
    """Authenticated principal + Conversation.

    Created only by the trusted entry point / composition root from an
    authenticated Web/CLI identity. Never built from model output or Tool
    arguments. Data re-checks ownership on every call.
    """

    principal_id: PrincipalId
    conversation_id: ConversationId


@dataclass(frozen=True)
class CallRef:
    """Identity of one ToolCall: unique ``(run_id, call_id)``."""

    run_id: RunId
    call_id: CallId


# --- Content ---------------------------------------------------------------


@dataclass(frozen=True)
class TextBlock:
    text: str


@dataclass(frozen=True)
class AttachmentRef:
    """Immutable reference to a versioned file (FILE-02); never raw bytes."""

    file_id: str
    version: int


type ContentBlock = TextBlock | AttachmentRef


@dataclass(frozen=True)
class ToolCall:
    """One model-issued call, in original order (``ordinal``) within its Run."""

    call_id: CallId
    ordinal: int
    tool_id: ToolId
    arguments: Mapping[str, JsonValue]


@dataclass(frozen=True)
class ModelToolResult:
    """The ToolResult content the model sees for one call."""

    blocks: tuple[ContentBlock, ...]
    is_error: bool = False


@dataclass(frozen=True)
class TokenUsage:
    """Provider-reported usage. Execution metadata, never prompt text."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    cached_input_tokens: int | None = None


class RawRepresentation(Enum):
    """What a stored raw Provider request is equal to (V3-P14 is OPEN).

    Records must never claim more exactness than was achieved.
    """

    WIRE_EXACT = "wire_exact"
    ADAPTER_SERIALIZED = "adapter_serialized"
