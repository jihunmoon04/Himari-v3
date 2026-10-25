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
releases it when the process dies.

Private files: the database holds raw Provider requests and responses. Its
directory must belong to this user and not be writable by others; the
database, lock, WAL, SHM and journal files must be regular files of this
user with mode 600 and a single name. New files are created 0600 without
following symlinks; anything else is refused, never silently re-permissioned.

Identity: a database is accepted only with HIMARI's ``application_id``, the
supported ``user_version`` and exactly the expected tables and indexes.
An older version (v2 and later) is upgraded only when it matches that
version's schema exactly, in one transaction; any other version or shape is
refused, never repaired.

Backup before upgrade (V3-P65 §8.1): the upgrade runs only after a copy of
the older database is written as ``<name>.v<old>-<UTC time>.bak`` (private,
like the database), checked, and made durable — the file and its directory
entry. Any failure refuses the open and leaves the database as it was.

Deleted content (V3-P65): ``secure_delete`` overwrites freed pages, so a
purged Conversation does not linger in the database file.

Files (V3-P66): the bytes of uploaded and presented files live in the file
store next to the database (``<dir>/files``, ``data.blobs``); the ``blobs``
and ``files`` rows here say what they are and who refers to them.
"""

from __future__ import annotations

import dataclasses
import errno
import fcntl
import logging
import os
import sqlite3
import stat
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime, timedelta

from himari.common import (
    ASK_USER_TOOL_ID,
    ArtifactId,
    AttachmentRef,
    BindingId,
    CallId,
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
    RawRepresentation,
    RunId,
    ToolId,
    TrustedScope,
    UnknownOutcomePolicy,
    WorkspaceId,
)
from himari.data import codec, rules
from himari.data.blobs import BlobRow, DirectoryBlobFiles, FileStore
from himari.data.api import (
    SEARCH_KINDS,
    EventPage,
    EventSearch,
    EventText,
    MAX_ATTACHMENTS,
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
    AttachedWorkspace,
    BindingAuthority,
    ConversationEvent,
    ConversationSummary,
    ConversationWorkspace,
    ConversationWorkspaces,
    DataError,
    DataErrorCode,
    EnvironmentActivation,
    EventKind,
    NO_IMAGE,
    FileOrigin,
    FileRecord,
    ImageSetup,
    ImageStatus,
    ImageView,
    Instructions,
    InstructionScope,
    MAX_ATTACHED_WORKSPACES,
    MAX_MEMORIES,
    MAX_WORKSPACES,
    Memory,
    MemoryCategory,
    MemoryChange,
    MemoryChangeKind,
    MemoryOrigin,
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
    RegisteredWorkspace,
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
    UiCsp,
    UiResource,
    UserMessage,
)

# v3: compaction_runs; v4: external_settings; v5: ui_resources; v6: model_settings;
# v7: the trash (all upgraded in place); v1 is refused
SCHEMA_VERSION = 13
APPLICATION_ID = 0x48494D41  # "HIMA": written into the database header (PRAGMA application_id)

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
_SCHEMA_V2 = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL) STRICT;
INSERT INTO meta VALUES ('next_id', 1);
CREATE TABLE conversations (
    conversation_id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    title TEXT,
    created_at TEXT NOT NULL,
    state_revision INTEGER NOT NULL DEFAULT 0,
    workspace_id TEXT,
    workspace_cwd TEXT,
    snapshot_revision INTEGER,
    snapshot_schema_version INTEGER,
    snapshot_body TEXT,
    snapshot_covers_through INTEGER,
    snapshot_basis TEXT
) STRICT;
CREATE INDEX conversations_by_owner ON conversations (owner);
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
# v3: a Run listed here is a COMPACTION Run (RunPurpose); every other Run is a TURN.
_V3_ADDITIONS = """
CREATE TABLE compaction_runs (
    run_id TEXT PRIMARY KEY REFERENCES runs (run_id)
) STRICT;
"""
# v4: a Conversation's own on/off choices for External Tool sources (V3-P53).
_V4_ADDITIONS = """
CREATE TABLE external_settings (
    conversation_id TEXT NOT NULL REFERENCES conversations,
    source TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    PRIMARY KEY (conversation_id, source)
) STRICT;
"""
# v5: Tool views (MCP Apps, V3-P58), content-addressed and shared by every
# Conversation; a call stored with its view names it and keeps its payload.
# A new database runs the same ALTERs, so its schema matches an upgraded one.
_V5_ADDITIONS = """
CREATE TABLE ui_resources (
    digest TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    uri TEXT NOT NULL,
    html TEXT NOT NULL,
    meta TEXT NOT NULL
) STRICT;
ALTER TABLE tool_executions ADD COLUMN ui_digest TEXT REFERENCES ui_resources (digest);
ALTER TABLE tool_executions ADD COLUMN app_payload TEXT;
"""
# v6: a Conversation's model choice (V3-P64), kept as made.
_V6_ADDITIONS = """
CREATE TABLE model_settings (
    conversation_id TEXT PRIMARY KEY REFERENCES conversations,
    model_id TEXT NOT NULL,
    reasoning_effort TEXT
) STRICT;
"""
# v7: the trash (V3-P65). purge_started_at is set only with trashed_at.
_V7_ADDITIONS = """
ALTER TABLE conversations ADD COLUMN trashed_at TEXT;
ALTER TABLE conversations ADD COLUMN purge_started_at TEXT;
"""
# v8: files (V3-P66). A blob row is shared by one principal's Conversations;
# expired_at set: its bytes are gone, the records stay. attached_at NULL: a
# staged upload. run_id/call_id: the call that presented the file.
_V8_ADDITIONS = """
CREATE TABLE blobs (
    blob_key TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    last_used_at TEXT NOT NULL,
    expired_at TEXT
) STRICT;
CREATE INDEX blobs_by_use ON blobs (last_used_at);
CREATE TABLE files (
    seq INTEGER PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations,
    file_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    origin TEXT NOT NULL,
    name TEXT NOT NULL,
    media_type TEXT NOT NULL,
    blob_key TEXT NOT NULL REFERENCES blobs,
    created_at TEXT NOT NULL,
    workspace_path TEXT,
    attached_at TEXT,
    run_id TEXT REFERENCES runs (run_id),
    call_id TEXT,
    UNIQUE (file_id, version)
) STRICT;
CREATE INDEX files_by_conversation ON files (conversation_id, seq);
CREATE INDEX files_by_blob ON files (blob_key);
"""
# v9: images (V3-P67 §4.1). image_status NULL: not an image (files made
# before v9 included). view_blob_key: the model copy, a blob of its own.
_V9_ADDITIONS = """
ALTER TABLE files ADD COLUMN image_status TEXT;
ALTER TABLE files ADD COLUMN image_type TEXT;
ALTER TABLE files ADD COLUMN image_width INTEGER;
ALTER TABLE files ADD COLUMN image_height INTEGER;
ALTER TABLE files ADD COLUMN view_blob_key TEXT REFERENCES blobs (blob_key);
ALTER TABLE files ADD COLUMN view_type TEXT;
ALTER TABLE files ADD COLUMN view_width INTEGER;
ALTER TABLE files ADD COLUMN view_height INTEGER;
CREATE INDEX files_by_view ON files (view_blob_key);
"""
# v10: the instructions a user writes (V3-P69), by principal and scope ('user' only for now).
_V10_ADDITIONS = """
CREATE TABLE instructions (
    owner TEXT NOT NULL,
    scope TEXT NOT NULL,
    text TEXT NOT NULL,
    revision INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (owner, scope)
) STRICT;
"""
# v11: the notes HIMARI keeps about a principal (V3-P71), and the last id given out to each.
_V11_ADDITIONS = """
CREATE TABLE memories (
    owner TEXT NOT NULL,
    memory_id INTEGER NOT NULL,
    category TEXT NOT NULL,
    text TEXT NOT NULL,
    origin TEXT NOT NULL,
    source_conversation TEXT,
    source_run TEXT,
    source_call TEXT,
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (owner, memory_id)
) STRICT;
CREATE INDEX memories_by_source ON memories (source_conversation);
CREATE TABLE memory_ids (
    owner TEXT PRIMARY KEY,
    last_id INTEGER NOT NULL
) STRICT;
"""
# v12: the searchable text of user messages and final answers, folded (V3-P72 §4.1);
# an upgrade fills it from the Events already there (_fill_event_text).
_V12_ADDITIONS = """
CREATE TABLE event_text (
    conversation_id TEXT NOT NULL REFERENCES conversations,
    sequence INTEGER NOT NULL,
    folded TEXT NOT NULL,
    PRIMARY KEY (conversation_id, sequence),
    FOREIGN KEY (conversation_id, sequence) REFERENCES events (conversation_id, sequence)
) STRICT;
"""
# v13: the folders a principal registered as Workspaces (V3-P74 §10.1); a
# Conversation attaches one with a Binding naming its workspace_id.
_V13_ADDITIONS = """
CREATE TABLE workspaces (
    seq INTEGER PRIMARY KEY,
    workspace_id TEXT NOT NULL UNIQUE,
    owner TEXT NOT NULL,
    environment_kind TEXT NOT NULL,
    location TEXT NOT NULL,
    label TEXT NOT NULL,
    note TEXT,
    filesystem TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (owner, environment_kind, location)
) STRICT;
CREATE INDEX bindings_by_workspace ON bindings (workspace_id);
"""
_ADDITIONS = (_V3_ADDITIONS, _V4_ADDITIONS, _V5_ADDITIONS, _V6_ADDITIONS, _V7_ADDITIONS, _V8_ADDITIONS,
              _V9_ADDITIONS, _V10_ADDITIONS, _V11_ADDITIONS, _V12_ADDITIONS,
              _V13_ADDITIONS)  # what each version from v3 added
_SCHEMA = _SCHEMA_V2 + "".join(_ADDITIONS)
# An older version → (its exact schema, what brings it to SCHEMA_VERSION).
_UPGRADES = {
    version: (_SCHEMA_V2 + "".join(_ADDITIONS[: version - 2]), "".join(_ADDITIONS[version - 2:]))
    for version in range(2, SCHEMA_VERSION)
}


class StoreLockedError(RuntimeError):
    """Another live store (this or another process) has the database open."""


class StoreSecurityError(RuntimeError):
    """The database files or their directory are not private to this user."""


class StoreSchemaError(RuntimeError):
    """The file is not a HIMARI database of the supported schema."""


class StoreBackupError(RuntimeError):
    """The backup an upgrade needs first could not be made (nothing upgraded)."""


log = logging.getLogger(__name__)


def _check_private(path: str, info: os.stat_result) -> None:
    """A store file must be a regular file of this user, with no group or
    other permission bits and no second name (hard link)."""
    if not stat.S_ISREG(info.st_mode):
        raise StoreSecurityError(f"{path} is not a regular file")
    if info.st_uid != os.geteuid():
        raise StoreSecurityError(f"{path} is owned by another user")
    if info.st_mode & 0o077:
        raise StoreSecurityError(f"{path} is accessible to other users (mode {stat.S_IMODE(info.st_mode):o}; expected 600)")
    if info.st_nlink != 1:
        raise StoreSecurityError(f"{path} has other hard links")


def _open_private(path: str) -> int:
    """Open or create ``path`` (0600) without following a symlink, then
    verify what was opened. Existing files are never re-permissioned: a
    file that was readable by others may already have leaked."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise StoreSecurityError(f"{path} is a symbolic link") from None
        raise
    try:
        _check_private(path, os.fstat(fd))
    except BaseException:
        os.close(fd)
        raise
    return fd


def _check_existing_private(path: str) -> None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode):
        raise StoreSecurityError(f"{path} is a symbolic link")
    _check_private(path, info)


def _check_directory(path: str) -> None:
    """Only this user may change the directory, so nobody can swap a store
    file (or pre-create a WAL/SHM file) between these checks and SQLite's
    own opens by name."""
    info = os.stat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise StoreSecurityError(f"{path} is not a directory")
    if info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise StoreSecurityError(f"{path} must be owned by this user and not writable by others")


def _schema_signature(conn: sqlite3.Connection) -> list[tuple[str, str, str, str | None]]:
    return [tuple(row) for row in conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name")]


def _expected_signature(schema: str = _SCHEMA) -> list[tuple[str, str, str, str | None]]:
    reference = sqlite3.connect(":memory:")
    try:
        for statement in schema.split(";"):
            if statement.strip():
                reference.execute(statement)
        return _schema_signature(reference)
    finally:
        reference.close()


class SqliteStore:
    """Durable storage plus admin helpers (not part of any port)."""

    def __init__(self, path: str | os.PathLike[str], *, files_dir: str | os.PathLike[str] | None = None) -> None:
        self.path = os.path.abspath(os.fspath(path))
        # V3-P66: the file store's bytes; ``<dir>/files`` unless given.
        self.blobs = FileStore(DirectoryBlobFiles(files_dir if files_dir is not None
                                                  else os.path.join(os.path.dirname(self.path), "files")),
                               _SqliteBlobRows(self))
        _check_directory(os.path.dirname(self.path))
        lock_fd = _open_private(f"{self.path}.lock")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(lock_fd)
            raise StoreLockedError(f"{self.path} is in use by another store") from None
        self._lock_fd: int | None = lock_fd
        self.backup_path: str | None = None  # the backup made before an upgrade at this open
        try:
            os.close(_open_private(self.path))
            for suffix in ("-wal", "-shm", "-journal"):  # SQLite creates these with the database's mode
                _check_existing_private(self.path + suffix)
            self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=True)
        except BaseException:
            self._release_lock()
            raise
        try:
            self.connection.row_factory = sqlite3.Row
            self._configure()
        except BaseException:
            self.close()
            raise

    def _configure(self) -> None:
        conn = self.connection
        application = conn.execute("PRAGMA application_id").fetchone()[0]
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        empty = conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0
        upgrade: str | None = None
        if not (empty and application == 0 and version == 0):  # an existing database: verify before writing
            if application != APPLICATION_ID:
                raise StoreSchemaError(f"{self.path} is not a HIMARI database")
            if version in _UPGRADES and _schema_signature(conn) == _expected_signature(_UPGRADES[version][0]):
                upgrade = _UPGRADES[version][1]
            if version != SCHEMA_VERSION and upgrade is None:
                raise StoreSchemaError(f"unsupported schema version {version} (expected {SCHEMA_VERSION})")
            if upgrade is None and _schema_signature(conn) != _expected_signature():
                raise StoreSchemaError(f"{self.path} does not have the HIMARI schema {SCHEMA_VERSION}")
            if conn.execute("SELECT count(*) FROM meta WHERE key='next_id'").fetchone()[0] != 1:
                raise StoreSchemaError(f"{self.path} has no id counter")
        if conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
            raise RuntimeError("SQLite WAL mode is unavailable")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA secure_delete=ON")
        if upgrade is not None:  # atomic: to the current version or not at all
            self.backup_path = self._back_up(version)
            log.warning("backed up %s (schema v%d) to %s before upgrading to v%d; delete the backup once it is"
                        " not needed: it keeps every Conversation as it was", self.path, version,
                        self.backup_path, SCHEMA_VERSION)
            with self.transaction() as tx:
                tx.executescript_parts(upgrade)
                if version < 12:
                    _fill_event_text(tx)
                tx.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        if empty:
            with self.transaction() as tx:
                tx.executescript_parts(_SCHEMA)
                tx.execute(f"PRAGMA application_id={APPLICATION_ID}")
                tx.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def _back_up(self, version: int) -> str:
        """V3-P65 §8.1: copy, check and make durable a backup of this v``version``
        database before it is upgraded; its path. Any failure removes what it
        made and raises StoreBackupError."""
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        final = f"{self.path}.v{version}-{stamp}.bak"
        partial = f"{final}.partial"
        made: list[str] = []
        try:
            fd = os.open(partial, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            made.append(partial)
            try:
                _check_private(partial, os.fstat(fd))
            finally:
                os.close(fd)
            target = sqlite3.connect(partial)
            try:
                self.connection.backup(target)
                target.execute("PRAGMA journal_mode=DELETE")  # one self-contained file, no -wal/-shm
            finally:
                target.close()
            _check_existing_private(f"{partial}-journal")
            check = sqlite3.connect(f"file:{partial}?mode=ro", uri=True)
            try:
                if (check.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
                        or check.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID
                        or check.execute("PRAGMA user_version").fetchone()[0] != version
                        or _schema_signature(check) != _expected_signature(_UPGRADES[version][0])):
                    raise StoreBackupError("the copy does not read back as the database")
            finally:
                check.close()
            fd = os.open(partial, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            os.link(partial, final)  # never replaces an existing file
            made.append(final)
            os.unlink(partial)
            made.remove(partial)
            directory = os.open(os.path.dirname(self.path), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                os.fsync(directory)  # the new name survives power loss too
            finally:
                os.close(directory)
            return final
        except (OSError, sqlite3.Error, StoreBackupError, StoreSecurityError) as error:
            left = []
            for path in made:
                try:
                    os.unlink(path)
                except OSError:
                    left.append(path)
            note = f"; could not remove {', '.join(left)}" if left else ""
            raise StoreBackupError(f"could not back up {self.path} before upgrading it from schema v{version}"
                                   f" (nothing was upgraded): {error}{note}") from error

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

    def create_conversation(self, scope: TrustedScope, *, title: str | None = None) -> None:
        with self.transaction() as tx:
            tx.insert_conversation(scope, title)

    def activate_environment(
        self,
        scope: TrustedScope,
        kind: EnvironmentKind,
        workspace_id: WorkspaceId,
        *,
        cwd_relative: str | None = None,
    ) -> BindingAuthority:
        with self.transaction() as tx:
            return tx.activate(scope, kind, workspace_id, cwd_relative)

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

    def executemany(self, sql: str, params: Iterable[Sequence[object]]) -> None:
        self._conn.executemany(sql, params)

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
        conv = self.owned(scope)
        if conv["trashed_at"] is not None:  # V3-P65: a trashed Conversation is a missing one
            raise DataError(DataErrorCode.NOT_AUTHORIZED, scope.conversation_id)
        return conv

    def owned(self, scope: TrustedScope) -> sqlite3.Row:
        """The owner check alone (the trash methods, V3-P65)."""
        conv = self.one("SELECT * FROM conversations WHERE conversation_id=?", scope.conversation_id)
        if conv is None or conv["owner"] != scope.principal_id:
            # Same error for foreign and missing (indistinguishability).
            raise DataError(DataErrorCode.NOT_AUTHORIZED, scope.conversation_id)
        return conv

    def bump_state(self, scope: TrustedScope) -> None:
        self.execute("UPDATE conversations SET state_revision=state_revision+1 WHERE conversation_id=?", scope.conversation_id)

    def insert_conversation(self, scope: TrustedScope, title: str | None) -> None:
        if self.one("SELECT 1 FROM conversations WHERE conversation_id=?", scope.conversation_id):
            raise DataError(DataErrorCode.DUPLICATE_IDENTITY, scope.conversation_id)
        self.execute(
            "INSERT INTO conversations (conversation_id, owner, title, created_at) VALUES (?, ?, ?, ?)",
            scope.conversation_id, scope.principal_id, title, _timestamp(datetime.now(UTC)),
        )

    def activate(
        self, scope: TrustedScope, kind: EnvironmentKind, workspace_id: WorkspaceId, cwd_relative: str | None
    ) -> BindingAuthority:
        tx = self
        conv = tx.authorized(scope)
        if tx.one("SELECT 1 FROM activations WHERE conversation_id=? AND environment_kind=?", scope.conversation_id, kind):
            raise DataError(DataErrorCode.DUPLICATE_IDENTITY, f"active {kind} exists (P0: one per kind)")
        instance_id = EnvironmentInstanceId(tx.new_id("env"))
        # Each Binding carries its own working directory (default: the
        # Conversation's own folder, V3-033).
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


class _SqliteBlobRows:
    """The ``blobs`` rows for the file store (V3-P66 §4.5); each call one transaction."""

    def __init__(self, store: SqliteStore) -> None:
        self._store = store

    @staticmethod
    def _row(row: sqlite3.Row) -> BlobRow:
        return BlobRow(row["blob_key"], row["owner"], row["sha256"], row["size"],
                       datetime.fromisoformat(row["last_used_at"]),
                       None if row["expired_at"] is None else datetime.fromisoformat(row["expired_at"]))

    def blob_row(self, key: str) -> BlobRow | None:
        with self._store.transaction() as tx:
            row = tx.one("SELECT * FROM blobs WHERE blob_key=?", key)
            return None if row is None else self._row(row)

    def live_total(self) -> int:
        with self._store.transaction() as tx:
            row = tx.one("SELECT COALESCE(SUM(size), 0) AS total FROM blobs WHERE expired_at IS NULL")
            assert row is not None
            return int(row["total"])

    def expirable(self, used_before: datetime) -> Sequence[BlobRow]:
        with self._store.transaction() as tx:
            rows = tx.all("SELECT * FROM blobs b WHERE expired_at IS NULL AND last_used_at < ?"
                          " AND NOT EXISTS (SELECT 1 FROM files f WHERE (f.blob_key=b.blob_key OR f.view_blob_key=b.blob_key)"
                          " AND f.attached_at IS NULL)"
                          " ORDER BY last_used_at, blob_key", _timestamp(used_before))
            return [self._row(r) for r in rows]

    def expire_blobs(self, keys: Sequence[str], now: datetime) -> Sequence[str]:
        expired: list[str] = []
        with self._store.transaction() as tx:
            for key in keys:
                # A staged upload may have come to refer to it meanwhile (re-review N3).
                changed = tx.execute("UPDATE blobs SET expired_at=? WHERE blob_key=? AND expired_at IS NULL AND NOT EXISTS"
                           " (SELECT 1 FROM files f WHERE (f.blob_key=blobs.blob_key OR f.view_blob_key=blobs.blob_key)"
                           " AND f.attached_at IS NULL)",
                           _timestamp(now), key)
                if changed.rowcount:
                    expired.append(key)
        return expired

    def touch_blob(self, row: BlobRow) -> None:
        with self._store.transaction() as tx:
            tx.execute("INSERT INTO blobs (blob_key, owner, sha256, size, last_used_at, expired_at)"
                       " VALUES (?, ?, ?, ?, ?, NULL) ON CONFLICT (blob_key) DO UPDATE SET"
                       " last_used_at=excluded.last_used_at, expired_at=NULL",
                       row.key, row.owner, row.sha256, row.size, _timestamp(row.last_used_at))

    def reserve(self, row: BlobRow, max_bytes: int, used_before: datetime) -> Sequence[str]:
        with self._store.transaction() as tx:
            live = tx.one("SELECT COALESCE(SUM(size), 0) AS total FROM blobs WHERE expired_at IS NULL")
            assert live is not None
            need = int(live["total"]) + row.size - max_bytes
            chosen: list[str] = []
            if need > 0:
                freed = 0
                for candidate in tx.all(
                        "SELECT blob_key, size FROM blobs b WHERE expired_at IS NULL AND last_used_at < ?"
                        " AND NOT EXISTS (SELECT 1 FROM files f WHERE (f.blob_key=b.blob_key OR f.view_blob_key=b.blob_key)"
                          " AND f.attached_at IS NULL)"
                        " ORDER BY last_used_at, blob_key", _timestamp(used_before)):
                    if freed >= need:
                        break
                    chosen.append(candidate["blob_key"])
                    freed += candidate["size"]
                if freed < need:
                    raise DataError(DataErrorCode.STORE_FULL, f"needs {need} more bytes")  # rolls back: nothing changed
                for key in chosen:
                    tx.execute("UPDATE blobs SET expired_at=? WHERE blob_key=?", _timestamp(row.last_used_at), key)
            tx.execute("INSERT INTO blobs (blob_key, owner, sha256, size, last_used_at, expired_at)"
                       " VALUES (?, ?, ?, ?, ?, NULL) ON CONFLICT (blob_key) DO UPDATE SET"
                       " last_used_at=excluded.last_used_at, expired_at=NULL",
                       row.key, row.owner, row.sha256, row.size, _timestamp(row.last_used_at))
            return chosen

    def drop_unreferenced(self, key: str, used_before: datetime) -> bool:
        with self._store.transaction() as tx:
            cursor = tx.execute("DELETE FROM blobs WHERE blob_key=? AND (expired_at IS NOT NULL OR last_used_at < ?)"
                                " AND NOT EXISTS (SELECT 1 FROM files f WHERE f.blob_key=blobs.blob_key OR f.view_blob_key=blobs.blob_key)",
                                key, _timestamp(used_before))
            return cursor.rowcount > 0

    def unreferenced_keys(self, used_before: datetime) -> Sequence[str]:
        with self._store.transaction() as tx:
            rows = tx.all("SELECT blob_key FROM blobs b WHERE (expired_at IS NOT NULL OR last_used_at < ?)"
                          " AND NOT EXISTS (SELECT 1 FROM files f WHERE f.blob_key=b.blob_key OR f.view_blob_key=b.blob_key)",
                          _timestamp(used_before))
            return [r["blob_key"] for r in rows]

    def expired_keys(self) -> Sequence[str]:
        with self._store.transaction() as tx:
            return [r["blob_key"] for r in tx.all("SELECT blob_key FROM blobs WHERE expired_at IS NOT NULL")]

    def known_keys(self) -> frozenset[str]:
        with self._store.transaction() as tx:
            return frozenset(r["blob_key"] for r in tx.all("SELECT blob_key FROM blobs"))


def _instructions(row: sqlite3.Row | None) -> Instructions:
    if row is None:
        return Instructions("", 0)
    return Instructions(row["text"], row["revision"], datetime.fromisoformat(row["updated_at"]))


def _memory(row: sqlite3.Row) -> Memory:
    call = None if row["source_call"] is None else CallRef(RunId(row["source_run"]), CallId(row["source_call"]))
    source = None if row["source_conversation"] is None else ConversationId(row["source_conversation"])
    return Memory(row["memory_id"], MemoryCategory(row["category"]), row["text"], MemoryOrigin(row["origin"]),
                  source, call, row["revision"], datetime.fromisoformat(row["created_at"]),
                  datetime.fromisoformat(row["updated_at"]))


FILL_BATCH = 500  # Events read at a time while an upgrade fills event_text


def _fill_event_text(tx: _Tx) -> None:
    """V3-P72 §4.2: the searchable text of the Events a v11 (or older) database
    already holds, folded as new Events are. Read FILL_BATCH at a time in key
    order, so the memory it takes does not grow with the history (PR #79
    review B1); still one transaction with the rest of the upgrade."""
    kinds = [k.value for k in SEARCH_KINDS]
    marks = ", ".join("?" * len(kinds))
    after: tuple[str, int] = ("", 0)
    while True:
        rows = tx.all(f"SELECT conversation_id, sequence, kind, payload FROM events WHERE kind IN ({marks})"
                      " AND (conversation_id, sequence) > (?, ?) ORDER BY conversation_id, sequence LIMIT ?",
                      *kinds, *after, FILL_BATCH)
        if not rows:
            return
        texts = [(row["conversation_id"], row["sequence"],
                  rules.event_text(codec.payload_from(EventKind(row["kind"]), row["payload"]))) for row in rows]
        tx.executemany("INSERT INTO event_text (conversation_id, sequence, folded) VALUES (?, ?, ?)",
                       [(cid, seq, rules.fold(text)) for cid, seq, text in texts if text is not None])
        after = (rows[-1]["conversation_id"], rows[-1]["sequence"])


def _timestamp(moment: datetime) -> str:
    """Fixed-width UTC ISO text, so string order is time order."""
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


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


_RUN_COLUMNS = (
    "run_id, conversation_id, status, trigger_refs, prompt_basis, provider_key, model_id, raw_representation,"
    " usage, error_code, continuation, raw_response_content_type, projected_event_id"
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
        purpose=RunPurpose.COMPACTION if row["compaction"] else RunPurpose.TURN,
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
        ui_digest=row["ui_digest"],
    )


def _ui_meta(resource: UiResource) -> str:
    csp = resource.csp
    return codec.dump_json({
        "csp": {"connect_domains": csp.connect_domains, "resource_domains": csp.resource_domains,
                "frame_domains": csp.frame_domains, "base_uri_domains": csp.base_uri_domains},
        "prefers_border": resource.prefers_border,
    })


def _ui_resource(row: sqlite3.Row) -> UiResource:
    meta = codec.load_json(row["meta"])
    assert isinstance(meta, Mapping)
    csp = meta["csp"]
    assert isinstance(csp, Mapping)

    def domains(key: str) -> tuple[str, ...]:
        value = csp[key]
        assert isinstance(value, tuple)
        return tuple(str(d) for d in value)

    prefers_border = meta["prefers_border"]
    return UiResource(
        row["digest"], row["source"], row["uri"], row["html"],
        UiCsp(domains("connect_domains"), domains("resource_domains"), domains("frame_domains"),
              domains("base_uri_domains")),
        prefers_border if isinstance(prefers_border, bool) else None,
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


_FILE_COLUMNS = ("f.file_id, f.version, f.origin, f.name, f.media_type, f.created_at, f.workspace_path,"
                 " f.attached_at, f.run_id, f.call_id, b.blob_key, b.sha256, b.size, b.expired_at,"
                 " f.image_status, f.image_type, f.image_width, f.image_height, f.view_type, f.view_width,"
                 " f.view_height, v.blob_key AS view_key, v.sha256 AS view_sha256, v.size AS view_size,"
                 " v.expired_at AS view_expired_at")
_FILE_FROM = ("FROM files f JOIN blobs b ON b.blob_key=f.blob_key"
              " LEFT JOIN blobs v ON v.blob_key=f.view_blob_key")


def _file(row: sqlite3.Row) -> FileRecord:
    return FileRecord(
        file_id=FileId(row["file_id"]), version=row["version"], origin=FileOrigin(row["origin"]),
        name=row["name"], media_type=row["media_type"],
        blob=StoredBlob(row["blob_key"], row["sha256"], row["size"]),
        created_at=datetime.fromisoformat(row["created_at"]), workspace_path=row["workspace_path"],
        attached=row["attached_at"] is not None, expired=row["expired_at"] is not None,
        call=None if row["run_id"] is None else CallRef(RunId(row["run_id"]), CallId(row["call_id"])),
        image_status=ImageStatus(row["image_status"] or ImageStatus.NOT_IMAGE.value),
        image_type=row["image_type"], image_width=row["image_width"], image_height=row["image_height"],
        view=None if row["view_key"] is None else ImageView(
            StoredBlob(row["view_key"], row["view_sha256"], row["view_size"]), row["view_type"], row["view_width"],
            row["view_height"], expired=row["view_expired_at"] is not None),
    )


def _image_columns(image: ImageSetup) -> tuple[object, ...]:
    view = image.view
    return (image.status.value, image.image_type, image.width, image.height,
            None if view is None else view.blob.key, None if view is None else view.media_type,
            None if view is None else view.width, None if view is None else view.height)


_IMAGE_COLUMNS = "image_status, image_type, image_width, image_height, view_blob_key, view_type, view_width, view_height"


def _alive(tx: _Tx, blob: StoredBlob) -> bool:
    row = tx.one("SELECT size, expired_at FROM blobs WHERE blob_key=?", blob.key)
    return row is not None and row["expired_at"] is None and row["size"] == blob.size


def _refused(file_id: str, why: str) -> DataError:
    return DataError(DataErrorCode.ATTACHMENT_REFUSED, f"{file_id}: {why}", file_id=FileId(file_id))


class SqliteData:
    """Implements ConversationLog, ArtifactStore, WorkspaceState, StateQuery,
    ConversationCatalog, ConversationRetention, ExternalSourceSettings,
    FileCatalog, FileRetention, InstructionsStore (V3-P69), MemoryStore
    (V3-P71) and WorkspaceRegistry (V3-P74); ``blobs`` is the BlobStore
    (V3-P66)."""

    def __init__(self, store: SqliteStore) -> None:
        self._store = store
        self.blobs = store.blobs

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
            event.payload_schema_version, _timestamp(event.created_at), origin,
        )
        text = rules.event_text(payload)
        if text is not None:  # V3-P72 §4.1: searchable from the same transaction on
            tx.execute("INSERT INTO event_text (conversation_id, sequence, folded) VALUES (?, ?, ?)",
                       scope.conversation_id, event.sequence, rules.fold(text))
        return event

    @staticmethod
    def _run_row(tx: _Tx, scope: TrustedScope, run_id: RunId, *, response_body: bool = False) -> sqlite3.Row:
        # The raw bodies stay on disk unless asked for: a request body holds the
        # whole prompt of its time, and the Prompt loader reads a Run per turn (Q-42).
        body = ", raw_response_body" if response_body else ""
        row = tx.one(
            f"SELECT {_RUN_COLUMNS}{body},"
            " EXISTS (SELECT 1 FROM compaction_runs c WHERE c.run_id=runs.run_id) AS compaction"
            " FROM runs WHERE run_id=? AND conversation_id=?",
            run_id, scope.conversation_id,
        )
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
            "SELECT run_id FROM runs WHERE conversation_id=? AND status=? AND projected_event_id IS NOT NULL"
            " ORDER BY seq DESC LIMIT 1",
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

    @staticmethod
    def _check_attachments(tx: _Tx, scope: TrustedScope, file_ids: Sequence[str], *,
                           cutoff: datetime | None) -> list[sqlite3.Row]:
        """V3-P66 §4.2: staged uploads of this Conversation, each once, at most
        MAX_ATTACHMENTS, bytes not expired; ``cutoff``: none staged before it."""
        if len(file_ids) > MAX_ATTACHMENTS:
            raise DataError(DataErrorCode.ATTACHMENT_REFUSED, f"more than {MAX_ATTACHMENTS} attachments")
        rows: list[sqlite3.Row] = []
        for file_id in file_ids:
            if any(r["file_id"] == file_id for r in rows):
                raise _refused(file_id, "attached twice")
            row = tx.one(f"SELECT f.seq, {_FILE_COLUMNS} {_FILE_FROM} WHERE f.file_id=? AND f.conversation_id=?"
                         " ORDER BY f.version DESC LIMIT 1", file_id, scope.conversation_id)
            if row is None:
                raise _refused(file_id, "no such upload")
            if row["origin"] != FileOrigin.UPLOAD.value or row["version"] != 1:
                raise _refused(file_id, "not an upload")
            if row["attached_at"] is not None:
                raise _refused(file_id, "already sent")
            if row["expired_at"] is not None:
                raise _refused(file_id, "expired")
            if cutoff is not None and datetime.fromisoformat(row["created_at"]) < cutoff:
                raise _refused(file_id, "too old; upload it again")
            rows.append(row)
        return rows

    def _attach(self, tx: _Tx, scope: TrustedScope, blocks: Sequence[object]) -> None:
        """T1/T7a: check and attach the message's uploads in its transaction."""
        refs = [b for b in blocks if isinstance(b, AttachmentRef)]
        if not refs:
            return
        if any(r.version != 1 for r in refs):
            raise _refused(next(r.file_id for r in refs if r.version != 1), "not an upload")
        rows = self._check_attachments(tx, scope, [r.file_id for r in refs], cutoff=None)
        now = _timestamp(datetime.now(UTC))
        for row in rows:
            tx.execute("UPDATE files SET attached_at=? WHERE seq=?", now, row["seq"])

    @staticmethod
    def _attach_tool_images(tx: _Tx, scope: TrustedScope, call: CallRef, blocks: Sequence[object]) -> None:
        """V3-P67 §4.2: a result's attachments are this call's staged Tool images."""
        now = _timestamp(datetime.now(UTC))
        for ref in (b for b in blocks if isinstance(b, AttachmentRef)):
            row = tx.one("SELECT seq, origin, attached_at, run_id, call_id FROM files WHERE file_id=? AND version=?"
                         " AND conversation_id=?", ref.file_id, ref.version, scope.conversation_id)
            if (row is None or row["origin"] != FileOrigin.TOOL.value or row["attached_at"] is not None
                    or (row["run_id"], row["call_id"]) != (call.run_id, call.call_id)):
                raise DataError(DataErrorCode.INVALID_ARGUMENT, f"{ref.file_id} is not an image of this call")
            tx.execute("UPDATE files SET attached_at=? WHERE seq=?", now, row["seq"])

    def _present(self, tx: _Tx, scope: TrustedScope, call: CallRef, files: Sequence[PresentedFile]) -> None:
        """T7: record what the call presented (§7.1), each path's next version."""
        now = datetime.now(UTC)
        since = self._presented_since(tx, scope, now - PRESENT_WINDOW)
        if since + sum(f.blob.size for f in files) > PRESENT_LIMIT_BYTES:
            raise DataError(DataErrorCode.LIMIT_EXCEEDED, f"over {PRESENT_LIMIT_BYTES} bytes presented in the hour")
        for presented in files:
            blob = tx.one("SELECT * FROM blobs WHERE blob_key=?", presented.blob.key)
            if blob is None or blob["expired_at"] is not None or blob["size"] != presented.blob.size:
                raise DataError(DataErrorCode.BLOB_MISSING, presented.blob.key)
            known = tx.one("SELECT file_id, MAX(version) AS version FROM files WHERE conversation_id=?"
                           " AND origin=? AND workspace_path=? GROUP BY file_id ORDER BY MAX(seq) DESC LIMIT 1",
                           scope.conversation_id, FileOrigin.PRESENTED.value, presented.workspace_path)
            file_id, version = (known["file_id"], known["version"] + 1) if known else (tx.new_id("file"), 1)
            tx.execute("INSERT INTO files (conversation_id, file_id, version, origin, name, media_type, blob_key,"
                       " created_at, workspace_path, attached_at, run_id, call_id)"
                       " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       scope.conversation_id, file_id, version, FileOrigin.PRESENTED.value, presented.name,
                       presented.media_type, presented.blob.key, _timestamp(now), presented.workspace_path,
                       _timestamp(now), call.run_id, call.call_id)

    @staticmethod
    def _presented_since(tx: _Tx, scope: TrustedScope, since: datetime) -> int:
        row = tx.one("SELECT COALESCE(SUM(b.size), 0) AS total FROM files f JOIN blobs b ON b.blob_key=f.blob_key"
                     " WHERE f.conversation_id=? AND f.origin=? AND f.created_at >= ?",
                     scope.conversation_id, FileOrigin.PRESENTED.value, _timestamp(since))
        assert row is not None
        return int(row["total"])

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
            self._attach(tx, scope, message.blocks)
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
                if preparation.purpose is RunPurpose.COMPACTION:
                    tx.execute("INSERT INTO compaction_runs (run_id) VALUES (?)", run_id)
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
                case RawUncertain(error_code=code, body=body, content_type=content_type):
                    # What arrived of a stream before it ended (V3-P73 §4), kept as received.
                    self._set_run(tx, run_id, RunStatus.OUTCOME_UNKNOWN, raw_response_body=body,
                                  raw_response_content_type=content_type, error_code=code)

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
            if run["compaction"]:
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
            run = self._run_row(tx, scope, run_id, response_body=True)
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
            view = rules.check_app_view(completion)
            if completion.presented_files and completion.status is not ToolExecutionStatus.SUCCEEDED:
                raise DataError(DataErrorCode.INVALID_ARGUMENT, "only a succeeded call presents files")
            if completion.memory_change is not None and completion.status is not ToolExecutionStatus.SUCCEEDED:
                raise DataError(DataErrorCode.INVALID_ARGUMENT, "only a succeeded call changes memory")
            self._apply_artifacts(tx, scope, completion.artifact_mutations)  # CAS errors roll everything back
            if completion.memory_change is not None:
                self._apply_memory(tx, scope, call, completion.memory_change)
            if view is not None:
                self._store_ui_resource(tx, view.resource)
            if completion.presented_files:
                self._present(tx, scope, call, completion.presented_files)
            self._attach_tool_images(tx, scope, call, completion.model_result.blocks)
            tx.execute(
                "UPDATE tool_executions SET status=?, model_result=?, raw_result=?, ui_digest=?, app_payload=?"
                " WHERE seq=?",
                completion.status.value, codec.model_result(completion.model_result),
                codec.dump_json(completion.raw_result),
                None if view is None else view.resource.digest,
                None if view is None else codec.dump_json(view.payload), record["seq"],
            )
            if not completion.record_event:
                return None
            return self._new_event(tx, scope, ToolResultRecord(call, completion.model_result), call.run_id)

    @staticmethod
    def _store_ui_resource(tx: _Tx, resource: UiResource) -> None:
        """Content-addressed: an existing digest must hold the same view."""
        known = tx.one("SELECT * FROM ui_resources WHERE digest=?", resource.digest)
        if known is None:
            tx.execute("INSERT INTO ui_resources (digest, source, uri, html, meta) VALUES (?, ?, ?, ?, ?)",
                       resource.digest, resource.source, resource.uri, resource.html, _ui_meta(resource))
        elif _ui_resource(known) != resource:
            raise DataError(DataErrorCode.INVALID_ARGUMENT, "another view is stored under this digest")

    async def read_app_view(self, scope: TrustedScope, call: CallRef) -> StoredAppView | None:
        with self._tx() as tx:
            tx.authorized(scope)
            row = self._execution_row(tx, scope, call)
            if row["ui_digest"] is None:
                return None
            resource = tx.one("SELECT * FROM ui_resources WHERE digest=?", row["ui_digest"])
            assert resource is not None  # REFERENCES
            record = _execution(row)
            view = AppView(_ui_resource(resource), codec.load_json(row["app_payload"]))
            return StoredAppView(call, record.tool_id, record.arguments, record.status, view)

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
            self._attach(tx, scope, answer.blocks)
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
            discarded: list[RunId] = []
            rows = tx.all(
                "SELECT run_id, status, EXISTS (SELECT 1 FROM compaction_runs c WHERE c.run_id=runs.run_id) AS compaction"
                " FROM runs WHERE conversation_id=? ORDER BY seq",
                scope.conversation_id,
            )
            for run in rows:
                run_id = RunId(run["run_id"])
                if run["status"] == RunStatus.DISPATCH_CLAIMED.value:
                    self._set_run(tx, run_id, RunStatus.OUTCOME_UNKNOWN, error_code="interrupted")
                    unknown_runs.append(run_id)
                elif run["status"] == RunStatus.PREPARED.value:
                    self._set_run(tx, run_id, RunStatus.ABANDONED_NOT_SENT)
                    abandoned.append(run_id)
                elif run["status"] == RunStatus.RECEIVED.value and run["compaction"]:
                    self._set_run(tx, run_id, RunStatus.PROJECTION_FAILED, error_code="interrupted")
                    discarded.append(run_id)
                elif run["status"] == RunStatus.RECEIVED.value:
                    received.append(run_id)
            claimed = tx.all(
                "SELECT seq, run_id, call_id FROM tool_executions WHERE conversation_id=? AND status=? ORDER BY seq",
                scope.conversation_id, ToolExecutionStatus.EXECUTION_CLAIMED.value,
            )
            for row in claimed:
                tx.execute("UPDATE tool_executions SET status=? WHERE seq=?", ToolExecutionStatus.UNKNOWN.value, row["seq"])
                unknown_tools.append(CallRef(RunId(row["run_id"]), CallId(row["call_id"])))
            return RecoveryReport(tuple(unknown_runs), tuple(abandoned), tuple(unknown_tools), tuple(received),
                                  tuple(discarded))

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

    async def record_compaction_step(self, scope: TrustedScope, run_id: RunId) -> None:
        with self._tx() as tx:
            tx.authorized(scope)
            run = self._run_row(tx, scope, run_id)
            if run["status"] != RunStatus.RECEIVED.value or not run["compaction"]:
                raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"{run_id} is not a received compaction Run")
            self._set_run(tx, run_id, RunStatus.PROJECTED)

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
        with self._tx() as tx:
            conv = tx.authorized(scope)
            run = self._run_row(tx, scope, run_id)
            if run["status"] != RunStatus.RECEIVED.value or not run["compaction"]:
                raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"{run_id} is not a received compaction Run")
            current = conv["snapshot_revision"]
            if current != expected_revision:
                raise DataError(DataErrorCode.VERSION_CONFLICT, "snapshot")
            row = tx.one("SELECT max(sequence) AS s FROM events WHERE conversation_id=?", scope.conversation_id)
            last = int(row["s"] or 0) if row is not None else 0
            previous = conv["snapshot_covers_through"]
            if covers_through_sequence > last or (
                previous is not None and covers_through_sequence < previous
            ):
                raise DataError(DataErrorCode.STALE_BASIS, f"coverage {covers_through_sequence}")
            revision = int(current or 0) + 1
            tx.execute(
                "UPDATE conversations SET snapshot_revision=?, snapshot_schema_version=?, snapshot_body=?,"
                " snapshot_covers_through=?, snapshot_basis=? WHERE conversation_id=?",
                revision, schema_version, codec.dump_json(body), covers_through_sequence,
                codec.prompt_basis(basis), scope.conversation_id,
            )
            self._set_run(tx, run_id, RunStatus.PROJECTED)
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

    # --- WorkspaceRegistry (V3-P74 §10) ----------------------------------------------

    @staticmethod
    def _registered(tx: _Tx, row: sqlite3.Row) -> RegisteredWorkspace:
        used = tx.one("SELECT count(DISTINCT conversation_id) AS n FROM bindings WHERE workspace_id=?",
                      row["workspace_id"])
        assert used is not None
        return RegisteredWorkspace(WorkspaceId(row["workspace_id"]), EnvironmentKind(row["environment_kind"]),
                                   row["location"], row["label"], row["note"],
                                   datetime.fromisoformat(row["created_at"]), int(used["n"]), row["filesystem"])

    @staticmethod
    def _workspace_row(tx: _Tx, principal: PrincipalId, workspace_id: object) -> sqlite3.Row:
        row = tx.one("SELECT * FROM workspaces WHERE workspace_id=? AND owner=?", workspace_id, principal)
        if row is None:  # another principal's is a missing one
            raise DataError(DataErrorCode.NOT_FOUND, f"no workspace {workspace_id}")
        return row

    @staticmethod
    def _activation_row(tx: _Tx, scope: TrustedScope, kind: object) -> sqlite3.Row | None:
        return tx.one("SELECT * FROM activations WHERE conversation_id=? AND environment_kind=? ORDER BY seq LIMIT 1",
                      scope.conversation_id, kind)

    @staticmethod
    def _registered_binding(tx: _Tx, row: sqlite3.Row) -> bool:
        return tx.one("SELECT 1 FROM workspaces WHERE workspace_id=?", row["workspace_id"]) is not None

    async def list_workspaces(self, principal: PrincipalId, kind: EnvironmentKind) -> tuple[RegisteredWorkspace, ...]:
        with self._tx() as tx:
            rows = tx.all("SELECT * FROM workspaces WHERE owner=? AND environment_kind=? ORDER BY seq", principal, kind)
            return tuple(self._registered(tx, r) for r in rows)

    async def register_workspace(self, principal: PrincipalId, kind: EnvironmentKind, location: str, label: str,
                                 note: str | None = None, filesystem: str | None = None) -> RegisteredWorkspace:
        location = rules.check_workspace_location(location)
        label, note = rules.check_workspace_label(label), rules.check_workspace_note(note)
        filesystem = rules.check_workspace_filesystem(filesystem)
        with self._tx() as tx:
            if tx.one("SELECT 1 FROM workspaces WHERE owner=? AND environment_kind=? AND location=?",
                      principal, kind, location):
                raise DataError(DataErrorCode.DUPLICATE_IDENTITY, f"{location} is registered already")
            count = tx.one("SELECT count(*) AS n FROM workspaces WHERE owner=? AND environment_kind=?", principal, kind)
            assert count is not None
            if count["n"] >= MAX_WORKSPACES:
                raise DataError(DataErrorCode.LIMIT_EXCEEDED, f"at most {MAX_WORKSPACES} workspaces")
            workspace_id = tx.new_id("wsp")
            tx.execute("INSERT INTO workspaces (workspace_id, owner, environment_kind, location, label, note, filesystem,"
                       " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", workspace_id, principal, kind, location, label,
                       note, filesystem, _timestamp(datetime.now(UTC)))
            return self._registered(tx, self._workspace_row(tx, principal, workspace_id))

    async def update_workspace(self, principal: PrincipalId, workspace_id: WorkspaceId, *, label: str,
                               note: str | None) -> RegisteredWorkspace:
        label, note = rules.check_workspace_label(label), rules.check_workspace_note(note)
        with self._tx() as tx:
            self._workspace_row(tx, principal, workspace_id)
            tx.execute("UPDATE workspaces SET label=?, note=? WHERE workspace_id=?", label, note, workspace_id)
            return self._registered(tx, self._workspace_row(tx, principal, workspace_id))

    async def unregister_workspace(self, principal: PrincipalId, workspace_id: WorkspaceId) -> None:
        with self._tx() as tx:
            self._workspace_row(tx, principal, workspace_id)
            if tx.one("SELECT 1 FROM bindings WHERE workspace_id=?", workspace_id):
                raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"{workspace_id} is attached to a conversation")
            tx.execute("DELETE FROM workspaces WHERE workspace_id=?", workspace_id)

    async def read_conversation_workspaces(self, scope: TrustedScope,
                                           kind: EnvironmentKind) -> ConversationWorkspaces:
        with self._tx() as tx:
            tx.authorized(scope)
            activation = self._activation_row(tx, scope, kind)
            if activation is None:
                raise DataError(DataErrorCode.NOT_FOUND, f"{kind} is not active in this conversation")
            attached: list[AttachedWorkspace] = []
            for row in tx.all("SELECT * FROM bindings WHERE conversation_id=? AND environment_instance_id=?"
                              " ORDER BY seq", scope.conversation_id, activation["environment_instance_id"]):
                registered = tx.one("SELECT * FROM workspaces WHERE workspace_id=? AND owner=?", row["workspace_id"],
                                    scope.principal_id)
                attached.append(AttachedWorkspace(_binding(row),
                                                  None if registered is None else self._registered(tx, registered)))
            attached.sort(key=lambda a: a.workspace is not None)  # the own folder first; stable otherwise
            default = activation["default_binding_id"]
            return ConversationWorkspaces(None if default is None else BindingId(default), tuple(attached))

    async def attach_workspace(self, scope: TrustedScope, workspace_id: WorkspaceId) -> BindingAuthority:
        with self._tx() as tx:
            tx.authorized(scope)
            workspace = self._workspace_row(tx, scope.principal_id, workspace_id)
            kind = EnvironmentKind(workspace["environment_kind"])
            activation = self._activation_row(tx, scope, kind)
            if activation is None:
                raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"{kind} is not active in this conversation")
            if tx.one("SELECT 1 FROM bindings WHERE conversation_id=? AND workspace_id=?", scope.conversation_id,
                      workspace_id):
                raise DataError(DataErrorCode.DUPLICATE_IDENTITY, f"{workspace_id} is attached already")
            instance = EnvironmentInstanceId(activation["environment_instance_id"])
            count = tx.one("SELECT count(*) AS n FROM bindings b WHERE conversation_id=? AND environment_instance_id=?"
                           " AND EXISTS (SELECT 1 FROM workspaces w WHERE w.workspace_id=b.workspace_id)",
                           scope.conversation_id, instance)
            assert count is not None
            if count["n"] >= MAX_ATTACHED_WORKSPACES:
                raise DataError(DataErrorCode.LIMIT_EXCEEDED, f"at most {MAX_ATTACHED_WORKSPACES} attached workspaces")
            binding = BindingAuthority(BindingId(tx.new_id("bind")), instance, kind, workspace_id, 1, "")
            tx.execute("INSERT INTO bindings (conversation_id, binding_id, environment_instance_id, environment_kind,"
                       " workspace_id, revision, cwd_relative) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       scope.conversation_id, binding.binding_id, instance, kind, workspace_id, 1, "")
            tx.execute("UPDATE activations SET default_binding_id=? WHERE seq=?", binding.binding_id, activation["seq"])
            tx.bump_state(scope)
            return binding

    def _conversation_binding(self, tx: _Tx, scope: TrustedScope, binding_id: object) -> sqlite3.Row:
        tx.authorized(scope)
        row = tx.one("SELECT * FROM bindings WHERE conversation_id=? AND binding_id=?", scope.conversation_id,
                     binding_id)
        if row is None:
            raise DataError(DataErrorCode.NOT_FOUND, f"no binding {binding_id}")
        return row

    async def detach_workspace(self, scope: TrustedScope, binding_id: BindingId) -> None:
        with self._tx() as tx:
            row = self._conversation_binding(tx, scope, binding_id)
            if not self._registered_binding(tx, row):
                raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, "the conversation's own folder stays attached")
            tx.execute("DELETE FROM bindings WHERE binding_id=?", binding_id)
            instance = row["environment_instance_id"]
            if tx.one("SELECT 1 FROM activations WHERE environment_instance_id=? AND default_binding_id=?",
                      instance, binding_id):
                left = tx.all("SELECT * FROM bindings WHERE conversation_id=? AND environment_instance_id=? ORDER BY seq",
                              scope.conversation_id, instance)
                attached = [r for r in left if self._registered_binding(tx, r)]
                working = attached[-1] if attached else (left[0] if left else None)
                tx.execute("UPDATE activations SET default_binding_id=? WHERE environment_instance_id=?",
                           None if working is None else working["binding_id"], instance)
            tx.bump_state(scope)

    async def set_working_workspace(self, scope: TrustedScope, binding_id: BindingId) -> None:
        with self._tx() as tx:
            row = self._conversation_binding(tx, scope, binding_id)
            tx.execute("UPDATE activations SET default_binding_id=? WHERE environment_instance_id=?", binding_id,
                       row["environment_instance_id"])
            tx.bump_state(scope)

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
        with self._tx() as tx:  # T0: the Conversation and its activations, or nothing
            scope = TrustedScope(principal, ConversationId(tx.new_id("conv")))
            tx.insert_conversation(scope, title)
            for activation in activations:
                tx.activate(scope, activation.environment_kind, activation.workspace_id, None)
            return scope.conversation_id

    _SUMMARY = (
        "SELECT c.conversation_id, c.title, c.created_at, c.trashed_at,"
        " COALESCE((SELECT MAX(sequence) FROM events e WHERE e.conversation_id=c.conversation_id), 0) AS last_sequence,"
        " COALESCE((SELECT created_at FROM events e WHERE e.conversation_id=c.conversation_id"
        " ORDER BY sequence DESC LIMIT 1), c.created_at) AS last_activity,"
        " (SELECT payload FROM events e WHERE e.conversation_id=c.conversation_id AND e.kind='user_message'"
        " ORDER BY sequence LIMIT 1) AS first_user"
        " FROM conversations c"
    )

    @staticmethod
    def _summary(row: sqlite3.Row) -> ConversationSummary:
        first = row["first_user"]
        return ConversationSummary(
            conversation_id=ConversationId(row["conversation_id"]),
            title=row["title"],
            created_at=datetime.fromisoformat(row["created_at"]),
            last_activity_at=datetime.fromisoformat(row["last_activity"]),
            last_sequence=row["last_sequence"],
            preview="" if first is None else rules.preview(
                codec.payload_from(EventKind.USER_MESSAGE, first).blocks),  # type: ignore[union-attr]
            trashed_at=None if row["trashed_at"] is None else datetime.fromisoformat(row["trashed_at"]),
        )

    async def list_conversations(
        self, principal: PrincipalId, *, limit: int = 50, offset: int = 0, trashed: bool = False
    ) -> Sequence[ConversationSummary]:
        rules.check_page(limit, offset)
        where = ("c.trashed_at IS NOT NULL ORDER BY c.trashed_at DESC" if trashed
                 else "c.trashed_at IS NULL ORDER BY last_activity DESC")
        with self._tx() as tx:
            rows = tx.all(f"{self._SUMMARY} WHERE c.owner=? AND {where}, c.rowid DESC LIMIT ? OFFSET ?",
                          principal, limit, offset)
            return tuple(self._summary(r) for r in rows)

    async def read_conversation(self, scope: TrustedScope) -> ConversationSummary:
        with self._tx() as tx:
            tx.authorized(scope)
            row = tx.one(f"{self._SUMMARY} WHERE c.conversation_id=?", scope.conversation_id)
            assert row is not None
            return self._summary(row)

    async def rename_conversation(self, scope: TrustedScope, title: str | None) -> None:
        title = rules.normalize_title(title)
        with self._tx() as tx:
            tx.authorized(scope)
            tx.execute("UPDATE conversations SET title=? WHERE conversation_id=?", title, scope.conversation_id)

    async def trash_conversation(self, scope: TrustedScope) -> datetime:
        with self._tx() as tx:
            if tx.owned(scope)["trashed_at"] is not None:
                raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"{scope.conversation_id} is in the trash")
            now = datetime.now(UTC)
            tx.execute("UPDATE conversations SET trashed_at=? WHERE conversation_id=?", _timestamp(now),
                       scope.conversation_id)
            return now

    async def restore_conversation(self, scope: TrustedScope) -> None:
        with self._tx() as tx:
            conv = tx.owned(scope)
            if conv["trashed_at"] is None or conv["purge_started_at"] is not None:
                raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"{scope.conversation_id} cannot be restored")
            tx.execute("UPDATE conversations SET trashed_at=NULL WHERE conversation_id=?", scope.conversation_id)

    async def begin_purge(self, scope: TrustedScope) -> TrashedConversation:
        with self._tx() as tx:
            conv = tx.owned(scope)
            if conv["trashed_at"] is None:
                raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"{scope.conversation_id} is not in the trash")
            if conv["purge_started_at"] is None:
                tx.execute("UPDATE conversations SET purge_started_at=? WHERE conversation_id=?",
                           _timestamp(datetime.now(UTC)), scope.conversation_id)
            row = tx.one(f"{self._SUMMARY} WHERE c.conversation_id=?", scope.conversation_id)
            assert row is not None
            return TrashedConversation(self._summary(row), _workspace(conv))

    # Every table holding a Conversation's rows, children first (foreign keys
    # check the order). The tests find every table with a conversation_id.
    _PURGE = (
        "DELETE FROM files WHERE conversation_id=?",  # before runs: files.run_id (V3-P66 review C13)
        "DELETE FROM compaction_runs WHERE run_id IN (SELECT run_id FROM runs WHERE conversation_id=?)",
        "DELETE FROM tool_executions WHERE conversation_id=?",
        "DELETE FROM runs WHERE conversation_id=?",
        "DELETE FROM event_text WHERE conversation_id=?",  # before events: its foreign key (V3-P72)
        "DELETE FROM events WHERE conversation_id=?",
        "DELETE FROM idempotency WHERE conversation_id=?",
        "DELETE FROM artifacts WHERE conversation_id=?",
        "DELETE FROM bindings WHERE conversation_id=?",
        "DELETE FROM activations WHERE conversation_id=?",
        "DELETE FROM external_settings WHERE conversation_id=?",
        "DELETE FROM model_settings WHERE conversation_id=?",
        "DELETE FROM memories WHERE source_conversation=?",  # D5: the notes its calls wrote (V3-P71)
        "DELETE FROM conversations WHERE conversation_id=?",
    )

    async def purge_conversation(self, scope: TrustedScope) -> None:
        with self._tx() as tx:
            if tx.owned(scope)["purge_started_at"] is None:
                raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"{scope.conversation_id} is not being deleted")
            digests = [r["ui_digest"] for r in tx.all(
                "SELECT DISTINCT ui_digest FROM tool_executions WHERE conversation_id=? AND ui_digest IS NOT NULL",
                scope.conversation_id)]
            keys = [r["k"] for r in tx.all("SELECT blob_key AS k FROM files WHERE conversation_id=? UNION"
                                           " SELECT view_blob_key FROM files WHERE conversation_id=?"
                                           " AND view_blob_key IS NOT NULL", scope.conversation_id,
                                           scope.conversation_id)]
            for statement in self._PURGE:
                tx.execute(statement, scope.conversation_id)
            for digest in digests:  # a view another call still names stays
                tx.execute("DELETE FROM ui_resources WHERE digest=?"
                           " AND NOT EXISTS (SELECT 1 FROM tool_executions WHERE ui_digest=?)", digest, digest)
        await self.blobs.release(keys)  # after the commit, never failing the purge (V3-P66 §8.1)

    async def used_tool_ids(self, scope: TrustedScope) -> frozenset[ToolId]:
        with self._tx() as tx:
            tx.authorized(scope)
            rows = tx.all("SELECT DISTINCT tool_id FROM tool_executions WHERE conversation_id=?", scope.conversation_id)
            return frozenset(ToolId(r["tool_id"]) for r in rows)

    # --- ConversationRetention --------------------------------------------------

    async def due_purges(self, cutoff: datetime) -> Sequence[TrustedScope]:
        with self._tx() as tx:
            rows = tx.all("SELECT owner, conversation_id FROM conversations WHERE trashed_at IS NOT NULL"
                          " AND (purge_started_at IS NOT NULL OR trashed_at < ?) ORDER BY trashed_at, rowid",
                          _timestamp(cutoff))
            return tuple(TrustedScope(PrincipalId(r["owner"]), ConversationId(r["conversation_id"])) for r in rows)

    # --- FileCatalog (V3-P66) ----------------------------------------------------------

    async def stage_upload(self, scope: TrustedScope, blob: StoredBlob, name: str, media_type: str,
                           image: ImageSetup = NO_IMAGE) -> FileRecord:
        with self._tx() as tx:
            tx.authorized(scope)
            if self._staged(tx, scope) >= MAX_STAGED_UPLOADS:
                raise DataError(DataErrorCode.LIMIT_EXCEEDED, f"{MAX_STAGED_UPLOADS} uploads wait to be sent")
            return self._stage(tx, scope, FileOrigin.UPLOAD, None, blob, name, media_type, image)

    async def stage_tool_file(self, scope: TrustedScope, call: CallRef, blob: StoredBlob, name: str,
                              media_type: str, image: ImageSetup) -> FileRecord:
        with self._tx() as tx:
            tx.authorized(scope)
            self._execution_row(tx, scope, call)  # a call of this Conversation
            return self._stage(tx, scope, FileOrigin.TOOL, call, blob, name, media_type, image)

    @staticmethod
    def _stage(tx: _Tx, scope: TrustedScope, origin: FileOrigin, call: CallRef | None, blob: StoredBlob, name: str,
               media_type: str, image: ImageSetup) -> FileRecord:
        if not _alive(tx, blob) or (image.view is not None and not _alive(tx, image.view.blob)):
            raise DataError(DataErrorCode.BLOB_MISSING, blob.key)
        file_id = tx.new_id("file")
        tx.execute("INSERT INTO files (conversation_id, file_id, version, origin, name, media_type, blob_key,"
                   f" created_at, run_id, call_id, {_IMAGE_COLUMNS}) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?,"
                   " ?, ?, ?, ?, ?, ?, ?, ?)",
                   scope.conversation_id, file_id, origin.value, name, media_type, blob.key,
                   _timestamp(datetime.now(UTC)), None if call is None else call.run_id,
                   None if call is None else call.call_id, *_image_columns(image))
        stored = tx.one(f"SELECT {_FILE_COLUMNS} {_FILE_FROM} WHERE f.file_id=?", file_id)
        assert stored is not None
        return _file(stored)

    @staticmethod
    def _staged(tx: _Tx, scope: TrustedScope) -> int:
        row = tx.one("SELECT count(*) AS n FROM files WHERE conversation_id=? AND attached_at IS NULL AND origin=?",
                     scope.conversation_id, FileOrigin.UPLOAD.value)  # a Tool's images are not the user's (review N9)
        assert row is not None
        return int(row["n"])

    async def count_staged(self, scope: TrustedScope) -> int:
        with self._tx() as tx:
            tx.authorized(scope)
            return self._staged(tx, scope)

    async def set_placement(self, scope: TrustedScope, file_id: FileId, workspace_path: str) -> None:
        with self._tx() as tx:
            tx.authorized(scope)
            row = tx.one("SELECT seq, attached_at FROM files WHERE file_id=? AND conversation_id=? AND origin=?",
                         file_id, scope.conversation_id, FileOrigin.UPLOAD.value)
            if row is None:
                raise DataError(DataErrorCode.NOT_FOUND, file_id)
            if row["attached_at"] is not None:
                raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"{file_id} was already sent")
            tx.execute("UPDATE files SET workspace_path=? WHERE seq=?", workspace_path, row["seq"])

    async def check_attachments(self, scope: TrustedScope, file_ids: Sequence[FileId], *,
                                now: datetime | None = None) -> Sequence[FileRecord]:
        with self._tx() as tx:
            tx.authorized(scope)
            cutoff = (datetime.now(UTC) if now is None else now) - STAGED_SEND_CUTOFF
            return tuple(_file(r) for r in self._check_attachments(tx, scope, file_ids, cutoff=cutoff))

    async def read_files(self, scope: TrustedScope, *, limit: int = 50, offset: int = 0) -> Sequence[FileRecord]:
        with self._tx() as tx:
            tx.authorized(scope)
            rows = tx.all(f"SELECT {_FILE_COLUMNS} {_FILE_FROM} WHERE f.conversation_id=? AND f.attached_at IS NOT NULL"
                          " ORDER BY f.seq DESC LIMIT ? OFFSET ?", scope.conversation_id, limit, offset)
            return tuple(_file(r) for r in rows)

    async def read_file_records(self, scope: TrustedScope, refs: Sequence[tuple[FileId, int]]
                                ) -> Sequence[FileRecord]:
        with self._tx() as tx:
            tx.authorized(scope)
            found: list[FileRecord] = []
            for file_id, version in dict.fromkeys(refs):
                row = tx.one(f"SELECT {_FILE_COLUMNS} {_FILE_FROM} WHERE f.file_id=? AND f.version=?"
                             " AND f.conversation_id=?", file_id, version, scope.conversation_id)
                if row is not None:
                    found.append(_file(row))
            return tuple(found)

    async def read_call_files(self, scope: TrustedScope, calls: Sequence[CallRef]
                              ) -> Mapping[CallRef, Sequence[FileRecord]]:
        with self._tx() as tx:
            tx.authorized(scope)
            found: dict[CallRef, list[FileRecord]] = {}
            for call in dict.fromkeys(calls):
                rows = tx.all(f"SELECT {_FILE_COLUMNS} {_FILE_FROM} WHERE f.conversation_id=? AND f.run_id=?"
                              " AND f.call_id=? AND f.origin=? ORDER BY f.seq", scope.conversation_id, call.run_id,
                              call.call_id, FileOrigin.PRESENTED.value)
                if rows:
                    found[call] = [_file(r) for r in rows]
            return {call: tuple(files) for call, files in found.items()}

    async def presented_bytes_since(self, scope: TrustedScope, since: datetime) -> int:
        with self._tx() as tx:
            tx.authorized(scope)
            return self._presented_since(tx, scope, since)

    # --- FileRetention (V3-P66 §8.2) ------------------------------------------------------

    async def drop_stale_staged(self, cutoff: datetime) -> int:
        with self._tx() as tx:
            return tx.execute("DELETE FROM files WHERE attached_at IS NULL AND created_at < ?",
                              _timestamp(cutoff)).rowcount

    async def sweep_blobs(self, now: datetime) -> None:
        await self.blobs.sweep(now)

    # --- ExternalSourceSettings ------------------------------------------------

    async def read_external_settings(self, scope: TrustedScope) -> Sequence[ExternalSourceSetting]:
        with self._tx() as tx:
            tx.authorized(scope)
            rows = tx.all("SELECT source, enabled FROM external_settings WHERE conversation_id=? ORDER BY source",
                          scope.conversation_id)
            return tuple(ExternalSourceSetting(r["source"], bool(r["enabled"])) for r in rows)

    async def set_external_setting(self, scope: TrustedScope, source: str, enabled: bool | None) -> None:
        with self._tx() as tx:
            tx.authorized(scope)
            rows = {r["source"] for r in tx.all("SELECT source FROM external_settings WHERE conversation_id=?",
                                                 scope.conversation_id)}
            rules.check_external_setting(source, enabled, len(rows) + (enabled is not None and source not in rows))
            if enabled is None:
                tx.execute("DELETE FROM external_settings WHERE conversation_id=? AND source=?",
                           scope.conversation_id, source)
            else:
                tx.execute("INSERT INTO external_settings (conversation_id, source, enabled) VALUES (?, ?, ?)"
                           " ON CONFLICT (conversation_id, source) DO UPDATE SET enabled=excluded.enabled",
                           scope.conversation_id, source, int(enabled))

    # --- ConversationModelSettings ----------------------------------------------

    async def read_model_setting(self, scope: TrustedScope) -> ModelSetting | None:
        with self._tx() as tx:
            tx.authorized(scope)
            row = tx.one("SELECT model_id, reasoning_effort FROM model_settings WHERE conversation_id=?",
                         scope.conversation_id)
            return ModelSetting(row["model_id"], row["reasoning_effort"]) if row is not None else None

    async def set_model_setting(self, scope: TrustedScope, setting: ModelSetting | None) -> None:
        with self._tx() as tx:
            tx.authorized(scope)
            rules.check_model_setting(setting)
            if setting is None:
                tx.execute("DELETE FROM model_settings WHERE conversation_id=?", scope.conversation_id)
            else:
                tx.execute("INSERT INTO model_settings (conversation_id, model_id, reasoning_effort) VALUES (?, ?, ?)"
                           " ON CONFLICT (conversation_id) DO UPDATE SET model_id=excluded.model_id,"
                           " reasoning_effort=excluded.reasoning_effort",
                           scope.conversation_id, setting.model_id, setting.reasoning_effort)

    # --- InstructionsStore (V3-P69) ----------------------------------------------

    async def read_instructions(self, principal: PrincipalId, scope: InstructionScope) -> Instructions:
        rules.check_instruction_scope(scope)
        with self._tx() as tx:
            return _instructions(tx.one("SELECT text, revision, updated_at FROM instructions WHERE owner=? AND scope=?",
                                        principal, scope.value))

    async def write_instructions(self, principal: PrincipalId, scope: InstructionScope, text: str,
                                 expected_revision: int) -> Instructions:
        text = rules.check_instructions(scope, text)
        with self._tx() as tx:
            current = _instructions(tx.one("SELECT text, revision, updated_at FROM instructions"
                                           " WHERE owner=? AND scope=?", principal, scope.value))
            if expected_revision != current.revision:
                raise DataError(DataErrorCode.VERSION_CONFLICT, f"instructions are at revision {current.revision}")
            now = datetime.now(UTC)
            tx.execute("INSERT INTO instructions (owner, scope, text, revision, updated_at) VALUES (?, ?, ?, ?, ?)"
                       " ON CONFLICT (owner, scope) DO UPDATE SET text=excluded.text, revision=excluded.revision,"
                       " updated_at=excluded.updated_at",
                       principal, scope.value, text, current.revision + 1, _timestamp(now))
            return Instructions(text, current.revision + 1, now)

    # --- MemoryStore (V3-P71) ---------------------------------------------------------

    _MEMORY_COLUMNS = ("memory_id, category, text, origin, source_conversation, source_run, source_call, revision,"
                       " created_at, updated_at")

    def _read_memory(self, tx: _Tx, principal: PrincipalId, memory_id: object) -> Memory:
        row = None if not isinstance(memory_id, int) else tx.one(
            f"SELECT {self._MEMORY_COLUMNS} FROM memories WHERE owner=? AND memory_id=?", principal, memory_id)
        if row is None:
            raise DataError(DataErrorCode.NOT_FOUND, f"no note m{memory_id}")
        return _memory(row)

    @staticmethod
    def _check_room(tx: _Tx, principal: PrincipalId) -> None:
        row = tx.one("SELECT COUNT(*) AS n FROM memories WHERE owner=?", principal)
        assert row is not None
        if row["n"] >= MAX_MEMORIES:
            raise DataError(DataErrorCode.LIMIT_EXCEEDED, f"{MAX_MEMORIES} notes are kept at most")

    @staticmethod
    def _last_memory_id(tx: _Tx, principal: PrincipalId) -> int:
        row = tx.one("SELECT last_id FROM memory_ids WHERE owner=?", principal)
        return 0 if row is None else int(row["last_id"])

    def _next_memory_id(self, tx: _Tx, principal: PrincipalId) -> int:
        next_id = self._last_memory_id(tx, principal) + 1
        tx.execute("INSERT INTO memory_ids (owner, last_id) VALUES (?, ?)"
                   " ON CONFLICT (owner) DO UPDATE SET last_id=excluded.last_id", principal, next_id)
        return next_id

    @staticmethod
    def _write_memory(tx: _Tx, principal: PrincipalId, note: Memory) -> None:
        call = note.source_call
        tx.execute("INSERT INTO memories (owner, memory_id, category, text, origin, source_conversation, source_run,"
                   " source_call, revision, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                   " ON CONFLICT (owner, memory_id) DO UPDATE SET category=excluded.category, text=excluded.text,"
                   " origin=excluded.origin, source_conversation=excluded.source_conversation,"
                   " source_run=excluded.source_run, source_call=excluded.source_call, revision=excluded.revision,"
                   " updated_at=excluded.updated_at",
                   principal, note.memory_id, note.category.value, note.text, note.origin.value,
                   note.source_conversation, None if call is None else call.run_id,
                   None if call is None else call.call_id, note.revision, _timestamp(note.created_at),
                   _timestamp(note.updated_at))

    def _apply_memory(self, tx: _Tx, scope: TrustedScope, call: CallRef, change: MemoryChange) -> None:
        """A Tool's change, in the commit's transaction: its errors roll the commit back."""
        change = rules.check_memory_change(change)
        principal, now = scope.principal_id, datetime.now(UTC)
        if change.kind is MemoryChangeKind.SAVE and change.expected_revision is None:
            exists = tx.one("SELECT 1 FROM memories WHERE owner=? AND memory_id=?", principal, change.memory_id)
            if change.memory_id > self._last_memory_id(tx, principal) or exists is not None:
                raise DataError(DataErrorCode.INVALID_ARGUMENT, f"m{change.memory_id} was not given out for a new note")
            self._check_room(tx, principal)
            assert change.category is not None and change.text is not None
            self._write_memory(tx, principal, Memory(change.memory_id, change.category, change.text,
                                                     MemoryOrigin.MODEL, scope.conversation_id, call, 1, now, now))
            return
        note = self._read_memory(tx, principal, change.memory_id)
        if change.expected_revision != note.revision:
            raise DataError(DataErrorCode.VERSION_CONFLICT, f"m{note.memory_id} is at revision {note.revision}")
        if change.kind is MemoryChangeKind.FORGET:
            tx.execute("DELETE FROM memories WHERE owner=? AND memory_id=?", principal, note.memory_id)
            return
        assert change.category is not None and change.text is not None
        self._write_memory(tx, principal, dataclasses.replace(
            note, category=change.category, text=change.text, origin=MemoryOrigin.MODEL,
            source_conversation=scope.conversation_id, source_call=call, revision=note.revision + 1, updated_at=now))

    async def list_memories(self, principal: PrincipalId) -> tuple[Memory, ...]:
        with self._tx() as tx:
            rows = tx.all(f"SELECT {self._MEMORY_COLUMNS} FROM memories WHERE owner=?", principal)
            return rules.order_memories(_memory(r) for r in rows)

    async def read_memory(self, principal: PrincipalId, memory_id: int) -> Memory:
        with self._tx() as tx:
            return self._read_memory(tx, principal, memory_id)

    async def allocate_memory_id(self, principal: PrincipalId) -> int:
        with self._tx() as tx:
            return self._next_memory_id(tx, principal)

    async def save_memory(self, principal: PrincipalId, category: MemoryCategory, text: str, *,
                          memory_id: int | None = None, expected_revision: int | None = None) -> Memory:
        category, text, now = rules.check_memory_category(category), rules.check_memory_text(text), datetime.now(UTC)
        with self._tx() as tx:
            if memory_id is None:
                self._check_room(tx, principal)
                note = Memory(self._next_memory_id(tx, principal), category, text, MemoryOrigin.USER, None, None, 1,
                              now, now)
            else:
                current = self._read_memory(tx, principal, memory_id)
                if expected_revision != current.revision:
                    raise DataError(DataErrorCode.VERSION_CONFLICT, f"m{memory_id} is at revision {current.revision}")
                note = dataclasses.replace(current, category=category, text=text, origin=MemoryOrigin.USER,
                               revision=current.revision + 1, updated_at=now)
            self._write_memory(tx, principal, note)
            return note

    async def delete_memory(self, principal: PrincipalId, memory_id: int,
                            expected_revision: int | None = None) -> Memory:
        with self._tx() as tx:
            note = self._read_memory(tx, principal, memory_id)
            if expected_revision is not None and expected_revision != note.revision:
                raise DataError(DataErrorCode.VERSION_CONFLICT, f"m{memory_id} is at revision {note.revision}")
            tx.execute("DELETE FROM memories WHERE owner=? AND memory_id=?", principal, memory_id)
            return note

    async def restore_memory(self, principal: PrincipalId, before: Memory, *, expected_revision: int) -> Memory:
        category, text = rules.check_memory_category(before.category), rules.check_memory_text(before.text)
        source = before.source_conversation
        with self._tx() as tx:
            current = self._read_memory(tx, principal, before.memory_id)
            if expected_revision != current.revision:
                raise DataError(DataErrorCode.VERSION_CONFLICT, f"m{current.memory_id} is at revision {current.revision}")
            if source is not None and tx.one("SELECT 1 FROM conversations WHERE conversation_id=? AND owner=?",
                                             source, principal) is None:
                raise DataError(DataErrorCode.NOT_FOUND, "the conversation it came from was deleted")
            note = dataclasses.replace(current, category=category, text=text, origin=before.origin,
                                       source_conversation=source,
                                       source_call=None if source is None else before.source_call,
                                       revision=current.revision + 1, updated_at=datetime.now(UTC))
            self._write_memory(tx, principal, note)
            return note

    async def count_source_memories(self, scope: TrustedScope) -> int:
        with self._tx() as tx:
            tx.owned(scope)
            row = tx.one("SELECT COUNT(*) AS n FROM memories WHERE owner=? AND source_conversation=?",
                         scope.principal_id, scope.conversation_id)
            assert row is not None
            return int(row["n"])

    # --- ConversationSearch (V3-P72) --------------------------------------------------

    async def search_events(self, principal: PrincipalId, words: Sequence[str], *, current: ConversationId,
                            current_through: int | None, conversation: ConversationId | None = None,
                            per_conversation: int, limit: int) -> EventSearch:
        folded = rules.search_words(words)
        score = " + ".join(["(instr(t.folded, ?) > 0)"] * len(folded))
        only = "" if conversation is None else " AND t.conversation_id=?"
        # The order and limits of rules.pick_hits, in SQL: only the hits leave
        # the database, however many Events match (PR #79 review B2).
        # created_at is fixed-width UTC text, so its text order is time order.
        order = "matched DESC, created_at DESC, sequence DESC"
        with self._tx() as tx:
            if conversation is not None:
                tx.authorized(TrustedScope(principal, conversation))
            rows = tx.all(
                "WITH found AS ("
                f" SELECT t.conversation_id, t.sequence, e.kind, e.created_at, ({score}) AS matched"
                " FROM event_text t JOIN events e USING (conversation_id, sequence)"
                " JOIN conversations c ON c.conversation_id=t.conversation_id"
                " WHERE c.owner=? AND c.trashed_at IS NULL AND (t.conversation_id<>? OR t.sequence<=?)"
                f"{only}), ranked AS ("
                f" SELECT *, ROW_NUMBER() OVER (PARTITION BY conversation_id ORDER BY {order}) AS nth,"
                " COUNT(*) OVER () AS total FROM found WHERE matched > 0)"
                f" SELECT * FROM ranked WHERE nth <= ? ORDER BY {order} LIMIT ?",
                *folded, principal, current, -1 if current_through is None else current_through,
                *(() if conversation is None else (conversation,)), per_conversation, limit)
            total = rows[0]["total"] if rows else 0
            hits = tuple(EventText(ConversationId(r["conversation_id"]), r["sequence"], EventKind(r["kind"]),
                                   datetime.fromisoformat(r["created_at"]),
                                   self._event_text(tx, ConversationId(r["conversation_id"]), r["sequence"]),
                                   r["matched"]) for r in rows)
            ids = list(dict.fromkeys(h.conversation_id for h in hits))
            summaries = {r["conversation_id"]: self._summary(r) for r in tx.all(
                f"{self._SUMMARY} WHERE c.conversation_id IN ({', '.join('?' * len(ids))})", *ids)} if ids else {}
            return EventSearch(hits, total, tuple(summaries[cid] for cid in ids))

    @staticmethod
    def _event_text(tx: _Tx, conversation: ConversationId, sequence: int) -> str:
        row = tx.one("SELECT kind, payload FROM events WHERE conversation_id=? AND sequence=?", conversation, sequence)
        assert row is not None
        return rules.event_text(codec.payload_from(EventKind(row["kind"]), row["payload"])) or ""

    async def read_event_texts(self, principal: PrincipalId, conversation: ConversationId, *, from_sequence: int,
                               current: ConversationId, current_through: int | None, limit: int) -> EventPage:
        scope = TrustedScope(principal, conversation)
        with self._tx() as tx:
            tx.authorized(scope)
            last = rules.check_read(conversation, from_sequence, current, current_through)
            rows = tx.all(
                "SELECT e.sequence, e.kind, e.created_at, e.payload FROM events e"
                " JOIN event_text t USING (conversation_id, sequence)"
                " WHERE e.conversation_id=? AND e.sequence>=? AND (? < 0 OR e.sequence<=?)"
                " ORDER BY e.sequence LIMIT ?", conversation, from_sequence, last, last, limit + 1)
            items = tuple(EventText(conversation, r["sequence"], EventKind(r["kind"]),
                                    datetime.fromisoformat(r["created_at"]),
                                    rules.event_text(codec.payload_from(EventKind(r["kind"]), r["payload"])) or "")
                          for r in rows[:limit])
            summary = tx.one(f"{self._SUMMARY} WHERE c.conversation_id=?", conversation)
            assert summary is not None
            return EventPage(self._summary(summary), items, len(rows) > limit)

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
