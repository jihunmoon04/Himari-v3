"""Real-host Sandbox gate (opt-in: HIMARI_SANDBOX_HOST=1).

Like tests/live, this is a separate evidence gate, not part of module/full
pytest selection (testing-policy §2.1). It needs Linux with bubblewrap,
unprivileged user namespaces for bwrap, and Landlock ABI >= 6.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from testkit import SCOPE, calls, final, user

from himari.bootstrap import build_in_memory_runtime
from himari.common import (
    BindingId,
    CallId,
    ConversationId,
    EnvironmentInstanceId,
    PrincipalId,
    TextBlock,
    ToolId,
    TrustedScope,
    WorkspaceId,
    thaw,
)
from himari.control.api import DriveState
from himari.data.api import BindingAuthority, ConversationWorkspace
from himari.environment.inside import INSIDE, InsideEnvironment
from himari.environment.inside.sandbox import SandboxBackend, SandboxState
from himari.environment.inside.tools import MAX_OBSERVATION_BYTES
from himari.provider.scripted import ScriptedAdapter
from himari.tool.api import ToolContext, ToolInvocation, ToolOutcome, ToolStatus

pytestmark = pytest.mark.skipif(os.environ.get("HIMARI_SANDBOX_HOST") != "1", reason="host gate is opt-in")


@pytest.fixture(scope="module")
def backend() -> SandboxBackend:
    sandbox = SandboxBackend.discover()
    assert sandbox.state is SandboxState.CAPABLE_INACTIVE, "host lacks bwrap/Landlock/userns"
    assert asyncio.run(sandbox.start()) is SandboxState.ENFORCED, "startup self-test failed"
    return sandbox


@pytest.fixture
def env(tmp_path: Path, backend: SandboxBackend) -> InsideEnvironment:
    inside = InsideEnvironment(tmp_path / "workspaces", sandbox=backend)
    for ws in ("ws_alice", "ws_bob"):
        inside.provision(WorkspaceId(ws))
    (tmp_path / "workspaces/ws_bob/conversations/bob-secret.txt").write_text("bob only")
    return inside


def run(env: InsideEnvironment, argv: list[str], timeout: int | None = None, tool: str = "run_command", **extra: Any) -> ToolOutcome:
    handler = next(t for t in env.tools() if t.definition.tool_id == f"inside/{tool}")
    binding = BindingAuthority(BindingId("b"), EnvironmentInstanceId("e"), INSIDE, WorkspaceId("ws_alice"), 1, "conversations/c1")
    ctx = ToolContext(scope=TrustedScope(PrincipalId("alice"), ConversationId("c1")), artifacts=None,  # type: ignore[arg-type]
                      binding=binding, workspace=ConversationWorkspace(WorkspaceId("ws_alice"), "conversations/c1"))
    args: dict[str, Any] = {"argv": argv, **extra} if tool == "run_command" else dict(extra)
    if timeout is not None:
        args["timeout_seconds"] = timeout
    return asyncio.run(handler.run(ToolInvocation(ToolId(f"inside/{tool}"), CallId("c"), args), ctx))


def out(outcome: ToolOutcome) -> str:
    block = outcome.model_result.blocks[0]
    assert isinstance(block, TextBlock)
    return block.text


def raw(outcome: ToolOutcome) -> dict[str, Any]:
    value = thaw(outcome.raw_result)
    assert isinstance(value, dict)
    return value


def test_basic_command_in_the_working_directory(env: InsideEnvironment) -> None:
    outcome = run(env, ["sh", "-c", "pwd; echo hi; exit 3"])
    assert outcome.status is ToolStatus.SUCCEEDED and raw(outcome)["exit_code"] == 3
    assert "/workspace/conversations/c1\nhi" in out(outcome)


def test_command_writes_are_visible_to_file_tools(env: InsideEnvironment, tmp_path: Path) -> None:
    assert raw(run(env, ["sh", "-c", "mkdir -p out && printf 'made in sandbox' > out/r.txt"]))["exit_code"] == 0
    assert (tmp_path / "workspaces/ws_alice/conversations/c1/out/r.txt").read_text() == "made in sandbox"
    assert out(run(env, [], tool="read_file", path="out/r.txt")).endswith("made in sandbox")


def test_same_principal_folders_are_shared_other_principal_is_absent(env: InsideEnvironment) -> None:
    assert raw(run(env, ["sh", "-c", "echo x > /workspace/conversations/c2.txt"]))["exit_code"] == 0
    listing = run(env, ["sh", "-c", "ls -a /workspace /workspace/conversations; cat ../../../ws_bob/conversations/bob-secret.txt"])
    produced = out(listing).split("\n", 2)[2]  # skip the echoed "$ command" and status lines
    assert "bob only" not in produced and "ws_bob" not in produced.split("--- stderr")[0], produced
    assert "No such file or directory" in produced
    assert raw(listing)["exit_code"] != 0


@pytest.mark.parametrize("probe", [
    ["cat", "/etc/passwd"],
    ["ls", "/home"],
    ["sh", "-c", "cat /proc/1/environ"],
    ["python3", "-c", "import socket; socket.create_connection(('1.1.1.1', 53), timeout=3)"],
    ["python3", "-c", "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.sendto(b'x', ('1.1.1.1', 53))"],
    ["sh", "-c", "echo x > /usr/x"],
])
def test_host_and_network_are_out_of_reach(env: InsideEnvironment, probe: list[str]) -> None:
    outcome = run(env, probe)
    assert outcome.status is ToolStatus.SUCCEEDED and raw(outcome)["exit_code"] != 0, out(outcome)


def test_environment_is_scrubbed_and_secrets_do_not_leak(env: InsideEnvironment) -> None:
    os.environ["HIMARI_TEST_SECRET"] = "must-not-leak"
    try:
        text = out(run(env, ["env"]))
    finally:
        del os.environ["HIMARI_TEST_SECRET"]
    names = {line.split("=", 1)[0] for line in text.split("--- stdout")[1].splitlines()[1:] if "=" in line}
    assert "must-not-leak" not in text and "OPENROUTER" not in text
    assert names <= {"PATH", "HOME", "TMPDIR", "LANG", "TERM", "PWD", "SHLVL", "_"}, names


def test_timeout_kills_the_whole_tree_promptly(env: InsideEnvironment) -> None:
    marker = "37.4242"
    started = time.monotonic()
    outcome = run(env, ["sh", "-c", f"sleep {marker} & sleep {marker}"], timeout=1)
    assert time.monotonic() - started < 10
    assert outcome.status is ToolStatus.FAILED and raw(outcome)["timed_out"] is True
    time.sleep(0.5)
    left = subprocess.run(["pgrep", "-f", f"sleep {marker}"], capture_output=True, text=True)
    assert left.stdout.strip() == "", "a sandboxed process outlived the timeout"


def test_large_output_is_bounded(env: InsideEnvironment) -> None:
    outcome = run(env, ["sh", "-c", "head -c 3000000 /dev/zero | tr '\\0' 'a'; head -c 500000 /dev/urandom >&2"])
    assert len(out(outcome).encode()) <= MAX_OBSERVATION_BYTES
    assert raw(outcome)["stdout_bytes"] == 3000000 and raw(outcome)["stdout_truncated"] is True


def test_full_stack_through_the_driver(tmp_path: Path, backend: SandboxBackend) -> None:
    inside = InsideEnvironment(tmp_path / "ws", sandbox=backend)
    inside.provision(WorkspaceId("ws_alice"))
    provider = ScriptedAdapter([calls(("inside/run_command", {"argv": ["sh", "-c", "echo 6*7 | bc 2>/dev/null || echo 42"]})),
                                final("42")])
    rt = build_in_memory_runtime(provider, environments=[inside])
    rt.store.create_conversation(SCOPE)
    rt.store.activate_environment(SCOPE, INSIDE, WorkspaceId("ws_alice"))

    result = asyncio.run(rt.driver.drive(SCOPE, user("compute")))

    assert result.state is DriveState.COMPLETED
    record = asyncio.run(rt.data.read_tool_executions(SCOPE, result.run_ids[0]))[0]
    assert record.status.value == "succeeded" and record.unknown_outcome is not None  # claimed before running
    assert record.model_result is not None and "42" in str(record.model_result.blocks[0])


# --- PR #9 review: cancellation (B1), admission (B2), self-test coverage (B3) --------


def test_cancelling_a_running_command_kills_it(env: InsideEnvironment) -> None:
    marker = "53.1717"
    handler = next(t for t in env.tools() if t.definition.tool_id == "inside/run_command")
    binding = BindingAuthority(BindingId("b"), EnvironmentInstanceId("e"), INSIDE, WorkspaceId("ws_alice"), 1, "conversations/c1")
    ctx = ToolContext(scope=TrustedScope(PrincipalId("alice"), ConversationId("c1")), artifacts=None,  # type: ignore[arg-type]
                      binding=binding, workspace=ConversationWorkspace(WorkspaceId("ws_alice"), "conversations/c1"))

    async def scenario() -> None:
        task = asyncio.create_task(handler.run(ToolInvocation(ToolId("x"), CallId("c"), {"argv": ["sleep", marker]}), ctx))
        await asyncio.sleep(1.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not env.write_lock(WorkspaceId("ws_alice")).locked()

    asyncio.run(scenario())
    time.sleep(0.5)
    left = subprocess.run(["pgrep", "-f", f"sleep {marker}"], capture_output=True, text=True)
    assert left.stdout.strip() == "", "a sandboxed process survived cancellation"


def test_external_hard_link_blocks_the_command(env: InsideEnvironment, tmp_path: Path) -> None:
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("outside data")
    folder = tmp_path / "workspaces/ws_alice/conversations/c1"
    folder.mkdir(parents=True, exist_ok=True)
    os.link(outside, folder / "alias.txt")
    outcome = run(env, ["sh", "-c", "cat alias.txt; echo changed > alias.txt"])
    assert outcome.status is ToolStatus.DENIED and "hard-linked from outside" in out(outcome)
    assert outside.read_text() == "outside data"


@pytest.mark.parametrize("extra", [["--ro-bind", "/etc", "/etc"], ["--ro-bind", "{parent}", "/srv/data"],
                                   ["--ro-bind", "{parent}", "{parent}"]])
def test_self_test_fails_when_the_view_exposes_host_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                         extra: list[str]) -> None:
    from himari.environment.inside.sandbox import backend as sbmod
    from himari.environment.inside.sandbox.self_test import probe

    parent = str(tmp_path / "production-workspaces")
    os.makedirs(parent)
    extra = [x.replace("{parent}", parent) for x in extra]
    original = sbmod.bwrap_arguments

    def leaky(*args: Any, **kwargs: Any) -> list[str]:
        argv = original(*args, **kwargs)
        at = argv.index("--remount-ro")
        return argv[:at] + extra + argv[at:]

    monkeypatch.setattr(sbmod, "bwrap_arguments", leaky)
    report = asyncio.run(probe(SandboxBackend.discover(), (parent,)))
    assert not report.passed and any(f.startswith("mounts:") for f in report.failures), report.failures
