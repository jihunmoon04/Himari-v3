"""MCP client (V3-P53): config, Streamable HTTP, Tool mapping, and the full
path from a model's call to a fake MCP server and back."""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp_fake import ECHO_SCHEMA, URL, FakeMcpServer
from testkit import SCOPE, calls, final, runtime, user

from himari.bootstrap import build_runtime
from himari.common import ToolId, thaw, tool_id_problem
from himari.control.api import DriveState
from himari.data.api import ToolExecutionStatus, ToolResultRecord
from himari.data.memory import InMemoryStore
from himari.mcp import client as client_module
from himari.mcp.client import McpRefused, McpUncertain, http_client
from himari.mcp.config import McpConfigError, load_config, map_tool_names, parse_config
from himari.mcp.tools import McpConnection, connect, render_result
from himari.provider.scripted import ScriptedAdapter
from himari.tool.api import EffectProfile, ToolContext, ToolInvocation, ToolStatus
from himari.tool.schema import check_tool_input_schema

TOKEN = "secret-token-value"


def config_text(**server: Any) -> str:
    entry = {"url": URL, "tools": {"echo": {"effect": "read"}}, **server}
    return json.dumps({"servers": {"calliope": entry}})


def connected(server: FakeMcpServer, **entry: Any) -> McpConnection:
    config = parse_config(config_text(**entry), {"CALLIOPE_TOKEN": TOKEN})
    return asyncio.run(connect(config, check_schema=check_tool_input_schema, transport=server.transport()))


# --- config ------------------------------------------------------------------------------


def test_a_config_lists_servers_tools_and_the_operators_effects() -> None:
    config = parse_config(config_text(token_env="CALLIOPE_TOKEN", default="off", timeout_seconds=5,
                                      tools={"echo": {"effect": "read"}, "move": {"effect": "scoped_write"}}),
                          {"CALLIOPE_TOKEN": TOKEN})
    (server,) = config.servers
    assert (server.name, server.url, server.token, server.enabled_by_default, server.timeout_seconds) == (
        "calliope", URL, TOKEN, False, 5.0)
    assert [(t.name, t.tool_id, t.effect) for t in server.tools] == [
        ("echo", "calliope/echo", EffectProfile.READ), ("move", "calliope/move", EffectProfile.SCOPED_WRITE)]
    assert TOKEN not in repr(config)


@pytest.mark.parametrize("change, message", [
    ({"url": "http://example.com/mcp"}, "only to a loopback"),
    ({"url": "ftp://127.0.0.1/mcp"}, "https://"),
    ({"url": "https://user:pw@example.com/mcp"}, "https://"),
    ({"token_env": "MISSING_VARIABLE"}, "is not set"),
    ({"default": "yes"}, "default"),
    ({"timeout_seconds": 0}, "timeout_seconds"),
    ({"tools": {}}, "1..128 Tools"),
    ({"tools": {"echo": {"effect": "write"}}}, "effect must be"),
    ({"tools": {"echo": {"effect": "read", "x": 1}}}, "unknown keys"),
    ({"extra": True}, "unknown keys"),
])
def test_a_config_mistake_is_refused(change: dict[str, Any], message: str) -> None:
    with pytest.raises(McpConfigError, match=message):
        parse_config(config_text(**change), {})


@pytest.mark.parametrize("text", [
    '[]', '{"servers": []}', '{"servers": {"calliope": []}}', '{"servers": {"calliope": {"url": 3, "tools": {}}}}',
    config_text(url="http://[::1"), config_text(url="http://127.0.0.1:99999/mcp"),
    config_text(tools={"echo": {"effect": ["read"]}}), config_text(tools={"echo": {"effect": {"a": 1}}}),
    config_text(tools=["echo"]), config_text(tools={"echo": "read"}), config_text(default=["on"]),
    config_text(token_env=3), config_text(timeout_seconds="5"), config_text(timeout_seconds=True), "{bad json",
])
def test_any_malformed_config_is_a_config_error(text: str) -> None:
    """PR #20 review C2: never a bare TypeError/ValueError."""
    with pytest.raises(McpConfigError):
        parse_config(text, {})


@pytest.mark.parametrize("name", ["Calliope", "inside/x", "a_b", "", "x" * 25])
def test_a_server_name_is_a_short_lowercase_namespace(name: str) -> None:
    with pytest.raises(McpConfigError, match="server name"):
        parse_config(json.dumps({"servers": {name: {"url": URL, "tools": {"echo": {"effect": "read"}}}}}), {})


def test_https_and_loopback_http_urls_are_accepted() -> None:
    for url in ("https://mcp.example.com/mcp", "http://localhost:8790/mcp", "http://[::1]:8790/mcp"):
        parse_config(config_text(url=url), {})


def test_the_config_file_must_be_private(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    assert load_config(str(path), {}).servers == ()  # no file: no servers
    path.write_text(config_text())
    path.chmod(0o644)
    with pytest.raises(McpConfigError, match="private"):
        load_config(str(path), {})
    path.chmod(0o600)
    assert [s.name for s in load_config(str(path), {}).servers] == ["calliope"]
    link = tmp_path / "link.json"
    os.symlink(path, link)
    with pytest.raises(McpConfigError, match="symbolic link"):
        load_config(str(link), {})


def test_server_tool_names_map_to_valid_unique_tool_ids() -> None:
    names = ["get.position", "get-position", "a__b", "x" * 128, "plain", "日本"]
    ids = map_tool_names("calliope", names)
    assert ids["plain"] == "calliope/plain" and ids["a__b"] == "calliope/a_b"
    assert len(set(ids.values())) == len(names)
    assert all(tool_id_problem(i) is None for i in ids.values())
    # "get.position" and "get-position" collide after mapping: both get a hash, deterministically.
    assert ids["get.position"] != ids["get-position"] and ids["get.position"].startswith("calliope/get-position-")
    assert map_tool_names("calliope", list(reversed(names))) == ids


# --- the client ------------------------------------------------------------------------------


@pytest.mark.parametrize("sse", [False, True])
def test_the_client_starts_a_session_lists_pages_and_calls(sse: bool) -> None:
    server = FakeMcpServer([{"name": f"t{i}", "inputSchema": {"type": "object"}} for i in range(5)],
                           token=TOKEN, sse=sse, page_size=2)

    async def scenario() -> None:
        client = http_client(URL, TOKEN, timeout_seconds=5, transport=server.transport())
        try:
            assert [t["name"] for t in await client.list_tools()] == [f"t{i}" for i in range(5)]
            server.handlers["t1"] = lambda args: {"content": [{"type": "text", "text": f"got {args}"}]}
            assert (await client.call_tool("t1", {"a": 1}))["content"][0]["text"] == "got {'a': 1}"
        finally:
            await client.aclose()

    asyncio.run(scenario())
    initialize, initialized, *rest = server.requests
    assert json.loads(initialize.content)["method"] == "initialize"
    assert json.loads(initialized.content)["method"] == "notifications/initialized"
    for request in rest:
        assert request.headers["mcp-session-id"] == "session-1"
        assert request.headers["mcp-protocol-version"] == "2025-11-25"
    assert all(r.headers["authorization"] == f"Bearer {TOKEN}" for r in server.requests)


def test_a_forgotten_session_is_renewed_once_and_the_request_sent_again() -> None:
    server = FakeMcpServer()

    async def scenario() -> None:
        client = http_client(URL, None, timeout_seconds=5, transport=server.transport())
        await client.call_tool("echo", {"text": "one"})
        server.forget_sessions()
        await client.call_tool("echo", {"text": "two"})
        await client.aclose()

    asyncio.run(scenario())
    assert server.initializations == 2 and server.calls == [("echo", {"text": "one"}), ("echo", {"text": "two"})]
    assert "authorization" not in server.requests[0].headers


def test_concurrent_first_calls_share_one_session() -> None:
    server = FakeMcpServer()

    async def scenario() -> None:
        client = http_client(URL, None, timeout_seconds=5, transport=server.transport())
        await asyncio.gather(*(client.call_tool("echo", {"text": str(i)}) for i in range(5)))
        await client.aclose()

    asyncio.run(scenario())
    assert server.initializations == 1 and len(server.calls) == 5


@pytest.mark.parametrize("setup, failure", [
    (lambda s: setattr(s, "next_status", 400), McpRefused),
    (lambda s: setattr(s, "next_status", 403), McpRefused),
    (lambda s: setattr(s, "next_rpc_error", -32602), McpRefused),
    (lambda s: setattr(s, "next_status", 500), McpUncertain),
    (lambda s: setattr(s, "next_status", 401), McpRefused),
    (lambda s: setattr(s, "next_status", 415), McpRefused),
    # PR #20 review B1: these statuses do not prove the Tool did not run.
    (lambda s: setattr(s, "next_status", 408), McpUncertain),
    (lambda s: setattr(s, "next_status", 429), McpUncertain),
    (lambda s: setattr(s, "next_status", 409), McpUncertain),
    (lambda s: setattr(s, "next_status", 422), McpUncertain),
    (lambda s: setattr(s, "next_status", 201), McpUncertain),
    (lambda s: setattr(s, "next_status", 307), McpUncertain),  # never followed: the token stays here
    (lambda s: setattr(s, "next_rpc_error", -32603), McpUncertain),
    (lambda s: setattr(s, "next_error", httpx.ReadTimeout("slow")), McpUncertain),
    (lambda s: setattr(s, "down", True), McpRefused),
])
def test_call_failures_say_whether_the_tool_may_have_run(setup: Any, failure: type[Exception]) -> None:
    server = FakeMcpServer()

    async def scenario() -> None:
        client = http_client(URL, None, timeout_seconds=5, transport=server.transport())
        await client.initialize()
        setup(server)
        with pytest.raises(failure):
            await client.call_tool("echo", {"text": "x"})
        await client.aclose()

    asyncio.run(scenario())


def test_a_server_of_an_unknown_protocol_version_is_refused() -> None:
    server = FakeMcpServer(protocol_version="1999-01-01")

    async def scenario() -> None:
        client = http_client(URL, None, timeout_seconds=5, transport=server.transport())
        with pytest.raises(McpRefused, match="protocol version"):
            await client.initialize()
        await client.aclose()

    asyncio.run(scenario())


def test_results_are_rendered_as_text_for_the_model() -> None:
    assert render_result({"content": [{"type": "text", "text": "a"}, {"type": "image", "mimeType": "image/png"},
                                      {"type": "resource", "resource": {"uri": "u", "text": "inline"}},
                                      {"type": "resource_link", "name": "n", "uri": "u2"}]}) == (
        "a\n[image content omitted (image/png)]\ninline\n[resource link: n u2]")
    assert render_result({"content": [], "structuredContent": {"best": "e4"}}) == '{"best": "e4"}'
    assert render_result({}) == "(no content)"
    assert render_result({"content": [{"type": "text", "text": "x" * 100_000}]}).endswith("was cut]")


# --- connecting ------------------------------------------------------------------------------


def test_only_listed_tools_are_registered_with_the_operators_effect() -> None:
    server = FakeMcpServer([
        {"name": "echo", "description": "d" * 5000, "inputSchema": ECHO_SCHEMA,
         "annotations": {"readOnlyHint": False}},
        {"name": "surprise", "inputSchema": {"type": "object"}},
    ])
    connection = connected(server, tools={"echo": {"effect": "read"}, "missing": {"effect": "read"}})
    (handler,) = connection.handlers
    d = handler.definition
    assert (d.tool_id, d.external_source, d.effect_profile) == ("calliope/echo", "calliope", EffectProfile.READ)
    assert len(d.description) == 2000
    (status,) = connection.statuses
    assert status.available and status.tool_ids == ("calliope/echo",)
    assert status.problems == ("missing: the server does not offer it",)
    asyncio.run(connection.aclose())


def test_a_tool_whose_schema_is_refused_is_left_out() -> None:
    server = FakeMcpServer([{"name": "echo", "inputSchema": {"type": "object", "properties": {"a": {"$ref": "http://x"}}}}])
    connection = connected(server)
    assert connection.handlers == [] and "input schema refused" in connection.statuses[0].problems[0]


def test_an_unreachable_server_is_left_out_not_fatal() -> None:
    server = FakeMcpServer()
    server.down = True
    connection = connected(server)
    assert connection.handlers == []
    (status,) = connection.statuses
    assert not status.available and status.problems[0].startswith("unavailable: initialize: cannot connect")
    assert connection.defaults == {"calliope": True}


def test_a_wrong_token_leaves_the_server_out() -> None:
    server = FakeMcpServer(token="other")
    assert connected(server, token_env="CALLIOPE_TOKEN").statuses[0].problems == ("unavailable: initialize: HTTP 401",)


def test_a_refused_call_fails_and_an_is_error_result_is_shown_to_the_model() -> None:
    server = FakeMcpServer()
    server.handlers["echo"] = lambda args: {"content": [{"type": "text", "text": "bad square"}], "isError": True}
    (handler,) = connected(server).handlers
    ctx = ToolContext(scope=SCOPE, artifacts=None)  # type: ignore[arg-type]

    async def scenario() -> None:
        outcome = await handler.run(ToolInvocation(ToolId("calliope/echo"), "c", {"text": "x"}), ctx)  # type: ignore[arg-type]
        assert outcome.status is ToolStatus.FAILED and outcome.model_result.is_error
        assert outcome.model_result.blocks[0].text == "bad square"  # type: ignore[union-attr]
        server.next_status = 403
        refused = await handler.run(ToolInvocation(ToolId("calliope/echo"), "c", {"text": "x"}), ctx)  # type: ignore[arg-type]
        assert refused.status is ToolStatus.FAILED and "did not run" in refused.model_result.blocks[0].text  # type: ignore[union-attr]
        server.next_error = httpx.ReadTimeout("slow")
        with pytest.raises(McpUncertain):  # may have run: the Dispatcher decides by its claim
            await handler.run(ToolInvocation(ToolId("calliope/echo"), "c", {"text": "x"}), ctx)  # type: ignore[arg-type]

    asyncio.run(scenario())


# --- end to end --------------------------------------------------------------------------------


def mcp_runtime(server: FakeMcpServer, script: list[Any], *, default: str = "on", effect: str = "read") -> Any:
    connection = connected(server, default=default, tools={"echo": {"effect": effect}})
    adapter = ScriptedAdapter(script)
    rt = build_runtime(adapter, tools=connection.handlers, store=InMemoryStore(),
                       external_defaults=connection.defaults)
    rt.store.create_conversation(SCOPE)
    return rt, adapter


def test_a_model_call_reaches_the_server_and_its_result_comes_back() -> None:
    server = FakeMcpServer(sse=True)
    rt, adapter = mcp_runtime(server, [calls(("calliope/echo", {"text": "e4"})), final("done")])
    result = asyncio.run(rt.driver.drive(SCOPE, user("go")))
    assert result.state is DriveState.COMPLETED
    assert server.calls == [("echo", {"text": "e4"})]
    assert "calliope/echo" in [d.tool_id for d in adapter.prepared_inputs[0].tool_catalog]
    events = asyncio.run(rt.data.read_events(SCOPE))
    record = next(e.payload for e in events if isinstance(e.payload, ToolResultRecord))
    assert record.model_result.blocks[0].text == "echo: e4"  # type: ignore[union-attr]
    (execution,) = asyncio.run(rt.data.read_tool_executions(SCOPE, result.run_ids[0]))
    assert TOKEN not in json.dumps(thaw(execution.raw_result))


def test_invalid_arguments_never_reach_the_server() -> None:
    server = FakeMcpServer()
    rt, _ = mcp_runtime(server, [calls(("calliope/echo", {"text": 3})), final("done")])
    result = asyncio.run(rt.driver.drive(SCOPE, user("go")))
    assert result.state is DriveState.COMPLETED and server.calls == []
    (execution,) = asyncio.run(rt.data.read_tool_executions(SCOPE, result.run_ids[0]))
    assert execution.status is ToolExecutionStatus.DENIED


def test_a_source_turned_off_is_hidden_and_its_calls_denied() -> None:
    server = FakeMcpServer()
    rt, adapter = mcp_runtime(server, [calls(("calliope/echo", {"text": "a"})), final("one"),
                                       calls(("calliope/echo", {"text": "b"})), final("two")], default="off")
    asyncio.run(rt.driver.drive(SCOPE, user("first")))
    assert server.calls == []  # off by default: not offered, and the call is denied
    assert "calliope/echo" not in [d.tool_id for d in adapter.prepared_inputs[0].tool_catalog]
    asyncio.run(rt.data.set_external_setting(SCOPE, "calliope", True))
    asyncio.run(rt.driver.drive(SCOPE, user("second")))
    assert server.calls == [("echo", {"text": "b"})]
    assert "calliope/echo" in [d.tool_id for d in adapter.prepared_inputs[2].tool_catalog]


def test_an_uncertain_write_is_reported_unknown_and_never_resent() -> None:
    server = FakeMcpServer()
    server.next_error = httpx.ReadTimeout("slow")
    rt, adapter = mcp_runtime(server, [calls(("calliope/echo", {"text": "a"})), final("checked")], effect="unknown")
    result = asyncio.run(rt.driver.drive(SCOPE, user("go")))
    assert result.state is DriveState.COMPLETED
    (execution,) = asyncio.run(rt.data.read_tool_executions(SCOPE, result.run_ids[0]))
    assert execution.status is ToolExecutionStatus.UNKNOWN
    assert len(server.calls) == 1


def test_the_testkit_runtime_still_enables_registered_sources_by_default() -> None:
    server = FakeMcpServer()
    rt, adapter = runtime([calls(("calliope/echo", {"text": "a"})), final("ok")], tools=connected(server).handlers)
    asyncio.run(rt.driver.drive(SCOPE, user("go")))
    assert server.calls == [("echo", {"text": "a"})]


def test_a_registered_source_without_a_default_is_refused() -> None:
    with pytest.raises(ValueError, match="no default"):
        build_runtime(ScriptedAdapter([]), tools=connected(FakeMcpServer()).handlers, external_defaults={})


# --- PR #20 review regressions --------------------------------------------------------------


class Router(httpx.AsyncBaseTransport):
    """Several fake servers behind one transport, by URL."""

    def __init__(self, *servers: FakeMcpServer) -> None:
        self._by_url = {s.url: s for s in servers}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        return await self._by_url[str(request.url)].handle(request)


@pytest.mark.parametrize("list_result", [
    {"tools": [{"name": ["echo"], "inputSchema": ECHO_SCHEMA}]},  # an unhashable name
    {"tools": [{"name": "echo", "description": {"x": 1}, "inputSchema": ECHO_SCHEMA}]},
    {"tools": [{"name": "echo", "inputSchema": ECHO_SCHEMA}, {"name": "echo", "inputSchema": {"type": "object"}}]},
    {"tools": [{"name": "echo", "inputSchema": {"type": "object", "properties": {"a": {"pattern": "("}}}}]},
    {"tools": "echo"},
    {"tools": [3]},
    {"tools": [{"name": "echo", "inputSchema": ECHO_SCHEMA}], "nextCursor": ["x"]},
])
def test_a_malformed_tools_list_affects_only_its_server(list_result: dict[str, Any]) -> None:
    """PR #20 review B2: whatever one server lists, connect() returns and
    every other server is connected as usual."""
    bad = FakeMcpServer()
    bad.list_result = list_result
    good = FakeMcpServer(url="http://127.0.0.1:8791/mcp")
    config = parse_config(json.dumps({"servers": {
        "bad": {"url": bad.url, "tools": {"echo": {"effect": "read"}}},
        "good": {"url": good.url, "tools": {"echo": {"effect": "read"}}},
    }}), {})
    connection = asyncio.run(connect(config, check_schema=check_tool_input_schema, transport=Router(bad, good)))
    statuses = {s.source: s for s in connection.statuses}
    assert statuses["good"].available and statuses["good"].tool_ids == ("good/echo",)
    assert [h.definition.tool_id for h in connection.handlers] in (["good/echo"], ["bad/echo", "good/echo"])
    if "bad/echo" not in [h.definition.tool_id for h in connection.handlers]:
        assert statuses["bad"].problems  # and it says why


def test_a_tools_list_is_bounded_in_total() -> None:
    """PR #20 review C1: pages under the per-response bound still add up."""
    server = FakeMcpServer([{"name": f"t{i}", "inputSchema": {"type": "object"}} for i in range(1001)],
                           page_size=100)
    connection = connected(server)
    assert not connection.statuses[0].available and "larger than" in connection.statuses[0].problems[0]


def test_a_notification_response_body_is_never_read() -> None:
    server = FakeMcpServer()
    connection = connected(server)
    assert connection.statuses[0].available and not server.notification_body_read


def test_a_write_answered_429_is_unknown_not_failed() -> None:
    """PR #20 review B1 end to end: a claimed write whose answer does not
    prove it did not run is never recorded as FAILED (which invites a retry)."""
    server = FakeMcpServer()
    server.next_status = 429
    rt, _ = mcp_runtime(server, [calls(("calliope/echo", {"text": "a"})), final("checked")], effect="scoped_write")
    result = asyncio.run(rt.driver.drive(SCOPE, user("go")))
    (execution,) = asyncio.run(rt.data.read_tool_executions(SCOPE, result.run_ids[0]))
    assert execution.status is ToolExecutionStatus.UNKNOWN


def test_turning_a_source_off_during_validation_denies_the_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR #20 review B3: the source is checked again after the (slow,
    isolated) validation, as the last step before the call runs."""
    from himari.tool import dispatcher as dispatcher_module

    server = FakeMcpServer()
    rt, _ = mcp_runtime(server, [calls(("calliope/echo", {"text": "a"})), final("ok")])

    async def validate_then_turn_off(value: Any, schema: Any, **_: Any) -> list[str]:
        await rt.data.set_external_setting(SCOPE, "calliope", False)
        return []

    monkeypatch.setattr(dispatcher_module, "validate_isolated", validate_then_turn_off)
    result = asyncio.run(rt.driver.drive(SCOPE, user("go")))
    (execution,) = asyncio.run(rt.data.read_tool_executions(SCOPE, result.run_ids[0]))
    assert execution.status is ToolExecutionStatus.DENIED and server.calls == []
    assert "not enabled" in execution.model_result.blocks[0].text


def test_a_too_deep_schema_from_a_server_leaves_out_only_that_tool() -> None:
    """PR #19 re-review N1 at the MCP boundary: refused, not a crash."""
    deep: dict[str, Any] = {"type": "object"}
    for _ in range(100):
        deep = {"type": "object", "properties": {"x": deep}}
    server = FakeMcpServer([{"name": "deep", "inputSchema": deep},
                            {"name": "echo", "inputSchema": ECHO_SCHEMA}])
    connection = connected(server, tools={"deep": {"effect": "read"}, "echo": {"effect": "read"}})
    (status,) = connection.statuses
    assert status.tool_ids == ("calliope/echo",)
    assert status.problems == ("deep: input schema refused ($: schema is nested more than 64 levels deep)",)


def test_a_result_nested_too_deep_to_parse_is_uncertain() -> None:
    server = FakeMcpServer()
    server.handlers["echo"] = lambda args: {"content": []}
    deep = "[" * 100_000 + "]" * 100_000

    async def scenario() -> None:
        client = http_client(URL, None, timeout_seconds=5, transport=server.transport())
        await client.initialize()
        original = server._answer
        server._answer = lambda request_id, result, headers=None: httpx.Response(  # type: ignore[method-assign]
            200, content=f'{{"jsonrpc": "2.0", "id": {request_id}, "result": {{"x": {deep}}}}}'.encode(),
            headers={"content-type": "application/json"})
        try:
            with pytest.raises(McpUncertain, match="usable JSON"):
                await client.call_tool("echo", {"text": "x"})
        finally:
            server._answer = original  # type: ignore[method-assign]
            await client.aclose()

    asyncio.run(scenario())


# --- PR #20 re-review: byte-bounded SSE, strict cursors ----------------------------------------


def answer_with(server: FakeMcpServer, make: Any) -> None:
    """From now on every answer is ``make(request_id)`` (after initialize)."""
    server._answer = lambda request_id, result, headers=None: make(request_id)  # type: ignore[method-assign]


def sse(*chunks: bytes) -> Any:
    def make(request_id: int) -> httpx.Response:
        async def body() -> Any:
            for chunk in chunks:
                yield chunk.replace(b"ID", str(request_id).encode())
        return httpx.Response(200, content=body(), headers={"content-type": "text/event-stream"})
    return make


async def first_call(server: FakeMcpServer, make: Any) -> Any:
    client = http_client(URL, None, timeout_seconds=5, transport=server.transport())
    await client.initialize()
    answer_with(server, make)
    try:
        return await client.call_tool("echo", {"text": "x"})
    finally:
        await client.aclose()


def test_a_long_sse_line_without_a_newline_stops_at_the_byte_bound() -> None:
    """N1: the bound is checked on arriving bytes, not after a line is
    complete; reading stops right after it."""
    sent: list[int] = []

    def make(request_id: int) -> httpx.Response:
        async def body() -> Any:
            yield b"data: "
            for i in range(64):  # 64 MiB with no newline
                sent.append(i)
                yield b"x" * (1024 * 1024)
        return httpx.Response(200, content=body(), headers={"content-type": "text/event-stream"})

    with pytest.raises(McpUncertain, match="too large"):
        asyncio.run(first_call(FakeMcpServer(), make))
    assert len(sent) <= 5


def test_the_bound_counts_utf8_bytes_not_characters() -> None:
    text = "한" * 1_500_000  # 1.5 M characters, 4.5 MB
    with pytest.raises(McpUncertain, match="too large"):
        asyncio.run(first_call(FakeMcpServer(), sse(f'data: {{"x": "{text}"}}\n\n'.encode())))


def test_sse_lines_split_anywhere_and_any_line_ending_are_parsed() -> None:
    message = '{"jsonrpc": "2.0", "id": ID, "result": {"content": [{"type": "text", "text": "체크메이트"}]}}'.encode()
    cut = message.index("체".encode()) + 1  # inside a multi-byte character
    for ending in (b"\n", b"\r\n", b"\r"):
        result = asyncio.run(first_call(FakeMcpServer(), sse(
            b": comment" + ending + b"event: message" + ending + b"data: " + message[:cut], message[cut:] + ending,
            b"\r" if ending == b"\r" else b"", ending)))
        assert result["content"][0]["text"] == "체크메이트", ending


def test_a_compressed_response_is_not_used() -> None:
    def make(request_id: int) -> httpx.Response:
        return httpx.Response(200, content=b"\x1f\x8b", headers={"content-type": "application/json",
                                                                 "content-encoding": "gzip"})
    with pytest.raises(McpUncertain, match="compressed"):
        asyncio.run(first_call(FakeMcpServer(), make))


def test_responses_are_asked_for_uncompressed() -> None:
    server = FakeMcpServer()
    connected(server)
    assert {r.headers["accept-encoding"] for r in server.requests} == {"identity"}


@pytest.mark.parametrize("cursor, available", [(["bad"], False), (3, False), (None, True), ("", True)])
def test_a_broken_page_cursor_is_an_error_not_the_end(cursor: Any, available: bool) -> None:
    """C2: a present but invalid nextCursor must not silently end the list."""
    server = FakeMcpServer()
    server.list_result = {"tools": [{"name": "echo", "inputSchema": ECHO_SCHEMA}], "nextCursor": cursor}
    (status,) = connected(server).statuses
    assert status.available is available
    if not available:
        assert "invalid nextCursor" in status.problems[0]


# --- PR #20 re-review N2/C3: a linear, standard SSE line splitter -----------------------------


class Chunks:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def aiter_bytes(self) -> Any:
        for chunk in self._chunks:
            yield chunk


def split_lines(chunks: list[bytes]) -> list[bytes]:
    async def collect() -> list[bytes]:
        return [line async for line in client_module._bounded_lines(Chunks(chunks))]  # type: ignore[arg-type]
    return asyncio.run(collect())


def reference_lines(data: bytes) -> list[bytes]:
    """The SSE rule on the whole input at once: CRLF, LF or CR end a line."""
    lines, start, i = [], 0, 0
    while i < len(data):
        if data[i : i + 1] in (b"\r", b"\n"):
            lines.append(data[start:i])
            i += 2 if data[i : i + 2] == b"\r\n" else 1
            start = i
        else:
            i += 1
    if start < len(data):
        lines.append(data[start:])
    return lines


def test_lines_are_the_same_however_the_stream_is_chunked() -> None:
    data = "data: 체크\r\ndata: a\rdata: b\n\r\n\r\r\nid: 1\n\ndata: tail\r".encode()
    expected = reference_lines(data)
    rng = random.Random(7)
    for _ in range(300):
        cuts = sorted(rng.sample(range(1, len(data)), rng.randint(0, len(data) - 1)))
        chunks = [data[a:b] for a, b in zip([0, *cuts], [*cuts, len(data)])]
        assert split_lines(chunks) == expected, chunks
    assert split_lines([bytes([b]) for b in data]) == expected  # one byte at a time


def test_one_byte_chunks_without_a_newline_cost_linear_time() -> None:
    """N2: was quadratic (every chunk searched the whole buffer again):
    about 40 s for this input before, well under a second now."""
    started = time.monotonic()
    assert split_lines([b"x"] * 1_000_000) == [b"x" * 1_000_000]
    assert time.monotonic() - started < 5


def test_the_byte_bound_holds_for_one_byte_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_module, "MAX_RESPONSE_BYTES", 1000)
    with pytest.raises(McpUncertain, match="too large"):
        split_lines([b"x"] * 1001)
    assert split_lines([b"x"] * 1000) == [b"x" * 1000]


@pytest.mark.parametrize("ending", [b"\r", b"\n", b"\r\n"])
def test_a_message_ended_by_cr_only_at_the_end_of_the_stream_is_used(ending: bytes) -> None:
    """C3: ``data: ...\\r\\r`` is a complete SSE event (CR-only lines)."""
    def make(request_id: int) -> httpx.Response:
        message = b'{"jsonrpc": "2.0", "id": %d, "result": {"content": [{"type": "text", "text": "ok"}]}}' % request_id
        async def body() -> Any:
            for byte in b"data: " + message + ending * 2:  # one byte at a time
                yield bytes([byte])
        return httpx.Response(200, content=body(), headers={"content-type": "text/event-stream"})

    result = asyncio.run(first_call(FakeMcpServer(), make))
    assert result["content"][0]["text"] == "ok"
