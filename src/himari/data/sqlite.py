"""SQLite Data implementation of all P0 sub-ports (Q-41).

Durability: one database file in WAL mode with ``synchronous=FULL``, so a
committed transaction survives a process crash (and, given an honest disk,
power loss). Every port call is exactly one ``BEGIN IMMEDIATE … COMMIT``
transaction that validates first and writes last; any error — including
a crash between statements — leaves no partial T1–T9/T-R effect.

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
import json
import fcntl
import logging
import os
import sqlite3
import stat
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime

from himari.common import (
    AttachmentRef,
    CallId,
    CallRef,
    ConversationId,
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
)
from himari.data import codec, rules
from himari.data.blobs import BlobRow, DirectoryBlobFiles, FileStore
from himari.data.api import (
    AsleepAnswer,
    DayBefore,
    DayState,
    FellBy,
    PlanChange,
    PlanKind,
    PlanOp,
    PlanState,
    PlanWrite,
    SleepRecord,
    WokeBy,
    ALREADY_SENT,
    SEARCH_KINDS,
    EventPage,
    EventSearch,
    EventText,
    MAX_ATTACHMENTS,
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
    NO_IMAGE,
    FileOrigin,
    FileRecord,
    ImageSetup,
    ImageStatus,
    ImageView,
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
    ThoughtAbout,
    ThoughtChange,
    ThoughtOp,
    LifeJob,
    MAX_EVENTS_PER_DAY,
    WorldEvent,
    WorkDraft,
    LifeEntry,
    LifeKind,
    LifeOrigin,
    LifePlan,
    HimariMood,
    MoodSource,
    LifeWrite,
    LifeWriteStart,
    LifeWriteStatus,
    WorkPiece,
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
)

# v3: compaction_runs; v4: external_settings; v5: ui_resources; v6: model_settings;
# v7: the trash (all upgraded in place); v1 is refused
SCHEMA_VERSION = 29
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
    Wake: EventKind.WAKE,
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
# Conversation; a call stored with its view named it and kept its payload.
# Views are no longer stored or read: the table and columns stay, with
# whatever an earlier version put in them.
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
# v14: a principal's devices (V3-P83 §4.2): hashes of their token and pairing
# code only. Revoked rows stay so their ids are never given out again.
_V14_ADDITIONS = """
CREATE TABLE devices (
    owner TEXT NOT NULL,
    device_id TEXT NOT NULL,
    name TEXT NOT NULL,
    token_hash BLOB,
    pairing_hash BLOB,
    pairing_expires_at TEXT,
    pairing_failures INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    paired_at TEXT,
    last_seen_at TEXT,
    revoked_at TEXT,
    PRIMARY KEY (owner, device_id)
) STRICT;
CREATE UNIQUE INDEX devices_by_token ON devices (token_hash) WHERE token_hash IS NOT NULL;
CREATE INDEX devices_by_pairing ON devices (pairing_hash) WHERE pairing_hash IS NOT NULL;
CREATE TABLE device_ids (
    owner TEXT PRIMARY KEY,
    last_id INTEGER NOT NULL
) STRICT;
"""
# v15: memory passes (V3-P85 §11): a note's dates, source pass, retirement and the
# user's keep; episodes; each Conversation's reading position; the passes, their
# calls' raw records and their items. An upgrade puts every Conversation's cursor
# at its last Event (_fill_memory_cursors), so nothing old is read at once.
_V15_ADDITIONS = """
ALTER TABLE memories ADD COLUMN due_at TEXT;
ALTER TABLE memories ADD COLUMN due_instant TEXT;
ALTER TABLE memories ADD COLUMN valid_until TEXT;
ALTER TABLE memories ADD COLUMN valid_instant TEXT;
ALTER TABLE memories ADD COLUMN source_pass TEXT;
ALTER TABLE memories ADD COLUMN source_item INTEGER;
ALTER TABLE memories ADD COLUMN retired_at TEXT;
ALTER TABLE memories ADD COLUMN retired_pass TEXT;
ALTER TABLE memories ADD COLUMN retired_item INTEGER;
ALTER TABLE memories ADD COLUMN user_approved_at TEXT;
ALTER TABLE memory_ids ADD COLUMN last_episode_id INTEGER NOT NULL DEFAULT 0;
CREATE TABLE episodes (
    owner TEXT NOT NULL,
    episode_id INTEGER NOT NULL,
    conversation_id TEXT NOT NULL REFERENCES conversations,
    from_sequence INTEGER NOT NULL,
    through_sequence INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    text TEXT NOT NULL,
    folded TEXT NOT NULL,
    user_sequences TEXT NOT NULL,
    himari_sequences TEXT NOT NULL,
    origin TEXT NOT NULL,
    source_pass TEXT,
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (owner, episode_id)
) STRICT;
CREATE INDEX episodes_by_time ON episodes (owner, ended_at, episode_id);
CREATE INDEX episodes_by_conversation ON episodes (conversation_id);
CREATE TABLE memory_cursors (
    conversation_id TEXT PRIMARY KEY REFERENCES conversations,
    through_sequence INTEGER NOT NULL,
    failures INTEGER NOT NULL,
    retry_after TEXT,
    baseline_sequence INTEGER NOT NULL,
    backfill_through INTEGER NOT NULL
) STRICT;
CREATE TABLE memory_passes (
    seq INTEGER PRIMARY KEY,
    pass_id TEXT NOT NULL UNIQUE,
    owner TEXT NOT NULL,
    conversation_id TEXT NOT NULL REFERENCES conversations,
    kind TEXT NOT NULL,
    from_sequence INTEGER NOT NULL,
    through_sequence INTEGER NOT NULL,
    snapshot_sequence INTEGER NOT NULL,
    retry_of TEXT,
    status TEXT NOT NULL,
    provider_key TEXT NOT NULL,
    model_id TEXT NOT NULL,
    calls INTEGER NOT NULL,
    reserved_tokens INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    unmetered_tokens INTEGER NOT NULL,
    over_estimate INTEGER NOT NULL,
    omitted_chars INTEGER NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT
) STRICT;
CREATE INDEX memory_passes_by_conversation ON memory_passes (conversation_id, from_sequence);
CREATE INDEX memory_passes_by_owner ON memory_passes (owner, created_at);
CREATE TABLE memory_pass_calls (
    pass_id TEXT NOT NULL REFERENCES memory_passes (pass_id),
    call INTEGER NOT NULL,
    status TEXT NOT NULL,
    reserved_tokens INTEGER NOT NULL,
    prepared_at TEXT NOT NULL,
    charged_tokens INTEGER NOT NULL,
    raw_request_body BLOB NOT NULL,
    raw_request_content_type TEXT NOT NULL,
    raw_response_body BLOB,
    raw_response_content_type TEXT NOT NULL DEFAULT '',
    usage TEXT,
    error_code TEXT,
    PRIMARY KEY (pass_id, call)
) STRICT;
CREATE TABLE memory_pass_items (
    pass_id TEXT NOT NULL REFERENCES memory_passes (pass_id),
    item INTEGER NOT NULL,
    proposal TEXT NOT NULL,
    planned TEXT,
    outcome TEXT NOT NULL,
    target TEXT,
    previous TEXT,
    written_revision INTEGER,
    decided_at TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (pass_id, item)
) STRICT;
CREATE INDEX memory_pass_items_by_outcome ON memory_pass_items (outcome, created_at);
"""
# v16: a principal's "quiet" (V3-P88 §6.2): until when HIMARI does not speak first.
_V16_ADDITIONS = """
CREATE TABLE presence_settings (
    owner TEXT PRIMARY KEY,
    quiet_until TEXT
) STRICT;
"""
# v17: server-opened turns (V3-P87 §3.3): the wakes and the keys that keep them from
# repeating; a reminder's delivery on its loop (§5.3); each device's last input (§5.2).
_V17_ADDITIONS = """
ALTER TABLE memories ADD COLUMN remind_at TEXT;
ALTER TABLE memories ADD COLUMN remind_instant TEXT;
ALTER TABLE memories ADD COLUMN remind_state TEXT;
ALTER TABLE memories ADD COLUMN remind_wake TEXT;
ALTER TABLE memories ADD COLUMN remind_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE memories ADD COLUMN reminded_at TEXT;
ALTER TABLE devices ADD COLUMN last_input_at TEXT;
CREATE TABLE wakes (
    wake_id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    conversation_id TEXT NOT NULL REFERENCES conversations,
    event_sequence INTEGER NOT NULL,
    reasons TEXT NOT NULL,
    device_id TEXT NOT NULL,
    status TEXT NOT NULL,
    asked INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    ended_at TEXT
) STRICT;
CREATE INDEX wakes_by_owner ON wakes (owner, created_at);
CREATE INDEX wakes_opened ON wakes (status) WHERE status = 'opened';
CREATE TABLE wake_keys (
    owner TEXT NOT NULL,
    key TEXT NOT NULL,
    wake_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (owner, key)
) STRICT;
"""
# v18: the Run whose screen-Tool group a free wake ran unshown (V3-P91 §8), hidden with it.
_V18_ADDITIONS = """
ALTER TABLE wakes ADD COLUMN held_run_id TEXT;
"""
# v19 (V3-P107 §2): no reminder is ``told`` any more — one told to a sleeping device was said in
# its Conversation, so it counts as delivered. Data only: the tables stay as they are.
_V19_ADDITIONS = """
UPDATE memories SET remind_state='delivered', remind_wake=NULL WHERE remind_state='told';
"""
# v20 (V3-P108 §5): the companion personas a principal keeps, the last id given out to each and the
# one chosen. The one persona text of V3-P89 (instructions, scope 'companion_persona') becomes
# persona 1, chosen; its rows go (instructions store "" for a text that is only whitespace).
_V20_ADDITIONS = """
CREATE TABLE personas (
    owner TEXT NOT NULL,
    persona_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    name_key TEXT NOT NULL,
    text TEXT NOT NULL,
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (owner, persona_id)
) STRICT;
CREATE UNIQUE INDEX personas_by_name ON personas (owner, name_key);
CREATE TABLE persona_ids (
    owner TEXT PRIMARY KEY,
    last_id INTEGER NOT NULL
) STRICT;
CREATE TABLE persona_choice (
    owner TEXT PRIMARY KEY,
    persona_id INTEGER,
    revision INTEGER NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;
INSERT INTO personas (owner, persona_id, name, name_key, text, revision, created_at, updated_at)
    SELECT owner, 1, 'Persona', 'persona', text, 1, updated_at, updated_at FROM instructions
    WHERE scope='companion_persona' AND text <> '';
INSERT INTO persona_ids (owner, last_id) SELECT owner, 1 FROM personas;
INSERT INTO persona_choice (owner, persona_id, revision, updated_at) SELECT owner, 1, 1, updated_at FROM personas;
DELETE FROM instructions WHERE scope='companion_persona';
"""
# v21 (2026-11-24 cleanup): what is left of the work-agent era goes. A Conversation keeps only the
# Environment kinds it has (``environments``, from ``activations``); Bindings, registered Workspaces,
# Artifacts and the columns naming them are dropped. The machine folder of a Conversation is always
# ``conversations/<id>`` (V3-P109), so nothing it needs is lost.
_V21_ADDITIONS = """
CREATE TABLE environments (
    conversation_id TEXT NOT NULL REFERENCES conversations,
    environment_kind TEXT NOT NULL,
    PRIMARY KEY (conversation_id, environment_kind)
) STRICT;
INSERT INTO environments (conversation_id, environment_kind)
    SELECT DISTINCT conversation_id, environment_kind FROM activations;
DROP TABLE activations;
DROP TABLE bindings;
DROP TABLE workspaces;
DROP TABLE artifacts;
ALTER TABLE conversations DROP COLUMN workspace_id;
ALTER TABLE conversations DROP COLUMN workspace_cwd;
ALTER TABLE tool_executions DROP COLUMN workspace_binding_id;
"""
# v22 (character-life v0.1 §2): HIMARI's life per persona (facts, diaries, plans; a retired one stays
# with ``retired_at``), the last entry id given out to each principal, and the story writer's calls.
_V22_ADDITIONS = """
CREATE TABLE life_entries (
    owner TEXT NOT NULL,
    entry_id INTEGER NOT NULL,
    persona_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    day TEXT,
    text TEXT NOT NULL,
    origin TEXT NOT NULL,
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    retired_at TEXT,
    source_conversation TEXT,
    source_sequences TEXT NOT NULL,
    source TEXT,
    PRIMARY KEY (owner, entry_id)
) STRICT;
CREATE INDEX life_by_persona ON life_entries (owner, persona_id, kind, day);
CREATE TABLE life_ids (
    owner TEXT PRIMARY KEY,
    last_id INTEGER NOT NULL
) STRICT;
CREATE TABLE life_writes (
    seq INTEGER PRIMARY KEY,
    write_id TEXT NOT NULL UNIQUE,
    owner TEXT NOT NULL,
    persona_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    status TEXT NOT NULL,
    provider_key TEXT NOT NULL,
    model_id TEXT NOT NULL,
    reserved_tokens INTEGER NOT NULL,
    charged_tokens INTEGER NOT NULL,
    usage TEXT,
    raw_request_body BLOB NOT NULL,
    raw_request_content_type TEXT NOT NULL,
    raw_response_body BLOB,
    raw_response_content_type TEXT NOT NULL DEFAULT '',
    error TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT
) STRICT;
CREATE INDEX life_writes_by_owner ON life_writes (owner, created_at);
"""
# v23 (character-bond v0.1 §2): a bond entry is a life entry of kind 'bond' with its tag and (a date) its
# month and day; each persona's stage with its owner, when and why it last moved (no row: new).
_V23_ADDITIONS = """
ALTER TABLE life_entries ADD COLUMN tag TEXT;
ALTER TABLE life_entries ADD COLUMN month_day TEXT;
CREATE TABLE bond_stages (
    owner TEXT NOT NULL,
    persona_id INTEGER NOT NULL,
    stage TEXT NOT NULL,
    since TEXT NOT NULL,
    reason TEXT NOT NULL,
    revision INTEGER NOT NULL,
    PRIMARY KEY (owner, persona_id)
) STRICT;
"""
# v24 (character-state v0.1 §2, §3): a plan's start time (HH:MM), and each persona's mood with its owner,
# when it was set, by what (reply, writer) and the writer's why (no row: no mood).
_V24_ADDITIONS = """
ALTER TABLE life_entries ADD COLUMN time_of_day TEXT;
CREATE TABLE himari_moods (
    owner TEXT NOT NULL,
    persona_id INTEGER NOT NULL,
    mood TEXT NOT NULL,
    set_at TEXT NOT NULL,
    source TEXT NOT NULL,
    reason TEXT,
    PRIMARY KEY (owner, persona_id)
) STRICT;
"""
# v25 (character-world v0.1 §4): an arc is a life entry of kind 'arc' (its goal the text, its start the day)
# with its title and, once closed, its last day and ending; a diary names the arc its day moved.
_V25_ADDITIONS = """
ALTER TABLE life_entries ADD COLUMN title TEXT;
ALTER TABLE life_entries ADD COLUMN end_day TEXT;
ALTER TABLE life_entries ADD COLUMN ending TEXT;
ALTER TABLE life_entries ADD COLUMN arc_id INTEGER;
"""
# v26 (character-work v0.1 §3, §4): a work piece is a life entry of kind 'work' with its summary and plan (its
# project the arc_id, as a work plan's); an arc keeps its "so far"; a worker call is a life write with its plan.
_V26_ADDITIONS = """
ALTER TABLE life_entries ADD COLUMN summary TEXT;
ALTER TABLE life_entries ADD COLUMN plan_id INTEGER;
ALTER TABLE life_entries ADD COLUMN so_far TEXT;
ALTER TABLE life_writes ADD COLUMN plan_id INTEGER;
"""
# v27 (character-persona-fields §3): a persona keeps its fields (a JSON object); its text is made from them when it
# is saved. A persona from before is all one field, "free", so the text the model is told stays the same.
_V27_ADDITIONS = """
ALTER TABLE personas ADD COLUMN fields TEXT NOT NULL DEFAULT '{}';
UPDATE personas SET fields=json_object('free', text) WHERE text <> '';
"""
# v28 (character-mind v0.1 §14.2): a kept thought is a life entry of kind 'thought' with what it is about and when it
# expires (its conversation and answers the source columns); a work piece may be a draft; a life write names its job
# (from before: a worker's call 'work_end', else 'review'), why its model was chosen, and the thought it began with.
_V28_ADDITIONS = """
ALTER TABLE life_entries ADD COLUMN about TEXT;
ALTER TABLE life_entries ADD COLUMN expires_at TEXT;
ALTER TABLE life_entries ADD COLUMN draft INTEGER NOT NULL DEFAULT 0;
ALTER TABLE life_writes ADD COLUMN job TEXT NOT NULL DEFAULT 'review';
ALTER TABLE life_writes ADD COLUMN model_reason TEXT;
ALTER TABLE life_writes ADD COLUMN thought TEXT;
ALTER TABLE wakes ADD COLUMN thought TEXT;
UPDATE life_writes SET job='work_end' WHERE plan_id IS NOT NULL;
"""
# v29 (character-day v0.1 §18.2): a plan is a slot of HIMARI's schedule — its length, kind, state and why it last
# moved (from before: an hour, work with a project, else its own); a conversation may be its principal's day session
# (the date it opened, when it closed; one open at a time); HIMARI's sleeps and the day keeper's small state; a life
# write names the day session it was written in.
_V29_ADDITIONS = """
ALTER TABLE life_entries ADD COLUMN minutes INTEGER;
ALTER TABLE life_entries ADD COLUMN plan_kind TEXT;
ALTER TABLE life_entries ADD COLUMN plan_state TEXT;
ALTER TABLE life_entries ADD COLUMN reason TEXT;
UPDATE life_entries SET minutes=60, plan_kind=CASE WHEN arc_id IS NULL THEN 'own' ELSE 'work' END
    WHERE kind='plan';
ALTER TABLE conversations ADD COLUMN day_session TEXT;
ALTER TABLE conversations ADD COLUMN closed_at TEXT;
CREATE UNIQUE INDEX conversations_open_day ON conversations (owner)
    WHERE day_session IS NOT NULL AND closed_at IS NULL;
CREATE TABLE sleeps (
    sleep_id INTEGER PRIMARY KEY,
    owner TEXT NOT NULL,
    persona_id INTEGER NOT NULL,
    fell_at TEXT NOT NULL,
    how_fell TEXT NOT NULL,
    woke_at TEXT,
    how_woke TEXT,
    messages INTEGER NOT NULL DEFAULT 0,
    mumbles INTEGER NOT NULL DEFAULT 0,
    count INTEGER NOT NULL DEFAULT 0,
    count_at TEXT,
    closed_day INTEGER NOT NULL DEFAULT 0,
    first_message_at TEXT,
    last_message_at TEXT
) STRICT;
CREATE INDEX sleeps_by_owner ON sleeps (owner, persona_id, sleep_id);
CREATE UNIQUE INDEX sleeps_open ON sleeps (owner, persona_id) WHERE woke_at IS NULL;
CREATE TABLE day_state (
    owner TEXT NOT NULL,
    persona_id INTEGER NOT NULL,
    sleep_requested_at TEXT,
    woken_at TEXT,
    last_closing_at TEXT,
    before TEXT,
    PRIMARY KEY (owner, persona_id)
) STRICT;
ALTER TABLE life_writes ADD COLUMN conversation_id TEXT;
"""
_ADDITIONS = (_V3_ADDITIONS, _V4_ADDITIONS, _V5_ADDITIONS, _V6_ADDITIONS, _V7_ADDITIONS, _V8_ADDITIONS,
              _V9_ADDITIONS, _V10_ADDITIONS, _V11_ADDITIONS, _V12_ADDITIONS,
              _V13_ADDITIONS, _V14_ADDITIONS, _V15_ADDITIONS, _V16_ADDITIONS, _V17_ADDITIONS,
              _V18_ADDITIONS, _V19_ADDITIONS, _V20_ADDITIONS, _V21_ADDITIONS,
              _V22_ADDITIONS, _V23_ADDITIONS, _V24_ADDITIONS, _V25_ADDITIONS,
              _V26_ADDITIONS, _V27_ADDITIONS, _V28_ADDITIONS, _V29_ADDITIONS)  # what each version from v3 added
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
                if version < 15:
                    _fill_memory_cursors(tx)
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

    def activate_environment(self, scope: TrustedScope, kind: EnvironmentKind) -> None:
        with self.transaction() as tx:
            tx.authorized(scope)
            tx.add_environment(scope, kind)
            tx.bump_state(scope)

    def deactivate_environment(self, scope: TrustedScope, kind: EnvironmentKind) -> None:
        """Takes the kind away; its places (the folder) remain."""
        with self.transaction() as tx:
            tx.authorized(scope)
            tx.execute("DELETE FROM environments WHERE conversation_id=? AND environment_kind=?", scope.conversation_id, kind)
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

    def add_environment(self, scope: TrustedScope, kind: EnvironmentKind) -> None:
        if self.one("SELECT 1 FROM environments WHERE conversation_id=? AND environment_kind=?",
                    scope.conversation_id, kind):
            raise DataError(DataErrorCode.DUPLICATE_IDENTITY, f"{kind} is already there")
        self.execute("INSERT INTO environments (conversation_id, environment_kind) VALUES (?, ?)",
                     scope.conversation_id, kind)

    def environments(self, scope: TrustedScope) -> tuple[EnvironmentKind, ...]:
        return tuple(EnvironmentKind(r["environment_kind"]) for r in self.all(
            "SELECT environment_kind FROM environments WHERE conversation_id=? ORDER BY environment_kind",
            scope.conversation_id))


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


def _persona(row: sqlite3.Row) -> Persona:
    return Persona(row["persona_id"], row["name"], row["text"], row["revision"],
                   datetime.fromisoformat(row["created_at"]), datetime.fromisoformat(row["updated_at"]),
                   rules.persona_fields_from_json(row["fields"]))


def _moment(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _life_entry(row: sqlite3.Row) -> LifeEntry:
    source = None if row["source_conversation"] is None else ConversationId(row["source_conversation"])
    return LifeEntry(row["entry_id"], row["persona_id"], LifeKind(row["kind"]), row["day"], row["text"],
                     LifeOrigin(row["origin"]), row["revision"], datetime.fromisoformat(row["created_at"]),
                     datetime.fromisoformat(row["updated_at"]), source, tuple(json.loads(row["source_sequences"])),
                     row["source"], None if row["tag"] is None else BondTag(row["tag"]), row["month_day"],
                     row["time_of_day"], row["title"], row["end_day"], row["ending"], row["arc_id"], row["summary"],
                     row["plan_id"], row["so_far"], None if row["about"] is None else ThoughtAbout(row["about"]),
                     _moment(row["expires_at"]), bool(row["draft"]), row["minutes"],
                     None if row["plan_kind"] is None else PlanKind(row["plan_kind"]),
                     None if row["plan_state"] is None else PlanState(row["plan_state"]), row["reason"])


def _sleep(row: sqlite3.Row) -> SleepRecord:
    return SleepRecord(int(row["sleep_id"]), int(row["persona_id"]), datetime.fromisoformat(row["fell_at"]),
                       FellBy(row["how_fell"]), _moment(row["woke_at"]),
                       None if row["how_woke"] is None else WokeBy(row["how_woke"]), int(row["messages"]),
                       int(row["mumbles"]), int(row["count"]), _moment(row["count_at"]), bool(row["closed_day"]),
                       _moment(row["first_message_at"]), _moment(row["last_message_at"]))


def _mood(persona_id: int, row: sqlite3.Row | None) -> HimariMood | None:
    if row is None:
        return None
    return HimariMood(persona_id, row["mood"], datetime.fromisoformat(row["set_at"]), MoodSource(row["source"]),
                      row["reason"])


def _standing(persona_id: int, row: sqlite3.Row | None) -> BondStanding:
    if row is None:
        return BondStanding(persona_id)
    return BondStanding(persona_id, BondStage(row["stage"]), datetime.fromisoformat(row["since"]), row["reason"],
                        row["revision"])


def _life_write(row: sqlite3.Row) -> LifeWrite:
    usage = None if row["usage"] is None else json.loads(row["usage"])
    return LifeWrite(row["write_id"], row["persona_id"], row["day"], LifeWriteStatus(row["status"]),
                     row["provider_key"], row["model_id"], row["reserved_tokens"], row["charged_tokens"],
                     None if usage is None else usage[0], None if usage is None else usage[1], row["error"],
                     datetime.fromisoformat(row["created_at"]), _moment(row["finished_at"]), row["plan_id"],
                     LifeJob(row["job"]), row["model_reason"], row["thought"],
                     None if row["conversation_id"] is None else ConversationId(row["conversation_id"]))


def _memory(row: sqlite3.Row) -> Memory:
    call = None if row["source_call"] is None else CallRef(RunId(row["source_run"]), CallId(row["source_call"]))
    source = None if row["source_conversation"] is None else ConversationId(row["source_conversation"])
    return Memory(row["memory_id"], MemoryCategory(row["category"]), row["text"], MemoryOrigin(row["origin"]),
                  source, call, row["revision"], datetime.fromisoformat(row["created_at"]),
                  datetime.fromisoformat(row["updated_at"]), due_at=row["due_at"],
                  due_instant=_moment(row["due_instant"]), valid_until=row["valid_until"],
                  valid_instant=_moment(row["valid_instant"]), source_pass=row["source_pass"],
                  source_item=row["source_item"], user_approved_at=_moment(row["user_approved_at"]),
                  remind_at=row["remind_at"], remind_instant=_moment(row["remind_instant"]),
                  remind_state=row["remind_state"], reminded_at=_moment(row["reminded_at"]))


def _episode(row: sqlite3.Row) -> Episode:
    return Episode(row["episode_id"], ConversationId(row["conversation_id"]), row["from_sequence"],
                   row["through_sequence"], datetime.fromisoformat(row["started_at"]),
                   datetime.fromisoformat(row["ended_at"]), row["text"], tuple(json.loads(row["user_sequences"])),
                   tuple(json.loads(row["himari_sequences"])), MemoryOrigin(row["origin"]), row["source_pass"],
                   row["revision"], datetime.fromisoformat(row["created_at"]), datetime.fromisoformat(row["updated_at"]))


def _cursor(row: sqlite3.Row | None) -> MemoryCursor:
    if row is None:
        return MemoryCursor()
    return MemoryCursor(row["through_sequence"], row["failures"], _moment(row["retry_after"]),
                        row["baseline_sequence"], row["backfill_through"])


def _item(row: sqlite3.Row) -> PassItemRecord:
    return PassItemRecord(row["pass_id"], row["item"], row["proposal"], row["planned"], row["outcome"], row["target"],
                          row["previous"], row["written_revision"], _moment(row["decided_at"]),
                          datetime.fromisoformat(row["created_at"]))


def _pass(row: sqlite3.Row) -> MemoryPass:
    return MemoryPass(row["pass_id"], ConversationId(row["conversation_id"]), PassKind(row["kind"]),
                      row["from_sequence"], row["through_sequence"], row["snapshot_sequence"],
                      PassStatus(row["status"]), row["provider_key"], row["model_id"], row["calls"],
                      row["reserved_tokens"], row["input_tokens"], row["output_tokens"], row["unmetered_tokens"],
                      row["over_estimate"], row["omitted_chars"], row["error"], row["retry_of"],
                      datetime.fromisoformat(row["created_at"]), _moment(row["finished_at"]))


def _device(row: sqlite3.Row) -> Device:
    def moment(value: str | None) -> datetime | None:
        return None if value is None else datetime.fromisoformat(value)

    return Device(row["device_id"], row["name"], datetime.fromisoformat(row["created_at"]),
                  moment(row["pairing_expires_at"]), moment(row["paired_at"]), moment(row["last_seen_at"]))


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


def _fill_memory_cursors(tx: _Tx) -> None:
    """V3-P85 §11: every Conversation a v14 (or older) database holds reads
    from its last Event, which is also its baseline for a backfill."""
    tx.execute("INSERT INTO memory_cursors (conversation_id, through_sequence, failures, retry_after,"
               " baseline_sequence, backfill_through) SELECT conversation_id, MAX(sequence), 0, NULL, MAX(sequence), 0"
               " FROM events GROUP BY conversation_id")


def _wake(row: sqlite3.Row) -> WakeRecord:
    return WakeRecord(row["wake_id"], PrincipalId(row["owner"]), ConversationId(row["conversation_id"]),
                      int(row["event_sequence"]), codec.wake_reasons_from(json.loads(row["reasons"])),
                      row["device_id"], WakeStatus(row["status"]), datetime.fromisoformat(row["created_at"]),
                      None if row["ended_at"] is None else datetime.fromisoformat(row["ended_at"]),
                      None if row["held_run_id"] is None else RunId(row["held_run_id"]), row["thought"])


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
        unknown_outcome=None if row["unknown_outcome"] is None else UnknownOutcomePolicy(row["unknown_outcome"]),
        record_event=None if record_event is None else bool(record_event),
    )


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
    return DataError(DataErrorCode.ATTACHMENT_REFUSED, f"{file_id}: {why}", file_id=FileId(file_id), reason=why)


class SqliteData:
    """Implements ConversationLog, EnvironmentState, StateQuery,
    ConversationCatalog, ConversationRetention, ExternalSourceSettings,
    FileCatalog, FileRetention, InstructionsStore (V3-P69) and MemoryStore
    (V3-P71); ``blobs`` is the BlobStore (V3-P66)."""

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
                raise _refused(file_id, ALREADY_SENT)
            if row["expired_at"] is not None:
                raise _refused(file_id, "expired")
            if cutoff is not None and datetime.fromisoformat(row["created_at"]) < cutoff:
                raise _refused(file_id, "too old; upload it again")
            rows.append(row)
        return rows

    def _attach(self, tx: _Tx, scope: TrustedScope, blocks: Sequence[object]) -> None:
        """T1: check and attach the message's uploads in its transaction."""
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
                raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, "answers_call is older history only")
            rules.check_origin(message.origin)
            rules.check_interface(message.interface, message.origin)
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
            # V3-P90 §7.3 (PR #162 review P1-9): a free wake that passed or was dropped never was, nor the
            # events of the Run it held (V3-P91 §8)
            rows = tx.all(
                "SELECT * FROM events e WHERE conversation_id=? AND sequence>? AND NOT (kind='wake' AND EXISTS"
                " (SELECT 1 FROM wakes w WHERE w.conversation_id=e.conversation_id AND w.event_sequence=e.sequence"
                " AND w.status IN ('passed', 'dropped'))) AND NOT (e.origin_run_id IS NOT NULL AND EXISTS"
                " (SELECT 1 FROM wakes w WHERE w.conversation_id=e.conversation_id AND w.held_run_id=e.origin_run_id"
                " AND w.status IN ('passed', 'dropped'))) ORDER BY sequence LIMIT ?",
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
                    pending.append(
                        ToolExecutionRecord(
                            call=CallRef(run_id, c.call_id),
                            ordinal=c.ordinal,
                            tool_id=c.tool_id,
                            arguments=c.arguments,
                            status=ToolExecutionStatus.PENDING,
                        )
                    )
            event = self._new_event(tx, scope, output, run_id)
            for record in pending:
                tx.execute(
                    "INSERT INTO tool_executions (conversation_id, run_id, call_id, ordinal, tool_id, arguments, status,"
                    " raw_result) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    scope.conversation_id, run_id, record.call.call_id, record.ordinal, record.tool_id,
                    codec.dump_json(record.arguments), record.status.value, codec.dump_json(None),
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
            if completion.presented_files and completion.status is not ToolExecutionStatus.SUCCEEDED:
                raise DataError(DataErrorCode.INVALID_ARGUMENT, "only a succeeded call presents files")
            if completion.memory_change is not None and completion.status is not ToolExecutionStatus.SUCCEEDED:
                raise DataError(DataErrorCode.INVALID_ARGUMENT, "only a succeeded call changes memory")
            if completion.plan_change is not None and completion.status is not ToolExecutionStatus.SUCCEEDED:
                raise DataError(DataErrorCode.INVALID_ARGUMENT, "only a succeeded call changes the schedule")
            if completion.memory_change is not None:
                self._apply_memory(tx, scope, call, completion.memory_change)
            if completion.plan_change is not None:  # character-day §18.6
                self._apply_plan_change(tx, scope.principal_id, completion.plan_change)
            if completion.presented_files:
                self._present(tx, scope, call, completion.presented_files)
            self._attach_tool_images(tx, scope, call, completion.model_result.blocks)
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

    # --- EnvironmentState ------------------------------------------------------

    async def has_environment(self, scope: TrustedScope, environment_kind: EnvironmentKind) -> bool:
        with self._tx() as tx:
            tx.authorized(scope)
            return tx.one("SELECT 1 FROM environments WHERE conversation_id=? AND environment_kind=?",
                          scope.conversation_id, environment_kind) is not None

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
        with self._tx() as tx:  # T0: the Conversation, its Environments, and initial model, or nothing
            scope = TrustedScope(principal, ConversationId(tx.new_id("conv")))
            tx.insert_conversation(scope, title)
            for kind in environments:
                tx.add_environment(scope, kind)
            if model_setting is not None:
                tx.execute("INSERT INTO model_settings (conversation_id, model_id, reasoning_effort) VALUES (?, ?, ?)",
                           scope.conversation_id, model_setting.model_id, model_setting.reasoning_effort)
            return scope.conversation_id

    _SUMMARY = (
        "SELECT c.conversation_id, c.title, c.created_at, c.trashed_at, c.day_session, c.closed_at,"
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
            day_session=row["day_session"],
            closed_at=_moment(row["closed_at"]),
        )

    # --- day sessions (character-day v0.1 §3, §18.2) --------------------------------------------

    async def open_day_session(self, principal: PrincipalId, day: str, title: str, *,
                               environments: Sequence[EnvironmentKind] = (),
                               model_setting: ModelSetting | None = None) -> ConversationId:
        day = rules.check_life_day(day)
        title_text = rules.normalize_title(title)
        rules.check_environments(environments)
        rules.check_model_setting(model_setting)
        try:
            with self._tx() as tx:
                scope = TrustedScope(principal, ConversationId(tx.new_id("conv")))
                tx.insert_conversation(scope, title_text)
                tx.execute("UPDATE conversations SET day_session=? WHERE conversation_id=?", day,
                           scope.conversation_id)
                for kind in environments:
                    tx.add_environment(scope, kind)
                if model_setting is not None:
                    tx.execute("INSERT INTO model_settings (conversation_id, model_id, reasoning_effort)"
                               " VALUES (?, ?, ?)", scope.conversation_id, model_setting.model_id,
                               model_setting.reasoning_effort)
                return scope.conversation_id
        except sqlite3.IntegrityError as error:
            raise DataError(DataErrorCode.DUPLICATE_IDENTITY, "a day session is open already") from error

    async def current_day_session(self, principal: PrincipalId) -> ConversationSummary | None:
        with self._tx() as tx:
            row = tx.one(f"{self._SUMMARY} WHERE c.owner=? AND c.day_session IS NOT NULL AND c.closed_at IS NULL"
                         " AND c.trashed_at IS NULL", principal)
            return None if row is None else self._summary(row)

    async def close_day_session(self, principal: PrincipalId, conversation_id: ConversationId,
                                now: datetime) -> bool:
        with self._tx() as tx:
            return tx.execute("UPDATE conversations SET closed_at=? WHERE conversation_id=? AND owner=?"
                              " AND day_session IS NOT NULL AND closed_at IS NULL", _timestamp(now),
                              conversation_id, principal).rowcount == 1

    async def last_user_message_at(self, principal: PrincipalId) -> datetime | None:
        with self._tx() as tx:
            row = tx.one("SELECT MAX(e.created_at) AS at FROM events e JOIN conversations o"
                         " ON o.conversation_id=e.conversation_id"
                         " WHERE o.owner=? AND o.trashed_at IS NULL AND e.kind='user_message'", principal)
            return None if row is None or row["at"] is None else datetime.fromisoformat(row["at"])

    async def most_recent_user_conversation(self, principal: PrincipalId) -> ConversationSummary | None:
        rows = await self.list_user_conversations(principal, limit=1)
        return rows[0] if rows else None

    async def list_user_conversations(
        self, principal: PrincipalId, *, limit: int = 50, offset: int = 0
    ) -> Sequence[ConversationSummary]:
        rules.check_page(limit, offset)
        with self._tx() as tx:
            rows = tx.all(
                f"{self._SUMMARY} WHERE c.owner=? AND c.trashed_at IS NULL"
                " AND EXISTS (SELECT 1 FROM events e WHERE e.conversation_id=c.conversation_id"
                " AND e.kind='user_message')"
                " ORDER BY (SELECT MAX(e.created_at) FROM events e"
                " WHERE e.conversation_id=c.conversation_id AND e.kind='user_message') DESC, c.rowid DESC"
                " LIMIT ? OFFSET ?", principal, limit, offset)
            return tuple(self._summary(row) for row in rows)

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
            # character-day §3.2: a day session moved to the trash is closed with it (the next opens when needed)
            tx.execute("UPDATE conversations SET trashed_at=?, closed_at=CASE WHEN day_session IS NOT NULL"
                       " THEN COALESCE(closed_at, ?) END WHERE conversation_id=?", _timestamp(now),
                       _timestamp(now), scope.conversation_id)
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
            return TrashedConversation(self._summary(row), tx.environments(scope))

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
        "DELETE FROM environments WHERE conversation_id=?",
        "DELETE FROM external_settings WHERE conversation_id=?",
        "DELETE FROM model_settings WHERE conversation_id=?",
        "DELETE FROM memories WHERE source_conversation=?",  # D5: the notes its calls wrote (V3-P71)
        # V3-P85 §11: its episodes, reading position and passes with their records
        "DELETE FROM episodes WHERE conversation_id=?",
        "DELETE FROM memory_cursors WHERE conversation_id=?",
        "DELETE FROM memory_pass_items WHERE pass_id IN (SELECT pass_id FROM memory_passes WHERE conversation_id=?)",
        "DELETE FROM memory_pass_calls WHERE pass_id IN (SELECT pass_id FROM memory_passes WHERE conversation_id=?)",
        "DELETE FROM memory_passes WHERE conversation_id=?",
        "DELETE FROM wakes WHERE conversation_id=?",  # V3-P87 (its keys stay: what was said is not said again)
        # character-bond §2: what the two settled in it rests on the user's words: it goes, as its notes do,
        # unless the user has made it their own since
        "DELETE FROM life_entries WHERE kind='bond' AND origin<>'user' AND source_conversation=?",
        # character-mind §3.4: the thoughts kept from it go with it (as its notes do)
        "DELETE FROM life_entries WHERE kind='thought' AND source_conversation=?",
        # character-life §2: HIMARI's facts from it stay (its life, not the user's words); the link goes
        "UPDATE life_entries SET source_conversation=NULL, source_sequences='[]', source=NULL"
        " WHERE source_conversation=?",
        # character-day §7: a diary written in it stays (its life); which session it was written in goes
        "UPDATE life_writes SET conversation_id=NULL WHERE conversation_id=?",
        "DELETE FROM conversations WHERE conversation_id=?",
    )

    async def purge_conversation(self, scope: TrustedScope) -> None:
        with self._tx() as tx:
            if tx.owned(scope)["purge_started_at"] is None:
                raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"{scope.conversation_id} is not being deleted")
            # V3-P97 keeps the table for the views stored before: they still go with their last call
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

    def _public_presented_call(self, tx: _Tx, scope: TrustedScope, call: CallRef,
                               cache: dict[CallRef, bool]) -> bool:
        if call in cache:
            return cache[call]
        wake = self._wake_of_run_tx(tx, scope, call.run_id)
        if wake is not None and wake.status in (WakeStatus.OPENED, WakeStatus.PASSED, WakeStatus.DROPPED):
            cache[call] = False
            return False
        events = tx.all("SELECT payload FROM events WHERE conversation_id=? AND origin_run_id=? AND kind=?",
                        scope.conversation_id, call.run_id, EventKind.TOOL_RESULT.value)
        visible = any(isinstance(p := codec.payload_from(EventKind.TOOL_RESULT, r["payload"]), ToolResultRecord)
                      and p.call == call for r in events)
        cache[call] = visible
        return visible

    async def read_presented_files(self, scope: TrustedScope, *, limit: int = 50,
                                   offset: int = 0) -> Sequence[FileRecord]:
        rules.check_page(limit, offset)
        with self._tx() as tx:
            tx.authorized(scope)
            rows = tx.all(
                f"SELECT {_FILE_COLUMNS} {_FILE_FROM} WHERE f.conversation_id=? AND f.origin=?"
                " AND f.run_id IS NOT NULL AND f.call_id IS NOT NULL"
                " AND EXISTS (SELECT 1 FROM tool_executions t WHERE t.conversation_id=f.conversation_id"
                " AND t.run_id=f.run_id AND t.call_id=f.call_id AND t.status=?)"
                " AND NOT EXISTS (SELECT 1 FROM wakes w WHERE w.conversation_id=f.conversation_id"
                " AND w.held_run_id=f.run_id AND w.status IN ('opened', 'passed', 'dropped'))"
                " ORDER BY f.created_at DESC, f.file_id DESC, f.version DESC",
                scope.conversation_id, FileOrigin.PRESENTED.value, ToolExecutionStatus.SUCCEEDED.value)
            cache: dict[CallRef, bool] = {}
            records = [_file(r) for r in rows if self._public_presented_call(
                tx, scope, CallRef(RunId(r["run_id"]), CallId(r["call_id"])), cache)]
            return tuple(records[offset: offset + limit])

    async def read_presented_file(self, scope: TrustedScope, file_id: FileId,
                                  version: int) -> FileRecord | None:
        with self._tx() as tx:
            tx.authorized(scope)
            row = tx.one(
                f"SELECT {_FILE_COLUMNS} {_FILE_FROM} WHERE f.conversation_id=? AND f.file_id=? AND f.version=?"
                " AND f.origin=? AND f.run_id IS NOT NULL AND f.call_id IS NOT NULL"
                " AND EXISTS (SELECT 1 FROM tool_executions t WHERE t.conversation_id=f.conversation_id"
                " AND t.run_id=f.run_id AND t.call_id=f.call_id AND t.status=?)"
                " AND NOT EXISTS (SELECT 1 FROM wakes w WHERE w.conversation_id=f.conversation_id"
                " AND w.held_run_id=f.run_id AND w.status IN ('opened', 'passed', 'dropped'))",
                scope.conversation_id, file_id, version, FileOrigin.PRESENTED.value,
                ToolExecutionStatus.SUCCEEDED.value)
            if row is None:
                return None
            call = CallRef(RunId(row["run_id"]), CallId(row["call_id"]))
            if not self._public_presented_call(tx, scope, call, {}):
                return None
            return _file(row)

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

    # --- PersonaStore (V3-P108 §5.2) ------------------------------------------------

    _PERSONA_COLUMNS = "persona_id, name, text, revision, created_at, updated_at, fields"

    def _read_persona(self, tx: _Tx, principal: PrincipalId, persona_id: object) -> Persona:
        row = tx.one(f"SELECT {self._PERSONA_COLUMNS} FROM personas WHERE owner=? AND persona_id=?",
                     principal, rules.check_persona_id(persona_id))
        if row is None:
            raise DataError(DataErrorCode.NOT_FOUND, f"no persona {persona_id}")
        return _persona(row)

    @staticmethod
    def _check_name_free(tx: _Tx, principal: PrincipalId, key: str, besides: int | None) -> None:
        row = tx.one("SELECT persona_id FROM personas WHERE owner=? AND name_key=?", principal, key)
        if row is not None and row["persona_id"] != besides:
            raise DataError(DataErrorCode.DUPLICATE_IDENTITY, "another persona has this name")

    @staticmethod
    def _persona_choice(tx: _Tx, principal: PrincipalId) -> PersonaChoice:
        row = tx.one("SELECT persona_id, revision, updated_at FROM persona_choice WHERE owner=?", principal)
        if row is None:
            return PersonaChoice(None, 0)
        return PersonaChoice(row["persona_id"], row["revision"], datetime.fromisoformat(row["updated_at"]))

    @staticmethod
    def _write_choice(tx: _Tx, principal: PrincipalId, choice: PersonaChoice) -> None:
        assert choice.updated_at is not None
        tx.execute("INSERT INTO persona_choice (owner, persona_id, revision, updated_at) VALUES (?, ?, ?, ?)"
                   " ON CONFLICT (owner) DO UPDATE SET persona_id=excluded.persona_id, revision=excluded.revision,"
                   " updated_at=excluded.updated_at",
                   principal, choice.persona_id, choice.revision, _timestamp(choice.updated_at))

    async def list_personas(self, principal: PrincipalId) -> PersonaList:
        with self._tx() as tx:
            rows = tx.all(f"SELECT {self._PERSONA_COLUMNS} FROM personas WHERE owner=? ORDER BY persona_id", principal)
            return PersonaList(tuple(_persona(row) for row in rows), self._persona_choice(tx, principal))

    async def create_persona(self, principal: PrincipalId, name: str, text: str,
                             fields: Mapping[str, str] | None = None) -> Persona:
        name, key = rules.check_persona_name(name)
        kept, text = rules.check_persona_fields(text, fields)
        with self._tx() as tx:
            count = tx.one("SELECT COUNT(*) AS n FROM personas WHERE owner=?", principal)
            assert count is not None
            if count["n"] >= MAX_PERSONAS:
                raise DataError(DataErrorCode.LIMIT_EXCEEDED, f"at most {MAX_PERSONAS} personas")
            self._check_name_free(tx, principal, key, None)
            last = tx.one("SELECT last_id FROM persona_ids WHERE owner=?", principal)
            persona_id = (0 if last is None else last["last_id"]) + 1
            tx.execute("INSERT INTO persona_ids (owner, last_id) VALUES (?, ?)"
                       " ON CONFLICT (owner) DO UPDATE SET last_id=excluded.last_id", principal, persona_id)
            now = datetime.now(UTC)
            tx.execute("INSERT INTO personas (owner, persona_id, name, name_key, text, fields, revision, created_at,"
                       " updated_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
                       principal, persona_id, name, key, text, rules.persona_fields_json(kept), _timestamp(now),
                       _timestamp(now))
            return Persona(persona_id, name, text, 1, now, now, kept)

    async def update_persona(self, principal: PrincipalId, persona_id: int, name: str, text: str,
                             expected_revision: int, fields: Mapping[str, str] | None = None) -> Persona:
        name, key = rules.check_persona_name(name)
        kept, text = rules.check_persona_fields(text, fields)
        with self._tx() as tx:
            current = self._read_persona(tx, principal, persona_id)
            if expected_revision != current.revision:
                raise DataError(DataErrorCode.VERSION_CONFLICT, f"the persona is at revision {current.revision}")
            self._check_name_free(tx, principal, key, current.persona_id)
            now = datetime.now(UTC)
            tx.execute("UPDATE personas SET name=?, name_key=?, text=?, fields=?, revision=?, updated_at=?"
                       " WHERE owner=? AND persona_id=?",
                       name, key, text, rules.persona_fields_json(kept), current.revision + 1, _timestamp(now),
                       principal, current.persona_id)
            return dataclasses.replace(current, name=name, text=text, fields=kept, revision=current.revision + 1,
                                       updated_at=now)

    async def delete_persona(self, principal: PrincipalId, persona_id: int, expected_revision: int) -> PersonaChoice:
        with self._tx() as tx:
            current = self._read_persona(tx, principal, persona_id)
            if expected_revision != current.revision:
                raise DataError(DataErrorCode.VERSION_CONFLICT, f"the persona is at revision {current.revision}")
            tx.execute("DELETE FROM personas WHERE owner=? AND persona_id=?", principal, current.persona_id)
            choice = self._persona_choice(tx, principal)
            if choice.persona_id == current.persona_id:  # §4.3: no persona is chosen after it
                choice = PersonaChoice(None, choice.revision + 1, datetime.now(UTC))
                self._write_choice(tx, principal, choice)
            return choice

    async def choose_persona(self, principal: PrincipalId, persona_id: int | None,
                             expected_revision: int) -> PersonaChoice:
        with self._tx() as tx:
            if persona_id is not None:
                self._read_persona(tx, principal, persona_id)
            current = self._persona_choice(tx, principal)
            if expected_revision != current.revision:
                raise DataError(DataErrorCode.VERSION_CONFLICT, f"the choice is at revision {current.revision}")
            choice = PersonaChoice(persona_id, current.revision + 1, datetime.now(UTC))
            self._write_choice(tx, principal, choice)
            return choice

    async def active_persona(self, principal: PrincipalId) -> Persona | None:
        with self._tx() as tx:
            row = tx.one(f"SELECT {', '.join('p.' + c for c in self._PERSONA_COLUMNS.split(', '))} FROM persona_choice c"
                         " JOIN personas p ON p.owner=c.owner AND p.persona_id=c.persona_id WHERE c.owner=?",
                         principal)
            return None if row is None else _persona(row)

    # --- LifeStore, LifeWriteLog (character-life v0.1 §2, §3) -----------------------------

    _LIFE_COLUMNS = ("entry_id, persona_id, kind, day, text, origin, revision, created_at, updated_at,"
                     " source_conversation, source_sequences, source, tag, month_day, time_of_day, title, end_day,"
                     " ending, arc_id, summary, plan_id, so_far, about, expires_at, draft, minutes, plan_kind,"
                     " plan_state, reason")

    def _live_entry(self, tx: _Tx, principal: PrincipalId, entry_id: object) -> LifeEntry:
        row = None if isinstance(entry_id, bool) or not isinstance(entry_id, int) else tx.one(
            f"SELECT {self._LIFE_COLUMNS} FROM life_entries WHERE owner=? AND entry_id=? AND retired_at IS NULL",
            principal, entry_id)
        if row is None:
            raise DataError(DataErrorCode.NOT_FOUND, f"no life entry {entry_id}")
        return _life_entry(row)

    def _live_facts(self, tx: _Tx, principal: PrincipalId, persona_id: int) -> list[LifeEntry]:
        return [_life_entry(r) for r in tx.all(
            f"SELECT {self._LIFE_COLUMNS} FROM life_entries WHERE owner=? AND persona_id=? AND kind='fact'"
            " AND retired_at IS NULL", principal, persona_id)]

    @staticmethod
    def _new_life(tx: _Tx, principal: PrincipalId, entry: LifeEntry) -> LifeEntry:
        """``entry`` with the principal's next id, inserted."""
        last = tx.one("SELECT last_id FROM life_ids WHERE owner=?", principal)
        entry = dataclasses.replace(entry, entry_id=(0 if last is None else int(last["last_id"])) + 1)
        tx.execute("INSERT INTO life_ids (owner, last_id) VALUES (?, ?)"
                   " ON CONFLICT (owner) DO UPDATE SET last_id=excluded.last_id", principal, entry.entry_id)
        tx.execute("INSERT INTO life_entries (owner, entry_id, persona_id, kind, day, text, origin, revision,"
                   " created_at, updated_at, retired_at, source_conversation, source_sequences, source, tag,"
                   " month_day, time_of_day, title, end_day, ending, arc_id, summary, plan_id, so_far, about,"
                   " expires_at, draft, minutes, plan_kind, plan_state, reason)"
                   " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                   " ?, ?, ?, ?)",
                   principal, entry.entry_id, entry.persona_id, entry.kind.value, entry.day, entry.text,
                   entry.origin.value, entry.revision, _timestamp(entry.created_at), _timestamp(entry.updated_at),
                   entry.source_conversation, json.dumps(list(entry.source_sequences)), entry.source,
                   None if entry.tag is None else entry.tag.value, entry.month_day, entry.time_of_day, entry.title,
                   entry.end_day, entry.ending, entry.arc_id, entry.summary, entry.plan_id, entry.so_far,
                   None if entry.about is None else entry.about.value,
                   None if entry.expires_at is None else _timestamp(entry.expires_at), int(entry.draft),
                   entry.minutes, None if entry.plan_kind is None else entry.plan_kind.value,
                   None if entry.plan_state is None else entry.plan_state.value, entry.reason)
        return entry

    @staticmethod
    def _rewrite_life(tx: _Tx, principal: PrincipalId, entry: LifeEntry) -> LifeEntry:
        tx.execute("UPDATE life_entries SET text=?, origin=?, revision=?, updated_at=?, source_conversation=?,"
                   " source_sequences=?, source=?, tag=?, month_day=?, title=?, end_day=?, ending=?, arc_id=?,"
                   " summary=?, so_far=?, time_of_day=?, draft=?, created_at=?, day=?, minutes=?, plan_kind=?,"
                   " plan_state=?, reason=? WHERE owner=? AND entry_id=?",
                   entry.text, entry.origin.value, entry.revision, _timestamp(entry.updated_at),
                   entry.source_conversation, json.dumps(list(entry.source_sequences)), entry.source,
                   None if entry.tag is None else entry.tag.value, entry.month_day, entry.title, entry.end_day,
                   entry.ending, entry.arc_id, entry.summary, entry.so_far, entry.time_of_day, int(entry.draft),
                   _timestamp(entry.created_at), entry.day, entry.minutes,
                   None if entry.plan_kind is None else entry.plan_kind.value,
                   None if entry.plan_state is None else entry.plan_state.value, entry.reason,
                   principal, entry.entry_id)
        return entry

    @staticmethod
    def _retire_life(tx: _Tx, principal: PrincipalId, entry: LifeEntry, now: datetime) -> None:
        tx.execute("UPDATE life_entries SET retired_at=?, revision=?, updated_at=? WHERE owner=? AND entry_id=?",
                   _timestamp(now), entry.revision + 1, _timestamp(now), principal, entry.entry_id)

    def _add_fact(self, tx: _Tx, principal: PrincipalId, fact: LifeEntry, *, dedupe: bool) -> LifeEntry | str:
        """§2: a new fact within MAX_LIFE_FACTS, or why not (``duplicate``, ``full``)."""
        facts = self._live_facts(tx, principal, fact.persona_id)
        if dedupe and rules.same_fact(fact.text, facts):
            return "duplicate"
        fits, retire = rules.fact_room(facts)
        if not fits:
            return "full"
        if retire is not None:
            self._retire_life(tx, principal, retire, fact.created_at)
        return self._new_life(tx, principal, fact)

    async def list_life(self, principal: PrincipalId, persona_id: int, *,
                        since: str | None = None, now: datetime | None = None) -> tuple[LifeEntry, ...]:
        persona_id = rules.check_life_persona(persona_id)
        since = None if since is None else rules.check_life_day(since)
        at = now or datetime.now(UTC)
        with self._tx() as tx:
            rows = tx.all(f"SELECT {self._LIFE_COLUMNS} FROM life_entries WHERE owner=? AND persona_id=?"
                          " AND retired_at IS NULL AND kind <> 'bond' AND (kind='fact' OR kind='thought'"
                          " OR ? IS NULL OR day >= ?"
                          " OR (kind='arc' AND (end_day IS NULL OR end_day >= ?))"
                          " OR (kind='work' AND arc_id IN (SELECT a.entry_id FROM life_entries a WHERE a.owner=?"
                          " AND a.kind='arc' AND a.end_day IS NULL AND a.retired_at IS NULL)))",
                          principal, persona_id, since, since, since, principal)
            # character-mind §5.2: a kept thought is listed until it expires
            return rules.order_life(e for e in map(_life_entry, rows) if rules.live_thought(e, at))

    async def add_life_fact(self, principal: PrincipalId, persona_id: int, text: str) -> LifeEntry:
        persona_id = rules.check_life_persona(persona_id)
        text = rules.check_life_text(LifeKind.FACT, text)
        now = datetime.now(UTC)
        with self._tx() as tx:
            added = self._add_fact(tx, principal, LifeEntry(0, persona_id, LifeKind.FACT, None, text, LifeOrigin.USER,
                                                            1, now, now), dedupe=False)
            if not isinstance(added, LifeEntry):
                raise DataError(DataErrorCode.LIMIT_EXCEEDED, f"at most {MAX_LIFE_FACTS} facts, all yours")
            return added

    async def edit_life(self, principal: PrincipalId, entry_id: int, text: str, *,
                        expected_revision: int, title: str | None = None,
                        summary: str | None = None) -> LifeEntry:
        with self._tx() as tx:
            entry = self._live_entry(tx, principal, entry_id)
            rules.check_editable(entry)
            text = rules.check_life_text(entry.kind, text)
            titled = rules.edited_title(entry, title)
            summed = rules.edited_summary(entry, summary)
            if expected_revision != entry.revision:
                raise DataError(DataErrorCode.VERSION_CONFLICT, f"the entry is at revision {entry.revision}")
            return self._rewrite_life(tx, principal, dataclasses.replace(
                entry, text=text, title=titled, summary=summed, origin=LifeOrigin.USER, revision=entry.revision + 1,
                updated_at=datetime.now(UTC)))

    async def retire_life(self, principal: PrincipalId, entry_id: int, *, expected_revision: int) -> None:
        with self._tx() as tx:
            entry = self._live_entry(tx, principal, entry_id)
            if expected_revision != entry.revision:
                raise DataError(DataErrorCode.VERSION_CONFLICT, f"the entry is at revision {entry.revision}")
            rules.check_retire(entry)  # character-day §4.2: a sleep slot is only moved
            self._retire_life(tx, principal, entry, datetime.now(UTC))

    # --- the schedule (character-day v0.1 §4, §18.2) ---------------------------------------------

    def _plans(self, tx: _Tx, principal: PrincipalId, persona_id: int) -> list[LifeEntry]:
        """Every plan of the persona not retired (cancelled and missed too), and its open arcs."""
        return [_life_entry(r) for r in tx.all(
            f"SELECT {self._LIFE_COLUMNS} FROM life_entries WHERE owner=? AND persona_id=? AND retired_at IS NULL"
            " AND (kind='plan' OR (kind='arc' AND end_day IS NULL))", principal, persona_id)]

    def _write_plan(self, tx: _Tx, principal: PrincipalId, persona_id: int, plan: PlanWrite, origin: LifeOrigin,
                    zone: str, now: datetime) -> LifeEntry:
        persona_id = rules.check_life_persona(persona_id)
        plan = rules.check_plan_write(plan)
        where = rules.check_zone(zone)
        entries = self._plans(tx, principal, persona_id)
        plans = [e for e in entries if e.kind is LifeKind.PLAN]
        before = None
        if plan.entry_id is not None:
            before = rules.live_plan_of(self._live_entry(tx, principal, plan.entry_id), persona_id)
        plan = rules.check_plan_limits(plan, before, plans, [e for e in entries if e.kind is LifeKind.ARC],
                                       origin=origin, zone=where, now=now)
        made = rules.planned_entry(persona_id, plan, before, origin, now)
        return self._new_life(tx, principal, made) if before is None else self._rewrite_life(tx, principal, made)

    def _cancel_plan(self, tx: _Tx, principal: PrincipalId, entry_id: object, reason: object, origin: LifeOrigin,
                     zone: str, now: datetime, persona_id: int | None = None) -> LifeEntry:
        why = rules.check_plan_reason(reason)
        if why is None:
            raise DataError(DataErrorCode.INVALID_ARGUMENT, "a cancelled slot says why")
        before = rules.live_plan_of(self._live_entry(tx, principal, entry_id), persona_id)
        plans = [e for e in self._plans(tx, principal, before.persona_id) if e.kind is LifeKind.PLAN]
        rules.check_cancel(before, plans, origin=origin, zone=rules.check_zone(zone), now=now)
        return self._rewrite_life(tx, principal, dataclasses.replace(
            before, plan_state=PlanState.CANCELLED, reason=why, origin=origin, revision=before.revision + 1,
            updated_at=now))

    def _apply_plan_change(self, tx: _Tx, principal: PrincipalId, change: PlanChange) -> LifeEntry | None:
        """§18.6: a schedule Tool's change (origin HIMARI) in the transaction of its call's result."""
        if not isinstance(change, PlanChange) or not isinstance(change.op, PlanOp):
            raise DataError(DataErrorCode.INVALID_ARGUMENT, "not a plan change")
        if change.op is PlanOp.WRITE:
            if change.write is None:
                raise DataError(DataErrorCode.INVALID_ARGUMENT, "a write carries its slot")
            return self._write_plan(tx, principal, change.persona_id, change.write, LifeOrigin.HIMARI, change.zone,
                                    change.at)
        if change.op is PlanOp.CANCEL:
            return self._cancel_plan(tx, principal, change.entry_id, change.reason, LifeOrigin.HIMARI, change.zone,
                                     change.at, rules.check_life_persona(change.persona_id))
        persona_id = rules.check_life_persona(change.persona_id)
        zone = rules.check_zone(change.zone)
        plans = [e for e in self._plans(tx, principal, persona_id) if e.kind is LifeKind.PLAN]
        slot, write = rules.sleep_now(plans, zone=zone, now=change.at, reason=rules.check_plan_reason(change.reason))
        made = slot
        if write is not None:
            rules.check_himari_count(plans, None if slot is None else slot.entry_id, zone, change.at)
            made = rules.planned_entry(persona_id, rules.check_plan_write(write), slot, LifeOrigin.HIMARI, change.at)
            made = self._new_life(tx, principal, made) if slot is None else self._rewrite_life(tx, principal, made)
        state = self._day_state(tx, principal, persona_id)
        self._put_day_state(tx, principal, dataclasses.replace(state, sleep_requested_at=change.at))
        return made

    async def write_plan(self, principal: PrincipalId, persona_id: int, plan: PlanWrite, *, origin: LifeOrigin,
                         zone: str, now: datetime) -> LifeEntry:
        with self._tx() as tx:
            return self._write_plan(tx, principal, persona_id, plan, origin, zone, now)

    async def cancel_plan(self, principal: PrincipalId, entry_id: int, reason: str, *, origin: LifeOrigin,
                          zone: str, now: datetime) -> LifeEntry:
        with self._tx() as tx:
            return self._cancel_plan(tx, principal, entry_id, reason, origin, zone, now)

    async def mark_plan_missed(self, principal: PrincipalId, entry_id: int, *, now: datetime) -> LifeEntry:
        with self._tx() as tx:
            before = rules.live_plan_of(self._live_entry(tx, principal, entry_id))
            if before.plan_kind is not PlanKind.TOGETHER:
                raise DataError(DataErrorCode.INVALID_ARGUMENT, "only a together slot is missed")
            return self._rewrite_life(tx, principal, dataclasses.replace(
                before, plan_state=PlanState.MISSED, revision=before.revision + 1, updated_at=now))

    async def apply_life_changes(self, principal: PrincipalId, persona_id: int, changes: Sequence[LifeChange], *,
                                 conversation: ConversationId, source: str, now: datetime) -> tuple[str, ...]:
        persona_id = rules.check_life_persona(persona_id)
        with self._tx() as tx:
            outcomes: list[str] = []
            for change in changes:
                outcome = rules.life_change_outcome(change, self._live_facts(tx, principal, persona_id))
                if outcome is not None:
                    outcomes.append(outcome)
                    continue
                text = None if change.text is None else rules.check_life_text(LifeKind.FACT, change.text)
                sequences = tuple(change.sequences)
                if change.op is LifeChangeOp.ADD:
                    assert text is not None
                    added = self._add_fact(tx, principal, LifeEntry(
                        0, persona_id, LifeKind.FACT, None, text, LifeOrigin.CONVERSATION, 1, now, now, conversation,
                        sequences, source), dedupe=True)
                    outcomes.append("applied" if isinstance(added, LifeEntry) else added)
                    continue
                entry = self._live_entry(tx, principal, change.entry_id)
                if change.op is LifeChangeOp.RETIRE:
                    self._retire_life(tx, principal, entry, now)
                else:
                    assert text is not None
                    self._rewrite_life(tx, principal, dataclasses.replace(
                        entry, text=text, origin=LifeOrigin.CONVERSATION, revision=entry.revision + 1, updated_at=now,
                        source_conversation=conversation, source_sequences=sequences, source=source))
                outcomes.append("applied")
            return tuple(outcomes)

    def _live_thoughts(self, tx: _Tx, principal: PrincipalId, persona_id: int, now: datetime) -> list[LifeEntry]:
        return [e for e in map(_life_entry, tx.all(
            f"SELECT {self._LIFE_COLUMNS} FROM life_entries WHERE owner=? AND persona_id=? AND kind='thought'"
            " AND retired_at IS NULL", principal, persona_id)) if rules.live_thought(e, now)]

    async def apply_thought_changes(self, principal: PrincipalId, persona_id: int,
                                    changes: Sequence[ThoughtChange], *, conversation: ConversationId, source: str,
                                    now: datetime) -> tuple[str, ...]:
        persona_id = rules.check_life_persona(persona_id)
        with self._tx() as tx:
            outcomes: list[str] = []
            for change in changes:
                thoughts = self._live_thoughts(tx, principal, persona_id, now)
                outcome = rules.thought_change_outcome(change, thoughts)
                if outcome is not None:
                    outcomes.append(outcome)
                    continue
                if change.op is ThoughtOp.SAID:  # §5.1: said now, so no longer unsaid
                    self._retire_life(tx, principal, next(t for t in thoughts if t.entry_id == change.entry_id), now)
                else:
                    if (old := rules.thought_room(thoughts)) is not None:
                        self._retire_life(tx, principal, old, now)
                    self._new_life(tx, principal, rules.kept_thought(persona_id, change, conversation, source, now))
                outcomes.append("applied")
            return tuple(outcomes)

    async def life_principals(self, active_since: datetime) -> tuple[PrincipalId, ...]:
        with self._tx() as tx:
            rows = tx.all("SELECT DISTINCT c.owner FROM conversations c WHERE c.trashed_at IS NULL AND EXISTS"
                          " (SELECT 1 FROM events e WHERE e.conversation_id=c.conversation_id"
                          " AND e.kind='user_message' AND e.created_at >= ?) ORDER BY c.owner",
                          _timestamp(active_since))
            return tuple(PrincipalId(r["owner"]) for r in rows)

    async def life_attempts(self, principal: PrincipalId, persona_id: int, day: str, *,
                            job: LifeJob = LifeJob.REVIEW) -> int:
        with self._tx() as tx:
            row = tx.one("SELECT COUNT(*) AS n FROM life_writes WHERE owner=? AND persona_id=? AND day=? AND job=?",
                         principal, persona_id, day, LifeJob(job).value)
            assert row is not None
            return int(row["n"])

    async def work_attempts(self, principal: PrincipalId, plan_id: int, *, job: LifeJob = LifeJob.WORK_END) -> int:
        with self._tx() as tx:
            row = tx.one("SELECT COUNT(*) AS n FROM life_writes WHERE owner=? AND plan_id=? AND job=?", principal,
                         plan_id, LifeJob(job).value)
            assert row is not None
            return int(row["n"])

    async def life_done(self, principal: PrincipalId, persona_id: int, day: str, job: LifeJob) -> bool:
        with self._tx() as tx:
            return tx.one("SELECT 1 FROM life_writes WHERE owner=? AND persona_id=? AND day=? AND job=?"
                          " AND status='applied' LIMIT 1", principal, persona_id, day, LifeJob(job).value) is not None

    async def note_life_thought(self, write_id: str, thought: str) -> None:
        thought = rules.check_thought_note(thought)
        with self._tx() as tx:
            self._life_write_row(tx, write_id)
            tx.execute("UPDATE life_writes SET thought=? WHERE write_id=?", thought, write_id)

    async def life_usage(self, principal: PrincipalId, since: datetime) -> int:
        with self._tx() as tx:
            row = tx.one("SELECT COALESCE(SUM(charged_tokens), 0) AS n FROM life_writes WHERE owner=?"
                         " AND created_at >= ?", principal, _timestamp(since))
            assert row is not None
            return int(row["n"])

    @staticmethod
    def _life_write_row(tx: _Tx, write_id: object) -> sqlite3.Row:
        row = None if not isinstance(write_id, str) else tx.one("SELECT * FROM life_writes WHERE write_id=?",
                                                                 write_id)
        if row is None:
            raise DataError(DataErrorCode.NOT_FOUND, f"no life write {write_id}")
        return row

    async def begin_life_write(self, principal: PrincipalId, start: LifeWriteStart) -> str:
        if (not isinstance(start, LifeWriteStart) or not isinstance(start.body, bytes) or start.reserved_tokens < 0
                or not rules.check_plan_ref(start.plan_id)):
            raise DataError(DataErrorCode.INVALID_ARGUMENT, "not a life write")
        persona_id, day = rules.check_life_persona(start.persona_id), rules.check_life_day(start.day)
        job = rules.write_job(start)
        try:
            with self._tx() as tx:
                write_id = tx.new_id("lwrite")
                tx.execute("INSERT INTO life_writes (write_id, owner, persona_id, day, status, provider_key, model_id,"
                           " reserved_tokens, charged_tokens, usage, raw_request_body, raw_request_content_type,"
                           " error, created_at, finished_at, plan_id, job, model_reason, thought, conversation_id)"
                           " VALUES (?, ?, ?, ?, 'prepared', ?, ?, ?, ?, NULL, ?, ?, NULL, ?, NULL, ?, ?, ?, NULL, ?)",
                           write_id, principal, persona_id, day, start.provider_key, start.model_id,
                           start.reserved_tokens, start.reserved_tokens, start.body, start.content_type,
                           _timestamp(datetime.now(UTC)), start.plan_id, job.value, start.model_reason,
                           start.conversation_id)
                return write_id
        except sqlite3.Error as error:
            raise DataError(DataErrorCode.RAW_PERSISTENCE_UNAVAILABLE, type(error).__name__) from error

    async def mark_life_write_dispatched(self, write_id: str) -> None:
        with self._tx() as tx:
            self._life_write_row(tx, write_id)
            if tx.execute("UPDATE life_writes SET status='dispatched' WHERE write_id=? AND status='prepared'",
                          write_id).rowcount != 1:
                raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"{write_id} is not prepared")

    async def record_life_write(self, write_id: str, outcome: RawRunOutcome) -> None:
        with self._tx() as tx:
            row = self._life_write_row(tx, write_id)
            if row["status"] not in ("prepared", "dispatched"):
                raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"{write_id} is {row['status']}")
            usage = outcome.usage if isinstance(outcome, RawReceived) else None
            reported = (None if usage is None or usage.input_tokens is None or usage.output_tokens is None
                        else (usage.input_tokens, usage.output_tokens))
            received = isinstance(outcome, RawReceived)
            tx.execute("UPDATE life_writes SET status=?, raw_response_body=?, raw_response_content_type=?, usage=?,"
                       " charged_tokens=?, error=?, finished_at=? WHERE write_id=?",
                       "received" if received else "failed", outcome.body, outcome.content_type,
                       None if reported is None else json.dumps(list(reported)),
                       row["reserved_tokens"] if reported is None else sum(reported),
                       None if isinstance(outcome, RawReceived) else outcome.error_code[:200],
                       None if received else _timestamp(datetime.now(UTC)), write_id)

    async def apply_life_write(self, write_id: str, plan: LifePlan, *, now: datetime,
                               zone: str | None = None) -> tuple[LifeEntry, ...]:
        if not isinstance(plan, LifePlan):
            raise DataError(DataErrorCode.INVALID_ARGUMENT, "not a life plan")
        diary = rules.check_life_text(LifeKind.DIARY, plan.diary)
        plans = rules.check_life_plans(plan)
        facts = [rules.check_life_text(LifeKind.FACT, text) for text in plan.facts]
        mood = rules.writer_mood(plan, now)
        arc = rules.check_arc_move(plan.arc)
        where = None if zone is None else rules.check_zone(zone)
        with self._tx() as tx:
            row = self._life_write_row(tx, write_id)
            if row["status"] != "received":
                raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"{write_id} is {row['status']}")
            if row["job"] != LifeJob.REVIEW.value:
                raise DataError(DataErrorCode.INVALID_ARGUMENT, f"{write_id} is not a review")
            principal, persona_id, day = PrincipalId(row["owner"]), int(row["persona_id"]), str(row["day"])
            written: list[LifeEntry] = []
            # character-mind §5.2: the kept thoughts the review read that have expired retire now
            for old in rules.expired_thoughts(map(_life_entry, tx.all(
                    f"SELECT {self._LIFE_COLUMNS} FROM life_entries WHERE owner=? AND persona_id=? AND kind='thought'"
                    " AND retired_at IS NULL", principal, persona_id)), now):
                self._retire_life(tx, principal, old, now)
            found = tx.one(f"SELECT {self._LIFE_COLUMNS} FROM life_entries WHERE owner=? AND persona_id=?"
                           " AND kind='diary' AND day=? AND retired_at IS NULL", principal, persona_id, day)
            before = None if found is None else _life_entry(found)
            arc_id, moved = (None, None) if arc is None else self._move_arc(tx, principal, persona_id, day, arc,
                                                                            write_id, now)
            if moved is not None:
                written.append(moved)
            if before is None:
                written.append(self._new_life(tx, principal, LifeEntry(
                    0, persona_id, LifeKind.DIARY, day, diary, LifeOrigin.WRITER, 1, now, now, source=write_id,
                    arc_id=arc_id)))
            elif before.origin is not LifeOrigin.USER:  # §2: one diary a day; the user's stays
                written.append(self._rewrite_life(tx, principal, dataclasses.replace(
                    before, text=diary, origin=LifeOrigin.WRITER, revision=before.revision + 1, updated_at=now,
                    source=write_id, arc_id=arc_id)))
            started = None if arc is None or arc.op is not ArcOp.START or moved is None else moved.entry_id
            open_arcs = self._open_arcs(tx, principal, persona_id)
            sleeps = [] if where is None else [e for e in self._plans(tx, principal, persona_id) if e.sleep_slot]
            for plan_day, text, at, project, minutes in plans:
                if where is not None and rules.covers_sleep(plan_day, at, minutes, sleeps, where):
                    continue  # character-day §18.7: a plan over a sleep slot is left out alone
                project_id = rules.plan_project(project, at, open_arcs, started)
                written.append(self._new_life(tx, principal, LifeEntry(
                    0, persona_id, LifeKind.PLAN, plan_day, text, LifeOrigin.WRITER, 1, now, now, source=write_id,
                    time_of_day=at, arc_id=project_id, minutes=minutes,
                    plan_kind=PlanKind.OWN if project_id is None else PlanKind.WORK)))
            for text in facts:
                added = self._add_fact(tx, principal, LifeEntry(
                    0, persona_id, LifeKind.FACT, None, text, LifeOrigin.WRITER, 1, now, now, source=write_id),
                    dedupe=True)
                if isinstance(added, LifeEntry):
                    written.append(added)
            if mood is not None:  # character-state §2: the day's end mood, kept unless a later one is stored
                self._keep_mood(tx, principal, dataclasses.replace(mood, persona_id=persona_id))
            tx.execute("UPDATE life_writes SET status='applied', finished_at=? WHERE write_id=?", _timestamp(now),
                       write_id)
            return tuple(written)

    async def add_events(self, write_id: str, events: Sequence[WorldEvent], *, now: datetime) -> tuple[LifeEntry, ...]:
        checked = [rules.check_world_event(event) for event in events]
        with self._tx() as tx:
            row = self._life_write_row(tx, write_id)
            if row["status"] != "received":
                raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"{write_id} is {row['status']}")
            if row["job"] != LifeJob.WORLD.value:
                raise DataError(DataErrorCode.INVALID_ARGUMENT, f"{write_id} is not the world's")
            principal, persona_id, day = PrincipalId(row["owner"]), int(row["persona_id"]), str(row["day"])
            found = tx.one("SELECT COUNT(*) AS n FROM life_entries WHERE owner=? AND persona_id=? AND kind='event'"
                           " AND day=? AND retired_at IS NULL", principal, persona_id, day)
            room = MAX_EVENTS_PER_DAY - (0 if found is None else int(found["n"]))
            kept = tuple(self._new_life(tx, principal, rules.world_entry(persona_id, day, event, write_id, now))
                         for event in checked[:max(0, room)])
            tx.execute("UPDATE life_writes SET status='applied', finished_at=? WHERE write_id=?", _timestamp(now),
                       write_id)
            return kept

    async def apply_settle_write(self, write_id: str, facts: Sequence[str], *,
                                 now: datetime) -> tuple[LifeEntry, ...]:
        texts = [rules.check_life_text(LifeKind.FACT, text) for text in facts]
        with self._tx() as tx:
            row = self._life_write_row(tx, write_id)
            if row["status"] != "received":
                raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"{write_id} is {row['status']}")
            if row["job"] != LifeJob.SETTLE.value:
                raise DataError(DataErrorCode.INVALID_ARGUMENT, f"{write_id} does not settle facts")
            principal, persona_id = PrincipalId(row["owner"]), int(row["persona_id"])
            kept: list[LifeEntry] = []
            for text in texts:
                added = self._add_fact(tx, principal, LifeEntry(
                    0, persona_id, LifeKind.FACT, None, text, LifeOrigin.WRITER, 1, now, now, source=write_id),
                    dedupe=True)
                if isinstance(added, LifeEntry):
                    kept.append(added)
            tx.execute("UPDATE life_writes SET status='applied', finished_at=? WHERE write_id=?", _timestamp(now),
                       write_id)
            return tuple(kept)

    def _move_arc(self, tx: _Tx, principal: PrincipalId, persona_id: int, day: str, move: ArcMove, write_id: str,
                  now: datetime) -> tuple[int | None, LifeEntry | None]:
        """character-world §4: the arc a write's move names for the day's diary and the arc it
        wrote (a start or a close; an advance writes none), or (None, None) when the move is left
        out — a start past MAX_OPEN_ARCS or titled as an open arc, or a move of what is not an
        open arc of the persona."""
        arcs = self._open_arcs(tx, principal, persona_id)
        if move.op is ArcOp.START:
            if not rules.arc_starts(move, arcs):
                return None, None
            assert move.goal is not None
            started = self._new_life(tx, principal, LifeEntry(
                0, persona_id, LifeKind.ARC, day, move.goal, LifeOrigin.WRITER, 1, now, now, source=write_id,
                title=move.title))
            return started.entry_id, started
        target = rules.arc_moved(move, arcs)
        if target is None:
            return None, None
        if move.op is ArcOp.ADVANCE:
            return target.entry_id, None
        return target.entry_id, self._rewrite_life(tx, principal, dataclasses.replace(
            target, revision=target.revision + 1, updated_at=now, end_day=day, ending=move.ending))

    def _open_arcs(self, tx: _Tx, principal: PrincipalId, persona_id: int) -> list[LifeEntry]:
        return [_life_entry(r) for r in tx.all(
            f"SELECT {self._LIFE_COLUMNS} FROM life_entries WHERE owner=? AND persona_id=? AND kind='arc'"
            " AND retired_at IS NULL AND end_day IS NULL", principal, persona_id)]

    async def apply_work_write(self, write_id: str, piece: WorkPiece, *, now: datetime,
                               draft_read: tuple[int, int] | None = None) -> tuple[LifeEntry, ...]:
        piece = rules.check_work_piece(piece)
        with self._tx() as tx:
            row = self._life_write_row(tx, write_id)
            if row["status"] != "received":
                raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"{write_id} is {row['status']}")
            if row["job"] != LifeJob.WORK_END.value:
                raise DataError(DataErrorCode.INVALID_ARGUMENT, f"{write_id} is not a work's end")
            principal, persona_id = PrincipalId(row["owner"]), int(row["persona_id"])
            plan_row = None if row["plan_id"] is None else tx.one(
                f"SELECT {self._LIFE_COLUMNS} FROM life_entries WHERE owner=? AND entry_id=? AND retired_at IS NULL",
                principal, row["plan_id"])
            plan = None if plan_row is None else _life_entry(plan_row)
            arcs = self._open_arcs(tx, principal, persona_id)
            pieces = [] if plan is None else [_life_entry(r) for r in tx.all(
                f"SELECT {self._LIFE_COLUMNS} FROM life_entries WHERE owner=? AND kind='work' AND plan_id=?"
                " AND retired_at IS NULL", principal, plan.entry_id)]
            project = rules.work_project(plan, persona_id, arcs, pieces)
            assert plan is not None
            draft = next((p for p in pieces if p.draft), None)  # character-mind §4.4: the start step's
            rules.check_draft_read(draft, draft_read)  # review P0-3: never over the user's newer edit
            made = (self._new_life(tx, principal, rules.work_entry(plan, project, piece, write_id, now))
                    if draft is None else self._rewrite_life(tx, principal, rules.finished(draft, piece, write_id, now)))
            moved = self._rewrite_life(tx, principal, rules.worked_on(project, plan, piece, now))
            tx.execute("UPDATE life_writes SET status='applied', finished_at=? WHERE write_id=?", _timestamp(now),
                       write_id)
            return made, moved

    async def apply_work_start(self, write_id: str, draft: WorkDraft, *, now: datetime) -> LifeEntry:
        draft = rules.check_work_draft(draft)
        with self._tx() as tx:
            row = self._life_write_row(tx, write_id)
            if row["status"] != "received":
                raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"{write_id} is {row['status']}")
            if row["job"] != LifeJob.WORK_START.value:
                raise DataError(DataErrorCode.INVALID_ARGUMENT, f"{write_id} is not a work's start")
            principal, persona_id = PrincipalId(row["owner"]), int(row["persona_id"])
            plan = _life_entry(found) if (found := tx.one(
                f"SELECT {self._LIFE_COLUMNS} FROM life_entries WHERE owner=? AND entry_id=? AND retired_at IS NULL",
                principal, row["plan_id"])) is not None else None
            pieces = [] if plan is None else [_life_entry(r) for r in tx.all(
                f"SELECT {self._LIFE_COLUMNS} FROM life_entries WHERE owner=? AND kind='work' AND plan_id=?"
                " AND retired_at IS NULL", principal, plan.entry_id)]
            if pieces:
                raise DataError(DataErrorCode.INVALID_ARGUMENT, "the plan has a piece already")
            project = rules.work_project(plan, persona_id, self._open_arcs(tx, principal, persona_id), pieces)
            assert plan is not None
            made = self._new_life(tx, principal, rules.draft_entry(plan, project, draft, write_id, now))
            tx.execute("UPDATE life_writes SET status='applied', finished_at=? WHERE write_id=?", _timestamp(now),
                       write_id)
            return made

    async def fail_life_write(self, write_id: str, error: str) -> None:
        with self._tx() as tx:
            self._life_write_row(tx, write_id)
            tx.execute("UPDATE life_writes SET status='failed', error=?, finished_at=?,"
                       " charged_tokens=CASE WHEN status='prepared' THEN 0 ELSE charged_tokens END"
                       " WHERE write_id=? AND status IN ('prepared', 'dispatched', 'received')",
                       error[:200], _timestamp(datetime.now(UTC)), write_id)

    async def interrupt_life_writes(self) -> int:
        with self._tx() as tx:
            return tx.execute("UPDATE life_writes SET status='interrupted', error='interrupted', finished_at=?,"
                              " charged_tokens=CASE WHEN status='prepared' THEN 0 ELSE charged_tokens END"
                              " WHERE status IN ('prepared', 'dispatched', 'received')",
                              _timestamp(datetime.now(UTC))).rowcount

    async def list_life_writes(self, principal: PrincipalId, *, limit: int = 20) -> tuple[LifeWrite, ...]:
        with self._tx() as tx:
            return tuple(_life_write(r) for r in tx.all("SELECT * FROM life_writes WHERE owner=? ORDER BY seq DESC"
                                                        " LIMIT ?", principal, max(0, limit)))

    async def session_writes(self, principal: PrincipalId, conversation_id: ConversationId) -> tuple[LifeWrite, ...]:
        with self._tx() as tx:
            return tuple(_life_write(r) for r in tx.all("SELECT * FROM life_writes WHERE owner=? AND conversation_id=?"
                                                        " ORDER BY seq", principal, conversation_id))

    # --- SleepStore (character-day v0.1 §5, §18.2) -------------------------------------------

    @staticmethod
    def _day_state(tx: _Tx, principal: PrincipalId, persona_id: int) -> DayState:
        row = tx.one("SELECT * FROM day_state WHERE owner=? AND persona_id=?", principal, persona_id)
        if row is None:
            return DayState(persona_id)
        return DayState(persona_id, _moment(row["sleep_requested_at"]), _moment(row["woken_at"]),
                        _moment(row["last_closing_at"]), codec.day_before_from(row["before"]))

    @staticmethod
    def _put_day_state(tx: _Tx, principal: PrincipalId, state: DayState) -> None:
        def stamp(moment: datetime | None) -> str | None:
            return None if moment is None else _timestamp(moment)

        tx.execute("INSERT INTO day_state (owner, persona_id, sleep_requested_at, woken_at, last_closing_at, before)"
                   " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (owner, persona_id) DO UPDATE SET"
                   " sleep_requested_at=excluded.sleep_requested_at, woken_at=excluded.woken_at,"
                   " last_closing_at=excluded.last_closing_at, before=excluded.before",
                   principal, rules.check_life_persona(state.persona_id), stamp(state.sleep_requested_at),
                   stamp(state.woken_at), stamp(state.last_closing_at), codec.day_before(state.before))

    @staticmethod
    def _sleep_row(tx: _Tx, principal: PrincipalId, sleep_id: object) -> SleepRecord:
        row = None if isinstance(sleep_id, bool) or not isinstance(sleep_id, int) else tx.one(
            "SELECT * FROM sleeps WHERE owner=? AND sleep_id=?", principal, sleep_id)
        if row is None:
            raise DataError(DataErrorCode.NOT_FOUND, f"no sleep {sleep_id}")
        return _sleep(row)

    async def open_sleep(self, principal: PrincipalId, persona_id: int, *, at: datetime, how: FellBy) -> SleepRecord:
        persona_id = rules.check_life_persona(persona_id)
        how = FellBy(how)
        with self._tx() as tx:
            if tx.one("SELECT 1 FROM sleeps WHERE owner=? AND persona_id=? AND woke_at IS NULL", principal,
                      persona_id) is not None:
                raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, "it is asleep already")
            cursor = tx.execute("INSERT INTO sleeps (owner, persona_id, fell_at, how_fell) VALUES (?, ?, ?, ?)",
                                principal, persona_id, _timestamp(at), how.value)
            assert cursor.lastrowid is not None
            return self._sleep_row(tx, principal, int(cursor.lastrowid))

    async def current_sleep(self, principal: PrincipalId, persona_id: int) -> SleepRecord | None:
        with self._tx() as tx:
            row = tx.one("SELECT * FROM sleeps WHERE owner=? AND persona_id=? ORDER BY sleep_id DESC LIMIT 1",
                         principal, rules.check_life_persona(persona_id))
            return None if row is None else _sleep(row)

    async def end_sleep(self, principal: PrincipalId, sleep_id: int, *, at: datetime, how: WokeBy) -> SleepRecord:
        how = WokeBy(how)
        with self._tx() as tx:
            if not self._sleep_row(tx, principal, sleep_id).asleep:
                raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"sleep {sleep_id} ended already")
            tx.execute("UPDATE sleeps SET woke_at=?, how_woke=? WHERE sleep_id=?", _timestamp(at), how.value, sleep_id)
            return self._sleep_row(tx, principal, sleep_id)

    async def count_wake(self, principal: PrincipalId, sleep_id: int, *, count: int, at: datetime,
                         answer: AsleepAnswer, message: bool) -> SleepRecord:
        answer = AsleepAnswer(answer)
        with self._tx() as tx:
            sleep = self._sleep_row(tx, principal, sleep_id)
            if not sleep.asleep:
                raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"sleep {sleep_id} ended already")
            first = sleep.first_message_at if sleep.first_message_at is not None or not message else at
            tx.execute("UPDATE sleeps SET count=?, count_at=?, messages=?, mumbles=?, first_message_at=?,"
                       " last_message_at=? WHERE sleep_id=?", max(0, int(count)), _timestamp(at),
                       sleep.messages + int(message), sleep.mumbles + int(answer is AsleepAnswer.MUMBLE),
                       None if first is None else _timestamp(first),
                       _timestamp(at) if message else
                       (None if sleep.last_message_at is None else _timestamp(sleep.last_message_at)), sleep_id)
            return self._sleep_row(tx, principal, sleep_id)

    async def recent_sleeps(self, principal: PrincipalId, persona_id: int, *, limit: int) -> tuple[SleepRecord, ...]:
        with self._tx() as tx:
            return tuple(_sleep(r) for r in tx.all(
                "SELECT * FROM sleeps WHERE owner=? AND persona_id=? ORDER BY sleep_id DESC LIMIT ?", principal,
                rules.check_life_persona(persona_id), max(0, limit)))

    async def note_closing(self, principal: PrincipalId, persona_id: int, sleep_id: int, *, at: datetime,
                           before: DayBefore | None) -> None:
        with self._tx() as tx:
            self._sleep_row(tx, principal, sleep_id)
            tx.execute("UPDATE sleeps SET closed_day=1 WHERE sleep_id=?", sleep_id)
            state = self._day_state(tx, principal, rules.check_life_persona(persona_id))
            self._put_day_state(tx, principal, dataclasses.replace(state, last_closing_at=at, before=before))

    async def read_day_state(self, principal: PrincipalId, persona_id: int) -> DayState:
        with self._tx() as tx:
            return self._day_state(tx, principal, rules.check_life_persona(persona_id))

    async def write_day_state(self, principal: PrincipalId, state: DayState) -> None:
        if not isinstance(state, DayState):
            raise DataError(DataErrorCode.INVALID_ARGUMENT, "not a day state")
        with self._tx() as tx:
            self._put_day_state(tx, principal, state)

    # --- BondStore (character-bond v0.1 §2–§4) ---------------------------------------------

    def _live_bond(self, tx: _Tx, principal: PrincipalId, persona_id: int) -> list[LifeEntry]:
        return [_life_entry(r) for r in tx.all(
            f"SELECT {self._LIFE_COLUMNS} FROM life_entries WHERE owner=? AND persona_id=? AND kind='bond'"
            " AND retired_at IS NULL", principal, persona_id)]

    def _add_bond(self, tx: _Tx, principal: PrincipalId, entry: LifeEntry, *, dedupe: bool) -> LifeEntry | str:
        """§2: a new bond entry within MAX_BOND_ENTRIES, or why not (``duplicate``, ``full``)."""
        entries = self._live_bond(tx, principal, entry.persona_id)
        if dedupe and rules.same_bond(entry.tag, entry.text, entries):
            return "duplicate"
        fits, retire = rules.bond_room(entries)
        if not fits:
            return "full"
        if retire is not None:
            self._retire_life(tx, principal, retire, entry.created_at)
        return self._new_life(tx, principal, entry)

    @staticmethod
    def _stored_standing(tx: _Tx, principal: PrincipalId, persona_id: int) -> BondStanding:
        return _standing(persona_id, tx.one("SELECT stage, since, reason, revision FROM bond_stages WHERE owner=?"
                                            " AND persona_id=?", principal, persona_id))

    @staticmethod
    def _keep_standing(tx: _Tx, principal: PrincipalId, standing: BondStanding) -> None:
        assert standing.since is not None and standing.reason is not None
        tx.execute("INSERT INTO bond_stages (owner, persona_id, stage, since, reason, revision)"
                   " VALUES (?, ?, ?, ?, ?, ?)"
                   " ON CONFLICT (owner, persona_id) DO UPDATE SET stage=excluded.stage, since=excluded.since,"
                   " reason=excluded.reason, revision=excluded.revision", principal, standing.persona_id,
                   standing.stage.value, _timestamp(standing.since), standing.reason, standing.revision)

    async def list_bond(self, principal: PrincipalId, persona_id: int) -> tuple[LifeEntry, ...]:
        persona_id = rules.check_life_persona(persona_id)
        with self._tx() as tx:
            return rules.order_bond(self._live_bond(tx, principal, persona_id))

    async def add_bond(self, principal: PrincipalId, persona_id: int, tag: BondTag, text: str,
                       month_day: str | None = None) -> LifeEntry:
        persona_id = rules.check_life_persona(persona_id)
        tag = rules.check_bond_tag(tag)
        text = rules.check_life_text(LifeKind.BOND, text)
        month_day = rules.check_month_day(tag, month_day)
        now = datetime.now(UTC)
        with self._tx() as tx:
            added = self._add_bond(tx, principal, LifeEntry(0, persona_id, LifeKind.BOND, None, text, LifeOrigin.USER,
                                                            1, now, now, tag=tag, month_day=month_day), dedupe=False)
            if not isinstance(added, LifeEntry):
                raise DataError(DataErrorCode.LIMIT_EXCEEDED, f"at most {MAX_BOND_ENTRIES} bond entries, all yours")
            return added

    async def edit_bond(self, principal: PrincipalId, entry_id: int, tag: BondTag, text: str,
                        month_day: str | None = None, *, expected_revision: int) -> LifeEntry:
        tag = rules.check_bond_tag(tag)
        text = rules.check_life_text(LifeKind.BOND, text)
        month_day = rules.check_month_day(tag, month_day)
        with self._tx() as tx:
            entry = self._live_entry(tx, principal, entry_id)
            if entry.kind is not LifeKind.BOND:
                raise DataError(DataErrorCode.NOT_FOUND, f"no bond entry {entry_id}")
            if expected_revision != entry.revision:
                raise DataError(DataErrorCode.VERSION_CONFLICT, f"the entry is at revision {entry.revision}")
            return self._rewrite_life(tx, principal, dataclasses.replace(
                entry, tag=tag, text=text, month_day=month_day, origin=LifeOrigin.USER, revision=entry.revision + 1,
                updated_at=datetime.now(UTC)))

    async def apply_bond_changes(self, principal: PrincipalId, persona_id: int, changes: Sequence[BondChange], *,
                                 conversation: ConversationId, source: str, now: datetime) -> tuple[str, ...]:
        persona_id = rules.check_life_persona(persona_id)
        with self._tx() as tx:
            outcomes: list[str] = []
            for change in changes:
                outcome = rules.bond_change_outcome(change, self._live_bond(tx, principal, persona_id))
                if outcome is not None:
                    outcomes.append(outcome)
                    continue
                sequences = tuple(change.sequences)
                if change.op is LifeChangeOp.RETIRE:
                    self._retire_life(tx, principal, self._live_entry(tx, principal, change.entry_id), now)
                    outcomes.append("applied")
                    continue
                tag = rules.check_bond_tag(change.tag)
                text = rules.check_life_text(LifeKind.BOND, change.text)
                month_day = rules.check_month_day(tag, change.month_day)
                if change.op is LifeChangeOp.ADD:
                    added = self._add_bond(tx, principal, LifeEntry(
                        0, persona_id, LifeKind.BOND, None, text, LifeOrigin.CONVERSATION, 1, now, now, conversation,
                        sequences, source, tag, month_day), dedupe=True)
                    outcomes.append("applied" if isinstance(added, LifeEntry) else added)
                    continue
                entry = self._live_entry(tx, principal, change.entry_id)
                self._rewrite_life(tx, principal, dataclasses.replace(
                    entry, tag=tag, text=text, month_day=month_day, origin=LifeOrigin.CONVERSATION,
                    revision=entry.revision + 1, updated_at=now, source_conversation=conversation,
                    source_sequences=sequences, source=source))
                outcomes.append("applied")
            return tuple(outcomes)

    async def bond_stage(self, principal: PrincipalId, persona_id: int) -> BondStanding:
        persona_id = rules.check_life_persona(persona_id)
        with self._tx() as tx:
            return self._stored_standing(tx, principal, persona_id)

    async def read_bond(self, principal: PrincipalId, persona_id: int, *, now: datetime,
                        zone: str) -> BondSnapshot:
        persona_id = rules.check_life_persona(persona_id)
        where = rules.check_zone(zone)
        with self._tx() as tx:
            standing = self._stored_standing(tx, principal, persona_id)
            entries = rules.order_bond(self._live_bond(tx, principal, persona_id))
            first = tx.one("SELECT MIN(created_at) AS at FROM conversations WHERE owner=?", principal)
            met = None if first is None or first["at"] is None else \
                datetime.fromisoformat(first["at"]).astimezone(where).date()
            today = now.astimezone(where).date()
            moved = rules.settle_stage(standing, entries, met, today, where, now)
            if moved is not standing:
                self._keep_standing(tx, principal, moved)
            return BondSnapshot(moved, None if met is None else met.isoformat(),
                                0 if met is None else together_days(met, today), entries,
                                anniversaries_on(today, met, entries))

    async def set_bond_stage(self, principal: PrincipalId, persona_id: int, stage: BondStage, *,
                             expected_revision: int) -> BondStanding:
        persona_id = rules.check_life_persona(persona_id)
        if not isinstance(stage, BondStage):
            raise DataError(DataErrorCode.INVALID_ARGUMENT, f"no stage {stage!r}")
        with self._tx() as tx:
            current = self._stored_standing(tx, principal, persona_id)
            if expected_revision != current.revision:
                raise DataError(DataErrorCode.VERSION_CONFLICT, f"the stage is at revision {current.revision}")
            chosen = BondStanding(persona_id, stage, datetime.now(UTC), "user", current.revision + 1)
            self._keep_standing(tx, principal, chosen)
            return chosen

    # --- MoodStore (character-state v0.1 §2) -----------------------------------------------

    @staticmethod
    def _keep_mood(tx: _Tx, principal: PrincipalId, mood: HimariMood) -> HimariMood:
        """``mood`` stored unless the stored one was set after it; the one kept."""
        stored = _mood(mood.persona_id, tx.one("SELECT mood, set_at, source, reason FROM himari_moods WHERE owner=?"
                                               " AND persona_id=?", principal, mood.persona_id))
        kept = rules.kept_mood(stored, mood)
        if kept is mood:
            tx.execute("INSERT INTO himari_moods (owner, persona_id, mood, set_at, source, reason)"
                       " VALUES (?, ?, ?, ?, ?, ?)"
                       " ON CONFLICT (owner, persona_id) DO UPDATE SET mood=excluded.mood, set_at=excluded.set_at,"
                       " source=excluded.source, reason=excluded.reason", principal, mood.persona_id, mood.mood,
                       _timestamp(mood.set_at), mood.source.value, mood.reason)
        return kept

    async def read_mood(self, principal: PrincipalId, persona_id: int) -> HimariMood | None:
        persona_id = rules.check_life_persona(persona_id)
        with self._tx() as tx:
            return _mood(persona_id, tx.one("SELECT mood, set_at, source, reason FROM himari_moods WHERE owner=?"
                                            " AND persona_id=?", principal, persona_id))

    async def set_mood(self, principal: PrincipalId, persona_id: int, mood: str, *, source: MoodSource,
                       at: datetime, reason: str | None = None) -> HimariMood:
        checked = rules.check_mood(persona_id, mood, source, at, reason)
        with self._tx() as tx:
            return self._keep_mood(tx, principal, checked)

    # --- MemoryStore (V3-P71) ---------------------------------------------------------

    _MEMORY_COLUMNS = ("memory_id, category, text, origin, source_conversation, source_run, source_call, revision,"
                       " created_at, updated_at, due_at, due_instant, valid_until, valid_instant, source_pass,"
                       " source_item, user_approved_at, remind_at, remind_instant, remind_state, reminded_at")

    def _read_memory(self, tx: _Tx, principal: PrincipalId, memory_id: object) -> Memory:
        """A live note: a retired one (V3-P85 §5.4) is not found."""
        row = None if not isinstance(memory_id, int) else tx.one(
            f"SELECT {self._MEMORY_COLUMNS} FROM memories WHERE owner=? AND memory_id=? AND retired_at IS NULL",
            principal, memory_id)
        if row is None:
            raise DataError(DataErrorCode.NOT_FOUND, f"no note m{memory_id}")
        return _memory(row)

    @staticmethod
    def _check_room(tx: _Tx, principal: PrincipalId) -> None:
        row = tx.one("SELECT COUNT(*) AS n FROM memories WHERE owner=? AND retired_at IS NULL", principal)
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
        """Every note write. V3-P87 §5.3: only a loop keeps a reminder; a reminder
        written anew (another moment, or gone) starts its delivery afresh —
        scheduled (none without one), no claim, no attempt — and one written as
        it was keeps how it was delivered."""
        note = rules.unreminded(note)
        call = note.source_call

        def moment(value: datetime | None) -> str | None:
            return None if value is None else _timestamp(value)

        before = tx.one("SELECT remind_at, remind_instant FROM memories WHERE owner=? AND memory_id=?",
                        principal, note.memory_id)
        anew = before is None or (before["remind_at"], before["remind_instant"]) != (
            note.remind_at, moment(note.remind_instant))

        tx.execute("INSERT INTO memories (owner, memory_id, category, text, origin, source_conversation, source_run,"
                   " source_call, revision, created_at, updated_at, due_at, due_instant, valid_until, valid_instant,"
                   " source_pass, source_item, user_approved_at)"
                   " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                   " ON CONFLICT (owner, memory_id) DO UPDATE SET category=excluded.category, text=excluded.text,"
                   " origin=excluded.origin, source_conversation=excluded.source_conversation,"
                   " source_run=excluded.source_run, source_call=excluded.source_call, revision=excluded.revision,"
                   " updated_at=excluded.updated_at, due_at=excluded.due_at, due_instant=excluded.due_instant,"
                   " valid_until=excluded.valid_until, valid_instant=excluded.valid_instant,"
                   " source_pass=excluded.source_pass, source_item=excluded.source_item,"
                   " user_approved_at=excluded.user_approved_at",
                   principal, note.memory_id, note.category.value, note.text, note.origin.value,
                   note.source_conversation, None if call is None else call.run_id,
                   None if call is None else call.call_id, note.revision, _timestamp(note.created_at),
                   _timestamp(note.updated_at), note.due_at, moment(note.due_instant), note.valid_until,
                   moment(note.valid_instant), note.source_pass, note.source_item, moment(note.user_approved_at))
        if anew:
            tx.execute("UPDATE memories SET remind_at=?, remind_instant=?, remind_state=?, remind_wake=NULL,"
                       " remind_attempts=0, reminded_at=NULL WHERE owner=? AND memory_id=?",
                       note.remind_at, moment(note.remind_instant), None if note.remind_at is None else "scheduled",
                       principal, note.memory_id)

    def _apply_memory(self, tx: _Tx, scope: TrustedScope, call: CallRef, change: MemoryChange) -> None:
        """A Tool's change, in the commit's transaction: its errors roll the commit back."""
        change = rules.check_memory_change(change)
        principal, now = scope.principal_id, datetime.now(UTC)
        if change.kind is MemoryChangeKind.SAVE and change.expected_revision is None:
            exists = tx.one("SELECT 1 FROM memories WHERE owner=? AND memory_id=?", principal, change.memory_id)  # retired too
            if change.memory_id > self._last_memory_id(tx, principal) or exists is not None:
                raise DataError(DataErrorCode.INVALID_ARGUMENT, f"m{change.memory_id} was not given out for a new note")
            self._check_room(tx, principal)
            assert change.category is not None and change.text is not None
            self._write_memory(tx, principal, Memory(change.memory_id, change.category, change.text,
                                                     MemoryOrigin.MODEL, scope.conversation_id, call, 1, now, now,
                                                     due_at=change.due_at, due_instant=change.due_instant,
                                                     valid_until=change.valid_until, valid_instant=change.valid_instant,
                                                     remind_at=change.remind_at,
                                                     remind_instant=change.remind_instant))
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
            source_conversation=scope.conversation_id, source_call=call, revision=note.revision + 1, updated_at=now,
            source_pass=None, source_item=None, user_approved_at=None, due_at=change.due_at,
            due_instant=change.due_instant, valid_until=change.valid_until, valid_instant=change.valid_instant,
            remind_at=change.remind_at, remind_instant=change.remind_instant))

    async def list_memories(self, principal: PrincipalId) -> tuple[Memory, ...]:
        with self._tx() as tx:
            rows = tx.all(f"SELECT {self._MEMORY_COLUMNS} FROM memories WHERE owner=? AND retired_at IS NULL",
                          principal)
            return rules.order_memories(_memory(r) for r in rows)

    async def read_memory(self, principal: PrincipalId, memory_id: int) -> Memory:
        with self._tx() as tx:
            return self._read_memory(tx, principal, memory_id)

    async def allocate_memory_id(self, principal: PrincipalId) -> int:
        with self._tx() as tx:
            return self._next_memory_id(tx, principal)

    async def save_memory(self, principal: PrincipalId, category: MemoryCategory, text: str, *,
                          memory_id: int | None = None, expected_revision: int | None = None,
                          dates: NoteDates | None = None) -> Memory:
        category, now = rules.check_memory_category(category), datetime.now(UTC)
        text = rules.check_note_text(category, text)
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
            note = rules.dated(note, dates)
            self._write_memory(tx, principal, note)
            return self._read_memory(tx, principal, note.memory_id)  # with its reminder's state

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
                                       revision=current.revision + 1, updated_at=datetime.now(UTC),
                                       due_at=before.due_at, due_instant=before.due_instant,
                                       valid_until=before.valid_until, valid_instant=before.valid_instant,
                                       remind_at=before.remind_at, remind_instant=before.remind_instant,
                                       source_pass=before.source_pass, source_item=before.source_item,
                                       user_approved_at=before.user_approved_at)
            self._write_memory(tx, principal, note)
            return self._read_memory(tx, principal, note.memory_id)

    # --- DeviceStore (V3-P83 §4) ----------------------------------------------------

    _DEVICE_COLUMNS = "device_id, name, created_at, pairing_expires_at, paired_at, last_seen_at"

    @staticmethod
    def _live_names(tx: _Tx, principal: PrincipalId, *, besides: str | None = None) -> list[str]:
        rows = tx.all("SELECT device_id, name FROM devices WHERE owner=? AND revoked_at IS NULL", principal)
        return [r["name"] for r in rows if r["device_id"] != besides]

    def _read_device(self, tx: _Tx, principal: PrincipalId, device_id: object) -> Device:
        row = None if not isinstance(device_id, str) else tx.one(
            f"SELECT {self._DEVICE_COLUMNS} FROM devices WHERE owner=? AND device_id=? AND revoked_at IS NULL",
            principal, device_id)
        if row is None:
            raise DataError(DataErrorCode.NOT_FOUND, f"no device {device_id}")
        return _device(row)

    async def add_device(self, principal: PrincipalId, name: str, pairing_hash: bytes,
                         expires_at: datetime) -> Device:
        name, now = rules.check_device_name(name), datetime.now(UTC)
        with self._tx() as tx:
            tx.execute("DELETE FROM devices WHERE owner=? AND paired_at IS NULL AND revoked_at IS NULL", principal)
            names = self._live_names(tx, principal)
            rules.check_device_name_free(name, names)
            if len(names) >= MAX_DEVICES:
                raise DataError(DataErrorCode.LIMIT_EXCEEDED, f"{MAX_DEVICES} devices at most")
            row = tx.one("SELECT last_id FROM device_ids WHERE owner=?", principal)
            number = (0 if row is None else int(row["last_id"])) + 1
            tx.execute("INSERT INTO device_ids (owner, last_id) VALUES (?, ?)"
                       " ON CONFLICT (owner) DO UPDATE SET last_id=excluded.last_id", principal, number)
            device = Device(f"d{number}", name, now, expires_at, None, None)
            tx.execute("INSERT INTO devices (owner, device_id, name, pairing_hash, pairing_expires_at, created_at)"
                       " VALUES (?, ?, ?, ?, ?, ?)", principal, device.device_id, name, pairing_hash,
                       _timestamp(expires_at), _timestamp(now))
            return device

    async def pair_device(self, pairing_hash: bytes, token_hash: bytes, now: datetime) -> DevicePairing:
        with self._tx() as tx:
            row = tx.one(f"SELECT owner, token_hash, {self._DEVICE_COLUMNS} FROM devices"
                         " WHERE pairing_hash=? AND revoked_at IS NULL", pairing_hash)
            if row is None:
                return DevicePairing(PairingResult.INVALID)
            if datetime.fromisoformat(row["pairing_expires_at"]) <= now:
                return DevicePairing(PairingResult.EXPIRED)
            principal = PrincipalId(row["owner"])
            if row["paired_at"] is not None:
                if row["token_hash"] != token_hash:
                    return DevicePairing(PairingResult.USED)
                return DevicePairing(PairingResult.RESENT, principal, _device(row))
            if tx.one("SELECT 1 FROM devices WHERE token_hash=?", token_hash) is not None:
                return DevicePairing(PairingResult.TOKEN_IN_USE)
            tx.execute("UPDATE devices SET token_hash=?, paired_at=? WHERE owner=? AND device_id=?",
                       token_hash, _timestamp(now), principal, row["device_id"])
            return DevicePairing(PairingResult.PAIRED, principal, self._read_device(tx, principal, row["device_id"]))

    async def record_pairing_failure(self, now: datetime) -> None:
        live = ("paired_at IS NULL AND revoked_at IS NULL AND pairing_hash IS NOT NULL"
                " AND pairing_expires_at > ?")
        with self._tx() as tx:
            tx.execute(f"UPDATE devices SET pairing_failures=pairing_failures+1 WHERE {live}", _timestamp(now))
            tx.execute("DELETE FROM devices WHERE paired_at IS NULL AND pairing_failures >= ?", PAIRING_FAILURE_LIMIT)

    async def device_for_token(self, token_hash: bytes) -> tuple[PrincipalId, Device] | None:
        with self._tx() as tx:
            row = tx.one(f"SELECT owner, {self._DEVICE_COLUMNS} FROM devices"
                         " WHERE token_hash=? AND revoked_at IS NULL AND paired_at IS NOT NULL", token_hash)
            return None if row is None else (PrincipalId(row["owner"]), _device(row))

    async def list_devices(self, principal: PrincipalId) -> tuple[Device, ...]:
        with self._tx() as tx:
            rows = tx.all(f"SELECT {self._DEVICE_COLUMNS} FROM devices WHERE owner=? AND revoked_at IS NULL"
                          " ORDER BY rowid", principal)
            return tuple(_device(r) for r in rows)

    async def rename_device(self, principal: PrincipalId, device_id: str, name: str) -> Device:
        name = rules.check_device_name(name)
        with self._tx() as tx:
            self._read_device(tx, principal, device_id)
            rules.check_device_name_free(name, self._live_names(tx, principal, besides=device_id))
            tx.execute("UPDATE devices SET name=? WHERE owner=? AND device_id=?", name, principal, device_id)
            return self._read_device(tx, principal, device_id)

    async def revoke_device(self, principal: PrincipalId, device_id: str) -> None:
        with self._tx() as tx:
            self._read_device(tx, principal, device_id)
            tx.execute("UPDATE devices SET revoked_at=?, token_hash=NULL, pairing_hash=NULL, pairing_expires_at=NULL"
                       " WHERE owner=? AND device_id=?", _timestamp(datetime.now(UTC)), principal, device_id)

    async def touch_device(self, principal: PrincipalId, device_id: str, at: datetime) -> None:
        with self._tx() as tx:
            tx.execute("UPDATE devices SET last_seen_at=? WHERE owner=? AND device_id=? AND revoked_at IS NULL",
                       _timestamp(at), principal, device_id)

    async def record_input(self, principal: PrincipalId, device_id: str, at: datetime) -> None:
        with self._tx() as tx:
            row = tx.one("SELECT last_input_at FROM devices WHERE owner=? AND device_id=? AND revoked_at IS NULL",
                         principal, device_id)
            known = None if row is None else _moment(row["last_input_at"])
            if row is None or (known is not None and known >= at):
                return
            tx.execute("UPDATE devices SET last_input_at=? WHERE owner=? AND device_id=?",
                       _timestamp(at), principal, device_id)

    async def read_inputs(self, principal: PrincipalId) -> Mapping[str, datetime]:
        with self._tx() as tx:
            rows = tx.all("SELECT device_id, last_input_at FROM devices WHERE owner=? AND revoked_at IS NULL"
                          " AND last_input_at IS NOT NULL", principal)
            return {r["device_id"]: datetime.fromisoformat(r["last_input_at"]) for r in rows}

    # --- WakeStore (V3-P87) -------------------------------------------------------------

    async def begin_wake(self, scope: TrustedScope, reasons: Sequence[WakeReason], device_id: str, *,
                         keys: Sequence[str] = (), reminders: Sequence[int] = (),
                         interface: InterfaceSnapshot | None = None) -> ConversationEvent:
        rules.check_wake(reasons, device_id, keys, reminders)
        rules.check_interface(interface, MessageOrigin(OriginKind.DEVICE, device_id))
        with self._tx() as tx:
            tx.authorized(scope)
            now = _timestamp(datetime.now(UTC))
            wake_id = tx.new_id("wake")
            for key in keys:
                if tx.one("SELECT 1 FROM wake_keys WHERE owner=? AND key=?", scope.principal_id, key) is not None:
                    raise DataError(DataErrorCode.DUPLICATE_IDENTITY, f"wake key {key!r} was taken")
                tx.execute("INSERT INTO wake_keys (owner, key, wake_id, created_at) VALUES (?, ?, ?, ?)",
                           scope.principal_id, key, wake_id, now)
            for memory_id in reminders:
                claimed = tx.execute("UPDATE memories SET remind_state='claimed', remind_wake=? WHERE owner=?"
                                     " AND memory_id=? AND remind_state='scheduled'"
                                     " AND retired_at IS NULL", wake_id, scope.principal_id, memory_id)
                if claimed.rowcount != 1:
                    raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"reminder m{memory_id} is not waiting")
            event = self._new_event(tx, scope, Wake(wake_id, tuple(reasons), device_id, interface), None)
            tx.execute("INSERT INTO wakes (wake_id, owner, conversation_id, event_sequence, reasons, device_id,"
                       " status, asked, created_at, ended_at) VALUES (?, ?, ?, ?, ?, ?, 'opened', 0, ?, NULL)",
                       wake_id, scope.principal_id, scope.conversation_id, event.sequence,
                       codec.dumps(codec.wake_reasons(tuple(reasons))), device_id, now)
            return event

    async def end_wake(self, principal: PrincipalId, wake_id: str, status: WakeStatus) -> WakeRecord:
        if status.in_progress:
            raise DataError(DataErrorCode.INVALID_ARGUMENT, "a wake ends answered, cancelled or failed")
        with self._tx() as tx:
            row = tx.one("SELECT * FROM wakes WHERE owner=? AND wake_id=?", principal, wake_id)
            if row is None:
                raise DataError(DataErrorCode.NOT_FOUND, f"no wake {wake_id}")
            was = WakeStatus(row["status"])
            if not was.in_progress or (was is WakeStatus.SPEAKING and status in (WakeStatus.PASSED, WakeStatus.DROPPED)):
                return _wake(row)  # ended already, once; a shown wake never becomes hidden
            now = _timestamp(datetime.now(UTC))
            tx.execute("UPDATE wakes SET status=?, ended_at=? WHERE wake_id=?", status.value, now, wake_id)
            # §5.3 (re-review P2): only reminders still claimed by this very wake
            if status is WakeStatus.ANSWERED:
                tx.execute("UPDATE memories SET remind_state='delivered', reminded_at=?, remind_wake=NULL"
                           " WHERE owner=? AND remind_state='claimed' AND remind_wake=?", now, principal, wake_id)
            else:
                tx.execute("UPDATE memories SET remind_attempts=remind_attempts+1, remind_wake=NULL,"
                           " remind_state=CASE WHEN remind_attempts+1 >= ? THEN 'missed' ELSE 'scheduled' END"
                           " WHERE owner=? AND remind_state='claimed' AND remind_wake=?",
                           REMINDER_ATTEMPTS, principal, wake_id)
            ended = tx.one("SELECT * FROM wakes WHERE wake_id=?", wake_id)
            assert ended is not None
            return _wake(ended)

    async def read_wake(self, principal: PrincipalId, wake_id: str) -> WakeRecord:
        with self._tx() as tx:
            row = tx.one("SELECT * FROM wakes WHERE owner=? AND wake_id=?", principal, wake_id)
            if row is None:
                raise DataError(DataErrorCode.NOT_FOUND, f"no wake {wake_id}")
            return _wake(row)

    async def opened_wakes(self) -> tuple[WakeRecord, ...]:
        with self._tx() as tx:
            return tuple(_wake(r) for r in tx.all("SELECT * FROM wakes WHERE status IN ('opened', 'speaking')"
                                                  " ORDER BY created_at"))

    async def reveal_wake(self, principal: PrincipalId, wake_id: str) -> bool:
        with self._tx() as tx:
            return tx.execute("UPDATE wakes SET status='speaking' WHERE owner=? AND wake_id=? AND status='opened'",
                              principal, wake_id).rowcount == 1

    async def finalize_hidden_run(self, scope: TrustedScope, run_id: RunId, wake_id: str, status: WakeStatus, *,
                                  thought: str | None = None) -> None:
        if status not in (WakeStatus.PASSED, WakeStatus.DROPPED):
            raise DataError(DataErrorCode.INVALID_ARGUMENT, "a hidden wake ends passed or dropped")
        with self._tx() as tx:
            tx.authorized(scope)
            run = self._run_row(tx, scope, run_id)
            if run["status"] == RunStatus.RECEIVED.value:
                self._set_run(tx, run_id, RunStatus.PROJECTED)  # no event: projected_event_id stays NULL
            tx.execute("UPDATE wakes SET status=?, ended_at=?, thought=? WHERE owner=? AND wake_id=?"
                       " AND status='opened'", status.value, _timestamp(datetime.now(UTC)), thought,
                       scope.principal_id, wake_id)

    async def hold_wake_run(self, scope: TrustedScope, wake_id: str, run_id: RunId) -> bool:
        with self._tx() as tx:
            tx.authorized(scope)
            self._run_row(tx, scope, run_id)
            tx.execute("UPDATE wakes SET held_run_id=? WHERE owner=? AND wake_id=? AND conversation_id=?"
                       " AND status='opened' AND held_run_id IS NULL",
                       run_id, scope.principal_id, wake_id, scope.conversation_id)
            row = tx.one("SELECT held_run_id FROM wakes WHERE owner=? AND wake_id=? AND conversation_id=?"
                         " AND status='opened'", scope.principal_id, wake_id, scope.conversation_id)
            return row is not None and row["held_run_id"] == run_id

    def _wake_of_run_tx(self, tx: _Tx, scope: TrustedScope, run_id: RunId) -> WakeRecord | None:
        # PR #167 review P1-2: a Run that only goes on after a tool round is triggered by that
        # round: follow it back. Re-review P1-7, P1-8: a Run that names any Event is decided
        # there — its latest Event is a free wake's, or the Run is no free wake's (a user's
        # message answered beside an older wake's group never becomes that wake's).
        level: list[RunId] = [run_id]
        seen: set[RunId] = set()
        while level and len(seen) < 32:
            ids: list[str] = []
            below: list[RunId] = []
            for current in level:
                if current in seen:
                    continue
                seen.add(current)
                for ref in codec.trigger_refs_from(self._run_row(tx, scope, current)["trigger_refs"]):
                    if isinstance(ref, EventRef):
                        ids.append(ref.event_id)
                    elif isinstance(ref, CompletedGroupRef):
                        below.append(ref.run_id)
            if ids:
                last = tx.one(f"SELECT sequence FROM events WHERE conversation_id=? AND event_id IN"
                              f" ({', '.join('?' * len(ids))}) ORDER BY sequence DESC LIMIT 1",
                              scope.conversation_id, *ids)
                row = None if last is None else tx.one(
                    "SELECT * FROM wakes WHERE conversation_id=? AND event_sequence=?",
                    scope.conversation_id, last["sequence"])
                wake = None if row is None else _wake(row)
                return wake if wake is not None and free_wake(wake.reasons) else None
            level = below
        return None

    async def wake_of_run(self, scope: TrustedScope, run_id: RunId) -> WakeRecord | None:
        with self._tx() as tx:
            tx.authorized(scope)
            return self._wake_of_run_tx(tx, scope, run_id)

    async def unshown_wakes(self, scope: TrustedScope) -> frozenset[int]:
        with self._tx() as tx:
            tx.authorized(scope)
            rows = tx.all("SELECT event_sequence, reasons, held_run_id FROM wakes WHERE conversation_id=?"
                          " AND status='opened'", scope.conversation_id)
            opened = [r for r in rows if free_wake(codec.wake_reasons_from(json.loads(r["reasons"])))]
            held = [r["held_run_id"] for r in opened if r["held_run_id"] is not None]  # V3-P91 §8
            events = tx.all(f"SELECT sequence FROM events WHERE conversation_id=? AND origin_run_id IN"
                            f" ({', '.join('?' * len(held))})", scope.conversation_id, *held) if held else []
            return frozenset({int(r["event_sequence"]) for r in opened} | {int(r["sequence"]) for r in events})

    async def wake_thoughts(self, scope: TrustedScope, *, after: int = 0, through: int | None = None,
                            limit: int | None = None) -> tuple[WakeRecord, ...]:
        with self._tx() as tx:
            tx.authorized(scope)
            return tuple(_wake(r) for r in tx.all(
                "SELECT * FROM wakes WHERE conversation_id=? AND event_sequence>? AND (? IS NULL OR event_sequence<=?)"
                " AND thought IS NOT NULL AND status IN ('passed', 'dropped') ORDER BY event_sequence LIMIT ?",
                scope.conversation_id, after, through, through, -1 if limit is None else max(0, limit)))

    async def wakes_since(self, principal: PrincipalId, since: datetime) -> tuple[WakeRecord, ...]:
        with self._tx() as tx:
            return tuple(_wake(r) for r in tx.all("SELECT * FROM wakes WHERE owner=? AND created_at>=?"
                                                  " ORDER BY created_at", principal, _timestamp(since)))

    async def taken_keys(self, principal: PrincipalId, keys: Sequence[str]) -> frozenset[str]:
        wanted = list(dict.fromkeys(keys))
        if not wanted:
            return frozenset()
        with self._tx() as tx:
            rows = tx.all(f"SELECT key FROM wake_keys WHERE owner=? AND key IN ({', '.join('?' * len(wanted))})",
                          principal, *wanted)
            return frozenset(r["key"] for r in rows)

    async def miss_reminders(self, principal: PrincipalId, before: datetime) -> int:
        with self._tx() as tx:
            return tx.execute("UPDATE memories SET remind_state='missed' WHERE owner=?"
                              " AND remind_state='scheduled'"
                              " AND remind_instant<? AND retired_at IS NULL", principal, _timestamp(before)).rowcount

    async def release_keys(self, principal: PrincipalId, wake_id: str) -> None:
        with self._tx() as tx:
            tx.execute("DELETE FROM wake_keys WHERE owner=? AND wake_id=?", principal, wake_id)

    async def expire_keys(self, principal: PrincipalId, keys: Sequence[str]) -> None:
        if not keys:
            return
        with self._tx() as tx:
            now = _timestamp(datetime.now(UTC))
            tx.executemany("INSERT INTO wake_keys (owner, key, wake_id, created_at) VALUES (?, ?, NULL, ?)"
                           " ON CONFLICT (owner, key) DO NOTHING", [(principal, k, now) for k in dict.fromkeys(keys)])

    async def read_quiet(self, principal: PrincipalId) -> datetime | None:
        with self._tx() as tx:
            row = tx.one("SELECT quiet_until FROM presence_settings WHERE owner=?", principal)
            return None if row is None else _moment(row["quiet_until"])

    async def write_quiet(self, principal: PrincipalId, until: datetime | None) -> None:
        with self._tx() as tx:
            tx.execute("INSERT INTO presence_settings (owner, quiet_until) VALUES (?, ?)"
                       " ON CONFLICT (owner) DO UPDATE SET quiet_until=excluded.quiet_until",
                       principal, None if until is None else _timestamp(until))

    async def count_source_memories(self, scope: TrustedScope) -> int:
        with self._tx() as tx:
            tx.owned(scope)
            row = tx.one("SELECT COUNT(*) AS n FROM memories WHERE owner=? AND source_conversation=?"
                         " AND retired_at IS NULL", scope.principal_id, scope.conversation_id)
            assert row is not None
            return int(row["n"])

    # --- MemoryPassLog (V3-P85) --------------------------------------------------------

    _UNFINISHED = (
        "(EXISTS (SELECT 1 FROM runs r WHERE r.conversation_id={c} AND r.status IN"
        " ('prepared', 'dispatch_claimed', 'received'))"
        " OR EXISTS (SELECT 1 FROM tool_executions t WHERE t.conversation_id={c} AND t.status='execution_claimed'))"
    )
    _EPISODE_COLUMNS = ("episode_id, conversation_id, from_sequence, through_sequence, started_at, ended_at, text,"
                        " user_sequences, himari_sequences, origin, source_pass, revision, created_at, updated_at")

    async def unretire_memory(self, principal: PrincipalId, memory_id: int, *, expected_revision: int,
                              now: datetime) -> Memory:
        with self._tx() as tx:
            row = tx.one(f"SELECT {self._MEMORY_COLUMNS}, retired_at FROM memories WHERE owner=? AND memory_id=?"
                         " AND retired_at IS NOT NULL", principal, memory_id)
            if row is None or datetime.fromisoformat(row["retired_at"]) <= now - RETIRED_KEEP:
                raise DataError(DataErrorCode.NOT_FOUND, f"no retired note m{memory_id}")
            note = _memory(row)
            if note.source_conversation is not None and tx.one(
                    "SELECT 1 FROM conversations WHERE conversation_id=? AND owner=?", note.source_conversation,
                    principal) is None:
                raise DataError(DataErrorCode.NOT_FOUND, "the conversation it came from was deleted")
            if expected_revision != note.revision:
                raise DataError(DataErrorCode.VERSION_CONFLICT, f"m{memory_id} is at revision {note.revision}")
            self._check_room(tx, principal)
            tx.execute("UPDATE memories SET retired_at=NULL, retired_pass=NULL, retired_item=NULL, revision=?,"
                       " updated_at=? WHERE owner=? AND memory_id=?", note.revision + 1, _timestamp(now), principal,
                       memory_id)
            return dataclasses.replace(note, revision=note.revision + 1, updated_at=now)

    async def memory_revision(self, principal: PrincipalId, memory_id: int) -> tuple[int, bool] | None:
        with self._tx() as tx:
            row = tx.one("SELECT revision, retired_at FROM memories WHERE owner=? AND memory_id=?", principal,
                         memory_id)
            return None if row is None else (int(row["revision"]), row["retired_at"] is not None)

    async def memory_revisions(self, principal: PrincipalId, memory_ids: Sequence[int]) -> dict[int, tuple[int, bool]]:
        ids = sorted({i for i in memory_ids if isinstance(i, int)})
        if not ids:
            return {}
        with self._tx() as tx:
            rows = tx.all(f"SELECT memory_id, revision, retired_at FROM memories WHERE owner=? AND memory_id IN"
                          f" ({', '.join('?' * len(ids))})", principal, *ids)
            return {int(r["memory_id"]): (int(r["revision"]), r["retired_at"] is not None) for r in rows}

    async def count_source_episodes(self, scope: TrustedScope) -> int:
        with self._tx() as tx:
            tx.owned(scope)
            row = tx.one("SELECT COUNT(*) AS n FROM episodes WHERE owner=? AND conversation_id=?",
                         scope.principal_id, scope.conversation_id)
            assert row is not None
            return int(row["n"])

    async def page_episodes(self, principal: PrincipalId, *, before: tuple[datetime, int] | None,
                            limit: int) -> tuple[RecentEpisode, ...]:
        with self._tx() as tx:
            if before is None:
                return self._recent_rows(tx, principal, "", limit=limit)
            return self._recent_rows(tx, principal, " AND (e.ended_at, e.episode_id) < (?, ?)",
                                     _timestamp(before[0]), before[1], limit=limit)

    def _episode_row(self, tx: _Tx, principal: PrincipalId, episode_id: object, expected_revision: int) -> Episode:
        row = None if not isinstance(episode_id, int) else tx.one(
            f"SELECT {self._EPISODE_COLUMNS} FROM episodes WHERE owner=? AND episode_id=?", principal, episode_id)
        if row is None:
            raise DataError(DataErrorCode.NOT_FOUND, f"no episode e{episode_id}")
        episode = _episode(row)
        if expected_revision != episode.revision:
            raise DataError(DataErrorCode.VERSION_CONFLICT, f"e{episode_id} is at revision {episode.revision}")
        return episode

    async def update_episode(self, principal: PrincipalId, episode_id: int, text: str, *,
                             expected_revision: int) -> Episode:
        text = rules.check_episode_text(text)
        with self._tx() as tx:
            episode = self._episode_row(tx, principal, episode_id, expected_revision)
            now = datetime.now(UTC)
            tx.execute("UPDATE episodes SET text=?, folded=?, origin='user', revision=?, updated_at=? WHERE owner=?"
                       " AND episode_id=?", text, rules.fold(text), episode.revision + 1, _timestamp(now), principal,
                       episode_id)
            return dataclasses.replace(episode, text=text, origin=MemoryOrigin.USER, revision=episode.revision + 1,
                                       updated_at=now)

    async def delete_episode(self, principal: PrincipalId, episode_id: int, *, expected_revision: int) -> None:
        with self._tx() as tx:
            self._episode_row(tx, principal, episode_id, expected_revision)
            tx.execute("DELETE FROM episodes WHERE owner=? AND episode_id=?", principal, episode_id)

    def _recent_rows(self, tx: _Tx, principal: PrincipalId, where: str, *params: object, limit: int,
                     order: str = "e.ended_at DESC, e.episode_id DESC",
                     order_params: Sequence[object] = ()) -> tuple[RecentEpisode, ...]:
        columns = ", ".join(f"e.{c.strip()}" for c in self._EPISODE_COLUMNS.split(","))
        rows = tx.all(f"SELECT {columns}, c.title FROM episodes e JOIN conversations c"
                      " ON c.conversation_id=e.conversation_id WHERE e.owner=? AND c.trashed_at IS NULL"
                      f"{where} ORDER BY {order} LIMIT ?", principal, *params, *order_params, limit)
        return tuple(RecentEpisode(_episode(r), r["title"]) for r in rows)

    async def recent_episodes(self, principal: PrincipalId, *, since: datetime, besides: ConversationId | None,
                              limit: int) -> tuple[RecentEpisode, ...]:
        with self._tx() as tx:
            return self._recent_rows(tx, principal, " AND e.ended_at >= ? AND e.conversation_id != ?",
                                     _timestamp(since), besides or "", limit=limit)

    async def count_episodes(self, principal: PrincipalId) -> int:
        with self._tx() as tx:
            row = tx.one("SELECT COUNT(*) AS n FROM episodes WHERE owner=?", principal)
            assert row is not None
            return int(row["n"])

    async def find_episodes(self, principal: PrincipalId, words: Sequence[str], *, start: datetime | None = None,
                            end: datetime | None = None, limit: int = 200) -> tuple[RecentEpisode, ...]:
        folded = list(dict.fromkeys(rules.fold(w) for w in words if w.strip()))
        where = "".join([" AND e.ended_at >= ?" if start is not None else "",
                         " AND e.ended_at <= ?" if end is not None else "",
                         (" AND (" + " OR ".join("instr(e.folded, ?) > 0" for _ in folded) + ")") if folded else ""])
        params = [*([_timestamp(start)] if start is not None else []), *([_timestamp(end)] if end is not None else []),
                  *folded]
        if folded:
            order = ("(" + " + ".join("(instr(e.folded, ?) > 0)" for _ in folded) + ") DESC,"
                     " e.ended_at DESC, e.episode_id DESC")
        elif start is not None or end is not None:
            order = "e.ended_at, e.episode_id"
        else:
            order = "e.ended_at DESC, e.episode_id DESC"
        with self._tx() as tx:
            return self._recent_rows(tx, principal, where, *params, limit=limit, order=order, order_params=folded)

    async def read_memory_cursor(self, scope: TrustedScope) -> MemoryCursor:
        with self._tx() as tx:
            tx.owned(scope)
            return _cursor(tx.one("SELECT * FROM memory_cursors WHERE conversation_id=?", scope.conversation_id))

    async def memory_due(self, *, quiet_before: datetime, now: datetime, after: ConversationId | None = None,
                         limit: int = 50) -> Sequence[DueConversation]:
        with self._tx() as tx:
            rows = tx.all(
                "SELECT c.owner, c.conversation_id, m.through_sequence, m.failures, m.retry_after,"
                " m.baseline_sequence, m.backfill_through,"
                " (SELECT MAX(e.created_at) FROM events e WHERE e.conversation_id=c.conversation_id) AS last_at"
                " FROM conversations c LEFT JOIN memory_cursors m ON m.conversation_id=c.conversation_id"
                " WHERE c.trashed_at IS NULL AND c.conversation_id > ?"
                " AND EXISTS (SELECT 1 FROM events e WHERE e.conversation_id=c.conversation_id"
                " AND e.kind='user_message' AND e.sequence > COALESCE(m.through_sequence, 0))"
                " AND (m.retry_after IS NULL OR m.retry_after <= ?)"
                f" AND NOT {self._UNFINISHED.format(c='c.conversation_id')}"
                # character-day §18.8: a closed day session is read at once, without the quiet
                " AND (c.closed_at IS NOT NULL"
                " OR (SELECT MAX(e.created_at) FROM events e WHERE e.conversation_id=c.conversation_id) < ?)"
                " ORDER BY c.conversation_id LIMIT ?",
                after or "", _timestamp(now), _timestamp(quiet_before), limit)
            return tuple(DueConversation(TrustedScope(PrincipalId(r["owner"]), ConversationId(r["conversation_id"])),
                                         _cursor(r) if r["through_sequence"] is not None else MemoryCursor(),
                                         datetime.fromisoformat(r["last_at"])) for r in rows)

    async def last_event(self, scope: TrustedScope) -> tuple[int, datetime] | None:
        with self._tx() as tx:
            tx.authorized(scope)
            row = tx.one("SELECT sequence, created_at FROM events WHERE conversation_id=? ORDER BY sequence DESC"
                         " LIMIT 1", scope.conversation_id)
            return None if row is None else (int(row["sequence"]), datetime.fromisoformat(row["created_at"]))

    async def unfinished_work(self, scope: TrustedScope) -> bool:
        with self._tx() as tx:
            tx.owned(scope)
            row = tx.one(f"SELECT {self._UNFINISHED.format(c='?')} AS busy", scope.conversation_id,
                         scope.conversation_id)
            assert row is not None
            return bool(row["busy"])

    async def last_episode(self, scope: TrustedScope) -> Episode | None:
        with self._tx() as tx:
            tx.owned(scope)
            row = tx.one(f"SELECT {self._EPISODE_COLUMNS} FROM episodes WHERE owner=? AND conversation_id=?"
                         " ORDER BY ended_at DESC, episode_id DESC LIMIT 1", scope.principal_id, scope.conversation_id)
            return None if row is None else _episode(row)

    async def list_episodes(self, principal: PrincipalId) -> tuple[Episode, ...]:
        with self._tx() as tx:
            rows = tx.all(f"SELECT {self._EPISODE_COLUMNS} FROM episodes WHERE owner=?"
                          " ORDER BY ended_at DESC, episode_id DESC", principal)
            return tuple(_episode(r) for r in rows)

    async def pass_usage(self, principal: PrincipalId, since: datetime) -> int:
        with self._tx() as tx:
            row = tx.one("SELECT COALESCE(SUM(c.charged_tokens), 0) AS n FROM memory_pass_calls c"
                         " JOIN memory_passes p ON p.pass_id=c.pass_id WHERE p.owner=? AND c.prepared_at >= ?",
                         principal, _timestamp(since))
            assert row is not None
            return int(row["n"])

    @staticmethod
    def _pass_row(tx: _Tx, pass_id: object) -> sqlite3.Row:
        row = None if not isinstance(pass_id, str) else tx.one("SELECT * FROM memory_passes WHERE pass_id=?", pass_id)
        if row is None:
            raise DataError(DataErrorCode.NOT_FOUND, f"no memory pass {pass_id}")
        return row

    @staticmethod
    def _running_pass(tx: _Tx, pass_id: str) -> sqlite3.Row:
        row = SqliteData._pass_row(tx, pass_id)
        if row["status"] != PassStatus.RUNNING.value:
            raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"{pass_id} is {row['status']}")
        return row

    async def begin_pass(self, scope: TrustedScope, start: PassStart) -> str:
        if (not isinstance(start, PassStart) or not isinstance(start.kind, PassKind)
                or not 0 <= start.from_sequence < start.through_sequence <= start.snapshot_sequence
                or start.omitted_chars < 0 or (start.kind is PassKind.RETRY) != (start.retry_of is not None)):
            raise DataError(DataErrorCode.INVALID_ARGUMENT, "not a pass start")
        with self._tx() as tx:
            try:
                tx.authorized(scope)
            except DataError:
                raise DataError(DataErrorCode.NOT_FOUND, scope.conversation_id) from None
            if tx.one("SELECT 1 FROM memory_passes WHERE owner=? AND status='running'", scope.principal_id):
                raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, "another pass of this principal runs")
            pass_id = tx.new_id("mpass")
            tx.execute("INSERT INTO memory_passes (pass_id, owner, conversation_id, kind, from_sequence,"
                       " through_sequence, snapshot_sequence, retry_of, status, provider_key, model_id, calls,"
                       " reserved_tokens, input_tokens, output_tokens, unmetered_tokens, over_estimate,"
                       " omitted_chars, error, created_at, finished_at)"
                       " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, 0, 0, 0, 0, 0, 0, ?, NULL, ?, NULL)",
                       pass_id, scope.principal_id, scope.conversation_id, start.kind.value, start.from_sequence,
                       start.through_sequence, start.snapshot_sequence, start.retry_of, start.provider_key,
                       start.model_id, start.omitted_chars, _timestamp(datetime.now(UTC)))
            return pass_id

    async def prepare_pass_call(self, pass_id: str, body: bytes, content_type: str, reserved_tokens: int) -> int:
        if not isinstance(body, bytes) or reserved_tokens < 0:
            raise DataError(DataErrorCode.INVALID_ARGUMENT, "a call stores its request and a reservation")
        try:
            with self._tx() as tx:
                row = self._running_pass(tx, pass_id)
                call = int(row["calls"]) + 1
                tx.execute("INSERT INTO memory_pass_calls (pass_id, call, status, reserved_tokens, prepared_at,"
                           " charged_tokens, raw_request_body, raw_request_content_type)"
                           " VALUES (?, ?, 'prepared', ?, ?, ?, ?, ?)", pass_id, call, reserved_tokens,
                           _timestamp(datetime.now(UTC)), reserved_tokens, body, content_type)
                tx.execute("UPDATE memory_passes SET calls=?, reserved_tokens=? WHERE pass_id=?",
                           call, reserved_tokens, pass_id)
                return call
        except sqlite3.Error as error:
            raise DataError(DataErrorCode.RAW_PERSISTENCE_UNAVAILABLE, type(error).__name__) from error

    async def mark_pass_call_dispatched(self, pass_id: str, call: int) -> None:
        with self._tx() as tx:
            self._running_pass(tx, pass_id)
            if tx.execute("UPDATE memory_pass_calls SET status='dispatched' WHERE pass_id=? AND call=?"
                          " AND status='prepared'", pass_id, call).rowcount != 1:
                raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"call {call} of {pass_id} is not prepared")

    async def record_pass_call(self, pass_id: str, call: int, outcome: RawRunOutcome) -> None:
        with self._tx() as tx:
            self._pass_row(tx, pass_id)
            row = tx.one("SELECT * FROM memory_pass_calls WHERE pass_id=? AND call=?", pass_id, call)
            if row is None or row["status"] not in ("prepared", "dispatched"):
                raise DataError(DataErrorCode.INVALID_STATE_TRANSITION, f"call {call} of {pass_id} is recorded")
            usage = outcome.usage if isinstance(outcome, RawReceived) else None
            reported = (None if usage is None or usage.input_tokens is None or usage.output_tokens is None
                        else (usage.input_tokens, usage.output_tokens))
            tx.execute("UPDATE memory_pass_calls SET status=?, raw_response_body=?, raw_response_content_type=?,"
                       " usage=?, error_code=?, charged_tokens=? WHERE pass_id=? AND call=?",
                       "received" if isinstance(outcome, RawReceived) else "failed", outcome.body,
                       outcome.content_type, None if reported is None else json.dumps(list(reported)),
                       None if isinstance(outcome, RawReceived) else outcome.error_code,
                       row["reserved_tokens"] if reported is None else sum(reported), pass_id, call)
            reserved = row["reserved_tokens"]
            if reported is None:
                tx.execute("UPDATE memory_passes SET reserved_tokens=0, unmetered_tokens=unmetered_tokens+?"
                           " WHERE pass_id=?", reserved, pass_id)
            else:
                tx.execute("UPDATE memory_passes SET reserved_tokens=0, input_tokens=input_tokens+?,"
                           " output_tokens=output_tokens+?, over_estimate=over_estimate+? WHERE pass_id=?",
                           reported[0], reported[1], int(sum(reported) > reserved), pass_id)

    async def read_pass_response(self, pass_id: str, call: int) -> bytes | None:
        with self._tx() as tx:
            row = tx.one("SELECT raw_response_body FROM memory_pass_calls WHERE pass_id=? AND call=?", pass_id, call)
            if row is None:
                raise DataError(DataErrorCode.NOT_FOUND, f"no call {call} of {pass_id}")
            return None if row["raw_response_body"] is None else bytes(row["raw_response_body"])

    @staticmethod
    def _settle_calls(tx: _Tx, pass_id: str) -> None:
        """§4.8, before a pass ends: a call that may have been sent and has no
        recorded outcome is counted as its reservation used (``uncertain``);
        one stored but never sent is not counted (``abandoned``)."""
        uncertain = tx.one("SELECT COALESCE(SUM(reserved_tokens), 0) AS n FROM memory_pass_calls WHERE pass_id=?"
                           " AND status='dispatched'", pass_id)
        assert uncertain is not None
        tx.execute("UPDATE memory_pass_calls SET status='uncertain' WHERE pass_id=? AND status='dispatched'", pass_id)
        tx.execute("UPDATE memory_pass_calls SET status='abandoned', charged_tokens=0 WHERE pass_id=?"
                   " AND status='prepared'", pass_id)
        tx.execute("UPDATE memory_passes SET reserved_tokens=0, unmetered_tokens=unmetered_tokens+? WHERE pass_id=?",
                   int(uncertain["n"]), pass_id)

    @staticmethod
    def _count_failure(tx: _Tx, row: sqlite3.Row, status: PassStatus, error: str, retry_after: datetime) -> PassStatus:
        """§4.5: a RUNNING pass ends as ``status`` (FAILED or INTERRUPTED),
        or SKIPPED when it is the live part's last allowed failure."""
        SqliteData._settle_calls(tx, row["pass_id"])
        now = _timestamp(datetime.now(UTC))
        final = status
        if row["kind"] == PassKind.LIVE.value:
            cursor = _cursor(tx.one("SELECT * FROM memory_cursors WHERE conversation_id=?", row["conversation_id"]))
            if cursor.through_sequence == row["from_sequence"]:
                failures = cursor.failures + 1
                if failures >= SKIP_AFTER_FAILURES:
                    final = PassStatus.SKIPPED
                    cursor = dataclasses.replace(cursor, through_sequence=row["through_sequence"], failures=0,
                                                 retry_after=None)
                else:
                    cursor = dataclasses.replace(cursor, failures=failures, retry_after=retry_after)
                SqliteData._write_cursor(tx, ConversationId(row["conversation_id"]), cursor)
        tx.execute("UPDATE memory_passes SET status=?, error=?, finished_at=? WHERE pass_id=?",
                   final.value, error[:200], now, row["pass_id"])
        return final

    @staticmethod
    def _write_cursor(tx: _Tx, conversation: ConversationId, cursor: MemoryCursor) -> None:
        tx.execute("INSERT INTO memory_cursors (conversation_id, through_sequence, failures, retry_after,"
                   " baseline_sequence, backfill_through) VALUES (?, ?, ?, ?, ?, ?)"
                   " ON CONFLICT (conversation_id) DO UPDATE SET through_sequence=excluded.through_sequence,"
                   " failures=excluded.failures, retry_after=excluded.retry_after,"
                   " baseline_sequence=excluded.baseline_sequence, backfill_through=excluded.backfill_through",
                   conversation, cursor.through_sequence, cursor.failures,
                   None if cursor.retry_after is None else _timestamp(cursor.retry_after),
                   cursor.baseline_sequence, cursor.backfill_through)

    async def fail_pass(self, pass_id: str, error: str, retry_after: datetime) -> PassStatus:
        with self._tx() as tx:
            return self._count_failure(tx, self._running_pass(tx, pass_id), PassStatus.FAILED, error, retry_after)

    async def interrupt_running_passes(self, retry_after: datetime) -> int:
        with self._tx() as tx:
            rows = tx.all("SELECT * FROM memory_passes WHERE status='running' ORDER BY seq")
            for row in rows:
                self._count_failure(tx, row, PassStatus.INTERRUPTED, "interrupted", retry_after)
            return len(rows)

    def _discard(self, tx: _Tx, pass_id: str, reason: str) -> PassApplied:
        self._settle_calls(tx, pass_id)
        tx.execute("UPDATE memory_passes SET status='discarded', error=?, finished_at=?"
                   " WHERE pass_id=?", reason, _timestamp(datetime.now(UTC)), pass_id)
        return PassApplied(PassStatus.DISCARDED, reason=reason)

    def _pass_checks(self, tx: _Tx, row: sqlite3.Row) -> str | None:
        """§4.4's checks by kind; why the pass cannot be applied, or None."""
        conversation = row["conversation_id"]
        conv = tx.one("SELECT trashed_at FROM conversations WHERE conversation_id=? AND owner=?", conversation,
                      row["owner"])
        if conv is None or conv["trashed_at"] is not None:
            return "conversation_gone"
        kind = PassKind(row["kind"])
        applied = tx.all("SELECT from_sequence, through_sequence FROM memory_passes WHERE conversation_id=?"
                         " AND status='applied'", conversation)
        if any(rules.overlaps(row["from_sequence"], row["through_sequence"], a["from_sequence"],
                              a["through_sequence"]) for a in applied):
            return "already_read"
        cursor = _cursor(tx.one("SELECT * FROM memory_cursors WHERE conversation_id=?", conversation))
        if kind is PassKind.LIVE:
            last = tx.one("SELECT COALESCE(MAX(sequence), 0) AS last FROM events WHERE conversation_id=?",
                          conversation)
            assert last is not None
            if cursor.through_sequence != row["from_sequence"]:
                return "cursor_moved"
            if last["last"] != row["snapshot_sequence"]:
                return "new_events"
            busy = tx.one(f"SELECT {self._UNFINISHED.format(c='?')} AS busy", conversation, conversation)
            if busy is not None and busy["busy"]:
                return "unfinished_work"
        elif kind is PassKind.BACKFILL:
            if cursor.backfill_through != row["from_sequence"] or row["through_sequence"] > cursor.baseline_sequence:
                return "backfill_moved"
        else:
            skipped = tx.one("SELECT status, from_sequence, through_sequence FROM memory_passes WHERE pass_id=?"
                             " AND conversation_id=?", row["retry_of"], conversation)
            if (skipped is None or skipped["status"] != PassStatus.SKIPPED.value
                    or (skipped["from_sequence"], skipped["through_sequence"])
                    != (row["from_sequence"], row["through_sequence"])):
                return "not_skipped"
        return None

    async def apply_pass(self, pass_id: str, items: Sequence[PassItem], episode: EpisodePlan | None, *,
                         now: datetime) -> PassApplied:
        if (not all(isinstance(i, PassItem) and isinstance(i.action, PassAction) for i in items)
                or len({i.item for i in items}) != len(items)
                or (episode is not None and not isinstance(episode, EpisodePlan))):
            raise DataError(DataErrorCode.INVALID_ARGUMENT, "not a pass plan")
        with self._tx() as tx:
            row = tx.one("SELECT * FROM memory_passes WHERE pass_id=?", pass_id)
            if row is None:  # its Conversation was deleted for good meanwhile, rows and all
                return PassApplied(PassStatus.DISCARDED, reason="conversation_gone")
            if row["status"] != PassStatus.RUNNING.value:
                return PassApplied(PassStatus(row["status"]), reason="not_running")
            why = self._pass_checks(tx, row)
            if why is not None:
                return self._discard(tx, pass_id, why)
            principal, conversation = PrincipalId(row["owner"]), ConversationId(row["conversation_id"])
            kind, stamp = PassKind(row["kind"]), _timestamp(now)
            outcomes: list[str] = []
            for item in items:
                note = None
                if item.memory_id is not None and item.action in (PassAction.UPDATE, PassAction.FORGET):
                    found = tx.one(f"SELECT {self._MEMORY_COLUMNS} FROM memories WHERE owner=? AND memory_id=?"
                                   " AND retired_at IS NULL", principal, item.memory_id)
                    note = None if found is None else _memory(found)
                live = tx.one("SELECT COUNT(*) AS n FROM memories WHERE owner=? AND retired_at IS NULL", principal)
                assert live is not None
                outcome, action, normal = rules.decide_item(item, kind, note, live["n"])
                target, previous, written = None, None, None
                if action is PassAction.ADD:
                    assert normal.category is not None and normal.text is not None
                    added = Memory(self._next_memory_id(tx, principal), normal.category, normal.text,
                                   MemoryOrigin.EXTRACTOR, conversation, None, 1, now, now, due_at=normal.due_at,
                                   due_instant=normal.due_instant, valid_until=normal.valid_until,
                                   valid_instant=normal.valid_instant, source_pass=pass_id, source_item=item.item)
                    self._write_memory(tx, principal, added)
                    target, written = f"m{added.memory_id}", 1
                elif action is PassAction.UPDATE:
                    assert note is not None and normal.category is not None and normal.text is not None
                    previous = rules.memory_json(note)
                    changed = dataclasses.replace(
                        note, category=normal.category, text=normal.text, origin=MemoryOrigin.EXTRACTOR,
                        source_conversation=conversation, source_call=None, source_pass=pass_id,
                        source_item=item.item, due_at=normal.due_at, due_instant=normal.due_instant,
                        valid_until=normal.valid_until, valid_instant=normal.valid_instant,
                        revision=note.revision + 1, updated_at=now)
                    self._write_memory(tx, principal, changed)
                    target, written = f"m{note.memory_id}", changed.revision
                elif action is PassAction.FORGET:
                    assert note is not None
                    previous = rules.memory_json(note)
                    tx.execute("UPDATE memories SET retired_at=?, retired_pass=?, retired_item=?, revision=?,"
                               " updated_at=? WHERE owner=? AND memory_id=?", stamp, pass_id, item.item,
                               note.revision + 1, stamp, principal, note.memory_id)
                    target, written = f"m{note.memory_id}", note.revision + 1
                elif item.memory_id is not None:
                    target = f"m{item.memory_id}"
                planned = (rules.planned_json(normal) if outcome in ("applied", "proposed", "sensitive_proposed")
                           and normal.action is not PassAction.FORGET else None)
                tx.execute("INSERT INTO memory_pass_items (pass_id, item, proposal, planned, outcome, target,"
                           " previous, written_revision, decided_at, created_at)"
                           " VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)", pass_id, item.item, item.proposal, planned,
                           outcome, target, previous, written, stamp)
                outcomes.append(outcome)
            episode_id = None if episode is None else self._write_episode(tx, row, kind, episode, now)
            cursor = _cursor(tx.one("SELECT * FROM memory_cursors WHERE conversation_id=?", conversation))
            if kind is PassKind.LIVE:
                self._write_cursor(tx, conversation, dataclasses.replace(
                    cursor, through_sequence=row["through_sequence"], failures=0, retry_after=None))
            elif kind is PassKind.BACKFILL:
                done = row["through_sequence"] >= cursor.baseline_sequence
                self._write_cursor(tx, conversation, dataclasses.replace(
                    cursor, backfill_through=0 if done else row["through_sequence"],
                    baseline_sequence=0 if done else cursor.baseline_sequence))
            else:
                tx.execute("UPDATE memory_passes SET status='retried' WHERE pass_id=?", row["retry_of"])
            self._settle_calls(tx, pass_id)
            tx.execute("UPDATE memory_passes SET status='applied', finished_at=? WHERE pass_id=?", stamp, pass_id)
            return PassApplied(PassStatus.APPLIED, tuple(outcomes), episode_id)

    def _write_episode(self, tx: _Tx, row: sqlite3.Row, kind: PassKind, plan: EpisodePlan, now: datetime) -> int | None:
        """§6.2: rewrite this Conversation's newest episode, or add one; None
        when the text is not an episode's (the rest still applies)."""
        try:
            text = rules.check_episode_text(plan.text)
        except DataError:
            return None
        principal, conversation = PrincipalId(row["owner"]), ConversationId(row["conversation_id"])
        found = tx.one(f"SELECT {self._EPISODE_COLUMNS} FROM episodes WHERE owner=? AND conversation_id=?"
                       " ORDER BY ended_at DESC, episode_id DESC LIMIT 1", principal, conversation)
        previous = None if found is None else _episode(found)
        users, himari = json.dumps(list(plan.user_sequences)), json.dumps(list(plan.himari_sequences))
        stamp = _timestamp(now)
        if rules.extends(plan, kind, previous):
            assert previous is not None
            tx.execute("UPDATE episodes SET through_sequence=?, ended_at=?, text=?, folded=?, user_sequences=?,"
                       " himari_sequences=?, source_pass=?, revision=?, updated_at=? WHERE owner=? AND episode_id=?",
                       row["through_sequence"], _timestamp(plan.ended_at), text, rules.fold(text),
                       json.dumps(list(previous.user_sequences) + list(plan.user_sequences)),
                       json.dumps(list(previous.himari_sequences) + list(plan.himari_sequences)), row["pass_id"],
                       previous.revision + 1, stamp, principal, previous.episode_id)
            return previous.episode_id
        last = tx.one("SELECT last_episode_id FROM memory_ids WHERE owner=?", principal)
        episode_id = (0 if last is None else int(last["last_episode_id"])) + 1
        tx.execute("INSERT INTO memory_ids (owner, last_id, last_episode_id) VALUES (?, 0, ?)"
                   " ON CONFLICT (owner) DO UPDATE SET last_episode_id=excluded.last_episode_id", principal, episode_id)
        tx.execute(f"INSERT INTO episodes ({self._EPISODE_COLUMNS}, owner, folded)"
                   " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   episode_id, conversation, row["from_sequence"], row["through_sequence"],
                   _timestamp(plan.started_at), _timestamp(plan.ended_at), text, users, himari,
                   MemoryOrigin.EXTRACTOR.value, row["pass_id"], 1, stamp, stamp, principal, rules.fold(text))
        tx.execute("DELETE FROM episodes WHERE owner=? AND episode_id IN (SELECT episode_id FROM episodes"
                   " WHERE owner=? ORDER BY ended_at DESC, episode_id DESC LIMIT -1 OFFSET ?)",
                   principal, principal, MAX_EPISODES)
        return episode_id

    async def read_pass(self, pass_id: str) -> MemoryPass:
        with self._tx() as tx:
            return _pass(self._pass_row(tx, pass_id))

    async def list_passes(self, scope: TrustedScope) -> tuple[MemoryPass, ...]:
        with self._tx() as tx:
            tx.owned(scope)
            return tuple(_pass(r) for r in tx.all("SELECT * FROM memory_passes WHERE conversation_id=? ORDER BY seq",
                                                  scope.conversation_id))

    async def read_pass_outcomes(self, pass_id: str) -> tuple[str, ...]:
        with self._tx() as tx:
            self._pass_row(tx, pass_id)
            return tuple(r["outcome"] for r in tx.all("SELECT outcome FROM memory_pass_items WHERE pass_id=?"
                                                      " ORDER BY item", pass_id))

    async def read_pass_items(self, pass_id: str) -> tuple[PassItemRecord, ...]:
        with self._tx() as tx:
            self._pass_row(tx, pass_id)
            return tuple(_item(r) for r in tx.all("SELECT * FROM memory_pass_items WHERE pass_id=? ORDER BY item",
                                                  pass_id))

    async def applied_pass_items(self, scope: TrustedScope, *,
                                 limit: int) -> tuple[tuple[MemoryPass, tuple[PassItemRecord, ...]], ...]:
        with self._tx() as tx:
            tx.owned(scope)
            passes = tx.all("SELECT * FROM memory_passes p WHERE conversation_id=? AND status='applied' AND EXISTS"
                            " (SELECT 1 FROM memory_pass_items i WHERE i.pass_id=p.pass_id AND i.outcome IN"
                            " ('applied', 'kept', 'sensitive_proposed')) ORDER BY seq DESC LIMIT ?",
                            scope.conversation_id, limit)
            if not passes:
                return ()
            ids = [r["pass_id"] for r in passes]
            items = tx.all(f"SELECT * FROM memory_pass_items WHERE pass_id IN ({', '.join('?' * len(ids))})"
                           " AND outcome IN ('applied', 'kept', 'sensitive_proposed') ORDER BY item", *ids)
            by_pass: dict[str, list[PassItemRecord]] = {}
            for row in items:
                by_pass.setdefault(row["pass_id"], []).append(_item(row))
            return tuple((_pass(r), tuple(by_pass.get(r["pass_id"], ()))) for r in reversed(passes))

    async def list_proposals(self, principal: PrincipalId) -> tuple[Proposal, ...]:
        with self._tx() as tx:
            rows = tx.all("SELECT i.*, p.conversation_id, p.kind, c.title, c.trashed_at FROM memory_pass_items i"
                          " JOIN memory_passes p ON p.pass_id=i.pass_id JOIN conversations c"
                          " ON c.conversation_id=p.conversation_id WHERE p.owner=? AND i.outcome IN"
                          " ('proposed', 'sensitive_proposed') ORDER BY i.created_at, p.seq, i.item", principal)
            return tuple(Proposal(_item(r), ConversationId(r["conversation_id"]), PassKind(r["kind"]), r["title"],
                                  r["trashed_at"] is not None) for r in rows)

    async def decide_proposal(self, principal: PrincipalId, pass_id: str, item: int, *, keep: bool,
                              now: datetime, dates: NoteDates | None = None) -> Memory | None:
        with self._tx() as tx:
            owner = tx.one("SELECT conversation_id FROM memory_passes WHERE pass_id=? AND owner=?", pass_id, principal)
            row = None if owner is None else tx.one("SELECT * FROM memory_pass_items WHERE pass_id=? AND item=?",
                                                    pass_id, item)
            if owner is None or row is None:
                raise DataError(DataErrorCode.NOT_FOUND, f"no proposal {pass_id}/{item}")
            if row["outcome"] not in ("proposed", "sensitive_proposed"):
                raise DataError(DataErrorCode.VERSION_CONFLICT, f"{pass_id}/{item} was {row['outcome']}")
            note = None
            if keep:
                planned = None if row["planned"] is None else planned_item(row["planned"], item, PassAction.ADD)
                if planned is None or planned.category is None:
                    raise DataError(DataErrorCode.INVALID_ARGUMENT, "the proposal names no note")
                text = rules.check_note_text(planned.category, planned.text)
                self._check_room(tx, principal)
                proposed = Memory(0, planned.category, text, MemoryOrigin.EXTRACTOR,
                                  ConversationId(owner["conversation_id"]), None, 1, now, now, due_at=planned.due_at,
                                  due_instant=planned.due_instant, valid_until=planned.valid_until,
                                  valid_instant=planned.valid_instant, source_pass=pass_id, source_item=item,
                                  user_approved_at=now)
                proposed = rules.check_fresh(proposed if dates is None else rules.dated(proposed, dates), now)
                note = dataclasses.replace(proposed, memory_id=self._next_memory_id(tx, principal))
                self._write_memory(tx, principal, note)
            tx.execute("UPDATE memory_pass_items SET outcome=?, decided_at=?, target=? WHERE pass_id=? AND item=?",
                       "kept" if keep else "discarded", _timestamp(now),
                       row["target"] if note is None else f"m{note.memory_id}", pass_id, item)
            return note

    async def pass_summary(self, principal: PrincipalId, *, day_start: datetime, since: datetime) -> PassSummary:
        charged = await self.pass_usage(principal, day_start)
        with self._tx() as tx:
            last = tx.one("SELECT MAX(finished_at) AS at FROM memory_passes WHERE owner=? AND status='applied'",
                          principal)
            skipped = tx.all("SELECT * FROM memory_passes WHERE owner=? AND status='skipped' AND finished_at >= ?"
                             " ORDER BY seq", principal, _timestamp(since))
            failed = tx.one("SELECT COUNT(*) AS n FROM memory_passes WHERE owner=? AND status IN"
                            " ('failed', 'interrupted') AND finished_at >= ?", principal, _timestamp(since))
            assert last is not None and failed is not None
            return PassSummary(charged, _moment(last["at"]), tuple(_pass(r) for r in skipped), int(failed["n"]))

    async def clean_memories(self, now: datetime) -> MemoryCleanup:
        with self._tx() as tx:
            loops = tx.execute(
                "DELETE FROM memories WHERE category='loop' AND retired_at IS NULL"
                " AND (remind_state IS NULL OR remind_state NOT IN ('scheduled', 'claimed'))"  # V3-P87
                " AND ((due_instant IS NOT NULL AND due_instant < ?) OR (due_instant IS NULL AND updated_at < ?))",
                _timestamp(now - STALE_DUE), _timestamp(now - STALE_UNDATED)).rowcount
            expired = tx.execute("DELETE FROM memories WHERE valid_instant IS NOT NULL AND valid_instant < ?",
                                 _timestamp(now - EXPIRED_KEEP)).rowcount
            retired = tx.execute("DELETE FROM memories WHERE retired_at IS NOT NULL AND retired_at < ?",
                                 _timestamp(now - RETIRED_KEEP)).rowcount
            proposals = tx.execute(
                "UPDATE memory_pass_items SET outcome='discarded', decided_at=? WHERE outcome IN"
                " ('proposed', 'sensitive_proposed') AND created_at < ?", _timestamp(now),
                _timestamp(now - PROPOSAL_KEEP)).rowcount
            return MemoryCleanup(loops, expired, retired, proposals)

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
            return PersistedStateBasis(
                conversation_sequence=last["last"],
                state_revision=conv["state_revision"],
                environments=tx.environments(scope),
            )
