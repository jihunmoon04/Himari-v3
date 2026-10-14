"""Prompt public contract: pure ``compose`` + thin context loader (V3-043).

Source documents: docs/prompt-composition.md, docs/control-prompt-provider-v0.1.md
§2 and §6.1, docs/current-state-contract-v0.1.md.

Composition order is fixed: Stable Prefix → Conversation (compressed + exact
tail, ToolCall groups as units) → Current State. Provider-specific options are
separate fields, never trailing message text.

Compaction (PROMPT-04, Q-14): when the next request is estimated to reach a
share of the token budget, everything older than a recent exact tail is
folded into one summary at once. Prompt decides mechanically *whether* and
*where* (``CompactionPlanner``, pure); the model only writes the summary;
Control makes the call and Data commits the summary as the latest
PromptSnapshot. Exact Events are never changed.
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
    JsonValue,
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
    INVALID_SUMMARY = "invalid_summary"  # a compaction output that cannot be used


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
    usage_mark: UsageMark | None = None


@dataclass(frozen=True)
class UsageMark(DeepFrozen):
    """The Provider-measured input size of the latest Run that reported one,
    with the basis that input was composed from. The next request's size is
    this plus an estimate of what was added after ``event_sequence`` — valid
    only while the snapshot is still ``snapshot_revision``."""

    input_tokens: int
    event_sequence: int
    snapshot_revision: int | None


@dataclass(frozen=True)
class ContextBudget(DeepFrozen):
    """Token budget of one model request (input side).

    ``limit_tokens``: an estimate above it is never sent. ``compact_at``: at
    or above this share of the limit the history is compacted before the
    Run. ``keep_recent``: the exact tail kept after compaction is at most
    this share (always at least the latest turn)."""

    limit_tokens: int
    compact_at: float = 0.8
    keep_recent: float = 0.25
    max_summary_chars: int = 24_000

    def __post_init__(self) -> None:
        if self.limit_tokens <= 0 or not (0 < self.keep_recent < self.compact_at <= 1):
            raise ValueError("budget needs limit_tokens > 0 and 0 < keep_recent < compact_at <= 1")


@dataclass(frozen=True)
class ContextEstimate(DeepFrozen):
    """``measured``: based on the Provider-reported size of an earlier
    request plus an estimate of the additions; otherwise estimated from
    UTF-8 size alone (deliberately pessimistic)."""

    tokens: int
    measured: bool


@dataclass(frozen=True)
class CompactionPlan(DeepFrozen):
    """One summarizing call: ``request`` has no tools and carries the folded
    history (and the previous summary) as data. Its summary replaces
    everything up to ``covers_through_sequence``."""

    request: ModelInput
    covers_through_sequence: int
    expected_revision: int | None  # the snapshot this plan extends
    folded_parts: int


@dataclass(frozen=True)
class ContextDecision(DeepFrozen):
    estimate: ContextEstimate
    compaction: CompactionPlan | None = None


class PromptComposer(Protocol):
    """PROMPT-01/03: pure and deterministic — same inputs, same ModelInput.
    Raises PromptError(INCOMPLETE_HISTORY) rather than inventing results."""

    def compose(self, inputs: PromptInputs) -> ModelInput: ...


class CompactionPlanner(Protocol):
    """PROMPT-04, pure: no I/O, no model calls."""

    def decide(self, inputs: PromptInputs, model_input: ModelInput, budget: ContextBudget) -> ContextDecision:
        """Estimate ``model_input`` (composed from ``inputs``) and, if it
        reaches ``budget.compact_at``, plan one compaction — None if there is
        nothing older than the tail to fold."""
        ...

    def estimate(self, inputs: PromptInputs, model_input: ModelInput) -> ContextEstimate: ...

    def summary_body(self, blocks: Sequence[ContentBlock], budget: ContextBudget) -> JsonValue:
        """The snapshot body for a compaction output; PromptError
        (INVALID_SUMMARY) if it is empty, too long or not plain text."""
        ...

    @property
    def snapshot_schema_version(self) -> int: ...


class PromptContextLoader(Protocol):
    """Reads Data sub-ports, Tool catalog and optional observations."""

    async def load(
        self,
        scope: TrustedScope,
        trigger_refs: Sequence[TriggerRef],
        request_options: RequestOptions,
    ) -> PromptInputs: ...
