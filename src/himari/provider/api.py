"""Provider public contract: the ProviderAdapter SPI (PROVIDER-01~03).

Source documents: docs/control-prompt-provider-v0.1.md §4–§6, docs/contracts.md §8.

One Run = exactly one ``invoke_prepared`` attempt. Adapters must disable
implicit HTTP retries. Credentials are adapter configuration, never part of
the prepared body that Data stores.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from himari.common import ASK_USER_TOOL_ID, ContentBlock, DeepFrozen, RawRepresentation, TokenUsage, ToolCall
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


@dataclass(frozen=True)
class NativePreparedRequest(DeepFrozen):
    body: bytes
    content_type: str
    raw_representation: RawRepresentation  # what ``body`` equals (V3-P14 OPEN)
    endpoint: str  # transport descriptor without credentials
    digest: str


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


@dataclass(frozen=True)
class NormalizedFinal(DeepFrozen):
    blocks: tuple[ContentBlock, ...]
    continuation_ref: str | None = None


@dataclass(frozen=True)
class NormalizedToolCalls(DeepFrozen):
    calls: tuple[ToolCall, ...]
    commentary: tuple[ContentBlock, ...] = ()  # V3-P16: not a final signal
    continuation_ref: str | None = None


type NormalizedModelOutput = NormalizedFinal | NormalizedToolCalls


@runtime_checkable
class ProviderAdapter(Protocol):
    @property
    def provider_key(self) -> str: ...

    def describe_capabilities(self, model_id: str) -> ProviderCapabilities: ...

    def prepare_native_request(self, model_input: ModelInput) -> NativePreparedRequest:
        """PROVIDER-02: no network. Validates native ToolCall/ToolResult
        adjacency and Current State placement; raises ProviderError instead of
        sending an illegal request."""
        ...

    async def invoke_prepared(self, request: NativePreparedRequest) -> NativeInvocationOutcome:
        """PROVIDER-01: at most one send of exactly this prepared request."""
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
