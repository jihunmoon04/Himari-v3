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

# ``[[mood:NAME]]`` (V3-P83 §7.2). A name outside MOODS still comes out of
# the text, as "neutral"; inside code (a fenced block, or inline code closed
# on the same line within INLINE_CODE_CHARS) it is text.
MOOD_MARK = re.compile(r"\[\[mood:([a-z0-9_]{1,24})\]\]")
_MOOD_PREFIX = re.compile(r"\[(\[(m(o(o(d(:([a-z0-9_]{1,24}\]?)?)?)?)?)?)?)?")
_MOOD_SPECIAL = re.compile(r"[\[`\n]")
INLINE_CODE_CHARS = 200


@dataclass(frozen=True)
class MoodScan(DeepFrozen):
    """Where a split stopped: the raw text and the plain text confirmed so
    far, and what the next character is in — an open fence (its character
    and length) or not, at a line's start or not. ``scan_moods`` goes on
    from here, so a growing text is never counted from its start again."""

    raw: int = 0
    plain: int = 0
    fence: tuple[str, int] | None = None
    line_start: bool = True


def split_moods(text: str, *, complete: bool) -> tuple[str, tuple[tuple[int, str], ...], int]:
    """``text`` without its mood marks, each mark as ``(at, mood)`` where
    ``at`` counts code points of the plain text, and how much of ``text``
    that covers. ``complete=False`` (a text still growing) leaves an end
    that could still change meaning — the start of a mark, an inline code
    not yet closed, a line that may open or close a fence — for later; what
    it confirms is always the start of the complete text's split."""
    plain, moods, state = scan_moods(text, MoodScan(), complete=complete)
    return plain, moods, state.raw


def scan_moods(rest: str, state: MoodScan, *, complete: bool
               ) -> tuple[str, tuple[tuple[int, str], ...], MoodScan]:
    """``split_moods`` from ``state`` on: ``rest`` is the raw text from
    ``state.raw``. The ``at`` of a mood counts from the start of the whole
    plain text. Returns the newly confirmed plain text, its moods and the
    state after it."""
    out: list[str] = []
    moods: list[tuple[int, str]] = []
    plain = state.plain
    fence, line_start = state.fence, state.line_start
    i, n = 0, len(rest)

    def emit(piece: str) -> None:
        nonlocal plain
        out.append(piece)
        plain += len(piece)

    while i < n:
        if line_start:
            j = i
            while j < n and j - i < 3 and rest[j] == " ":
                j += 1
            if j == n and not complete:
                break  # spaces so far: the line may still open or close a fence
            if j < n and rest[j] in "`~":
                k = j
                while k < n and rest[k] == rest[j]:
                    k += 1
                if k == n and not complete:
                    break
                if k - j >= 3:
                    eol = rest.find("\n", k)
                    if eol == -1 and not complete:
                        break  # a fence line, known only once it ends
                    end = n if eol == -1 else eol + 1
                    char, run, info = rest[j], k - j, rest[k:end]
                    if fence is not None or not (char == "`" and "`" in info):  # else an ordinary line
                        if fence is None:
                            fence = (char, run)
                        elif char == fence[0] and run >= fence[1] and not info.strip():
                            fence = None
                        emit(rest[i:end])
                        i, line_start = end, eol != -1
                        continue
        if fence is not None:  # a line of code
            eol = rest.find("\n", i)
            end = n if eol == -1 else eol + 1
            emit(rest[i:end])
            i, line_start = end, eol != -1
            continue
        found = _MOOD_SPECIAL.search(rest, i)
        stop = n if found is None else found.start()
        if stop > i:
            emit(rest[i:stop])
            i, line_start = stop, False
            continue
        char = rest[i]
        if char == "\n":
            emit("\n")
            i, line_start = i + 1, True
            continue
        if char == "[":
            mark = MOOD_MARK.match(rest, i)
            if mark is not None:
                name = mark.group(1)
                moods.append((plain, name if name in MOODS else "neutral"))
                i, line_start = mark.end(), False
                continue
            if not complete and _MOOD_PREFIX.fullmatch(rest, i):
                break  # the start of a mark, perhaps
            emit("[")
            i, line_start = i + 1, False
            continue
        close = _inline_code_end(rest, i, complete)  # a backtick
        if close is None:
            break
        emit(rest[i:close])
        i, line_start = close, False
    return "".join(out), tuple(moods), MoodScan(state.raw + i, plain, fence, line_start)


def _inline_code_end(rest: str, i: int, complete: bool) -> int | None:
    """Where the inline code opened by the backticks at ``i`` ends (after its
    closing run), or the end of the opening run when it is not closed on
    its line within INLINE_CODE_CHARS; None while that is not known yet."""
    n = len(rest)
    k = i
    while k < n and rest[k] == "`":
        k += 1
    eol = rest.find("\n", k)
    line_end = n if eol == -1 else eol
    open_end = eol == -1 and not complete  # the line may still grow
    if k == n and open_end:
        return None
    j = k
    while True:
        p = rest.find("`", j, line_end)
        if p == -1 or p - k > INLINE_CODE_CHARS:
            if p == -1 and open_end and n - k <= INLINE_CODE_CHARS:
                return None
            return k
        q = p
        while q < n and rest[q] == "`":
            q += 1
        if q == n and open_end:
            return None
        if q - p == k - i:
            return q
        j = q
