"""The machine Environment (V3-P74): its Tools through a real runner (fake sandbox), path locks."""

from __future__ import annotations

import asyncio
import io
import os
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import gitserver
import pytest
from runnerkit import RunnerKit, start_runner

from himari.common import (
    BindingId,
    CallId,
    ConversationId,
    EnvironmentInstanceId,
    PrincipalId,
    ToolId,
    TrustedScope,
    WorkspaceId,
)
from himari.data.api import BindingAuthority, ConversationWorkspace, DataError, DataErrorCode, InitialActivation
from himari.data.memory import InMemoryData, InMemoryStore
from himari.environment.api import ConnectionStatus, EnvironmentPlugin
from himari.environment.machine import MACHINE, MachineEnvironment, MachineUnavailable, machine_workspace_for
from himari.environment.machine.locks import PathLocks, overlap
from himari.runner.hostfs import Host
from himari.sandbox.api import CommandResult
from himari.tool.api import EffectProfile, ToolContext, ToolHandler, ToolInvocation, ToolOutcome, ToolStatus
from himari.tool.registry import ToolRegistry

SCOPE = TrustedScope(PrincipalId("operator"), ConversationId("c1"))
WORKSPACE = machine_workspace_for(PrincipalId("operator"))


class _NoArtifacts:
    async def read_artifact(self, scope: Any, artifact_id: Any) -> Any:
        raise KeyError(artifact_id)

    async def list_artifacts(self, scope: Any, *, kind: Any = None) -> Any:
        return ()


@pytest.fixture
def kit(tmp_path: Path) -> Iterator[RunnerKit]:
    started = start_runner(tmp_path)
    yield started
    started.close()


@pytest.fixture
def env(kit: RunnerKit) -> MachineEnvironment:
    return MachineEnvironment(kit.socket)


def binding() -> BindingAuthority:
    return BindingAuthority(BindingId("b1"), EnvironmentInstanceId("e1"), MACHINE, WORKSPACE, 1, "conversations/c1")


def call(env: MachineEnvironment, tool: str, **args: Any) -> ToolOutcome:
    handler = next(t for t in env.tools() if t.definition.tool_id == f"machine/{tool}")
    ctx = ToolContext(scope=SCOPE, artifacts=_NoArtifacts(), binding=binding(),
                      workspace=ConversationWorkspace(WORKSPACE, "conversations/c1"))
    return asyncio.run(handler.run(ToolInvocation(ToolId(f"machine/{tool}"), CallId("c"), args), ctx))


def text(outcome: ToolOutcome) -> str:
    block = outcome.model_result.blocks[0]
    assert hasattr(block, "text")
    return str(block.text)


def test_plugin_conforms(env: MachineEnvironment) -> None:
    assert isinstance(env, EnvironmentPlugin) and env.kind == MACHINE
    tools = list(env.tools())
    assert all(isinstance(t, ToolHandler) and t.definition.tool_id.startswith("machine/") for t in tools)
    assert all("workspace_binding_id" not in t.definition.input_schema["properties"] for t in tools)  # type: ignore[operator]
    effects = {str(t.definition.tool_id): t.definition.effect_profile for t in tools}
    assert effects["machine/read_file"] is EffectProfile.READ and effects["machine/run_command"] is EffectProfile.UNKNOWN
    assert effects["machine/git_push"] is EffectProfile.UNKNOWN
    ToolRegistry(tools)
    assert MachineEnvironment.owns(WORKSPACE) and not MachineEnvironment.owns(WorkspaceId("ws_x"))


def test_round_trip_in_the_conversation_folder(env: MachineEnvironment, kit: RunnerKit) -> None:
    folder = kit.himari_home / "conversations/c1"
    written = call(env, "write_file", path="src/main.py", content="print('hi')\n")
    assert written.status is ToolStatus.SUCCEEDED and text(written) == f"created {folder}/src/main.py (12 bytes)"
    assert text(call(env, "read_file", path="src/main.py")) == f"{folder}/src/main.py (12 bytes)\nprint('hi')\n"
    assert call(env, "edit_file", path="src/main.py", old_text="hi", new_text="hello").status is ToolStatus.SUCCEEDED
    assert (folder / "src/main.py").read_text() == "print('hello')\n"
    assert "src/" in text(call(env, "list_files"))
    assert call(env, "delete_file", path=f"{folder}/src/main.py").status is ToolStatus.SUCCEEDED
    assert not (folder / "src/main.py").exists()


def test_reading_is_broad_writing_is_not(env: MachineEnvironment, kit: RunnerKit) -> None:
    read = call(env, "read_file", path="~/projects/a/main.c")
    assert read.status is ToolStatus.SUCCEEDED and text(read).endswith("int main(){}\n")
    hidden = call(env, "read_file", path="~/.ssh/id")
    assert hidden.status is ToolStatus.FAILED and "no such file" in text(hidden)
    outside = call(env, "write_file", path="~/projects/a/x.c", content="x")
    assert outside.status is ToolStatus.DENIED and "attach the folder as a workspace" in text(outside)
    assert not (kit.home / "projects/a/x.c").exists()
    listing = text(call(env, "list_files", path="~"))
    assert ".ssh" not in listing and ".gitconfig" in listing and "projects/" in listing


def test_edit_refuses_a_file_changed_meanwhile(env: MachineEnvironment, kit: RunnerKit) -> None:
    call(env, "write_file", path="f.txt", content="one two")
    target = kit.himari_home / "conversations/c1/f.txt"

    original = env.call

    async def racing(op: str, args: dict[str, Any], fds: Any = ()) -> Any:
        result = await original(op, args, fds)
        if op == "open":
            target.write_text("someone else")
        return result

    env.call = racing  # type: ignore[method-assign]
    edited = call(env, "edit_file", path="f.txt", old_text="one", new_text="1")
    assert edited.status is ToolStatus.FAILED and "changed" in text(edited)
    assert target.read_text() == "someone else"


def test_run_command(env: MachineEnvironment, kit: RunnerKit) -> None:
    outcome = call(env, "run_command", argv=["make", "-j2"], timeout_seconds=60)
    assert outcome.status is ToolStatus.SUCCEEDED and "out" in text(outcome)
    argv, layout, timeout = kit.sandbox.calls[-1]
    folder = str(kit.himari_home / "conversations/c1")
    assert argv == ["make", "-j2"] and timeout == 60 and layout.cwd == folder
    assert [p for p, _ in layout.writable] == [folder]
    kit.sandbox.result = CommandResult(137, False, b"", b"Killed\n", 0, 7, 9)
    kit.runner.commands = type("C", (), {"procs": "/x", "oom_kills": iter([0, 1]).__next__})()
    outcome = call(env, "run_command", argv=["big"])
    assert "ran out of memory" in text(outcome) and dict(outcome.raw_result)["oom"] is True  # type: ignore[arg-type]
    assert call(env, "run_command", argv=["x"], timeout_seconds=0).status is ToolStatus.DENIED


def test_an_unreachable_runner_did_nothing(tmp_path: Path) -> None:
    env = MachineEnvironment(str(tmp_path / "missing.sock"))
    outcome = call(env, "write_file", path="a", content="x")
    assert outcome.status is ToolStatus.DENIED and "not reachable" in text(outcome)
    assert call(env, "run_command", argv=["true"]).status is ToolStatus.DENIED
    observed = asyncio.run(env.observe(SCOPE, [binding()]))
    assert observed[0].status is ConnectionStatus.OFFLINE and observed[0].working_directory is None


def test_observe_tells_the_real_folder(env: MachineEnvironment, kit: RunnerKit) -> None:
    observed = asyncio.run(env.observe(SCOPE, [binding()]))
    folder = str(kit.himari_home / "conversations/c1")
    assert observed[0].status is ConnectionStatus.ONLINE
    assert observed[0].working_directory == folder and observed[0].writable == (folder,)


def test_uploads_and_removal(env: MachineEnvironment, kit: RunnerKit) -> None:
    async def place() -> str:
        async with env.locked(WORKSPACE, 5, conversation_id="c1", cwd_relative="conversations/c1"):
            temp = await env.copy_in("c1", "conversations/c1", io.BytesIO(b"bytes"), clear=True)
            path = await env.free_path("c1", "conversations/c1", "report.pdf", None)
            assert await env.commit_copy("c1", "conversations/c1", temp, path)
            await env.discard_copy("c1", "conversations/c1", temp)
            return path

    path = asyncio.run(place())
    assert path == f"{kit.himari_home}/conversations/c1/uploads/report.pdf" and Path(path).read_bytes() == b"bytes"
    import hashlib

    assert asyncio.run(env.holds("c1", "conversations/c1", path, 5, hashlib.sha256(b"bytes").hexdigest()))
    assert asyncio.run(env.remove_conversation_folder(WORKSPACE, SCOPE, "conversations/c1"))
    assert not (kit.himari_home / "conversations/c1").exists()
    assert not asyncio.run(env.remove_conversation_folder(WORKSPACE, SCOPE, "projects"))


def test_uploads_without_the_runner(tmp_path: Path) -> None:
    env = MachineEnvironment(str(tmp_path / "missing.sock"))
    with pytest.raises(MachineUnavailable):
        asyncio.run(env.free_path("c1", "conversations/c1", "a", None))


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


def test_a_runner_without_its_sandbox_refuses_file_tools(tmp_path: Path) -> None:
    kit = start_runner(tmp_path, enforced=False)
    try:
        env = MachineEnvironment(kit.socket)
        for outcome in (call(env, "read_file", path="~/projects/a/main.c"), call(env, "write_file", path="a",
                                                                                content="x")):
            assert outcome.status is ToolStatus.DENIED and "not enforced" in text(outcome)
    finally:
        kit.close()



def _handler(env: MachineEnvironment, tool: str) -> tuple[ToolHandler, ToolContext]:
    handler = next(t for t in env.tools() if t.definition.tool_id == f"machine/{tool}")
    ctx = ToolContext(scope=SCOPE, artifacts=_NoArtifacts(), binding=binding(),
                      workspace=ConversationWorkspace(WORKSPACE, "conversations/c1"))
    return handler, ctx


def _logged_writes(monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], threading.Event, threading.Event]:
    """The runner's file writes log when they start and end; the first one
    blocks in its thread until ``gate`` is set."""
    log: list[str] = []
    entered, gate = threading.Event(), threading.Event()
    real = Host.write

    def logged(host: Host, fd: int, name: str, data: bytes | int, expected: list[int] | None) -> bool:
        number = sum(1 for e in log if e.startswith("start")) + 1
        log.append(f"start {number}")
        if number == 1:
            entered.set()
            gate.wait(10)
        try:
            return real(host, fd, name, data, expected)
        finally:
            log.append(f"end {number}")

    monkeypatch.setattr(Host, "write", logged)
    return log, entered, gate


def test_a_cancelled_write_keeps_the_folder_until_it_ends(env: MachineEnvironment, kit: RunnerKit,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """PR #87 review C1: cancel → a later write waits → the first write ends → the later one enters."""
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
    assert (kit.himari_home / "conversations/c1/a.txt").read_text() == "second"


def test_a_cancelled_command_keeps_the_folder_until_it_is_reaped(env: MachineEnvironment, kit: RunnerKit,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    log, _, gate = _logged_writes(monkeypatch)
    gate.set()
    kit.sandbox.gate = threading.Event()
    kit.sandbox.reap_seconds = 0.3
    kit.sandbox.events = log  # "reaped" goes in the same log
    run, ctx = _handler(env, "run_command")
    write, _ = _handler(env, "write_file")

    async def scenario() -> None:
        command = asyncio.create_task(run.run(ToolInvocation(run.definition.tool_id, CallId("c1"),
                                                             {"argv": ["sleep", "100"]}), ctx))
        deadline = asyncio.get_running_loop().time() + 5
        while not kit.sandbox.calls and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        command.cancel()
        later = asyncio.create_task(write.run(ToolInvocation(write.definition.tool_id, CallId("c2"),
                                                             {"path": "b.txt", "content": "x"}), ctx))
        with pytest.raises(asyncio.CancelledError):
            await command
        assert (await later).status is ToolStatus.SUCCEEDED

    asyncio.run(scenario())
    assert log == ["reaped", "start 1", "end 1"]


# --- attached Workspaces (V3-P74 §10) ------------------------------------------------------------


def attached_setup(kit: RunnerKit) -> tuple[MachineEnvironment, InMemoryData, TrustedScope, str]:
    """A Conversation with ~/projects/a attached (the working directory)."""
    data = InMemoryData(InMemoryStore())
    scope = TrustedScope(PrincipalId("operator"), asyncio.run(data.create_conversation(
        PrincipalId("operator"), activations=[InitialActivation(MACHINE, WORKSPACE)])))
    project = str(kit.home / "projects/a")
    registered = asyncio.run(data.register_workspace(scope.principal_id, MACHINE, project, "a", "the C project"))
    asyncio.run(data.attach_workspace(scope, registered.workspace_id))
    return MachineEnvironment(kit.socket, registry=data), data, scope, project


def call_in(env: MachineEnvironment, data: InMemoryData, scope: TrustedScope, tool: str, **args: Any) -> ToolOutcome:
    """As the Dispatcher calls it: the working Binding and its Workspace."""
    async def go() -> ToolOutcome:
        handler = next(t for t in env.tools() if t.definition.tool_id == f"machine/{tool}")
        binding = await data.resolve_default_binding(scope, MACHINE)
        assert binding is not None and binding.cwd_relative is not None
        ctx = ToolContext(scope=scope, artifacts=_NoArtifacts(), binding=binding,
                          workspace=ConversationWorkspace(binding.workspace_id, binding.cwd_relative))
        return await handler.run(ToolInvocation(ToolId(f"machine/{tool}"), CallId("c"), args), ctx)

    return asyncio.run(go())


def test_attached_folders_are_written_and_the_working_one_is_the_cwd(kit: RunnerKit) -> None:
    env, data, scope, project = attached_setup(kit)
    own = kit.himari_home / "conversations" / scope.conversation_id
    assert call_in(env, data, scope, "write_file", path="new.c", content="x").status is ToolStatus.SUCCEEDED
    assert (Path(project) / "new.c").read_text() == "x"  # relative to the working directory
    assert call_in(env, data, scope, "write_file", path=f"{own}/notes.md", content="n").status is ToolStatus.SUCCEEDED
    outside = call_in(env, data, scope, "write_file", path="~/projects/other.c", content="x")
    assert outside.status is ToolStatus.DENIED and "attach" in text(outside)
    assert call_in(env, data, scope, "run_command", argv=["make"]).status is ToolStatus.SUCCEEDED
    _, layout, _ = kit.sandbox.calls[-1]
    assert layout.cwd == project and sorted(p for p, _ in layout.writable) == sorted([str(own), project])
    found = asyncio.run(data.read_conversation_workspaces(scope, MACHINE))
    asyncio.run(data.set_working_workspace(scope, found.attached[0].binding.binding_id))  # back to the own folder
    assert call_in(env, data, scope, "write_file", path="here.txt", content="h").status is ToolStatus.SUCCEEDED
    assert (own / "here.txt").read_text() == "h"
    asyncio.run(data.detach_workspace(scope, found.attached[1].binding.binding_id))
    assert call_in(env, data, scope, "write_file", path=f"{project}/late.c", content="x").status is ToolStatus.DENIED


def test_the_state_names_the_folders_and_what_may_be_read(kit: RunnerKit) -> None:
    env, data, scope, project = attached_setup(kit)
    bindings = asyncio.run(data.read_current_state_basis(scope)).bindings
    observed = asyncio.run(env.observe(scope, bindings))
    view = observed[0]
    own = str(kit.himari_home / "conversations" / scope.conversation_id)
    assert view.status is ConnectionStatus.ONLINE and view.working_directory == project
    assert [(w.path, w.label, w.note) for w in view.workspaces] == [(own, "this conversation", None),
                                                                    (project, "a", "the C project")]
    assert view.readable is not None and str(kit.home) in view.readable
    assert view.network == "none: commands cannot reach the network"  # this runner's proxy is not started
    assert view.push == "not set up on this server"


def test_a_write_waits_for_every_folder_of_the_conversation(kit: RunnerKit) -> None:
    """§9.1: a write locks all of the Conversation's folders (which one a path lands in is not known first)."""
    env, data, scope, project = attached_setup(kit)

    async def scenario() -> None:
        handler = next(t for t in env.tools() if t.definition.tool_id == "machine/write_file")
        binding = await data.resolve_default_binding(scope, MACHINE)
        assert binding is not None
        ctx = ToolContext(scope=scope, artifacts=_NoArtifacts(), binding=binding,
                          workspace=ConversationWorkspace(binding.workspace_id, binding.cwd_relative or ""))
        async with env.locks.hold([f"{project}/sub"]):  # another Conversation's command under the project
            task = asyncio.create_task(handler.run(ToolInvocation(ToolId("machine/write_file"), CallId("c"),
                                                                  {"path": "own.txt", "content": "x"}), ctx))
            await asyncio.sleep(0.2)
            assert not task.done()
        assert (await task).status is ToolStatus.SUCCEEDED

    asyncio.run(scenario())


def test_a_workspace_gone_from_disk_is_named(kit: RunnerKit) -> None:
    """A registered folder moved or deleted later: the refusal says which, and how to go on."""
    env, data, scope, project = attached_setup(kit)
    found = asyncio.run(data.read_conversation_workspaces(scope, MACHINE))
    asyncio.run(data.set_working_workspace(scope, found.attached[0].binding.binding_id))
    Path(project).rename(kit.home / "projects/moved")
    refused = call_in(env, data, scope, "write_file", path="x.txt", content="x")
    assert refused.status is ToolStatus.FAILED and project in text(refused) and "detach" in text(refused)


def test_folders_data_will_not_give_run_nothing(kit: RunnerKit) -> None:
    """B1: when Data refuses the Conversation's folders, a Tool does nothing; it never falls back to the own
    folder alone."""
    env, data, scope, project = attached_setup(kit)

    class Refusing:
        async def read_conversation_workspaces(self, scope: TrustedScope, kind: Any) -> Any:
            raise DataError(DataErrorCode.NOT_AUTHORIZED, "not yours")

    refusing = MachineEnvironment(kit.socket, registry=Refusing(),  # type: ignore[arg-type]
                                  documents=object())  # type: ignore[arg-type]  # never reached
    calls = len(kit.sandbox.calls)
    for tool, args in (("write_file", {"path": "x.txt", "content": "x"}), ("run_command", {"argv": ["true"]}),
                       ("read_file", {"path": "~/projects/a/main.c"}), ("read_document", {"path": "doc.md"})):
        refused = call_in(refusing, data, scope, tool, **args)
        assert refused.status is ToolStatus.DENIED and "could not be read" in text(refused), tool
    assert len(kit.sandbox.calls) == calls
    assert not (kit.himari_home / "conversations" / scope.conversation_id / "x.txt").exists()
    assert not (Path(project) / "x.txt").exists()


@pytest.mark.skipif(not gitserver.AVAILABLE, reason="needs git-http-backend and openssl")
def test_git_push(tmp_path: Path) -> None:
    """§12 through the Tool: created, up to date, rejected; told in the state; refused when not set up."""
    server = gitserver.GitServer(tmp_path / "server", "tok")
    token = tmp_path / "kit/secret-root/token"  # on the kit's hidden list
    token.parent.mkdir(parents=True)
    token.write_text("tok")
    token.chmod(0o600)
    kit = start_runner(tmp_path / "kit", push={"repos": ["github.com/owner/*"], "token_file": str(token)})
    try:
        server.bare("repo")
        assert kit.runner.pusher is not None
        kit.runner.pusher = gitserver.LocalPusher(server, kit.runner.pusher.repos, str(token),
                                                  kit.runner.config.state_dir)
        kit.sandbox.execute = True
        env = MachineEnvironment(kit.socket)
        repo = kit.himari_home / "conversations/c1"
        repo.mkdir()
        gitserver.git("init", "-q", "-b", "main", cwd=repo)
        gitserver.git("commit", "-q", "--allow-empty", "-m", "a", cwd=repo)
        gitserver.git("remote", "add", "origin", "https://github.com/owner/repo.git", cwd=repo)
        head = gitserver.git("rev-parse", "HEAD", cwd=repo).strip()
        pushed = call(env, "git_push", branch="main")
        assert pushed.status is ToolStatus.SUCCEEDED
        assert text(pushed) == f"pushed main to https://github.com/owner/repo.git: a new branch at {head[:12]}"
        assert text(call(env, "git_push", branch="main")).startswith("main is already up to date")
        gitserver.git("commit", "-q", "--amend", "--allow-empty", "-m", "b", cwd=repo)
        rejected = call(env, "git_push", branch="main")
        assert rejected.status is ToolStatus.FAILED and "rejected main" in text(rejected) and server.head("repo") == head
        bindings = (binding(),)
        assert asyncio.run(env.observe(SCOPE, bindings))[0].push == (
            "to the 'origin' remote of a repository in the workspaces, if it is one of: github.com/owner/* "
            "(https remotes only)")
        kit.runner.pusher = None
        refused = call(env, "git_push", branch="main", repository=".")
        assert refused.status is ToolStatus.DENIED and "not set up" in text(refused)
    finally:
        kit.close()
        server.close()



# --- the review record of the file Tools' changes (V3-P76 §7, §8) -----------------------------------


class RecordingLog:
    """A ChangeLog that records what the Tools tell it."""

    def __init__(self, fail_begin: bool = False) -> None:
        self.seen: list[tuple[Any, ...]] = []
        self.fail_begin = fail_begin

    async def begin(self, scope: TrustedScope, call: CallId, path: str) -> object:
        if self.fail_begin:
            raise OSError("the record cannot be written")
        self.seen.append(("begin", path))
        return len(self.seen)

    async def kept(self, token: object, path: str, copy: int | None, prior: tuple[int, ...],
                   after: tuple[int, ...] | None, complete: bool) -> None:
        held = b"" if copy is None else os.read(copy, 1 << 20)
        if copy is not None:
            os.close(copy)
        self.seen.append(("kept", path, held, bool(prior), after == (), complete))

    async def dropped(self, token: object) -> None:
        self.seen.append(("dropped",))

    async def unknown(self, token: object) -> None:
        self.seen.append(("unknown",))

    async def commands_ran(self, scope: TrustedScope) -> None:
        self.seen.append(("commands_ran",))


def test_each_change_goes_to_the_record(kit: RunnerKit) -> None:
    log = RecordingLog()
    env = MachineEnvironment(kit.socket, changes=log)
    folder = kit.himari_home / "conversations/c1"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "real.txt").write_text("before\n")
    (folder / "link.txt").symlink_to("real.txt")
    assert call(env, "write_file", path="link.txt", content="after\n").status is ToolStatus.SUCCEEDED
    assert call(env, "write_file", path="new/deep.txt", content="x").status is ToolStatus.SUCCEEDED
    assert call(env, "edit_file", path="real.txt", old_text="after", new_text="edited").status is ToolStatus.SUCCEEDED
    assert call(env, "delete_file", path="link.txt").status is ToolStatus.SUCCEEDED  # the link itself
    assert call(env, "delete_file", path="missing.txt").status is ToolStatus.FAILED
    assert call(env, "run_command", argv=["true"]).status is ToolStatus.SUCCEEDED
    real, deep, link = str(folder / "real.txt"), str(folder / "new/deep.txt"), str(folder / "link.txt")
    assert log.seen == [
        ("begin", real), ("kept", real, b"before\n", True, False, True),  # recorded under where it lands
        ("begin", deep), ("kept", deep, b"", False, False, True),
        ("begin", real), ("kept", real, b"after\n", True, False, True),
        ("begin", link), ("kept", link, b"", True, True, True),  # a link goes without a copy
        ("begin", str(folder / "missing.txt")), ("dropped",),
        ("commands_ran",)]


def test_a_lost_answer_is_unknown_and_an_unrecorded_change_is_not_made(kit: RunnerKit,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    from himari.runner import api as runner

    log = RecordingLog()
    env = MachineEnvironment(kit.socket, changes=log)
    folder = kit.himari_home / "conversations/c1"
    calls_made = env.call

    async def lost(op: str, args: dict[str, Any], fds: Any = ()) -> Any:
        if args.get("keep"):
            await calls_made(op, args, fds)
            raise runner.RunnerUnavailable("the connection broke")
        return await calls_made(op, args, fds)

    monkeypatch.setattr(env, "call", lost)
    with pytest.raises(runner.RunnerUnavailable):  # the Dispatcher records the call's outcome as unknown
        call(env, "write_file", path="a.txt", content="x")
    assert log.seen == [("begin", str(folder / "a.txt")), ("unknown",)] and (folder / "a.txt").exists()
    monkeypatch.undo()
    unrecorded = MachineEnvironment(kit.socket, changes=RecordingLog(fail_begin=True))
    outcome = call(unrecorded, "write_file", path="b.txt", content="x")
    assert outcome.status is ToolStatus.FAILED and "could not be recorded" in text(outcome)
    assert not (folder / "b.txt").exists()


def test_a_new_file_written_in_part_says_so(kit: RunnerKit, monkeypatch: pytest.MonkeyPatch) -> None:
    """§4.1: the O_EXCL path's unfinished write is told apart from a whole one."""
    env = MachineEnvironment(kit.socket, changes=RecordingLog())
    calls_made = env.call

    async def partial(op: str, args: dict[str, Any], fds: Any = ()) -> Any:
        result, received = await calls_made(op, args, fds)
        if args.get("keep"):
            result = {**result, "partial": True, "complete": False, "after_identity": None}
        return result, received

    monkeypatch.setattr(env, "call", partial)
    outcome = call(env, "write_file", path="p.txt", content="x")
    assert outcome.status is ToolStatus.SUCCEEDED and "its content may be partial" in text(outcome)


def test_an_older_runner_gets_plain_writes(kit: RunnerKit, monkeypatch: pytest.MonkeyPatch) -> None:
    """One not restarted since HIMARI was updated would take ``keep`` for a plain write: none is sent."""
    log = RecordingLog()
    env = MachineEnvironment(kit.socket, changes=log)
    hello = env.hello

    async def old() -> dict[str, Any]:
        return {**await hello(), "version": 1}

    monkeypatch.setattr(env, "hello", old)
    assert call(env, "write_file", path="a.txt", content="x").status is ToolStatus.SUCCEEDED and log.seen == []
    monkeypatch.undo()  # restarted: recorded from then on
    assert call(env, "write_file", path="a.txt", content="y").status is ToolStatus.SUCCEEDED
    assert [s[0] for s in log.seen] == ["begin", "kept"]
