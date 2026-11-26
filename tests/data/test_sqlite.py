"""SQLite-specific Data guarantees (Q-41): durability across real process
death, atomicity of every multi-write transaction, single writer, private
files, schema versioning, and raw-persistence failure before send."""

from __future__ import annotations

import asyncio
import fcntl
import os
import sqlite3
import stat
import subprocess
import sys
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from testkit import (FAKE, SCOPE, V15_UNDO, V21_UNDO, V22_UNDO, V23_UNDO, V24_UNDO, V25_UNDO, V26_UNDO, V27_UNDO, V28_UNDO, V29_UNDO, V30_UNDO, FakeEnvironment, RecordingTool, final,
                     text_result, user)

from himari.bootstrap import build_runtime
from himari.common import (
    ConversationId,
    EnvironmentKind,
    PrincipalId,
    RunId,
    CallId,
    CallRef,
    RawRepresentation,
    TextBlock,
    TokenUsage,
    ToolCall,
    ToolId,
    TrustedScope,
    UnknownOutcomePolicy,
    thaw,
)
from himari.control.api import DriveState
from himari.data.api import (
    ArcMove,
    ArcOp,
    DayState,
    FellBy,
    LifeJob,
    LifeKind,
    LifeOrigin,
    LifePlan,
    LifeWriteStart,
    PlanKind,
    PlanWrite,
    ThoughtAbout,
    ThoughtChange,
    ThoughtOp,
    WorkPiece,
    BondStage,
    BondTag,
    MoodSource,
    MemoryCursor,
    InstructionScope,
    MemoryCategory,
    MemoryChange,
    MemoryChangeKind,
    AssistantFinal,
    AssistantToolCalls,
    DataError,
    DataErrorCode,
    ModelSetting,
    PromptBasis,
    RawNoResponse,
    RawReceived,
    RawUncertain,
    RunPreparation,
    RunStatus,
    ToolCompletion,
    ToolExecutionStatus,
)
from himari.data.sqlite import (
    APPLICATION_ID,
    SCHEMA_VERSION,
    SqliteData,
    SqliteStore,
    StoreLockedError,
    StoreSchemaError,
    StoreSecurityError,
)
from himari.provider.scripted import ScriptedAdapter
from himari.tool.api import EffectProfile
from himari.tool.dispatcher import UNKNOWN_OUTCOME_RESULT

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


@pytest.fixture(autouse=True)
def _every_data_backend() -> None:
    """Overrides tests/data/conftest.py: these tests are SQLite-only."""


def open_store(path: Path) -> tuple[SqliteStore, SqliteData]:
    store = SqliteStore(path)
    return store, SqliteData(store)


def preparation(body: bytes = b"{}", provider: str = "p", model: str = "m") -> RunPreparation:
    return RunPreparation(body, "application/json", RawRepresentation.WIRE_EXACT, (), PromptBasis(0, 0), provider, model)


async def everything(data: SqliteData, store: SqliteStore) -> dict[str, Any]:
    """Every public read of SCOPE, for before/after comparisons."""
    runs = [r for r in {e.origin_run_id for e in await data.read_events(SCOPE)} if r is not None]
    return {
        "events": await data.read_events(SCOPE),
        "runs": [await data.read_run(SCOPE, r) for r in sorted(runs)],
        "raw": store.raw_request_bodies(SCOPE),
        "responses": [await data.read_raw_response(SCOPE, r) for r in sorted(runs)],
        "executions": [await data.read_tool_executions(SCOPE, r) for r in sorted(runs)],
        "memories": await data.list_memories(SCOPE.principal_id),
        "snapshot": await data.get_latest_snapshot(SCOPE),
        "state": await data.read_current_state_basis(SCOPE),
    }


# --- durability across restart, exact values ---------------------------------------------


def test_everything_reads_back_identically_after_reopening(tmp_path: Path) -> None:
    path = tmp_path / "h.db"
    store, data = open_store(path)
    store.create_conversation(SCOPE)
    store.activate_environment(SCOPE, FAKE)

    async def fill() -> dict[str, Any]:
        await data.append_event(SCOPE, user("héllo ✓"), idempotency_key="k1")
        run_id = await data.prepare_run(SCOPE, preparation(b"\x00\xff\xfe raw \x80", "openrouter", "model-x"))
        await data.mark_dispatch(SCOPE, run_id)
        await data.record_raw_outcome(SCOPE, run_id, RawReceived(b"\x01resp\xff", "application/json",
                                                                 TokenUsage(10, 20, 5, None)))
        group = (ToolCall(CallId("a"), 0, ToolId("note"), {"n": 1.5, "f": 2.0, "nested": [{"k": None}, True]}),
                 ToolCall(CallId("b"), 1, ToolId("fake/w"), {}))
        await data.project_model_output(SCOPE, run_id, AssistantToolCalls(group, commentary=(TextBlock("thinking"),)),
                                        continuation={"reasoning_details": [{"type": "enc", "data": "Zm9v", "n": 1.0}]})
        await data.commit_tool_outcome(SCOPE, CallRef(run_id, CallId("a")), ToolCompletion(
            ToolExecutionStatus.SUCCEEDED, text_result("noted"), raw_result={"bytes": 3, "list": [1, "2"]},
            memory_change=MemoryChange(MemoryChangeKind.SAVE, await data.allocate_memory_id(SCOPE.principal_id),
                                       MemoryCategory.USER, "Writes ✓ notes.")))
        await data.claim_tool_execution(SCOPE, CallRef(run_id, CallId("b")),
                                        unknown_outcome=UnknownOutcomePolicy.REPORT_TO_MODEL, record_event=False)
        failed = await data.prepare_run(SCOPE, preparation(b"second"))
        await data.mark_dispatch(SCOPE, failed)
        await data.record_raw_outcome(SCOPE, failed, RawNoResponse("http_500", b"err body", "text/plain"))
        await data.replace_latest_snapshot(SCOPE, expected_revision=None, schema_version=3,
                                           body={"turns": [1, 2.25, "x"]}, covers_through_sequence=2,
                                           basis=PromptBasis(2, 1, None, "ctx", "state"))
        return await everything(data, store)

    before = asyncio.run(fill())
    store.close()
    store2, data2 = open_store(path)
    after = asyncio.run(everything(data2, store2))
    store2.close()
    assert after == before
    assert after["raw"][0] == b"\x00\xff\xfe raw \x80" and after["responses"][0].body == b"\x01resp\xff"
    arguments = after["executions"][0][0].arguments
    assert arguments["f"] == 2.0 and isinstance(arguments["f"], float)  # 2.0 stays a float, not 2
    assert [m.text for m in after["memories"]] == ["Writes ✓ notes."]  # written with its call
    payload = thaw(after["runs"][0].continuation.payload)
    assert payload == {"reasoning_details": [{"type": "enc", "data": "Zm9v", "n": 1.0}]}
    assert isinstance(payload["reasoning_details"][0]["n"], float)


def test_what_arrived_of_a_stream_survives_reopening(tmp_path: Path) -> None:
    # V3-P73 §4: an uncertain Run keeps the part of a stream that arrived, as received.
    path = tmp_path / "h.db"
    store, data = open_store(path)
    store.create_conversation(SCOPE)

    async def fill() -> RunId:
        run_id = await data.prepare_run(SCOPE, preparation(b"{}"))
        await data.mark_dispatch(SCOPE, run_id)
        await data.record_raw_outcome(SCOPE, run_id, RawUncertain("cancelled_by_user", b"data: {\"a\"\n",
                                                                  "text/event-stream"))
        return run_id

    run_id = asyncio.run(fill())
    store.close()
    store2, data2 = open_store(path)
    try:
        run = asyncio.run(data2.read_run(SCOPE, run_id))
        raw = asyncio.run(data2.read_raw_response(SCOPE, run_id))
        assert (run.status, run.error_code) == (RunStatus.OUTCOME_UNKNOWN, "cancelled_by_user")
        assert (raw.body, raw.content_type) == (b"data: {\"a\"\n", "text/event-stream")
    finally:
        store2.close()


def test_files_are_private(tmp_path: Path) -> None:
    store, data = open_store(tmp_path / "h.db")
    store.create_conversation(SCOPE)
    asyncio.run(data.append_event(SCOPE, user("x")))
    try:
        for name in ("h.db", "h.db-wal", "h.db-shm", "h.db.lock"):
            assert stat.S_IMODE((tmp_path / name).stat().st_mode) == 0o600, name
    finally:
        store.close()


# --- single writer ----------------------------------------------------------------------------


OTHER_PROCESS = r"""
import sys
sys.path[:0] = [sys.argv[2]]
from himari.data.sqlite import SqliteStore, StoreLockedError
try:
    SqliteStore(sys.argv[1]).close()
    print("OPENED")
except StoreLockedError:
    print("LOCKED")
"""


def test_only_one_store_may_open_a_database(tmp_path: Path) -> None:
    path = tmp_path / "h.db"
    store = SqliteStore(path)

    def other_process() -> str:
        done = subprocess.run([sys.executable, "-I", "-c", OTHER_PROCESS, str(path), str(ROOT / "src")],
                              capture_output=True, text=True, timeout=60)
        return done.stdout.strip() or done.stderr

    with pytest.raises(StoreLockedError):
        SqliteStore(path)
    assert other_process() == "LOCKED"
    store.close()
    assert other_process() == "OPENED"
    SqliteStore(path).close()


def tamper(path: Path, *statements: str) -> None:
    conn = sqlite3.connect(path, isolation_level=None)
    for statement in statements:
        conn.execute(statement)
    conn.close()


def foreign_database(path: Path, *statements: str) -> None:
    tamper(path, "CREATE TABLE notes (x)", *statements)
    path.chmod(0o600)  # private, so only the identity checks can refuse it


@pytest.mark.parametrize("change, message", [
    (f"PRAGMA user_version={SCHEMA_VERSION + 1}", "unsupported schema version"),
    ("PRAGMA application_id=1", "not a HIMARI database"),
    ("DROP INDEX runs_by_conversation", "does not have the HIMARI schema"),
    ("ALTER TABLE runs ADD COLUMN extra TEXT", "does not have the HIMARI schema"),
    ("CREATE TABLE extra (x)", "does not have the HIMARI schema"),
    ("DELETE FROM meta", "no id counter"),
])
def test_a_changed_himari_database_is_refused(tmp_path: Path, change: str, message: str) -> None:
    path = tmp_path / "h.db"
    SqliteStore(path).close()
    tamper(path, change)
    with pytest.raises(StoreSchemaError, match=message):
        SqliteStore(path)


# What each version added, undone to build an older database. SQLite's DROP
# COLUMN restores the table's stored definition exactly.
_ADDED_IN = {
    3: ("DROP TABLE compaction_runs",),
    4: ("DROP TABLE external_settings",),
    5: ("ALTER TABLE tool_executions DROP COLUMN app_payload", "ALTER TABLE tool_executions DROP COLUMN ui_digest",
        "DROP TABLE ui_resources"),
    6: ("DROP TABLE model_settings",),
    7: ("ALTER TABLE conversations DROP COLUMN purge_started_at", "ALTER TABLE conversations DROP COLUMN trashed_at"),
    8: ("DROP TABLE files", "DROP TABLE blobs"),
    9: ("DROP INDEX files_by_view", *(f"ALTER TABLE files DROP COLUMN {c}" for c in (
        "view_height", "view_width", "view_type", "view_blob_key", "image_height", "image_width", "image_type",
        "image_status"))),
    10: ("DROP TABLE instructions",),
    11: ("DROP TABLE memory_ids", "DROP INDEX memories_by_source", "DROP TABLE memories"),
    12: ("DROP TABLE event_text",),
    13: ("DROP INDEX bindings_by_workspace", "DROP TABLE workspaces"),
    14: ("DROP TABLE device_ids", "DROP INDEX devices_by_pairing", "DROP INDEX devices_by_token", "DROP TABLE devices"),
    15: V15_UNDO,
    16: ("DROP TABLE presence_settings",),
    17: ("DROP TABLE wake_keys", "DROP INDEX wakes_opened", "DROP INDEX wakes_by_owner", "DROP TABLE wakes",
         "ALTER TABLE devices DROP COLUMN last_input_at",
         *(f"ALTER TABLE memories DROP COLUMN {c}" for c in ("reminded_at", "remind_attempts", "remind_wake",
                                                               "remind_state", "remind_instant", "remind_at"))),
    18: ("ALTER TABLE wakes DROP COLUMN held_run_id",),
    19: (),  # V3-P107 §2: data only (told reminders become delivered)
    20: ("DROP TABLE persona_choice", "DROP TABLE persona_ids", "DROP INDEX personas_by_name", "DROP TABLE personas"),
    21: V21_UNDO,
    22: V22_UNDO,
    23: V23_UNDO,
    24: V24_UNDO,
    25: V25_UNDO,
    26: V26_UNDO,
    27: V27_UNDO,
    28: V28_UNDO,
    29: V29_UNDO,
    30: V30_UNDO,
}


def old_database(path: Path, version: int) -> None:
    """A database as schema ``version`` wrote it, with one Conversation and one Event."""
    store = SqliteStore(path)
    store.create_conversation(SCOPE)
    asyncio.run(SqliteData(store).append_event(SCOPE, user(f"from v{version}")))
    store.close()
    tamper(path, *(statement for v, statements in sorted(_ADDED_IN.items(), reverse=True) if v > version
                   for statement in statements),
           f"PRAGMA user_version={version}")


@pytest.mark.parametrize("version", [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23,
                                     24, 25, 26, 27, 28, 29])
def test_an_older_database_is_upgraded_in_place_and_keeps_its_data(tmp_path: Path, version: int) -> None:
    path = tmp_path / "h.db"
    old_database(path, version)
    store = SqliteStore(path)
    try:
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 30
        data = SqliteData(store)
        events = asyncio.run(data.read_events(SCOPE))
        assert [e.payload.blocks for e in events] == [(TextBlock(f"from v{version}"),)]  # type: ignore[union-attr]
        asyncio.run(data.set_external_setting(SCOPE, "calliope", False))  # the v4 table works
        columns = [r[1] for r in store.connection.execute("PRAGMA table_info(tool_executions)")]
        assert columns[-2:] == ["ui_digest", "app_payload"]  # and the v5 ones
        asyncio.run(data.set_model_setting(SCOPE, ModelSetting("openai/gpt-6-luna", "high")))  # and the v6 one
        assert asyncio.run(data.count_staged(SCOPE)) == 0  # the v8 tables
        assert asyncio.run(data.write_instructions(SCOPE.principal_id, InstructionScope.USER, "Be brief.", 0)
                           ).revision == 1  # the v10 table
        assert asyncio.run(data.save_memory(SCOPE.principal_id, MemoryCategory.USER, "Writes Python.")
                           ).memory_id == 1  # the v11 tables
        found = asyncio.run(data.search_events(SCOPE.principal_id, ["FROM V"], current=ConversationId("conv_none"),
                                               current_through=None, per_conversation=3, limit=10))
        assert [h.text for h in found.hits] == [f"from v{version}"]  # v12: the Events already there were filled
        assert not asyncio.run(data.has_environment(SCOPE, FAKE))  # v21: the Environments it has (none)
        assert asyncio.run(data.add_device(SCOPE.principal_id, "Desk", b"code", datetime.now(UTC) + timedelta(minutes=10))
                           ).device_id == "d1"  # the v14 tables
        if version < 15:  # v15: the Conversation reads from its last Event, which is also its backfill baseline
            assert asyncio.run(data.read_memory_cursor(SCOPE)) == MemoryCursor(1, 0, None, 1, 0)
        assert asyncio.run(data.list_episodes(SCOPE.principal_id)) == ()
        asyncio.run(data.write_quiet(SCOPE.principal_id, datetime(2026, 11, 1, tzinfo=UTC)))  # the v16 table
        assert asyncio.run(data.read_quiet(SCOPE.principal_id)) == datetime(2026, 11, 1, tzinfo=UTC)
        assert asyncio.run(data.opened_wakes()) == ()  # the v17 tables
        assert "held_run_id" in [r[1] for r in store.connection.execute("PRAGMA table_info(wakes)")]  # the v18 column
        made = asyncio.run(data.create_persona(SCOPE.principal_id, "Teacher", "Call me 선생님."))  # the v20 tables
        assert asyncio.run(data.choose_persona(SCOPE.principal_id, made.persona_id, 0)).revision == 1
        fact = asyncio.run(data.add_life_fact(SCOPE.principal_id, made.persona_id, "고양이를 무서워한다."))
        assert fact.entry_id == 1  # the v22 tables
        assert asyncio.run(data.list_life(SCOPE.principal_id, made.persona_id)) == (fact,)
        assert asyncio.run(data.list_life_writes(SCOPE.principal_id)) == ()
        name = asyncio.run(data.add_bond(SCOPE.principal_id, made.persona_id, BondTag.NAME, "대장이라고 부른다."))
        assert name.entry_id == 2 and name.tag is BondTag.NAME  # the v23 columns
        assert asyncio.run(data.bond_stage(SCOPE.principal_id, made.persona_id)).revision == 0  # and table
        assert asyncio.run(data.read_mood(SCOPE.principal_id, made.persona_id)) is None  # the v24 table
        assert asyncio.run(data.set_mood(SCOPE.principal_id, made.persona_id, "happy", source=MoodSource.REPLY,
                                         at=datetime.now(UTC))).mood == "happy"
        assert name.time_of_day is None  # and column
        asyncio.run(data.trash_conversation(SCOPE))  # and the v7 columns
    finally:
        store.close()
    SqliteStore(path).close()  # and it opens as a plain v24 database afterwards


def test_a_v19_companion_persona_becomes_persona_1_chosen(tmp_path: Path) -> None:
    """V3-P108 §5.3: the one persona text becomes persona 1 and is chosen; an empty one is no persona;
    the instructions stay and no companion_persona row is left."""
    path = tmp_path / "h.db"
    old_database(path, 19)
    tamper(path, "INSERT INTO instructions (owner, scope, text, revision, updated_at) VALUES"
                 " ('alice', 'companion_persona', 'Call me 선생님.', 3, '2026-11-01T00:00:00.000000+00:00')",
           "INSERT INTO instructions (owner, scope, text, revision, updated_at) VALUES"
           " ('alice', 'user', 'Be brief.', 2, '2026-11-02T00:00:00.000000+00:00')",
           "INSERT INTO instructions (owner, scope, text, revision, updated_at) VALUES"
           " ('bob', 'companion_persona', '', 1, '2026-11-03T00:00:00.000000+00:00')",
           "PRAGMA user_version=19")
    store = SqliteStore(path)
    try:
        assert list(tmp_path.glob("h.db.v19-*.bak"))  # backed up first
        data = SqliteData(store)
        alice = asyncio.run(data.list_personas(PrincipalId("alice")))
        assert [(p.persona_id, p.name, p.text, p.revision) for p in alice.personas] == [
            (1, "Persona", "Call me 선생님.", 1)]
        assert alice.personas[0].created_at == datetime(2026, 11, 1, tzinfo=UTC)
        assert (alice.choice.persona_id, alice.choice.revision) == (1, 1)
        assert asyncio.run(data.active_persona(PrincipalId("alice"))) == alice.personas[0]
        bob = asyncio.run(data.list_personas(PrincipalId("bob")))
        assert bob.personas == () and bob.choice.persona_id is None
        assert asyncio.run(data.read_instructions(PrincipalId("alice"), InstructionScope.USER)).text == "Be brief."
        assert store.connection.execute(
            "SELECT count(*) FROM instructions WHERE scope='companion_persona'").fetchone()[0] == 0
        # the next persona takes the next id, and "Persona" is taken
        assert asyncio.run(data.create_persona(PrincipalId("alice"), "Friend", "")).persona_id == 2
        with pytest.raises(DataError):
            asyncio.run(data.create_persona(PrincipalId("alice"), "persona", ""))
    finally:
        store.close()


def test_a_v21_database_gains_an_empty_life_and_keeps_its_personas(tmp_path: Path) -> None:
    """v22 (character-life §2): backed up first; the life tables are there and empty; the personas and
    their choice stay as they were."""
    path = tmp_path / "h.db"
    old_database(path, 21)
    store = SqliteStore(path)
    data = SqliteData(store)
    asyncio.run(data.create_persona(SCOPE.principal_id, "Friend", "반말로."))
    asyncio.run(data.choose_persona(SCOPE.principal_id, 1, 0))
    store.close()
    tamper(path, *V30_UNDO, *V29_UNDO, *V28_UNDO, *V27_UNDO, *V26_UNDO, *V25_UNDO, *V24_UNDO, *V23_UNDO, *V22_UNDO, "PRAGMA user_version=21")
    store = SqliteStore(path)
    try:
        assert list(tmp_path.glob("h.db.v21-*.bak"))
        data = SqliteData(store)
        assert await_(data.active_persona(SCOPE.principal_id)) is not None
        tables = {r[0] for r in store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"life_entries", "life_ids", "life_writes"} <= tables
        assert await_(data.list_life(SCOPE.principal_id, 1)) == ()
        assert await_(data.add_life_fact(SCOPE.principal_id, 1, "고양이를 무서워한다.")).entry_id == 1
    finally:
        store.close()


def test_a_v22_database_keeps_its_life_and_gains_the_bond(tmp_path: Path) -> None:
    """v23 (character-bond §2): backed up first; the life's entries stay as they were (no tag), the bond
    starts empty and every stage new."""
    path = tmp_path / "h.db"
    old_database(path, 22)
    store = SqliteStore(path)
    data = SqliteData(store)
    asyncio.run(data.create_persona(SCOPE.principal_id, "Friend", "반말로."))
    fact = asyncio.run(data.add_life_fact(SCOPE.principal_id, 1, "고양이를 무서워한다."))
    store.close()
    tamper(path, *V30_UNDO, *V29_UNDO, *V28_UNDO, *V27_UNDO, *V26_UNDO, *V25_UNDO, *V24_UNDO, *V23_UNDO, "PRAGMA user_version=22")
    store = SqliteStore(path)
    try:
        assert list(tmp_path.glob("h.db.v22-*.bak"))
        data = SqliteData(store)
        assert await_(data.list_life(SCOPE.principal_id, 1)) == (fact,)
        assert await_(data.list_bond(SCOPE.principal_id, 1)) == ()
        assert await_(data.bond_stage(SCOPE.principal_id, 1)).stage is BondStage.NEW
        promise = await_(data.add_bond(SCOPE.principal_id, 1, BondTag.PROMISE, "주말마다 영화 얘기 하기."))
        assert promise.entry_id == fact.entry_id + 1  # the ids go on
        assert await_(data.list_life(SCOPE.principal_id, 1)) == (fact,)  # the bond is not the life
    finally:
        store.close()


def test_a_v23_database_keeps_its_life_and_bond_and_gains_moods(tmp_path: Path) -> None:
    """v24 (character-state §2, §3): backed up first; the entries stay as they were (no time), no persona
    has a mood yet, and a mood and a plan's time are kept from then on."""
    path = tmp_path / "h.db"
    old_database(path, 23)
    store = SqliteStore(path)
    data = SqliteData(store)
    asyncio.run(data.create_persona(SCOPE.principal_id, "Friend", "반말로."))
    fact = asyncio.run(data.add_life_fact(SCOPE.principal_id, 1, "고양이를 무서워한다."))
    joke = asyncio.run(data.add_bond(SCOPE.principal_id, 1, BondTag.JOKE, "감자 사건"))
    store.close()
    tamper(path, *V30_UNDO, *V29_UNDO, *V28_UNDO, *V27_UNDO, *V26_UNDO, *V25_UNDO, *V24_UNDO, "PRAGMA user_version=23")
    store = SqliteStore(path)
    try:
        assert list(tmp_path.glob("h.db.v23-*.bak"))
        data = SqliteData(store)
        assert await_(data.list_life(SCOPE.principal_id, 1)) == (fact,) and fact.time_of_day is None
        assert await_(data.list_bond(SCOPE.principal_id, 1)) == (joke,)
        assert await_(data.read_mood(SCOPE.principal_id, 1)) is None
        at = datetime(2026, 11, 25, 9, 0, tzinfo=UTC)
        kept = await_(data.set_mood(SCOPE.principal_id, 1, "happy", source=MoodSource.REPLY, at=at))
        assert await_(data.read_mood(SCOPE.principal_id, 1)) == kept and kept.mood == "happy"
    finally:
        store.close()


def test_a_v24_database_keeps_its_life_and_gains_arcs(tmp_path: Path) -> None:
    """v25 (character-world §4): backed up first; the entries stay as they were (no title, no arc),
    and an arc and a diary naming it are kept from then on."""
    path = tmp_path / "h.db"
    old_database(path, 24)
    store = SqliteStore(path)
    data = SqliteData(store)
    asyncio.run(data.create_persona(SCOPE.principal_id, "Friend", "반말로."))
    fact = asyncio.run(data.add_life_fact(SCOPE.principal_id, 1, "고양이를 무서워한다."))
    store.close()
    tamper(path, *V30_UNDO, *V29_UNDO, *V28_UNDO, *V27_UNDO, *V26_UNDO, *V25_UNDO, "PRAGMA user_version=24")
    store = SqliteStore(path)
    try:
        assert list(tmp_path.glob("h.db.v24-*.bak"))
        data = SqliteData(store)
        assert await_(data.list_life(SCOPE.principal_id, 1)) == (fact,)
        assert (fact.title, fact.end_day, fact.ending, fact.arc_id) == (None, None, None, None)
        write = await_(data.begin_life_write(SCOPE.principal_id, LifeWriteStart(
            1, "2026-11-25", "scripted", "m", b"{}", "application/json", 10)))
        await_(data.mark_life_write_dispatched(write))
        await_(data.record_life_write(write, RawReceived(b"{}", "application/json", None)))
        now = datetime(2026, 11, 25, 13, 0, tzinfo=UTC)
        written = await_(data.apply_life_write(write, LifePlan(
            "그림 연습을 시작했다.", arc=ArcMove(ArcOp.START, title="그림 연습", goal="한 달 안에 풍경화 한 장")),
            now=now))
        arc, diary = written
        assert (arc.kind, arc.title, arc.text, arc.day, arc.end_day) == (
            LifeKind.ARC, "그림 연습", "한 달 안에 풍경화 한 장", "2026-11-25", None)
        assert diary.arc_id == arc.entry_id
        assert await_(data.list_life(SCOPE.principal_id, 1)) == (fact, arc, diary)
    finally:
        store.close()


def test_a_v25_database_keeps_its_arcs_and_gains_work(tmp_path: Path) -> None:
    """v26 (character-work §3, §4): backed up first; the arcs and plans stay as they were (no so far, no
    project), the story writer's calls stay its own, and a work plan, a worker's call and its piece are kept."""
    path = tmp_path / "h.db"
    old_database(path, 25)
    store = SqliteStore(path)
    data = SqliteData(store)
    asyncio.run(data.create_persona(SCOPE.principal_id, "Friend", "반말로."))
    write = asyncio.run(data.begin_life_write(SCOPE.principal_id, LifeWriteStart(
        1, "2026-11-24", "scripted", "m", b"{}", "application/json", 10)))
    asyncio.run(data.mark_life_write_dispatched(write))
    asyncio.run(data.record_life_write(write, RawReceived(b"{}", "application/json", None)))
    arc, _ = asyncio.run(data.apply_life_write(write, LifePlan(
        "전자포를 구상했다.", arc=ArcMove(ArcOp.START, title="블랙홀 전자포", goal="설정집 한 권")),
        now=datetime(2026, 11, 24, 13, 0, tzinfo=UTC)))
    store.close()
    tamper(path, *V30_UNDO, *V29_UNDO, *V28_UNDO, *V27_UNDO, *V26_UNDO, "PRAGMA user_version=25")
    store = SqliteStore(path)
    try:
        assert list(tmp_path.glob("h.db.v25-*.bak"))
        data = SqliteData(store)
        (kept,) = await_(data.list_life(SCOPE.principal_id, 1, since="2026-11-25"))
        assert kept == arc and kept.so_far is None
        assert await_(data.life_attempts(SCOPE.principal_id, 1, "2026-11-24")) == 1
        assert (await_(data.list_life_writes(SCOPE.principal_id))[0]).plan_id is None
        write = await_(data.begin_life_write(SCOPE.principal_id, LifeWriteStart(
            1, "2026-11-25", "scripted", "m", b"{}", "application/json", 10)))
        await_(data.mark_life_write_dispatched(write))
        await_(data.record_life_write(write, RawReceived(b"{}", "application/json", None)))
        now = datetime(2026, 11, 25, 13, 0, tzinfo=UTC)
        written = await_(data.apply_life_write(write, LifePlan(
            "하루.", (("2026-11-26", "냉각부 도면 그려야지."),), times=("20:00",), plan_arcs=(arc.entry_id,)), now=now))
        plan = written[-1]
        assert plan.arc_id == arc.entry_id
        work = await_(data.begin_life_write(SCOPE.principal_id, LifeWriteStart(
            1, "2026-11-26", "scripted", "m", b"{}", "application/json", 10, plan_id=plan.entry_id)))
        await_(data.mark_life_write_dispatched(work))
        await_(data.record_life_write(work, RawReceived(b"{}", "application/json", None)))
        piece, project = await_(data.apply_work_write(work, WorkPiece("냉각부 설계.", "냉각부", "끝냈어.",
                                                                      "냉각부까지."), now=now))
        assert (piece.kind, piece.plan_id, piece.summary, project.so_far) == (
            LifeKind.WORK, plan.entry_id, "끝냈어.", "냉각부까지.")
        assert await_(data.work_attempts(SCOPE.principal_id, plan.entry_id)) == 1
    finally:
        store.close()


def test_a_v26_database_keeps_its_personas_text_as_the_field_free(tmp_path: Path) -> None:
    """v27 (character-persona-fields §3, P3): backed up first; a persona from before is all one field, free, so
    the text the model is told is the same; an empty one has no field; a persona saved from fields after works."""
    path = tmp_path / "h.db"
    old_database(path, 26)
    store = SqliteStore(path)
    data = SqliteData(store)
    text = "이름: 루시\n말투: 반말\n[familiar] 사실 예전에 해커였다.\n  끝에 공백  "
    asyncio.run(data.create_persona(SCOPE.principal_id, "Friend", text))
    asyncio.run(data.create_persona(SCOPE.principal_id, "Empty", ""))
    asyncio.run(data.choose_persona(SCOPE.principal_id, 1, 0))
    store.close()
    tamper(path, *V30_UNDO, *V29_UNDO, *V28_UNDO, *V27_UNDO, "PRAGMA user_version=26")
    store = SqliteStore(path)
    try:
        assert list(tmp_path.glob("h.db.v26-*.bak"))
        data = SqliteData(store)
        friend, empty = await_(data.list_personas(SCOPE.principal_id)).personas
        assert (friend.text, dict(friend.fields)) == (text, {"free": text})
        assert (empty.text, dict(empty.fields)) == ("", {})
        assert await_(data.active_persona(SCOPE.principal_id)) == friend
        edited = await_(data.update_persona(SCOPE.principal_id, 1, "Friend", "", 1,
                                            {"name": "루시", "makes": "작곡", "free": text}))
        assert edited.text == f"이름: 루시\n하는 일과 만드는 것: 작곡\n{text}"
        assert await_(data.active_persona(SCOPE.principal_id)) == edited
    finally:
        store.close()


def test_a_v27_database_names_its_life_writes_jobs_and_keeps_thoughts_after(tmp_path: Path) -> None:
    """v28 (character-mind §14.2): backed up first; a worker's call from before is a work_end job, a story writer's
    a review; the entries are as they were (no thought, no draft); thoughts are kept and listed after it."""
    path = tmp_path / "h.db"
    old_database(path, 27)
    store = SqliteStore(path)
    data = SqliteData(store)
    for plan_id in (None, 5):
        write = asyncio.run(data.begin_life_write(SCOPE.principal_id, LifeWriteStart(
            1, "2026-11-24", "scripted", "m", b"{}", "application/json", 10, plan_id=plan_id)))
    fact = asyncio.run(data.add_life_fact(SCOPE.principal_id, 1, "고양이를 무서워한다."))
    store.close()
    tamper(path, *V30_UNDO, *V29_UNDO, *V28_UNDO, "PRAGMA user_version=27")
    store = SqliteStore(path)
    try:
        assert list(tmp_path.glob("h.db.v27-*.bak"))
        jobs = [tuple(r) for r in store.connection.execute("SELECT plan_id, job FROM life_writes ORDER BY seq")]
        assert jobs == [(None, "review"), (5, "work_end")] and write
        data = SqliteData(store)
        (kept,) = await_(data.list_life(SCOPE.principal_id, 1))
        assert kept == fact and (kept.about, kept.expires_at, kept.draft) == (None, None, False)
        now = datetime.now(UTC)
        assert await_(data.apply_thought_changes(SCOPE.principal_id, 1, [ThoughtChange(
            ThoughtOp.KEEP, "피곤해 보였다.", ThoughtAbout.USER, sequences=(2,))], conversation=SCOPE.conversation_id,
            source="mpass_1", now=now)) == ("applied",)
        assert [e.kind for e in await_(data.list_life(SCOPE.principal_id, 1))] == [LifeKind.FACT, LifeKind.THOUGHT]
    finally:
        store.close()


def test_a_v28_database_makes_its_plans_hour_slots_and_keeps_its_conversations(tmp_path: Path) -> None:
    """v29 (character-day §18.2): backed up first; a plan from before is an hour long, a work slot with its project,
    else its own; no conversation is a day session; sleeps and the keeper's state start empty and work."""
    path = tmp_path / "h.db"
    old_database(path, 28)
    store = SqliteStore(path)
    data = SqliteData(store)
    now = datetime.now(UTC)
    write = asyncio.run(data.begin_life_write(SCOPE.principal_id, LifeWriteStart(
        1, "2026-11-24", "scripted", "m", b"{}", "application/json", 10)))
    asyncio.run(data.record_life_write(write, RawReceived(b"{}", "application/json")))
    asyncio.run(data.apply_life_write(write, LifePlan(
        "일기", (("2026-11-25", "책 읽기"), ("2026-11-26", "설정집")), times=("20:00", "21:00"),
        arc=ArcMove(ArcOp.START, title="설정집", goal="세계 설정"), plan_arcs=(None, 0)), now=now))
    store.close()
    tamper(path, *V30_UNDO, *V29_UNDO, "PRAGMA user_version=28")
    store = SqliteStore(path)
    try:
        assert list(tmp_path.glob("h.db.v28-*.bak"))
        rows = [tuple(r) for r in store.connection.execute(
            "SELECT text, minutes, plan_kind, plan_state, reason FROM life_entries WHERE kind='plan' ORDER BY entry_id")]
        assert rows == [("책 읽기", 60, "own", None, None), ("설정집", 60, "work", None, None)]
        data = SqliteData(store)
        assert await_(data.read_conversation(SCOPE)).day_session is None
        assert await_(data.current_day_session(SCOPE.principal_id)) is None
        sleep = await_(data.open_sleep(SCOPE.principal_id, 1, at=now, how=FellBy.SCHEDULE))
        assert await_(data.current_sleep(SCOPE.principal_id, 1)) == sleep
        assert await_(data.read_day_state(SCOPE.principal_id, 1)) == DayState(1)
    finally:
        store.close()


def test_a_v29_database_is_backed_up_and_takes_diary_slots_and_amends(tmp_path: Path) -> None:
    """v30 (character-diary §11, §16.2): backed up first, the version moved, a diary slot and an amend write are kept
    from then on (an older server stops at the version, never at them), and a write keeps what its request was made
    of (review P1-6: its basis, empty for the writes from before)."""
    path = tmp_path / "h.db"
    old_database(path, 29)
    store = SqliteStore(path)
    try:
        assert list(tmp_path.glob("h.db.v29-*.bak")) and store.backup_path is not None
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 30
        data = SqliteData(store)
        now = datetime(2026, 11, 27, 3, 0, tzinfo=UTC)  # 12:00 in Seoul
        await_(data.write_plan(SCOPE.principal_id, 0, PlanWrite("2026-11-27", "23:30", 480, PlanKind.SLEEP, "잠"),
                               origin=LifeOrigin.WRITER, zone="Asia/Seoul", now=now))
        slot = await_(data.write_plan(SCOPE.principal_id, 0, PlanWrite("2026-11-27", "22:00", 30, PlanKind.DIARY,
                                                                       "일기 쓰기"),
                                      origin=LifeOrigin.WRITER, zone="Asia/Seoul", now=now))
        assert slot.plan_kind is PlanKind.DIARY and slot.diary_slot
        write = await_(data.begin_life_write(SCOPE.principal_id, LifeWriteStart(
            0, "2026-11-26", "scripted", "m", b"{}", "application/json", 10, job=LifeJob.AMEND)))
        latest = await_(data.list_life_writes(SCOPE.principal_id))[0]
        assert latest.job is LifeJob.AMEND and write and (latest.basis_sequence, latest.basis_at) == (None, None)
    finally:
        store.close()


def await_[T](coroutine: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coroutine)


def test_a_v18_told_reminder_is_delivered_after_the_upgrade(tmp_path: Path) -> None:
    """V3-P107 §2: ``told`` is no more — said in its Conversation, so delivered; the rest stays."""
    path = tmp_path / "h.db"
    old_database(path, 18)
    store = SqliteStore(path)
    data = SqliteData(store)
    told = asyncio.run(data.save_memory(SCOPE.principal_id, MemoryCategory.LOOP, "약 먹기"))
    kept = asyncio.run(data.save_memory(SCOPE.principal_id, MemoryCategory.LOOP, "물 마시기"))
    store.close()
    tamper(path, f"UPDATE memories SET remind_state='told', remind_wake='wake_1' WHERE memory_id={told.memory_id}",
           f"UPDATE memories SET remind_state='scheduled' WHERE memory_id={kept.memory_id}",
           *(statement for v in range(SCHEMA_VERSION, 18, -1) for statement in _ADDED_IN[v]),  # back to v18's shape
           "PRAGMA user_version=18")
    store = SqliteStore(path)
    try:
        rows = dict(((r[0], (r[1], r[2])) for r in store.connection.execute(
            "SELECT memory_id, remind_state, remind_wake FROM memories")))
        assert rows[told.memory_id] == ("delivered", None)
        assert rows[kept.memory_id] == ("scheduled", None)
    finally:
        store.close()


def test_a_v20_conversation_keeps_its_environments_and_loses_the_rest(tmp_path: Path) -> None:
    """v21: each Conversation keeps the Environment kinds it had (one row per kind, even if a kind was
    activated twice); Bindings, Workspaces, Artifacts and the columns naming them are gone."""
    path = tmp_path / "h.db"
    other = TrustedScope(SCOPE.principal_id, ConversationId("conv_2"))
    store = SqliteStore(path)
    for scope in (SCOPE, other):
        store.create_conversation(scope)
        asyncio.run(SqliteData(store).append_event(scope, user(f"in {scope.conversation_id}")))
    store.close()
    tamper(path, *V30_UNDO, *V29_UNDO, *V28_UNDO, *V27_UNDO, *V26_UNDO, *V25_UNDO, *V24_UNDO, *V23_UNDO, *V22_UNDO, *V21_UNDO,
           "INSERT INTO activations (conversation_id, environment_instance_id, environment_kind, default_binding_id)"
           " VALUES ('conv_1', 'env_a', 'machine', 'bind_a'), ('conv_1', 'env_b', 'machine', NULL),"
           " ('conv_2', 'env_c', 'inside', 'bind_c')",
           "INSERT INTO bindings (conversation_id, binding_id, environment_instance_id, environment_kind, workspace_id,"
           " revision, cwd_relative) VALUES ('conv_1', 'bind_a', 'env_a', 'machine', 'mws_x', 1, 'conversations/conv_1')",
           "INSERT INTO workspaces (workspace_id, owner, environment_kind, location, label, created_at)"
           " VALUES ('ws_r', 'alice', 'machine', '/srv/x', 'x', '2026-11-01T00:00:00+00:00')",
           "INSERT INTO artifacts (conversation_id, artifact_id, kind, revision, content)"
           " VALUES ('conv_1', 'art_1', 'note', 1, '\"n\"')",
           "UPDATE conversations SET workspace_id='mws_x', workspace_cwd='conversations/conv_1'"
           " WHERE conversation_id='conv_1'",
           "PRAGMA user_version=20")
    store = SqliteStore(path)
    try:
        data = SqliteData(store)
        machine, inside = EnvironmentKind("machine"), EnvironmentKind("inside")
        assert asyncio.run(data.read_current_state_basis(SCOPE)).environments == (machine,)
        assert asyncio.run(data.read_current_state_basis(other)).environments == (inside,)
        assert asyncio.run(data.has_environment(SCOPE, machine)) and not asyncio.run(data.has_environment(other, machine))
        tables = {r[0] for r in store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert not tables & {"activations", "bindings", "workspaces", "artifacts"} and "environments" in tables
        columns = {r[1] for r in store.connection.execute("PRAGMA table_info(conversations)")}
        assert not columns & {"workspace_id", "workspace_cwd"}
        assert "workspace_binding_id" not in {r[1] for r in store.connection.execute("PRAGMA table_info(tool_executions)")}
        events = asyncio.run(data.read_events(SCOPE))
        assert [e.payload.blocks for e in events] == [(TextBlock("in conv_1"),)]  # type: ignore[union-attr]
        asyncio.run(data.trash_conversation(SCOPE))
        assert asyncio.run(data.begin_purge(SCOPE)).environments == (machine,)
        asyncio.run(data.purge_conversation(SCOPE))
        assert store.connection.execute("SELECT count(*) FROM environments").fetchone()[0] == 1  # conv_2's
    finally:
        store.close()


@pytest.mark.parametrize("version", [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23,
                                     24, 25, 26, 27, 28, 29])
@pytest.mark.parametrize("change", ["CREATE TABLE extra (x)", "PRAGMA application_id=1", "DROP TABLE artifacts"])
def test_only_an_exact_older_database_is_upgraded(tmp_path: Path, version: int, change: str) -> None:
    path = tmp_path / "h.db"
    old_database(path, version)
    if change == "DROP TABLE artifacts" and version >= 21:  # v21 dropped it: lose a table it has instead
        change = "DROP TABLE environments"
    tamper(path, change)
    with pytest.raises(StoreSchemaError):
        SqliteStore(path)
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == version  # refused, never repaired
        if version < 18:
            assert "held_run_id" not in [r[1] for r in conn.execute("PRAGMA table_info(wakes)")]  # v18's
    finally:
        conn.close()
    assert not list(tmp_path.glob("*.bak*"))  # refused before any backup


def test_a_database_claiming_an_older_version_with_the_current_shape_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "h.db"
    SqliteStore(path).close()
    tamper(path, "PRAGMA user_version=3")
    with pytest.raises(StoreSchemaError, match="unsupported schema version 3"):
        SqliteStore(path)


@pytest.mark.parametrize("statements", [(), (f"PRAGMA user_version={SCHEMA_VERSION}",),
                                        (f"PRAGMA user_version={SCHEMA_VERSION}", f"PRAGMA application_id={APPLICATION_ID}")])
def test_a_foreign_database_is_refused_whatever_its_header_says(tmp_path: Path, statements: tuple[str, ...]) -> None:
    path = tmp_path / "other.db"
    foreign_database(path, *statements)
    with pytest.raises(StoreSchemaError):
        SqliteStore(path)
    conn = sqlite3.connect(path)
    assert [r[0] for r in conn.execute("SELECT name FROM sqlite_master")] == ["notes"]  # left untouched
    conn.close()


def test_a_failed_open_releases_the_connection_and_the_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "other.db"
    foreign_database(path, f"PRAGMA user_version={SCHEMA_VERSION}")
    opened: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def recording_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        conn: sqlite3.Connection = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", recording_connect)
    with pytest.raises(StoreSchemaError):
        SqliteStore(path)
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")  # closed
    lock = os.open(f"{path}.lock", os.O_RDWR)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # released
    finally:
        os.close(lock)


# --- private files (PR #10 review B1) -------------------------------------------------------


@pytest.mark.parametrize("suffix", ["", ".lock", "-wal", "-shm", "-journal"])
@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660])
def test_existing_files_open_to_others_are_refused_not_repaired(tmp_path: Path, suffix: str, mode: int) -> None:
    path = tmp_path / "h.db"
    SqliteStore(path).close()
    target = Path(f"{path}{suffix}")
    target.touch()
    target.chmod(mode)
    with pytest.raises(StoreSecurityError, match="accessible to other users"):
        SqliteStore(path)
    assert stat.S_IMODE(target.stat().st_mode) == mode  # reported, not silently fixed


@pytest.mark.parametrize("suffix", ["", ".lock", "-wal"])
def test_symlinked_store_files_are_refused(tmp_path: Path, suffix: str) -> None:
    path = tmp_path / "h.db"
    if suffix:
        SqliteStore(path).close()
    target = tmp_path / "elsewhere"
    target.write_bytes(b"precious")
    target.chmod(0o644)
    Path(f"{path}{suffix}").unlink(missing_ok=True)
    Path(f"{path}{suffix}").symlink_to(target)
    with pytest.raises(StoreSecurityError, match="symbolic link"):
        SqliteStore(path)
    assert target.read_bytes() == b"precious" and stat.S_IMODE(target.stat().st_mode) == 0o644


def test_a_hard_linked_database_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "h.db"
    SqliteStore(path).close()
    os.link(path, tmp_path / "copy.db")
    with pytest.raises(StoreSecurityError, match="hard links"):
        SqliteStore(path)


@pytest.mark.parametrize("mode", [0o770, 0o707, 0o777])
def test_a_directory_others_can_change_is_refused(tmp_path: Path, mode: int) -> None:
    folder = tmp_path / "data"
    folder.mkdir()
    folder.chmod(mode)
    with pytest.raises(StoreSecurityError, match="not writable by others"):
        SqliteStore(folder / "h.db")
    assert not (folder / "h.db").exists() and not (folder / "h.db.lock").exists()


# --- D15: no send when the request cannot be stored ---------------------------------------


def test_unwritable_database_blocks_the_send(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "h.db")
    store.create_conversation(SCOPE)
    provider = ScriptedAdapter([final("never")])
    rt = build_runtime(provider, store=store)
    store.connection.execute("PRAGMA query_only=ON")  # every write now fails inside SQLite
    with pytest.raises(DataError) as exc:
        asyncio.run(SqliteData(store).prepare_run(SCOPE, preparation()))
    assert exc.value.code is DataErrorCode.RAW_PERSISTENCE_UNAVAILABLE
    with pytest.raises(Exception):
        asyncio.run(rt.driver.drive(SCOPE, user("hi")))
    assert provider.sent == []
    store.close()


# --- atomicity: an error after any write of a transaction leaves no trace -----------------


class FailAt:
    """Connection stand-in raising after the n-th write statement."""

    def __init__(self, conn: sqlite3.Connection, n: int) -> None:
        self.conn, self.n, self.writes = conn, n, 0

    @property
    def in_transaction(self) -> bool:
        return self.conn.in_transaction

    def execute(self, sql: str, params: Any = ()) -> sqlite3.Cursor:
        cursor = self.conn.execute(sql, params)
        if sql.lstrip().split()[0].upper() in ("INSERT", "UPDATE", "DELETE"):
            self.writes += 1
            if self.writes == self.n:
                raise sqlite3.OperationalError("injected failure")
        return cursor


Operation = Callable[[], Coroutine[Any, Any, object]]
Setup = Callable[[SqliteStore, SqliteData], Coroutine[Any, Any, Operation]]


async def _received(data: SqliteData) -> Any:
    run_id = await data.prepare_run(SCOPE, preparation())
    await data.mark_dispatch(SCOPE, run_id)
    await data.record_raw_outcome(SCOPE, run_id, RawReceived(b"r", "application/json"))
    return run_id


GROUP = (ToolCall(CallId("c0"), 0, ToolId("note"), {}), ToolCall(CallId("c1"), 1, ToolId("fake/w"), {}),
         ToolCall(CallId("c2"), 2, ToolId("fake/v"), {}))


async def op_project(store: SqliteStore, data: SqliteData) -> Operation:
    run_id = await _received(data)
    return lambda: data.project_model_output(SCOPE, run_id, AssistantToolCalls(GROUP), continuation={"c": 1})


async def op_commit(store: SqliteStore, data: SqliteData) -> Operation:
    run_id = await _received(data)
    await data.project_model_output(SCOPE, run_id, AssistantToolCalls(GROUP))
    note = await data.save_memory(SCOPE.principal_id, MemoryCategory.USER, "v1")
    done = ToolCompletion(ToolExecutionStatus.SUCCEEDED, text_result("ok"), raw_result=[1],
                          memory_change=MemoryChange(MemoryChangeKind.SAVE, note.memory_id, MemoryCategory.USER, "v2",
                                                     expected_revision=1))
    return lambda: data.commit_tool_outcome(SCOPE, CallRef(run_id, CallId("c0")), done)


async def op_unknown(store: SqliteStore, data: SqliteData) -> Operation:
    run_id = await _received(data)
    await data.project_model_output(SCOPE, run_id, AssistantToolCalls(GROUP))
    call = CallRef(run_id, CallId("c1"))
    await data.claim_tool_execution(SCOPE, call, unknown_outcome=UnknownOutcomePolicy.REPORT_TO_MODEL, record_event=True)
    return lambda: data.commit_unknown_outcome(SCOPE, call, UNKNOWN_OUTCOME_RESULT)


async def op_recover(store: SqliteStore, data: SqliteData) -> Operation:
    for _ in range(2):
        run_id = await _received(data)
        await data.project_model_output(SCOPE, run_id, AssistantToolCalls(GROUP))
        for call in ("c1", "c2"):
            await data.claim_tool_execution(SCOPE, CallRef(run_id, CallId(call)),
                                            unknown_outcome=UnknownOutcomePolicy.REPORT_TO_MODEL, record_event=True)
    claimed = await data.prepare_run(SCOPE, preparation())
    await data.mark_dispatch(SCOPE, claimed)
    await data.prepare_run(SCOPE, preparation())
    return lambda: data.recover_interrupted(SCOPE)


async def op_append(store: SqliteStore, data: SqliteData) -> Operation:
    return lambda: data.append_event(SCOPE, user("keyed"), idempotency_key="key")


async def op_final(store: SqliteStore, data: SqliteData) -> Operation:
    run_id = await _received(data)
    return lambda: data.project_model_output(SCOPE, run_id, AssistantFinal((TextBlock("done"),)), continuation=[1])


async def op_activate(store: SqliteStore, data: SqliteData) -> Operation:
    async def activate() -> None:
        store.activate_environment(SCOPE, FAKE)
    return activate


async def op_create(store: SqliteStore, data: SqliteData) -> Operation:
    environments = (FAKE, EnvironmentKind("other"))
    return lambda: data.create_conversation(PrincipalId("carol"), title="t", environments=environments)


async def op_purge(store: SqliteStore, data: SqliteData) -> Operation:
    run_id = await _received(data)
    await data.project_model_output(SCOPE, run_id, AssistantToolCalls(GROUP))
    await data.commit_tool_outcome(SCOPE, CallRef(run_id, CallId("c2")), ToolCompletion(
        ToolExecutionStatus.SUCCEEDED, text_result("v"), raw_result={"n": 1}))
    store.activate_environment(SCOPE, FAKE)
    await data.set_external_setting(SCOPE, "fake", False)
    await data.set_model_setting(SCOPE, ModelSetting("m", None))
    await data.trash_conversation(SCOPE)
    await data.begin_purge(SCOPE)
    return lambda: data.purge_conversation(SCOPE)


@pytest.mark.parametrize("setup", [op_project, op_commit, op_unknown, op_recover, op_append, op_final,
                                   op_activate, op_create, op_purge], ids=lambda f: f.__name__)
def test_a_failure_after_any_write_rolls_the_whole_transaction_back(tmp_path: Path, setup: Setup) -> None:
    store, data = open_store(tmp_path / "h.db")
    store.create_conversation(SCOPE)
    asyncio.run(data.append_event(SCOPE, user("start")))
    operation = asyncio.run(setup(store, data))
    real = store.connection
    before = list(real.iterdump())
    n = 0
    while True:
        n += 1
        trap = FailAt(real, n)
        store.connection = trap  # type: ignore[assignment]
        try:
            asyncio.run(operation())
        except sqlite3.OperationalError:
            store.connection = real
            assert list(real.iterdump()) == before, f"partial effect after write {n}"
            continue
        store.connection = real
        break
    assert n > 2, "the operation should involve several writes"
    assert list(real.iterdump()) != before  # and it does commit when nothing fails
    store.close()


# --- real process death (SIGKILL) ----------------------------------------------------------


def child(*args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), str(ROOT / "tests")])}
    return subprocess.run([sys.executable, str(HERE / "crash_child.py"), *args],
                          capture_output=True, text=True, timeout=120, env=env)


@pytest.mark.parametrize("op", ["project", "commit", "recover"])
def test_killed_mid_transaction_leaves_the_state_before_it(tmp_path: Path, op: str) -> None:
    counted = child(str(tmp_path / "count.db"), "transaction", op, "count", "0", str(tmp_path / "count.dump"))
    assert counted.returncode == 0, counted.stderr
    writes = int(counted.stdout.split()[-1])
    assert writes > 2
    for n in sorted({1, writes // 2, writes}):
        db, dump = tmp_path / f"{n}.db", tmp_path / f"{n}.dump"
        killed = child(str(db), "transaction", op, "mid", str(n), str(dump))
        assert killed.returncode == -9, killed.stderr
        reopened = SqliteStore(db)
        try:
            assert "\n".join(reopened.connection.iterdump()) == dump.read_text(), f"{op}: partial state after write {n}"
        finally:
            reopened.close()


@pytest.mark.parametrize("op", ["project", "commit", "recover"])
def test_killed_right_after_commit_keeps_the_commit(tmp_path: Path, op: str) -> None:
    db, dump = tmp_path / "h.db", tmp_path / "h.dump"
    killed = child(str(db), "transaction", op, "after", "0", str(dump))
    assert killed.returncode == -9, killed.stderr
    reopened = SqliteStore(db)
    try:
        assert "\n".join(reopened.connection.iterdump()) != dump.read_text()
        data = SqliteData(reopened)
        events = asyncio.run(data.read_events(SCOPE))
        if op == "project":
            assert isinstance(events[-1].payload, AssistantToolCalls)
        elif op == "commit":
            assert [m.text for m in asyncio.run(data.list_memories(SCOPE.principal_id))] == ["a"]
        else:
            report = asyncio.run(data.recover_interrupted(SCOPE))  # already done: nothing left
            assert report.tools_unknown == () and report.runs_abandoned_not_sent == ()
    finally:
        reopened.close()


def test_d03_process_killed_while_sending_is_never_resent(tmp_path: Path) -> None:
    db = tmp_path / "h.db"
    killed = child(str(db), "provider")
    assert killed.returncode == -9, killed.stderr

    store = SqliteStore(db)
    provider = ScriptedAdapter([final("back")])
    rt = build_runtime(provider, store=store)
    result = asyncio.run(rt.driver.drive(SCOPE, user("hello again")))
    assert result.state is DriveState.COMPLETED
    assert result.recovery is not None and len(result.recovery.runs_outcome_unknown) == 1
    unknown = result.recovery.runs_outcome_unknown[0]
    assert asyncio.run(rt.data.read_run(SCOPE, unknown)).status is RunStatus.OUTCOME_UNKNOWN
    assert len(provider.sent) == 1 and unknown not in result.run_ids
    assert len(store.raw_request_bodies(SCOPE)) == 2  # the killed Run's request was stored before the send
    store.close()


def test_d17_process_killed_during_an_effect_never_reruns_it(tmp_path: Path) -> None:
    db = tmp_path / "h.db"
    killed = child(str(db), "tool")
    assert killed.returncode == -9, killed.stderr

    store = SqliteStore(db)
    write = RecordingTool("fake/write", effect=EffectProfile.UNKNOWN, environment_kind=FAKE)
    provider = ScriptedAdapter([final("I checked")])
    rt = build_runtime(provider, store=store, environments=[FakeEnvironment(write)])
    result = asyncio.run(rt.driver.drive(SCOPE, user("continue")))
    assert result.state is DriveState.COMPLETED
    assert result.recovery is not None and len(result.recovery.tools_unknown) == 1
    assert write.invocations == []  # never re-run after the crash
    call = result.recovery.tools_unknown[0]
    (record,) = asyncio.run(rt.data.read_tool_executions(SCOPE, call.run_id))
    assert record.status is ToolExecutionStatus.UNKNOWN and record.model_result == UNKNOWN_OUTCOME_RESULT
    assert dict(record.arguments) == {"v": 1}
    store.close()


def test_files_of_a_v8_database_read_as_not_images(tmp_path: Path) -> None:
    """V3-P67: a file recorded before v9 has no image fields; it reads as a plain file."""
    from himari.data.api import ImageStatus, ViewState, view_state

    async def chunks() -> Any:
        yield b"\x89PNG\r\n\x1a\n" + bytes(40)

    path = tmp_path / "h.db"
    store = SqliteStore(path)
    store.create_conversation(SCOPE)
    data = SqliteData(store)
    blob = asyncio.run(data.blobs.put(SCOPE.principal_id, chunks(), 1 << 20))
    record = asyncio.run(data.stage_upload(SCOPE, blob, "old.png", "image/png"))
    store.close()
    tamper(path, *_ADDED_IN[29], *_ADDED_IN[28], *_ADDED_IN[26], *_ADDED_IN[25], *_ADDED_IN[24], *_ADDED_IN[23], *_ADDED_IN[22], *_ADDED_IN[21], *_ADDED_IN[20], *_ADDED_IN[17], *_ADDED_IN[16], *_ADDED_IN[15], *_ADDED_IN[14], *_ADDED_IN[13], *_ADDED_IN[12], *_ADDED_IN[11], *_ADDED_IN[10], *_ADDED_IN[9], "PRAGMA user_version=8")
    store = SqliteStore(path)
    try:
        again, = asyncio.run(SqliteData(store).read_file_records(SCOPE, [(record.file_id, 1)]))
        assert (again.name, again.image_status, again.view) == ("old.png", ImageStatus.NOT_IMAGE, None)
        assert view_state(again) is ViewState.NOT_IMAGE
    finally:
        store.close()
