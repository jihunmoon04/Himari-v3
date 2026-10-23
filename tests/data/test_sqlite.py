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
from pathlib import Path
from typing import Any

import pytest
from testkit import FAKE, SCOPE, FakeEnvironment, RecordingTool, final, text_result, user

from himari.bootstrap import build_runtime
from himari.common import (
    ASK_USER_TOOL_ID,
    ArtifactId,
    EnvironmentKind,
    PrincipalId,
    CallId,
    CallRef,
    RawRepresentation,
    TextBlock,
    TokenUsage,
    ToolCall,
    ToolId,
    UnknownOutcomePolicy,
    WorkspaceId,
    thaw,
)
from himari.control.api import DriveState
from himari.data.api import (
    AppView,
    InstructionScope,
    ArtifactMutation,
    AssistantFinal,
    AssistantToolCalls,
    DataError,
    DataErrorCode,
    InitialActivation,
    ModelSetting,
    PromptBasis,
    RawNoResponse,
    RawReceived,
    RunPreparation,
    RunStatus,
    ToolCompletion,
    ToolExecutionStatus,
    UiCsp,
    UiResource,
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
from himari.tool.api import EffectProfile, WorkspaceRequirement
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
        "artifacts": await data.list_artifacts(SCOPE),
        "snapshot": await data.get_latest_snapshot(SCOPE),
        "state": await data.read_current_state_basis(SCOPE),
        "waiting": await data.find_waiting_call(SCOPE),
    }


# --- durability across restart, exact values ---------------------------------------------


def test_everything_reads_back_identically_after_reopening(tmp_path: Path) -> None:
    path = tmp_path / "h.db"
    store, data = open_store(path)
    store.create_conversation(SCOPE)
    binding = store.activate_environment(SCOPE, FAKE, WorkspaceId("ws_1"), cwd_relative="conversations/x")

    async def fill() -> dict[str, Any]:
        await data.append_event(SCOPE, user("héllo ✓"), idempotency_key="k1")
        run_id = await data.prepare_run(SCOPE, preparation(b"\x00\xff\xfe raw \x80", "openrouter", "model-x"))
        await data.mark_dispatch(SCOPE, run_id)
        await data.record_raw_outcome(SCOPE, run_id, RawReceived(b"\x01resp\xff", "application/json",
                                                                 TokenUsage(10, 20, 5, None)))
        group = (ToolCall(CallId("a"), 0, ToolId("note"), {"n": 1.5, "f": 2.0, "nested": [{"k": None}, True], "workspace_binding_id": binding.binding_id}),
                 ToolCall(CallId("b"), 1, ToolId("fake/w"), {}))
        await data.project_model_output(SCOPE, run_id, AssistantToolCalls(group, commentary=(TextBlock("thinking"),)),
                                        continuation={"reasoning_details": [{"type": "enc", "data": "Zm9v", "n": 1.0}]})
        await data.commit_tool_outcome(SCOPE, CallRef(run_id, CallId("a")), ToolCompletion(
            ToolExecutionStatus.SUCCEEDED, text_result("noted"), raw_result={"bytes": 3, "list": [1, "2"]},
            artifact_mutations=(ArtifactMutation("note", {"text": "v1", "score": 3.0}),)))
        await data.claim_tool_execution(SCOPE, CallRef(run_id, CallId("b")),
                                        unknown_outcome=UnknownOutcomePolicy.REPORT_TO_MODEL, record_event=False)
        failed = await data.prepare_run(SCOPE, preparation(b"second"))
        await data.mark_dispatch(SCOPE, failed)
        await data.record_raw_outcome(SCOPE, failed, RawNoResponse("http_500", b"err body", "text/plain"))
        await data.replace_latest_snapshot(SCOPE, expected_revision=None, schema_version=3,
                                           body={"turns": [1, 2.25, "x"]}, covers_through_sequence=2,
                                           basis=PromptBasis(2, 1, None, {ArtifactId("art_1"): 1}))
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
    assert isinstance(after["artifacts"][0].content["score"], float)
    payload = thaw(after["runs"][0].continuation.payload)
    assert payload == {"reasoning_details": [{"type": "enc", "data": "Zm9v", "n": 1.0}]}
    assert isinstance(payload["reasoning_details"][0]["n"], float)


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


@pytest.mark.parametrize("version", [2, 3, 4, 5, 6, 7, 8, 9])
def test_an_older_database_is_upgraded_in_place_and_keeps_its_data(tmp_path: Path, version: int) -> None:
    path = tmp_path / "h.db"
    old_database(path, version)
    store = SqliteStore(path)
    try:
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 10
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
        asyncio.run(data.trash_conversation(SCOPE))  # and the v7 columns
    finally:
        store.close()
    SqliteStore(path).close()  # and it opens as a plain v10 database afterwards


@pytest.mark.parametrize("version", [2, 3, 4, 5, 6, 7, 8, 9])
@pytest.mark.parametrize("change", ["CREATE TABLE extra (x)", "PRAGMA application_id=1", "DROP TABLE artifacts"])
def test_only_an_exact_older_database_is_upgraded(tmp_path: Path, version: int, change: str) -> None:
    path = tmp_path / "h.db"
    old_database(path, version)
    tamper(path, change)
    with pytest.raises(StoreSchemaError):
        SqliteStore(path)
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == version  # refused, never repaired
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='instructions'").fetchall()  # v10's
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
    await data.commit_tool_outcome(SCOPE, CallRef(run_id, CallId("c1")), ToolCompletion(
        ToolExecutionStatus.SUCCEEDED, text_result("x"), artifact_mutations=(ArtifactMutation("note", "v1"),)))
    (note,) = await data.list_artifacts(SCOPE)
    done = ToolCompletion(ToolExecutionStatus.SUCCEEDED, text_result("ok"), raw_result=[1],
                          artifact_mutations=(ArtifactMutation("plan", "p"),
                                              ArtifactMutation("note", "v2", note.artifact_id, expected_revision=1)))
    return lambda: data.commit_tool_outcome(SCOPE, CallRef(run_id, CallId("c0")), done)


async def op_unknown(store: SqliteStore, data: SqliteData) -> Operation:
    run_id = await _received(data)
    await data.project_model_output(SCOPE, run_id, AssistantToolCalls(GROUP))
    call = CallRef(run_id, CallId("c1"))
    await data.claim_tool_execution(SCOPE, call, unknown_outcome=UnknownOutcomePolicy.REPORT_TO_MODEL, record_event=True)
    return lambda: data.commit_unknown_outcome(SCOPE, call, UNKNOWN_OUTCOME_RESULT)


async def op_answer(store: SqliteStore, data: SqliteData) -> Operation:
    run_id = await _received(data)
    await data.project_model_output(SCOPE, run_id, AssistantToolCalls((ToolCall(CallId("q"), 0, ASK_USER_TOOL_ID, {}),)))
    return lambda: data.answer_pending_call(SCOPE, CallRef(run_id, CallId("q")), user("yes"))


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
    async def activate() -> object:
        return store.activate_environment(SCOPE, FAKE, WorkspaceId("ws"))
    return activate


async def op_create(store: SqliteStore, data: SqliteData) -> Operation:
    activations = (InitialActivation(FAKE, WorkspaceId("ws_a")), InitialActivation(EnvironmentKind("other"), WorkspaceId("ws_b")))
    return lambda: data.create_conversation(PrincipalId("carol"), title="t", activations=activations)


async def op_purge(store: SqliteStore, data: SqliteData) -> Operation:
    run_id = await _received(data)
    await data.project_model_output(SCOPE, run_id, AssistantToolCalls(GROUP))
    view = AppView(UiResource.create("fake", "ui://fake/v", "<!DOCTYPE html><p>v</p>", UiCsp()), {"v": 1})
    await data.commit_tool_outcome(SCOPE, CallRef(run_id, CallId("c2")), ToolCompletion(
        ToolExecutionStatus.SUCCEEDED, text_result("v"), app_view=view, artifact_mutations=(ArtifactMutation("note", "n"),)))
    await data.set_external_setting(SCOPE, "fake", False)
    await data.set_model_setting(SCOPE, ModelSetting("m", None))
    await data.trash_conversation(SCOPE)
    await data.begin_purge(SCOPE)
    return lambda: data.purge_conversation(SCOPE)


@pytest.mark.parametrize("setup", [op_project, op_commit, op_unknown, op_answer, op_recover, op_append, op_final,
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


@pytest.mark.parametrize("op", ["project", "commit", "answer", "recover"])
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


@pytest.mark.parametrize("op", ["project", "commit", "answer", "recover"])
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
            assert len(asyncio.run(data.list_artifacts(SCOPE))) == 2
        elif op == "answer":
            assert asyncio.run(data.find_waiting_call(SCOPE)) is None
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
    write = RecordingTool("fake/write", effect=EffectProfile.UNKNOWN, environment_kind=FAKE,
                          workspace=WorkspaceRequirement.DEFAULT)
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
    tamper(path, *_ADDED_IN[10], *_ADDED_IN[9], "PRAGMA user_version=8")
    store = SqliteStore(path)
    try:
        again, = asyncio.run(SqliteData(store).read_file_records(SCOPE, [(record.file_id, 1)]))
        assert (again.name, again.image_status, again.view) == ("old.png", ImageStatus.NOT_IMAGE, None)
        assert view_state(again) is ViewState.NOT_IMAGE
    finally:
        store.close()
