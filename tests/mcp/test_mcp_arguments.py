"""Tool arguments reach MCP servers as plain JSON at every depth (live trial,
2026-10-16): HIMARI keeps a call's arguments deep-frozen, and a nested object
or list used to fail to encode — after the call was claimed, so it was
reported as an unknown outcome although nothing was sent."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from mcp_fake import URL, FakeMcpServer
from mcp_stdio_fake import FakeLauncher
from testkit import calls, final, user

from himari.bootstrap.application import Application
from himari.bootstrap.operator import OPERATOR
from himari.common import CallId, ConversationId, PrincipalId, ToolId, TrustedScope
from himari.data.api import ToolExecutionStatus
from himari.mcp.config import parse_config
from himari.mcp.jsonrpc import McpRefused, request
from himari.mcp.tools import McpConnection, connect
from himari.provider.scripted import ScriptedAdapter
from himari.tool.api import ToolContext, ToolInvocation, ToolStatus
from himari.tool.schema import check_tool_input_schema

NESTED: dict[str, Any] = {
    "game": "g-9594f0efcd5bf0ca",
    "propositions": [{"id": "p1", "kind": "judgement", "label": None, "scopes": [{"kind": "specific_line"}]}],
    "sentences": [{"text": "5...h6는 부정확수입니다.", "class": "sourced", "cites": ["p1"]}],
    "agent": {"name": "HIMARI", "version": 1},
}
SUBMIT = {"name": "submit", "description": "Take the commentary.", "inputSchema": {"type": "object"}}


def answer(arguments: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": "accepted"}]}


def run(scenario: Callable[[], Awaitable[None]]) -> None:
    asyncio.run(asyncio.wait_for(scenario(), 30))


def config_of(entry: dict[str, Any]) -> Any:
    return parse_config(json.dumps({"servers": {"calliope": entry}}), {})


async def invoke(connection: McpConnection, arguments: dict[str, Any]) -> Any:
    (tool,) = connection.handlers
    ctx = ToolContext(scope=TrustedScope(PrincipalId("alice"), ConversationId("conv-a")),
                      artifacts=None)  # type: ignore[arg-type]
    # A ToolInvocation deep-freezes its arguments, as every call the model makes.
    return await tool.run(ToolInvocation(ToolId("calliope/submit"), "c1", arguments), ctx)  # type: ignore[arg-type]


def test_a_request_is_plain_json_at_every_depth() -> None:
    frozen = ToolInvocation(ToolId("s/t"), CallId("c"), NESTED).arguments
    message = request(1, "tools/call", {"name": "submit", "arguments": frozen})
    assert json.loads(json.dumps(message))["params"]["arguments"] == NESTED


def test_nested_arguments_reach_an_http_server_intact() -> None:
    server = FakeMcpServer([SUBMIT])
    server.handlers["submit"] = answer

    async def scenario() -> None:
        connection = await connect(config_of({"url": URL, "tools": {"submit": {"effect": "scoped_write"}}}),
                                   check_schema=check_tool_input_schema, transport=server.transport())
        outcome = await invoke(connection, NESTED)
        assert outcome.status is ToolStatus.SUCCEEDED
        assert server.calls == [("submit", NESTED)]
        await connection.aclose()

    run(scenario)


def test_nested_arguments_reach_a_stdio_server_intact() -> None:
    launcher = FakeLauncher()
    launcher.tools = [SUBMIT]
    launcher.handlers["submit"] = answer

    async def scenario() -> None:
        connection = await connect(
            config_of({"command": "/usr/bin/fake-server", "timeout_seconds": 5,
                       "tools": {"submit": {"effect": "scoped_write"}}}),
            check_schema=check_tool_input_schema, launcher_for=lambda source, stdio: launcher)
        outcome = await invoke(connection, NESTED)
        assert outcome.status is ToolStatus.SUCCEEDED
        assert [arguments for _, name, arguments in launcher.calls() if name == "submit"] == [NESTED]
        await connection.aclose()

    run(scenario)


def test_a_claimed_call_with_nested_arguments_runs_through_the_application(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    (root / "mcp.json").write_text(json.dumps({"servers": {"calliope": {
        "url": URL, "tools": {"submit": {"effect": "scoped_write"}}}}}))
    (root / "mcp.json").chmod(0o600)
    server = FakeMcpServer([SUBMIT])
    server.handlers["submit"] = answer
    script = ScriptedAdapter([calls(("calliope/submit", NESTED)), final("saved")])

    async def scenario() -> None:
        app = await Application.open(root, script, model_id="scripted-model", sandbox=False,
                                     mcp_transport=server.transport())
        try:
            scope = TrustedScope(OPERATOR, await app.create_conversation(OPERATOR))
            result = await app.runtime.driver.drive(scope, user("save it"))
            (execution,) = await app.data.read_tool_executions(scope, result.run_ids[0])
            assert execution.status is ToolExecutionStatus.SUCCEEDED  # it was UNKNOWN
            assert server.calls == [("submit", NESTED)]
        finally:
            await app.close()

    run(scenario)


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_what_cannot_be_encoded_is_refused_unsent(bad: float) -> None:
    """Strict JSON: such a request is refused (it never left), never uncertain."""
    server = FakeMcpServer()
    launcher = FakeLauncher()

    async def scenario() -> None:
        http = await connect(config_of({"url": URL, "tools": {"echo": {"effect": "read"}}}),
                             check_schema=check_tool_input_schema, transport=server.transport())
        sent = len(server.requests)
        with pytest.raises(McpRefused, match="could not be encoded"):
            await http.by_source["calliope"].call_tool("echo", {"text": bad}, key="conv-a")
        assert len(server.requests) == sent  # nothing went out
        await http.aclose()
        stdio = await connect(
            config_of({"command": "/usr/bin/fake-server", "timeout_seconds": 5, "tools": {"echo": {"effect": "read"}}}),
            check_schema=check_tool_input_schema, launcher_for=lambda source, s: launcher)
        client = stdio.by_source["calliope"]
        await client.call_tool("echo", {"text": "first"}, key="conv-a")
        with pytest.raises(McpRefused, match="could not be encoded"):
            await client.call_tool("echo", {"text": bad}, key="conv-a")
        session = client.current_session("conv-a")
        assert session is not None and not session.busy  # type: ignore[attr-defined]  # no request left waiting
        assert (await client.call_tool("echo", {"text": "after"}, key="conv-a"))["content"][0]["text"] == "echo: after"
        await stdio.aclose()

    run(scenario)
