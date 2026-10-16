"""V3-P60 groundwork: the mcp.json keys, servers without Tools, lists read
only for declared capabilities, instructions and prompts kept at connect,
and prompt requests in the Conversation's session."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from mcp_fake import URL, FakeMcpServer
from mcp_stdio_fake import FakeLauncher

from himari.mcp import tools as tools_module

from himari.bootstrap.application import Application
from himari.bootstrap.operator import OPERATOR
from himari.common import TrustedScope
from himari.mcp.config import McpConfigError, McpResourcePolicy, parse_config
from himari.mcp.jsonrpc import McpRefused
from himari.mcp.tools import McpConnection, connect
from himari.provider.scripted import ScriptedAdapter
from himari.tool.api import EffectProfile
from himari.tool.schema import check_tool_input_schema

PROMPT = {"name": "calliope_commentary", "title": "Commentary", "description": "How to write it.",
          "arguments": [{"name": "game", "description": "The game id", "required": True}, {"name": "ply"}]}


def config_of(**changes: Any) -> Any:
    entry = {"url": URL, "tools": {"echo": {"effect": "read"}}, **changes}
    return parse_config(json.dumps({"servers": {"calliope": entry}}), {})


def connected(server: FakeMcpServer, **changes: Any) -> McpConnection:
    return asyncio.run(connect(config_of(**changes), check_schema=check_tool_input_schema, transport=server.transport()))


# --- mcp.json -------------------------------------------------------------------------------


def test_the_defaults_are_system_instructions_prompts_and_cautious_resources() -> None:
    (server,) = config_of().servers
    assert (server.instructions, server.prompts) == ("system", True)
    assert server.resources == McpResourcePolicy(EffectProfile.UNKNOWN, "listed")


def test_the_operator_sets_each_of_them() -> None:
    (server,) = config_of(instructions="data", prompts=False, resources={"effect": "read", "uris": "any"}).servers
    assert (server.instructions, server.prompts, server.resources) == (
        "data", False, McpResourcePolicy(EffectProfile.READ, "any"))
    assert config_of(resources=False).servers[0].resources is None
    assert config_of(instructions="off").servers[0].instructions == "off"


def test_tools_are_optional() -> None:
    entry = {"url": URL}
    (server,) = parse_config(json.dumps({"servers": {"calliope": entry}}), {}).servers
    assert server.tools == ()


@pytest.mark.parametrize("changes, message", [
    ({"instructions": True}, "instructions must be one of"),
    ({"instructions": "user"}, "instructions must be one of"),
    ({"prompts": "yes"}, "prompts must be true or false"),
    ({"resources": {"effect": "write"}}, "resources.effect must be one of"),
    ({"resources": {"uris": "all"}}, "resources.uris must be one of"),
    ({"resources": {"other": 1}}, "unknown keys"),
])
def test_a_mistake_in_them_is_refused(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(McpConfigError, match=message):
        config_of(**changes)


# --- connect --------------------------------------------------------------------------------


def test_instructions_and_prompts_are_kept_with_their_placement() -> None:
    server = FakeMcpServer()
    server.capabilities = {"tools": {}, "prompts": {}}
    server.instructions = "Use ply before commenting."
    server.prompts = [PROMPT, {"name": ""}, {"no": "name"}]
    connection = connected(server, instructions="data")
    note = connection.notes["calliope"]
    assert (note.placement, note.text) == ("data", "Use ply before commenting.")
    (prompt,) = connection.prompts["calliope"]  # malformed entries left out
    assert (prompt.name, prompt.title, [(a.name, a.required) for a in prompt.arguments]) == (
        "calliope_commentary", "Commentary", [("game", True), ("ply", False)])
    assert connection.capabilities["calliope"] == frozenset({"tools", "prompts"})


def test_instructions_off_or_absent_give_no_note() -> None:
    server = FakeMcpServer()
    server.instructions = "Some notes."
    assert connected(server, instructions="off").notes == {}
    assert connected(FakeMcpServer()).notes == {}


def test_lists_are_read_only_for_declared_capabilities() -> None:
    server = FakeMcpServer()
    server.capabilities = {}  # declares nothing
    server.instructions = "Hello."
    server.prompts = [PROMPT]
    connection = connected(server, tools={}, prompts=True)
    assert server.methods == []  # neither tools/list nor prompts/list
    (status,) = connection.statuses
    assert status.available and connection.notes["calliope"].text == "Hello."


def test_listed_tools_on_a_server_without_tools_are_a_problem() -> None:
    server = FakeMcpServer()
    server.capabilities = {"prompts": {}}
    server.prompts = [PROMPT]
    (status,) = connected(server).statuses
    assert status.available and status.tool_ids == ()
    assert "declares no tools" in status.problems[0]


def test_a_prompts_only_server_connects_without_tools() -> None:
    server = FakeMcpServer()
    server.capabilities = {"prompts": {}}
    server.prompts = [PROMPT]
    connection = connected(server, tools={})
    (status,) = connection.statuses
    assert status.available and status.problems == () and connection.handlers == []
    assert "tools/list" not in server.methods


def test_a_server_with_nothing_to_use_is_left_out() -> None:
    server = FakeMcpServer()
    server.capabilities = {"prompts": {}}  # but no prompts, no instructions, no listed Tools
    (status,) = connected(server, tools={}, resources=False).statuses
    assert not status.available and "nothing to use" in status.problems[0]


def test_resources_alone_make_a_server_usable() -> None:
    server = FakeMcpServer()
    server.capabilities = {"resources": {}}
    (status,) = connected(server, tools={}).statuses
    assert status.available
    (status,) = connected(server, tools={}, resources=False).statuses
    assert not status.available


def test_a_prompts_only_server_can_be_switched_per_conversation(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    (root / "mcp.json").write_text(json.dumps({"servers": {"calliope": {"url": URL, "default": "off"}}}))
    (root / "mcp.json").chmod(0o600)
    server = FakeMcpServer()
    server.capabilities = {"prompts": {}}
    server.prompts = [PROMPT]

    async def run() -> None:
        app = await Application.open(root, ScriptedAdapter([]), model_id="scripted-model", sandbox=False,
                                     mcp_transport=server.transport())
        try:
            scope = TrustedScope(OPERATOR, await app.create_conversation(OPERATOR))
            (view,) = await app.tool_sources(scope)
            assert (view.source, view.enabled, view.available) == ("calliope", False, True)
            await app.set_tool_source(scope, "calliope", True)
            (view,) = await app.tool_sources(scope)
            assert view.enabled
        finally:
            await app.close()

    asyncio.run(run())


# --- prompts/get in the Conversation's session (V3-P60 C2) ----------------------------------


def test_a_prompt_is_got_from_the_conversations_own_process() -> None:
    async def run() -> None:
        launcher = FakeLauncher()
        launcher.capabilities = {"tools": {}, "prompts": {}}
        launcher.prompts = [PROMPT]
        entry = {"command": "/usr/bin/fake-server", "tools": {"echo": {"effect": "read"}},
                 "processes": {"scope": "per_conversation", "max": 2}}
        config = parse_config(json.dumps({"servers": {"calliope": entry}}), {})
        connection = await connect(config, check_schema=check_tool_input_schema,
                                   launcher_for=lambda source, stdio: launcher)
        try:
            a = await connection.get_prompt("calliope", "calliope_commentary", {"game": "g"}, "conv-a")
            b = await connection.get_prompt("calliope", "calliope_commentary", {"game": "g"}, "conv-b")
            again = await connection.get_prompt("calliope", "calliope_commentary", {"game": "g"}, "conv-a")
            texts = [r["messages"][0]["content"]["text"] for r in (a, b, again)]
            assert texts == ["prompt calliope_commentary from process 1", "prompt calliope_commentary from process 2",
                             "prompt calliope_commentary from process 1"]  # 0 was the listing session
            with pytest.raises(McpRefused, match="no prompt"):
                await connection.get_prompt("calliope", "missing", {}, "conv-a")
        finally:
            await connection.aclose()

    asyncio.run(asyncio.wait_for(run(), 20))


# --- prompts are optional: their failure costs nothing else (PR #40 review) ----------------


@pytest.mark.parametrize("bad", [
    {"name": "example", "arguments": 123},
    {"name": "example", "arguments": [{"name": 5}]},
    {"name": 7},
    "not an object",
    {"name": "example", "title": {"x": 1}, "description": ["y"]},  # unusable texts: kept, emptied
])
def test_a_malformed_prompt_never_breaks_the_connection(bad: Any) -> None:
    """B1: a bad prompts/list entry stays with its server; the good one and the Tools stay."""
    server = FakeMcpServer()
    server.capabilities = {"tools": {}, "prompts": {}}
    server.prompts = [bad, PROMPT]
    connection = connected(server)
    (status,) = connection.statuses
    assert status.available and status.tool_ids == ("calliope/echo",)
    names = [p.name for p in connection.prompts["calliope"]]
    assert "calliope_commentary" in names


def test_one_servers_bad_prompts_do_not_touch_another_server() -> None:
    async def run() -> None:
        http = FakeMcpServer()
        http.capabilities = {"tools": {}, "prompts": {}}
        http.prompts = [{"name": "broken", "arguments": 123}]
        launcher = FakeLauncher()
        entries = {"calliope": {"url": URL, "tools": {"echo": {"effect": "read"}}},
                   "other": {"command": "/usr/bin/fake-server", "tools": {"echo": {"effect": "read"}}}}
        config = parse_config(json.dumps({"servers": entries}), {})
        connection = await connect(config, check_schema=check_tool_input_schema, transport=http.transport(),
                                   launcher_for=lambda source, stdio: launcher)
        try:
            assert [(s.source, s.available) for s in connection.statuses] == [("calliope", True), ("other", True)]
            assert "left out" in connection.statuses[0].problems[0]
        finally:
            await connection.aclose()

    asyncio.run(asyncio.wait_for(run(), 20))


def test_a_prompt_list_that_never_answers_keeps_the_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """B2: prompts/list has its own time limit; the Tools listed before it stay."""
    monkeypatch.setattr(tools_module, "PROMPTS_STARTUP_SECONDS", 0.3)

    async def run() -> None:
        launcher = FakeLauncher()
        launcher.capabilities = {"tools": {}, "prompts": {}}
        launcher.hang_prompts_list = True
        entry = {"command": "/usr/bin/fake-server", "tools": {"echo": {"effect": "read"}}}
        config = parse_config(json.dumps({"servers": {"calliope": entry}}), {})
        connection = await connect(config, check_schema=check_tool_input_schema,
                                   launcher_for=lambda source, stdio: launcher)
        try:
            (status,) = connection.statuses
            assert status.available and status.tool_ids == ("calliope/echo",)
            assert status.problems == ("prompts left out: no answer in time",)
            assert connection.prompts == {}
        finally:
            await connection.aclose()

    asyncio.run(asyncio.wait_for(run(), 20))


def test_a_prompt_list_over_its_byte_bound_is_left_out() -> None:
    server = FakeMcpServer()
    server.capabilities = {"tools": {}, "prompts": {}}
    server.prompts = [{"name": f"p{i}", "description": "x" * 20000} for i in range(60)]  # 1.2 MB
    (status,) = connected(server).statuses
    assert status.available and status.tool_ids == ("calliope/echo",)
    assert "bytes" in status.problems[0]



def test_a_prompt_list_that_never_answers_keeps_the_tools_views(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR #40 re-review B3: a timed-out prompts/list leaves a stdio listing
    session checking; the views are read before it, so they stay."""
    monkeypatch.setattr(tools_module, "PROMPTS_STARTUP_SECONDS", 0.3)
    from mcp_fake import ECHO_SCHEMA, VIEW_URI, view_entry

    async def run() -> None:
        launcher = FakeLauncher()
        launcher.capabilities = {"tools": {}, "prompts": {}, "resources": {}}
        launcher.tools = [{"name": "echo", "description": "Echo.", "inputSchema": ECHO_SCHEMA,
                           "_meta": {"ui": {"resourceUri": VIEW_URI}}}]
        launcher.resources = {VIEW_URI: {"contents": [view_entry()]}}
        launcher.hang_prompts_list = True
        entry = {"command": "/usr/bin/fake-server", "tools": {"echo": {"effect": "read"}}, "ui": {}}
        config = parse_config(json.dumps({"servers": {"calliope": entry}}), {})
        connection = await connect(config, check_schema=check_tool_input_schema,
                                   launcher_for=lambda source, stdio: launcher)
        try:
            (status,) = connection.statuses
            assert status.available and status.tool_ids == ("calliope/echo", "calliope/list_resources", "calliope/read_resource")
            assert connection.ui_for("calliope/echo") is not None  # the view was read
            assert status.problems == ("prompts left out: no answer in time",)
        finally:
            await connection.aclose()

    asyncio.run(asyncio.wait_for(run(), 20))
