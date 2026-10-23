"""In-memory Data implementation of all P0 sub-ports.

The ``InMemoryStore`` object plays the role of durable storage: a "process
restart" is modelled by building a new driver over the same store. Every port
call validates first and mutates last, so each call is all-or-nothing under
single-threaded asyncio (the transaction boundaries T1–T9, T7a, T-R).
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from himari.common import (
    ASK_USER_TOOL_ID,
    ArtifactId,
    AttachmentRef,
    BindingId,
    CallRef,
    ConversationId,
    EnvironmentInstanceId,
    EnvironmentKind,
    EventId,
    FileId,
    JsonValue,
    ModelToolResult,
    PrincipalId,
    ProviderContinuation,
    RunId,
    ToolId,
    TrustedScope,
    UnknownOutcomePolicy,
    WorkspaceId,
)
from himari.data import rules
from himari.data.blobs import BlobRow, FileStore, MemoryBlobFiles
from himari.data.api import (
    MAX_ATTACHMENTS,
    NO_IMAGE,
    MAX_STAGED_UPLOADS,
    PRESENT_LIMIT_BYTES,
    PRESENT_WINDOW,
    STAGED_SEND_CUTOFF,
    AnswerCommit,
    AppView,
    Artifact,
    ArtifactMutation,
    AssistantFinal,
    AssistantOutput,
    AssistantToolCalls,
    BindingAuthority,
    ConversationEvent,
    ConversationSummary,
    ConversationWorkspace,
    DataError,
    DataErrorCode,
    EnvironmentActivation,
    EventKind,
    FileOrigin,
    FileRecord,
    ImageSetup,
    Instructions,
    InstructionScope,
    PresentedFile,
    StoredBlob,
    EventPayload,
    ExternalSourceSetting,
    InitialActivation,
    ModelSetting,
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
    RunPurpose,
    RunRecord,
    RunStatus,
    StoredAppView,
    ToolCompletion,
    ToolExecutionRecord,
    ToolExecutionStatus,
    ToolResultRecord,
    TrashedConversation,
    UiResource,
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
class _File:
    """A ``files`` row (V3-P66): mutable only in ``workspace_path`` and ``attached_at``."""

    file_id: FileId
    version: int
    origin: FileOrigin
    name: str
    media_type: str
    blob_key: str
    created_at: datetime
    workspace_path: str | None = None
    attached_at: datetime | None = None
    call: CallRef | None = None
    image: ImageSetup = NO_IMAGE  # V3-P67

    def keys(self) -> set[str]:
        """The blobs it refers to: the file, and its model copy."""
        return {self.blob_key} | ({self.image.view.blob.key} if self.image.view is not None else set())


@dataclass
class _Conversation:
    owner: PrincipalId
    title: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
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
    external: dict[str, bool] = field(default_factory=dict)  # V3-P53 choices by source
    model: ModelSetting | None = None  # V3-P64
    app_payloads: dict[CallRef, JsonValue] = field(default_factory=dict)  # V3-P58, by call
    trashed_at: datetime | None = None  # V3-P65
    purge_started_at: datetime | None = None
    files: list[_File] = field(default_factory=list)  # V3-P66, in creation order


class InMemoryStore:
    """Durable-storage stand-in plus admin helpers (not part of any port)."""

    def __init__(self) -> None:
        self._conversations: dict[ConversationId, _Conversation] = {}
        self._ids = itertools.count(1)
        self.ui_resources: dict[str, UiResource] = {}  # V3-P58: by digest, shared by every Conversation
        self.blob_rows: dict[str, BlobRow] = {}  # V3-P66: by key, shared by a principal's Conversations
        self.blobs = FileStore(MemoryBlobFiles(), _MemoryBlobRows(self))
        self.instructions: dict[tuple[PrincipalId, InstructionScope], Instructions] = {}  # V3-P69

    # --- admin helpers (trusted setup paths, not model-reachable) --------

    def create_conversation(self, scope: TrustedScope, *, title: str | None = None) -> None:
        if scope.conversation_id in self._conversations:
            raise DataError(DataErrorCode.DUPLICATE_IDENTITY, scope.conversation_id)
        self._conversations[scope.conversation_id] = _Conversation(owner=scope.principal_id, title=title)

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
        # Each Binding carries its own working directory (default: the
        # Conversation's own folder, V3-033), so activating another
        # Environment never changes an existing one's.
        relative = cwd_relative if cwd_relative is not None else f"conversations/{scope.conversation_id}"
        binding = BindingAuthority(
            binding_id=BindingId(self.new_id("bind")),
            environment_instance_id=instance_id,
            environment_kind=kind,
            workspace_id=workspace_id,
            revision=1,
            cwd_relative=relative,
        )
        conv.activations[instance_id] = EnvironmentActivation(instance_id, kind, binding.binding_id)
        conv.bindings[binding.binding_id] = binding
        if conv.workspace is None:  # the Conversation's first (default) working directory
            conv.workspace = ConversationWorkspace(workspace_id, relative)
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
        conv = self._owned(scope)
        if conv.trashed_at is not None:  # V3-P65: a trashed Conversation is a missing one
            raise DataError(DataErrorCode.NOT_AUTHORIZED, scope.conversation_id)
        return conv

    def _owned(self, scope: TrustedScope) -> _Conversation:
        """The owner check alone (the trash methods, V3-P65)."""
        conv = self._conversations.get(scope.conversation_id)
        if conv is None or conv.owner != scope.principal_id:
            # Same error for foreign and missing (indistinguishability).
            raise DataError(DataErrorCode.NOT_AUTHORIZED, scope.conversation_id)
        return conv


class _MemoryBlobRows:
    """The ``blobs`` rows for the file store (V3-P66 §4.5)."""

    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    def _files(self) -> list[_File]:
        return [f for conv in self._store._conversations.values() for f in conv.files]

    def blob_row(self, key: str) -> BlobRow | None:
        return self._store.blob_rows.get(key)

    def live_total(self) -> int:
        return sum(r.size for r in self._store.blob_rows.values() if r.expired_at is None)

    def expirable(self, used_before: datetime) -> Sequence[BlobRow]:
        staged = {k for f in self._files() if f.attached_at is None for k in f.keys()}
        rows = [r for r in self._store.blob_rows.values()
                if r.expired_at is None and r.last_used_at < used_before and r.key not in staged]
        return sorted(rows, key=lambda r: (r.last_used_at, r.key))

    def expire_blobs(self, keys: Sequence[str], now: datetime) -> Sequence[str]:
        staged = {k for f in self._files() if f.attached_at is None for k in f.keys()}  # as in SQLite (re-review N3)
        expired = []
        for key in keys:
            row = self._store.blob_rows.get(key)
            if row is not None and row.expired_at is None and key not in staged:
                self._store.blob_rows[key] = replace(row, expired_at=now)
                expired.append(key)
        return expired

    def touch_blob(self, row: BlobRow) -> None:
        self._store.blob_rows[row.key] = replace(row, expired_at=None)

    def reserve(self, row: BlobRow, max_bytes: int, used_before: datetime) -> Sequence[str]:
        need = self.live_total() + row.size - max_bytes
        chosen: list[str] = []
        if need > 0:
            freed = 0
            for candidate in self.expirable(used_before):
                if freed >= need:
                    break
                chosen.append(candidate.key)
                freed += candidate.size
            if freed < need:
                raise DataError(DataErrorCode.STORE_FULL, f"needs {need} more bytes")
            for key in chosen:
                self._store.blob_rows[key] = replace(self._store.blob_rows[key], expired_at=row.last_used_at)
        self._store.blob_rows[row.key] = replace(row, expired_at=None)
        return chosen

    def drop_unreferenced(self, key: str, used_before: datetime) -> bool:
        if key in self.unreferenced_keys(used_before):
            del self._store.blob_rows[key]
            return True
        return False

    def unreferenced_keys(self, used_before: datetime) -> Sequence[str]:
        used = {k for f in self._files() for k in f.keys()}
        return [r.key for r in self._store.blob_rows.values()
                if r.key not in used and (r.expired_at is not None or r.last_used_at < used_before)]

    def expired_keys(self) -> Sequence[str]:
        return [r.key for r in self._store.blob_rows.values() if r.expired_at is not None]

    def known_keys(self) -> frozenset[str]:
        return frozenset(self._store.blob_rows)


def _refused(file_id: str, why: str) -> DataError:
    return DataError(DataErrorCode.ATTACHMENT_REFUSED, f"{file_id}: {why}", file_id=FileId(file_id))


class InMemoryData:
    """Implements ConversationLog, ArtifactStore, WorkspaceState, StateQuery,
    ConversationCatalog, ConversationRetention, ExternalSourceSettings,
    FileCatalog, FileRetention and InstructionsStore (V3-P69); ``blobs`` is the
    BlobStore (V3-P66)."""

    def __init__(self, store: InMemoryStore) -> None:
        self._store = store
        self.blobs = store.blobs

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
        projected = [
            r for r in conv.runs.values() if r.record.status is RunStatus.PROJECTED and r.projected_event_id is not None
        ]
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

    # --- files (V3-P66) ---------------------------------------------------------------

    def _record(self, file: _File) -> FileRecord:
        row = self._store.blob_rows[file.blob_key]
        image, view = file.image, file.image.view
        if view is not None:
            view_row = self._store.blob_rows[view.blob.key]
            view = replace(view, expired=view_row.expired_at is not None)
        return FileRecord(file.file_id, file.version, file.origin, file.name, file.media_type,
                          StoredBlob(row.key, row.sha256, row.size), file.created_at, file.workspace_path,
                          file.attached_at is not None, row.expired_at is not None, file.call,
                          image.status, image.image_type, image.width, image.height, view)

    def _alive(self, blob: StoredBlob) -> bool:
        row = self._store.blob_rows.get(blob.key)
        return row is not None and row.expired_at is None and row.size == blob.size

    def _new_file(self, conv: _Conversation, origin: FileOrigin, call: CallRef | None, blob: StoredBlob,
                  name: str, media_type: str, image: ImageSetup) -> FileRecord:
        if not self._alive(blob) or (image.view is not None and not self._alive(image.view.blob)):
            raise DataError(DataErrorCode.BLOB_MISSING, blob.key)
        file = _File(FileId(self._store.new_id("file")), 1, origin, name, media_type, blob.key, datetime.now(UTC),
                     call=call, image=image)
        conv.files.append(file)
        return self._record(file)

    def _tool_images(self, conv: _Conversation, call: CallRef, blocks: Sequence[object]) -> list[_File]:
        """V3-P67 §4.2: a result's attachments are this call's staged Tool images."""
        found: list[_File] = []
        for ref in (b for b in blocks if isinstance(b, AttachmentRef)):
            file = next((f for f in conv.files if f.file_id == ref.file_id and f.version == ref.version), None)
            if (file is None or file.origin is not FileOrigin.TOOL or file.attached_at is not None or file.call != call
                    or any(f is file for f in found)):  # twice in one result: refused, as in SQLite (review N8)
                raise DataError(DataErrorCode.INVALID_ARGUMENT, f"{ref.file_id} is not an image of this call")
            found.append(file)
        return found

    def _check_attachments(self, conv: _Conversation, file_ids: Sequence[str], *,
                           cutoff: datetime | None) -> list[_File]:
        """V3-P66 §4.2, as in SQLite."""
        if len(file_ids) > MAX_ATTACHMENTS:
            raise DataError(DataErrorCode.ATTACHMENT_REFUSED, f"more than {MAX_ATTACHMENTS} attachments")
        chosen: list[_File] = []
        for file_id in file_ids:
            if any(f.file_id == file_id for f in chosen):
                raise _refused(file_id, "attached twice")
            versions = [f for f in conv.files if f.file_id == file_id]
            if not versions:
                raise _refused(file_id, "no such upload")
            file = max(versions, key=lambda f: f.version)
            if file.origin is not FileOrigin.UPLOAD or file.version != 1:
                raise _refused(file_id, "not an upload")
            if file.attached_at is not None:
                raise _refused(file_id, "already sent")
            if self._store.blob_rows[file.blob_key].expired_at is not None:
                raise _refused(file_id, "expired")
            if cutoff is not None and file.created_at < cutoff:
                raise _refused(file_id, "too old; upload it again")
            chosen.append(file)
        return chosen

    def _attach(self, conv: _Conversation, blocks: Sequence[object]) -> list[_File]:
        """Checks only; the caller sets ``attached_at`` once nothing else can fail."""
        refs = [b for b in blocks if isinstance(b, AttachmentRef)]
        if not refs:
            return []
        if any(r.version != 1 for r in refs):
            raise _refused(next(r.file_id for r in refs if r.version != 1), "not an upload")
        return self._check_attachments(conv, [r.file_id for r in refs], cutoff=None)

    @staticmethod
    def _mark_attached(files: Sequence[_File]) -> None:
        now = datetime.now(UTC)
        for file in files:
            file.attached_at = now

    def _presented_since(self, conv: _Conversation, since: datetime) -> int:
        return sum(self._store.blob_rows[f.blob_key].size for f in conv.files
                   if f.origin is FileOrigin.PRESENTED and f.created_at >= since)

    def _stage_presented(self, conv: _Conversation, call: CallRef, files: Sequence[PresentedFile]) -> list[_File]:
        now = datetime.now(UTC)
        if self._presented_since(conv, now - PRESENT_WINDOW) + sum(f.blob.size for f in files) > PRESENT_LIMIT_BYTES:
            raise DataError(DataErrorCode.LIMIT_EXCEEDED, f"over {PRESENT_LIMIT_BYTES} bytes presented in the hour")
        staged: list[_File] = []
        for presented in files:
            row = self._store.blob_rows.get(presented.blob.key)
            if row is None or row.expired_at is not None or row.size != presented.blob.size:
                raise DataError(DataErrorCode.BLOB_MISSING, presented.blob.key)
            earlier = [f for f in [*conv.files, *staged]
                       if f.origin is FileOrigin.PRESENTED and f.workspace_path == presented.workspace_path]
            if earlier:
                file_id, version = earlier[-1].file_id, max(f.version for f in earlier
                                                            if f.file_id == earlier[-1].file_id) + 1
            else:
                file_id, version = FileId(self._store.new_id("file")), 1
            staged.append(_File(file_id, version, FileOrigin.PRESENTED, presented.name, presented.media_type,
                                presented.blob.key, now, presented.workspace_path, now, call))
        return staged

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
        attached = self._attach(conv, message.blocks)
        self._mark_attached(attached)
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
                purpose=preparation.purpose,
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
        if run.record.purpose is RunPurpose.COMPACTION:
            raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"{run_id} is a compaction Run")
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
        view = rules.check_app_view(completion)
        known = None if view is None else self._store.ui_resources.get(view.resource.digest)
        if view is not None and known is not None and known != view.resource:
            raise DataError(DataErrorCode.INVALID_ARGUMENT, "another view is stored under this digest")
        if completion.presented_files and completion.status is not ToolExecutionStatus.SUCCEEDED:
            raise DataError(DataErrorCode.INVALID_ARGUMENT, "only a succeeded call presents files")
        staged = self._stage_artifacts(conv, completion.artifact_mutations)  # validates CAS first
        presented = self._stage_presented(conv, call, completion.presented_files)
        images = self._tool_images(conv, call, completion.model_result.blocks)
        conv.artifacts.update(staged)
        conv.files.extend(presented)
        self._mark_attached(images)
        if view is not None:
            self._store.ui_resources.setdefault(view.resource.digest, view.resource)
            conv.app_payloads[call] = view.payload
        conv.executions[call] = replace(
            record,
            status=completion.status,
            model_result=completion.model_result,
            raw_result=completion.raw_result,
            ui_digest=None if view is None else view.resource.digest,
        )
        if not completion.record_event:
            return None
        return self._new_event(conv, scope, ToolResultRecord(call, completion.model_result), call.run_id)

    async def read_app_view(self, scope: TrustedScope, call: CallRef) -> StoredAppView | None:
        conv = self._conv(scope)
        record = self._execution(conv, call)
        if record.ui_digest is None:
            return None
        view = AppView(self._store.ui_resources[record.ui_digest], conv.app_payloads[call])
        return StoredAppView(call, record.tool_id, record.arguments, record.status, view)

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
        self._mark_attached(self._attach(conv, answer.blocks))
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
        discarded: list[RunId] = []
        for run in conv.runs.values():
            if run.record.status is RunStatus.DISPATCH_CLAIMED:
                self._set_status(run, RunStatus.OUTCOME_UNKNOWN, error_code="interrupted")
                unknown_runs.append(run.record.run_id)
            elif run.record.status is RunStatus.PREPARED:
                self._set_status(run, RunStatus.ABANDONED_NOT_SENT)
                abandoned.append(run.record.run_id)
            elif run.record.status is RunStatus.RECEIVED and run.record.purpose is RunPurpose.COMPACTION:
                self._set_status(run, RunStatus.PROJECTION_FAILED, error_code="interrupted")
                discarded.append(run.record.run_id)
            elif run.record.status is RunStatus.RECEIVED:
                received.append(run.record.run_id)
        for ref, record in list(conv.executions.items()):
            if record.status is ToolExecutionStatus.EXECUTION_CLAIMED:
                conv.executions[ref] = replace(record, status=ToolExecutionStatus.UNKNOWN)
                unknown_tools.append(ref)
        return RecoveryReport(tuple(unknown_runs), tuple(abandoned), tuple(unknown_tools), tuple(received),
                              tuple(discarded))

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

    async def record_compaction_step(self, scope: TrustedScope, run_id: RunId) -> None:
        run = self._run(self._conv(scope), run_id)
        if run.record.status is not RunStatus.RECEIVED or run.record.purpose is not RunPurpose.COMPACTION:
            raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"{run_id} is not a received compaction Run")
        self._set_status(run, RunStatus.PROJECTED)

    async def commit_compaction(
        self,
        scope: TrustedScope,
        run_id: RunId,
        *,
        expected_revision: int | None,
        schema_version: int,
        body: JsonValue,
        covers_through_sequence: int,
        basis: PromptBasis,
    ) -> int:
        conv = self._conv(scope)
        run = self._run(conv, run_id)
        if run.record.status is not RunStatus.RECEIVED or run.record.purpose is not RunPurpose.COMPACTION:
            raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"{run_id} is not a received compaction Run")
        current = conv.snapshot.revision if conv.snapshot else None
        if current != expected_revision:
            raise DataError(DataErrorCode.VERSION_CONFLICT, "snapshot")
        last = conv.events[-1].sequence if conv.events else 0
        if covers_through_sequence > last or (
            conv.snapshot is not None and covers_through_sequence < conv.snapshot.covers_through_sequence
        ):
            raise DataError(DataErrorCode.STALE_BASIS, f"coverage {covers_through_sequence}")
        revision = (current or 0) + 1
        conv.snapshot = PromptSnapshot(revision, schema_version, body, covers_through_sequence, basis)
        self._set_status(run, RunStatus.PROJECTED)
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

    # --- ConversationCatalog ----------------------------------------------------

    async def create_conversation(
        self,
        principal: PrincipalId,
        *,
        title: str | None = None,
        activations: Sequence[InitialActivation] = (),
    ) -> ConversationId:
        title = rules.normalize_title(title)
        rules.check_activations(activations)
        scope = TrustedScope(principal, ConversationId(self._store.new_id("conv")))
        self._store.create_conversation(scope, title=title)
        for activation in activations:  # validated above: cannot fail half way
            self._store.activate_environment(scope, activation.environment_kind, activation.workspace_id)
        return scope.conversation_id

    def _summary(self, conversation_id: ConversationId, conv: _Conversation) -> ConversationSummary:
        first_user = next((e for e in conv.events if isinstance(e.payload, UserMessage)), None)
        return ConversationSummary(
            conversation_id=conversation_id,
            title=conv.title,
            created_at=conv.created_at,
            last_activity_at=conv.events[-1].created_at if conv.events else conv.created_at,
            last_sequence=len(conv.events),
            preview="" if first_user is None else rules.preview(first_user.payload.blocks),  # type: ignore[union-attr]
            trashed_at=conv.trashed_at,
        )

    async def list_conversations(
        self, principal: PrincipalId, *, limit: int = 50, offset: int = 0, trashed: bool = False
    ) -> Sequence[ConversationSummary]:
        rules.check_page(limit, offset)
        owned = [(i, cid, conv) for i, (cid, conv) in enumerate(self._store._conversations.items())
                 if conv.owner == principal and (conv.trashed_at is not None) == trashed]
        rows = [(self._summary(cid, conv), i) for i, cid, conv in owned]
        rows.sort(key=lambda row: (row[0].trashed_at if trashed else row[0].last_activity_at, row[1]), reverse=True)
        return tuple(summary for summary, _ in rows[offset : offset + limit])

    async def read_conversation(self, scope: TrustedScope) -> ConversationSummary:
        return self._summary(scope.conversation_id, self._conv(scope))

    async def rename_conversation(self, scope: TrustedScope, title: str | None) -> None:
        conv = self._conv(scope)
        conv.title = rules.normalize_title(title)

    async def trash_conversation(self, scope: TrustedScope) -> datetime:
        conv = self._store._owned(scope)
        if conv.trashed_at is not None:
            raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"{scope.conversation_id} is in the trash")
        conv.trashed_at = datetime.now(UTC)
        return conv.trashed_at

    async def restore_conversation(self, scope: TrustedScope) -> None:
        conv = self._store._owned(scope)
        if conv.trashed_at is None or conv.purge_started_at is not None:
            raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"{scope.conversation_id} cannot be restored")
        conv.trashed_at = None

    async def begin_purge(self, scope: TrustedScope) -> TrashedConversation:
        conv = self._store._owned(scope)
        if conv.trashed_at is None:
            raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"{scope.conversation_id} is not in the trash")
        if conv.purge_started_at is None:
            conv.purge_started_at = datetime.now(UTC)
        return TrashedConversation(self._summary(scope.conversation_id, conv), conv.workspace)

    async def purge_conversation(self, scope: TrustedScope) -> None:
        conv = self._store._owned(scope)
        if conv.purge_started_at is None:
            raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"{scope.conversation_id} is not being deleted")
        del self._store._conversations[scope.conversation_id]
        used = {e.ui_digest for c in self._store._conversations.values() for e in c.executions.values()}
        for digest in {e.ui_digest for e in conv.executions.values()} - used:
            if digest is not None:
                del self._store.ui_resources[digest]
        await self.blobs.release({k for f in conv.files for k in f.keys()})  # never fails the purge (V3-P66 §8.1)

    async def used_tool_ids(self, scope: TrustedScope) -> frozenset[ToolId]:
        return frozenset(e.tool_id for e in self._conv(scope).executions.values())

    # --- ConversationRetention --------------------------------------------------

    async def due_purges(self, cutoff: datetime) -> Sequence[TrustedScope]:
        due = [(conv.trashed_at, i, TrustedScope(conv.owner, cid))
               for i, (cid, conv) in enumerate(self._store._conversations.items())
               if conv.trashed_at is not None and (conv.purge_started_at is not None or conv.trashed_at < cutoff)]
        return tuple(scope for _, _, scope in sorted(due, key=lambda row: (row[0], row[1])))

    # --- FileCatalog (V3-P66) ----------------------------------------------------------

    async def stage_upload(self, scope: TrustedScope, blob: StoredBlob, name: str, media_type: str,
                           image: ImageSetup = NO_IMAGE) -> FileRecord:
        conv = self._conv(scope)
        if sum(1 for f in conv.files if f.attached_at is None and f.origin is FileOrigin.UPLOAD) >= MAX_STAGED_UPLOADS:
            raise DataError(DataErrorCode.LIMIT_EXCEEDED, f"{MAX_STAGED_UPLOADS} uploads wait to be sent")
        return self._new_file(conv, FileOrigin.UPLOAD, None, blob, name, media_type, image)

    async def stage_tool_file(self, scope: TrustedScope, call: CallRef, blob: StoredBlob, name: str,
                              media_type: str, image: ImageSetup) -> FileRecord:
        conv = self._conv(scope)
        self._execution(conv, call)  # a call of this Conversation
        return self._new_file(conv, FileOrigin.TOOL, call, blob, name, media_type, image)

    async def count_staged(self, scope: TrustedScope) -> int:
        return sum(1 for f in self._conv(scope).files if f.attached_at is None and f.origin is FileOrigin.UPLOAD)

    async def set_placement(self, scope: TrustedScope, file_id: FileId, workspace_path: str) -> None:
        conv = self._conv(scope)
        file = next((f for f in conv.files if f.file_id == file_id and f.origin is FileOrigin.UPLOAD), None)
        if file is None:
            raise DataError(DataErrorCode.NOT_FOUND, file_id)
        if file.attached_at is not None:
            raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"{file_id} was already sent")
        file.workspace_path = workspace_path

    async def check_attachments(self, scope: TrustedScope, file_ids: Sequence[FileId], *,
                                now: datetime | None = None) -> Sequence[FileRecord]:
        conv = self._conv(scope)
        cutoff = (datetime.now(UTC) if now is None else now) - STAGED_SEND_CUTOFF
        return tuple(self._record(f) for f in self._check_attachments(conv, file_ids, cutoff=cutoff))

    async def read_files(self, scope: TrustedScope, *, limit: int = 50, offset: int = 0) -> Sequence[FileRecord]:
        attached = [f for f in reversed(self._conv(scope).files) if f.attached_at is not None]
        return tuple(self._record(f) for f in attached[offset: offset + limit])

    async def read_file_records(self, scope: TrustedScope, refs: Sequence[tuple[FileId, int]]
                                ) -> Sequence[FileRecord]:
        conv = self._conv(scope)
        found = []
        for file_id, version in dict.fromkeys(refs):
            file = next((f for f in conv.files if f.file_id == file_id and f.version == version), None)
            if file is not None:
                found.append(self._record(file))
        return tuple(found)

    async def read_call_files(self, scope: TrustedScope, calls: Sequence[CallRef]
                              ) -> Mapping[CallRef, Sequence[FileRecord]]:
        conv = self._conv(scope)
        found = {}
        for call in dict.fromkeys(calls):
            files = [self._record(f) for f in conv.files if f.call == call and f.origin is FileOrigin.PRESENTED]
            if files:
                found[call] = tuple(files)
        return found

    async def presented_bytes_since(self, scope: TrustedScope, since: datetime) -> int:
        return self._presented_since(self._conv(scope), since)

    # --- FileRetention (V3-P66 §8.2) ------------------------------------------------------

    async def drop_stale_staged(self, cutoff: datetime) -> int:
        dropped = 0
        for conv in self._store._conversations.values():
            keep = [f for f in conv.files if f.attached_at is not None or f.created_at >= cutoff]
            dropped += len(conv.files) - len(keep)
            conv.files[:] = keep
        return dropped

    async def sweep_blobs(self, now: datetime) -> None:
        await self.blobs.sweep(now)

    # --- ExternalSourceSettings ------------------------------------------------

    async def read_external_settings(self, scope: TrustedScope) -> Sequence[ExternalSourceSetting]:
        return tuple(ExternalSourceSetting(s, on) for s, on in sorted(self._conv(scope).external.items()))

    async def set_external_setting(self, scope: TrustedScope, source: str, enabled: bool | None) -> None:
        conv = self._conv(scope)
        after = len(conv.external) + (enabled is not None and source not in conv.external)
        rules.check_external_setting(source, enabled, after)
        if enabled is None:
            conv.external.pop(source, None)
        else:
            conv.external[source] = enabled

    # --- ConversationModelSettings ----------------------------------------------

    async def read_model_setting(self, scope: TrustedScope) -> ModelSetting | None:
        return self._conv(scope).model

    async def set_model_setting(self, scope: TrustedScope, setting: ModelSetting | None) -> None:
        conv = self._conv(scope)
        rules.check_model_setting(setting)
        conv.model = setting

    # --- InstructionsStore (V3-P69) ----------------------------------------------

    async def read_instructions(self, principal: PrincipalId, scope: InstructionScope) -> Instructions:
        rules.check_instruction_scope(scope)
        return self._store.instructions.get((principal, scope), Instructions("", 0))

    async def write_instructions(self, principal: PrincipalId, scope: InstructionScope, text: str,
                                 expected_revision: int) -> Instructions:
        text = rules.check_instructions(scope, text)
        current = self._store.instructions.get((principal, scope), Instructions("", 0))
        if expected_revision != current.revision:
            raise DataError(DataErrorCode.VERSION_CONFLICT, f"instructions are at revision {current.revision}")
        record = Instructions(text, current.revision + 1, datetime.now(UTC))
        self._store.instructions[(principal, scope)] = record
        return record

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
