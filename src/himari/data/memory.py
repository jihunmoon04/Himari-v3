"""In-memory Data implementation of all P0 sub-ports.

The ``InMemoryStore`` object plays the role of durable storage: a "process
restart" is modelled by building a new driver over the same store. Every port
call validates first and mutates last, so each call is all-or-nothing under
single-threaded asyncio (the transaction boundaries T1–T9, T-R).
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from himari.common import (
    AttachmentRef,
    CallRef,
    ConversationId,
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
)
from himari.data import rules
from himari.data.blobs import BlobRow, FileStore, MemoryBlobFiles
from himari.data.api import (
    ALREADY_SENT,
    EventPage,
    EventSearch,
    EventText,
    MAX_ATTACHMENTS,
    NO_IMAGE,
    MAX_STAGED_UPLOADS,
    PRESENT_LIMIT_BYTES,
    PRESENT_WINDOW,
    STAGED_SEND_CUTOFF,
    AssistantFinal,
    AssistantOutput,
    AssistantToolCalls,
    ConversationEvent,
    ConversationSummary,
    DataError,
    DataErrorCode,
    EventKind,
    FileOrigin,
    FileRecord,
    ImageSetup,
    Instructions,
    InstructionScope,
    MAX_DEVICES,
    MAX_MEMORIES,
    MAX_LIFE_FACTS,
    MAX_BOND_ENTRIES,
    MAX_PERSONAS,
    PAIRING_FAILURE_LIMIT,
    ArcMove,
    ArcOp,
    BondChange,
    BondSnapshot,
    BondStage,
    BondStanding,
    BondTag,
    anniversaries_on,
    together_days,
    LifeChange,
    LifeChangeOp,
    LifeEntry,
    LifeKind,
    LifeOrigin,
    LifePlan,
    HimariMood,
    MoodSource,
    LifeWrite,
    LifeWriteStart,
    LifeWriteStatus,
    Persona,
    PersonaChoice,
    PersonaList,
    Device,
    DevicePairing,
    Memory,
    MemoryCategory,
    MemoryChange,
    MemoryChangeKind,
    MemoryCleanup,
    MemoryCursor,
    MemoryOrigin,
    MemoryPass,
    DueConversation,
    Episode,
    EpisodePlan,
    MAX_EPISODES,
    PassAction,
    PassApplied,
    PassItem,
    PassKind,
    PassStart,
    PassStatus,
    NoteDates,
    PassItemRecord,
    PassSummary,
    Proposal,
    PROPOSAL_KEEP,
    RecentEpisode,
    planned_item,
    RETIRED_KEEP,
    EXPIRED_KEEP,
    SKIP_AFTER_FAILURES,
    STALE_DUE,
    STALE_UNDATED,
    PresentedFile,
    StoredBlob,
    EventPayload,
    ExternalSourceSetting,
    ModelSetting,
    PairingResult,
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
    ToolCompletion,
    ToolExecutionRecord,
    ToolExecutionStatus,
    ToolResultRecord,
    TrashedConversation,
    InterfaceSnapshot,
    MessageOrigin,
    OriginKind,
    UserMessage,
    REMINDER_ATTEMPTS,
    Wake,
    WakeKind,
    WakeReason,
    WakeRecord,
    WakeStatus,
    free_wake,
    EventRef,
    CompletedGroupRef,
    PENDING_REMINDERS,
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
    Wake: EventKind.WAKE,
}


@dataclass
class _Run:
    record: RunRecord
    raw_request_body: bytes
    raw_response_body: bytes | None = None
    raw_response_content_type: str = ""


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
    snapshot: PromptSnapshot | None = None
    state_revision: int = 0
    environments: set[EnvironmentKind] = field(default_factory=set)
    idempotency: dict[str, EventId] = field(default_factory=dict)
    external: dict[str, bool] = field(default_factory=dict)  # V3-P53 choices by source
    model: ModelSetting | None = None  # V3-P64
    trashed_at: datetime | None = None  # V3-P65
    purge_started_at: datetime | None = None
    files: list[_File] = field(default_factory=list)  # V3-P66, in creation order


@dataclass
class _LifeWrite:
    """A ``life_writes`` row (character-life §3)."""

    owner: PrincipalId
    record: LifeWrite
    request: bytes
    response: bytes | None = None


@dataclass
class _Device:
    """A ``devices`` row (V3-P83 §4.2)."""

    device: Device
    token_hash: bytes | None = None
    pairing_hash: bytes | None = None
    pairing_failures: int = 0
    revoked: bool = False
    last_input_at: datetime | None = None  # V3-P87 §5.2


class InMemoryStore:
    """Durable-storage stand-in plus admin helpers (not part of any port)."""

    def __init__(self) -> None:
        self._conversations: dict[ConversationId, _Conversation] = {}
        self._ids = itertools.count(1)
        self.blob_rows: dict[str, BlobRow] = {}  # V3-P66: by key, shared by a principal's Conversations
        self.blobs = FileStore(MemoryBlobFiles(), _MemoryBlobRows(self))
        self.instructions: dict[tuple[PrincipalId, InstructionScope], Instructions] = {}  # V3-P69
        # V3-P108: each persona with its name key, the last id given out and the choice, per principal
        self.personas: dict[tuple[PrincipalId, int], tuple[Persona, str]] = {}
        self.persona_ids: dict[PrincipalId, int] = {}
        self.persona_choices: dict[PrincipalId, PersonaChoice] = {}
        # character-life v0.1: each life entry with when it retired (None: live), the last id given out
        # to each principal, and the story writer's calls in creation order
        self.life: dict[tuple[PrincipalId, int], tuple[LifeEntry, datetime | None]] = {}
        self.life_ids: dict[PrincipalId, int] = {}
        self.life_writes: dict[str, _LifeWrite] = {}
        self.bond_stages: dict[tuple[PrincipalId, int], BondStanding] = {}  # character-bond §2 (none: new)
        self.moods: dict[tuple[PrincipalId, int], HimariMood] = {}  # character-state §2 (none: no mood)
        self.memories: dict[tuple[PrincipalId, int], Memory] = {}  # V3-P71
        self.memory_ids: dict[PrincipalId, int] = {}  # the last id given out, per principal
        # V3-P85: notes an automatic forget retired (§5.4), with when, pass and item.
        self.retired_memories: dict[tuple[PrincipalId, int], tuple[Memory, datetime, str, int]] = {}
        self.episodes: dict[tuple[PrincipalId, int], Episode] = {}
        self.episode_ids: dict[PrincipalId, int] = {}
        self.memory_cursors: dict[ConversationId, MemoryCursor] = {}
        self.memory_passes: dict[str, tuple[PrincipalId, MemoryPass]] = {}  # in creation order
        self.pass_calls: dict[tuple[str, int], dict[str, object]] = {}
        self.pass_items: dict[tuple[str, int], dict[str, object]] = {}
        # V3-P74 §10.1: registered folders in registration order, and whose each is.
        self.devices: dict[tuple[PrincipalId, str], _Device] = {}  # V3-P83, in creation order; revoked ones stay
        self.quiet: dict[PrincipalId, datetime] = {}  # V3-P88 §6.2
        # V3-P87 §3.3: wakes by id, the keys taken (→ wake id), and each loop's reminder
        # delivery (state, claiming wake, attempts) by (principal, memory id)
        self.wakes: dict[str, WakeRecord] = {}
        self.wake_keys: dict[tuple[PrincipalId, str], str | None] = {}
        self.reminders: dict[tuple[PrincipalId, int], tuple[str, str | None, int]] = {}
        self.reminded_at: dict[tuple[PrincipalId, int], datetime] = {}
        self.device_ids: dict[PrincipalId, int] = {}

    # --- admin helpers (trusted setup paths, not model-reachable) --------

    def create_conversation(
        self,
        scope: TrustedScope,
        *,
        title: str | None = None,
        model_setting: ModelSetting | None = None,
    ) -> None:
        if scope.conversation_id in self._conversations:
            raise DataError(DataErrorCode.DUPLICATE_IDENTITY, scope.conversation_id)
        self._conversations[scope.conversation_id] = _Conversation(
            owner=scope.principal_id,
            title=title,
            model=model_setting,
        )

    def activate_environment(self, scope: TrustedScope, kind: EnvironmentKind) -> None:
        conv = self._authorized(scope)
        if kind in conv.environments:
            raise DataError(DataErrorCode.DUPLICATE_IDENTITY, f"{kind} is already there")
        conv.environments.add(kind)
        conv.state_revision += 1

    def deactivate_environment(self, scope: TrustedScope, kind: EnvironmentKind) -> None:
        """Takes the kind away; its places (the folder) remain."""
        conv = self._authorized(scope)
        conv.environments.discard(kind)
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
    return DataError(DataErrorCode.ATTACHMENT_REFUSED, f"{file_id}: {why}", file_id=FileId(file_id), reason=why)


class InMemoryData:
    """Implements ConversationLog, EnvironmentState, StateQuery,
    ConversationCatalog, ConversationRetention, ExternalSourceSettings,
    FileCatalog, FileRetention, InstructionsStore (V3-P69) and MemoryStore
    (V3-P71); ``blobs`` is the BlobStore (V3-P66)."""

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
                raise _refused(file_id, ALREADY_SENT)
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
            raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, "answers_call is older history only")
        rules.check_origin(message.origin)
        rules.check_interface(message.interface, message.origin)
        attached = self._attach(conv, message.blocks)
        self._mark_attached(attached)
        event = self._new_event(conv, scope, message, None)
        if idempotency_key is not None:
            conv.idempotency[idempotency_key] = event.event_id
        return event

    async def read_events(
        self, scope: TrustedScope, *, after_sequence: int = 0, limit: int | None = None
    ) -> Sequence[ConversationEvent]:
        gone = [w for w in self._store.wakes.values() if w.conversation_id == scope.conversation_id
                and w.status in (WakeStatus.PASSED, WakeStatus.DROPPED)]  # V3-P90 §7.3: never was
        hidden = {w.event_sequence for w in gone}
        held = {w.held_run_id for w in gone if w.held_run_id is not None}  # V3-P91 §8: what it looked at, with it
        events = [e for e in self._conv(scope).events if e.sequence > after_sequence
                  and not (e.kind is EventKind.WAKE and e.sequence in hidden)
                  and not (e.origin_run_id is not None and e.origin_run_id in held)]
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
            case RawUncertain(error_code=code, body=body, content_type=content_type):
                run.raw_response_body = body  # what arrived of a stream before it ended (V3-P73 §4)
                run.raw_response_content_type = content_type
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
                pending.append(
                    ToolExecutionRecord(
                        call=CallRef(run_id, c.call_id),
                        ordinal=c.ordinal,
                        tool_id=c.tool_id,
                        arguments=c.arguments,
                        status=ToolExecutionStatus.PENDING,
                    )
                )
        event = self._new_event(conv, scope, output, run_id)
        for record in pending:
            conv.executions[record.call] = record
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
        if completion.presented_files and completion.status is not ToolExecutionStatus.SUCCEEDED:
            raise DataError(DataErrorCode.INVALID_ARGUMENT, "only a succeeded call presents files")
        if completion.memory_change is not None and completion.status is not ToolExecutionStatus.SUCCEEDED:
            raise DataError(DataErrorCode.INVALID_ARGUMENT, "only a succeeded call changes memory")
        presented = self._stage_presented(conv, call, completion.presented_files)
        images = self._tool_images(conv, call, completion.model_result.blocks)
        memory = (None if completion.memory_change is None
                  else self._stage_memory(scope, call, completion.memory_change))
        if memory is not None:
            key, note = memory
            if note is None:
                del self._store.memories[key]
            else:
                self._put_memory(key[0], note)
        conv.files.extend(presented)
        self._mark_attached(images)
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

    # --- EnvironmentState ------------------------------------------------------

    async def has_environment(self, scope: TrustedScope, environment_kind: EnvironmentKind) -> bool:
        return environment_kind in self._conv(scope).environments

    # --- ConversationCatalog ----------------------------------------------------

    async def create_conversation(
        self,
        principal: PrincipalId,
        *,
        title: str | None = None,
        environments: Sequence[EnvironmentKind] = (),
        model_setting: ModelSetting | None = None,
    ) -> ConversationId:
        title = rules.normalize_title(title)
        rules.check_environments(environments)
        rules.check_model_setting(model_setting)
        scope = TrustedScope(principal, ConversationId(self._store.new_id("conv")))
        self._store.create_conversation(scope, title=title, model_setting=model_setting)
        self._store._conversations[scope.conversation_id].environments.update(environments)
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

    async def last_user_message_at(self, principal: PrincipalId) -> datetime | None:
        times = [e.created_at for conv in self._store._conversations.values()
                 if conv.owner == principal and conv.trashed_at is None
                 for e in conv.events if e.kind is EventKind.USER_MESSAGE]
        return max(times, default=None)

    async def most_recent_user_conversation(self, principal: PrincipalId) -> ConversationSummary | None:
        rows = await self.list_user_conversations(principal, limit=1)
        return rows[0] if rows else None

    async def list_user_conversations(
        self, principal: PrincipalId, *, limit: int = 50, offset: int = 0
    ) -> Sequence[ConversationSummary]:
        rules.check_page(limit, offset)
        rows: list[tuple[datetime, int, ConversationId, _Conversation]] = []
        for i, (cid, conv) in enumerate(self._store._conversations.items()):
            if conv.owner != principal or conv.trashed_at is not None:
                continue
            times = [e.created_at for e in conv.events if e.kind is EventKind.USER_MESSAGE]
            if times:
                rows.append((max(times), i, cid, conv))
        rows.sort(key=lambda row: row[:2], reverse=True)
        return tuple(self._summary(cid, conv) for _, _, cid, conv in rows[offset:offset + limit])

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
        return TrashedConversation(self._summary(scope.conversation_id, conv), tuple(sorted(conv.environments)))

    async def purge_conversation(self, scope: TrustedScope) -> None:
        conv = self._store._owned(scope)
        if conv.purge_started_at is None:
            raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"{scope.conversation_id} is not being deleted")
        del self._store._conversations[scope.conversation_id]
        for wake_id in [w for w, r in self._store.wakes.items() if r.conversation_id == scope.conversation_id]:
            del self._store.wakes[wake_id]  # V3-P87 (its keys stay)
        for key in [k for k, m in self._store.memories.items()
                    if k[0] == scope.principal_id and m.source_conversation == scope.conversation_id]:
            del self._store.memories[key]  # D5: the notes its calls wrote go with it
        for key in [k for k, (m, *_) in self._store.retired_memories.items()
                    if k[0] == scope.principal_id and m.source_conversation == scope.conversation_id]:
            del self._store.retired_memories[key]
        for key in [k for k, e in self._store.episodes.items() if e.conversation_id == scope.conversation_id]:
            del self._store.episodes[key]  # V3-P85 §11: its episodes, reading position and passes
        self._store.memory_cursors.pop(scope.conversation_id, None)
        for key, (entry, retired) in list(self._store.life.items()):  # character-life §2: the facts stay, unlinked
            if entry.source_conversation != scope.conversation_id:
                continue
            if entry.kind is LifeKind.BOND and entry.origin is not LifeOrigin.USER:  # character-bond §2: as notes
                del self._store.life[key]
            else:
                self._store.life[key] = (replace(entry, source_conversation=None, source_sequences=(), source=None),
                                         retired)
        for pass_id in [p for p, (_, mp) in self._store.memory_passes.items()
                        if mp.conversation_id == scope.conversation_id]:
            del self._store.memory_passes[pass_id]
            for table in (self._store.pass_calls, self._store.pass_items):
                for row_key in [k for k in table if k[0] == pass_id]:
                    del table[row_key]
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

    async def _public_presented_calls(self, scope: TrustedScope, conv: _Conversation) -> set[CallRef]:
        unshown = await self.unshown_wakes(scope)
        hidden_held = {w.held_run_id for w in self._store.wakes.values()
                       if w.conversation_id == scope.conversation_id and w.held_run_id is not None
                       and w.status in (WakeStatus.OPENED, WakeStatus.PASSED, WakeStatus.DROPPED)}
        result_calls = {e.payload.call for e in conv.events
                        if e.sequence not in unshown and isinstance(e.payload, ToolResultRecord)}
        public_calls: set[CallRef] = set()
        for call in result_calls:
            if call.run_id in hidden_held:
                continue
            record = conv.executions.get(call)
            if record is None or record.status is not ToolExecutionStatus.SUCCEEDED:
                continue
            wake = await self.wake_of_run(scope, call.run_id)
            if wake is not None and wake.status in (WakeStatus.OPENED, WakeStatus.PASSED, WakeStatus.DROPPED):
                continue
            public_calls.add(call)
        return public_calls

    async def read_presented_files(self, scope: TrustedScope, *, limit: int = 50,
                                   offset: int = 0) -> Sequence[FileRecord]:
        rules.check_page(limit, offset)
        conv = self._conv(scope)
        public_calls = await self._public_presented_calls(scope, conv)
        presented = sorted(
            (f for f in conv.files if f.origin is FileOrigin.PRESENTED and f.call in public_calls),
            key=lambda f: (f.created_at, f.file_id, f.version),
            reverse=True,
        )
        return tuple(self._record(f) for f in presented[offset: offset + limit])

    async def read_presented_file(self, scope: TrustedScope, file_id: FileId,
                                  version: int) -> FileRecord | None:
        conv = self._conv(scope)
        file = next((f for f in conv.files
                     if f.file_id == file_id and f.version == version and f.origin is FileOrigin.PRESENTED), None)
        if file is None or file.call is None or file.call not in await self._public_presented_calls(scope, conv):
            return None
        return self._record(file)

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

    # --- PersonaStore (V3-P108 §5.2) ------------------------------------------------

    def _personas_of(self, principal: PrincipalId) -> list[tuple[Persona, str]]:
        return sorted((row for (owner, _), row in self._store.personas.items() if owner == principal),
                      key=lambda row: row[0].persona_id)

    def _persona(self, principal: PrincipalId, persona_id: object) -> Persona:
        row = self._store.personas.get((principal, rules.check_persona_id(persona_id)))
        if row is None:
            raise DataError(DataErrorCode.NOT_FOUND, f"no persona {persona_id}")
        return row[0]

    def _check_name_free(self, principal: PrincipalId, key: str, besides: int | None) -> None:
        if any(k == key and p.persona_id != besides for p, k in self._personas_of(principal)):
            raise DataError(DataErrorCode.DUPLICATE_IDENTITY, "another persona has this name")

    def _choice(self, principal: PrincipalId) -> PersonaChoice:
        return self._store.persona_choices.get(principal, PersonaChoice(None, 0))

    async def list_personas(self, principal: PrincipalId) -> PersonaList:
        return PersonaList(tuple(p for p, _ in self._personas_of(principal)), self._choice(principal))

    async def create_persona(self, principal: PrincipalId, name: str, text: str) -> Persona:
        name, key = rules.check_persona_name(name)
        text = rules.check_persona_text(text)
        if len(self._personas_of(principal)) >= MAX_PERSONAS:
            raise DataError(DataErrorCode.LIMIT_EXCEEDED, f"at most {MAX_PERSONAS} personas")
        self._check_name_free(principal, key, None)
        persona_id = self._store.persona_ids.get(principal, 0) + 1
        self._store.persona_ids[principal] = persona_id
        now = datetime.now(UTC)
        persona = Persona(persona_id, name, text, 1, now, now)
        self._store.personas[(principal, persona_id)] = (persona, key)
        return persona

    async def update_persona(self, principal: PrincipalId, persona_id: int, name: str, text: str,
                             expected_revision: int) -> Persona:
        name, key = rules.check_persona_name(name)
        text = rules.check_persona_text(text)
        current = self._persona(principal, persona_id)
        if expected_revision != current.revision:
            raise DataError(DataErrorCode.VERSION_CONFLICT, f"the persona is at revision {current.revision}")
        self._check_name_free(principal, key, current.persona_id)
        persona = replace(current, name=name, text=text, revision=current.revision + 1, updated_at=datetime.now(UTC))
        self._store.personas[(principal, current.persona_id)] = (persona, key)
        return persona

    async def delete_persona(self, principal: PrincipalId, persona_id: int, expected_revision: int) -> PersonaChoice:
        current = self._persona(principal, persona_id)
        if expected_revision != current.revision:
            raise DataError(DataErrorCode.VERSION_CONFLICT, f"the persona is at revision {current.revision}")
        del self._store.personas[(principal, current.persona_id)]
        choice = self._choice(principal)
        if choice.persona_id == current.persona_id:
            choice = PersonaChoice(None, choice.revision + 1, datetime.now(UTC))
            self._store.persona_choices[principal] = choice
        return choice

    async def choose_persona(self, principal: PrincipalId, persona_id: int | None,
                             expected_revision: int) -> PersonaChoice:
        if persona_id is not None:
            self._persona(principal, persona_id)
        current = self._choice(principal)
        if expected_revision != current.revision:
            raise DataError(DataErrorCode.VERSION_CONFLICT, f"the choice is at revision {current.revision}")
        choice = PersonaChoice(persona_id, current.revision + 1, datetime.now(UTC))
        self._store.persona_choices[principal] = choice
        return choice

    async def active_persona(self, principal: PrincipalId) -> Persona | None:
        chosen = self._choice(principal).persona_id
        row = None if chosen is None else self._store.personas.get((principal, chosen))
        return None if row is None else row[0]

    # --- LifeStore, LifeWriteLog (character-life v0.1 §2, §3) -----------------------------

    def _live_entry(self, principal: PrincipalId, entry_id: object) -> LifeEntry:
        found = None if isinstance(entry_id, bool) or not isinstance(entry_id, int) else \
            self._store.life.get((principal, entry_id))
        if found is None or found[1] is not None:
            raise DataError(DataErrorCode.NOT_FOUND, f"no life entry {entry_id}")
        return found[0]

    def _live_facts(self, principal: PrincipalId, persona_id: int) -> list[LifeEntry]:
        return [e for (owner, _), (e, retired) in self._store.life.items()
                if owner == principal and retired is None and e.persona_id == persona_id and e.kind is LifeKind.FACT]

    def _new_life(self, principal: PrincipalId, entry: LifeEntry) -> LifeEntry:
        entry = replace(entry, entry_id=self._store.life_ids.get(principal, 0) + 1)
        self._store.life_ids[principal] = entry.entry_id
        self._store.life[(principal, entry.entry_id)] = (entry, None)
        return entry

    def _put_life(self, principal: PrincipalId, entry: LifeEntry) -> LifeEntry:
        self._store.life[(principal, entry.entry_id)] = (entry, None)
        return entry

    def _retire_life(self, principal: PrincipalId, entry: LifeEntry, now: datetime) -> None:
        self._store.life[(principal, entry.entry_id)] = (replace(entry, revision=entry.revision + 1, updated_at=now),
                                                         now)

    def _add_fact(self, principal: PrincipalId, fact: LifeEntry, *, dedupe: bool) -> LifeEntry | str:
        facts = self._live_facts(principal, fact.persona_id)
        if dedupe and rules.same_fact(fact.text, facts):
            return "duplicate"
        fits, retire = rules.fact_room(facts)
        if not fits:
            return "full"
        if retire is not None:
            self._retire_life(principal, retire, fact.created_at)
        return self._new_life(principal, fact)

    async def list_life(self, principal: PrincipalId, persona_id: int, *,
                        since: str | None = None) -> tuple[LifeEntry, ...]:
        persona_id = rules.check_life_persona(persona_id)
        since = None if since is None else rules.check_life_day(since)
        return rules.order_life(
            e for (owner, _), (e, retired) in self._store.life.items()
            if owner == principal and retired is None and e.persona_id == persona_id and e.kind is not LifeKind.BOND
            and (e.kind is LifeKind.FACT or since is None or (e.day or "") >= since
                 or (e.kind is LifeKind.ARC and (e.end_day is None or e.end_day >= since))))

    async def add_life_fact(self, principal: PrincipalId, persona_id: int, text: str) -> LifeEntry:
        persona_id = rules.check_life_persona(persona_id)
        text = rules.check_life_text(LifeKind.FACT, text)
        now = datetime.now(UTC)
        added = self._add_fact(principal, LifeEntry(0, persona_id, LifeKind.FACT, None, text, LifeOrigin.USER, 1,
                                                    now, now), dedupe=False)
        if not isinstance(added, LifeEntry):
            raise DataError(DataErrorCode.LIMIT_EXCEEDED, f"at most {MAX_LIFE_FACTS} facts, all yours")
        return added

    async def edit_life(self, principal: PrincipalId, entry_id: int, text: str, *,
                        expected_revision: int, title: str | None = None) -> LifeEntry:
        entry = self._live_entry(principal, entry_id)
        text = rules.check_life_text(entry.kind, text)
        titled = rules.edited_title(entry, title)
        if expected_revision != entry.revision:
            raise DataError(DataErrorCode.VERSION_CONFLICT, f"the entry is at revision {entry.revision}")
        return self._put_life(principal, replace(entry, text=text, title=titled, origin=LifeOrigin.USER,
                                                 revision=entry.revision + 1, updated_at=datetime.now(UTC)))

    async def retire_life(self, principal: PrincipalId, entry_id: int, *, expected_revision: int) -> None:
        entry = self._live_entry(principal, entry_id)
        if expected_revision != entry.revision:
            raise DataError(DataErrorCode.VERSION_CONFLICT, f"the entry is at revision {entry.revision}")
        self._retire_life(principal, entry, datetime.now(UTC))

    async def apply_life_changes(self, principal: PrincipalId, persona_id: int, changes: Sequence[LifeChange], *,
                                 conversation: ConversationId, source: str, now: datetime) -> tuple[str, ...]:
        persona_id = rules.check_life_persona(persona_id)
        outcomes: list[str] = []
        for change in changes:
            outcome = rules.life_change_outcome(change, self._live_facts(principal, persona_id))
            if outcome is not None:
                outcomes.append(outcome)
                continue
            text = None if change.text is None else rules.check_life_text(LifeKind.FACT, change.text)
            sequences = tuple(change.sequences)
            if change.op is LifeChangeOp.ADD:
                assert text is not None
                added = self._add_fact(principal, LifeEntry(0, persona_id, LifeKind.FACT, None, text,
                                                             LifeOrigin.CONVERSATION, 1, now, now, conversation,
                                                             sequences, source), dedupe=True)
                outcomes.append("applied" if isinstance(added, LifeEntry) else added)
                continue
            entry = self._live_entry(principal, change.entry_id)
            if change.op is LifeChangeOp.RETIRE:
                self._retire_life(principal, entry, now)
            else:
                assert text is not None
                self._put_life(principal, replace(entry, text=text, origin=LifeOrigin.CONVERSATION,
                                                  revision=entry.revision + 1, updated_at=now,
                                                  source_conversation=conversation, source_sequences=sequences,
                                                  source=source))
            outcomes.append("applied")
        return tuple(outcomes)

    async def life_principals(self, active_since: datetime) -> tuple[PrincipalId, ...]:
        return tuple(sorted({conv.owner for conv in self._store._conversations.values() if conv.trashed_at is None
                             and any(e.kind is EventKind.USER_MESSAGE and e.created_at >= active_since
                                     for e in conv.events)}))

    async def life_attempts(self, principal: PrincipalId, persona_id: int, day: str) -> int:
        return sum(1 for w in self._store.life_writes.values()
                   if w.owner == principal and w.record.persona_id == persona_id and w.record.day == day)

    async def life_usage(self, principal: PrincipalId, since: datetime) -> int:
        return sum(w.record.charged_tokens for w in self._store.life_writes.values()
                   if w.owner == principal and w.record.created_at >= since)

    def _life_write(self, write_id: object) -> _LifeWrite:
        found = self._store.life_writes.get(write_id) if isinstance(write_id, str) else None
        if found is None:
            raise DataError(DataErrorCode.NOT_FOUND, f"no life write {write_id}")
        return found

    async def begin_life_write(self, principal: PrincipalId, start: LifeWriteStart) -> str:
        if (not isinstance(start, LifeWriteStart) or not isinstance(start.body, bytes) or start.reserved_tokens < 0):
            raise DataError(DataErrorCode.INVALID_ARGUMENT, "not a life write")
        persona_id, day = rules.check_life_persona(start.persona_id), rules.check_life_day(start.day)
        write_id = self._store.new_id("lwrite")
        self._store.life_writes[write_id] = _LifeWrite(principal, LifeWrite(
            write_id, persona_id, day, LifeWriteStatus.PREPARED, start.provider_key, start.model_id,
            start.reserved_tokens, start.reserved_tokens, None, None, None, datetime.now(UTC), None), start.body)
        return write_id

    async def mark_life_write_dispatched(self, write_id: str) -> None:
        row = self._life_write(write_id)
        if row.record.status is not LifeWriteStatus.PREPARED:
            raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"{write_id} is not prepared")
        row.record = replace(row.record, status=LifeWriteStatus.DISPATCHED)

    async def record_life_write(self, write_id: str, outcome: RawRunOutcome) -> None:
        row = self._life_write(write_id)
        if row.record.status not in (LifeWriteStatus.PREPARED, LifeWriteStatus.DISPATCHED):
            raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"{write_id} is {row.record.status.value}")
        usage = outcome.usage if isinstance(outcome, RawReceived) else None
        reported = (None if usage is None or usage.input_tokens is None or usage.output_tokens is None
                    else (usage.input_tokens, usage.output_tokens))
        received = isinstance(outcome, RawReceived)
        row.response = outcome.body
        row.record = replace(
            row.record, status=LifeWriteStatus.RECEIVED if received else LifeWriteStatus.FAILED,
            charged_tokens=row.record.reserved_tokens if reported is None else sum(reported),
            input_tokens=None if reported is None else reported[0],
            output_tokens=None if reported is None else reported[1],
            error=None if isinstance(outcome, RawReceived) else outcome.error_code[:200],
            finished_at=None if received else datetime.now(UTC))

    async def apply_life_write(self, write_id: str, plan: LifePlan, *, now: datetime) -> tuple[LifeEntry, ...]:
        if not isinstance(plan, LifePlan):
            raise DataError(DataErrorCode.INVALID_ARGUMENT, "not a life plan")
        diary = rules.check_life_text(LifeKind.DIARY, plan.diary)
        plans = rules.check_life_plans(plan)
        facts = [rules.check_life_text(LifeKind.FACT, text) for text in plan.facts]
        mood = rules.writer_mood(plan, now)
        arc = rules.check_arc_move(plan.arc)
        row = self._life_write(write_id)
        if row.record.status is not LifeWriteStatus.RECEIVED:
            raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"{write_id} is {row.record.status.value}")
        principal, persona_id, day = row.owner, row.record.persona_id, row.record.day
        written: list[LifeEntry] = []
        before = next((e for (owner, _), (e, retired) in self._store.life.items()
                       if owner == principal and retired is None and e.persona_id == persona_id
                       and e.kind is LifeKind.DIARY and e.day == day), None)
        arc_id, moved = (None, None) if arc is None else self._move_arc(principal, persona_id, day, arc, write_id,
                                                                        now)
        if moved is not None:
            written.append(moved)
        if before is None:
            written.append(self._new_life(principal, LifeEntry(0, persona_id, LifeKind.DIARY, day, diary,
                                                               LifeOrigin.WRITER, 1, now, now, source=write_id,
                                                               arc_id=arc_id)))
        elif before.origin is not LifeOrigin.USER:  # §2: one diary a day; the user's stays
            written.append(self._put_life(principal, replace(before, text=diary, origin=LifeOrigin.WRITER,
                                                             revision=before.revision + 1, updated_at=now,
                                                             source=write_id, arc_id=arc_id)))
        for plan_day, text, at in plans:
            written.append(self._new_life(principal, LifeEntry(0, persona_id, LifeKind.PLAN, plan_day, text,
                                                               LifeOrigin.WRITER, 1, now, now, source=write_id,
                                                               time_of_day=at)))
        for text in facts:
            added = self._add_fact(principal, LifeEntry(0, persona_id, LifeKind.FACT, None, text, LifeOrigin.WRITER,
                                                        1, now, now, source=write_id), dedupe=True)
            if isinstance(added, LifeEntry):
                written.append(added)
        if mood is not None:  # character-state §2: the day's end mood, kept unless a later one is stored
            self._keep_mood(principal, replace(mood, persona_id=persona_id))
        row.record = replace(row.record, status=LifeWriteStatus.APPLIED, finished_at=now)
        return tuple(written)

    def _move_arc(self, principal: PrincipalId, persona_id: int, day: str, move: ArcMove, write_id: str,
                  now: datetime) -> tuple[int | None, LifeEntry | None]:
        """character-world §4: as SqliteData._move_arc."""
        arcs = [e for (owner, _), (e, retired) in self._store.life.items()
                if owner == principal and retired is None and e.persona_id == persona_id and e.arc_open]
        if move.op is ArcOp.START:
            if not rules.arc_starts(move, arcs):
                return None, None
            assert move.goal is not None
            started = self._new_life(principal, LifeEntry(0, persona_id, LifeKind.ARC, day, move.goal,
                                                          LifeOrigin.WRITER, 1, now, now, source=write_id,
                                                          title=move.title))
            return started.entry_id, started
        target = rules.arc_moved(move, arcs)
        if target is None:
            return None, None
        if move.op is ArcOp.ADVANCE:
            return target.entry_id, None
        return target.entry_id, self._put_life(principal, replace(target, revision=target.revision + 1,
                                                                  updated_at=now, end_day=day, ending=move.ending))

    @staticmethod
    def _end_write(row: _LifeWrite, status: LifeWriteStatus, error: str) -> None:
        if row.record.status not in (LifeWriteStatus.PREPARED, LifeWriteStatus.DISPATCHED, LifeWriteStatus.RECEIVED):
            return
        charged = 0 if row.record.status is LifeWriteStatus.PREPARED else row.record.charged_tokens
        row.record = replace(row.record, status=status, error=error[:200], charged_tokens=charged,
                             finished_at=datetime.now(UTC))

    async def fail_life_write(self, write_id: str, error: str) -> None:
        self._end_write(self._life_write(write_id), LifeWriteStatus.FAILED, error)

    async def interrupt_life_writes(self) -> int:
        open_ = [w for w in self._store.life_writes.values()
                 if w.record.status in (LifeWriteStatus.PREPARED, LifeWriteStatus.DISPATCHED, LifeWriteStatus.RECEIVED)]
        for row in open_:
            self._end_write(row, LifeWriteStatus.INTERRUPTED, "interrupted")
        return len(open_)

    async def list_life_writes(self, principal: PrincipalId, *, limit: int = 20) -> tuple[LifeWrite, ...]:
        return tuple(reversed([w.record for w in self._store.life_writes.values() if w.owner == principal]))[
            :max(0, limit)]

    # --- BondStore (character-bond v0.1 §2–§4) ---------------------------------------------

    def _live_bond(self, principal: PrincipalId, persona_id: int) -> list[LifeEntry]:
        return [e for (owner, _), (e, retired) in self._store.life.items()
                if owner == principal and retired is None and e.persona_id == persona_id and e.kind is LifeKind.BOND]

    def _add_bond(self, principal: PrincipalId, entry: LifeEntry, *, dedupe: bool) -> LifeEntry | str:
        entries = self._live_bond(principal, entry.persona_id)
        if dedupe and rules.same_bond(entry.tag, entry.text, entries):
            return "duplicate"
        fits, retire = rules.bond_room(entries)
        if not fits:
            return "full"
        if retire is not None:
            self._retire_life(principal, retire, entry.created_at)
        return self._new_life(principal, entry)

    async def list_bond(self, principal: PrincipalId, persona_id: int) -> tuple[LifeEntry, ...]:
        persona_id = rules.check_life_persona(persona_id)
        return rules.order_bond(self._live_bond(principal, persona_id))

    async def add_bond(self, principal: PrincipalId, persona_id: int, tag: BondTag, text: str,
                       month_day: str | None = None) -> LifeEntry:
        persona_id = rules.check_life_persona(persona_id)
        tag = rules.check_bond_tag(tag)
        text = rules.check_life_text(LifeKind.BOND, text)
        month_day = rules.check_month_day(tag, month_day)
        now = datetime.now(UTC)
        added = self._add_bond(principal, LifeEntry(0, persona_id, LifeKind.BOND, None, text, LifeOrigin.USER, 1, now,
                                                    now, tag=tag, month_day=month_day), dedupe=False)
        if not isinstance(added, LifeEntry):
            raise DataError(DataErrorCode.LIMIT_EXCEEDED, f"at most {MAX_BOND_ENTRIES} bond entries, all yours")
        return added

    async def edit_bond(self, principal: PrincipalId, entry_id: int, tag: BondTag, text: str,
                        month_day: str | None = None, *, expected_revision: int) -> LifeEntry:
        tag = rules.check_bond_tag(tag)
        text = rules.check_life_text(LifeKind.BOND, text)
        month_day = rules.check_month_day(tag, month_day)
        entry = self._live_entry(principal, entry_id)
        if entry.kind is not LifeKind.BOND:
            raise DataError(DataErrorCode.NOT_FOUND, f"no bond entry {entry_id}")
        if expected_revision != entry.revision:
            raise DataError(DataErrorCode.VERSION_CONFLICT, f"the entry is at revision {entry.revision}")
        return self._put_life(principal, replace(entry, tag=tag, text=text, month_day=month_day,
                                                 origin=LifeOrigin.USER, revision=entry.revision + 1,
                                                 updated_at=datetime.now(UTC)))

    async def apply_bond_changes(self, principal: PrincipalId, persona_id: int, changes: Sequence[BondChange], *,
                                 conversation: ConversationId, source: str, now: datetime) -> tuple[str, ...]:
        persona_id = rules.check_life_persona(persona_id)
        outcomes: list[str] = []
        for change in changes:
            outcome = rules.bond_change_outcome(change, self._live_bond(principal, persona_id))
            if outcome is not None:
                outcomes.append(outcome)
                continue
            sequences = tuple(change.sequences)
            if change.op is LifeChangeOp.RETIRE:
                self._retire_life(principal, self._live_entry(principal, change.entry_id), now)
                outcomes.append("applied")
                continue
            tag = rules.check_bond_tag(change.tag)
            text = rules.check_life_text(LifeKind.BOND, change.text)
            month_day = rules.check_month_day(tag, change.month_day)
            if change.op is LifeChangeOp.ADD:
                added = self._add_bond(principal, LifeEntry(0, persona_id, LifeKind.BOND, None, text,
                                                            LifeOrigin.CONVERSATION, 1, now, now, conversation,
                                                            sequences, source, tag, month_day), dedupe=True)
                outcomes.append("applied" if isinstance(added, LifeEntry) else added)
                continue
            entry = self._live_entry(principal, change.entry_id)
            self._put_life(principal, replace(entry, tag=tag, text=text, month_day=month_day,
                                              origin=LifeOrigin.CONVERSATION, revision=entry.revision + 1,
                                              updated_at=now, source_conversation=conversation,
                                              source_sequences=sequences, source=source))
            outcomes.append("applied")
        return tuple(outcomes)

    async def bond_stage(self, principal: PrincipalId, persona_id: int) -> BondStanding:
        persona_id = rules.check_life_persona(persona_id)
        return self._store.bond_stages.get((principal, persona_id), BondStanding(persona_id))

    async def read_bond(self, principal: PrincipalId, persona_id: int, *, now: datetime,
                        zone: str) -> BondSnapshot:
        persona_id = rules.check_life_persona(persona_id)
        where = rules.check_zone(zone)
        standing = self._store.bond_stages.get((principal, persona_id), BondStanding(persona_id))
        entries = rules.order_bond(self._live_bond(principal, persona_id))
        first = min((c.created_at for c in self._store._conversations.values() if c.owner == principal), default=None)
        met = None if first is None else first.astimezone(where).date()
        today = now.astimezone(where).date()
        moved = rules.settle_stage(standing, entries, met, today, where, now)
        if moved is not standing:
            self._store.bond_stages[(principal, persona_id)] = moved
        return BondSnapshot(moved, None if met is None else met.isoformat(),
                            0 if met is None else together_days(met, today), entries,
                            anniversaries_on(today, met, entries))

    async def set_bond_stage(self, principal: PrincipalId, persona_id: int, stage: BondStage, *,
                             expected_revision: int) -> BondStanding:
        persona_id = rules.check_life_persona(persona_id)
        if not isinstance(stage, BondStage):
            raise DataError(DataErrorCode.INVALID_ARGUMENT, f"no stage {stage!r}")
        current = self._store.bond_stages.get((principal, persona_id), BondStanding(persona_id))
        if expected_revision != current.revision:
            raise DataError(DataErrorCode.VERSION_CONFLICT, f"the stage is at revision {current.revision}")
        chosen = BondStanding(persona_id, stage, datetime.now(UTC), "user", current.revision + 1)
        self._store.bond_stages[(principal, persona_id)] = chosen
        return chosen

    # --- MoodStore (character-state v0.1 §2) -----------------------------------------------

    def _keep_mood(self, principal: PrincipalId, mood: HimariMood) -> HimariMood:
        kept = rules.kept_mood(self._store.moods.get((principal, mood.persona_id)), mood)
        self._store.moods[(principal, mood.persona_id)] = kept
        return kept

    async def read_mood(self, principal: PrincipalId, persona_id: int) -> HimariMood | None:
        persona_id = rules.check_life_persona(persona_id)
        return self._store.moods.get((principal, persona_id))

    async def set_mood(self, principal: PrincipalId, persona_id: int, mood: str, *, source: MoodSource,
                       at: datetime, reason: str | None = None) -> HimariMood:
        return self._keep_mood(principal, rules.check_mood(persona_id, mood, source, at, reason))

    # --- DeviceStore (V3-P83 §4) ----------------------------------------------------

    def _live_devices(self, principal: PrincipalId) -> list[_Device]:
        return [d for (owner, _), d in self._store.devices.items() if owner == principal and not d.revoked]

    def _device(self, principal: PrincipalId, device_id: object) -> _Device:
        row = self._store.devices.get((principal, device_id)) if isinstance(device_id, str) else None
        if row is None or row.revoked:
            raise DataError(DataErrorCode.NOT_FOUND, f"no device {device_id}")
        return row

    async def add_device(self, principal: PrincipalId, name: str, pairing_hash: bytes,
                         expires_at: datetime) -> Device:
        name = rules.check_device_name(name)
        live = self._live_devices(principal)
        waiting = [d for d in live if d.device.paired_at is None]
        live = [d for d in live if d.device.paired_at is not None]
        rules.check_device_name_free(name, (d.device.name for d in live))
        if len(live) >= MAX_DEVICES:
            raise DataError(DataErrorCode.LIMIT_EXCEEDED, f"{MAX_DEVICES} devices at most")
        for row in waiting:  # one live code at a time
            del self._store.devices[(principal, row.device.device_id)]
        number = self._store.device_ids.get(principal, 0) + 1
        self._store.device_ids[principal] = number
        device = Device(f"d{number}", name, datetime.now(UTC), expires_at, None, None)
        self._store.devices[(principal, device.device_id)] = _Device(device, pairing_hash=pairing_hash)
        return device

    async def pair_device(self, pairing_hash: bytes, token_hash: bytes, now: datetime) -> DevicePairing:
        found = next(((owner, row) for (owner, _), row in self._store.devices.items()
                      if row.pairing_hash == pairing_hash and not row.revoked), None)
        if found is None:
            return DevicePairing(PairingResult.INVALID)
        principal, row = found
        expires = row.device.pairing_expires_at
        if expires is None or expires <= now:
            return DevicePairing(PairingResult.EXPIRED)
        if row.device.paired_at is not None:
            if row.token_hash != token_hash:
                return DevicePairing(PairingResult.USED)
            return DevicePairing(PairingResult.RESENT, principal, row.device)
        if any(d.token_hash == token_hash for d in self._store.devices.values()):
            return DevicePairing(PairingResult.TOKEN_IN_USE)
        row.token_hash = token_hash
        row.device = replace(row.device, paired_at=now)
        return DevicePairing(PairingResult.PAIRED, principal, row.device)

    async def record_pairing_failure(self, now: datetime) -> None:
        for key, row in list(self._store.devices.items()):
            expires = row.device.pairing_expires_at
            if (row.device.paired_at is None and not row.revoked and row.pairing_hash is not None
                    and expires is not None and expires > now):
                row.pairing_failures += 1
            if row.device.paired_at is None and row.pairing_failures >= PAIRING_FAILURE_LIMIT:
                del self._store.devices[key]

    async def device_for_token(self, token_hash: bytes) -> tuple[PrincipalId, Device] | None:
        return next(((owner, row.device) for (owner, _), row in self._store.devices.items()
                     if row.token_hash == token_hash and not row.revoked and row.device.paired_at is not None), None)

    async def list_devices(self, principal: PrincipalId) -> tuple[Device, ...]:
        return tuple(d.device for d in self._live_devices(principal))

    async def rename_device(self, principal: PrincipalId, device_id: str, name: str) -> Device:
        name = rules.check_device_name(name)
        row = self._device(principal, device_id)
        rules.check_device_name_free(name, (d.device.name for d in self._live_devices(principal) if d is not row))
        row.device = replace(row.device, name=name)
        return row.device

    async def revoke_device(self, principal: PrincipalId, device_id: str) -> None:
        row = self._device(principal, device_id)
        row.revoked, row.token_hash, row.pairing_hash = True, None, None
        row.device = replace(row.device, pairing_expires_at=None)

    async def touch_device(self, principal: PrincipalId, device_id: str, at: datetime) -> None:
        row = self._store.devices.get((principal, device_id))
        if row is not None and not row.revoked:
            row.device = replace(row.device, last_seen_at=at)

    async def record_input(self, principal: PrincipalId, device_id: str, at: datetime) -> None:
        row = self._store.devices.get((principal, device_id))
        if row is not None and not row.revoked and (row.last_input_at is None or row.last_input_at < at):
            row.last_input_at = at

    async def read_inputs(self, principal: PrincipalId) -> Mapping[str, datetime]:
        return {device_id: row.last_input_at for (owner, device_id), row in self._store.devices.items()
                if owner == principal and not row.revoked and row.last_input_at is not None}

    # --- WakeStore (V3-P87) -------------------------------------------------------------

    async def begin_wake(self, scope: TrustedScope, reasons: Sequence[WakeReason], device_id: str, *,
                         keys: Sequence[str] = (), reminders: Sequence[int] = (),
                         interface: InterfaceSnapshot | None = None) -> ConversationEvent:
        rules.check_wake(reasons, device_id, keys, reminders)
        rules.check_interface(interface, MessageOrigin(OriginKind.DEVICE, device_id))
        conv = self._conv(scope)
        for key in keys:
            if (scope.principal_id, key) in self._store.wake_keys:
                raise DataError(DataErrorCode.DUPLICATE_IDENTITY, f"wake key {key!r} was taken")
        for memory_id in reminders:
            state = self._store.reminders.get((scope.principal_id, memory_id))
            if state is None or state[0] != "scheduled" \
                    or (scope.principal_id, memory_id) not in self._store.memories:
                raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"reminder m{memory_id} is not waiting")
        wake_id = self._store.new_id("wake")  # nothing above wrote: one step, as the transaction
        for key in keys:
            self._store.wake_keys[(scope.principal_id, key)] = wake_id
        for memory_id in reminders:
            _, _, attempts = self._store.reminders[(scope.principal_id, memory_id)]
            self._store.reminders[(scope.principal_id, memory_id)] = ("claimed", wake_id, attempts)
        event = self._new_event(conv, scope, Wake(wake_id, tuple(reasons), device_id, interface), None)
        self._store.wakes[wake_id] = WakeRecord(wake_id, scope.principal_id, scope.conversation_id, event.sequence,
                                                tuple(reasons), device_id, WakeStatus.OPENED, datetime.now(UTC),
                                                None)
        return event

    async def end_wake(self, principal: PrincipalId, wake_id: str, status: WakeStatus) -> WakeRecord:
        if status.in_progress:
            raise DataError(DataErrorCode.INVALID_ARGUMENT, "a wake ends answered, cancelled or failed")
        record = await self.read_wake(principal, wake_id)
        if not record.status.in_progress or (record.status is WakeStatus.SPEAKING
                                             and status in (WakeStatus.PASSED, WakeStatus.DROPPED)):
            return record
        ended = replace(record, status=status, ended_at=datetime.now(UTC))
        self._store.wakes[wake_id] = ended
        for key, (state, claimer, attempts) in list(self._store.reminders.items()):
            if key[0] != principal or state != "claimed" or claimer != wake_id:
                continue  # §5.3 (re-review P2): only what this very wake still claims
            if status is WakeStatus.ANSWERED:
                self._store.reminders[key] = ("delivered", None, attempts)
                self._store.reminded_at[key] = ended.ended_at or datetime.now(UTC)
            else:
                self._store.reminders[key] = ("missed" if attempts + 1 >= REMINDER_ATTEMPTS else "scheduled", None,
                                              attempts + 1)
        return ended

    async def read_wake(self, principal: PrincipalId, wake_id: str) -> WakeRecord:
        record = self._store.wakes.get(wake_id)
        if record is None or record.principal != principal:
            raise DataError(DataErrorCode.NOT_FOUND, f"no wake {wake_id}")
        return record

    async def opened_wakes(self) -> tuple[WakeRecord, ...]:
        return tuple(r for r in self._store.wakes.values() if r.status.in_progress)

    async def reveal_wake(self, principal: PrincipalId, wake_id: str) -> bool:
        record = await self.read_wake(principal, wake_id)
        if record.status is not WakeStatus.OPENED:
            return False
        self._store.wakes[wake_id] = replace(record, status=WakeStatus.SPEAKING)
        return True

    async def finalize_hidden_run(self, scope: TrustedScope, run_id: RunId, wake_id: str, status: WakeStatus) -> None:
        if status not in (WakeStatus.PASSED, WakeStatus.DROPPED):
            raise DataError(DataErrorCode.INVALID_ARGUMENT, "a hidden wake ends passed or dropped")
        conv = self._conv(scope)
        run = self._run(conv, run_id)
        record = await self.read_wake(scope.principal_id, wake_id)
        if run.record.status is RunStatus.RECEIVED:  # one step, as the transaction: nothing above wrote
            run.record = replace(run.record, status=RunStatus.PROJECTED)
        if record.status is WakeStatus.OPENED:
            self._store.wakes[wake_id] = replace(record, status=status, ended_at=datetime.now(UTC))

    async def hold_wake_run(self, scope: TrustedScope, wake_id: str, run_id: RunId) -> bool:
        self._run(self._conv(scope), run_id)
        record = await self.read_wake(scope.principal_id, wake_id)
        if record.conversation_id != scope.conversation_id or record.status is not WakeStatus.OPENED:
            return False
        if record.held_run_id is None:
            self._store.wakes[wake_id] = replace(record, held_run_id=run_id)
            return True
        return record.held_run_id == run_id

    async def wake_of_run(self, scope: TrustedScope, run_id: RunId) -> WakeRecord | None:
        conv = self._conv(scope)
        level: list[RunId] = [run_id]
        seen: set[RunId] = set()
        while level and len(seen) < 32:  # as SQLite (PR #167 re-review P1-7, P1-8)
            ids: set[str] = set()
            below: list[RunId] = []
            for current in level:
                if current in seen:
                    continue
                seen.add(current)
                for ref in self._run(conv, current).record.trigger_refs:
                    if isinstance(ref, EventRef):
                        ids.add(ref.event_id)
                    elif isinstance(ref, CompletedGroupRef):
                        below.append(ref.run_id)
            if ids:
                last = max((e.sequence for e in conv.events if e.event_id in ids), default=None)
                return next((w for w in self._store.wakes.values() if w.conversation_id == scope.conversation_id
                             and w.event_sequence == last and free_wake(w.reasons)), None)
            level = below
        return None

    async def unshown_wakes(self, scope: TrustedScope) -> frozenset[int]:
        conv = self._conv(scope)
        opened = [w for w in self._store.wakes.values()
                  if w.conversation_id == scope.conversation_id and w.status is WakeStatus.OPENED and free_wake(w.reasons)]
        held = {w.held_run_id for w in opened if w.held_run_id is not None}  # V3-P91 §8
        return frozenset({w.event_sequence for w in opened}
                         | {e.sequence for e in conv.events if e.origin_run_id is not None and e.origin_run_id in held})

    async def wakes_since(self, principal: PrincipalId, since: datetime) -> tuple[WakeRecord, ...]:
        return tuple(sorted((r for r in self._store.wakes.values() if r.principal == principal
                             and r.created_at >= since), key=lambda r: r.created_at))

    async def taken_keys(self, principal: PrincipalId, keys: Sequence[str]) -> frozenset[str]:
        return frozenset(k for k in keys if (principal, k) in self._store.wake_keys)

    async def miss_reminders(self, principal: PrincipalId, before: datetime) -> int:
        missed = 0
        for (owner, memory_id), note in self._store.memories.items():
            state = self._store.reminders.get((owner, memory_id))
            if owner == principal and state is not None and state[0] == "scheduled" \
                    and note.remind_instant is not None and note.remind_instant < before:
                self._store.reminders[(owner, memory_id)] = ("missed", None, state[2])
                missed += 1
        return missed

    async def release_keys(self, principal: PrincipalId, wake_id: str) -> None:
        for key in [k for k, w in self._store.wake_keys.items() if k[0] == principal and w == wake_id]:
            del self._store.wake_keys[key]

    async def expire_keys(self, principal: PrincipalId, keys: Sequence[str]) -> None:
        for key in keys:
            self._store.wake_keys.setdefault((principal, key), None)

    async def read_quiet(self, principal: PrincipalId) -> datetime | None:
        return self._store.quiet.get(principal)

    async def write_quiet(self, principal: PrincipalId, until: datetime | None) -> None:
        if until is None:
            self._store.quiet.pop(principal, None)
        else:
            self._store.quiet[principal] = until

    # --- MemoryStore (V3-P71) ---------------------------------------------------------

    def _memory(self, principal: PrincipalId, memory_id: object) -> Memory:
        note = self._store.memories.get((principal, memory_id)) if isinstance(memory_id, int) else None
        if note is None:
            raise DataError(DataErrorCode.NOT_FOUND, f"no note m{memory_id}")
        return self._reminded(principal, note)

    def _reminded(self, principal: PrincipalId, note: Memory) -> Memory:
        """A note with how its reminder was delivered (kept beside it, as wakes change it)."""
        state = self._store.reminders.get((principal, note.memory_id))
        return replace(note, remind_state=None if state is None else state[0],
                       reminded_at=self._store.reminded_at.get((principal, note.memory_id)))

    def _put_memory(self, principal: PrincipalId, note: Memory) -> None:
        """Every note write, as SQLite's _write_memory (V3-P87 §5.3): only a loop keeps
        a reminder; one written anew is scheduled afresh, one written as it was keeps
        its delivery."""
        note = rules.unreminded(note)
        key = (principal, note.memory_id)
        old = self._store.memories.get(key)
        if old is None and key in self._store.retired_memories:
            old = self._store.retired_memories[key][0]
        if old is None or (old.remind_at, old.remind_instant) != (note.remind_at, note.remind_instant):
            if note.remind_at is None:
                self._store.reminders.pop(key, None)
            else:
                self._store.reminders[key] = ("scheduled", None, 0)
            self._store.reminded_at.pop(key, None)
        self._store.memories[key] = replace(note, remind_state=None, reminded_at=None)

    def _memory_count(self, principal: PrincipalId) -> int:
        return sum(1 for owner, _ in self._store.memories if owner == principal)

    def _new_memory_id(self, principal: PrincipalId) -> int:
        self._store.memory_ids[principal] = self._store.memory_ids.get(principal, 0) + 1
        return self._store.memory_ids[principal]

    def _stage_memory(self, scope: TrustedScope, call: CallRef,
                      change: MemoryChange) -> tuple[tuple[PrincipalId, int], Memory | None]:
        """The note a Tool's change leaves (None: deleted), checked, not yet applied."""
        change = rules.check_memory_change(change)
        principal, key, now = scope.principal_id, (scope.principal_id, change.memory_id), datetime.now(UTC)
        if change.kind is MemoryChangeKind.SAVE and change.expected_revision is None:
            if (change.memory_id > self._store.memory_ids.get(principal, 0) or key in self._store.memories
                    or key in self._store.retired_memories):
                raise DataError(DataErrorCode.INVALID_ARGUMENT, f"m{change.memory_id} was not given out for a new note")
            if self._memory_count(principal) >= MAX_MEMORIES:
                raise DataError(DataErrorCode.LIMIT_EXCEEDED, f"{MAX_MEMORIES} notes are kept at most")
            assert change.category is not None and change.text is not None
            return key, Memory(change.memory_id, change.category, change.text, MemoryOrigin.MODEL,
                               scope.conversation_id, call, 1, now, now, due_at=change.due_at,
                               due_instant=change.due_instant, valid_until=change.valid_until,
                               valid_instant=change.valid_instant, remind_at=change.remind_at,
                               remind_instant=change.remind_instant)
        note = self._memory(principal, change.memory_id)
        if change.expected_revision != note.revision:
            raise DataError(DataErrorCode.VERSION_CONFLICT, f"m{note.memory_id} is at revision {note.revision}")
        if change.kind is MemoryChangeKind.FORGET:
            return key, None
        assert change.category is not None and change.text is not None
        return key, replace(note, category=change.category, text=change.text, origin=MemoryOrigin.MODEL,
                            source_conversation=scope.conversation_id, source_call=call,
                            revision=note.revision + 1, updated_at=now, source_pass=None, source_item=None,
                            user_approved_at=None, due_at=change.due_at, due_instant=change.due_instant,
                            valid_until=change.valid_until, valid_instant=change.valid_instant,
                            remind_at=change.remind_at, remind_instant=change.remind_instant)

    async def list_memories(self, principal: PrincipalId) -> tuple[Memory, ...]:
        return rules.order_memories(self._reminded(owner, m) for (owner, _), m in self._store.memories.items()
                                    if owner == principal)

    async def read_memory(self, principal: PrincipalId, memory_id: int) -> Memory:
        return self._memory(principal, memory_id)

    async def allocate_memory_id(self, principal: PrincipalId) -> int:
        return self._new_memory_id(principal)

    async def save_memory(self, principal: PrincipalId, category: MemoryCategory, text: str, *,
                          memory_id: int | None = None, expected_revision: int | None = None,
                          dates: NoteDates | None = None) -> Memory:
        category, now = rules.check_memory_category(category), datetime.now(UTC)
        text = rules.check_note_text(category, text)
        if memory_id is None:
            if self._memory_count(principal) >= MAX_MEMORIES:
                raise DataError(DataErrorCode.LIMIT_EXCEEDED, f"{MAX_MEMORIES} notes are kept at most")
            new_id = self._new_memory_id(principal)
            note = Memory(new_id, category, text, MemoryOrigin.USER, None, None, 1, now, now)
        else:
            current = self._memory(principal, memory_id)
            if expected_revision != current.revision:
                raise DataError(DataErrorCode.VERSION_CONFLICT, f"m{memory_id} is at revision {current.revision}")
            note = replace(current, category=category, text=text, origin=MemoryOrigin.USER,
                           revision=current.revision + 1, updated_at=now)
        note = rules.dated(note, dates)
        self._put_memory(principal, note)
        return self._memory(principal, note.memory_id)  # with its reminder's state

    async def delete_memory(self, principal: PrincipalId, memory_id: int,
                            expected_revision: int | None = None) -> Memory:
        note = self._memory(principal, memory_id)
        if expected_revision is not None and expected_revision != note.revision:
            raise DataError(DataErrorCode.VERSION_CONFLICT, f"m{memory_id} is at revision {note.revision}")
        del self._store.memories[(principal, memory_id)]
        return note

    async def restore_memory(self, principal: PrincipalId, before: Memory, *, expected_revision: int) -> Memory:
        category, text = rules.check_memory_category(before.category), rules.check_memory_text(before.text)
        current = self._memory(principal, before.memory_id)
        if expected_revision != current.revision:
            raise DataError(DataErrorCode.VERSION_CONFLICT, f"m{current.memory_id} is at revision {current.revision}")
        source = before.source_conversation
        if source is not None:
            conv = self._store._conversations.get(source)
            if conv is None or conv.owner != principal:
                raise DataError(DataErrorCode.NOT_FOUND, "the conversation it came from was deleted")
        note = replace(current, category=category, text=text, origin=before.origin, source_conversation=source,
                       source_call=None if source is None else before.source_call,
                       revision=current.revision + 1, updated_at=datetime.now(UTC), due_at=before.due_at,
                       due_instant=before.due_instant, valid_until=before.valid_until,
                       valid_instant=before.valid_instant, remind_at=before.remind_at,
                       remind_instant=before.remind_instant, source_pass=before.source_pass,
                       source_item=before.source_item, user_approved_at=before.user_approved_at)
        self._put_memory(principal, note)
        return self._memory(principal, note.memory_id)  # with its reminder's state

    async def count_source_memories(self, scope: TrustedScope) -> int:
        self._store._owned(scope)
        return sum(1 for (owner, _), m in self._store.memories.items()
                   if owner == scope.principal_id and m.source_conversation == scope.conversation_id)

    # --- MemoryPassLog (V3-P85) --------------------------------------------------------

    @staticmethod
    def _unfinished(conv: _Conversation) -> bool:
        return (any(r.record.status in (RunStatus.PREPARED, RunStatus.DISPATCH_CLAIMED, RunStatus.RECEIVED)
                    for r in conv.runs.values())
                or any(e.status is ToolExecutionStatus.EXECUTION_CLAIMED for e in conv.executions.values()))

    async def unretire_memory(self, principal: PrincipalId, memory_id: int, *, expected_revision: int,
                              now: datetime) -> Memory:
        found = self._store.retired_memories.get((principal, memory_id))
        if found is None or found[1] <= now - RETIRED_KEEP:
            raise DataError(DataErrorCode.NOT_FOUND, f"no retired note m{memory_id}")
        note = found[0]
        if note.source_conversation is not None:
            conv = self._store._conversations.get(note.source_conversation)
            if conv is None or conv.owner != principal:
                raise DataError(DataErrorCode.NOT_FOUND, "the conversation it came from was deleted")
        if expected_revision != note.revision:
            raise DataError(DataErrorCode.VERSION_CONFLICT, f"m{memory_id} is at revision {note.revision}")
        if self._memory_count(principal) >= MAX_MEMORIES:
            raise DataError(DataErrorCode.LIMIT_EXCEEDED, f"{MAX_MEMORIES} notes are kept at most")
        back = replace(note, revision=note.revision + 1, updated_at=now)
        self._put_memory(principal, back)  # while still retired: its reminder keeps how it was delivered (as SQLite)
        del self._store.retired_memories[(principal, memory_id)]
        return self._memory(principal, memory_id)

    async def memory_revision(self, principal: PrincipalId, memory_id: int) -> tuple[int, bool] | None:
        note = self._store.memories.get((principal, memory_id))
        if note is not None:
            return note.revision, False
        retired = self._store.retired_memories.get((principal, memory_id))
        return None if retired is None else (retired[0].revision, True)

    async def memory_revisions(self, principal: PrincipalId, memory_ids: Sequence[int]) -> dict[int, tuple[int, bool]]:
        found: dict[int, tuple[int, bool]] = {}
        for memory_id in memory_ids:
            state = await self.memory_revision(principal, memory_id)
            if state is not None:
                found[memory_id] = state
        return found

    async def count_source_episodes(self, scope: TrustedScope) -> int:
        self._store._owned(scope)
        return len(self._episodes_of(scope.principal_id, scope.conversation_id))

    async def page_episodes(self, principal: PrincipalId, *, before: tuple[datetime, int] | None,
                            limit: int) -> tuple[RecentEpisode, ...]:
        return self._with_titles((e for e in self._episodes_of(principal)
                                  if before is None or (e.ended_at, e.episode_id) < before), limit)

    def _episode(self, principal: PrincipalId, episode_id: object, expected_revision: int) -> Episode:
        episode = self._store.episodes.get((principal, episode_id)) if isinstance(episode_id, int) else None
        if episode is None:
            raise DataError(DataErrorCode.NOT_FOUND, f"no episode e{episode_id}")
        if expected_revision != episode.revision:
            raise DataError(DataErrorCode.VERSION_CONFLICT, f"e{episode_id} is at revision {episode.revision}")
        return episode

    async def update_episode(self, principal: PrincipalId, episode_id: int, text: str, *,
                             expected_revision: int) -> Episode:
        text = rules.check_episode_text(text)
        episode = self._episode(principal, episode_id, expected_revision)
        rewritten = replace(episode, text=text, origin=MemoryOrigin.USER, revision=episode.revision + 1,
                            updated_at=datetime.now(UTC))
        self._store.episodes[(principal, episode_id)] = rewritten
        return rewritten

    async def delete_episode(self, principal: PrincipalId, episode_id: int, *, expected_revision: int) -> None:
        self._episode(principal, episode_id, expected_revision)
        del self._store.episodes[(principal, episode_id)]

    def _with_titles(self, episodes: Iterable[Episode], limit: int) -> tuple[RecentEpisode, ...]:
        found: list[RecentEpisode] = []
        for episode in episodes:  # newest first
            conv = self._store._conversations.get(episode.conversation_id)
            if conv is None or conv.trashed_at is not None:
                continue
            found.append(RecentEpisode(episode, conv.title))
            if len(found) == limit:
                break
        return tuple(found)

    async def recent_episodes(self, principal: PrincipalId, *, since: datetime, besides: ConversationId | None,
                              limit: int) -> tuple[RecentEpisode, ...]:
        return self._with_titles((e for e in self._episodes_of(principal)
                                  if e.ended_at >= since and e.conversation_id != besides), limit)

    async def count_episodes(self, principal: PrincipalId) -> int:
        return sum(1 for owner, _ in self._store.episodes if owner == principal)

    async def find_episodes(self, principal: PrincipalId, words: Sequence[str], *, start: datetime | None = None,
                            end: datetime | None = None, limit: int = 200) -> tuple[RecentEpisode, ...]:
        folded = list(dict.fromkeys(rules.fold(w) for w in words if w.strip()))
        found = [e for e in self._episodes_of(principal)  # newest first
                 if (start is None or e.ended_at >= start) and (end is None or e.ended_at <= end)
                 and (not folded or any(w in rules.fold(e.text) for w in folded))]
        if folded:
            found.sort(key=lambda e: sum(1 for w in folded if w in rules.fold(e.text)), reverse=True)  # stable
        elif start is not None or end is not None:
            found.reverse()
        return self._with_titles(found, limit)

    async def read_memory_cursor(self, scope: TrustedScope) -> MemoryCursor:
        self._store._owned(scope)
        return self._store.memory_cursors.get(scope.conversation_id, MemoryCursor())

    async def memory_due(self, *, quiet_before: datetime, now: datetime, after: ConversationId | None = None,
                         limit: int = 50) -> Sequence[DueConversation]:
        due: list[DueConversation] = []
        for cid in sorted(c for c in self._store._conversations if c > (after or "")):
            conv = self._store._conversations[cid]
            cursor = self._store.memory_cursors.get(cid, MemoryCursor())
            if (conv.trashed_at is not None or not conv.events
                    or not any(e.kind is EventKind.USER_MESSAGE and e.sequence > cursor.through_sequence
                               for e in conv.events)
                    or (cursor.retry_after is not None and cursor.retry_after > now)
                    or self._unfinished(conv) or conv.events[-1].created_at >= quiet_before):
                continue
            due.append(DueConversation(TrustedScope(conv.owner, cid), cursor, conv.events[-1].created_at))
            if len(due) == limit:
                break
        return tuple(due)

    async def last_event(self, scope: TrustedScope) -> tuple[int, datetime] | None:
        conv = self._store._authorized(scope)
        return (conv.events[-1].sequence, conv.events[-1].created_at) if conv.events else None

    async def unfinished_work(self, scope: TrustedScope) -> bool:
        return self._unfinished(self._store._owned(scope))

    def _episodes_of(self, principal: PrincipalId,
                     conversation: ConversationId | None = None) -> list[Episode]:
        found = [e for (owner, _), e in self._store.episodes.items()
                 if owner == principal and (conversation is None or e.conversation_id == conversation)]
        return sorted(found, key=lambda e: (e.ended_at, e.episode_id), reverse=True)

    async def last_episode(self, scope: TrustedScope) -> Episode | None:
        self._store._owned(scope)
        found = self._episodes_of(scope.principal_id, scope.conversation_id)
        return found[0] if found else None

    async def list_episodes(self, principal: PrincipalId) -> tuple[Episode, ...]:
        return tuple(self._episodes_of(principal))

    async def pass_usage(self, principal: PrincipalId, since: datetime) -> int:
        owned = {p for p, (owner, _) in self._store.memory_passes.items() if owner == principal}
        total = 0
        for (pass_id, _), row in self._store.pass_calls.items():
            prepared, charged = row["prepared_at"], row["charged"]
            assert isinstance(prepared, datetime) and isinstance(charged, int)
            if pass_id in owned and prepared >= since:
                total += charged
        return total

    def _pass(self, pass_id: object) -> tuple[PrincipalId, MemoryPass]:
        found = self._store.memory_passes.get(pass_id) if isinstance(pass_id, str) else None
        if found is None:
            raise DataError(DataErrorCode.NOT_FOUND, f"no memory pass {pass_id}")
        return found

    def _running_pass(self, pass_id: str) -> tuple[PrincipalId, MemoryPass]:
        owner, mp = self._pass(pass_id)
        if mp.status is not PassStatus.RUNNING:
            raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"{pass_id} is {mp.status.value}")
        return owner, mp

    def _put_pass(self, owner: PrincipalId, mp: MemoryPass) -> None:
        self._store.memory_passes[mp.pass_id] = (owner, mp)

    async def begin_pass(self, scope: TrustedScope, start: PassStart) -> str:
        if (not isinstance(start, PassStart) or not isinstance(start.kind, PassKind)
                or not 0 <= start.from_sequence < start.through_sequence <= start.snapshot_sequence
                or start.omitted_chars < 0 or (start.kind is PassKind.RETRY) != (start.retry_of is not None)):
            raise DataError(DataErrorCode.INVALID_ARGUMENT, "not a pass start")
        try:
            self._store._authorized(scope)
        except DataError:
            raise DataError(DataErrorCode.NOT_FOUND, scope.conversation_id) from None
        if any(owner == scope.principal_id and mp.status is PassStatus.RUNNING
               for owner, mp in self._store.memory_passes.values()):
            raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, "another pass of this principal runs")
        pass_id = self._store.new_id("mpass")
        self._put_pass(scope.principal_id, MemoryPass(
            pass_id, scope.conversation_id, start.kind, start.from_sequence, start.through_sequence,
            start.snapshot_sequence, PassStatus.RUNNING, start.provider_key, start.model_id, 0, 0, 0, 0, 0, 0,
            start.omitted_chars, None, start.retry_of, datetime.now(UTC), None))
        return pass_id

    async def prepare_pass_call(self, pass_id: str, body: bytes, content_type: str, reserved_tokens: int) -> int:
        if not isinstance(body, bytes) or reserved_tokens < 0:
            raise DataError(DataErrorCode.INVALID_ARGUMENT, "a call stores its request and a reservation")
        owner, mp = self._running_pass(pass_id)
        call = mp.calls + 1
        self._store.pass_calls[(pass_id, call)] = {"status": "prepared", "reserved": reserved_tokens,
                                                   "prepared_at": datetime.now(UTC), "charged": reserved_tokens,
                                                   "request": body, "response": None}
        self._put_pass(owner, replace(mp, calls=call, reserved_tokens=reserved_tokens))
        return call

    async def mark_pass_call_dispatched(self, pass_id: str, call: int) -> None:
        self._running_pass(pass_id)
        row = self._store.pass_calls.get((pass_id, call))
        if row is None or row["status"] != "prepared":
            raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"call {call} of {pass_id} is not prepared")
        row["status"] = "dispatched"

    async def record_pass_call(self, pass_id: str, call: int, outcome: RawRunOutcome) -> None:
        owner, mp = self._pass(pass_id)
        row = self._store.pass_calls.get((pass_id, call))
        if row is None or row["status"] not in ("prepared", "dispatched"):
            raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"call {call} of {pass_id} is recorded")
        usage = outcome.usage if isinstance(outcome, RawReceived) else None
        reserved = row["reserved"]
        assert isinstance(reserved, int)
        reported = (None if usage is None or usage.input_tokens is None or usage.output_tokens is None
                    else usage.input_tokens + usage.output_tokens)
        row.update(status="received" if isinstance(outcome, RawReceived) else "failed", response=outcome.body,
                   charged=reserved if reported is None else reported)
        if usage is None or usage.input_tokens is None or usage.output_tokens is None:
            self._put_pass(owner, replace(mp, reserved_tokens=0, unmetered_tokens=mp.unmetered_tokens + reserved))
        else:
            self._put_pass(owner, replace(
                mp, reserved_tokens=0, input_tokens=mp.input_tokens + usage.input_tokens,
                output_tokens=mp.output_tokens + usage.output_tokens,
                over_estimate=mp.over_estimate + int(usage.input_tokens + usage.output_tokens > reserved)))

    async def read_pass_response(self, pass_id: str, call: int) -> bytes | None:
        row = self._store.pass_calls.get((pass_id, call))
        if row is None:
            raise DataError(DataErrorCode.NOT_FOUND, f"no call {call} of {pass_id}")
        response = row["response"]
        assert response is None or isinstance(response, bytes)
        return response

    def _settle_calls(self, owner: PrincipalId, mp: MemoryPass) -> MemoryPass:
        """§4.8, before a pass ends: a call that may have been sent and has no
        recorded outcome counts as its reservation used; one never sent does not."""
        uncertain = 0
        for (pass_id, _), row in self._store.pass_calls.items():
            if pass_id != mp.pass_id:
                continue
            if row["status"] == "dispatched":
                reserved = row["reserved"]
                assert isinstance(reserved, int)
                uncertain += reserved
                row["status"] = "uncertain"
            elif row["status"] == "prepared":
                row.update(status="abandoned", charged=0)
        settled = replace(mp, reserved_tokens=0, unmetered_tokens=mp.unmetered_tokens + uncertain)
        self._put_pass(owner, settled)
        return settled

    def _count_failure(self, owner: PrincipalId, mp: MemoryPass, status: PassStatus, error: str,
                       retry_after: datetime) -> PassStatus:
        mp = self._settle_calls(owner, mp)
        final = status
        if mp.kind is PassKind.LIVE:
            cursor = self._store.memory_cursors.get(mp.conversation_id, MemoryCursor())
            if cursor.through_sequence == mp.from_sequence:
                failures = cursor.failures + 1
                if failures >= SKIP_AFTER_FAILURES:
                    final = PassStatus.SKIPPED
                    cursor = replace(cursor, through_sequence=mp.through_sequence, failures=0, retry_after=None)
                else:
                    cursor = replace(cursor, failures=failures, retry_after=retry_after)
                self._store.memory_cursors[mp.conversation_id] = cursor
        self._put_pass(owner, replace(mp, status=final, error=error[:200], finished_at=datetime.now(UTC)))
        return final

    async def fail_pass(self, pass_id: str, error: str, retry_after: datetime) -> PassStatus:
        owner, mp = self._running_pass(pass_id)
        return self._count_failure(owner, mp, PassStatus.FAILED, error, retry_after)

    async def interrupt_running_passes(self, retry_after: datetime) -> int:
        running = [(o, mp) for o, mp in self._store.memory_passes.values() if mp.status is PassStatus.RUNNING]
        for owner, mp in running:
            self._count_failure(owner, mp, PassStatus.INTERRUPTED, "interrupted", retry_after)
        return len(running)

    def _pass_checks(self, owner: PrincipalId, mp: MemoryPass) -> str | None:
        conv = self._store._conversations.get(mp.conversation_id)
        if conv is None or conv.owner != owner or conv.trashed_at is not None:
            return "conversation_gone"
        if any(o.conversation_id == mp.conversation_id and o.status is PassStatus.APPLIED
               and rules.overlaps(mp.from_sequence, mp.through_sequence, o.from_sequence, o.through_sequence)
               for _, o in self._store.memory_passes.values()):
            return "already_read"
        cursor = self._store.memory_cursors.get(mp.conversation_id, MemoryCursor())
        if mp.kind is PassKind.LIVE:
            if cursor.through_sequence != mp.from_sequence:
                return "cursor_moved"
            if (conv.events[-1].sequence if conv.events else 0) != mp.snapshot_sequence:
                return "new_events"
            if self._unfinished(conv):
                return "unfinished_work"
        elif mp.kind is PassKind.BACKFILL:
            if cursor.backfill_through != mp.from_sequence or mp.through_sequence > cursor.baseline_sequence:
                return "backfill_moved"
        else:
            skipped = self._store.memory_passes.get(mp.retry_of or "")
            if (skipped is None or skipped[1].conversation_id != mp.conversation_id
                    or skipped[1].status is not PassStatus.SKIPPED
                    or (skipped[1].from_sequence, skipped[1].through_sequence)
                    != (mp.from_sequence, mp.through_sequence)):
                return "not_skipped"
        return None

    async def apply_pass(self, pass_id: str, items: Sequence[PassItem], episode: EpisodePlan | None, *,
                         now: datetime) -> PassApplied:
        if (not all(isinstance(i, PassItem) and isinstance(i.action, PassAction) for i in items)
                or len({i.item for i in items}) != len(items)
                or (episode is not None and not isinstance(episode, EpisodePlan))):
            raise DataError(DataErrorCode.INVALID_ARGUMENT, "not a pass plan")
        if pass_id not in self._store.memory_passes:  # its Conversation was deleted for good meanwhile
            return PassApplied(PassStatus.DISCARDED, reason="conversation_gone")
        owner, mp = self._pass(pass_id)
        if mp.status is not PassStatus.RUNNING:
            return PassApplied(mp.status, reason="not_running")
        why = self._pass_checks(owner, mp)
        if why is not None:
            mp = self._settle_calls(owner, mp)
            self._put_pass(owner, replace(mp, status=PassStatus.DISCARDED, error=why, finished_at=datetime.now(UTC)))
            return PassApplied(PassStatus.DISCARDED, reason=why)
        outcomes: list[str] = []
        for item in items:
            note = (self._store.memories.get((owner, item.memory_id))
                    if item.memory_id is not None and item.action in (PassAction.UPDATE, PassAction.FORGET) else None)
            note = None if note is None else self._reminded(owner, note)  # with its reminder's state (as SQLite)
            outcome, action, normal = rules.decide_item(item, mp.kind, note, self._memory_count(owner))
            target: str | None = None
            previous: str | None = None
            written: int | None = None
            if action is PassAction.ADD:
                assert normal.category is not None and normal.text is not None
                added = Memory(self._new_memory_id(owner), normal.category, normal.text, MemoryOrigin.EXTRACTOR,
                               mp.conversation_id, None, 1, now, now, due_at=normal.due_at,
                               due_instant=normal.due_instant, valid_until=normal.valid_until,
                               valid_instant=normal.valid_instant, source_pass=pass_id, source_item=item.item)
                self._put_memory(owner, added)
                target, written = f"m{added.memory_id}", 1
            elif action is PassAction.UPDATE:
                assert note is not None and normal.category is not None and normal.text is not None
                previous = rules.memory_json(note)
                changed = replace(note, category=normal.category, text=normal.text, origin=MemoryOrigin.EXTRACTOR,
                                  source_conversation=mp.conversation_id, source_call=None, source_pass=pass_id,
                                  source_item=item.item, due_at=normal.due_at, due_instant=normal.due_instant,
                                  valid_until=normal.valid_until, valid_instant=normal.valid_instant,
                                  revision=note.revision + 1, updated_at=now)
                self._put_memory(owner, changed)
                target, written = f"m{note.memory_id}", changed.revision
            elif action is PassAction.FORGET:
                assert note is not None
                previous = rules.memory_json(note)
                retired = replace(note, revision=note.revision + 1, updated_at=now)
                del self._store.memories[(owner, note.memory_id)]
                self._store.retired_memories[(owner, note.memory_id)] = (retired, now, pass_id, item.item)
                target, written = f"m{note.memory_id}", retired.revision
            elif item.memory_id is not None:
                target = f"m{item.memory_id}"
            planned = (rules.planned_json(normal) if outcome in ("applied", "proposed", "sensitive_proposed")
                       and normal.action is not PassAction.FORGET else None)
            self._store.pass_items[(pass_id, item.item)] = {
                "proposal": item.proposal, "planned": planned, "outcome": outcome, "target": target,
                "previous": previous, "written": written, "decided_at": None, "created_at": now}
            outcomes.append(outcome)
        episode_id = None if episode is None else self._write_episode(owner, mp, episode, now)
        cursor = self._store.memory_cursors.get(mp.conversation_id, MemoryCursor())
        if mp.kind is PassKind.LIVE:
            self._store.memory_cursors[mp.conversation_id] = replace(
                cursor, through_sequence=mp.through_sequence, failures=0, retry_after=None)
        elif mp.kind is PassKind.BACKFILL:
            done = mp.through_sequence >= cursor.baseline_sequence
            self._store.memory_cursors[mp.conversation_id] = replace(
                cursor, backfill_through=0 if done else mp.through_sequence,
                baseline_sequence=0 if done else cursor.baseline_sequence)
        else:
            assert mp.retry_of is not None
            skipped_owner, skipped = self._store.memory_passes[mp.retry_of]
            self._put_pass(skipped_owner, replace(skipped, status=PassStatus.RETRIED))
        mp = self._settle_calls(owner, self._store.memory_passes[pass_id][1])
        self._put_pass(owner, replace(mp, status=PassStatus.APPLIED, finished_at=now))
        return PassApplied(PassStatus.APPLIED, tuple(outcomes), episode_id)

    def _write_episode(self, owner: PrincipalId, mp: MemoryPass, plan: EpisodePlan, now: datetime) -> int | None:
        try:
            text = rules.check_episode_text(plan.text)
        except DataError:
            return None
        found = self._episodes_of(owner, mp.conversation_id)
        previous = found[0] if found else None
        if rules.extends(plan, mp.kind, previous):
            assert previous is not None
            self._store.episodes[(owner, previous.episode_id)] = replace(
                previous, through_sequence=mp.through_sequence, ended_at=plan.ended_at, text=text,
                user_sequences=previous.user_sequences + plan.user_sequences,
                himari_sequences=previous.himari_sequences + plan.himari_sequences, source_pass=mp.pass_id,
                revision=previous.revision + 1, updated_at=now)
            return previous.episode_id
        episode_id = self._store.episode_ids[owner] = self._store.episode_ids.get(owner, 0) + 1
        self._store.episodes[(owner, episode_id)] = Episode(
            episode_id, mp.conversation_id, mp.from_sequence, mp.through_sequence, plan.started_at, plan.ended_at,
            text, plan.user_sequences, plan.himari_sequences, MemoryOrigin.EXTRACTOR, mp.pass_id, 1, now, now)
        for old in self._episodes_of(owner)[MAX_EPISODES:]:
            del self._store.episodes[(owner, old.episode_id)]
        return episode_id

    async def read_pass(self, pass_id: str) -> MemoryPass:
        return self._pass(pass_id)[1]

    async def list_passes(self, scope: TrustedScope) -> tuple[MemoryPass, ...]:
        self._store._owned(scope)
        return tuple(mp for _, mp in self._store.memory_passes.values() if mp.conversation_id == scope.conversation_id)

    async def read_pass_outcomes(self, pass_id: str) -> tuple[str, ...]:
        self._pass(pass_id)
        rows = sorted((k[1], v) for k, v in self._store.pass_items.items() if k[0] == pass_id)
        return tuple(str(v["outcome"]) for _, v in rows)

    @staticmethod
    def _item_record(pass_id: str, item: int, row: Mapping[str, object]) -> PassItemRecord:
        def text(key: str) -> str | None:
            value = row[key]
            return value if isinstance(value, str) else None

        written, decided, created = row["written"], row["decided_at"], row["created_at"]
        assert isinstance(created, datetime)
        return PassItemRecord(pass_id, item, str(row["proposal"]), text("planned"), str(row["outcome"]),
                              text("target"), text("previous"), written if isinstance(written, int) else None,
                              decided if isinstance(decided, datetime) else None, created)

    async def read_pass_items(self, pass_id: str) -> tuple[PassItemRecord, ...]:
        self._pass(pass_id)
        return tuple(self._item_record(p, i, row) for (p, i), row in sorted(self._store.pass_items.items())
                     if p == pass_id)

    async def applied_pass_items(self, scope: TrustedScope, *,
                                 limit: int) -> tuple[tuple[MemoryPass, tuple[PassItemRecord, ...]], ...]:
        self._store._owned(scope)
        shown = ("applied", "kept", "sensitive_proposed")
        found: list[tuple[MemoryPass, tuple[PassItemRecord, ...]]] = []
        for _, mp in self._store.memory_passes.values():  # in creation order
            if mp.conversation_id != scope.conversation_id or mp.status is not PassStatus.APPLIED:
                continue
            items = tuple(self._item_record(p, i, row) for (p, i), row in sorted(self._store.pass_items.items())
                          if p == mp.pass_id and row["outcome"] in shown)
            if items:
                found.append((mp, items))
        return tuple(found[-limit:])

    async def list_proposals(self, principal: PrincipalId) -> tuple[Proposal, ...]:
        order = {p: n for n, p in enumerate(self._store.memory_passes)}
        found: list[tuple[datetime, int, int, Proposal]] = []
        for (pass_id, item), row in self._store.pass_items.items():
            owner, mp = self._store.memory_passes[pass_id]
            if owner != principal or row["outcome"] not in ("proposed", "sensitive_proposed"):
                continue
            conv = self._store._conversations[mp.conversation_id]
            record = self._item_record(pass_id, item, row)
            found.append((record.created_at, order[pass_id], item,
                          Proposal(record, mp.conversation_id, mp.kind, conv.title, conv.trashed_at is not None)))
        return tuple(p for *_, p in sorted(found, key=lambda f: f[:3]))

    async def decide_proposal(self, principal: PrincipalId, pass_id: str, item: int, *, keep: bool,
                              now: datetime, dates: NoteDates | None = None) -> Memory | None:
        found = self._store.memory_passes.get(pass_id)
        row = None if found is None or found[0] != principal else self._store.pass_items.get((pass_id, item))
        if found is None or row is None:
            raise DataError(DataErrorCode.NOT_FOUND, f"no proposal {pass_id}/{item}")
        if row["outcome"] not in ("proposed", "sensitive_proposed"):
            raise DataError(DataErrorCode.VERSION_CONFLICT, f"{pass_id}/{item} was {row['outcome']}")
        note = None
        if keep:
            planned_text = row["planned"]
            planned = planned_item(planned_text, item, PassAction.ADD) if isinstance(planned_text, str) else None
            if planned is None or planned.category is None:
                raise DataError(DataErrorCode.INVALID_ARGUMENT, "the proposal names no note")
            text = rules.check_note_text(planned.category, planned.text)
            if self._memory_count(principal) >= MAX_MEMORIES:
                raise DataError(DataErrorCode.LIMIT_EXCEEDED, f"{MAX_MEMORIES} notes are kept at most")
            proposed = Memory(0, planned.category, text, MemoryOrigin.EXTRACTOR, found[1].conversation_id, None, 1,
                              now, now, due_at=planned.due_at, due_instant=planned.due_instant,
                              valid_until=planned.valid_until, valid_instant=planned.valid_instant,
                              source_pass=pass_id, source_item=item, user_approved_at=now)
            proposed = rules.check_fresh(proposed if dates is None else rules.dated(proposed, dates), now)
            note = replace(proposed, memory_id=self._new_memory_id(principal))
            self._put_memory(principal, note)
        row.update(outcome="kept" if keep else "discarded", decided_at=now,
                   target=row["target"] if note is None else f"m{note.memory_id}")
        return note

    async def pass_summary(self, principal: PrincipalId, *, day_start: datetime, since: datetime) -> PassSummary:
        mine = [mp for owner, mp in self._store.memory_passes.values() if owner == principal]
        applied = [mp.finished_at for mp in mine if mp.status is PassStatus.APPLIED and mp.finished_at is not None]
        return PassSummary(
            await self.pass_usage(principal, day_start), max(applied, default=None),
            tuple(mp for mp in mine if mp.status is PassStatus.SKIPPED and mp.finished_at is not None
                  and mp.finished_at >= since),
            sum(1 for mp in mine if mp.status in (PassStatus.FAILED, PassStatus.INTERRUPTED)
                and mp.finished_at is not None and mp.finished_at >= since))

    async def clean_memories(self, now: datetime) -> MemoryCleanup:
        notes = self._store.memories
        loops = [k for k, m in notes.items() if m.category is MemoryCategory.LOOP
                 and self._store.reminders.get(k, ("",))[0] not in PENDING_REMINDERS  # V3-P87: kept till it is told
                 and ((m.due_instant is not None and m.due_instant < now - STALE_DUE)
                      or (m.due_instant is None and m.updated_at < now - STALE_UNDATED))]
        for key in loops:
            del notes[key]
        expired = [k for k, m in notes.items() if m.valid_instant is not None and m.valid_instant < now - EXPIRED_KEEP]
        for key in expired:
            del notes[key]
        retired = [k for k, (_, at, *_) in self._store.retired_memories.items() if at < now - RETIRED_KEEP]
        for key in retired:
            del self._store.retired_memories[key]
        proposals = 0
        for row in self._store.pass_items.values():
            created = row["created_at"]
            assert isinstance(created, datetime)
            if row["outcome"] in ("proposed", "sensitive_proposed") and created < now - PROPOSAL_KEEP:
                row.update(outcome="discarded", decided_at=now)
                proposals += 1
        return MemoryCleanup(len(loops), len(expired), len(retired), proposals)

    # --- ConversationSearch (V3-P72) --------------------------------------------------

    def _searchable(self, principal: PrincipalId, current: ConversationId, current_through: int | None,
                    only: ConversationId | None) -> Iterator[tuple[ConversationId, ConversationEvent, str]]:
        """(Conversation, Event, text) for each searchable Event (§3)."""
        for cid, conv in self._store._conversations.items():
            if conv.owner != principal or conv.trashed_at is not None or (only is not None and cid != only):
                continue
            for event in conv.events:
                text = rules.event_text(event.payload)
                if text is None or (cid == current and (current_through is None or event.sequence > current_through)):
                    continue
                yield cid, event, text

    async def search_events(self, principal: PrincipalId, words: Sequence[str], *, current: ConversationId,
                            current_through: int | None, conversation: ConversationId | None = None,
                            per_conversation: int, limit: int) -> EventSearch:
        folded = rules.search_words(words)
        if conversation is not None:
            self._store._authorized(TrustedScope(principal, conversation))
        found = []
        for cid, event, text in self._searchable(principal, current, current_through, conversation):
            count = rules.matched(rules.fold(text), folded)
            if count:
                found.append(EventText(cid, event.sequence, event.kind, event.created_at, text, count))
        hits, total = rules.pick_hits(found, per_conversation, limit)
        ids = dict.fromkeys(h.conversation_id for h in hits)
        return EventSearch(hits, total, tuple(self._summary(cid, self._store._conversations[cid]) for cid in ids))

    async def read_event_texts(self, principal: PrincipalId, conversation: ConversationId, *, from_sequence: int,
                               current: ConversationId, current_through: int | None, limit: int) -> EventPage:
        conv = self._store._authorized(TrustedScope(principal, conversation))
        last = rules.check_read(conversation, from_sequence, current, current_through)
        items = [EventText(conversation, e.sequence, e.kind, e.created_at, text)
                 for cid, e, text in self._searchable(principal, current, current_through, conversation)
                 if e.sequence >= from_sequence and (last < 0 or e.sequence <= last)]
        return EventPage(self._summary(conversation, conv), tuple(items[:limit]), len(items) > limit)

    # --- StateQuery -------------------------------------------------------------

    async def read_current_state_basis(self, scope: TrustedScope) -> PersistedStateBasis:
        conv = self._conv(scope)
        return PersistedStateBasis(
            conversation_sequence=len(conv.events),
            state_revision=conv.state_revision,
            environments=tuple(sorted(conv.environments)),
        )
