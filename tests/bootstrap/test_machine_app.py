"""Application with the machine Environment (V3-P74 part 1): new Conversations
of a machine principal work in their folder under the runner's HIMARI home;
others keep Inside (Q1). The runner is in-process (fake sandbox)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from runnerkit import RunnerKit, start_runner
from testkit import calls, final

from himari.bootstrap.application import Application, FilesUnavailable, WorkspaceRefused, workspace_for
from himari.common import PrincipalId, TrustedScope
from himari.data.api import MAX_WORKSPACE_NOTE_CHARS
from himari.environment.machine import DRIVE_NOTE, MachineEnvironment
from himari.prompt.api import state_block
from himari.provider.scripted import ScriptedAdapter

OPERATOR, ALICE = PrincipalId("operator"), PrincipalId("alice")


@pytest.fixture
def kit(tmp_path: Path) -> Iterator[RunnerKit]:
    started = start_runner(tmp_path / "runner")
    yield started
    started.close()


async def chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


async def open_app(root: Path, socket: str, adapter: ScriptedAdapter) -> Application:
    return await Application.open(root, adapter, model_id="scripted-model", sandbox=False, cleanup_seconds=None,
                                  machine_socket=socket)


def test_a_machine_conversation_works_in_its_own_folder(tmp_path: Path, kit: RunnerKit) -> None:
    async def scenario() -> None:
        adapter = ScriptedAdapter([calls(("machine/write_file", {"path": "hello.txt", "content": "hi"})),
                                   calls(("machine/read_file", {"path": "~/projects/a/main.c"})), final("done")])
        app = await open_app(tmp_path / "data", kit.socket, adapter)
        try:
            scope = TrustedScope(OPERATOR, await app.create_conversation(OPERATOR))
            folder = kit.himari_home / "conversations" / scope.conversation_id
            await app.send(scope, "write it")
            await app.runner.wait(scope)
            assert (folder / "hello.txt").read_text() == "hi"
            first = adapter.prepared_inputs[0]
            tools = {str(t.tool_id) for t in first.tool_catalog}
            assert "machine/run_command" in tools and not any(t.startswith("inside/") for t in tools)
            block = state_block(first.current_state) or ""
            assert f'"working_directory":"{folder}"' in block.replace(" ", "") and '"this conversation"' in block
            assert '"read": "everything the user can read' in block
            assert '"connection":"online"' in block.replace(" ", "")
            events = await app.data.read_events(scope)
            results = await app.data.read_tool_executions(scope, events[3].origin_run_id)  # type: ignore[arg-type]
            assert "int main(){}" in results[0].model_result.blocks[0].text  # type: ignore[union-attr]
        finally:
            await app.close()

    asyncio.run(scenario())


def test_other_principals_keep_inside(tmp_path: Path, kit: RunnerKit) -> None:
    async def scenario() -> None:
        adapter = ScriptedAdapter([calls(("inside/write_file", {"path": "a.txt", "content": "x"})), final("ok")])
        app = await open_app(tmp_path / "data", kit.socket, adapter)
        try:
            scope = TrustedScope(ALICE, await app.create_conversation(ALICE))
            await app.send(scope, "go")
            await app.runner.wait(scope)
            inside = Path(app.inside.host_root(workspace_for(ALICE))) / "conversations" / scope.conversation_id
            assert (inside / "a.txt").read_text() == "x"
            assert not (kit.himari_home / "conversations" / scope.conversation_id).exists()
        finally:
            await app.close()

    asyncio.run(scenario())


def test_attachments_go_to_the_folder_and_purge_removes_it(tmp_path: Path, kit: RunnerKit) -> None:
    async def scenario() -> None:
        adapter = ScriptedAdapter([final("got it")])
        app = await open_app(tmp_path / "data", kit.socket, adapter)
        try:
            scope = TrustedScope(OPERATOR, await app.create_conversation(OPERATOR))
            folder = kit.himari_home / "conversations" / scope.conversation_id
            record = await app.upload(scope, "notes.txt", "text/plain", chunks(b"hello"))
            await app.send(scope, "see", [record.file_id])
            await app.runner.wait(scope)
            assert (folder / "uploads/notes.txt").read_bytes() == b"hello"
            info = adapter.prepared_inputs[0].attachments[f"{record.file_id}#1"]
            assert info.path == f"{folder}/uploads/notes.txt"
            await app.trash(scope)
            await app.purge(scope)
            assert not folder.exists()
        finally:
            await app.close()

    asyncio.run(scenario())


def test_without_the_runner_nothing_happens(tmp_path: Path) -> None:
    async def scenario() -> None:
        adapter = ScriptedAdapter([calls(("machine/write_file", {"path": "a.txt", "content": "x"})), final("no")])
        app = await open_app(tmp_path / "data", str(tmp_path / "missing.sock"), adapter)
        try:
            scope = TrustedScope(OPERATOR, await app.create_conversation(OPERATOR))
            await app.send(scope, "go")
            await app.runner.wait(scope)
            events = await app.data.read_events(scope)
            result = (await app.data.read_tool_executions(scope, events[1].origin_run_id))[0]  # type: ignore[arg-type]
            assert result.status.value == "denied" and "not reachable" in result.model_result.blocks[0].text  # type: ignore[union-attr]
            block = state_block(adapter.prepared_inputs[0].current_state) or ""
            assert '"connection":"offline"' in block.replace(" ", "")
            record = await app.upload(scope, "a.txt", "text/plain", chunks(b"x"))
            with pytest.raises(FilesUnavailable):
                await app.send(scope, "with a file", [record.file_id])
        finally:
            await app.close()

    asyncio.run(scenario())


def test_folders_are_registered_attached_and_shown_to_the_model(tmp_path: Path, kit: RunnerKit) -> None:
    """V3-P74 §10: the runner checks a folder, Data keeps it, the next model request is told of it."""
    async def scenario() -> None:
        adapter = ScriptedAdapter([calls(("machine/write_file", {"path": "made.c", "content": "x"})), final("done")])
        app = await open_app(tmp_path / "data", kit.socket, adapter)
        try:
            scope = TrustedScope(OPERATOR, await app.create_conversation(OPERATOR))
            project = str(kit.home / "projects/a")
            own = await app.conversation_workspaces(scope)
            assert [(f.label, f.own, f.working) for f in own] == [("this conversation", True, True)]
            folders = await app.attach_workspace(scope, path="~/projects/a")  # registered on the way
            assert [(f.path, f.label, f.working) for f in folders[1:]] == [(project, "a", True)]
            assert [w.location for w in await app.machine_workspaces(OPERATOR)] == [project]
            with pytest.raises(WorkspaceRefused) as refused:
                await app.attach_workspace(scope, path=project)
            assert refused.value.code == "duplicate"
            with pytest.raises(WorkspaceRefused) as refused:
                await app.register_workspace(OPERATOR, "~/.ssh")
            assert refused.value.code == "not_usable"
            await app.send(scope, "write it")
            await app.runner.wait(scope)
            assert (Path(project) / "made.c").read_text() == "x"
            block = state_block(adapter.prepared_inputs[0].current_state) or ""
            assert f'"working_directory": "{project}"' in block and '"label": "a"' in block
            with pytest.raises(WorkspaceRefused) as refused:
                await app.detach_workspace(scope, folders[0].binding_id)
            assert refused.value.code == "own_folder"
            left = await app.detach_workspace(scope, folders[1].binding_id)
            assert [f.own for f in left] == [True] and left[0].working
            await app.unregister_workspace(OPERATOR, folders[1].workspace_id or "")
            assert await app.machine_workspaces(OPERATOR) == ()
        finally:
            await app.close()

    asyncio.run(scenario())


def test_a_cloud_folder_gets_the_drive_note(tmp_path: Path, kit: RunnerKit, monkeypatch: pytest.MonkeyPatch) -> None:
    """§14: the model is told a folder is cloud-synced apart from the user's note, which stays theirs whole."""
    from himari.runner import hostfs

    monkeypatch.setattr(hostfs, "mount_type", lambda path, mountinfo="": "fuse.rclone")  # the runner is in-process

    async def scenario() -> None:
        adapter = ScriptedAdapter([final("ok")])
        app = await open_app(tmp_path / "data", kit.socket, adapter)
        try:
            longest = "n" * MAX_WORKSPACE_NOTE_CHARS
            registered = await app.register_workspace(OPERATOR, "~/projects/a", note=longest)
            assert registered.note == longest and registered.filesystem == "fuse.rclone"
            edited = await app.update_workspace(OPERATOR, registered.workspace_id, "lectures", None)
            assert edited.note is None and edited.filesystem == "fuse.rclone"
            scope = TrustedScope(OPERATOR, await app.create_conversation(OPERATOR))
            await app.attach_workspace(scope, workspace_id=registered.workspace_id)
            assert app.machine is not None
            shown = (await app.machine.places(scope)).shown
            assert [(w.label, w.note, w.notice) for w in shown[1:]] == [("lectures", None, DRIVE_NOTE)]
            assert shown[0].notice is None
            await app.send(scope, "go")
            await app.runner.wait(scope)
            block = state_block(adapter.prepared_inputs[0].current_state) or ""
            assert f'"notice": "{DRIVE_NOTE}"' in block
        finally:
            await app.close()

    asyncio.run(scenario())


def test_two_paths_to_one_folder_registered_at_once(tmp_path: Path, kit: RunnerKit,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """§10.1: two names of one folder (a bind mount, say) registered at the same time: one wins, the other is
    told it is a duplicate, so one folder never gets two path locks."""
    one, other = str(kit.home / "projects/a"), str(kit.home / "alias/a")

    async def check(self: MachineEnvironment, path: str, others: list[str]) -> dict[str, object]:
        await asyncio.sleep(0.05)  # the runner's check takes a while: both would pass without the lock
        same = next((o for o in others if o in (one, other)), None)
        return {"path": path, "filesystem": "ext4", "same_as": same}

    monkeypatch.setattr(MachineEnvironment, "check_workspace", check)

    async def scenario() -> None:
        app = await open_app(tmp_path / "data", kit.socket, ScriptedAdapter([]))
        try:
            scope = TrustedScope(OPERATOR, await app.create_conversation(OPERATOR))
            results = await asyncio.gather(app.register_workspace(OPERATOR, one),
                                           app.attach_workspace(scope, path=other), return_exceptions=True)
            assert [w.location for w in await app.machine_workspaces(OPERATOR)] == [one]
            assert results[0].location == one  # type: ignore[union-attr]
            folders = results[1]
            assert not isinstance(folders, BaseException) and folders[1].path == one  # attached as the one it is
            with pytest.raises(WorkspaceRefused) as refused:
                await app.register_workspace(OPERATOR, other)
            assert refused.value.code == "duplicate"
        finally:
            await app.close()

    asyncio.run(scenario())


def test_inside_conversations_and_other_principals_attach_nothing(tmp_path: Path, kit: RunnerKit) -> None:
    async def scenario() -> None:
        app = await open_app(tmp_path / "data", kit.socket, ScriptedAdapter([]))
        try:
            plain = TrustedScope(OPERATOR, await app.data.create_conversation(OPERATOR))  # no machine activation
            with pytest.raises(WorkspaceRefused) as refused:
                await app.attach_workspace(plain, path="~/projects/a")
            assert refused.value.code == "not_machine" and await app.machine_workspaces(OPERATOR) == ()
            alice = TrustedScope(ALICE, await app.create_conversation(ALICE))
            assert await app.conversation_workspaces(alice) == ()  # an Inside Conversation
            with pytest.raises(WorkspaceRefused) as refused:
                await app.attach_workspace(alice, path="~/projects/a")
            assert refused.value.code == "unavailable"
            with pytest.raises(WorkspaceRefused):
                await app.machine_workspaces(ALICE)
        finally:
            await app.close()

    asyncio.run(scenario())
