"""Prompt public contract: pure ``compose`` + thin context loader (V3-043).

Source documents: docs/prompt-composition.md, docs/control-prompt-provider-v0.1.md
§2 and §6.1, docs/current-state-contract-v0.1.md.

Composition order is fixed: Stable Prefix → Conversation (compressed + exact
tail, ToolCall groups as units) → Current State. Provider-specific options are
separate fields, never trailing message text.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from himari.common import (
    CallId,
    ContentBlock,
    DeepFrozen,
    EventId,
    FrozenMap,
    ModelToolResult,
    ProviderContinuation,
    RunId,
    ToolCall,
    TrustedScope,
)
from himari.data.api import (
    ConversationEvent,
    PersistedStateBasis,
    PromptBasis,
    PromptSnapshot,
    ToolExecutionRecord,
    TriggerRef,
)
from himari.environment.api import BindingObservation
from himari.tool.api import ToolDefinition


class PromptErrorCode(Enum):
    INCOMPLETE_HISTORY = "incomplete_history"  # missing/UNKNOWN/unpaired tool result
    STALE_BASIS = "stale_basis"


class PromptError(Exception):
    def __init__(self, code: PromptErrorCode, message: str = "") -> None:
        super().__init__(f"{code.value}: {message}" if message else code.value)
        self.code = code


@dataclass(frozen=True)
class RequestOptions(DeepFrozen):
    model_id: str
    reasoning_effort: str | None = None


# --- Provider-neutral history (CPP §6.1) ------------------------------------


@dataclass(frozen=True)
class CompressedHistory(DeepFrozen):
    summary: tuple[ContentBlock, ...]
    covers_through_sequence: int


@dataclass(frozen=True)
class UserTurn(DeepFrozen):
    event_id: EventId
    blocks: tuple[ContentBlock, ...]


@dataclass(frozen=True)
class AssistantTurn(DeepFrozen):
    event_id: EventId
    blocks: tuple[ContentBlock, ...]
    continuation: ProviderContinuation | None = None  # opaque; only a Provider reads it


@dataclass(frozen=True)
class ToolGroupTurn(DeepFrozen):
    """An assistant ToolCall group immediately followed by its results in
    original ordinal order. An ask_user answer appears only here, as the
    result of its call — never again as a UserTurn (V3-P18)."""

    run_id: RunId
    commentary: tuple[ContentBlock, ...]
    calls: tuple[ToolCall, ...]
    results: Mapping[CallId, ModelToolResult]  # complete: one per call
    continuation: ProviderContinuation | None = None  # opaque; only a Provider reads it


type HistoryPart = CompressedHistory | UserTurn | AssistantTurn | ToolGroupTurn


@dataclass(frozen=True)
class CurrentStateProjection(DeepFrozen):
    """Read projection, not a source of truth or an execution permission."""

    basis: PersistedStateBasis
    observations: tuple[BindingObservation, ...] = ()


@dataclass(frozen=True)
class ModelInput(DeepFrozen):
    stable_prefix: tuple[str, ...]
    tool_catalog: tuple[ToolDefinition, ...]
    conversation: tuple[HistoryPart, ...]
    current_state: CurrentStateProjection
    request_options: RequestOptions
    trigger_refs: tuple[TriggerRef, ...]
    prompt_basis: PromptBasis


@dataclass(frozen=True)
class PromptInputs(DeepFrozen):
    """Everything ``compose`` needs, gathered by the loader. No I/O handles."""

    instructions: tuple[str, ...]
    tool_catalog: tuple[ToolDefinition, ...]
    events: tuple[ConversationEvent, ...]
    tool_executions: tuple[ToolExecutionRecord, ...]
    snapshot: PromptSnapshot | None
    state_basis: PersistedStateBasis
    observations: tuple[BindingObservation, ...]
    request_options: RequestOptions
    trigger_refs: tuple[TriggerRef, ...]
    continuations: Mapping[RunId, ProviderContinuation] = FrozenMap()  # by producing Run


class PromptComposer(Protocol):
    """PROMPT-01/03: pure and deterministic — same inputs, same ModelInput.
    Raises PromptError(INCOMPLETE_HISTORY) rather than inventing results."""

    def compose(self, inputs: PromptInputs) -> ModelInput: ...


class PromptContextLoader(Protocol):
    """Reads Data sub-ports, Tool catalog and optional observations."""

    async def load(
        self,
        scope: TrustedScope,
        trigger_refs: Sequence[TriggerRef],
        request_options: RequestOptions,
    ) -> PromptInputs: ...
