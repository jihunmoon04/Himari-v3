"""Small public value types shared by several module contracts.

Only identifiers, plain immutable values and the deep-immutability rule live
here — no module-specific DTOs. Anything owned by one module belongs in that module's
``api.py`` (V3-046). Every ``himari.<module>.api`` may import this module.
"""

from __future__ import annotations

import dataclasses
import re
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
FileId = NewType("FileId", str)  # V3-P66: one uploaded or presented file of a Conversation
ToolId = NewType("ToolId", str)
EnvironmentKind = NewType("EnvironmentKind", str)

# The Tool ids every Provider can carry as a function name (V3-P52): one or
# two segments joined by "/" (the namespace of an Environment or External
# Tool), each made of [A-Za-z0-9-] runs joined by single "_" — so a Provider
# may encode "/" as "__" reversibly — and at most 64 characters with "/"
# counted as two (the common function-name limit, ^[A-Za-z0-9_-]{1,64}$).
# Names from elsewhere (e.g. MCP allows "." and 128 characters) are mapped to
# such an id by whoever registers them; the Registry refuses any other id.
_TOOL_ID = re.compile(r"[A-Za-z0-9-]+(?:_[A-Za-z0-9-]+)*(?:/[A-Za-z0-9-]+(?:_[A-Za-z0-9-]+)*)?")
MAX_TOOL_NAME_CHARS = 64


def tool_id_problem(tool_id: str) -> str | None:
    """Why ``tool_id`` is not a valid Tool id, or None."""
    if not _TOOL_ID.fullmatch(tool_id):
        return "must be [A-Za-z0-9-] runs joined by single '_', with at most one '/'"
    if len(tool_id.replace("/", "__")) > MAX_TOOL_NAME_CHARS:
        return f"is longer than {MAX_TOOL_NAME_CHARS} characters ('/' counts as two)"
    return None


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


@dataclass(frozen=True)
class FileReference(DeepFrozen):
    """V3-P76 §3: part of a file (or all of it) the user pointed at in the
    editor. Built by the server from the file itself, never from text the
    browser sent. ``start``/``end`` are UTF-16 code units (the editor's
    positions); None for the whole file, which carries no excerpt."""

    path: str
    identity: tuple[int, ...]
    start: int | None = None
    end: int | None = None
    start_line: int | None = None
    end_line: int | None = None
    excerpt: str = ""
    truncated: bool = False
    size: int | None = None  # the whole file's, when it is the whole file


type ContentBlock = TextBlock | AttachmentRef | FileReference


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
    cache_write_tokens: int | None = None  # input written to the provider's cache (V3-P62 §9)


@dataclass(frozen=True)
class ProviderContinuation(DeepFrozen):
    """Opaque reasoning state a model returned with one assistant turn (V3-049),
    e.g. encrypted reasoning items or signed thinking blocks.

    Data stores it on the Run that produced it, taking ``provider_key`` and
    ``model_id`` from that Run's record; Prompt carries it on the matching
    history turn without reading it; only a Provider with the same key and
    model re-inserts ``payload`` into a native request.
    """

    provider_key: str
    model_id: str
    payload: JsonValue


class UnknownOutcomePolicy(Enum):
    """What happens when a claimed call's effect is uncertain (V3-047).

    Declared on the ToolDefinition and persisted on the ToolExecutionRecord
    at claim time, so recovery uses the policy that applied when the effect
    may have happened — not whatever the current build registers.
    """

    REPORT_TO_MODEL = "report_to_model"  # attach an "outcome unknown" result; the model verifies
    REQUIRE_USER = "require_user"  # halt the group and block the Conversation for a user
    # No user-resolution API exists in P0, so P0 must not register REQUIRE_USER Tools.


class RawRepresentation(Enum):
    """What a stored raw Provider request is equal to (V3-P14 is OPEN).

    Records must never claim more exactness than was achieved.
    """

    WIRE_EXACT = "wire_exact"
    ADAPTER_SERIALIZED = "adapter_serialized"
    # V3-P67 §7.4 (V3-048 amended): wire-exact but for image data, stored as
    # ``himari-blob:`` markers that the adapter expands when it sends: the
    # sent bytes can be rebuilt exactly while the blobs remain.
    WIRE_EXACT_BLOB_REFS = "wire_exact_blob_refs"


# The moods an answer may show with ``[[mood:NAME]]`` (V3-P83 §7.1): the
# contract between the server and a device's character. Adding one keeps old
# apps working (they draw an unknown mood as "neutral"); removing one does not.
MOODS = ("neutral", "happy", "excited", "sad", "surprised", "thinking", "embarrassed", "worried")
