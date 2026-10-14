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
    # The Provider the request is for, set by the Driver. Prompt never
    # interprets it; it only decides which earlier measurements still apply.
    provider_key: str | None = None


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
    # With a readable compaction snapshot, only the Events after its coverage
    # and their Tool records (Q-42); otherwise the whole history.
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
    context_key: str | None = None  # PromptBasis.context_key of that Run
    state_revision: int | None = None
    provider_key: str | None = None  # the Provider that measured it (RunRecord)


@dataclass(frozen=True)
class ContextBudget(DeepFrozen):
    """Token budget of model requests (input side).

    ``limit_tokens``: the working budget. At or above ``compact_at`` of it
    the history is compacted before the Run; the exact tail kept is at most
    ``keep_recent`` of it (always at least the latest turn); every
    summarizing request fits within it. ``hard_limit_tokens``: a Run whose
    estimate is above it is never sent — the model's context window less
    the room kept for its answer, or the budget itself when the window is
    unknown (None)."""

    limit_tokens: int
    compact_at: float = 0.8
    keep_recent: float = 0.25
    max_summary_chars: int = 24_000
    hard_limit_tokens: int | None = None

    def __post_init__(self) -> None:
        if self.limit_tokens <= 0 or not (0 < self.keep_recent < self.compact_at <= 1):
            raise ValueError("budget needs limit_tokens > 0 and 0 < keep_recent < compact_at <= 1")
        if self.hard_limit_tokens is not None and self.hard_limit_tokens < self.limit_tokens:
            raise ValueError("the hard limit cannot be below the budget")

    @property
    def ceiling_tokens(self) -> int:
        return self.limit_tokens if self.hard_limit_tokens is None else self.hard_limit_tokens


@dataclass(frozen=True)
class ContextEstimate(DeepFrozen):
    """``measured``: based on the Provider-reported size of an earlier
    request plus an estimate of the additions; otherwise estimated from
    UTF-8 size alone (deliberately pessimistic)."""

    tokens: int
    measured: bool


@dataclass(frozen=True)
class CompactionPlan(DeepFrozen):
    """One compaction: a chain of tool-less summarizing calls, one per
    piece of the folded history (usually one). Call ``i`` gets the summary
    so far (``previous_summary`` for the first) and piece ``i`` as data;
    the last call's summary replaces everything up to
    ``covers_through_sequence``. Pieces are opaque to Control."""

    pieces: tuple[JsonValue, ...]
    covers_through_sequence: int
    expected_revision: int | None  # the snapshot this plan extends
    folded_parts: int
    previous_summary: str | None
    request_options: RequestOptions
    prompt_basis: PromptBasis
    # Longest summary any call may return, in characters (and estimated
    # tokens): the room the next call of the chain reserves for it.
    summary_limit: int


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
        nothing older than the tail to fold, or if not even a chain of
        pieces of the oldest part fits summarizing requests within
        ``budget.limit_tokens``. A plan may fold less than everything before
        the tail so that its requests fit; another compaction can then
        continue from it."""
        ...

    def estimate(self, inputs: PromptInputs, model_input: ModelInput) -> ContextEstimate: ...

    def plan_now(self, inputs: PromptInputs, model_input: ModelInput, budget: ContextBudget) -> CompactionPlan | None:
        """Like ``decide`` but whatever the size (a manual compaction): None
        only if there is nothing older than the latest part, or it cannot fit
        summarizing requests within the budget."""
        ...

    def request_size(self, model_input: ModelInput) -> int:
        """Pessimistic size of exactly this request, from its content alone."""
        ...

    def step_request(self, plan: CompactionPlan, index: int, summary: str | None) -> ModelInput:
        """The summarizing request for piece ``index``, given the summary so far."""
        ...

    def summary_text(self, blocks: Sequence[ContentBlock], plan: CompactionPlan) -> str:
        """A compaction output as summary text; PromptError (INVALID_SUMMARY)
        if it is empty, over ``plan.summary_limit`` or not plain text."""
        ...

    def snapshot_body(self, summary: str) -> JsonValue: ...

    def summary_of(self, snapshot: PromptSnapshot | None) -> str | None:
        """The summary a snapshot holds, if it is a readable compaction snapshot."""
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
