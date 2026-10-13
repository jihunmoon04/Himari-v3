"""SQLite Data implementation of all P0 sub-ports (Q-41).

Durability: one database file in WAL mode with ``synchronous=FULL``, so a
committed transaction survives a process crash (and, given an honest disk,
power loss). Every port call is exactly one ``BEGIN IMMEDIATE … COMMIT``
transaction that validates first and writes last; any error — including
a crash between statements — leaves no partial T1–T9/T7a/T-R effect.

Calls run synchronously on the caller's thread, like ``InMemoryData``: a
port call never yields to the event loop, so its result is observed by the
caller iff it committed. Transactions are short (no network or process I/O
inside) at the cost of blocking the loop for one commit's fsync.

Single writer (P0, V3-045): ``SqliteStore`` takes an exclusive ``flock`` on
``<path>.lock`` for its whole lifetime, so a second process (or a second
store in the same process) cannot open the same database; the kernel
releases it when the process dies. The database and lock files are created
``0600`` — they hold raw Provider requests and responses.
"""

from __future__ import annotations

import dataclasses
import fcntl
import os
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime

from himari.common import (
    ASK_USER_TOOL_ID,
    ArtifactId,
    BindingId,
    CallId,
    CallRef,
    ConversationId,
    EnvironmentInstanceId,
    EnvironmentKind,
    EventId,
    JsonValue,
    ModelToolResult,
    ProviderContinuation,
    RawRepresentation,
    RunId,
    ToolId,
    TrustedScope,
    UnknownOutcomePolicy,
    WorkspaceId,
)
from himari.data import codec
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

SCHEMA_VERSION = 1

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

# ``seq`` columns are rowid aliases: creation order, as dict insertion order
# is in the in-memory store. Rows are never re-inserted on update.
_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL) STRICT;
INSERT INTO meta VALUES ('next_id', 1);
CREATE TABLE conversations (
    conversation_id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    state_revision INTEGER NOT NULL DEFAULT 0,
    workspace_id TEXT,
    workspace_cwd TEXT,
    snapshot_revision INTEGER,
    snapshot_schema_version INTEGER,
    snapshot_body TEXT,
    snapshot_covers_through INTEGER,
    snapshot_basis TEXT
) STRICT;
CREATE TABLE events (
    conversation_id TEXT NOT NULL REFERENCES conversations,
    sequence INTEGER NOT NULL,
    event_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    payload_schema_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    origin_run_id TEXT,
    PRIMARY KEY (conversation_id, sequence)
) STRICT;
CREATE TABLE idempotency (
    conversation_id TEXT NOT NULL REFERENCES conversations,
    key TEXT NOT NULL,
    event_id TEXT NOT NULL,
    PRIMARY KEY (conversation_id, key)
) STRICT;
CREATE TABLE runs (
    seq INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE,
    conversation_id TEXT NOT NULL REFERENCES conversations,
    status TEXT NOT NULL,
    trigger_refs TEXT NOT NULL,
    prompt_basis TEXT NOT NULL,
    provider_key TEXT NOT NULL,
    model_id TEXT NOT NULL,
    raw_representation TEXT NOT NULL,
    usage TEXT,
    error_code TEXT,
    continuation TEXT,
    raw_request_body BLOB NOT NULL,
    raw_request_content_type TEXT NOT NULL,
    raw_response_body BLOB,
    raw_response_content_type TEXT NOT NULL DEFAULT '',
    projected_event_id TEXT
) STRICT;
CREATE INDEX runs_by_conversation ON runs (conversation_id, seq);
CREATE TABLE tool_executions (
    seq INTEGER PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations,
    run_id TEXT NOT NULL REFERENCES runs (run_id),
    call_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    tool_id TEXT NOT NULL,
    arguments TEXT NOT NULL,
    status TEXT NOT NULL,
    model_result TEXT,
    raw_result TEXT NOT NULL,
    workspace_binding_id TEXT,
    unknown_outcome TEXT,
    record_event INTEGER,
    UNIQUE (run_id, call_id)
) STRICT;
CREATE INDEX executions_by_conversation ON tool_executions (conversation_id, seq);
CREATE TABLE artifacts (
    seq INTEGER PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations,
    artifact_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    revision INTEGER NOT NULL,
    content TEXT NOT NULL
) STRICT;
CREATE TABLE activations (
    seq INTEGER PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations,
    environment_instance_id TEXT NOT NULL UNIQUE,
    environment_kind TEXT NOT NULL,
    default_binding_id TEXT
) STRICT;
CREATE TABLE bindings (
    seq INTEGER PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations,
    binding_id TEXT NOT NULL UNIQUE,
    environment_instance_id TEXT NOT NULL,
    environment_kind TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    cwd_relative TEXT
) STRICT;
"""


class StoreLockedError(RuntimeError):
    """Another live store (this or another process) has the database open."""


def _create_private(path: str) -> None:
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    os.close(fd)


class SqliteStore:
    """Durable storage plus admin helpers (not part of any port)."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = os.fspath(path)
        lock_fd = os.open(f"{self.path}.lock", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(lock_fd)
            raise StoreLockedError(f"{self.path} is in use by another store") from None
        self._lock_fd: int | None = lock_fd
        try:
            _create_private(self.path)
            self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=True)
            self.connection.row_factory = sqlite3.Row
            self._configure()
        except BaseException:
            self._release_lock()
            raise

    def _configure(self) -> None:
        conn = self.connection
        if conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
            raise RuntimeError("SQLite WAL mode is unavailable")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            if conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]:
                raise RuntimeError(f"{self.path} is not a HIMARI database")
            with self.transaction() as tx:
                tx.executescript_parts(_SCHEMA)
                tx.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        elif version != SCHEMA_VERSION:
            raise RuntimeError(f"unsupported schema version {version} (expected {SCHEMA_VERSION})")

    def close(self) -> None:
        try:
            self.connection.close()
        finally:
            self._release_lock()

    def _release_lock(self) -> None:
        if self._lock_fd is not None:
            os.close(self._lock_fd)  # also drops the flock
            self._lock_fd = None

    @contextmanager
    def transaction(self) -> Iterator[_Tx]:
        """One atomic unit: everything inside commits together or not at all."""
        conn = self.connection
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield _Tx(conn)
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    # --- admin helpers (trusted setup paths, not model-reachable) --------

    def create_conversation(self, scope: TrustedScope) -> None:
        with self.transaction() as tx:
            if tx.one("SELECT 1 FROM conversations WHERE conversation_id=?", scope.conversation_id):
                raise DataError(DataErrorCode.DUPLICATE_IDENTITY, scope.conversation_id)
            tx.execute(
                "INSERT INTO conversations (conversation_id, owner) VALUES (?, ?)",
                scope.conversation_id, scope.principal_id,
            )

    def activate_environment(
        self,
        scope: TrustedScope,
        kind: EnvironmentKind,
        workspace_id: WorkspaceId,
        *,
        cwd_relative: str | None = None,
    ) -> BindingAuthority:
        with self.transaction() as tx:
            conv = tx.authorized(scope)
            if tx.one("SELECT 1 FROM activations WHERE conversation_id=? AND environment_kind=?", scope.conversation_id, kind):
                raise DataError(DataErrorCode.DUPLICATE_IDENTITY, f"active {kind} exists (P0: one per kind)")
            instance_id = EnvironmentInstanceId(tx.new_id("env"))
            relative = cwd_relative if cwd_relative is not None else f"conversations/{scope.conversation_id}"
            binding = BindingAuthority(
                binding_id=BindingId(tx.new_id("bind")),
                environment_instance_id=instance_id,
                environment_kind=kind,
                workspace_id=workspace_id,
                revision=1,
                cwd_relative=relative,
            )
            tx.execute(
                "INSERT INTO activations (conversation_id, environment_instance_id, environment_kind, default_binding_id)"
                " VALUES (?, ?, ?, ?)",
                scope.conversation_id, instance_id, kind, binding.binding_id,
            )
            tx.execute(
                "INSERT INTO bindings (conversation_id, binding_id, environment_instance_id, environment_kind,"
                " workspace_id, revision, cwd_relative) VALUES (?, ?, ?, ?, ?, ?, ?)",
                scope.conversation_id, binding.binding_id, instance_id, kind, workspace_id, 1, relative,
            )
            if conv["workspace_id"] is None:  # the Conversation's first (default) working directory
                tx.execute(
                    "UPDATE conversations SET workspace_id=?, workspace_cwd=? WHERE conversation_id=?",
                    workspace_id, relative, scope.conversation_id,
                )
            tx.bump_state(scope)
            return binding

    def revoke_binding(self, scope: TrustedScope, binding_id: BindingId) -> None:
        with self.transaction() as tx:
            tx.authorized(scope)
            tx.execute("DELETE FROM bindings WHERE conversation_id=? AND binding_id=?", scope.conversation_id, binding_id)
            tx.execute(
                "UPDATE activations SET default_binding_id=NULL WHERE conversation_id=? AND default_binding_id=?",
                scope.conversation_id, binding_id,
            )
            tx.bump_state(scope)

    def deactivate_environment(self, scope: TrustedScope, kind: EnvironmentKind) -> None:
        """Ends the activation; Bindings and the Workspace itself remain (V3-009)."""
        with self.transaction() as tx:
            tx.authorized(scope)
            tx.execute("DELETE FROM activations WHERE conversation_id=? AND environment_kind=?", scope.conversation_id, kind)
            tx.bump_state(scope)

    def raw_request_bodies(self, scope: TrustedScope) -> list[bytes]:
        """Test/audit helper: every persisted raw request, in Run order."""
        with self.transaction() as tx:
            tx.authorized(scope)
            rows = tx.all("SELECT raw_request_body FROM runs WHERE conversation_id=? ORDER BY seq", scope.conversation_id)
            return [bytes(r["raw_request_body"]) for r in rows]


class _Tx:
    """Statement helpers bound to the open transaction."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, sql: str, *params: object) -> sqlite3.Cursor:
        return self._conn.execute(sql, params)

    def executescript_parts(self, script: str) -> None:
        # executescript() would COMMIT the open transaction; run each statement instead.
        for statement in script.split(";"):
            if statement.strip():
                self._conn.execute(statement)

    def one(self, sql: str, *params: object) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._conn.execute(sql, params).fetchone()
        return row

    def all(self, sql: str, *params: object) -> list[sqlite3.Row]:
        return list(self._conn.execute(sql, params).fetchall())

    def new_id(self, prefix: str) -> str:
        row = self.one("SELECT value FROM meta WHERE key='next_id'")
        assert row is not None
        self.execute("UPDATE meta SET value=value+1 WHERE key='next_id'")
        return f"{prefix}_{row['value']}"

    def authorized(self, scope: TrustedScope) -> sqlite3.Row:
        conv = self.one("SELECT * FROM conversations WHERE conversation_id=?", scope.conversation_id)
        if conv is None or conv["owner"] != scope.principal_id:
            # Same error for foreign and missing (indistinguishability).
            raise DataError(DataErrorCode.NOT_AUTHORIZED, scope.conversation_id)
        return conv

    def bump_state(self, scope: TrustedScope) -> None:
        self.execute("UPDATE conversations SET state_revision=state_revision+1 WHERE conversation_id=?", scope.conversation_id)


# --- row decoding ---------------------------------------------------------------------


def _event(row: sqlite3.Row) -> ConversationEvent:
    kind = EventKind(row["kind"])
    return ConversationEvent(
        event_id=EventId(row["event_id"]),
        conversation_id=ConversationId(row["conversation_id"]),
        sequence=row["sequence"],
        kind=kind,
        payload=codec.payload_from(kind, row["payload"]),
        payload_schema_version=row["payload_schema_version"],
        created_at=datetime.fromisoformat(row["created_at"]),
        origin_run_id=None if row["origin_run_id"] is None else RunId(row["origin_run_id"]),
    )


def _run_record(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        run_id=RunId(row["run_id"]),
        conversation_id=ConversationId(row["conversation_id"]),
        status=RunStatus(row["status"]),
        trigger_refs=codec.trigger_refs_from(row["trigger_refs"]),
        prompt_basis=codec.prompt_basis_from(row["prompt_basis"]),
        provider_key=row["provider_key"],
        model_id=row["model_id"],
        raw_representation=RawRepresentation(row["raw_representation"]),
        usage=codec.usage_from(row["usage"]),
        error_code=row["error_code"],
        continuation=codec.continuation_from(row["continuation"]),
    )


def _execution(row: sqlite3.Row) -> ToolExecutionRecord:
    record_event = row["record_event"]
    return ToolExecutionRecord(
        call=CallRef(RunId(row["run_id"]), CallId(row["call_id"])),
        ordinal=row["ordinal"],
        tool_id=ToolId(row["tool_id"]),
        arguments=codec.load_json(row["arguments"]),  # type: ignore[arg-type]
        status=ToolExecutionStatus(row["status"]),
        model_result=None if row["model_result"] is None else codec.model_result_from(row["model_result"]),
        raw_result=codec.load_json(row["raw_result"]),
        workspace_binding_id=None if row["workspace_binding_id"] is None else BindingId(row["workspace_binding_id"]),
        unknown_outcome=None if row["unknown_outcome"] is None else UnknownOutcomePolicy(row["unknown_outcome"]),
        record_event=None if record_event is None else bool(record_event),
    )


def _artifact(row: sqlite3.Row) -> Artifact:
    return Artifact(ArtifactId(row["artifact_id"]), row["kind"], row["revision"], codec.load_json(row["content"]))


def _binding(row: sqlite3.Row) -> BindingAuthority:
    return BindingAuthority(
        binding_id=BindingId(row["binding_id"]),
        environment_instance_id=EnvironmentInstanceId(row["environment_instance_id"]),
        environment_kind=EnvironmentKind(row["environment_kind"]),
        workspace_id=WorkspaceId(row["workspace_id"]),
        revision=row["revision"],
        cwd_relative=row["cwd_relative"],
    )


def _activation(row: sqlite3.Row) -> EnvironmentActivation:
    default = row["default_binding_id"]
    return EnvironmentActivation(
        EnvironmentInstanceId(row["environment_instance_id"]),
        EnvironmentKind(row["environment_kind"]),
        None if default is None else BindingId(default),
    )


def _workspace(conv: sqlite3.Row) -> ConversationWorkspace | None:
    if conv["workspace_id"] is None:
        return None
    return ConversationWorkspace(WorkspaceId(conv["workspace_id"]), conv["workspace_cwd"])


class SqliteData:
    """Implements ConversationLog, ArtifactStore, WorkspaceState, StateQuery."""

    def __init__(self, store: SqliteStore) -> None:
        self._store = store

    # --- helpers -------------------------------------------------------------

    def _tx(self) -> AbstractContextManager[_Tx]:
        return self._store.transaction()

    @staticmethod
    def _new_event(tx: _Tx, scope: TrustedScope, payload: EventPayload, origin: RunId | None) -> ConversationEvent:
        row = tx.one("SELECT COALESCE(MAX(sequence), 0) AS last FROM events WHERE conversation_id=?", scope.conversation_id)
        assert row is not None
        event = ConversationEvent(
            event_id=EventId(tx.new_id("evt")),
            conversation_id=scope.conversation_id,
            sequence=row["last"] + 1,
            kind=_KIND_OF[type(payload)],
            payload=payload,
            payload_schema_version=1,
            created_at=datetime.now(UTC),
            origin_run_id=origin,
        )
        tx.execute(
            "INSERT INTO events (conversation_id, sequence, event_id, kind, payload, payload_schema_version,"
            " created_at, origin_run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            scope.conversation_id, event.sequence, event.event_id, event.kind.value, codec.payload(payload),
            event.payload_schema_version, event.created_at.isoformat(), origin,
        )
        return event

    @staticmethod
    def _run_row(tx: _Tx, scope: TrustedScope, run_id: RunId) -> sqlite3.Row:
        row = tx.one("SELECT * FROM runs WHERE run_id=? AND conversation_id=?", run_id, scope.conversation_id)
        if row is None:
            raise DataError(DataErrorCode.NOT_FOUND, run_id)
        return row

    @staticmethod
    def _execution_row(tx: _Tx, scope: TrustedScope, call: CallRef) -> sqlite3.Row:
        row = tx.one(
            "SELECT * FROM tool_executions WHERE run_id=? AND call_id=? AND conversation_id=?",
            call.run_id, call.call_id, scope.conversation_id,
        )
        if row is None:
            raise DataError(DataErrorCode.NOT_FOUND, f"{call.run_id}/{call.call_id}")
        return row

    @staticmethod
    def _waiting(tx: _Tx, scope: TrustedScope) -> CallRef | None:
        latest = tx.one(
            "SELECT run_id FROM runs WHERE conversation_id=? AND status=? ORDER BY seq DESC LIMIT 1",
            scope.conversation_id, RunStatus.PROJECTED.value,
        )
        if latest is None:
            return None
        group = tx.all("SELECT * FROM tool_executions WHERE run_id=? AND conversation_id=?", latest["run_id"], scope.conversation_id)
        if (
            len(group) == 1
            and group[0]["tool_id"] == ASK_USER_TOOL_ID
            and group[0]["status"] == ToolExecutionStatus.PENDING.value
        ):
            return CallRef(RunId(group[0]["run_id"]), CallId(group[0]["call_id"]))
        return None

    @staticmethod
    def _set_run(tx: _Tx, run_id: str, status: RunStatus, **columns: object) -> None:
        assignments = ", ".join(["status=?", *(f"{name}=?" for name in columns)])
        tx.execute(f"UPDATE runs SET {assignments} WHERE run_id=?", status.value, *columns.values(), run_id)

    # --- ConversationLog --------------------------------------------------

    async def append_event(
        self, scope: TrustedScope, message: UserMessage, *, idempotency_key: str | None = None
    ) -> ConversationEvent:
        with self._tx() as tx:
            tx.authorized(scope)
            if idempotency_key is not None:
                known = tx.one("SELECT event_id FROM idempotency WHERE conversation_id=? AND key=?", scope.conversation_id, idempotency_key)
                if known is not None:
                    row = tx.one("SELECT * FROM events WHERE event_id=?", known["event_id"])
                    assert row is not None
                    existing = _event(row)
                    if existing.payload != message:
                        raise DataError(DataErrorCode.DUPLICATE_IDENTITY, f"idempotency key {idempotency_key!r} reused")
                    return existing
            if message.answers_call is not None:
                raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, "answers go through T7a")
            if self._waiting(tx, scope) is not None:
                raise DataError(
                    DataErrorCode.INVALID_STATE_TRANSITION, "an ask_user call is waiting; use answer_pending_call"
                )
            event = self._new_event(tx, scope, message, None)
            if idempotency_key is not None:
                tx.execute(
                    "INSERT INTO idempotency (conversation_id, key, event_id) VALUES (?, ?, ?)",
                    scope.conversation_id, idempotency_key, event.event_id,
                )
            return event

    async def read_events(
        self, scope: TrustedScope, *, after_sequence: int = 0, limit: int | None = None
    ) -> Sequence[ConversationEvent]:
        with self._tx() as tx:
            tx.authorized(scope)
            rows = tx.all(
                "SELECT * FROM events WHERE conversation_id=? AND sequence>? ORDER BY sequence LIMIT ?",
                scope.conversation_id, after_sequence, -1 if limit is None else limit,
            )
            return tuple(_event(r) for r in rows)

    async def prepare_run(self, scope: TrustedScope, preparation: RunPreparation) -> RunId:
        try:
            with self._tx() as tx:
                tx.authorized(scope)
                run_id = RunId(tx.new_id("run"))
                tx.execute(
                    "INSERT INTO runs (run_id, conversation_id, status, trigger_refs, prompt_basis, provider_key, model_id,"
                    " raw_representation, raw_request_body, raw_request_content_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    run_id, scope.conversation_id, RunStatus.PREPARED.value,
                    codec.trigger_refs(preparation.trigger_refs), codec.prompt_basis(preparation.prompt_basis),
                    preparation.provider_key, preparation.model_id, preparation.raw_representation.value,
                    preparation.raw_request_body, preparation.raw_content_type,
                )
                return run_id
        except sqlite3.Error as error:
            # Anything that keeps the request from being stored (read-only or
            # full disk, I/O error at BEGIN or COMMIT): the caller must not send.
            raise DataError(DataErrorCode.RAW_PERSISTENCE_UNAVAILABLE, type(error).__name__) from error

    async def mark_dispatch(self, scope: TrustedScope, run_id: RunId) -> None:
        with self._tx() as tx:
            tx.authorized(scope)
            run = self._run_row(tx, scope, run_id)
            if run["status"] != RunStatus.PREPARED.value:
                raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"{run_id} is {run['status']}")
            self._set_run(tx, run_id, RunStatus.DISPATCH_CLAIMED)

    async def record_raw_outcome(
        self, scope: TrustedScope, run_id: RunId, outcome: RawRunOutcome
    ) -> None:
        with self._tx() as tx:
            tx.authorized(scope)
            run = self._run_row(tx, scope, run_id)
            if run["status"] != RunStatus.DISPATCH_CLAIMED.value:
                raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"{run_id} is {run['status']}")
            match outcome:
                case RawReceived(body=body, content_type=content_type, usage=usage):
                    self._set_run(tx, run_id, RunStatus.RECEIVED, raw_response_body=body,
                                  raw_response_content_type=content_type, usage=codec.usage(usage))
                case RawNoResponse(error_code=code, body=body, content_type=content_type):
                    self._set_run(tx, run_id, RunStatus.FAILED_NO_RESPONSE, raw_response_body=body,
                                  raw_response_content_type=content_type, error_code=code)
                case RawUncertain(error_code=code):
                    self._set_run(tx, run_id, RunStatus.OUTCOME_UNKNOWN, error_code=code)

    async def project_model_output(
        self,
        scope: TrustedScope,
        run_id: RunId,
        output: AssistantOutput,
        *,
        continuation: JsonValue = None,
    ) -> ProjectionResult:
        with self._tx() as tx:
            tx.authorized(scope)
            run = self._run_row(tx, scope, run_id)
            if run["status"] != RunStatus.RECEIVED.value:
                raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"{run_id} is {run['status']}")
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
            event = self._new_event(tx, scope, output, run_id)
            for record in pending:
                tx.execute(
                    "INSERT INTO tool_executions (conversation_id, run_id, call_id, ordinal, tool_id, arguments, status,"
                    " raw_result, workspace_binding_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    scope.conversation_id, run_id, record.call.call_id, record.ordinal, record.tool_id,
                    codec.dump_json(record.arguments), record.status.value, codec.dump_json(None),
                    record.workspace_binding_id,
                )
            stamped = (
                None
                if continuation is None
                else ProviderContinuation(run["provider_key"], run["model_id"], continuation)
            )
            self._set_run(tx, run_id, RunStatus.PROJECTED, projected_event_id=event.event_id,
                          continuation=codec.continuation(stamped))
            return ProjectionResult(event=event, pending=tuple(pending))

    async def record_projection_failure(
        self, scope: TrustedScope, run_id: RunId, error_code: str
    ) -> None:
        with self._tx() as tx:
            tx.authorized(scope)
            run = self._run_row(tx, scope, run_id)
            if run["status"] != RunStatus.RECEIVED.value:
                raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"{run_id} is {run['status']}")
            self._set_run(tx, run_id, RunStatus.PROJECTION_FAILED, error_code=error_code)

    async def read_run(self, scope: TrustedScope, run_id: RunId) -> RunRecord:
        with self._tx() as tx:
            tx.authorized(scope)
            return _run_record(self._run_row(tx, scope, run_id))

    async def read_raw_response(self, scope: TrustedScope, run_id: RunId) -> RawReceived:
        with self._tx() as tx:
            tx.authorized(scope)
            run = self._run_row(tx, scope, run_id)
            if run["raw_response_body"] is None:
                raise DataError(DataErrorCode.NOT_FOUND, f"{run_id} has no stored response")
            return RawReceived(bytes(run["raw_response_body"]), run["raw_response_content_type"],
                               codec.usage_from(run["usage"]))

    async def read_tool_executions(
        self, scope: TrustedScope, run_id: RunId
    ) -> Sequence[ToolExecutionRecord]:
        with self._tx() as tx:
            tx.authorized(scope)
            rows = tx.all(
                "SELECT * FROM tool_executions WHERE run_id=? AND conversation_id=? ORDER BY ordinal",
                run_id, scope.conversation_id,
            )
            return tuple(_execution(r) for r in rows)

    async def claim_tool_execution(
        self,
        scope: TrustedScope,
        call: CallRef,
        *,
        unknown_outcome: UnknownOutcomePolicy,
        record_event: bool,
    ) -> None:
        with self._tx() as tx:
            tx.authorized(scope)
            record = self._execution_row(tx, scope, call)
            if record["status"] != ToolExecutionStatus.PENDING.value:
                raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"{call.call_id} is {record['status']}")
            tx.execute(
                "UPDATE tool_executions SET status=?, unknown_outcome=?, record_event=? WHERE seq=?",
                ToolExecutionStatus.EXECUTION_CLAIMED.value, unknown_outcome.value, int(record_event), record["seq"],
            )

    async def commit_tool_outcome(
        self, scope: TrustedScope, call: CallRef, completion: ToolCompletion
    ) -> ConversationEvent | None:
        with self._tx() as tx:
            tx.authorized(scope)
            record = self._execution_row(tx, scope, call)
            status = ToolExecutionStatus(record["status"])
            if status in _TERMINAL_COMPLETIONS or status is ToolExecutionStatus.UNKNOWN:
                raise DataError(DataErrorCode.ALREADY_COMMITTED, f"{call.call_id} is {status.value}")
            if completion.status not in _TERMINAL_COMPLETIONS:
                raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"not a definite outcome: {completion.status}")
            self._apply_artifacts(tx, scope, completion.artifact_mutations)  # CAS errors roll everything back
            tx.execute(
                "UPDATE tool_executions SET status=?, model_result=?, raw_result=? WHERE seq=?",
                completion.status.value, codec.model_result(completion.model_result),
                codec.dump_json(completion.raw_result), record["seq"],
            )
            if not completion.record_event:
                return None
            return self._new_event(tx, scope, ToolResultRecord(call, completion.model_result), call.run_id)

    async def commit_unknown_outcome(
        self, scope: TrustedScope, call: CallRef, model_result: ModelToolResult
    ) -> ConversationEvent | None:
        with self._tx() as tx:
            tx.authorized(scope)
            row = self._execution_row(tx, scope, call)
            record = _execution(row)
            if record.model_result is not None:
                raise DataError(DataErrorCode.ALREADY_COMMITTED, f"{call.call_id} already has a result")
            if record.status not in (ToolExecutionStatus.EXECUTION_CLAIMED, ToolExecutionStatus.UNKNOWN):
                raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"{call.call_id} is {record.status.value}")
            if record.unknown_outcome is not UnknownOutcomePolicy.REPORT_TO_MODEL:
                raise DataError(
                    DataErrorCode.INVALID_STATE_TRANSITION,
                    f"{call.call_id}: policy {record.unknown_outcome} requires a user",
                )
            tx.execute(
                "UPDATE tool_executions SET status=?, model_result=? WHERE seq=?",
                ToolExecutionStatus.UNKNOWN.value, codec.model_result(model_result), row["seq"],
            )
            if record.record_event is False:  # claim-time policy; None (legacy) records the Event
                return None
            return self._new_event(tx, scope, ToolResultRecord(call, model_result), call.run_id)

    def _apply_artifacts(self, tx: _Tx, scope: TrustedScope, mutations: Sequence[ArtifactMutation]) -> None:
        """Applies in order inside the transaction; a later CAS failure rolls
        back the earlier mutations with everything else."""
        for m in mutations:
            if m.artifact_id is None:
                tx.execute(
                    "INSERT INTO artifacts (conversation_id, artifact_id, kind, revision, content) VALUES (?, ?, ?, 1, ?)",
                    scope.conversation_id, tx.new_id("art"), m.kind, codec.dump_json(m.content),
                )
                continue
            current = tx.one(
                "SELECT revision FROM artifacts WHERE artifact_id=? AND conversation_id=?", m.artifact_id, scope.conversation_id
            )
            if current is None:
                raise DataError(DataErrorCode.NOT_FOUND, m.artifact_id)
            if m.expected_revision is not None and m.expected_revision != current["revision"]:
                raise DataError(DataErrorCode.VERSION_CONFLICT, m.artifact_id)
            tx.execute(
                "UPDATE artifacts SET content=?, revision=revision+1 WHERE artifact_id=?",
                codec.dump_json(m.content), m.artifact_id,
            )

    async def find_waiting_call(self, scope: TrustedScope) -> CallRef | None:
        with self._tx() as tx:
            tx.authorized(scope)
            return self._waiting(tx, scope)

    async def answer_pending_call(
        self, scope: TrustedScope, call: CallRef, answer: UserMessage
    ) -> AnswerCommit:
        with self._tx() as tx:
            tx.authorized(scope)
            if self._waiting(tx, scope) != call:
                raise DataError(DataErrorCode.ALREADY_COMMITTED, f"{call.call_id} is not waiting")
            record = self._execution_row(tx, scope, call)
            result = ModelToolResult(blocks=answer.blocks)
            user_event = self._new_event(tx, scope, dataclasses.replace(answer, answers_call=call), None)
            tx.execute(
                "UPDATE tool_executions SET status=?, model_result=? WHERE seq=?",
                ToolExecutionStatus.SUCCEEDED.value, codec.model_result(result), record["seq"],
            )
            tool_event = self._new_event(tx, scope, ToolResultRecord(call, result), call.run_id)
            return AnswerCommit(user_event=user_event, tool_result_event=tool_event)

    async def recover_interrupted(self, scope: TrustedScope) -> RecoveryReport:
        with self._tx() as tx:
            tx.authorized(scope)
            unknown_runs: list[RunId] = []
            abandoned: list[RunId] = []
            unknown_tools: list[CallRef] = []
            received: list[RunId] = []
            for run in tx.all("SELECT run_id, status FROM runs WHERE conversation_id=? ORDER BY seq", scope.conversation_id):
                run_id = RunId(run["run_id"])
                if run["status"] == RunStatus.DISPATCH_CLAIMED.value:
                    self._set_run(tx, run_id, RunStatus.OUTCOME_UNKNOWN, error_code="interrupted")
                    unknown_runs.append(run_id)
                elif run["status"] == RunStatus.PREPARED.value:
                    self._set_run(tx, run_id, RunStatus.ABANDONED_NOT_SENT)
                    abandoned.append(run_id)
                elif run["status"] == RunStatus.RECEIVED.value:
                    received.append(run_id)
            claimed = tx.all(
                "SELECT seq, run_id, call_id FROM tool_executions WHERE conversation_id=? AND status=? ORDER BY seq",
                scope.conversation_id, ToolExecutionStatus.EXECUTION_CLAIMED.value,
            )
            for row in claimed:
                tx.execute("UPDATE tool_executions SET status=? WHERE seq=?", ToolExecutionStatus.UNKNOWN.value, row["seq"])
                unknown_tools.append(CallRef(RunId(row["run_id"]), CallId(row["call_id"])))
            return RecoveryReport(tuple(unknown_runs), tuple(abandoned), tuple(unknown_tools), tuple(received))

    async def get_latest_snapshot(self, scope: TrustedScope) -> PromptSnapshot | None:
        with self._tx() as tx:
            conv = tx.authorized(scope)
            if conv["snapshot_revision"] is None:
                return None
            return PromptSnapshot(
                conv["snapshot_revision"],
                conv["snapshot_schema_version"],
                codec.load_json(conv["snapshot_body"]),
                conv["snapshot_covers_through"],
                codec.prompt_basis_from(conv["snapshot_basis"]),
            )

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
        with self._tx() as tx:
            conv = tx.authorized(scope)
            current = conv["snapshot_revision"]
            if current != expected_revision:
                raise DataError(DataErrorCode.VERSION_CONFLICT, "snapshot")
            revision = int(current or 0) + 1
            tx.execute(
                "UPDATE conversations SET snapshot_revision=?, snapshot_schema_version=?, snapshot_body=?,"
                " snapshot_covers_through=?, snapshot_basis=? WHERE conversation_id=?",
                revision, schema_version, codec.dump_json(body), covers_through_sequence,
                codec.prompt_basis(basis), scope.conversation_id,
            )
            return revision

    # --- ArtifactStore ---------------------------------------------------------

    async def read_artifact(self, scope: TrustedScope, artifact_id: ArtifactId) -> Artifact:
        with self._tx() as tx:
            tx.authorized(scope)
            row = tx.one("SELECT * FROM artifacts WHERE artifact_id=? AND conversation_id=?", artifact_id, scope.conversation_id)
            if row is None:
                raise DataError(DataErrorCode.NOT_FOUND, artifact_id)
            return _artifact(row)

    async def list_artifacts(
        self, scope: TrustedScope, *, kind: str | None = None
    ) -> Sequence[Artifact]:
        with self._tx() as tx:
            tx.authorized(scope)
            rows = tx.all("SELECT * FROM artifacts WHERE conversation_id=? ORDER BY seq", scope.conversation_id)
            return tuple(_artifact(r) for r in rows if kind is None or r["kind"] == kind)

    # --- WorkspaceState --------------------------------------------------------

    async def read_binding_authority(
        self, scope: TrustedScope, binding_id: BindingId
    ) -> BindingAuthority:
        with self._tx() as tx:
            tx.authorized(scope)
            row = tx.one("SELECT * FROM bindings WHERE binding_id=? AND conversation_id=?", binding_id, scope.conversation_id)
            if row is None:
                raise DataError(DataErrorCode.NOT_FOUND, binding_id)
            return _binding(row)

    async def resolve_default_binding(
        self, scope: TrustedScope, environment_kind: EnvironmentKind
    ) -> BindingAuthority | None:
        with self._tx() as tx:
            tx.authorized(scope)
            for act in tx.all("SELECT * FROM activations WHERE conversation_id=? ORDER BY seq", scope.conversation_id):
                if act["environment_kind"] == environment_kind and act["default_binding_id"] is not None:
                    row = tx.one(
                        "SELECT * FROM bindings WHERE binding_id=? AND conversation_id=?",
                        act["default_binding_id"], scope.conversation_id,
                    )
                    return None if row is None else _binding(row)
            return None

    async def read_activation(
        self, scope: TrustedScope, environment_kind: EnvironmentKind
    ) -> EnvironmentActivation | None:
        with self._tx() as tx:
            tx.authorized(scope)
            row = tx.one(
                "SELECT * FROM activations WHERE conversation_id=? AND environment_kind=? ORDER BY seq LIMIT 1",
                scope.conversation_id, environment_kind,
            )
            return None if row is None else _activation(row)

    async def resolve_conversation_workspace(self, scope: TrustedScope) -> ConversationWorkspace:
        with self._tx() as tx:
            workspace = _workspace(tx.authorized(scope))
            if workspace is None:
                raise DataError(DataErrorCode.NOT_FOUND, "conversation workspace")
            return workspace

    # --- StateQuery -------------------------------------------------------------

    async def read_current_state_basis(self, scope: TrustedScope) -> PersistedStateBasis:
        with self._tx() as tx:  # one transaction: a coherent read
            conv = tx.authorized(scope)
            last = tx.one("SELECT COALESCE(MAX(sequence), 0) AS last FROM events WHERE conversation_id=?", scope.conversation_id)
            assert last is not None
            cid = scope.conversation_id
            return PersistedStateBasis(
                conversation_sequence=last["last"],
                state_revision=conv["state_revision"],
                environments=tuple(_activation(r) for r in tx.all("SELECT * FROM activations WHERE conversation_id=? ORDER BY seq", cid)),
                bindings=tuple(_binding(r) for r in tx.all("SELECT * FROM bindings WHERE conversation_id=? ORDER BY seq", cid)),
                artifacts=tuple(_artifact(r) for r in tx.all("SELECT * FROM artifacts WHERE conversation_id=? ORDER BY seq", cid)),
                conversation_workspace=_workspace(conv),
            )
