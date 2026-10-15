"""scripts/mcp_conformance/client.py: the adapter the official MCP
conformance suite runs keeps working without Node.js — the whole
Application connects and a scripted model's call reaches the server."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

from mcp_fake import URL, FakeMcpServer

ROOT = Path(__file__).resolve().parents[2]
ADD_SCHEMA: dict[str, Any] = {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                              "required": ["a", "b"]}


def _adapter() -> ModuleType:
    spec = importlib.util.spec_from_file_location("conformance_client", ROOT / "scripts" / "mcp_conformance" / "client.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


client = _adapter()


def adding_server() -> FakeMcpServer:
    server = FakeMcpServer([{"name": "add_numbers", "description": "Add two numbers.", "inputSchema": ADD_SCHEMA}])
    server.handlers["add_numbers"] = lambda args: {"content": [{"type": "text", "text": str(args["a"] + args["b"])}]}
    return server


def test_tools_call_reaches_the_server_through_the_application() -> None:
    server = adding_server()
    assert asyncio.run(client.run(URL, "tools_call", transport=server.transport())) == 0
    assert server.calls == [("add_numbers", {"a": 2, "b": 3})]


def test_a_scenario_without_a_call_only_connects() -> None:
    server = adding_server()
    assert asyncio.run(client.run(URL, "initialize", transport=server.transport())) == 0
    assert server.initializations == 1 and server.calls == []


def test_an_unreachable_server_fails_the_scenario() -> None:
    server = adding_server()
    server.down = True
    assert asyncio.run(client.run(URL, "initialize", transport=server.transport())) == 1
    assert asyncio.run(client.run(URL, "tools_call", transport=server.transport())) == 1
