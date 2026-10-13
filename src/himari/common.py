"""Small public value types shared by several module contracts.

Only identifiers, plain immutable values and the deep-immutability rule live
here — no module-specific DTOs. Anything owned by one module belongs in that module's
``api.py`` (V3-046). Every ``himari.<module>.api`` may import this module.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, NewType, NoReturn, cast

# --- Deep immutability (public DTO rule) -----------------------------------
# ``frozen=True`` only blocks attribute reassignment. Every public DTO also
# inherits ``DeepFrozen``, which replaces each field value with a deep-frozen
# copy at construction: mutable inputs are copied, so neither the caller nor a
# Tool handler holding the object can change a recorded value afterwards.


class FrozenDict[K, V](dict[K, V]):
    """Read-only ``dict``. Subclasses dict so ``json`` and ``dataclasses.asdict``
    keep working; every mutating method raises ``TypeError``."""

    __slots__ = ()

    def _readonly(self) -> NoReturn:
        raise TypeError("FrozenDict is immutable")

    def __setitem__(self, key: K, value: V) -> NoReturn:
        self._readonly()

    def __delitem__(self, key: K) -> NoReturn:
        self._readonly()

    def __ior__(self, other: object) -> NoReturn:  # type: ignore[misc]
        self._readonly()

    def clear(self) -> NoReturn:
        self._readonly()

    def pop(self, *args: object) -> NoReturn:
        self._readonly()

    def popitem(self) -> NoReturn:
        self._readonly()

    def setdefault(self, *args: object) -> NoReturn:
        self._readonly()

    def update(self, *args: object, **kwargs: object) -> NoReturn:
        self._readonly()

    def __hash__(self) -> int:  # type: ignore[override]
        return hash(frozenset(self.items()))

    def __reduce__(self) -> tuple[type[FrozenDict[K, V]], tuple[dict[K, V]]]:
        return (type(self), (dict(self),))  # pickle/deepcopy without __setitem__


def deep_freeze[T](value: T) -> T:
    """Deep-frozen copy: Mapping → FrozenDict, list/tuple → tuple, set →
    frozenset, recursively. Scalars, bytes, enums, datetimes and (already
    frozen) DTO instances are returned as is."""
    result: object
    if _is_frozen(value):
        result = value
    elif isinstance(value, Mapping):
        result = FrozenDict({k: deep_freeze(v) for k, v in value.items()})
    elif isinstance(value, (list, tuple)):
        result = tuple(deep_freeze(v) for v in cast(Iterable[Any], value))
    elif isinstance(value, (set, frozenset)):
        result = frozenset(deep_freeze(v) for v in cast(Iterable[Any], value))
    elif isinstance(value, bytearray):
        result = bytes(value)
    else:
        result = value  # DTO instances (DeepFrozen), datetimes, service objects
    return cast(T, result)


def _is_frozen(value: object) -> bool:
    if isinstance(value, FrozenDict):
        return all(_is_frozen(v) for v in value.values())
    if isinstance(value, (tuple, frozenset)) and not isinstance(value, str):
        return all(_is_frozen(v) for v in value)
    return not isinstance(value, (Mapping, list, set, bytearray))


class DeepFrozen:
    """Mixin for public frozen dataclasses: deep-freezes every field."""

    def __post_init__(self) -> None:
        for f in dataclasses.fields(cast(Any, self)):
            object.__setattr__(self, f.name, deep_freeze(getattr(self, f.name)))


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
class TrustedScope(DeepFrozen):
    """Authenticated principal + Conversation.

    Created only by the trusted entry point / composition root from an
    authenticated Web/CLI identity. Never built from model output or Tool
    arguments. Data re-checks ownership on every call.
    """

    principal_id: PrincipalId
    conversation_id: ConversationId


@dataclass(frozen=True)
class CallRef(DeepFrozen):
    """Identity of one ToolCall: unique ``(run_id, call_id)``."""

    run_id: RunId
    call_id: CallId


# --- Content ---------------------------------------------------------------


@dataclass(frozen=True)
class TextBlock(DeepFrozen):
    text: str


@dataclass(frozen=True)
class AttachmentRef(DeepFrozen):
    """Immutable reference to a versioned file (FILE-02); never raw bytes."""

    file_id: str
    version: int


type ContentBlock = TextBlock | AttachmentRef


@dataclass(frozen=True)
class ToolCall(DeepFrozen):
    """One model-issued call, in original order (``ordinal``) within its Run."""

    call_id: CallId
    ordinal: int
    tool_id: ToolId
    arguments: Mapping[str, JsonValue]


@dataclass(frozen=True)
class ModelToolResult(DeepFrozen):
    """The ToolResult content the model sees for one call."""

    blocks: tuple[ContentBlock, ...]
    is_error: bool = False


@dataclass(frozen=True)
class TokenUsage(DeepFrozen):
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
