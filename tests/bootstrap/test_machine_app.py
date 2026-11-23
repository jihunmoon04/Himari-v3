"""Application with the machine Environment (V3-P74 part 1): new Conversations
of a machine principal work in their folder under the runner's HIMARI home;
others only talk (V3-P101). The runner is in-process."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from runnerkit import RunnerKit, start_runner
from testkit import calls, final

from himari.bootstrap.application import Application, FilesUnavailable
from himari.common import PrincipalId, TrustedScope
from himari.prompt.api import ATTACHMENT_NOTE, state_block
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
            assert "machine/write_file" in tools and "machine/run_command" not in tools
            assert not any(t.startswith("inside/") for t in tools)
            block = state_block(first.current_state) or ""
            assert f'"working_directory":"{folder}"' in block.replace(" ", "")
            assert f'"writable":["{folder}"]' in block.replace(" ", "")
            assert '"read": "everything the user can read' in block
            assert "git_push" not in block and "machine/git_push" not in tools  # V3-P100
            assert '"connection":"online"' in block.replace(" ", "")
            events = await app.data.read_events(scope)
            results = await app.data.read_tool_executions(scope, events[3].origin_run_id)  # type: ignore[arg-type]
            assert "int main(){}" in results[0].model_result.blocks[0].text  # type: ignore[union-attr]
        finally:
            await app.close()

    asyncio.run(scenario())


def test_other_principals_only_talk(tmp_path: Path, kit: RunnerKit) -> None:
    """V3-P101: a principal the machine Environment is not for gets no Environment: no file Tools, no uploads."""
    async def scenario() -> None:
        adapter = ScriptedAdapter([final("ok")])
        app = await open_app(tmp_path / "data", kit.socket, adapter)
        try:
            scope = TrustedScope(ALICE, await app.create_conversation(ALICE))
            await app.send(scope, "go")
            await app.runner.wait(scope)
            tools = {str(t.tool_id) for t in adapter.prepared_inputs[0].tool_catalog}
            assert not any(t.startswith(("inside/", "machine/")) for t in tools)
            with pytest.raises(FilesUnavailable):
                await app.upload(scope, "a.txt", None, chunks(b"a"))
            assert not (kit.himari_home / "conversations" / scope.conversation_id).exists()
            assert not (tmp_path / "data" / "workspaces").exists()
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


def test_an_old_inside_conversation_is_told_of_no_files(tmp_path: Path, kit: RunnerKit) -> None:
    """V3-P101: Inside is gone; a Conversation made with it only talks, so the model is told of no file Tools,
    attachments or folder, while a machine Conversation on the same server still is."""
    from himari.common import EnvironmentKind, WorkspaceId
    from himari.data.api import InitialActivation

    async def scenario() -> None:
        adapter = ScriptedAdapter([final("old"), final("new")])
        app = await open_app(tmp_path / "data", kit.socket, adapter)
        try:
            old = TrustedScope(OPERATOR, await app.data.create_conversation(
                OPERATOR, activations=(InitialActivation(EnvironmentKind("inside"), WorkspaceId("ws_old")),)))
            new = TrustedScope(OPERATOR, await app.create_conversation(OPERATOR))
            for scope in (old, new):
                await app.send(scope, "hi")
                await app.runner.wait(scope)
            told_old, told_new = ("\n".join(i.stable_prefix) for i in adapter.prepared_inputs)
            assert ATTACHMENT_NOTE not in told_old and ATTACHMENT_NOTE in told_new
            block = state_block(adapter.prepared_inputs[0].current_state) or ""
            assert "inside" not in block and "working_directory" not in block, block
        finally:
            await app.close()

    asyncio.run(scenario())
