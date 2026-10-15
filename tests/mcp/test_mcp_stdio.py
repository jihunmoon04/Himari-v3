"""MCP over stdio (V3-P59 §5) with an in-process fake server: the config,
listing, sessions per server or Conversation, cancelling and draining,
closing, certainty, restarts."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from mcp_stdio_fake import FakeLauncher

from himari.common import ConversationId, PrincipalId, ToolId, TrustedScope
from himari.mcp import stdio as stdio_module
from himari.mcp.config import McpConfigError, McpStdioConfig, parse_config
from himari.mcp.jsonrpc import McpRefused, McpUncertain
from himari.mcp.stdio import RESTART_NOTICE, State, StdioServer
from himari.mcp.tools import McpConnection, McpTool, connect
from himari.tool.api import ToolContext, ToolInvocation, ToolOutcome, ToolStatus
from himari.tool.schema import check_tool_input_schema

COMMAND = "/usr/bin/fake-server"


def entry(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"command": COMMAND, "args": ["--serve"], "timeout_seconds": 5,
                            "tools": {"echo": {"effect": "scoped_write"}}}
    return {**base, **changes}


def config_of(**changes: Any) -> Any:
    return parse_config(json.dumps({"servers": {"calliope": entry(**changes)}}), {"CALLIOPE_TOKEN": "t0k3n"})


async def connected(launcher: FakeLauncher | None, **changes: Any) -> McpConnection:
    return await connect(config_of(**changes), check_schema=check_tool_input_schema,
                         launcher_for=None if launcher is None else (lambda source, stdio: launcher))


def scope(conversation: str = "conv-a") -> TrustedScope:
    return TrustedScope(PrincipalId("alice"), ConversationId(conversation))


async def call(handler: McpTool, text: str, conversation: str = "conv-a") -> ToolOutcome:
    ctx = ToolContext(scope=scope(conversation), artifacts=None)  # type: ignore[arg-type]
    return await handler.run(ToolInvocation(ToolId("calliope/echo"), "c", {"text": text}), ctx)  # type: ignore[arg-type]


def text_of(outcome: ToolOutcome) -> str:
    return outcome.model_result.blocks[0].text  # type: ignore[union-attr]


def run(scenario: Callable[[], Awaitable[None]]) -> None:
    asyncio.run(asyncio.wait_for(scenario(), 30))


def gate() -> tuple[asyncio.Event, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]]:
    """A handler that answers only once the event is set."""
    event = asyncio.Event()

    async def slow(arguments: dict[str, Any]) -> dict[str, Any]:
        await event.wait()
        return {"content": [{"type": "text", "text": f"slow: {arguments.get('text')}"}]}

    return event, slow


# --- config -------------------------------------------------------------------------------


def test_a_stdio_server_is_configured_by_command() -> None:
    config = config_of(cwd="/srv/calliope", env={"PYTHONPATH": "."}, env_from=["CALLIOPE_TOKEN"],
                       read_only=["/srv/calliope"], writable=["/var/lib/calliope"], command="/srv/calliope/run",
                       processes={"scope": "per_conversation", "idle_seconds": 600, "max": 3})
    (server,) = config.servers
    assert server.url is None and server.stdio is not None
    stdio = server.stdio
    assert (stdio.command, stdio.args, stdio.cwd, stdio.read_only, stdio.writable) == (
        "/srv/calliope/run", ("--serve",), "/srv/calliope", ("/srv/calliope",), ("/var/lib/calliope",))
    assert dict(stdio.env) == {"PYTHONPATH": ".", "CALLIOPE_TOKEN": "t0k3n"}
    assert (stdio.scope, stdio.idle_seconds, stdio.max_processes) == ("per_conversation", 600.0, 3)
    assert "t0k3n" not in repr(config)
    shared = config_of().servers[0].stdio
    assert shared is not None and (shared.scope, shared.idle_seconds, shared.max_processes, shared.cwd) == (
        "shared", 1800.0, 1, "/tmp")


@pytest.mark.parametrize("changes, message", [
    ({"url": "https://example.com/mcp"}, "either url or command"),
    ({"token_env": "CALLIOPE_TOKEN"}, "unknown keys"),
    ({"command": "relative/server"}, "absolute"),
    ({"command": "/opt/server/run"}, "read_only directory"),  # invisible in the sandbox
    ({"args": ["x"] * 65}, "at most 64"),
    ({"env": {"1BAD": "x"}}, "variable name"),
    ({"env_from": ["NOT_SET"]}, "is not set"),
    ({"env": {"CALLIOPE_TOKEN": "x"}, "env_from": ["CALLIOPE_TOKEN"]}, "both env and env_from"),
    ({"read_only": ["/a/../b"]}, "normalized"),
    ({"writable": ["/a", "/b", "/c", "/d", "/e"]}, "at most 4"),
    ({"processes": {"scope": "per_server"}}, "scope"),
    ({"processes": {"idle_seconds": 10}}, "60..86400"),
    ({"processes": {"max": 3}}, "per_conversation only"),
    ({"processes": {"scope": "per_conversation", "max": 17}}, "1..16"),
])
def test_a_stdio_config_mistake_is_refused(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(McpConfigError, match=message):
        config_of(**changes)


# --- connect and call -------------------------------------------------------------------


def test_tools_are_listed_by_a_session_of_their_own_and_calls_use_another() -> None:
    async def scenario() -> None:
        launcher = FakeLauncher()
        connection = await connected(launcher, env={"PYTHONPATH": "."}, cwd="/usr")
        try:
            (status,) = connection.statuses
            assert status.available and status.tool_ids == ("calliope/echo",)
            (lister,) = launcher.processes
            assert lister.shutdowns == 1 and lister.argv == [COMMAND, "--serve"]
            assert (lister.env, lister.cwd) == ({"PYTHONPATH": "."}, "/usr")
            methods = [m.get("method") for m in lister.server.received]
            assert methods == ["initialize", "notifications/initialized", "tools/list"]
            outcome = await call(connection.handlers[0], "e4")
            assert (outcome.status, text_of(outcome)) == (ToolStatus.SUCCEEDED, "echo: e4")
            assert len(launcher.processes) == 2 and launcher.calls() == [(1, "echo", {"text": "e4"})]
        finally:
            await connection.aclose()
        assert all(p.shutdowns == 1 for p in launcher.processes)

    run(scenario)


def test_without_the_server_sandbox_a_stdio_server_is_left_out() -> None:
    async def scenario() -> None:
        connection = await connected(None)
        (status,) = connection.statuses
        assert not status.available and "server sandbox" in status.problems[0]
        assert connection.handlers == []

    run(scenario)


def test_a_launch_the_sandbox_refuses_leaves_the_server_out() -> None:
    async def scenario() -> None:
        launcher = FakeLauncher()
        launcher.fail_launch = RuntimeError("a bound tree contains HIMARI's data")
        connection = await connected(launcher)
        (status,) = connection.statuses
        assert not status.available and "HIMARI's data" in status.problems[0]

    run(scenario)


def test_shared_calls_of_many_conversations_go_to_one_process_at_once() -> None:
    async def scenario() -> None:
        launcher = FakeLauncher()
        event, slow = gate()
        launcher.handlers["echo"] = slow
        connection = await connected(launcher)
        handler = connection.handlers[0]
        try:
            calls = [asyncio.create_task(call(handler, f"m{i}", f"conv-{i}")) for i in range(3)]
            await asyncio.sleep(0.05)
            assert len(launcher.calls()) == 3  # all written before any answer
            event.set()
            assert sorted(text_of(o) for o in await asyncio.gather(*calls)) == ["slow: m0", "slow: m1", "slow: m2"]
            assert {p for p, _, _ in launcher.calls()} == {1}
        finally:
            await connection.aclose()

    run(scenario)


def test_per_conversation_gives_each_conversation_its_process_within_the_limit() -> None:
    async def scenario() -> None:
        launcher = FakeLauncher()
        connection = await connected(launcher, processes={"scope": "per_conversation", "max": 2})
        handler = connection.handlers[0]
        try:
            await call(handler, "a1", "conv-a")
            await call(handler, "b1", "conv-b")
            await call(handler, "a2", "conv-a")
            assert {args["text"]: p for p, _, args in launcher.calls()} == {"a1": 1, "a2": 1, "b1": 2}
            await call(handler, "c1", "conv-c")  # the least recently used idle one (b) makes room
            assert launcher.processes[2].shutdowns == 1 and launcher.processes[1].shutdowns == 0
            outcome = await call(handler, "b2", "conv-b")
            assert text_of(outcome).startswith(RESTART_NOTICE)  # its process was replaced
        finally:
            await connection.aclose()

    run(scenario)


def test_per_conversation_with_every_process_busy_refuses_without_running() -> None:
    async def scenario() -> None:
        launcher = FakeLauncher()
        event, slow = gate()
        launcher.handlers["echo"] = slow
        connection = await connected(launcher, processes={"scope": "per_conversation", "max": 1})
        handler = connection.handlers[0]
        try:
            busy = asyncio.create_task(call(handler, "a", "conv-a"))
            await asyncio.sleep(0.05)
            refused = await call(handler, "b", "conv-b")
            assert refused.status is ToolStatus.FAILED and "did not run" in text_of(refused)
            assert "no free session" in str(refused.raw_result)
            event.set()
            assert (await busy).status is ToolStatus.SUCCEEDED
        finally:
            await connection.aclose()

    run(scenario)


def test_a_server_ping_is_answered_and_its_other_requests_refused() -> None:
    async def scenario() -> None:
        launcher = FakeLauncher()

        async def asking(arguments: dict[str, Any]) -> dict[str, Any]:
            server = launcher.processes[-1].server
            await server.send({"jsonrpc": "2.0", "id": "p1", "method": "ping"})
            await server.send({"jsonrpc": "2.0", "id": "s1", "method": "sampling/createMessage", "params": {}})
            await server.send({"jsonrpc": "2.0", "method": "notifications/progress", "params": {}})
            await asyncio.sleep(0.05)
            return {"content": [{"type": "text", "text": "done"}]}

        launcher.handlers["echo"] = asking
        connection = await connected(launcher)
        try:
            assert text_of(await call(connection.handlers[0], "x")) == "done"
            answers = {m["id"]: m for m in launcher.processes[-1].server.received if "method" not in m}
            assert answers["p1"]["result"] == {} and answers["s1"]["error"]["code"] == -32601
        finally:
            await connection.aclose()

    run(scenario)


# --- cancelling and draining (§5.5) -------------------------------------------------------


def test_a_stopped_call_is_cancelled_and_new_calls_wait_until_it_is_answered() -> None:
    async def scenario() -> None:
        launcher = FakeLauncher()
        event, slow = gate()
        launcher.handlers["echo"] = slow
        launcher.honour_cancel = False  # the cancelled call finishes on its own, later
        connection = await connected(launcher)
        handler = connection.handlers[0]
        try:
            stopped = asyncio.create_task(call(handler, "a"))
            await asyncio.sleep(0.05)
            stopped.cancel()
            with pytest.raises(asyncio.CancelledError):
                await stopped
            process = launcher.processes[1]
            note = [m for m in process.server.received if m.get("method") == "notifications/cancelled"]
            assert len(note) == 1 and note[0]["params"]["reason"] == "cancelled by the user"
            waiting = asyncio.create_task(call(handler, "b", "conv-b"))
            await asyncio.sleep(0.05)
            assert [t for _, _, t in launcher.calls()] == [{"text": "a"}]  # DRAINING: b not written
            launcher.handlers["echo"] = lambda args: {"content": [{"type": "text", "text": f"now: {args['text']}"}]}
            event.set()  # the cancelled call answers: back to READY
            assert text_of(await waiting) == "now: b"
            assert {p for p, _, _ in launcher.calls()} == {1}  # the same process
        finally:
            await connection.aclose()

    run(scenario)


def test_an_unanswered_cancellation_closes_the_process_and_takes_its_other_calls(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stdio_module, "CANCEL_GRACE_SECONDS", 0.2)

    async def scenario() -> None:
        launcher = FakeLauncher()
        event, slow = gate()
        launcher.handlers["echo"] = slow
        launcher.honour_cancel = False
        connection = await connected(launcher)
        handler = connection.handlers[0]
        try:
            other = asyncio.create_task(call(handler, "other", "conv-b"))  # another Conversation's call
            stopped = asyncio.create_task(call(handler, "a"))
            await asyncio.sleep(0.05)
            stopped.cancel()
            await asyncio.gather(stopped, return_exceptions=True)
            with pytest.raises(McpUncertain, match="may have run"):  # V3-047: never reported as not run
                await other
            assert launcher.processes[1].shutdowns == 1
            launcher.handlers["echo"] = lambda args: {"content": [{"type": "text", "text": "fresh"}]}
            outcome = await call(handler, "next", "conv-b")
            assert text_of(outcome) == f"{RESTART_NOTICE}\n\nfresh" and outcome.raw_result["restarted"] is True  # type: ignore[index,call-overload]
            assert len(launcher.processes) == 3
        finally:
            event.set()
            await connection.aclose()

    run(scenario)


def test_a_timeout_cancels_the_request_and_is_uncertain() -> None:
    async def scenario() -> None:
        launcher = FakeLauncher()
        event, slow = gate()
        launcher.handlers["echo"] = slow
        connection = await connected(launcher, timeout_seconds=1)
        try:
            with pytest.raises(McpUncertain, match="no answer in 1 s"):
                await call(connection.handlers[0], "a")
            await asyncio.sleep(0.05)  # the notification is sent in the background
            note = [m for m in launcher.processes[1].server.received if m.get("method") == "notifications/cancelled"]
            assert note and note[0]["params"]["reason"] == "timed out"
        finally:
            event.set()
            await connection.aclose()

    run(scenario)


# --- breakage and certainty (§5.4) ----------------------------------------------------------


def test_a_crash_mid_call_is_uncertain_and_the_next_call_restarts() -> None:
    async def scenario() -> None:
        launcher = FakeLauncher()

        async def crashing(arguments: dict[str, Any]) -> dict[str, Any]:
            launcher.processes[-1].crash()
            await asyncio.sleep(10)
            return {}

        launcher.handlers["echo"] = crashing
        connection = await connected(launcher)
        try:
            with pytest.raises(McpUncertain):
                await call(connection.handlers[0], "a")
            launcher.handlers["echo"] = lambda args: {"content": [{"type": "text", "text": "back"}]}
            # the Conversation's first answer from the new process says so
            assert text_of(await call(connection.handlers[0], "b")) == f"{RESTART_NOTICE}\n\nback"
        finally:
            await connection.aclose()

    run(scenario)


@pytest.mark.parametrize("line", [b"not json", b'{"no": "jsonrpc"}', b'{"jsonrpc": "2.0", "id": "x", "result": {}}'])
def test_output_that_is_not_an_mcp_message_closes_the_process(line: bytes) -> None:
    async def scenario() -> None:
        launcher = FakeLauncher()

        async def garbling(arguments: dict[str, Any]) -> dict[str, Any]:
            await launcher.processes[-1].server.send_raw(line)
            await asyncio.sleep(10)
            return {}

        launcher.handlers["echo"] = garbling
        connection = await connected(launcher)
        try:
            with pytest.raises(McpUncertain):
                await call(connection.handlers[0], "a")
            assert launcher.processes[1].shutdowns == 1
        finally:
            await connection.aclose()

    run(scenario)


def test_a_process_already_ended_before_writing_is_not_run() -> None:
    async def scenario() -> None:
        launcher = FakeLauncher()
        server = StdioServer("calliope", McpStdioConfig(COMMAND, (), "/tmp", (), (), (), "shared", 60.0, 1),
                             launcher, capabilities={}, timeout_seconds=5)
        session = await server._session("conv-a")
        launcher.processes[0].returncode = 1  # ended; the reader has not noticed yet
        with pytest.raises(McpRefused, match="not ready"):
            await session.request("tools/call", {"name": "echo", "arguments": {}}, 5)
        assert launcher.calls() == []
        await server.aclose()

    run(scenario)


def test_failed_starts_back_off(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        launcher = FakeLauncher()
        connection = await connected(launcher)
        handler = connection.handlers[0]
        try:
            launcher.processes[-1].shutdowns = 0
            await call(handler, "a")
            launcher.processes[-1].crash()
            await asyncio.sleep(0.05)
            launcher.fail_launch = RuntimeError("sandbox setup failed")
            first = await call(handler, "b")
            assert first.status is ToolStatus.FAILED and "could not be started" in str(first.raw_result)
            launcher.fail_launch = None
            second = await call(handler, "c")  # within the 1 s back-off: refused without launching
            assert second.status is ToolStatus.FAILED and "trying again" in str(second.raw_result)
            assert len(launcher.processes) == 2
        finally:
            await connection.aclose()

    run(scenario)


def test_initialize_is_never_cancelled_a_slow_start_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stdio_module, "STARTUP_SECONDS", 0.2)

    async def scenario() -> None:
        launcher = FakeLauncher()
        launcher.initialize_delay = 1.0
        connection = await connected(launcher)
        (status,) = connection.statuses
        assert not status.available and "did not start" in status.problems[0]
        (process,) = launcher.processes
        assert process.shutdowns == 1
        assert not [m for m in process.server.received if m.get("method") == "notifications/cancelled"]

    run(scenario)


def test_an_idle_process_is_closed_and_the_next_call_starts_another() -> None:
    async def scenario() -> None:
        launcher = FakeLauncher()
        server = StdioServer("calliope", McpStdioConfig(COMMAND, (), "/tmp", (), (), (), "shared", 0.2, 1),
                             launcher, capabilities={}, timeout_seconds=5)
        try:
            await server.request("tools/call", {"name": "echo", "arguments": {"text": "a"}}, "conv-a")
            await asyncio.sleep(0.5)
            assert launcher.processes[0].shutdowns == 1  # idle: closed
            await server.request("tools/call", {"name": "echo", "arguments": {"text": "b"}}, "conv-a")
            assert len(launcher.processes) == 2 and server.take_restart("conv-a")
            assert not server.take_restart("conv-a")  # told once
        finally:
            await server.aclose()

    run(scenario)


def test_session_states_follow_the_cancelled_list() -> None:
    async def scenario() -> None:
        launcher = FakeLauncher()
        event, slow = gate()
        launcher.handlers["echo"] = slow
        launcher.honour_cancel = False
        server = StdioServer("calliope", McpStdioConfig(COMMAND, (), "/tmp", (), (), (), "shared", 60.0, 1),
                             launcher, capabilities={}, timeout_seconds=5)
        try:
            session = await server._session(None)

            def state() -> State:  # read afresh each time
                return session.state

            assert state() is State.READY
            calls = [asyncio.create_task(session.request("tools/call", {"name": "echo", "arguments": {"text": t}}, 5))
                     for t in ("a", "b")]
            await asyncio.sleep(0.05)
            calls[0].cancel()
            await asyncio.sleep(0.01)
            assert state() is State.DRAINING
            calls[1].cancel()  # cancelling more while draining
            await asyncio.gather(*calls, return_exceptions=True)
            event.set()
            await asyncio.sleep(0.05)
            assert state() is State.READY  # both answered
        finally:
            await server.aclose()

    run(scenario)


# --- PR #34 review -------------------------------------------------------------------------


def test_b1_concurrent_conversations_never_pass_the_process_limit() -> None:
    """Two Conversations needing a place at once must not both close the same
    idle session and both start a process."""
    async def scenario() -> None:
        launcher = FakeLauncher()
        launcher.shutdown_delay = 0.1  # a slow close widens the window
        event, slow = gate()
        connection = await connected(launcher, processes={"scope": "per_conversation", "max": 1})
        handler = connection.handlers[0]
        try:
            await call(handler, "a", "conv-a")  # conv-a holds the one place, idle
            launcher.handlers["echo"] = slow
            b = asyncio.create_task(call(handler, "b", "conv-b"))
            c = asyncio.create_task(call(handler, "c", "conv-c"))
            await asyncio.sleep(0.4)
            assert launcher.most_alive == 1
            event.set()
            outcomes = await asyncio.gather(b, c)
            assert sorted(o.status.value for o in outcomes) == ["failed", "succeeded"]
            refused = next(o for o in outcomes if o.status is ToolStatus.FAILED)
            assert "no free session" in str(refused.raw_result) and "did not run" in text_of(refused)
            assert launcher.most_alive == 1
        finally:
            event.set()
            await connection.aclose()

    run(scenario)


def test_b1_a_session_chosen_to_make_room_admits_no_request() -> None:
    async def scenario() -> None:
        launcher = FakeLauncher()
        server = StdioServer("calliope", McpStdioConfig(COMMAND, (), "/tmp", (), (), (), "per_conversation", 60.0, 1),
                             launcher, capabilities={}, timeout_seconds=5)
        try:
            session = await server._session("conv-a")
            closing = asyncio.create_task(session.close("its place went to another Conversation"))
            await asyncio.sleep(0)  # close has begun; the process is still shutting down
            assert session.process.returncode is None
            with pytest.raises(McpRefused, match="not ready"):  # CLOSING from the moment close is called
                await session.request("tools/call", {"name": "echo", "arguments": {}}, 5)
            await closing
            assert launcher.calls() == []
        finally:
            await server.aclose()

    run(scenario)


def test_b2_a_server_that_stops_reading_times_out_and_is_closed() -> None:
    async def scenario() -> None:
        launcher = FakeLauncher()
        connection = await connected(launcher, timeout_seconds=1)
        try:
            launcher.stop_reading = True
            start = asyncio.get_running_loop().time()
            with pytest.raises(McpUncertain, match="could not be written in 1 s"):
                await call(connection.handlers[0], "a")
            assert asyncio.get_running_loop().time() - start < 2
            await asyncio.sleep(0.05)
            assert launcher.processes[1].shutdowns == 1  # not used again: part of the request may be in it
            launcher.stop_reading = False
            outcome = await call(connection.handlers[0], "b")
            assert text_of(outcome) == f"{RESTART_NOTICE}\n\necho: b"
        finally:
            await connection.aclose()

    run(scenario)


def test_b2_a_stop_while_writing_closes_the_session() -> None:
    async def scenario() -> None:
        launcher = FakeLauncher()
        connection = await connected(launcher)
        try:
            launcher.stop_reading = True
            stopped = asyncio.create_task(call(connection.handlers[0], "a"))
            await asyncio.sleep(0.05)
            stopped.cancel()
            await asyncio.gather(stopped, return_exceptions=True)
            await asyncio.sleep(0.05)
            assert launcher.processes[1].shutdowns == 1
            assert not [m for m in launcher.processes[1].server.received
                        if m.get("method") == "notifications/cancelled"]  # nothing more goes into that stream
        finally:
            await connection.aclose()

    run(scenario)

