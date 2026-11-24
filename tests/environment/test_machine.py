"""The machine Environment (V3-P74, V3-P109): its Tools on each Conversation's
own folder under a temporary data root, in this process; path locks."""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import threading
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
from himari.data.api import BindingAuthority, ConversationWorkspace
from himari.environment.api import ConnectionStatus, EnvironmentPlugin
from himari.environment.machine import MACHINE, MachineEnvironment, MachineUnavailable, machine_workspace_for
from himari.environment.machine.errors import FolderError
from himari.environment.machine.folders import ConversationFolders
from himari.environment.machine.locks import PathLocks, overlap
from himari.environment.machine.results import ToolFiles
from himari.tool.api import EffectProfile, ToolContext, ToolHandler, ToolInvocation, ToolOutcome, ToolStatus
from himari.tool.registry import ToolRegistry

SCOPE = TrustedScope(PrincipalId("operator"), ConversationId("c1"))
WORKSPACE = machine_workspace_for(PrincipalId("operator"))


class _NoArtifacts:
    async def read_artifact(self, scope: Any, artifact_id: Any) -> Any:
        raise KeyError(artifact_id)

    async def list_artifacts(self, scope: Any, *, kind: Any = None) -> Any:
        return ()


class _NothingGiven:
    """``present_file``'s catalog before anything is stored: nothing given yet."""

    async def presented_bytes_since(self, scope: Any, since: Any) -> int:
        return 0


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "data").mkdir(mode=0o700)
    return tmp_path / "data" / "conversations"


@pytest.fixture
def env(root: Path) -> MachineEnvironment:
    return MachineEnvironment(str(root))


def binding() -> BindingAuthority:
    return BindingAuthority(BindingId("b1"), EnvironmentInstanceId("e1"), MACHINE, WORKSPACE, 1, "conversations/c1")


def _handler(env: MachineEnvironment, tool: str) -> tuple[ToolHandler, ToolContext]:
    handler = next(t for t in env.tools() if t.definition.tool_id == f"machine/{tool}")
    ctx = ToolContext(scope=SCOPE, artifacts=_NoArtifacts(), binding=binding(),
                      workspace=ConversationWorkspace(WORKSPACE, "conversations/c1"))
    return handler, ctx


def call(env: MachineEnvironment, tool: str, **args: Any) -> ToolOutcome:
    handler, ctx = _handler(env, tool)
    return asyncio.run(handler.run(ToolInvocation(ToolId(f"machine/{tool}"), CallId("c"), args), ctx))


def text(outcome: ToolOutcome) -> str:
    block = outcome.model_result.blocks[0]
    assert hasattr(block, "text")
    return str(block.text)


def raw(outcome: ToolOutcome) -> dict[str, Any]:
    found = thaw(outcome.raw_result)
    assert isinstance(found, dict)
    return found


def test_plugin_conforms(env: MachineEnvironment) -> None:
    assert isinstance(env, EnvironmentPlugin) and env.kind == MACHINE
    tools = list(env.tools())
    assert all(isinstance(t, ToolHandler) and t.definition.tool_id.startswith("machine/") for t in tools)
    assert all("workspace_binding_id" not in t.definition.input_schema["properties"] for t in tools)  # type: ignore[operator]
    effects = {str(t.definition.tool_id): t.definition.effect_profile for t in tools}
    assert effects["machine/read_file"] is EffectProfile.READ
    assert "machine/git_push" not in effects and "machine/run_command" not in effects  # V3-P100
    for tool in tools:  # V3-P109 §4.2: each stays in the folder; none mentions a server or a home
        described = tool.definition.description
        assert "conversation's own folder" in described or "conversation's folder" in described, tool.definition.tool_id
        assert "on this server" not in described and "~" not in described, tool.definition.tool_id
    ToolRegistry(tools)
    assert MachineEnvironment.owns(WORKSPACE) and not MachineEnvironment.owns(WorkspaceId("ws_x"))


def test_round_trip_in_the_conversation_folder(env: MachineEnvironment, root: Path) -> None:
    folder = root / "c1"
    written = call(env, "write_file", path="src/main.py", content="print('hi')\n")
    assert written.status is ToolStatus.SUCCEEDED and text(written) == f"created {folder}/src/main.py (12 bytes)"
    assert text(call(env, "read_file", path="src/main.py")) == f"{folder}/src/main.py (12 bytes)\nprint('hi')\n"
    assert call(env, "edit_file", path="src/main.py", old_text="hi", new_text="hello").status is ToolStatus.SUCCEEDED
    assert (folder / "src/main.py").read_text() == "print('hello')\n"
    assert "src/" in text(call(env, "list_files"))
    assert call(env, "delete_file", path=f"{folder}/src/main.py").status is ToolStatus.SUCCEEDED
    assert not (folder / "src/main.py").exists()


def test_the_folder_and_its_files_are_private(env: MachineEnvironment, root: Path) -> None:
    """§3 D1: folders 0700, new files 0600; a rewrite keeps the file's mode; no temporary file is left."""
    call(env, "write_file", path="deep/a.txt", content="x")
    assert (root.stat().st_mode & 0o777, (root / "c1").stat().st_mode & 0o777) == (0o700, 0o700)
    assert (root / "c1/deep").stat().st_mode & 0o777 == 0o700
    assert (root / "c1/deep/a.txt").stat().st_mode & 0o777 == 0o600
    (root / "c1/deep/a.txt").chmod(0o640)
    call(env, "write_file", path="deep/a.txt", content="y")
    assert (root / "c1/deep/a.txt").stat().st_mode & 0o777 == 0o640
    assert not [p for p in (root / "c1/deep").iterdir() if p.name.endswith(".tmp")]


@pytest.mark.parametrize("tool,args", [
    ("read_file", {"path": "/etc/hostname"}),
    ("read_file", {"path": "../c2/secret.txt"}),
    ("list_files", {"path": ".."}),
    ("list_files", {"path": "/"}),
    ("write_file", {"path": "../c2/secret.txt", "content": "x"}),
    ("write_file", {"path": "/tmp/himari-v3-p109-outside.txt", "content": "x"}),
    ("edit_file", {"path": "../c2/secret.txt", "old_text": "theirs", "new_text": "mine"}),
    ("delete_file", {"path": "../c2/secret.txt"}),
    ("present_file", {"path": "../c2/secret.txt"}),
])
def test_nothing_outside_the_folder_is_read_or_written(root: Path, tool: str, args: dict[str, Any]) -> None:
    """Q1: reading is as narrow as writing; leaving the folder is DENIED, and nothing changes."""
    env = MachineEnvironment(str(root), files=ToolFiles(None, _NothingGiven()))  # type: ignore[arg-type]
    (root / "c2").mkdir(parents=True)
    (root / "c2/secret.txt").write_text("theirs")
    outcome = call(env, tool, **args)
    assert outcome.status is ToolStatus.DENIED, text(outcome)
    assert "outside this conversation's folder" in text(outcome)
    assert (root / "c2/secret.txt").read_text() == "theirs"
    assert not Path("/tmp/himari-v3-p109-outside.txt").exists()


def test_a_home_path_is_not_a_path_here(env: MachineEnvironment) -> None:
    outcome = call(env, "read_file", path="~/notes.txt")
    assert outcome.status is ToolStatus.FAILED and "~ is not used here" in text(outcome)


def test_links_are_never_followed(env: MachineEnvironment, root: Path, tmp_path: Path) -> None:
    """§3 D5: a link in the folder — at the end or on the way — is refused, whatever it points to."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f.txt").write_text("outside")
    call(env, "write_file", path="keep.txt", content="mine")
    folder = root / "c1"
    os.symlink(outside / "f.txt", folder / "file-link")
    os.symlink(outside, folder / "dir-link")
    os.symlink(folder / "keep.txt", folder / "inner-link")  # even one that stays inside
    for path in ("file-link", "dir-link/f.txt", "inner-link"):
        read = call(env, "read_file", path=path)
        assert read.status is ToolStatus.FAILED and "links" in text(read), path
        wrote = call(env, "write_file", path=path, content="x")
        assert wrote.status is ToolStatus.FAILED and "links" in text(wrote), path
    assert call(env, "list_files", path="dir-link").status is ToolStatus.FAILED
    assert call(env, "write_file", path="dir-link/new/x.txt", content="x").status is ToolStatus.FAILED
    assert (outside / "f.txt").read_text() == "outside" and not (outside / "new").exists()
    assert (folder / "keep.txt").read_text() == "mine"
    listing = text(call(env, "list_files"))
    assert "file-link  [symlink]" in listing and "dir-link  [symlink]" in listing
    assert call(env, "delete_file", path="file-link").status is ToolStatus.SUCCEEDED  # the link itself
    assert (outside / "f.txt").exists() and not (folder / "file-link").is_symlink()


def test_the_folder_itself_as_a_link_is_refused(env: MachineEnvironment, root: Path, tmp_path: Path) -> None:
    root.mkdir(mode=0o700)
    (tmp_path / "elsewhere").mkdir()
    os.symlink(tmp_path / "elsewhere", root / "c1")
    outcome = call(env, "write_file", path="a.txt", content="x")
    assert outcome.status is ToolStatus.DENIED and "folder cannot be used" in text(outcome)
    assert not (tmp_path / "elsewhere/a.txt").exists()
    observed = asyncio.run(env.observe(SCOPE, [binding()]))
    assert observed[0].status is ConnectionStatus.OFFLINE and observed[0].working_directory is None


def test_edit_refuses_a_file_changed_meanwhile(env: MachineEnvironment, root: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    call(env, "write_file", path="f.txt", content="one two")
    target = root / "c1/f.txt"
    real = ConversationFolders.open_read

    def racing(self: ConversationFolders, folder: str, path: object) -> Any:
        opened = real(self, folder, path)
        target.write_text("someone else")
        return opened

    monkeypatch.setattr(ConversationFolders, "open_read", racing)
    edited = call(env, "edit_file", path="f.txt", old_text="one", new_text="1")
    assert edited.status is ToolStatus.FAILED and "changed" in text(edited)
    assert target.read_text() == "someone else"


def test_reading_in_parts_and_listing_order(env: MachineEnvironment, root: Path) -> None:
    folder = root / "c1"
    folder.mkdir(parents=True)
    (folder / "big.txt").write_bytes(b"a" * 300_000)
    (folder / "b.txt").write_text("b")
    (folder / "a-dir").mkdir()
    (folder / "Z.txt").write_text("z")
    first = raw(call(env, "read_file", path="big.txt"))
    assert first["truncated"] and first["next_offset"]
    rest = call(env, "read_file", path="big.txt", offset=first["next_offset"])
    assert rest.status is ToolStatus.SUCCEEDED and raw(rest)["offset"] == first["next_offset"]
    lines = text(call(env, "list_files")).splitlines()
    assert lines[1:] == ["a-dir/", "Z.txt  (1 bytes)", "b.txt  (1 bytes)", "big.txt  (300000 bytes)"]
    assert call(env, "read_file", path="a-dir").status is ToolStatus.FAILED
    assert call(env, "delete_file", path="a-dir").status is ToolStatus.FAILED
    assert call(env, "read_file", path="missing.txt").status is ToolStatus.FAILED
    assert call(env, "write_file", path=".", content="x").status is ToolStatus.FAILED


def test_observe_tells_the_folder(env: MachineEnvironment, root: Path) -> None:
    observed = asyncio.run(env.observe(SCOPE, [binding()]))
    folder = str(root / "c1")
    assert observed[0].status is ConnectionStatus.ONLINE
    assert observed[0].working_directory == folder and observed[0].writable == (folder,)
    assert observed[0].readable is not None and "only this conversation's folder" in observed[0].readable


def test_another_binding_folder_is_not_used(env: MachineEnvironment) -> None:
    with pytest.raises(MachineUnavailable):
        asyncio.run(env.folder("c1", "conversations/c2"))
    with pytest.raises(MachineUnavailable):
        asyncio.run(env.folder("../x", "conversations/../x"))


def test_uploads_and_removal(env: MachineEnvironment, root: Path) -> None:
    async def place(data: bytes, preferred: str | None = None) -> str:
        async with env.locked(WORKSPACE, 5, conversation_id="c1", cwd_relative="conversations/c1"):
            temp = await env.copy_in("c1", "conversations/c1", io.BytesIO(data), clear=True)
            path = await env.free_path("c1", "conversations/c1", "report.pdf", preferred)
            assert await env.commit_copy("c1", "conversations/c1", temp, path)
            await env.discard_copy("c1", "conversations/c1", temp)
            return path

    uploads = root / "c1/uploads"
    path = asyncio.run(place(b"bytes"))
    assert path == f"{uploads}/report.pdf" and Path(path).read_bytes() == b"bytes"
    assert Path(path).stat().st_mode & 0o777 == 0o600
    assert asyncio.run(place(b"other")) == f"{uploads}/report (2).pdf"  # never replaces
    assert asyncio.run(place(b"third", preferred=f"{uploads}/mine.pdf")) == f"{uploads}/mine.pdf"
    old = "/home/coder/HIMARI/conversations/c1/uploads/report.pdf"  # §5: an old folder's recorded path
    assert asyncio.run(place(b"fourth", preferred=old)) == f"{uploads}/report (3).pdf"
    assert sorted(p.name for p in uploads.iterdir()) == ["mine.pdf", "report (2).pdf", "report (3).pdf", "report.pdf"]
    digest = hashlib.sha256(b"bytes").hexdigest()
    assert asyncio.run(env.holds("c1", "conversations/c1", path, 5, digest))
    assert not asyncio.run(env.holds("c1", "conversations/c1", path, 6, digest))
    assert not asyncio.run(env.holds("c1", "conversations/c1", old, 5, digest))

    async def taken() -> bool:
        temp = await env.copy_in("c1", "conversations/c1", io.BytesIO(b"x"))
        try:
            return await env.commit_copy("c1", "conversations/c1", temp, path)
        finally:
            await env.discard_copy("c1", "conversations/c1", temp)

    assert not asyncio.run(taken()) and Path(path).read_bytes() == b"bytes"
    assert not [p for p in uploads.iterdir() if p.name.endswith(".tmp")]
    outside = root.parent / "keep-me"
    outside.mkdir()
    (outside / "f").write_text("kept")
    os.symlink(outside, root / "c1/link-inside")
    assert asyncio.run(env.remove_conversation_folder(WORKSPACE, SCOPE, "conversations/c1"))
    assert not (root / "c1").exists() and (outside / "f").read_text() == "kept"
    assert not asyncio.run(env.remove_conversation_folder(WORKSPACE, SCOPE, "conversations/c1"))
    assert not asyncio.run(env.remove_conversation_folder(WORKSPACE, SCOPE, "projects"))


def test_upload_steps_refuse_names_outside_uploads(env: MachineEnvironment, root: Path) -> None:
    async def scenario() -> None:
        temp = await env.copy_in("c1", "conversations/c1", io.BytesIO(b"x"))
        with pytest.raises(MachineUnavailable):
            await env.commit_copy("c1", "conversations/c1", temp, str(root / "c1/escaped.pdf"))
        with pytest.raises(MachineUnavailable):
            await env.commit_copy("c1", "conversations/c1", "../x.tmp", str(root / "c1/uploads/a.pdf"))
        with pytest.raises(MachineUnavailable):
            await env.free_path("c1", "conversations/c1", "../a.pdf", None)
        await env.discard_copy("c1", "conversations/c1", temp)

    asyncio.run(scenario())
    assert not (root / "c1/escaped.pdf").exists() and not (root / "c1/uploads/a.pdf").exists()


def test_folder_ids_are_checked() -> None:
    folders = ConversationFolders("/tmp/x")
    for bad in ("", "..", "a/b", "a" * 129, 5):
        with pytest.raises(FolderError):
            folders.path_of(bad)
    with pytest.raises(ValueError):
        ConversationFolders("relative/root")


# --- path locks (§9.1) ----------------------------------------------------------------


def test_overlap_is_by_whole_components() -> None:
    assert overlap(["/h/a"], ["/h/a"]) and overlap(["/h"], ["/h/a/b"]) and overlap(["/h/a/b"], ["/h"])
    assert not overlap(["/h/a"], ["/h/ab"]) and not overlap(["/h/a"], ["/h/b"])


def test_locks_wait_only_for_overlapping_paths() -> None:
    async def scenario() -> list[str]:
        locks = PathLocks()
        order: list[str] = []
        release = asyncio.Event()

        async def hold(name: str, paths: list[str], wait: bool = False) -> None:
            async with locks.hold(paths):
                order.append(f"{name}+")
                if wait:
                    await release.wait()
                order.append(f"{name}-")

        first = asyncio.create_task(hold("parent", ["/h/HIMARI"], wait=True))
        await asyncio.sleep(0)
        child = asyncio.create_task(hold("child", ["/h/HIMARI/conversations/c1"]))
        other = asyncio.create_task(hold("other", ["/h/projects/x"]))
        await asyncio.sleep(0.05)
        assert order == ["parent+", "other+", "other-"]  # the child waits, the other path does not
        release.set()
        await asyncio.gather(first, child, other)
        with pytest.raises(TimeoutError):
            async with locks.hold(["/h"]):
                async with locks.hold(["/h/x"], timeout=0.05):
                    pass
        return order

    assert asyncio.run(scenario())[-2:] == ["child+", "child-"]


def test_overlapping_requests_keep_their_order() -> None:
    async def scenario() -> list[str]:
        locks = PathLocks()
        order: list[str] = []
        gate = asyncio.Event()

        async def hold(name: str, paths: list[str]) -> None:
            async with locks.hold(paths):
                order.append(name)
                if name == "a":
                    await gate.wait()

        a = asyncio.create_task(hold("a", ["/h/x"]))
        await asyncio.sleep(0)
        big = asyncio.create_task(hold("big", ["/h"]))  # waits for a
        await asyncio.sleep(0)
        late = asyncio.create_task(hold("late", ["/h/y"]))  # overlaps big, which came first: waits
        await asyncio.sleep(0.05)
        assert order == ["a"]
        gate.set()
        await asyncio.gather(a, big, late)
        return order

    assert asyncio.run(scenario()) == ["a", "big", "late"]


def test_a_cancelled_waiter_leaves_nothing_behind() -> None:
    async def scenario() -> None:
        locks = PathLocks()
        async with locks.hold(["/h"]):
            waiter = asyncio.create_task(locks.hold(["/h/a"]).__aenter__())
            await asyncio.sleep(0.01)
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        async with locks.hold(["/h/a"], timeout=0.1):
            pass

    asyncio.run(scenario())


def _logged_writes(monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], threading.Event, threading.Event]:
    """File writes log when they start and end; the first one blocks in its
    thread until ``gate`` is set."""
    log: list[str] = []
    entered, gate = threading.Event(), threading.Event()
    real = ConversationFolders.write

    def logged(folders: ConversationFolders, folder: str, path: object, data: bytes, **options: Any) -> Any:
        number = sum(1 for e in log if e.startswith("start")) + 1
        log.append(f"start {number}")
        if number == 1:
            entered.set()
            gate.wait(10)
        try:
            return real(folders, folder, path, data, **options)
        finally:
            log.append(f"end {number}")

    monkeypatch.setattr(ConversationFolders, "write", logged)
    return log, entered, gate


def test_a_cancelled_write_keeps_the_folder_until_it_ends(env: MachineEnvironment, root: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """PR #87 review C1 (§3 D8): cancel → a later write waits → the first write ends → the later one enters."""
    log, entered, gate = _logged_writes(monkeypatch)
    write, ctx = _handler(env, "write_file")

    async def scenario() -> None:
        first = asyncio.create_task(write.run(ToolInvocation(write.definition.tool_id, CallId("c1"),
                                                             {"path": "a.txt", "content": "first"}), ctx))
        assert await asyncio.to_thread(entered.wait, 5)
        first.cancel()
        second = asyncio.create_task(write.run(ToolInvocation(write.definition.tool_id, CallId("c2"),
                                                              {"path": "a.txt", "content": "second"}), ctx))
        await asyncio.sleep(0.3)
        assert log == ["start 1"] and not first.done() and not second.done()
        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert (await second).status is ToolStatus.SUCCEEDED

    asyncio.run(scenario())
    assert log == ["start 1", "end 1", "start 2", "end 2"]
    assert (root / "c1/a.txt").read_text() == "second"


def test_a_write_waits_for_the_conversations_folder(env: MachineEnvironment, root: Path) -> None:
    """§9.1: a write locks the Conversation's folder, so it waits for a lock held under it."""
    own = root / "c1"
    write, ctx = _handler(env, "write_file")

    async def scenario() -> None:
        async with env.locks.hold([f"{own}/sub"]):
            task = asyncio.create_task(write.run(ToolInvocation(write.definition.tool_id, CallId("c"),
                                                                {"path": "own.txt", "content": "x"}), ctx))
            await asyncio.sleep(0.2)
            assert not task.done()
        assert (await task).status is ToolStatus.SUCCEEDED

    asyncio.run(scenario())
    assert (own / "own.txt").read_text() == "x"


# --- review of V3-P109 (Codex) ---------------------------------------------------------


def test_purge_never_follows_a_linked_root(tmp_path: Path) -> None:
    """P1: the folders' root replaced by a link to another tree with the same id: nothing there is removed."""
    foreign = tmp_path / "foreign"
    (foreign / "c1").mkdir(parents=True)
    (foreign / "c1/keep.txt").write_text("theirs")
    os.symlink(foreign, tmp_path / "conversations")
    env = MachineEnvironment(str(tmp_path / "conversations"))
    with pytest.raises(MachineUnavailable):
        asyncio.run(env.remove_conversation_folder(WORKSPACE, SCOPE, "conversations/c1"))
    assert (foreign / "c1/keep.txt").read_text() == "theirs"
    assert not asyncio.run(MachineEnvironment(str(tmp_path / "missing")).remove_conversation_folder(
        WORKSPACE, SCOPE, "conversations/c1"))


def test_ids_with_capitals_are_not_folders() -> None:
    """P1: Data's ids are lower case; on a case-insensitive disk "C1" would be "c1"'s folder."""
    with pytest.raises(FolderError):
        ConversationFolders("/tmp/x").path_of("C1")


def test_a_kept_group_write_bit_survives_the_umask(env: MachineEnvironment, root: Path) -> None:
    """P2: the replaced file's mode is set on the new one, not filtered by the umask (022 here)."""
    old = os.umask(0o022)
    try:
        call(env, "write_file", path="shared.txt", content="a")
        (root / "c1/shared.txt").chmod(0o664)
        call(env, "write_file", path="shared.txt", content="b")
        assert (root / "c1/shared.txt").stat().st_mode & 0o777 == 0o664
    finally:
        os.umask(old)


def test_commit_takes_only_a_regular_temporary_file(env: MachineEnvironment, root: Path, tmp_path: Path) -> None:
    """P2: a link (or anything but a regular ``.tmp``) in uploads is never committed under a name."""
    async def scenario() -> None:
        uploads = Path(await env.folder("c1", "conversations/c1")) / "uploads"
        uploads.mkdir(exist_ok=True)
        (tmp_path / "secret").write_text("outside")
        os.symlink(tmp_path / "secret", uploads / ".himari-x.tmp")
        with pytest.raises(MachineUnavailable):
            await env.commit_copy("c1", "conversations/c1", ".himari-x.tmp", str(uploads / "a.pdf"))
        (uploads / "plain").write_text("x")
        with pytest.raises(MachineUnavailable):
            await env.commit_copy("c1", "conversations/c1", "plain", str(uploads / "b.pdf"))

    asyncio.run(scenario())
    assert not (root / "c1/uploads/a.pdf").exists() and not (root / "c1/uploads/b.pdf").exists()


@pytest.mark.parametrize("name,ok", [
    ("notes.txt", True), ("a.b.c", True), ("console.txt", True),
    ("notes.txt.", False), ("notes ", False), ("con", False), ("NUL.txt", False), ("com1", False),
    ("c:notes", False), ("notes:stream", False), ("..", False), ("", False),
])
def test_windows_components_name_one_thing(monkeypatch: pytest.MonkeyPatch, name: str, ok: bool) -> None:
    """P1/P2: on Windows no drive or stream colon, trailing dot or space, or device name passes."""
    from himari.environment.machine import folders as folders_module

    monkeypatch.setattr(folders_module, "_POSIX", False)
    assert folders_module._component_ok(name) is ok


def test_paths_come_back_on_the_folders_own_spelling(env: MachineEnvironment, root: Path) -> None:
    """P1: what is used is the trusted folder plus checked components, never the caller's prefix."""
    folder = asyncio.run(env.folder("c1", "conversations/c1"))
    assert env.folders.resolve(folder, f"{folder}/./a//b.txt") == os.path.join(folder, "a", "b.txt")
    assert env.folders.resolve(folder, ".") == folder
