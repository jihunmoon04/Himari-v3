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

Exit status 0 only when HIMARI did what the scenario asks: connected, and
for a scenario with a call, the Turn completed with that call recorded as
SUCCEEDED and its result text holding the expected answer (a completed Turn
alone is not enough: a failed Tool result is still answered by the model).
The suite's own checks judge what reached its server; this exit status
judges what HIMARI made of the answer.
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
from himari.data.api import ToolExecutionStatus, UserMessage
from himari.provider.api import NormalizedFinal, NormalizedToolCalls
from himari.provider.scripted import ScriptedAdapter

SOURCE = "conformance"
# The Tool each scenario's server offers, the arguments the model sends and
# text the result must contain ("" when the scenario fixes no answer).
# A scenario not listed here only connects (initialize, tools/list).
CALLS: dict[str, tuple[str, dict[str, JsonValue], str]] = {
    "tools_call": ("add_numbers", {"a": 2, "b": 3}, "is 5"),  # "The sum of 2 and 3 is 5"
    "elicitation-sep1034-client-defaults": ("test_client_elicitation_defaults", {}, ""),
    "sse-retry": ("test_reconnection", {}, ""),
}


async def run(url: str, scenario: str, *, transport: httpx.AsyncBaseTransport | None = None) -> int:
    """``transport``: tests only (the repository's fake server instead of the suite's)."""
    tool, arguments, expected = CALLS.get(scenario, ("add_numbers", {}, ""))
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
            if result.state is not DriveState.COMPLETED:
                return 1
            executions = [e for run_id in result.run_ids for e in await app.data.read_tool_executions(scope, run_id)]
            text = " ".join(getattr(block, "text", "") for e in executions if e.model_result is not None
                            for block in e.model_result.blocks)
            print(f"calls={[e.status.value for e in executions]} result={text[:200]!r}")
            succeeded = len(executions) == 1 and executions[0].status is ToolExecutionStatus.SUCCEEDED
            return 0 if succeeded and expected in text else 1
        finally:
            await app.close()


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: client.py <server-url> (MCP_CONFORMANCE_SCENARIO names the scenario)", file=sys.stderr)
        return 2
    return asyncio.run(run(argv[-1], os.environ.get("MCP_CONFORMANCE_SCENARIO", "")))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
