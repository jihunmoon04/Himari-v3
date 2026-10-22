"""Inside and files (V3-P66): ``inside/present_file``, ``read_file``'s
offset, and placing uploads under ``uploads/``."""

from __future__ import annotations

import asyncio
import io
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from himari.common import (
    BindingId,
    CallId,
    ConversationId,
    EnvironmentInstanceId,
    PrincipalId,
    ToolId,
    TrustedScope,
    WorkspaceId,
    thaw,
)
from himari.data.api import BindingAuthority, ConversationWorkspace, StoredBlob
from himari.data.memory import InMemoryStore
from himari.environment.inside import (
    INSIDE,
    InsideEnvironment,
    InsideFiles,
    PartlyPlaced,
    Upload,
    UploadsFolderUnusable,
    WorkspaceBusy,
)
from himari.environment.inside import fs
from himari.tool.api import ToolContext, ToolInvocation, ToolOutcome, ToolStatus


ALICE = WorkspaceId("ws_alice")
SCOPE = TrustedScope(PrincipalId("alice"), ConversationId("c1"))


class _NoArtifacts:
    async def read_artifact(self, scope: Any, artifact_id: Any) -> Any:
        raise KeyError(artifact_id)

    async def list_artifacts(self, scope: Any, *, kind: Any = None) -> Any:
        return ()


def text(outcome: ToolOutcome) -> str:
    return str(outcome.model_result.blocks[0].text)  # type: ignore[union-attr]


class _Catalog:
    def __init__(self, given: int = 0) -> None:
        self.given = given

    async def presented_bytes_since(self, scope: TrustedScope, since: datetime) -> int:
        return self.given


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def env(tmp_path: Path, store: InMemoryStore) -> InsideEnvironment:
    environment = InsideEnvironment(tmp_path / "workspaces", files=InsideFiles(store.blobs, _Catalog(), max_bytes=1000))  # type: ignore[arg-type]
    environment.provision(ALICE)
    return environment


@pytest.fixture
def folder(env: InsideEnvironment) -> Path:
    path = Path(env.host_root(ALICE)) / "conversations/c1"
    path.mkdir(parents=True)
    return path


def run(env: InsideEnvironment, tool: str, **args: Any) -> ToolOutcome:
    handler = next(t for t in env.tools() if t.definition.tool_id == f"inside/{tool}")
    binding = BindingAuthority(BindingId("b1"), EnvironmentInstanceId("e1"), INSIDE, ALICE, 1)
    ctx = ToolContext(scope=SCOPE, artifacts=_NoArtifacts(), binding=binding,
                      workspace=ConversationWorkspace(ALICE, "conversations/c1"))
    return asyncio.run(handler.run(ToolInvocation(ToolId(f"inside/{tool}"), CallId("c"), args), ctx))


def stored(store: InMemoryStore, blob: StoredBlob) -> bytes:
    with store.blobs.open(blob) as handle:
        return handle.read()


# --- present_file ---------------------------------------------------------------------


def test_present_file_stores_the_bytes_and_carries_them_with_the_result(
        env: InsideEnvironment, store: InMemoryStore, folder: Path) -> None:
    (folder / "report.md").write_text("# done")
    outcome = run(env, "present_file", path="report.md")
    assert outcome.status is ToolStatus.SUCCEEDED
    assert text(outcome) == "Presented report.md to the user (6 bytes)."  # no version (review B3)
    given, = outcome.presented_files
    assert (given.name, given.media_type, given.workspace_path) == (
        "report.md", "text/markdown", "/workspace/conversations/c1/report.md")
    assert stored(store, given.blob) == b"# done"


def test_present_file_is_offered_only_with_files() -> None:
    plain = InsideEnvironment("/nonexistent/workspaces")
    assert "inside/present_file" not in {t.definition.tool_id for t in plain.tools()}


@pytest.mark.parametrize("make, status", [
    (lambda f: os.symlink("/etc/passwd", f / "x"), ToolStatus.DENIED),
    (lambda f: os.link((f / "y").write_text("y") and f / "y", f / "x"), ToolStatus.DENIED),
    (lambda f: (f / "x").mkdir(), ToolStatus.FAILED),
    (lambda f: (f / "x").write_bytes(b"z" * 1001), ToolStatus.FAILED),
])
def test_present_file_refuses_what_it_may_not_give(env: InsideEnvironment, store: InMemoryStore, folder: Path,
                                                   make: Any, status: ToolStatus) -> None:
    make(folder)
    outcome = run(env, "present_file", path="x")
    assert outcome.status is status and not outcome.presented_files
    assert store.blob_rows == {}


def test_present_file_checks_the_hours_limit_before_storing(tmp_path: Path, store: InMemoryStore) -> None:
    from himari.data.api import PRESENT_LIMIT_BYTES
    env = InsideEnvironment(tmp_path / "w", files=InsideFiles(store.blobs, _Catalog(PRESENT_LIMIT_BYTES - 3), 1000))  # type: ignore[arg-type]
    env.provision(ALICE)
    folder = Path(env.host_root(ALICE)) / "conversations/c1"
    folder.mkdir(parents=True)
    (folder / "four.txt").write_text("four")
    outcome = run(env, "present_file", path="four.txt")
    assert outcome.status is ToolStatus.FAILED and "last hour" in text(outcome)
    assert store.blob_rows == {}  # nothing stored (review N1)


def test_present_file_refuses_a_file_that_changes_while_read(env: InsideEnvironment, folder: Path,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    (folder / "live.log").write_text("first")
    real = fs.read_file_with_identity

    def racing(root_fd: int, relative: str, max_bytes: int, offset: int = 0):  # type: ignore[no-untyped-def]
        result = real(root_fd, relative, max_bytes, offset)
        (folder / "live.log").write_text("second, longer")
        return result

    monkeypatch.setattr(fs, "read_file_with_identity", racing)
    outcome = run(env, "present_file", path="live.log")
    assert outcome.status is ToolStatus.FAILED and "changed while it was being read" in text(outcome)


def test_present_file_reports_a_full_store(env: InsideEnvironment, store: InMemoryStore, folder: Path) -> None:
    store.blobs.configure(max_bytes=3, keep_days=30)
    (folder / "big.txt").write_text("four")
    outcome = run(env, "present_file", path="big.txt")
    assert outcome.status is ToolStatus.FAILED and "full" in text(outcome)


# --- read_file's offset ---------------------------------------------------------------------


def test_read_file_reads_on_from_an_offset(env: InsideEnvironment, folder: Path) -> None:
    from himari.environment.inside.tools import MAX_OBSERVATION_BYTES
    content = ("é" * MAX_OBSERVATION_BYTES).encode()  # two bytes a character
    (folder / "long.txt").write_bytes(content)
    seen = b""
    offset = 0
    for _ in range(10):
        outcome = run(env, "read_file", path="long.txt", offset=offset)
        raw = thaw(outcome.raw_result)
        body = text(outcome).split("\n", 1)[1]
        seen += body.split("\n[truncated", 1)[0].encode()
        if raw["next_offset"] is None:  # type: ignore[index]
            break
        offset = raw["next_offset"]  # type: ignore[index]
        assert f"read on with offset={offset}" in text(outcome)
    assert seen == content  # every byte once, no character split


def test_read_file_past_the_end(env: InsideEnvironment, folder: Path) -> None:
    (folder / "s.txt").write_text("short")
    assert "past the end" in text(run(env, "read_file", path="s.txt", offset=99))


# --- placing uploads (§5.2) -----------------------------------------------------------------


def place(env: InsideEnvironment, *uploads: Upload) -> list[str]:
    async def go() -> list[str]:
        async with env.locked(ALICE, 1):
            return await env.place_uploads(ALICE, "conversations/c1", uploads)
    return asyncio.run(go())


def upload(name: str, data: bytes, placed_at: str | None = None) -> Upload:
    return Upload(name, lambda: io.BytesIO(data), placed_at)


def test_uploads_go_under_uploads_and_never_replace(env: InsideEnvironment, folder: Path) -> None:
    first = place(env, upload("r.pdf", b"one"), upload("r.pdf", b"two"), upload("notes", b"n"))
    assert first == ["/workspace/conversations/c1/uploads/r.pdf", "/workspace/conversations/c1/uploads/r (2).pdf",
                     "/workspace/conversations/c1/uploads/notes"]
    again = place(env, upload("notes", b"m"))
    assert again == ["/workspace/conversations/c1/uploads/notes (2)"]
    assert (folder / "uploads/r.pdf").read_bytes() == b"one" and (folder / "uploads/r (2).pdf").read_bytes() == b"two"
    assert all(os.stat(p).st_nlink == 1 for p in (folder / "uploads").iterdir())


def test_a_retry_keeps_what_was_placed(env: InsideEnvironment, folder: Path) -> None:
    path, = place(env, upload("a.txt", b"a"))
    assert place(env, upload("a.txt", b"a", placed_at=path)) == [path]
    (folder / "uploads/a.txt").unlink()
    assert place(env, upload("a.txt", b"a", placed_at=path)) == [path]  # gone since: placed again
    assert sorted(p.name for p in (folder / "uploads").iterdir()) == ["a.txt"]


def test_a_failed_copy_leaves_only_a_temporary_file_that_the_next_placing_removes(
        env: InsideEnvironment, folder: Path) -> None:
    class Broken(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            raise OSError(28, "No space left on device")

    with pytest.raises(PartlyPlaced) as stopped:
        place(env, Upload("a.txt", lambda: Broken()))
    assert stopped.value.placed == [] and isinstance(stopped.value.cause, fs.FsError)
    assert list((folder / "uploads").iterdir()) == []  # the temporary file was removed too
    (folder / "uploads/.himari-left.tmp").write_bytes(b"crash")  # a crash leaves one
    (folder / "uploads/.himari-dir.tmp").mkdir()  # not a regular file: left alone
    assert place(env, upload("a.txt", b"a")) == ["/workspace/conversations/c1/uploads/a.txt"]
    assert sorted(p.name for p in (folder / "uploads").iterdir()) == [".himari-dir.tmp", "a.txt"]


@pytest.mark.parametrize("make", [lambda f: os.symlink("/tmp", f / "uploads"),
                                  lambda f: (f / "uploads").write_text("a file")])
def test_an_uploads_that_is_not_a_folder_is_refused(env: InsideEnvironment, folder: Path, make: Any) -> None:
    make(folder)
    with pytest.raises(UploadsFolderUnusable):
        place(env, upload("a.txt", b"a"))


def test_placing_waits_for_the_write_lock_only_so_long(env: InsideEnvironment, folder: Path) -> None:
    async def go() -> None:
        async with env.write_lock(ALICE):
            with pytest.raises(WorkspaceBusy):
                async with env.locked(ALICE, 0.05):
                    pass
        async with env.locked(ALICE, 0.05):  # free again, and not left held by the timed-out wait
            pass

    asyncio.run(go())


def test_placement_works_here(env: InsideEnvironment) -> None:
    assert env.placement_works()
    assert not [p for p in Path(env.host_root(ALICE)).parent.iterdir() if p.name.startswith(".himari-")]


def test_a_stop_part_way_says_what_was_placed(env: InsideEnvironment, folder: Path) -> None:
    def broken() -> io.BytesIO:
        raise OSError(28, "No space left on device")

    with pytest.raises(PartlyPlaced) as stopped:
        place(env, upload("a.txt", b"a"), Upload("b.txt", broken))
    assert stopped.value.placed == ["/workspace/conversations/c1/uploads/a.txt"]  # PR #59 review C1


def test_the_directory_is_made_durable_after_the_rename(env: InsideEnvironment, folder: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    import stat as stat_module
    synced: list[bool] = []
    real = os.fsync

    def recording(fd: int) -> None:
        synced.append(stat_module.S_ISDIR(os.fstat(fd).st_mode))
        real(fd)

    monkeypatch.setattr(os, "fsync", recording)
    place(env, upload("a.txt", b"a"))
    assert synced == [False, True]  # the file, then its directory (review N2, C2)


def test_a_read_window_ending_inside_a_character_is_not_invalid(env: InsideEnvironment, folder: Path) -> None:
    from himari.environment.inside.tools import MAX_READ_BYTES
    (folder / "k.txt").write_bytes("한".encode() * (MAX_READ_BYTES // 3 + 5))
    outcome = run(env, "read_file", path="k.txt", offset=1)  # starts inside a character: that byte is shown as U+FFFD
    assert "not valid UTF-8" in text(outcome)
    outcome = run(env, "read_file", path="k.txt", offset=3)
    assert "not valid UTF-8" not in text(outcome)  # PR #59 review C12
