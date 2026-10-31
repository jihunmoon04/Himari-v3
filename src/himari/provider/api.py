"""Provider public contract: the ProviderAdapter SPI (PROVIDER-01~03).

Source documents: docs/control-prompt-provider-v0.1.md §4–§6, docs/contracts.md §8.

One Run = exactly one ``invoke_prepared`` attempt. Adapters must disable
implicit HTTP retries. Credentials are adapter configuration, never part of
the prepared body that Data stores.

Streaming (V3-P73 §3.5): a request prepared with ``streamed`` is invoked
with an ``OutputSink`` for what the model writes, shown while it writes,
and a ``RawCapture`` that the Driver owns: the adapter appends every body
byte it receives there before parsing it, so whatever arrived is there
however the invocation ends (returned, cancelled, raised).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from himari.common import (
    ASK_USER_TOOL_ID,
    ContentBlock,
    DeepFrozen,
    JsonValue,
    RawRepresentation,
    TokenUsage,
    ToolCall,
)
from himari.prompt.api import ModelInput


class ProviderErrorCode(Enum):
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    INVALID_NATIVE_ORDER = "invalid_native_order"  # no legal native rendering
    INCOMPLETE_HISTORY = "incomplete_history"
    INVALID_MODEL_OUTPUT = "invalid_model_output"  # incl. ask_user mixed (V3-P15)


class ProviderError(Exception):
    def __init__(self, code: ProviderErrorCode, message: str = "") -> None:
        super().__init__(f"{code.value}: {message}" if message else code.value)
        self.code = code


@dataclass(frozen=True)
class ProviderCapabilities(DeepFrozen):
    model_id: str
    reasoning_efforts: frozenset[str] = frozenset()
    parallel_tool_calls: bool = False
    tool_result_attachments: bool = False
    context_tokens: int | None = None  # the model's context window, if known
    max_output_tokens: int | None = None  # the most one answer may take of that window, if known
    # A marked cache breakpoint is accepted and honoured (V3-P62 §9 B2); only
    # where it was measured — unverified models get the request unchanged.
    cache_breakpoints: bool = False
    # Images in user messages are read (V3-P67 §7.1): only for models checked
    # with real requests; unlisted models do not get images.
    image_input: bool = False
    # Answers may be streamed (V3-P73 §9): only for models whose streamed
    # continuation was checked with real requests, replayed in a Tool turn.
    streaming: bool = False
    # Points on a screen image are where the model says (V3-P84 §5): only for
    # models whose [[move:X,Y]] fell on the asked target in real requests.
    screen_points: bool = False
    # Memory passes may use it (V3-P85 §4.8, Q6): only for models whose answers
    # passed the live memory check (well-formed, grounded, sensitive words marked).
    memory_passes: bool = False


@runtime_checkable
class ReadsImageCopies(Protocol):
    """A Provider that sends stored image copies (V3-P67 §7.4) is given a
    reader of them by the composition root once the file store exists."""

    def use_blobs(self, read: Callable[[str], bytes]) -> None: ...


@dataclass(frozen=True)
class NativePreparedRequest(DeepFrozen):
    body: bytes
    content_type: str
    raw_representation: RawRepresentation  # what ``body`` equals (V3-P14 OPEN)
    endpoint: str  # transport descriptor without credentials
    digest: str
    streamed: bool = False  # the body asks for a stream (V3-P73 §3.1)


@dataclass(frozen=True)
class NativeReceived(DeepFrozen):
    body: bytes
    content_type: str
    usage: TokenUsage | None = None


@dataclass(frozen=True)
class NativeNoResponse(DeepFrozen):
    """Definite failure: no usable model output. If the provider did answer
    (e.g. HTTP 429/500 or an error body), ``body`` is that exact received
    response so Data can keep it (V3-018); it is never normalized."""

    error_code: str
    body: bytes | None = None
    content_type: str = ""


@dataclass(frozen=True)
class NativeUncertain(DeepFrozen):
    error_code: str


type NativeInvocationOutcome = NativeReceived | NativeNoResponse | NativeUncertain

MAX_CAPTURE_BYTES = 32 * 1024 * 1024  # of one response (V3-P73 §3.4)


@dataclass(frozen=True)
class OutputDelta(DeepFrozen):
    """A piece of what the model is writing, for display only (V3-P73 §3.5)."""

    text: str = ""  # appended to the answer (or the commentary before tool calls)
    tool: str | None = None  # a tool call started: its Tool id
    thinking: bool = False  # reasoning arrived; its content is never passed on


type OutputSink = Callable[[OutputDelta], None]


class RawCapture:
    """The bytes of one Run's response as they arrive (V3-P73 §5.3). The
    Driver makes one per streamed Run and reads it once the invocation has
    ended; the adapter only appends, never past ``MAX_CAPTURE_BYTES``."""

    def __init__(self) -> None:
        self.content_type = ""
        self._chunks: list[bytes] = []
        self.size = 0

    def append(self, chunk: bytes) -> bool:
        """Keep ``chunk``; False (and nothing kept) if it would pass the limit."""
        if self.size + len(chunk) > MAX_CAPTURE_BYTES:
            return False
        self._chunks.append(chunk)
        self.size += len(chunk)
        return True

    def body(self) -> bytes | None:
        return b"".join(self._chunks) if self._chunks else None


@dataclass(frozen=True)
class NormalizedFinal(DeepFrozen):
    blocks: tuple[ContentBlock, ...]
    continuation: JsonValue = None  # opaque, provider-format reasoning state (V3-049)


@dataclass(frozen=True)
class NormalizedToolCalls(DeepFrozen):
    calls: tuple[ToolCall, ...]
    commentary: tuple[ContentBlock, ...] = ()  # V3-P16: not a final signal
    continuation: JsonValue = None  # opaque, provider-format reasoning state (V3-049)


type NormalizedModelOutput = NormalizedFinal | NormalizedToolCalls


@runtime_checkable
class ProviderAdapter(Protocol):
    @property
    def provider_key(self) -> str: ...

    def describe_capabilities(self, model_id: str) -> ProviderCapabilities: ...

    def prepare_native_request(self, model_input: ModelInput) -> NativePreparedRequest:
        """PROVIDER-02: no network. Validates native ToolCall/ToolResult
        adjacency and Current State placement; raises ProviderError instead of
        sending an illegal request. Re-inserts a turn's ``continuation`` only
        when its provider_key and model_id match this request (V3-049)."""
        ...

    async def invoke_prepared(self, request: NativePreparedRequest, sink: OutputSink | None = None,
                              capture: RawCapture | None = None) -> NativeInvocationOutcome:
        """PROVIDER-01: at most one send of exactly this prepared request.
        ``sink`` and ``capture`` are given only for a ``streamed`` request
        (V3-P73); an exception from ``sink`` is the adapter's to swallow."""
        ...

    def normalize(self, received: NativeReceived) -> NormalizedModelOutput:
        """PROVIDER-03: ``FINAL | TOOL_CALLS`` only. Raises
        ProviderError(INVALID_MODEL_OUTPUT); the raw body is already stored."""
        ...


def validate_tool_calls(calls: Sequence[ToolCall]) -> None:
    """Shared normalization rule for all adapters: non-empty, unique call ids,
    ordinals exactly ``0..n-1`` in listed order, ``ask_user`` only as the sole
    call (V3-P15)."""
    if not calls:
        raise ProviderError(ProviderErrorCode.INVALID_MODEL_OUTPUT, "empty tool call group")
    if len({c.call_id for c in calls}) != len(calls):
        raise ProviderError(ProviderErrorCode.INVALID_MODEL_OUTPUT, "duplicate call_id")
    if [c.ordinal for c in calls] != list(range(len(calls))):
        raise ProviderError(ProviderErrorCode.INVALID_MODEL_OUTPUT, "ordinals must be 0..n-1 in order")
    if len(calls) > 1 and any(c.tool_id == ASK_USER_TOOL_ID for c in calls):
        raise ProviderError(ProviderErrorCode.INVALID_MODEL_OUTPUT, "ask_user mixed with other calls")
