"""Sandbox and run_command logic that needs no bubblewrap (runs in CI)."""

from __future__ import annotations

import asyncio
import json
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


async def _passing(_: sb.SandboxBackend) -> bool:
    return True


async def _failing(_: sb.SandboxBackend) -> bool:
    return False


async def _raising(_: sb.SandboxBackend) -> bool:
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
