"""SQLite-only guarantees of V3-P65: a purge leaves no row of the Conversation
in any table, freed pages are overwritten, and an upgrade runs only after a
checked, durable backup."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest
from testkit import SCOPE, V15_UNDO, text_result, user

from himari.common import CallId, CallRef, ConversationId, RawRepresentation, ToolCall, ToolId, TrustedScope
from himari.data.api import (
    EpisodePlan,
    MemoryCategory,
    PassAction,
    PassItem,
    PassKind,
    PassStart,
    AppView,
    ArtifactMutation,
    AssistantToolCalls,
    ModelSetting,
    PresentedFile,
    PromptBasis,
    RawReceived,
    RunPreparation,
    RunPurpose,
    ToolCompletion,
    ToolExecutionStatus,
    UiCsp,
    UiResource,
)
from himari.data.sqlite import SCHEMA_VERSION, SqliteData, SqliteStore, StoreBackupError

OTHER = TrustedScope(SCOPE.principal_id, ConversationId("conv_other"))


@pytest.fixture(autouse=True)
def _every_data_backend() -> None:
    """Overrides tests/data/conftest.py: these tests are SQLite-only."""


def preparation(purpose: RunPurpose = RunPurpose.TURN) -> RunPreparation:
    return RunPreparation(b"{}", "application/json", RawRepresentation.WIRE_EXACT, (), PromptBasis(0, 0), "p", "m",
                          purpose)


async def chunks(data: bytes):  # type: ignore[no-untyped-def]
    yield data


async def fill(data: SqliteData, scope: TrustedScope) -> None:
    """A row in every table a Conversation can have."""
    upload = await data.blobs.put(scope.principal_id, chunks(f"up {scope.conversation_id}".encode()), 1000)
    await data.stage_upload(scope, upload, "u.txt", "text/plain")
    await data.append_event(scope, user("hello"), idempotency_key="k")
    run_id = await data.prepare_run(scope, preparation())
    await data.mark_dispatch(scope, run_id)
    await data.record_raw_outcome(scope, run_id, RawReceived(b"r", "application/json"))
    await data.project_model_output(scope, run_id, AssistantToolCalls((ToolCall(CallId("c"), 0, ToolId("fake/v"), {}),)))
    view = AppView(UiResource.create("fake", f"ui://fake/{scope.conversation_id}", "<!DOCTYPE html>", UiCsp()), {})
    given = await data.blobs.put(scope.principal_id, chunks(f"out {scope.conversation_id}".encode()), 1000)
    await data.commit_tool_outcome(scope, CallRef(run_id, CallId("c")), ToolCompletion(
        ToolExecutionStatus.SUCCEEDED, text_result("v"), app_view=view, artifact_mutations=(ArtifactMutation("note", "n"),),
        presented_files=(PresentedFile(given, "o.txt", "text/plain", "/workspace/o.txt"),)))
    now = datetime.now(UTC)  # V3-P85: an applied memory pass: its cursor, episode, call and item
    pass_id = await data.begin_pass(scope, PassStart(PassKind.LIVE, 0, 3, 3, "p", "m"))
    call = await data.prepare_pass_call(pass_id, b"{}", "application/json", 10)
    await data.record_pass_call(pass_id, call, RawReceived(b"{}", "application/json"))
    await data.apply_pass(pass_id, [PassItem(0, "{}", PassAction.ADD, category=MemoryCategory.USER,
                                             text=f"Note of {scope.conversation_id}.")],
                          EpisodePlan("Talked.", now, now, (1,), ()), now=now)
    await data.prepare_run(scope, preparation(RunPurpose.COMPACTION))
    await data.set_external_setting(scope, "fake", False)
    await data.set_model_setting(scope, ModelSetting("m", None))


def rows_of(conn: sqlite3.Connection, conversation_id: str) -> dict[str, int]:
    """Rows naming ``conversation_id`` in every table that has the column."""
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    counts = {}
    for table in tables:
        if "conversation_id" in [c[1] for c in conn.execute(f"PRAGMA table_info({table})")]:
            counts[table] = conn.execute(f"SELECT count(*) FROM {table} WHERE conversation_id=?",
                                         (conversation_id,)).fetchone()[0]
    return counts


def test_a_purge_leaves_no_row_of_the_conversation_in_any_table(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "h.db")
    data = SqliteData(store)
    store.create_conversation(SCOPE)
    store.create_conversation(OTHER)

    async def scenario() -> None:
        await fill(data, SCOPE)
        await fill(data, OTHER)
        await data.trash_conversation(SCOPE)
        await data.begin_purge(SCOPE)
        await data.purge_conversation(SCOPE)

    asyncio.run(scenario())
    conn = store.connection
    counts = rows_of(conn, SCOPE.conversation_id)
    assert len(counts) >= 10 and set(counts.values()) == {0}, counts
    assert all(n > 0 for table, n in rows_of(conn, OTHER.conversation_id).items()
               if table not in ("activations", "bindings")), "the other Conversation keeps its rows"
    assert conn.execute("SELECT count(*) FROM compaction_runs").fetchone()[0] == 1  # only the other one's
    for table in ("memory_pass_calls", "memory_pass_items", "memories"):  # V3-P85: only the other one's
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 1, table
    assert conn.execute("SELECT count(*) FROM ui_resources").fetchone()[0] == 1
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    store.close()


def test_freed_pages_are_overwritten(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "h.db")
    assert store.connection.execute("PRAGMA secure_delete").fetchone()[0] == 1
    store.close()


# --- backup before upgrade (§8.1) ----------------------------------------------------------


def v6_database(path: Path) -> None:
    """A database as schema v6 wrote it, with one Conversation and one Event."""
    store = SqliteStore(path)
    store.create_conversation(SCOPE)
    asyncio.run(SqliteData(store).append_event(SCOPE, user("from v6")))
    store.close()
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        for statement in ("DROP TABLE presence_settings", *V15_UNDO, "DROP TABLE device_ids", "DROP INDEX devices_by_pairing", "DROP INDEX devices_by_token",
                          "DROP TABLE devices", "DROP INDEX bindings_by_workspace", "DROP TABLE workspaces",
                          "DROP TABLE event_text", "DROP TABLE memory_ids", "DROP INDEX memories_by_source",
                          "DROP TABLE memories",
                          "DROP TABLE instructions", "DROP INDEX files_by_view", "DROP TABLE files", "DROP TABLE blobs",
                          "ALTER TABLE conversations DROP COLUMN purge_started_at",
                          "ALTER TABLE conversations DROP COLUMN trashed_at", "PRAGMA user_version=6"):
            conn.execute(statement)
    finally:
        conn.close()


def version_of(path: Path) -> int:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def dump(path: Path) -> list[str]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return list(conn.iterdump())
    finally:
        conn.close()


@pytest.fixture
def fsyncs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Records what each os.fsync was given: 'dir' or 'file'."""
    seen: list[str] = []
    real = os.fsync

    def recording(fd: int) -> None:
        seen.append("dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        real(fd)

    monkeypatch.setattr(os, "fsync", recording)
    return seen


def test_an_upgrade_backs_up_the_older_database_first(tmp_path: Path, fsyncs: list[str]) -> None:
    path = tmp_path / "h.db"
    v6_database(path)
    before = dump(path)
    store = SqliteStore(path)
    try:
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert store.backup_path is not None
        backup = Path(store.backup_path)
    finally:
        store.close()
    assert backup.parent == tmp_path and backup.name.startswith("h.db.v6-") and backup.suffix == ".bak"
    info = backup.lstat()
    assert stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1
    assert dump(backup) == before  # exactly the v6 database
    assert sorted(p.name for p in tmp_path.iterdir() if ".bak" in p.name) == [backup.name]  # no .partial left
    assert "file" in fsyncs and "dir" in fsyncs and fsyncs.index("file") < fsyncs.index("dir")

    again = SqliteStore(path)  # already v7: nothing to back up
    assert again.backup_path is None
    again.close()
    assert len(list(tmp_path.glob("*.bak"))) == 1


def test_a_new_database_is_not_backed_up(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "h.db")
    assert store.backup_path is None
    store.close()
    assert not list(tmp_path.glob("*.bak*"))


@pytest.mark.parametrize("failure", ["link", "file fsync", "dir fsync"])
def test_a_failed_backup_upgrades_nothing_and_leaves_no_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                              failure: str) -> None:
    path = tmp_path / "h.db"
    v6_database(path)
    before = dump(path)
    real_fsync = os.fsync

    def failing_fsync(fd: int) -> None:
        if ("dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file") == failure.split()[0]:
            raise OSError(5, "Input/output error")
        real_fsync(fd)

    def failing_link(source: str, target: str) -> None:
        raise FileExistsError(17, "File exists", target)

    with monkeypatch.context() as patch:
        if failure == "link":
            patch.setattr(os, "link", failing_link)
        else:
            patch.setattr(os, "fsync", failing_fsync)
        with pytest.raises(StoreBackupError, match="nothing was upgraded") as exc:
            SqliteStore(path)
    assert "could not remove" not in str(exc.value)
    assert dump(path) == before and version_of(path) == 6  # still the v6 database
    assert not [p for p in tmp_path.iterdir() if ".bak" in p.name]

    store = SqliteStore(path)  # the lock was released; once the disk is fine, it upgrades
    assert store.backup_path is not None
    store.close()


def test_a_backup_that_cannot_be_removed_is_named_in_the_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "h.db"
    v6_database(path)
    real_fsync = os.fsync

    def failing_fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(5, "Input/output error")
        real_fsync(fd)

    real_unlink = os.unlink

    def stubborn_unlink(target: str, *args: object, **kwargs: object) -> None:
        if str(target).endswith(".bak"):
            raise PermissionError(13, "Permission denied", target)
        real_unlink(target, *args, **kwargs)  # type: ignore[arg-type]

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", failing_fsync)
        patch.setattr(os, "unlink", stubborn_unlink)
        with pytest.raises(StoreBackupError, match=r"could not remove .*h\.db\.v6-.*\.bak") as exc:
            SqliteStore(path)
    assert "Input/output error" in str(exc.value)
    assert version_of(path) == 6
