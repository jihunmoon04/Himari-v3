"""HIMARI as the client under test of the official MCP conformance suite.

The suite (``npx @modelcontextprotocol/conformance client``) starts a test
server for a scenario and runs this command with the server's URL as the last
argument and the scenario's name in ``MCP_CONFORMANCE_SCENARIO``. The suite
then judges what reached its server.

The whole Application is the client, as an operator runs it: a private data
root with an ``mcp.json`` naming the server, ``Application.open`` (which
initializes and lists the server's Tools), and one Conversation in which a
scripted model calls the scenario's Tool through the Dispatcher. Nothing is
mocked between the model's call and the wire.

Exit status 0 when HIMARI did what the scenario asks, 1 otherwise; the
suite's own checks are what count.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import httpx

from himari.bootstrap.application import Application
from himari.bootstrap.operator import OPERATOR
from himari.common import CallId, JsonValue, TextBlock, ToolCall, ToolId, TrustedScope
from himari.control.api import DriveState
from himari.data.api import UserMessage
from himari.provider.api import NormalizedFinal, NormalizedToolCalls
from himari.provider.scripted import ScriptedAdapter

SOURCE = "conformance"
# The Tool each scenario's server offers and the arguments the model sends.
# A scenario not listed here only connects (initialize, tools/list).
CALLS: dict[str, tuple[str, dict[str, JsonValue]]] = {
    "tools_call": ("add_numbers", {"a": 2, "b": 3}),
    "elicitation-sep1034-client-defaults": ("test_client_elicitation_defaults", {}),
    "sse-retry": ("test_reconnection", {}),
}


async def run(url: str, scenario: str, *, transport: httpx.AsyncBaseTransport | None = None) -> int:
    """``transport``: tests only (the repository's fake server instead of the suite's)."""
    tool, arguments = CALLS.get(scenario, ("add_numbers", {}))
    with tempfile.TemporaryDirectory(prefix="himari-conformance-") as tmp:
        root = Path(tmp) / "root"
        root.mkdir(mode=0o700)
        config = {"servers": {SOURCE: {"url": url, "timeout_seconds": 20, "tools": {tool: {"effect": "read"}}}}}
        (root / "mcp.json").write_text(json.dumps(config))
        (root / "mcp.json").chmod(0o600)
        script = [
            NormalizedToolCalls(calls=(ToolCall(CallId("c0"), 0, ToolId(f"{SOURCE}/{tool}"), arguments),)),
            NormalizedFinal(blocks=(TextBlock("done"),)),
        ]
        app = await Application.open(root, ScriptedAdapter(script), model_id="scripted-model", sandbox=False,
                                     environ={}, mcp_transport=transport)
        try:
            (status,) = app.mcp.statuses
            print(f"available={status.available} tools={list(status.tool_ids)} problems={list(status.problems)}")
            if scenario not in CALLS:
                return 0 if status.available else 1
            if f"{SOURCE}/{tool}" not in status.tool_ids:
                return 1
            scope = TrustedScope(OPERATOR, await app.create_conversation(OPERATOR))
            result = await app.runtime.driver.drive(scope, UserMessage(blocks=(TextBlock("go"),)))
            print(f"drive={result.state.value}")
            return 0 if result.state is DriveState.COMPLETED else 1
        finally:
            await app.close()


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: client.py <server-url> (MCP_CONFORMANCE_SCENARIO names the scenario)", file=sys.stderr)
        return 2
    return asyncio.run(run(argv[-1], os.environ.get("MCP_CONFORMANCE_SCENARIO", "")))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
