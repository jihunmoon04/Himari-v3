"""Data public contract — consumer-specific sub-ports (V3-039).

Source documents: docs/data-contract-v0.2.md (§1.1, §4–§10), docs/contracts.md §6.
Sub-ports may share one backend and transaction; contract-change classification
(V3-038) is per sub-port. ``ControlState`` (DATA-09/10) is P2 and not declared.

Durability (V3-045): the in-process Conversation lock only prevents concurrent
drivers inside a live process. Crash duplication is prevented by the persisted
marks ``DISPATCH_CLAIMED`` (before Provider send) and ``EXECUTION_CLAIMED``
(before an effectful Tool run), plus restart cleanup (T-R).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Protocol

from himari.common import (
    DeepFrozen,
    ArtifactId,
    BindingId,
    CallRef,
    ContentBlock,
    ConversationId,
    EnvironmentInstanceId,
    EnvironmentKind,
    EventId,
    JsonValue,
    ModelToolResult,
    RawRepresentation,
    RunId,
    TokenUsage,
    ToolCall,
    ToolId,
    TrustedScope,
    WorkspaceId,
)

# --- Errors (Data v0.2 §11) --------------------------------------------------


class DataErrorCode(Enum):
    NOT_AUTHORIZED = "not_authorized"
    NOT_FOUND = "not_found"
    STALE_BASIS = "stale_basis"
    VERSION_CONFLICT = "version_conflict"
    ALREADY_COMMITTED = "already_committed"
    DUPLICATE_IDENTITY = "duplicate_identity"
    INVALID_EVENT_GROUP = "invalid_event_group"
    INCOMPLETE_TOOL_GROUP = "incomplete_tool_group"
    RAW_PERSISTENCE_UNAVAILABLE = "raw_persistence_unavailable"
    INVALID_STATE_TRANSITION = "invalid_state_transition"


class DataError(Exception):
    def __init__(self, code: DataErrorCode, message: str = "") -> None:
        super().__init__(f"{code.value}: {message}" if message else code.value)
        self.code = code


# --- Conversation Events (Data v0.2 §4) ---------------------------------------


class EventKind(Enum):
    USER_MESSAGE = "user_message"
    ASSISTANT_FINAL = "assistant_final"
    ASSISTANT_TOOL_CALLS = "assistant_tool_calls"
    TOOL_RESULT = "tool_result"


@dataclass(frozen=True)
class UserMessage(DeepFrozen):
    blocks: tuple[ContentBlock, ...]
    answers_call: CallRef | None = None  # set only via answer_pending_call (T7a)


@dataclass(frozen=True)
class AssistantFinal(DeepFrozen):
    blocks: tuple[ContentBlock, ...]


@dataclass(frozen=True)
class AssistantToolCalls(DeepFrozen):
    calls: tuple[ToolCall, ...]  # original order; ordinals unique
    commentary: tuple[ContentBlock, ...] = ()  # V3-P16


@dataclass(frozen=True)
class ToolResultRecord(DeepFrozen):
    call: CallRef
    model_result: ModelToolResult


type EventPayload = UserMessage | AssistantFinal | AssistantToolCalls | ToolResultRecord
type AssistantOutput = AssistantFinal | AssistantToolCalls


@dataclass(frozen=True)
class ConversationEvent(DeepFrozen):
    event_id: EventId
    conversation_id: ConversationId
    sequence: int  # per-Conversation, monotonic; the only chronology authority
    kind: EventKind
    payload: EventPayload
    payload_schema_version: int
    created_at: datetime  # observational only
    origin_run_id: RunId | None = None


# --- Runs (Data v0.2 §5) -----------------------------------------------------


class RunStatus(Enum):
    PREPARED = "prepared"
    DISPATCH_CLAIMED = "dispatch_claimed"  # P0: persisted right before send
    RECEIVED = "received"
    FAILED_NO_RESPONSE = "failed_no_response"
    OUTCOME_UNKNOWN = "outcome_unknown"
    PROJECTED = "projected"
    PROJECTION_FAILED = "projection_failed"
    ABANDONED_NOT_SENT = "abandoned_not_sent"


@dataclass(frozen=True)
class EventRef(DeepFrozen):
    event_id: EventId


@dataclass(frozen=True)
class CompletedGroupRef(DeepFrozen):
    run_id: RunId


type TriggerRef = EventRef | CompletedGroupRef


@dataclass(frozen=True)
class PromptBasis(DeepFrozen):
    """Versions of the sources a Run's input was composed from."""

    event_sequence: int
    state_revision: int
    snapshot_revision: int | None = None
    artifact_revisions: Mapping[ArtifactId, int] = field(default_factory=dict)


@dataclass(frozen=True)
class RunPreparation(DeepFrozen):
    """Everything persisted by ``prepare_run`` (T2) before any send."""

    raw_request_body: bytes
    raw_content_type: str
    raw_representation: RawRepresentation
    trigger_refs: tuple[TriggerRef, ...]
    prompt_basis: PromptBasis
    provider_key: str
    model_id: str


@dataclass(frozen=True)
class RawReceived(DeepFrozen):
    body: bytes
    content_type: str
    usage: TokenUsage | None = None


@dataclass(frozen=True)
class RawNoResponse(DeepFrozen):
    """Known failure: the Provider certainly produced no response."""

    error_code: str


@dataclass(frozen=True)
class RawUncertain(DeepFrozen):
    """Send may or may not have reached the Provider; never resent."""

    error_code: str


type RawRunOutcome = RawReceived | RawNoResponse | RawUncertain


@dataclass(frozen=True)
class RunRecord(DeepFrozen):
    run_id: RunId
    conversation_id: ConversationId
    status: RunStatus
    trigger_refs: tuple[TriggerRef, ...]
    prompt_basis: PromptBasis
    provider_key: str
    model_id: str
    raw_representation: RawRepresentation
    usage: TokenUsage | None = None
    error_code: str | None = None


# --- Tool executions (Data v0.2 §6) ----------------------------------------


class ToolExecutionStatus(Enum):
    PENDING = "pending"
    EXECUTION_CLAIMED = "execution_claimed"  # P0: persisted before effectful run
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"  # never a synthetic failure; blocks the next Run


@dataclass(frozen=True)
class ToolExecutionRecord(DeepFrozen):
    call: CallRef
    ordinal: int
    tool_id: ToolId
    arguments: Mapping[str, JsonValue]
    status: ToolExecutionStatus
    model_result: ModelToolResult | None = None  # what the model sees
    raw_result: JsonValue = None  # the Tool's own execution record; never sent to the model
    workspace_binding_id: BindingId | None = None


@dataclass(frozen=True)
class ProjectionResult(DeepFrozen):
    event: ConversationEvent
    pending: tuple[ToolExecutionRecord, ...]  # empty for AssistantFinal


# --- Artifacts (Data v0.2 §8) ----------------------------------------------


@dataclass(frozen=True)
class Artifact(DeepFrozen):
    artifact_id: ArtifactId
    kind: str  # e.g. "plan", "note"; Data does not interpret content
    revision: int
    content: JsonValue


@dataclass(frozen=True)
class ArtifactMutation(DeepFrozen):
    """Requested Plan/Note change carried by a ToolOutcome (V3-042).

    ``artifact_id=None`` creates a new Artifact; otherwise ``expected_revision``
    is checked (CAS) inside the same transaction as the Tool result.
    """

    kind: str
    content: JsonValue
    artifact_id: ArtifactId | None = None
    expected_revision: int | None = None


@dataclass(frozen=True)
class ToolCompletion(DeepFrozen):
    """Terminal outcome committed by the Tool Dispatcher (DATA-08, T7/T8)."""

    status: ToolExecutionStatus  # SUCCEEDED | FAILED | DENIED | CANCELLED
    model_result: ModelToolResult
    raw_result: JsonValue = None  # persisted on the ToolExecutionRecord
    artifact_mutations: tuple[ArtifactMutation, ...] = ()


@dataclass(frozen=True)
class AnswerCommit(DeepFrozen):
    user_event: ConversationEvent
    tool_result_event: ConversationEvent | None


@dataclass(frozen=True)
class RecoveryReport(DeepFrozen):
    """Result of restart cleanup (T-R). Nothing is re-executed."""

    runs_outcome_unknown: tuple[RunId, ...] = ()
    runs_abandoned_not_sent: tuple[RunId, ...] = ()
    tools_unknown: tuple[CallRef, ...] = ()


# --- Prompt snapshot (Data v0.2 §7) ----------------------------------------


@dataclass(frozen=True)
class PromptSnapshot(DeepFrozen):
    """Latest full provider-neutral prompt; one per Conversation.

    ``body`` is owned and interpreted by Prompt; Data stores it opaquely.
    """

    revision: int
    schema_version: int
    body: JsonValue
    covers_through_sequence: int
    basis: PromptBasis


# --- Operational state (Data v0.2 §8, CurrentState v0.1 §2) -----------------


@dataclass(frozen=True)
class BindingAuthority(DeepFrozen):
    """Current, authorized Binding metadata. Not an OS permission proof;
    the Environment re-validates before any effect."""

    binding_id: BindingId
    environment_instance_id: EnvironmentInstanceId
    environment_kind: EnvironmentKind
    workspace_id: WorkspaceId
    revision: int


@dataclass(frozen=True)
class ConversationWorkspace(DeepFrozen):
    workspace_id: WorkspaceId
    cwd_relative: str  # never an absolute host path


@dataclass(frozen=True)
class EnvironmentActivation(DeepFrozen):
    environment_instance_id: EnvironmentInstanceId
    environment_kind: EnvironmentKind  # P0: at most one active per kind (V3-P19)
    default_binding_id: BindingId | None = None


@dataclass(frozen=True)
class PersistedStateBasis(DeepFrozen):
    """One coherent, permission-filtered read (DATA-06)."""

    conversation_sequence: int
    state_revision: int
    environments: tuple[EnvironmentActivation, ...]
    bindings: tuple[BindingAuthority, ...]
    artifacts: tuple[Artifact, ...]
    conversation_workspace: ConversationWorkspace | None = None


# --- Sub-ports ---------------------------------------------------------------


class ConversationLog(Protocol):
    """DATA-01/02/03/05/08: events, Run ledger, Tool ledger, snapshot."""

    async def append_event(
        self, scope: TrustedScope, message: UserMessage, *, idempotency_key: str | None = None
    ) -> ConversationEvent:
        """T1: ordinary user input. While an ask_user call is waiting, Control
        must use ``answer_pending_call`` instead (one path per input)."""
        ...

    async def read_events(
        self, scope: TrustedScope, *, after_sequence: int = 0, limit: int | None = None
    ) -> Sequence[ConversationEvent]: ...

    async def prepare_run(self, scope: TrustedScope, preparation: RunPreparation) -> RunId:
        """T2: persist the request before any send. Raises
        RAW_PERSISTENCE_UNAVAILABLE when it cannot be stored — do not send."""
        ...

    async def mark_dispatch(self, scope: TrustedScope, run_id: RunId) -> None:
        """T3: ``PREPARED → DISPATCH_CLAIMED`` immediately before the one send.
        A Run already past PREPARED raises INVALID_STATE_TRANSITION."""
        ...

    async def record_raw_outcome(
        self, scope: TrustedScope, run_id: RunId, outcome: RawRunOutcome
    ) -> None:
        """T4: store the received body (``RECEIVED``) or failure class."""
        ...

    async def project_model_output(
        self, scope: TrustedScope, run_id: RunId, output: AssistantOutput
    ) -> ProjectionResult:
        """T5: assistant Event + every pending ToolExecution, atomically."""
        ...

    async def record_projection_failure(
        self, scope: TrustedScope, run_id: RunId, error_code: str
    ) -> None: ...

    async def read_run(self, scope: TrustedScope, run_id: RunId) -> RunRecord: ...

    async def read_tool_executions(
        self, scope: TrustedScope, run_id: RunId
    ) -> Sequence[ToolExecutionRecord]:
        """Ordered by original ordinal, not completion time."""
        ...

    async def claim_tool_execution(self, scope: TrustedScope, call: CallRef) -> None:
        """T6: ``PENDING → EXECUTION_CLAIMED`` before an effectful handler runs."""
        ...

    async def commit_tool_outcome(
        self, scope: TrustedScope, call: CallRef, completion: ToolCompletion
    ) -> ConversationEvent | None:
        """T7/T8 (DATA-08): terminal state + model result + raw result +
        Artifact mutations (CAS) + configured ``tool_result`` Event in one
        transaction.
        Called by the Tool Dispatcher only. Returns the Event if recorded."""
        ...

    async def find_waiting_call(self, scope: TrustedScope) -> CallRef | None:
        """Derived wait state (V3-P17): the latest PROJECTED Run's group is a
        single ``ask_user`` call still PENDING."""
        ...

    async def answer_pending_call(
        self, scope: TrustedScope, call: CallRef, answer: UserMessage
    ) -> AnswerCommit:
        """T7a: ``user_message`` (with ``answers_call``) + ToolExecution
        ``PENDING → SUCCEEDED`` + ``tool_result`` Event, atomically.
        Raises ALREADY_COMMITTED if the call is no longer waiting."""
        ...

    async def recover_interrupted(self, scope: TrustedScope) -> RecoveryReport:
        """T-R: DISPATCH_CLAIMED without response → OUTCOME_UNKNOWN,
        EXECUTION_CLAIMED without result → UNKNOWN, unclaimed PREPARED →
        ABANDONED_NOT_SENT. Never re-executes anything."""
        ...

    async def get_latest_snapshot(self, scope: TrustedScope) -> PromptSnapshot | None: ...

    async def replace_latest_snapshot(
        self,
        scope: TrustedScope,
        *,
        expected_revision: int | None,
        schema_version: int,
        body: JsonValue,
        covers_through_sequence: int,
        basis: PromptBasis,
    ) -> int:
        """T9: latest-only CAS replace; returns the new revision."""
        ...


class ArtifactStore(Protocol):
    """DATA-04: read side. Writes go through ``commit_tool_outcome``."""

    async def read_artifact(self, scope: TrustedScope, artifact_id: ArtifactId) -> Artifact: ...

    async def list_artifacts(
        self, scope: TrustedScope, *, kind: str | None = None
    ) -> Sequence[Artifact]: ...


class WorkspaceState(Protocol):
    """DATA-07/11: Registry, Binding, cwd mapping. (FILE-02 metadata: later.)"""

    async def read_binding_authority(
        self, scope: TrustedScope, binding_id: BindingId
    ) -> BindingAuthority: ...

    async def resolve_default_binding(
        self, scope: TrustedScope, environment_kind: EnvironmentKind
    ) -> BindingAuthority | None:
        """``workspace_requirement = DEFAULT`` resolution."""
        ...

    async def resolve_conversation_workspace(
        self, scope: TrustedScope
    ) -> ConversationWorkspace: ...


class StateQuery(Protocol):
    """DATA-06: coherent read for CurrentStateProjection."""

    async def read_current_state_basis(self, scope: TrustedScope) -> PersistedStateBasis: ...


__all__ = [
    "AnswerCommit",
    "Artifact",
    "ArtifactMutation",
    "ArtifactStore",
    "AssistantFinal",
    "AssistantOutput",
    "AssistantToolCalls",
    "BindingAuthority",
    "CompletedGroupRef",
    "ConversationEvent",
    "ConversationLog",
    "ConversationWorkspace",
    "DataError",
    "DataErrorCode",
    "EnvironmentActivation",
    "EventKind",
    "EventPayload",
    "EventRef",
    "PersistedStateBasis",
    "ProjectionResult",
    "PromptBasis",
    "PromptSnapshot",
    "RawNoResponse",
    "RawReceived",
    "RawRunOutcome",
    "RawUncertain",
    "RecoveryReport",
    "RunPreparation",
    "RunRecord",
    "RunStatus",
    "StateQuery",
    "ToolCompletion",
    "ToolExecutionRecord",
    "ToolExecutionStatus",
    "ToolResultRecord",
    "TriggerRef",
    "UserMessage",
    "WorkspaceState",
]
