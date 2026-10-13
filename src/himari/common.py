"""Small public value types shared by several module contracts.

Only identifiers, plain immutable values and the deep-immutability rule live
here — no module-specific DTOs. Anything owned by one module belongs in that module's
``api.py`` (V3-046). Every ``himari.<module>.api`` may import this module.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, NewType, NoReturn, cast

# --- Deep immutability (public DTO rule) -----------------------------------
# ``frozen=True`` only blocks attribute reassignment. Every public DTO also
# inherits ``DeepFrozen``, which replaces each field value with a deep-frozen
# copy at construction: mutable inputs are copied, so neither the caller nor a
# Tool handler holding the object can change a recorded value afterwards.
# Frozen values are shared safely (Data may return the stored objects).


class FrozenMap[K, V](Mapping[K, V]):
    """Immutable ``Mapping`` that is *not* a ``dict`` subclass, so unbound
    ``dict`` methods and C-level dict APIs cannot mutate it. Items live in a
    private ``MappingProxyType`` over a copy. Use ``thaw`` for JSON output.

    Python cannot stop deliberate low-level tampering (``object.__setattr__``
    also bypasses ``frozen=True``); the guarantee is that no public or dict
    API can change a recorded value.
    """

    __slots__ = ("_items", "_hash")
    _items: Mapping[K, V]
    _hash: int | None

    def __init__(self, items: Mapping[K, V] | Iterable[tuple[K, V]] = ()) -> None:
        object.__setattr__(self, "_items", MappingProxyType(dict(items)))
        object.__setattr__(self, "_hash", None)

    def __setattr__(self, name: str, value: object) -> NoReturn:
        raise AttributeError("FrozenMap is immutable")

    def __delattr__(self, name: str) -> NoReturn:
        raise AttributeError("FrozenMap is immutable")

    def __getitem__(self, key: K) -> V:
        return self._items[key]

    def __iter__(self) -> Iterator[K]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __repr__(self) -> str:
        return f"FrozenMap({dict(self._items)!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Mapping):
            return NotImplemented
        return dict(self._items) == dict(other.items())

    def __hash__(self) -> int:
        if self._hash is None:
            object.__setattr__(self, "_hash", hash(frozenset(self._items.items())))
        assert self._hash is not None
        return self._hash

    def __reduce__(self) -> tuple[type[FrozenMap[K, V]], tuple[dict[K, V]]]:
        return (type(self), (dict(self._items),))

    def __copy__(self) -> FrozenMap[K, V]:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> FrozenMap[K, V]:
        return self


def deep_freeze[T](value: T) -> T:
    """Deep-frozen copy: Mapping → FrozenMap, list/tuple → tuple, set →
    frozenset, recursively. Scalars, bytes, enums, datetimes and (already
    frozen) DTO instances are returned as is."""
    result: object
    if _is_frozen(value):
        result = value
    elif isinstance(value, Mapping):
        result = FrozenMap({k: deep_freeze(v) for k, v in value.items()})
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
    if isinstance(value, FrozenMap):
        return all(_is_frozen(v) for v in value.values())
    if isinstance(value, (tuple, frozenset)) and not isinstance(value, str):
        return all(_is_frozen(v) for v in value)
    return not isinstance(value, (Mapping, list, set, bytearray))


def thaw(value: object) -> object:
    """Plain JSON-compatible copy (dicts and lists) of a frozen value, for
    serialization boundaries. The result is a fresh, independent object."""
    if isinstance(value, Mapping):
        return {k: thaw(v) for k, v in value.items()}
    if isinstance(value, (tuple, list, frozenset, set)):
        return [thaw(v) for v in value]
    return value


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

ASK_USER_TOOL_ID = ToolId("ask_user")
"""The suspending question Tool (V3-044). Control provides its handler; Data
derives the wait state from it; Provider rejects it mixed with other calls."""

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
