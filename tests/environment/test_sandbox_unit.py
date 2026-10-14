"""Sandbox and run_command logic that needs no bubblewrap (runs in CI)."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
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
from himari.environment.inside import INSIDE, InsideEnvironment
from himari.environment.inside.sandbox import backend as sb
from himari.environment.inside.sandbox import self_test
from himari.environment.inside.tools import MAX_OBSERVATION_BYTES
from himari.tool.api import EffectProfile, ToolContext, ToolInvocation, ToolOutcome, ToolStatus
from himari.tool.registry import ToolRegistry

CAP = sb.HostCapability(bwrap="/usr/bin/bwrap", landlock_abi=8, helper="/srv/himari/landlock_helper.py")


# --- bwrap command line ---------------------------------------------------------------


def test_bwrap_arguments_come_from_trusted_inputs_only() -> None:
    args = sb.bwrap_arguments(CAP, root_fd=9, cwd="/workspace/conversations/c1", sync_fd=11, argv=["sh", "-c", "--bind / /x"])
    separator = args.index("--")  # bwrap options end here; the helper invocation follows
    sandbox_options, payload = args[:separator], args[separator + 1 :]
    assert payload[:4] == [sb.INTERPRETER, "-I", "-S", sb.HELPER_IN_SANDBOX]
    for flag in ("--unshare-all", "--unshare-user", "--die-with-parent", "--new-session", "--disable-userns", "--clearenv"):
        assert flag in sandbox_options, flag
    assert sandbox_options[sandbox_options.index("--cap-drop") + 1] == "ALL"
    assert sandbox_options[sandbox_options.index("--bind-fd") + 1 : sandbox_options.index("--bind-fd") + 3] == ["9", "/workspace"]
    assert sandbox_options.index("--remount-ro") < sandbox_options.index("--chdir")
    assert "--bind" not in sandbox_options and "--share-net" not in sandbox_options  # no host path bound read-write
    assert "--bind / /x" not in sandbox_options  # the requested argv never reaches bwrap's options
    assert payload[-3:] == ["sh", "-c", "--bind / /x"]
    policy = json.loads(payload[4])
    assert policy == {"read_roots": ["/"], "write_roots": ["/workspace", "/tmp"], "device_root": "/dev", "sync_fd": 11}
    env = {args[i + 1]: args[i + 2] for i, a in enumerate(args) if a == "--setenv"}
    assert env == sb.SANDBOX_ENV


@pytest.mark.parametrize(
    "argv",
    [[], "ls", ["ls", ""], ["x"] * (sb.MAX_ARGV + 1), ["a" * (sb.MAX_ARG_CHARS + 1)], ["ls", "a\0b"], [1]],
)
def test_invalid_argv_is_rejected(argv: Any) -> None:
    with pytest.raises(ValueError):
        sb.validate_argv(argv)


# --- state machine ----------------------------------------------------------------------


def test_no_capability_means_unavailable_and_nothing_runs() -> None:
    backend = sb.SandboxBackend(None)
    assert backend.state is sb.SandboxState.UNAVAILABLE
    assert asyncio.run(backend.start(_passing)) is sb.SandboxState.UNAVAILABLE
    with pytest.raises(sb.SandboxUnavailableError):
        asyncio.run(backend.run(["true"], root_fd=0, cwd="/workspace", timeout=5))


async def _passing(_: sb.SandboxBackend, __: tuple[str, ...] = ()) -> bool:
    return True


async def _failing(_: sb.SandboxBackend, __: tuple[str, ...] = ()) -> bool:
    return False


async def _raising(_: sb.SandboxBackend, __: tuple[str, ...] = ()) -> bool:
    raise RuntimeError("probe crashed")


@pytest.mark.parametrize(("test", "state"), [(_passing, sb.SandboxState.ENFORCED), (_failing, sb.SandboxState.SETUP_FAILED),
                                             (_raising, sb.SandboxState.SETUP_FAILED)])
def test_only_a_passing_self_test_enforces(test: Any, state: sb.SandboxState) -> None:
    backend = sb.SandboxBackend(CAP)
    assert backend.state is sb.SandboxState.CAPABLE_INACTIVE
    assert asyncio.run(backend.start(test)) is state
    with pytest.raises(RuntimeError):
        asyncio.run(backend.start(_passing))  # once per process, never re-promoted


def test_capable_but_not_started_refuses() -> None:
    with pytest.raises(sb.SandboxUnavailableError):
        asyncio.run(sb.SandboxBackend(CAP).run(["true"], root_fd=0, cwd="/workspace", timeout=5))


def test_setup_failure_at_run_time_latches(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = sb.SandboxBackend(CAP)
    asyncio.run(backend.start(_passing))

    async def broken(*a: Any, **k: Any) -> sb.CommandResult:
        raise sb.SandboxSetupError("no enforcement report")

    monkeypatch.setattr(backend, "launch", broken)
    with pytest.raises(sb.SandboxSetupError):
        asyncio.run(backend.run(["true"], root_fd=0, cwd="/workspace", timeout=5))
    assert backend.state is sb.SandboxState.SETUP_FAILED
    with pytest.raises(sb.SandboxUnavailableError):
        asyncio.run(backend.run(["true"], root_fd=0, cwd="/workspace", timeout=5))


def test_self_test_expectations_cover_every_layer() -> None:
    expected = self_test.EXPECTED
    for key in ("tcp_bind_inside", "udp_external", "interfaces", "nested_userns", "sees_other_principal",
                "unix_host_socket", "abstract_host_socket", "no_new_privs", "cap_eff_zero", "write_root"):
        assert key in expected, key
    assert expected["interfaces"] == ["lo"] and expected["write_sibling_conversation"] is True


# --- the Tool -------------------------------------------------------------------------------


class FakeBackend:
    def __init__(self, state: sb.SandboxState, result: sb.CommandResult | None = None, delay: float = 0) -> None:
        self.state = state
        self.result = result or sb.CommandResult(0, False, b"ok\n", b"", 3, 0, 5)
        self.delay = delay
        self.calls: list[dict[str, Any]] = []
        self.active = 0
        self.max_active = 0

    async def run(self, argv: list[str], *, root_fd: int, cwd: str, timeout: float) -> sb.CommandResult:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.calls.append({"argv": argv, "cwd": cwd, "timeout": timeout})
        await asyncio.sleep(self.delay)
        self.active -= 1
        return self.result


def make_env(tmp_path: Path, backend: Any) -> InsideEnvironment:
    env = InsideEnvironment(tmp_path / "workspaces", sandbox=backend)
    for ws in ("ws_alice", "ws_bob"):
        env.provision(WorkspaceId(ws))
    return env


def ctx_for(workspace: str = "ws_alice", cwd: str = "conversations/c1") -> ToolContext:
    binding = BindingAuthority(BindingId("b"), EnvironmentInstanceId("e"), INSIDE, WorkspaceId(workspace), 1, cwd)
    return ToolContext(scope=TrustedScope(PrincipalId("p"), ConversationId("c1")), artifacts=None,  # type: ignore[arg-type]
                       binding=binding, workspace=ConversationWorkspace(WorkspaceId(workspace), cwd))


def run_tool(env: InsideEnvironment, args: dict[str, Any], ctx: ToolContext | None = None) -> ToolOutcome:
    tool = next(t for t in env.tools() if t.definition.tool_id == "inside/run_command")
    return asyncio.run(tool.run(ToolInvocation(ToolId("inside/run_command"), CallId("c"), args), ctx or ctx_for()))


def text(outcome: ToolOutcome) -> str:
    block = outcome.model_result.blocks[0]
    assert hasattr(block, "text")
    return str(block.text)


def test_tool_exists_only_with_a_configured_sandbox(tmp_path: Path) -> None:
    assert "inside/run_command" not in {t.definition.tool_id for t in InsideEnvironment(tmp_path).tools()}
    env = make_env(tmp_path, FakeBackend(sb.SandboxState.ENFORCED))
    tool = next(t for t in env.tools() if t.definition.tool_id == "inside/run_command")
    assert tool.definition.effect_profile is EffectProfile.UNKNOWN  # claimed before running (V3-045)
    ToolRegistry(env.tools())  # schema passes the fail-closed registration checks


@pytest.mark.parametrize("state", [sb.SandboxState.UNAVAILABLE, sb.SandboxState.CAPABLE_INACTIVE, sb.SandboxState.SETUP_FAILED])
def test_not_enforced_means_denied_without_running(tmp_path: Path, state: sb.SandboxState) -> None:
    backend = FakeBackend(state)
    outcome = run_tool(make_env(tmp_path, backend), {"argv": ["ls"]})
    assert outcome.status is ToolStatus.DENIED and "not enforced" in text(outcome)
    assert backend.calls == []


@pytest.mark.parametrize("args", [{"argv": []}, {"argv": "ls"}, {"argv": ["ls"], "timeout_seconds": 0},
                                  {"argv": ["ls"], "timeout_seconds": sb.MAX_TIMEOUT_SECONDS + 1}])
def test_bad_arguments_are_denied_without_running(tmp_path: Path, args: dict[str, Any]) -> None:
    backend = FakeBackend(sb.SandboxState.ENFORCED)
    assert run_tool(make_env(tmp_path, backend), args).status is ToolStatus.DENIED
    assert backend.calls == []


def test_runs_in_the_bindings_working_directory_with_defaults(tmp_path: Path) -> None:
    backend = FakeBackend(sb.SandboxState.ENFORCED)
    env = make_env(tmp_path, backend)
    outcome = run_tool(env, {"argv": ["ls", "-la"]}, ctx_for(cwd="conversations/c9"))
    assert outcome.status is ToolStatus.SUCCEEDED and not outcome.model_result.is_error
    assert backend.calls == [{"argv": ["ls", "-la"], "cwd": "/workspace/conversations/c9", "timeout": sb.DEFAULT_TIMEOUT_SECONDS}]
    assert (tmp_path / "workspaces/ws_alice/conversations/c9").is_dir()
    assert text(outcome).startswith("$ ls -la\nexit code: 0\n--- stdout (3 bytes) ---\nok")


def test_nonzero_exit_is_a_completed_command_and_timeout_is_a_failure(tmp_path: Path) -> None:
    exited = run_tool(make_env(tmp_path, FakeBackend(sb.SandboxState.ENFORCED, sb.CommandResult(3, False, b"", b"boom", 0, 4, 1))),
                      {"argv": ["false"]})
    assert exited.status is ToolStatus.SUCCEEDED and exited.model_result.is_error and "exit code: 3" in text(exited)
    killed = run_tool(make_env(tmp_path / "t", FakeBackend(sb.SandboxState.ENFORCED, sb.CommandResult(None, True, b"", b"", 0, 0, 1000))),
                      {"argv": ["sleep", "99"], "timeout_seconds": 1})
    assert killed.status is ToolStatus.FAILED and "may have changed files" in text(killed)


def test_output_is_bounded_to_the_final_result_size(tmp_path: Path) -> None:
    huge = sb.CommandResult(0, False, b"\xff" * sb.MAX_OUTPUT_BYTES, b"e" * sb.MAX_OUTPUT_BYTES,
                            10 * sb.MAX_OUTPUT_BYTES, sb.MAX_OUTPUT_BYTES, 1)
    outcome = run_tool(make_env(tmp_path, FakeBackend(sb.SandboxState.ENFORCED, huge)), {"argv": ["x" * 4000] * 64})
    assert len(text(outcome).encode()) <= MAX_OBSERVATION_BYTES
    raw = thaw(outcome.raw_result)
    assert isinstance(raw, dict) and raw["stdout_truncated"] is True and raw["stderr_truncated"] is True
    assert raw["stdout_bytes"] == 10 * sb.MAX_OUTPUT_BYTES


def test_commands_on_one_workspace_are_serialized_but_not_across_workspaces(tmp_path: Path) -> None:
    backend = FakeBackend(sb.SandboxState.ENFORCED, delay=0.05)
    env = make_env(tmp_path, backend)
    tool = next(t for t in env.tools() if t.definition.tool_id == "inside/run_command")

    async def scenario(workspaces: list[str]) -> int:
        backend.max_active = 0
        await asyncio.gather(*(tool.run(ToolInvocation(ToolId("x"), CallId("c"), {"argv": ["true"]}), ctx_for(w))
                               for w in workspaces))
        return backend.max_active

    assert asyncio.run(scenario(["ws_alice", "ws_alice", "ws_alice"])) == 1
    assert asyncio.run(scenario(["ws_alice", "ws_bob"])) == 2


# --- PR #9 review B1: no process outlives launch() on any exit path -------------------

FAKE_SANDBOX = r"""
import os, sys, time, subprocess
sync_fd, pid_file, mode = int(sys.argv[1]), sys.argv[2], sys.argv[3]
child = subprocess.Popen(["sleep", "300"])
with open(pid_file, "w") as handle:
    handle.write(f"{os.getpid()} {child.pid}")
if mode != "no-sync":
    os.write(sync_fd, b"HIMARI_SANDBOX_ENFORCED\n")
os.close(sync_fd)
time.sleep(300)
"""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    with open(f"/proc/{pid}/stat") as handle:  # a zombie is dead for our purposes
        return handle.read().split(") ")[1][0] != "Z"


@pytest.mark.parametrize("mode", ["cancel", "no-sync", "timeout"])
def test_no_process_outlives_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    pid_file = tmp_path / "pids"
    monkeypatch.setattr(sb, "bwrap_arguments",
                        lambda cap, root_fd, cwd, sync_fd, argv: [sys.executable, "-c", FAKE_SANDBOX, str(sync_fd), str(pid_file), mode])
    monkeypatch.setattr(sb, "SETUP_TIMEOUT_SECONDS", 2.0)
    backend = sb.SandboxBackend(CAP)
    root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)

    async def scenario() -> None:
        task = asyncio.create_task(backend.launch(["x"], root_fd=root_fd, cwd="/workspace", timeout=1 if mode == "timeout" else 60))
        for _ in range(100):
            if pid_file.exists() and pid_file.read_text():
                break
            await asyncio.sleep(0.05)
        if mode == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif mode == "no-sync":
            with pytest.raises(sb.SandboxSetupError):
                await task
        else:
            result = await task
            assert result.timed_out

    try:
        asyncio.run(scenario())
    finally:
        os.close(root_fd)
    pids = [int(p) for p in pid_file.read_text().split()]
    deadline = time.monotonic() + 3
    while any(_alive(p) for p in pids) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not any(_alive(p) for p in pids), f"{mode}: survivors {pids}"


# --- PR #9 review B2: whole-Workspace admission ------------------------------------------


def test_admission_refuses_external_hard_links_but_allows_internal_ones(tmp_path: Path) -> None:
    from himari.environment.inside import admission

    root = tmp_path / "root"
    (root / "a").mkdir(parents=True)
    (root / "a/one.txt").write_text("x")
    os.link(root / "a/one.txt", root / "two.txt")  # both links inside: fine
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        admission.admit_workspace(fd, str(root))
        outside = tmp_path / "outside.txt"
        outside.write_text("host")
        os.link(outside, root / "a/alias.txt")
        with pytest.raises(admission.AdmissionError) as exc:
            admission.admit_workspace(fd, str(root))
        assert exc.value.category is admission.AdmissionCategory.EXTERNAL_HARDLINK
        with pytest.raises(admission.AdmissionError) as big:
            admission.admit_workspace(fd, str(root), max_entries=2)
        assert big.value.category is admission.AdmissionCategory.TOO_LARGE
    finally:
        os.close(fd)


def test_admission_refuses_nested_mounts_from_mountinfo() -> None:
    from himari.environment.inside import admission

    info = ("36 35 98:0 / /srv/ws/alice rw - ext4 /dev/x rw\n"
            "37 36 98:0 /other /srv/ws/alice/conversations/c1/mnt\\040x rw - ext4 /dev/x rw\n"
            "38 35 98:0 / /srv/ws/alice-other rw - ext4 /dev/x rw\n")
    assert admission.mounts_below("/srv/ws/alice", info) == ["/srv/ws/alice/conversations/c1/mnt x"]
    assert admission.mounts_below("/srv/ws/bob", info) == []


def test_run_command_is_refused_before_spawning_when_admission_fails(tmp_path: Path) -> None:
    backend = FakeBackend(sb.SandboxState.ENFORCED)
    env = make_env(tmp_path, backend)
    outside = tmp_path / "outside.txt"
    outside.write_text("host")
    os.link(outside, tmp_path / "workspaces/ws_alice/conversations/alias.txt")
    outcome = run_tool(env, {"argv": ["cat", "/workspace/conversations/alias.txt"]})
    assert outcome.status is ToolStatus.DENIED and "hard-linked from outside" in text(outcome)
    assert backend.calls == []


# --- PR #9 review B3: the self-test's mount allowlist (CI-safe part) ---------------------


def test_mount_allowlist_flags_any_extra_mount() -> None:
    binds = {p: (64, 100 + n) for n, p in enumerate(sorted(["/usr", "/etc/alternatives", "/run/himari/landlock_helper.py", "/workspace"]))}
    devices = {p: 0x100 + n for n, p in enumerate(sorted(self_test.DEVICE_NODES))}
    expected = self_test.ExpectedSources(binds, devices)
    fresh = [[p, t, r, False, [1, 1, 0]] for p, (t, r) in self_test.FRESH_MOUNTS.items()]
    nodes = [[p, "devtmpfs", "/" + p.rsplit("/", 1)[1], True, [5, 9, rdev]] for p, rdev in devices.items()]
    bound = [[p, "ext4", p, False, [dev, ino, 0]] for p, (dev, ino) in binds.items()]
    correct = fresh + nodes + bound + [["/proc/sys", "proc", "/sys", False, [2, 2, 0]]]

    def unexpected(table: object) -> list[str]:
        return self_test._unexpected_mounts(table, expected)

    def swap(point: str, entry: list[object]) -> list[list[object]]:
        return [e if e[0] != point else entry for e in correct]

    assert unexpected(correct) == []
    assert unexpected([*correct, ["/etc", "ext4", "/etc", False, [64, 7, 0]]]) == ["/etc"]
    assert unexpected([*correct, ["/dev/host-data", "ext4", "/etc", False, [64, 7, 0]]]) == ["/dev/host-data"]  # B4
    assert unexpected([*correct, ["/proc/host-data", "ext4", "/srv", False, [64, 8, 0]]]) == ["/proc/host-data"]
    assert unexpected([*correct, ["/usr", "ext4", "/etc", False, [64, 7, 0]]]) == ["/usr (stacked)"]
    # an allowed point with another source: host /etc bound at /etc/alternatives
    assert unexpected(swap("/etc/alternatives", ["/etc/alternatives", "ext4", "/etc", False, [64, 7, 0]])) == ["/etc/alternatives"]
    assert unexpected(swap("/dev/null", ["/dev/null", "ext4", "/etc/passwd", False, [64, 9, 0]])) == ["/dev/null"]
    other_device = devices["/dev/zero"]
    assert unexpected(swap("/dev/null", ["/dev/null", "devtmpfs", "/zero", True, [5, 9, other_device]])) == ["/dev/null"]
    assert unexpected(swap("/tmp", ["/tmp", "tmpfs", "/user/1000", False, [1, 1, 0]])) == ["/tmp"]
    assert unexpected(None) == ["<unreadable>"]
    assert "/etc/passwd" in self_test.DEFAULT_PROTECTED and "/home" in self_test.DEFAULT_PROTECTED


def test_admission_refuses_a_workspace_with_a_mount_below_it(tmp_path: Path) -> None:
    from himari.environment.inside import admission

    root = tmp_path / "root"
    root.mkdir()
    info = f"36 35 98:0 / {root}/conversations/c1/data rw - ext4 /dev/x rw\n"
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(admission.AdmissionError) as exc:
            admission.admit_workspace(fd, str(root), mountinfo=info)
        assert exc.value.category is admission.AdmissionCategory.NESTED_MOUNT
        admission.admit_workspace(fd, str(root), mountinfo="")  # no mounts below: admitted
    finally:
        os.close(fd)


# --- PR #9 re-review N1: cancellation while the process is being created ---------------


def _open_fds() -> int:
    return len(os.listdir("/proc/self/fd"))


def test_cancel_during_spawn_closes_the_sync_pipe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def slow_failing_spawn(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        await asyncio.sleep(0.02)
        raise OSError("spawn did not happen")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", slow_failing_spawn)
    backend = sb.SandboxBackend(CAP)
    root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)

    async def scenario() -> None:
        for _ in range(20):
            task = asyncio.create_task(backend.launch(["x"], root_fd=root_fd, cwd="/workspace", timeout=5))
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    try:
        before = _open_fds()
        asyncio.run(scenario())
        assert _open_fds() == before
    finally:
        os.close(root_fd)


def test_cancel_during_a_real_spawn_leaves_no_fd_or_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pid_dir = tmp_path / "pids"
    pid_dir.mkdir()
    counter = iter(range(1000))
    monkeypatch.setattr(sb, "bwrap_arguments", lambda cap, root_fd, cwd, sync_fd, argv: [
        sys.executable, "-c", FAKE_SANDBOX, str(sync_fd), str(pid_dir / str(next(counter))), "sync"])
    backend = sb.SandboxBackend(CAP)
    root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)

    async def scenario() -> None:
        for delay in [0.0] * 15 + [0.01, 0.03, 0.1, 0.3]:
            task = asyncio.create_task(backend.launch(["x"], root_fd=root_fd, cwd="/workspace", timeout=60))
            await asyncio.sleep(delay)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 15)  # a hang here fails as TimeoutError

    try:
        before = _open_fds()
        asyncio.run(scenario())
        assert _open_fds() == before
    finally:
        os.close(root_fd)
    pids = [int(p) for f in pid_dir.iterdir() if f.read_text() for p in f.read_text().split()]
    deadline = time.monotonic() + 3
    while any(_alive(p) for p in pids) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not any(_alive(p) for p in pids)


# --- PR #9 re-review N2 + advice: admission descriptor use and error mapping -------------

WIDE_TREE = r"""
import os, resource, sys
sys.path.insert(0, sys.argv[2])
from himari.environment.inside import admission
root = sys.argv[1]
for i in range(3000):
    os.makedirs(f"{root}/d{i}/sub")
resource.setrlimit(resource.RLIMIT_NOFILE, (128, resource.getrlimit(resource.RLIMIT_NOFILE)[1]))
fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
admission.admit_workspace(fd, root)
print("ADMITTED")
"""


def test_admission_of_a_wide_tree_uses_few_descriptors(tmp_path: Path) -> None:
    src = str(Path(__file__).resolve().parents[2] / "src")
    done = subprocess.run([sys.executable, "-I", "-c", WIDE_TREE, str(tmp_path), src],
                          capture_output=True, text=True, timeout=120)
    assert done.stdout.strip() == "ADMITTED", done.stderr[-2000:]


def test_unreadable_mount_table_is_a_typed_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from himari.environment.inside import admission

    def broken(host_root: str, mountinfo: str | None = None) -> list[str]:
        raise OSError("mountinfo unavailable")

    monkeypatch.setattr(admission, "mounts_below", broken)
    backend = FakeBackend(sb.SandboxState.ENFORCED)
    env = make_env(tmp_path, backend)
    outcome = run_tool(env, {"argv": ["true"]})
    assert outcome.status is ToolStatus.DENIED and "could not be verified" in text(outcome)
    assert backend.calls == []


# --- PR #9 re-review B5: a delayed spawn never outlives the cancelled call --------------


def test_cancelled_call_returns_only_after_a_late_spawned_process_is_dead(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_spawn = asyncio.create_subprocess_exec
    spawned: list[int] = []

    async def late_spawn(*args: object, **kwargs: Any) -> asyncio.subprocess.Process:
        await asyncio.sleep(sb._DRAIN_GRACE_SECONDS + 1)  # longer than any grace period
        process = await real_spawn(sys.executable, "-c", "import time; time.sleep(300)", stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        spawned.append(process.pid)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", late_spawn)
    monkeypatch.setattr(sb, "_DRAIN_GRACE_SECONDS", 0.5)
    backend = sb.SandboxBackend(CAP)
    root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)

    async def scenario() -> None:
        task = asyncio.create_task(backend.launch(["x"], root_fd=root_fd, cwd="/workspace", timeout=60))
        await asyncio.sleep(0.1)
        for _ in range(3):  # repeated cancellation must not cut the wait short
            task.cancel()
            await asyncio.sleep(0.2)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert spawned and not _alive(spawned[0]), "the call returned while its process could still run"

    try:
        asyncio.run(scenario())
    finally:
        os.close(root_fd)


# --- PR #11 review B1: started blocking work always completes before the call ends -------


class Gate:
    """Blocks a worker thread until opened; records how it ended."""

    def __init__(self) -> None:
        self.entered, self.release, self.finished = threading.Event(), threading.Event(), threading.Event()
        self.fd_valid_at_end: bool | None = None


async def _cancel_while_blocked(task: asyncio.Task[Any], gate: Gate, check: Callable[[], None]) -> None:
    while not gate.entered.is_set():
        await asyncio.sleep(0.01)
    for _ in range(3):  # repeated cancellation must not cut the wait short
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done(), "the call ended while its thread was still working"
        check()
    gate.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert gate.finished.is_set()


@pytest.mark.parametrize("tool_id", ["inside/write_file", "inside/read_file"])
def test_cancelled_file_tool_waits_for_its_thread_and_holds_the_lock(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool_id: str) -> None:
    from himari.environment.inside import tools as inside_tools

    env = make_env(tmp_path, None)
    (tmp_path / "workspaces/ws_alice/conversations/c1").mkdir(parents=True, exist_ok=True)
    (tmp_path / "workspaces/ws_alice/conversations/c1/f.txt").write_text("x")
    gate = Gate()
    cls = inside_tools.WriteFile if tool_id.endswith("write_file") else inside_tools.ReadFile
    real_act = cls.act

    def slow_act(self: Any, root_fd: int, relative: str, args: Any) -> ToolOutcome:
        gate.entered.set()
        gate.release.wait(10)
        try:
            return real_act(self, root_fd, relative, args)
        finally:
            gate.finished.set()

    monkeypatch.setattr(cls, "act", slow_act)
    tool = next(t for t in env.tools() if t.definition.tool_id == tool_id)
    lock = env.write_lock(WorkspaceId("ws_alice"))
    writes = tool_id.endswith("write_file")

    async def scenario() -> None:
        task = asyncio.create_task(tool.run(ToolInvocation(ToolId(tool_id), CallId("c"), {"path": "f.txt", "content": "y"}), ctx_for()))

        def held() -> None:
            assert lock.locked() is writes

        await _cancel_while_blocked(task, gate, held)
        assert not lock.locked()

    asyncio.run(scenario())


def test_cancelled_admission_never_outlives_the_root_descriptor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from himari.environment.inside import admission

    backend = FakeBackend(sb.SandboxState.ENFORCED)
    env = make_env(tmp_path, backend)
    gate = Gate()
    real_admit = admission.admit_workspace

    def slow_admit(root_fd: int, host_root: str, **kwargs: Any) -> None:
        gate.entered.set()
        gate.release.wait(10)
        try:
            os.fstat(root_fd)
            gate.fd_valid_at_end = True
        except OSError:
            gate.fd_valid_at_end = False
        try:
            real_admit(root_fd, host_root, **kwargs)
        finally:
            gate.finished.set()

    monkeypatch.setattr(admission, "admit_workspace", slow_admit)
    tool = next(t for t in env.tools() if t.definition.tool_id == "inside/run_command")
    lock = env.write_lock(WorkspaceId("ws_alice"))

    async def scenario() -> None:
        task = asyncio.create_task(tool.run(ToolInvocation(ToolId("inside/run_command"), CallId("c"), {"argv": ["true"]}), ctx_for()))
        await _cancel_while_blocked(task, gate, lambda: None if lock.locked() else pytest.fail("lock released"))

    asyncio.run(scenario())
    assert gate.fd_valid_at_end is True and backend.calls == []  # nothing ran after the cancellation
