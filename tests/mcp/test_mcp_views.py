"""MCP Apps views in the MCP connector (V3-P58): config, connect, run."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import pytest
from mcp_fake import ECHO_SCHEMA, URL, VIEW_HTML, VIEW_URI, FakeMcpServer, view_entry
from testkit import SCOPE, calls, final, user

from himari.bootstrap import build_runtime
from himari.common import thaw
from himari.control.api import DriveState
from himari.data.memory import InMemoryStore
from himari.mcp import tools as tools_module
from himari.mcp.client import UI_EXTENSION, UI_MIME_TYPE
from himari.mcp.config import McpConfigError, McpUiPolicy, parse_config
from himari.mcp.tools import McpConnection, connect
from himari.provider.scripted import ScriptedAdapter
from himari.tool.api import MAX_APP_PAYLOAD_BYTES, MAX_UI_HTML_BYTES, ToolContext, ToolInvocation, ToolStatus, UiCsp
from himari.tool.schema import check_tool_input_schema

UI = {"connect_domains": ["https://api.example.com"], "resource_domains": ["https://cdn.example.com"]}


def viewed_tool(name: str = "echo", uri: object = VIEW_URI, **ui: Any) -> dict[str, Any]:
    return {"name": name, "description": f"{name} with a view", "inputSchema": ECHO_SCHEMA,
            "_meta": {"ui": {"resourceUri": uri, **ui}}}


def config_text(**server: Any) -> str:
    entry = {"url": URL, "tools": {"echo": {"effect": "read"}}, **server}
    return json.dumps({"servers": {"calliope": entry}})


def connected(server: FakeMcpServer, **entry: Any) -> McpConnection:
    config = parse_config(config_text(**entry), {})
    return asyncio.run(connect(config, check_schema=check_tool_input_schema, transport=server.transport()))


def view_server(*tools: dict[str, Any], **resources: Any) -> FakeMcpServer:
    server = FakeMcpServer(list(tools) or [viewed_tool()])
    server.resources = {VIEW_URI: {"contents": [view_entry()]}, **resources}
    return server


def problems(connection: McpConnection) -> tuple[str, ...]:
    (status,) = connection.statuses
    return status.problems


# --- config --------------------------------------------------------------------------------


def test_ui_is_the_operators_upper_bound_and_off_by_default() -> None:
    (server,) = parse_config(config_text(ui=UI), {}).servers
    assert server.ui == McpUiPolicy(frozenset({"https://api.example.com"}), frozenset({"https://cdn.example.com"}))
    (plain,) = parse_config(config_text(), {}).servers
    assert plain.ui is None
    (empty,) = parse_config(config_text(ui={}), {}).servers
    assert empty.ui == McpUiPolicy()  # views on, no external origin


@pytest.mark.parametrize("ui, message", [
    ({"extra": []}, "unknown keys"),
    ({"connect_domains": "https://a.com"}, "list of at most"),
    ({"connect_domains": ["https://a.com/path"]}, "not an origin"),
    ({"connect_domains": ["https://a.com; script-src *"]}, "not an origin"),
    ({"connect_domains": ["https://A.com"]}, "not an origin"),
    ({"connect_domains": ["http://example.com"]}, "only to a loopback"),
    ({"connect_domains": ["http://*.localhost"]}, "only to a loopback"),
    ({"resource_domains": ["wss://live.example.com"]}, "only connect_domains"),
    ({"connect_domains": ["https://a.com:70000"]}, "not a valid port"),
    ({"connect_domains": [f"https://a{i}.com" for i in range(33)]}, "list of at most"),
])
def test_a_ui_mistake_is_refused(ui: dict[str, Any], message: str) -> None:
    with pytest.raises(McpConfigError, match=message):
        parse_config(config_text(ui=ui), {})


def test_websocket_and_loopback_origins_are_accepted() -> None:
    (server,) = parse_config(config_text(ui={"connect_domains": [
        "wss://live.example.com", "https://*.example.com:8443", "http://127.0.0.1:9000", "ws://localhost"]}), {}).servers
    assert server.ui is not None and len(server.ui.connect_domains) == 4


# --- connect -------------------------------------------------------------------------------


def test_only_a_server_with_ui_is_told_that_views_are_shown() -> None:
    with_ui, without = view_server(), view_server()
    connected(with_ui, ui=UI)
    connected(without)
    assert with_ui.client_capabilities == {"extensions": {UI_EXTENSION: {"mimeTypes": [UI_MIME_TYPE]}}}
    assert without.client_capabilities == {}
    assert without.resource_reads == []  # and its views are never read


def test_a_view_is_read_once_at_connect_and_given_to_its_tools() -> None:
    entry = view_entry(_meta={"ui": {"csp": {"connectDomains": ["https://api.example.com"],
                                             "resourceDomains": ["https://cdn.example.com"]},
                                     "prefersBorder": True, "permissions": {"camera": {}}, "domain": "x.example"}})
    server = view_server(viewed_tool("echo"), viewed_tool("shout"))
    server.resources[VIEW_URI] = {"contents": [entry]}
    connection = connected(server, ui=UI, tools={"echo": {"effect": "read"}, "shout": {"effect": "read"}})
    assert server.resource_reads == [VIEW_URI]  # one read for both Tools
    echo, shout = connection.ui_for("calliope/echo"), connection.ui_for("calliope/shout")
    assert echo is not None and echo is shout
    assert (echo.source, echo.uri, echo.html, echo.prefers_border) == ("calliope", VIEW_URI, VIEW_HTML, True)
    assert echo.csp == UiCsp(("https://api.example.com",), ("https://cdn.example.com",))  # as declared
    assert problems(connection) == ()
    assert connection.ui_policy("calliope") == McpUiPolicy(frozenset(UI["connect_domains"]),
                                                           frozenset(UI["resource_domains"]))


def test_tool_info_is_the_tool_as_its_server_listed_it() -> None:
    connection = connected(view_server(), ui=UI)
    assert connection.tool_info("calliope/echo") == {
        "name": "echo", "description": "echo with a view", "inputSchema": ECHO_SCHEMA}
    assert connection.tool_info("calliope/nope") is None


@pytest.mark.parametrize("ui_off", [False, True])
def test_an_app_only_tool_is_never_registered(ui_off: bool) -> None:
    server = view_server(viewed_tool("echo", visibility=["app"]))
    connection = connected(server, **({} if ui_off else {"ui": UI}))
    assert connection.handlers == []
    assert "app-only Tool" in problems(connection)[0]


@pytest.mark.parametrize("ui", [[], "model", {"visibility": ["model", "agent"]}, {"visibility": "model"}])
def test_a_tool_whose_visibility_cannot_be_read_is_left_out(ui: object) -> None:
    tool = {"name": "echo", "inputSchema": ECHO_SCHEMA,
            "_meta": {"ui": ui if not isinstance(ui, dict) else {"resourceUri": VIEW_URI, **ui}}}
    connection = connected(view_server(tool), ui=UI)
    assert connection.handlers == []
    assert "left out" in problems(connection)[0]


@pytest.mark.parametrize("uri", ["https://fake/view", 3, "ui://" + "x" * 2050])
def test_a_bad_resource_uri_leaves_out_only_the_view(uri: object) -> None:
    connection = connected(view_server(viewed_tool(uri=uri)), ui=UI)
    assert [h.definition.tool_id for h in connection.handlers] == ["calliope/echo"]
    assert connection.ui_for("calliope/echo") is None
    assert "view left out: resourceUri" in problems(connection)[0]


@pytest.mark.parametrize("answer, reason", [
    (None, "server error -32602"),
    ({"contents": []}, "no single entry"),
    ({"contents": [view_entry(), view_entry()]}, "no single entry"),
    ({"contents": [view_entry(uri="ui://fake/other")]}, "no single entry"),
    ({"contents": [view_entry(mimeType="text/html")]}, "mimeType"),
    ({"contents": [view_entry(blob="PHA+")]}, "exactly one of text or blob"),
    ({"contents": [{"uri": VIEW_URI, "mimeType": UI_MIME_TYPE, "blob": "not base64!"}]}, "base64"),
    ({"contents": [{"uri": VIEW_URI, "mimeType": UI_MIME_TYPE, "blob": base64.b64encode(b"\xff").decode()}]}, "UTF-8"),
    ({"contents": [view_entry(html="x" * (MAX_UI_HTML_BYTES + 1))]}, "larger than"),
    ({"contents": [view_entry(_meta={"ui": {"csp": []}})]}, "csp is not an object"),
    ({"contents": [view_entry(_meta={"ui": {"csp": {"connectDomains": "https://a.com"}}})]}, "connectDomains"),
    ({"contents": [view_entry(_meta={"ui": {"csp": {"resourceDomains": [3]}}})]}, "resourceDomains"),
    ({"contents": "nope"}, "no single entry"),
])
def test_a_view_that_cannot_be_used_is_left_out_and_its_tool_stays(answer: Any, reason: str) -> None:
    server = view_server()
    if answer is None:
        server.resources = {}
    else:
        server.resources[VIEW_URI] = answer
    connection = connected(server, ui=UI)
    assert [h.definition.tool_id for h in connection.handlers] == ["calliope/echo"]
    assert connection.ui_for("calliope/echo") is None
    (problem,) = problems(connection)
    assert problem.startswith("echo: view left out:") and reason in problem


def test_a_blob_view_and_a_spaced_mime_type_are_accepted() -> None:
    server = view_server()
    server.resources[VIEW_URI] = {"contents": [{"uri": VIEW_URI, "mimeType": "text/html; profile=mcp-app",
                                                "blob": base64.b64encode(VIEW_HTML.encode()).decode()}]}
    resource = connected(server, ui=UI).ui_for("calliope/echo")
    assert resource is not None and resource.html == VIEW_HTML


def test_a_servers_views_are_bounded_in_total(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools_module, "MAX_UI_BYTES_PER_SERVER", len(VIEW_HTML) + 10)
    server = view_server(viewed_tool("echo"), viewed_tool("shout", uri="ui://fake/second"))
    server.resources["ui://fake/second"] = {"contents": [view_entry(uri="ui://fake/second")]}
    connection = connected(server, ui=UI, tools={"echo": {"effect": "read"}, "shout": {"effect": "read"}})
    assert connection.ui_for("calliope/echo") is not None
    assert connection.ui_for("calliope/shout") is None
    assert "views are larger than" in problems(connection)[0]


def test_views_not_read_in_time_are_left_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools_module, "UI_STARTUP_SECONDS", 0.05)
    server = view_server()
    server.resource_delay = 5
    connection = connected(server, ui=UI)
    assert [h.definition.tool_id for h in connection.handlers] == ["calliope/echo"]
    assert problems(connection) == ("echo: view left out: not read in time",)


# --- run -----------------------------------------------------------------------------------


def run_echo(connection: McpConnection, text: str = "hi") -> Any:
    (tool,) = connection.handlers
    return asyncio.run(tool.run(ToolInvocation(tool.definition.tool_id, "c1", {"text": text}),  # type: ignore[arg-type]
                                ToolContext(SCOPE, None)))  # type: ignore[arg-type]


def test_a_result_carries_the_view_read_at_connect_and_no_request_is_added() -> None:
    server = view_server()
    server.handlers["echo"] = lambda args: {"content": [{"type": "text", "text": "hi"}],
                                            "structuredContent": {"said": args["text"]}, "_meta": {"t": 1},
                                            "extra": "not for the view"}
    connection = connected(server, ui=UI)
    reads = len(server.resource_reads)
    outcome = run_echo(connection)
    assert outcome.status is ToolStatus.SUCCEEDED
    assert outcome.app_view.resource is connection.ui_for("calliope/echo")
    assert thaw(outcome.app_view.payload) == {"content": [{"type": "text", "text": "hi"}],
                                              "structuredContent": {"said": "hi"}, "_meta": {"t": 1}}
    assert len(server.resource_reads) == reads  # V3-P58 §3.1: nothing after tools/call


def test_a_result_the_server_marks_as_error_keeps_its_view() -> None:
    server = view_server()
    server.handlers["echo"] = lambda args: {"content": [{"type": "text", "text": "bad"}], "isError": True}
    outcome = run_echo(connected(server, ui=UI))
    assert outcome.status is ToolStatus.FAILED
    assert outcome.app_view.payload["isError"] is True


def test_a_refused_call_has_no_view() -> None:
    server = view_server()
    connection = connected(server, ui=UI)
    server.next_status = 401
    outcome = run_echo(connection)
    assert (outcome.status, outcome.app_view) == (ToolStatus.FAILED, None)


def test_a_result_too_large_for_the_view_is_shown_as_text_only() -> None:
    server = view_server()
    server.handlers["echo"] = lambda args: {"content": [{"type": "text", "text": "ok"}],
                                            "structuredContent": {"blob": "x" * MAX_APP_PAYLOAD_BYTES}}
    outcome = run_echo(connected(server, ui=UI))
    assert (outcome.status, outcome.app_view) == (ToolStatus.SUCCEEDED, None)
    assert "larger than" in outcome.raw_result["ui_problem"]


def test_a_model_call_stores_its_view_with_the_result() -> None:
    server = view_server()
    connection = connected(server, ui=UI)
    adapter = ScriptedAdapter([calls(("calliope/echo", {"text": "e4"})), final("done")])
    rt = build_runtime(adapter, tools=connection.handlers, store=InMemoryStore(), external_defaults=connection.defaults)
    rt.store.create_conversation(SCOPE)
    result = asyncio.run(rt.driver.drive(SCOPE, user("go")))
    assert result.state is DriveState.COMPLETED
    (execution,) = asyncio.run(rt.data.read_tool_executions(SCOPE, result.run_ids[0]))
    stored = asyncio.run(rt.data.read_app_view(SCOPE, execution.call))
    assert stored is not None and stored.view.resource == connection.ui_for("calliope/echo")
    assert thaw(stored.view.payload) == {"content": [{"type": "text", "text": "echo: e4"}]}
