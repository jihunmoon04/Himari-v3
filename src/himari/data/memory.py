"""In-memory Data implementation of all P0 sub-ports.

The ``InMemoryStore`` object plays the role of durable storage: a "process
restart" is modelled by building a new driver over the same store. Every port
call validates first and mutates last, so each call is all-or-nothing under
single-threaded asyncio (the transaction boundaries T1–T9, T7a, T-R).
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from himari.common import (
    ASK_USER_TOOL_ID,
    ArtifactId,
    BindingId,
    CallRef,
    ConversationId,
    EnvironmentInstanceId,
    EnvironmentKind,
    EventId,
    JsonValue,
    ModelToolResult,
    PrincipalId,
    ProviderContinuation,
    RunId,
    TrustedScope,
    UnknownOutcomePolicy,
    WorkspaceId,
)
from himari.data.api import (
    AnswerCommit,
    Artifact,
    ArtifactMutation,
    AssistantFinal,
    AssistantOutput,
    AssistantToolCalls,
    BindingAuthority,
    ConversationEvent,
    ConversationWorkspace,
    DataError,
    DataErrorCode,
    EnvironmentActivation,
    EventKind,
    EventPayload,
    PersistedStateBasis,
    ProjectionResult,
    PromptBasis,
    PromptSnapshot,
    RawNoResponse,
    RawReceived,
    RawRunOutcome,
    RawUncertain,
    RecoveryReport,
    RunPreparation,
    RunRecord,
    RunStatus,
    ToolCompletion,
    ToolExecutionRecord,
    ToolExecutionStatus,
    ToolResultRecord,
    UserMessage,
)

_TERMINAL_COMPLETIONS = frozenset(
    {
        ToolExecutionStatus.SUCCEEDED,
        ToolExecutionStatus.FAILED,
        ToolExecutionStatus.DENIED,
        ToolExecutionStatus.CANCELLED,
    }
)

_KIND_OF: dict[type, EventKind] = {
    UserMessage: EventKind.USER_MESSAGE,
    AssistantFinal: EventKind.ASSISTANT_FINAL,
    AssistantToolCalls: EventKind.ASSISTANT_TOOL_CALLS,
    ToolResultRecord: EventKind.TOOL_RESULT,
}


@dataclass
class _Run:
    record: RunRecord
    raw_request_body: bytes
    raw_response_body: bytes | None = None
    raw_response_content_type: str = ""
    projected_event_id: EventId | None = None


@dataclass
class _Conversation:
    owner: PrincipalId
    events: list[ConversationEvent] = field(default_factory=list)
    runs: dict[RunId, _Run] = field(default_factory=dict)  # insertion order = creation order
    executions: dict[CallRef, ToolExecutionRecord] = field(default_factory=dict)
    artifacts: dict[ArtifactId, Artifact] = field(default_factory=dict)
    snapshot: PromptSnapshot | None = None
    state_revision: int = 0
    activations: dict[EnvironmentInstanceId, EnvironmentActivation] = field(default_factory=dict)
    bindings: dict[BindingId, BindingAuthority] = field(default_factory=dict)
    workspace: ConversationWorkspace | None = None
    idempotency: dict[str, EventId] = field(default_factory=dict)


class InMemoryStore:
    """Durable-storage stand-in plus admin helpers (not part of any port)."""

    def __init__(self) -> None:
        self._conversations: dict[ConversationId, _Conversation] = {}
        self._ids = itertools.count(1)

    # --- admin helpers (trusted setup paths, not model-reachable) --------

    def create_conversation(self, scope: TrustedScope) -> None:
        if scope.conversation_id in self._conversations:
            raise DataError(DataErrorCode.DUPLICATE_IDENTITY, scope.conversation_id)
        self._conversations[scope.conversation_id] = _Conversation(owner=scope.principal_id)

    def activate_environment(
        self,
        scope: TrustedScope,
        kind: EnvironmentKind,
        workspace_id: WorkspaceId,
        *,
        cwd_relative: str | None = None,
    ) -> BindingAuthority:
        conv = self._authorized(scope)
        if any(a.environment_kind == kind for a in conv.activations.values()):
            raise DataError(DataErrorCode.DUPLICATE_IDENTITY, f"active {kind} exists (P0: one per kind)")
        instance_id = EnvironmentInstanceId(self.new_id("env"))
        binding = BindingAuthority(
            binding_id=BindingId(self.new_id("bind")),
            environment_instance_id=instance_id,
            environment_kind=kind,
            workspace_id=workspace_id,
            revision=1,
        )
        conv.activations[instance_id] = EnvironmentActivation(instance_id, kind, binding.binding_id)
        conv.bindings[binding.binding_id] = binding
        if cwd_relative is not None:
            conv.workspace = ConversationWorkspace(workspace_id, cwd_relative)
        conv.state_revision += 1
        return binding

    def revoke_binding(self, scope: TrustedScope, binding_id: BindingId) -> None:
        conv = self._authorized(scope)
        conv.bindings.pop(binding_id, None)
        for iid, act in list(conv.activations.items()):
            if act.default_binding_id == binding_id:
                conv.activations[iid] = replace(act, default_binding_id=None)
        conv.state_revision += 1

    def deactivate_environment(self, scope: TrustedScope, kind: EnvironmentKind) -> None:
        """Ends the activation; Bindings and the Workspace itself remain (V3-009)."""
        conv = self._authorized(scope)
        for iid, act in list(conv.activations.items()):
            if act.environment_kind == kind:
                del conv.activations[iid]
        conv.state_revision += 1

    def raw_request_bodies(self, scope: TrustedScope) -> list[bytes]:
        """Test/audit helper: every persisted raw request, in Run order."""
        return [r.raw_request_body for r in self._authorized(scope).runs.values()]

    # --- internals ---------------------------------------------------------

    def new_id(self, prefix: str) -> str:
        return f"{prefix}_{next(self._ids)}"

    def _authorized(self, scope: TrustedScope) -> _Conversation:
        conv = self._conversations.get(scope.conversation_id)
        if conv is None or conv.owner != scope.principal_id:
            # Same error for foreign and missing (indistinguishability).
            raise DataError(DataErrorCode.NOT_AUTHORIZED, scope.conversation_id)
        return conv


class InMemoryData:
    """Implements ConversationLog, ArtifactStore, WorkspaceState, StateQuery."""

    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    # --- helpers -------------------------------------------------------------

    def _conv(self, scope: TrustedScope) -> _Conversation:
        return self._store._authorized(scope)

    def _new_event(
        self, conv: _Conversation, scope: TrustedScope, payload: EventPayload, origin: RunId | None
    ) -> ConversationEvent:
        event = ConversationEvent(
            event_id=EventId(self._store.new_id("evt")),
            conversation_id=scope.conversation_id,
            sequence=len(conv.events) + 1,
            kind=_KIND_OF[type(payload)],
            payload=payload,
            payload_schema_version=1,
            created_at=datetime.now(UTC),
            origin_run_id=origin,
        )
        conv.events.append(event)
        return event

    def _run(self, conv: _Conversation, run_id: RunId) -> _Run:
        run = conv.runs.get(run_id)
        if run is None:
            raise DataError(DataErrorCode.NOT_FOUND, run_id)
        return run

    @staticmethod
    def _set_status(run: _Run, status: RunStatus, **changes: object) -> None:
        run.record = replace(run.record, status=status, **changes)  # type: ignore[arg-type]

    def _waiting(self, conv: _Conversation) -> CallRef | None:
        projected = [r for r in conv.runs.values() if r.record.status is RunStatus.PROJECTED]
        if not projected:
            return None
        run_id = projected[-1].record.run_id
        group = [e for e in conv.executions.values() if e.call.run_id == run_id]
        if (
            len(group) == 1
            and group[0].tool_id == ASK_USER_TOOL_ID
            and group[0].status is ToolExecutionStatus.PENDING
        ):
            return group[0].call
        return None

    # --- ConversationLog --------------------------------------------------

    async def append_event(
        self, scope: TrustedScope, message: UserMessage, *, idempotency_key: str | None = None
    ) -> ConversationEvent:
        conv = self._conv(scope)
        if idempotency_key is not None and idempotency_key in conv.idempotency:
            existing_id = conv.idempotency[idempotency_key]
            existing = next(e for e in conv.events if e.event_id == existing_id)
            if existing.payload != message:
                raise DataError(DataErrorCode.DUPLICATE_IDENTITY, f"idempotency key {idempotency_key!r} reused")
            return existing
        if message.answers_call is not None:
            raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, "answers go through T7a")
        if self._waiting(conv) is not None:
            raise DataError(
                DataErrorCode.INVALID_STATE_TRANSITION, "an ask_user call is waiting; use answer_pending_call"
            )
        event = self._new_event(conv, scope, message, None)
        if idempotency_key is not None:
            conv.idempotency[idempotency_key] = event.event_id
        return event

    async def read_events(
        self, scope: TrustedScope, *, after_sequence: int = 0, limit: int | None = None
    ) -> Sequence[ConversationEvent]:
        events = [e for e in self._conv(scope).events if e.sequence > after_sequence]
        return tuple(events if limit is None else events[:limit])

    async def prepare_run(self, scope: TrustedScope, preparation: RunPreparation) -> RunId:
        conv = self._conv(scope)
        run_id = RunId(self._store.new_id("run"))
        conv.runs[run_id] = _Run(
            record=RunRecord(
                run_id=run_id,
                conversation_id=scope.conversation_id,
                status=RunStatus.PREPARED,
                trigger_refs=preparation.trigger_refs,
                prompt_basis=preparation.prompt_basis,
                provider_key=preparation.provider_key,
                model_id=preparation.model_id,
                raw_representation=preparation.raw_representation,
            ),
            raw_request_body=preparation.raw_request_body,
        )
        return run_id

    async def mark_dispatch(self, scope: TrustedScope, run_id: RunId) -> None:
        run = self._run(self._conv(scope), run_id)
        if run.record.status is not RunStatus.PREPARED:
            raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"{run_id} is {run.record.status.value}")
        self._set_status(run, RunStatus.DISPATCH_CLAIMED)

    async def record_raw_outcome(
        self, scope: TrustedScope, run_id: RunId, outcome: RawRunOutcome
    ) -> None:
        run = self._run(self._conv(scope), run_id)
        if run.record.status is not RunStatus.DISPATCH_CLAIMED:
            raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"{run_id} is {run.record.status.value}")
        match outcome:
            case RawReceived(body=body, content_type=content_type, usage=usage):
                run.raw_response_body = body
                run.raw_response_content_type = content_type
                self._set_status(run, RunStatus.RECEIVED, usage=usage)
            case RawNoResponse(error_code=code, body=body, content_type=content_type):
                run.raw_response_body = body  # the received error response, if any
                run.raw_response_content_type = content_type
                self._set_status(run, RunStatus.FAILED_NO_RESPONSE, error_code=code)
            case RawUncertain(error_code=code):
                self._set_status(run, RunStatus.OUTCOME_UNKNOWN, error_code=code)

    async def project_model_output(
        self,
        scope: TrustedScope,
        run_id: RunId,
        output: AssistantOutput,
        *,
        continuation: JsonValue = None,
    ) -> ProjectionResult:
        conv = self._conv(scope)
        run = self._run(conv, run_id)
        if run.record.status is not RunStatus.RECEIVED:
            raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"{run_id} is {run.record.status.value}")
        pending: list[ToolExecutionRecord] = []
        if isinstance(output, AssistantToolCalls):
            calls = output.calls
            if (
                not calls
                or len({c.call_id for c in calls}) != len(calls)
                or len({c.ordinal for c in calls}) != len(calls)
            ):
                raise DataError(DataErrorCode.INVALID_EVENT_GROUP, run_id)
            for c in sorted(calls, key=lambda c: c.ordinal):
                binding = c.arguments.get("workspace_binding_id")
                pending.append(
                    ToolExecutionRecord(
                        call=CallRef(run_id, c.call_id),
                        ordinal=c.ordinal,
                        tool_id=c.tool_id,
                        arguments=c.arguments,
                        status=ToolExecutionStatus.PENDING,
                        workspace_binding_id=BindingId(binding) if isinstance(binding, str) else None,
                    )
                )
        event = self._new_event(conv, scope, output, run_id)
        for record in pending:
            conv.executions[record.call] = record
        run.projected_event_id = event.event_id
        stamped = (
            None
            if continuation is None
            else ProviderContinuation(run.record.provider_key, run.record.model_id, continuation)
        )
        self._set_status(run, RunStatus.PROJECTED, continuation=stamped)
        return ProjectionResult(event=event, pending=tuple(pending))

    async def record_projection_failure(
        self, scope: TrustedScope, run_id: RunId, error_code: str
    ) -> None:
        run = self._run(self._conv(scope), run_id)
        if run.record.status is not RunStatus.RECEIVED:
            raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"{run_id} is {run.record.status.value}")
        self._set_status(run, RunStatus.PROJECTION_FAILED, error_code=error_code)

    async def read_run(self, scope: TrustedScope, run_id: RunId) -> RunRecord:
        return self._run(self._conv(scope), run_id).record

    async def read_raw_response(self, scope: TrustedScope, run_id: RunId) -> RawReceived:
        run = self._run(self._conv(scope), run_id)
        if run.raw_response_body is None:
            raise DataError(DataErrorCode.NOT_FOUND, f"{run_id} has no stored response")
        return RawReceived(run.raw_response_body, run.raw_response_content_type, run.record.usage)

    async def read_tool_executions(
        self, scope: TrustedScope, run_id: RunId
    ) -> Sequence[ToolExecutionRecord]:
        conv = self._conv(scope)
        group = [e for e in conv.executions.values() if e.call.run_id == run_id]
        return tuple(sorted(group, key=lambda e: e.ordinal))

    def _execution(self, conv: _Conversation, call: CallRef) -> ToolExecutionRecord:
        record = conv.executions.get(call)
        if record is None:
            raise DataError(DataErrorCode.NOT_FOUND, f"{call.run_id}/{call.call_id}")
        return record

    async def claim_tool_execution(
        self,
        scope: TrustedScope,
        call: CallRef,
        *,
        unknown_outcome: UnknownOutcomePolicy,
        record_event: bool,
    ) -> None:
        conv = self._conv(scope)
        record = self._execution(conv, call)
        if record.status is not ToolExecutionStatus.PENDING:
            raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"{call.call_id} is {record.status.value}")
        conv.executions[call] = replace(
            record,
            status=ToolExecutionStatus.EXECUTION_CLAIMED,
            unknown_outcome=unknown_outcome,
            record_event=record_event,
        )

    async def commit_tool_outcome(
        self, scope: TrustedScope, call: CallRef, completion: ToolCompletion
    ) -> ConversationEvent | None:
        conv = self._conv(scope)
        record = self._execution(conv, call)
        if record.status in _TERMINAL_COMPLETIONS or record.status is ToolExecutionStatus.UNKNOWN:
            raise DataError(DataErrorCode.ALREADY_COMMITTED, f"{call.call_id} is {record.status.value}")
        if completion.status not in _TERMINAL_COMPLETIONS:
            raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"not a definite outcome: {completion.status}")
        staged = self._stage_artifacts(conv, completion.artifact_mutations)  # validates CAS first
        conv.artifacts.update(staged)
        conv.executions[call] = replace(
            record,
            status=completion.status,
            model_result=completion.model_result,
            raw_result=completion.raw_result,
        )
        if not completion.record_event:
            return None
        return self._new_event(conv, scope, ToolResultRecord(call, completion.model_result), call.run_id)

    async def commit_unknown_outcome(
        self, scope: TrustedScope, call: CallRef, model_result: ModelToolResult
    ) -> ConversationEvent | None:
        conv = self._conv(scope)
        record = self._execution(conv, call)
        if record.model_result is not None:
            raise DataError(DataErrorCode.ALREADY_COMMITTED, f"{call.call_id} already has a result")
        if record.status not in (ToolExecutionStatus.EXECUTION_CLAIMED, ToolExecutionStatus.UNKNOWN):
            raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"{call.call_id} is {record.status.value}")
        if record.unknown_outcome is not UnknownOutcomePolicy.REPORT_TO_MODEL:
            raise DataError(
                DataErrorCode.INVALID_STATE_TRANSITION,
                f"{call.call_id}: policy {record.unknown_outcome} requires a user",
            )
        conv.executions[call] = replace(record, status=ToolExecutionStatus.UNKNOWN, model_result=model_result)
        if record.record_event is False:  # claim-time policy; None (legacy) records the Event
            return None
        return self._new_event(conv, scope, ToolResultRecord(call, model_result), call.run_id)

    def _stage_artifacts(
        self, conv: _Conversation, mutations: Sequence[ArtifactMutation]
    ) -> dict[ArtifactId, Artifact]:
        staged: dict[ArtifactId, Artifact] = {}
        for m in mutations:
            if m.artifact_id is None:
                aid = ArtifactId(self._store.new_id("art"))
                staged[aid] = Artifact(aid, m.kind, 1, m.content)
                continue
            current = staged.get(m.artifact_id) or conv.artifacts.get(m.artifact_id)
            if current is None:
                raise DataError(DataErrorCode.NOT_FOUND, m.artifact_id)
            if m.expected_revision is not None and m.expected_revision != current.revision:
                raise DataError(DataErrorCode.VERSION_CONFLICT, m.artifact_id)
            staged[m.artifact_id] = replace(current, content=m.content, revision=current.revision + 1)
        return staged

    async def find_waiting_call(self, scope: TrustedScope) -> CallRef | None:
        return self._waiting(self._conv(scope))

    async def answer_pending_call(
        self, scope: TrustedScope, call: CallRef, answer: UserMessage
    ) -> AnswerCommit:
        conv = self._conv(scope)
        if self._waiting(conv) != call:
            raise DataError(DataErrorCode.ALREADY_COMMITTED, f"{call.call_id} is not waiting")
        record = self._execution(conv, call)
        result = ModelToolResult(blocks=answer.blocks)
        user_event = self._new_event(conv, scope, replace(answer, answers_call=call), None)
        conv.executions[call] = replace(record, status=ToolExecutionStatus.SUCCEEDED, model_result=result)
        tool_event = self._new_event(conv, scope, ToolResultRecord(call, result), call.run_id)
        return AnswerCommit(user_event=user_event, tool_result_event=tool_event)

    async def recover_interrupted(self, scope: TrustedScope) -> RecoveryReport:
        conv = self._conv(scope)
        unknown_runs: list[RunId] = []
        abandoned: list[RunId] = []
        unknown_tools: list[CallRef] = []
        received: list[RunId] = []
        for run in conv.runs.values():
            if run.record.status is RunStatus.DISPATCH_CLAIMED:
                self._set_status(run, RunStatus.OUTCOME_UNKNOWN, error_code="interrupted")
                unknown_runs.append(run.record.run_id)
            elif run.record.status is RunStatus.PREPARED:
                self._set_status(run, RunStatus.ABANDONED_NOT_SENT)
                abandoned.append(run.record.run_id)
            elif run.record.status is RunStatus.RECEIVED:
                received.append(run.record.run_id)
        for ref, record in list(conv.executions.items()):
            if record.status is ToolExecutionStatus.EXECUTION_CLAIMED:
                conv.executions[ref] = replace(record, status=ToolExecutionStatus.UNKNOWN)
                unknown_tools.append(ref)
        return RecoveryReport(tuple(unknown_runs), tuple(abandoned), tuple(unknown_tools), tuple(received))

    async def get_latest_snapshot(self, scope: TrustedScope) -> PromptSnapshot | None:
        return self._conv(scope).snapshot

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
        conv = self._conv(scope)
        current = conv.snapshot.revision if conv.snapshot else None
        if current != expected_revision:
            raise DataError(DataErrorCode.VERSION_CONFLICT, "snapshot")
        revision = (current or 0) + 1
        conv.snapshot = PromptSnapshot(revision, schema_version, body, covers_through_sequence, basis)
        return revision

    # --- ArtifactStore ---------------------------------------------------------

    async def read_artifact(self, scope: TrustedScope, artifact_id: ArtifactId) -> Artifact:
        artifact = self._conv(scope).artifacts.get(artifact_id)
        if artifact is None:
            raise DataError(DataErrorCode.NOT_FOUND, artifact_id)
        return artifact

    async def list_artifacts(
        self, scope: TrustedScope, *, kind: str | None = None
    ) -> Sequence[Artifact]:
        return tuple(
            a for a in self._conv(scope).artifacts.values() if kind is None or a.kind == kind
        )

    # --- WorkspaceState --------------------------------------------------------

    async def read_binding_authority(
        self, scope: TrustedScope, binding_id: BindingId
    ) -> BindingAuthority:
        binding = self._conv(scope).bindings.get(binding_id)
        if binding is None:
            raise DataError(DataErrorCode.NOT_FOUND, binding_id)
        return binding

    async def resolve_default_binding(
        self, scope: TrustedScope, environment_kind: EnvironmentKind
    ) -> BindingAuthority | None:
        conv = self._conv(scope)
        for act in conv.activations.values():
            if act.environment_kind == environment_kind and act.default_binding_id is not None:
                return conv.bindings.get(act.default_binding_id)
        return None

    async def read_activation(
        self, scope: TrustedScope, environment_kind: EnvironmentKind
    ) -> EnvironmentActivation | None:
        return next(
            (a for a in self._conv(scope).activations.values() if a.environment_kind == environment_kind),
            None,
        )

    async def resolve_conversation_workspace(self, scope: TrustedScope) -> ConversationWorkspace:
        workspace = self._conv(scope).workspace
        if workspace is None:
            raise DataError(DataErrorCode.NOT_FOUND, "conversation workspace")
        return workspace

    # --- StateQuery -------------------------------------------------------------

    async def read_current_state_basis(self, scope: TrustedScope) -> PersistedStateBasis:
        conv = self._conv(scope)
        return PersistedStateBasis(
            conversation_sequence=len(conv.events),
            state_revision=conv.state_revision,
            environments=tuple(conv.activations.values()),
            bindings=tuple(conv.bindings.values()),
            artifacts=tuple(conv.artifacts.values()),
            conversation_workspace=conv.workspace,
        )
