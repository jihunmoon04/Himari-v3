"""V3-P60 part 3: a server's resources as two Tools (§5) — the lists, which
URIs may be read (refusals win), per-session lists and reads (B6, C6),
per-entry checks (B5, C7), pages and versions, and how they are offered."""

from __future__ import annotations

import asyncio
import base64
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
from himari.common import ConversationId, PrincipalId, ToolId, TrustedScope, thaw
from himari.control.api import DriveState
from himari.data.api import ToolExecutionStatus
from himari.mcp import resources as resources_module
from himari.mcp.config import parse_config
from himari.mcp.resources import OMITTED, PAGE_CHARS, ResourceTool, loose_pattern, strict_pattern
from himari.mcp.stdio import RESTART_NOTICE
from himari.mcp.tools import McpConnection, connect
from himari.provider.scripted import ScriptedAdapter
from himari.tool.api import EffectProfile, ToolContext, ToolInvocation, ToolOutcome, ToolStatus
from himari.tool.schema import check_tool_input_schema

GLOSSARY = "calliope://glossary/ko"
SECRET = "calliope://private/notes"


def text_entry(uri: str, text: str, **extra: Any) -> dict[str, Any]:
    return {"uri": uri, "mimeType": "text/plain", "text": text, **extra}


def answer(*entries: dict[str, Any]) -> dict[str, Any]:
    return {"contents": list(entries)}


def config_of(entry: dict[str, Any]) -> Any:
    return parse_config(json.dumps({"servers": {"calliope": entry}}), {})


async def open_http(server: FakeMcpServer, declared: tuple[str, ...] = ("tools", "resources"),
                    **changes: Any) -> McpConnection:
    server.capabilities = {name: {} for name in declared}
    entry = {"url": URL, "tools": {"echo": {"effect": "read"}}, **changes}
    return await connect(config_of(entry), check_schema=check_tool_input_schema, transport=server.transport())


async def open_stdio(launcher: FakeLauncher, **changes: Any) -> McpConnection:
    launcher.capabilities = {"tools": {}, "resources": {}}
    entry = {"command": "/usr/bin/fake-server", "timeout_seconds": 5, "tools": {"echo": {"effect": "read"}}, **changes}
    return await connect(config_of(entry), check_schema=check_tool_input_schema,
                         launcher_for=lambda source, stdio: launcher)


def tool(connection: McpConnection, name: str) -> ResourceTool:
    return next(t for t in connection.resource_tools if t.definition.tool_id == f"calliope/{name}")


async def use(connection: McpConnection, name: str, conversation: str = "conv-a", **arguments: Any) -> ToolOutcome:
    ctx = ToolContext(scope=TrustedScope(PrincipalId("alice"), ConversationId(conversation)),
                      artifacts=None)  # type: ignore[arg-type]
    return await tool(connection, name).run(ToolInvocation(ToolId(f"calliope/{name}"), "c", arguments), ctx)  # type: ignore[arg-type]


def raw(outcome: ToolOutcome) -> dict[str, Any]:
    record = thaw(outcome.raw_result)
    assert isinstance(record, dict)
    return record


def text_of(outcome: ToolOutcome) -> str:
    return outcome.model_result.blocks[0].text  # type: ignore[union-attr]


def run(scenario: Callable[[], Awaitable[None]]) -> None:
    asyncio.run(asyncio.wait_for(scenario(), 30))


def listing(outcome: ToolOutcome) -> dict[str, Any]:
    assert outcome.status is ToolStatus.SUCCEEDED, text_of(outcome)
    return json.loads(text_of(outcome))  # type: ignore[no-any-return]


# --- URI templates ----------------------------------------------------------------------------


def test_templates_match_the_uris_they_describe() -> None:
    simple = strict_pattern("calliope://games/{game}/plies/{ply}")
    assert simple is not None and simple.fullmatch("calliope://games/g1/plies/10")
    assert not simple.fullmatch("calliope://games/g1/../../x/plies/10")  # a simple value has no "/"
    reserved = strict_pattern("file:///{+path}")
    assert reserved is not None and reserved.fullmatch("file:///a/b/c.txt")
    query = strict_pattern("search://q{?term,page}")
    assert query is not None and query.fullmatch("search://q?term=e4&page=2") and query.fullmatch("search://q")
    assert strict_pattern("broken://{unclosed") is None and strict_pattern("bad://{}") is None
    assert loose_pattern("calliope://private/{id}").fullmatch("calliope://private/a/b?c")
    assert loose_pattern("broken://{unclosed").fullmatch("broken://anything")


# --- listing -----------------------------------------------------------------------------------


def test_the_list_joins_resources_and_templates_without_views_or_user_only_ones() -> None:
    server = FakeMcpServer()
    server.listed = [
        {"uri": GLOSSARY, "name": "glossary", "mimeType": "text/markdown", "size": 1000},
        {"uri": "ui://calliope/viewer.html", "name": "viewer", "mimeType": "text/html;profile=mcp-app"},
        {"uri": "calliope://view", "name": "also a view", "mimeType": "text/html; profile=mcp-app"},
        {"uri": SECRET, "name": "secret", "annotations": {"audience": ["user"]}},
        {"uri": "calliope://both", "name": "both", "annotations": {"audience": ["user", "assistant"]}},
        "not an entry",
    ]
    server.templates = [
        {"uriTemplate": "calliope://games/{game}", "name": "game"},
        {"uriTemplate": "calliope://private/{id}", "name": "private", "annotations": {"audience": ["user"]}},
    ]

    async def scenario() -> None:
        connection = await open_http(server)
        listed = listing(await use(connection, "list_resources"))
        assert [r["uri"] for r in listed["resources"]] == [GLOSSARY, "calliope://both"]
        assert listed["resources"][0] == {"uri": GLOSSARY, "name": "glossary", "mimeType": "text/markdown",
                                          "size": 1000}
        assert listed["templates"] == [{"uriTemplate": "calliope://games/{game}", "name": "game"}]
        assert "next_cursor" not in listed
        assert server.methods == ["tools/list", "resources/templates/list", "resources/list"]
        await connection.aclose()

    run(scenario)


def test_a_long_list_is_given_in_parts_with_himaris_cursor() -> None:
    server = FakeMcpServer()
    server.listed = [{"uri": f"calliope://r/{i}", "name": f"r{i}", "description": "d" * 400} for i in range(200)]
    server.list_page_size = 30  # the server's own pages are read whole

    async def scenario() -> None:
        connection = await open_http(server)
        seen: list[str] = []
        cursor: str | None = None
        for _ in range(10):
            outcome = await use(connection, "list_resources", **({} if cursor is None else {"cursor": cursor}))
            assert len(text_of(outcome)) <= 32_000
            part = listing(outcome)
            seen += [r["uri"] for r in part["resources"]]
            cursor = part.get("next_cursor")
            if cursor is None:
                break
        assert seen == [f"calliope://r/{i}" for i in range(200)]
        assert server.methods.count("resources/list") == 7  # one listing (cached), seven server pages
        bad = await use(connection, "list_resources", cursor="999")
        assert bad.status is ToolStatus.FAILED and "cursor" in text_of(bad)
        await connection.aclose()

    run(scenario)


def test_a_list_past_the_bound_is_cut_and_then_only_listed_uris_are_read() -> None:
    server = FakeMcpServer()
    server.listed = [{"uri": f"calliope://r/{i}", "name": "r"} for i in range(600)]
    server.templates = [{"uriTemplate": "calliope://games/{game}", "name": "game"}]
    server.resources = {"calliope://r/1": answer(text_entry("calliope://r/1", "one"))}

    async def scenario() -> None:
        connection = await open_http(server)
        part = listing(await use(connection, "list_resources"))
        assert "more than HIMARI reads" in part["note"]
        assert (await use(connection, "read_resource", uri="calliope://r/1")).status is ToolStatus.SUCCEEDED
        refused = await use(connection, "read_resource", uri="calliope://games/g1")
        assert refused.status is ToolStatus.FAILED and "only URIs in the part read" in text_of(refused)
        await connection.aclose()

    run(scenario)


# --- which URIs ----------------------------------------------------------------------------------


def test_listed_and_template_uris_are_read_others_refused_unless_any() -> None:
    server = FakeMcpServer()
    server.listed = [{"uri": GLOSSARY, "name": "glossary"}]
    server.templates = [{"uriTemplate": "calliope://games/{game}", "name": "game"}]
    server.resources = {GLOSSARY: answer(text_entry(GLOSSARY, "glossary text")),
                        "calliope://games/g1": answer(text_entry("calliope://games/g1", "game g1")),
                        "calliope://other": answer(text_entry("calliope://other", "other"))}

    async def scenario() -> None:
        connection = await open_http(server)
        assert "glossary text" in text_of(await use(connection, "read_resource", uri=GLOSSARY))
        assert "game g1" in text_of(await use(connection, "read_resource", uri="calliope://games/g1"))
        refused = await use(connection, "read_resource", uri="calliope://other")
        assert refused.status is ToolStatus.FAILED and "neither lists it" in text_of(refused)
        assert server.resource_reads == [GLOSSARY, "calliope://games/g1"]  # the refused one was never asked for
        await connection.aclose()
        wide = await open_http(server, resources={"uris": "any"})
        assert "other" in text_of(await use(wide, "read_resource", uri="calliope://other"))
        await wide.aclose()

    run(scenario)


def test_refusals_win_over_matches() -> None:
    server = FakeMcpServer()
    server.listed = [{"uri": "calliope://games/secret", "name": "s", "annotations": {"audience": ["user"]}},
                     {"uri": "ui://calliope/viewer.html", "name": "viewer"}]
    server.templates = [
        {"uriTemplate": "calliope://games/{game}", "name": "game"},
        {"uriTemplate": "calliope://games/private-{id}", "name": "private", "annotations": {"audience": ["user"]}},
    ]
    server.resources = {uri: answer(text_entry(uri, "x")) for uri in
                        ("calliope://games/secret", "calliope://games/private-1", "calliope://games/g1",
                         "ui://calliope/viewer.html", "ui://calliope/other")}

    async def scenario() -> None:
        for uris in ("listed", "any"):
            connection = await open_http(server, resources={"uris": uris})
            for uri in ("calliope://games/secret",  # listed for the user, though a template matches
                        "calliope://games/private-1",  # a user template matches, and an allowing one too
                        "ui://calliope/viewer.html", "ui://calliope/other"):  # views, listed or not
                outcome = await use(connection, "read_resource", uri=uri)
                assert outcome.status is ToolStatus.FAILED, uri
            assert (await use(connection, "read_resource", uri="calliope://games/g1")).status is ToolStatus.SUCCEEDED
            await connection.aclose()
        assert server.resource_reads == ["calliope://games/g1", "calliope://games/g1"]

    run(scenario)


def test_a_server_without_templates_still_lists_its_resources() -> None:
    server = FakeMcpServer()
    server.listed = [{"uri": GLOSSARY, "name": "glossary"}]
    server.templates = None  # resources/templates/list is an unknown method

    async def scenario() -> None:
        connection = await open_http(server)
        assert listing(await use(connection, "list_resources"))["templates"] == []
        await connection.aclose()

    run(scenario)


# --- reading -----------------------------------------------------------------------------------------


def test_restricted_entries_are_left_out_and_named_only_in_the_record() -> None:
    server = FakeMcpServer()
    server.listed = [{"uri": GLOSSARY, "name": "glossary"},
                     {"uri": "calliope://listed-for-user", "name": "u", "annotations": {"audience": ["user"]}}]
    server.resources = {GLOSSARY: answer(
        text_entry(GLOSSARY, "allowed text"),
        text_entry(SECRET, "not listed"),
        {"uri": "ui://calliope/viewer.html", "mimeType": "text/html;profile=mcp-app", "text": "<html>"},
        text_entry("calliope://ann", "for the user", annotations={"audience": ["user"]}),
        text_entry(GLOSSARY, "same uri, but for the user", annotations={"audience": ["user"]}),
        text_entry("calliope://listed-for-user", "the list says user; the entry says nothing"),
        {"uri": GLOSSARY, "mimeType": "text/plain"},  # neither text nor blob
        {"mimeType": "application/octet-stream", "blob": base64.b64encode(b"\x00" * 10).decode()},
    )}

    async def scenario() -> None:
        connection = await open_http(server)
        outcome = await use(connection, "read_resource", uri=GLOSSARY)
        text = text_of(outcome)
        assert outcome.status is ToolStatus.SUCCEEDED
        assert f"[resource {GLOSSARY} · text/plain]\nallowed text" in text
        assert f"[resource {GLOSSARY} · application/octet-stream]\n[binary, 10 bytes]" in text
        assert text.count(OMITTED) == 6
        for hidden in (SECRET, "ui://", "calliope://ann", "listed-for-user", "not listed", "for the user"):
            assert hidden not in text, hidden
        entries = raw(outcome)["entries"]
        omitted = [e for e in entries if "omitted" in e]
        assert [e["uri"] for e in omitted] == [
            SECRET, "ui://calliope/viewer.html", "calliope://ann", GLOSSARY, "calliope://listed-for-user", GLOSSARY]
        await connection.aclose()

    run(scenario)


def test_pages_offsets_and_versions() -> None:
    server = FakeMcpServer()
    server.listed = [{"uri": GLOSSARY, "name": "glossary"}]
    body = "".join(f"{i:05d}\n" for i in range(12_000))  # 72,000 characters
    server.resources = {GLOSSARY: answer(text_entry(GLOSSARY, body))}

    async def scenario() -> None:
        connection = await open_http(server)
        pages: list[str] = []
        offset, versions = 0, set()
        while True:
            outcome = await use(connection, "read_resource", uri=GLOSSARY, offset=offset)
            head, _, chunk = text_of(outcome).partition("\n\n")
            assert len(chunk) <= PAGE_CHARS
            pages.append(chunk)
            versions.add(raw(outcome)["version"])
            if "end of the resource" in head:
                break
            offset = raw(outcome)["next_offset"]
            assert f"next_offset {offset}" in head
        assert len(pages) == 3 and len(versions) == 1
        whole = "".join(pages)
        assert whole == f"[resource {GLOSSARY} · text/plain]\n{body}"
        server.resources[GLOSSARY] = answer(text_entry(GLOSSARY, body + "changed"))
        changed = await use(connection, "read_resource", uri=GLOSSARY, offset=PAGE_CHARS)
        assert raw(changed)["version"] not in versions
        past = await use(connection, "read_resource", uri=GLOSSARY, offset=10_000_000)
        assert past.status is ToolStatus.FAILED and "past the end" in text_of(past)
        await connection.aclose()

    run(scenario)


def test_a_missing_resource_is_a_definite_failure() -> None:
    server = FakeMcpServer()
    server.listed = [{"uri": GLOSSARY, "name": "glossary"}]
    server.missing_resource_code = -32002

    async def scenario() -> None:
        connection = await open_http(server)
        outcome = await use(connection, "read_resource", uri=GLOSSARY)
        assert outcome.status is ToolStatus.FAILED and "no such resource" in text_of(outcome)
        await connection.aclose()

    run(scenario)


# --- per session ------------------------------------------------------------------------------------


def test_the_lists_are_cached_per_session_for_a_while(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeMcpServer()
    server.listed = [{"uri": GLOSSARY, "name": "glossary"}]
    server.resources = {GLOSSARY: answer(text_entry(GLOSSARY, "g"))}

    async def scenario() -> None:
        connection = await open_http(server)
        await use(connection, "list_resources")
        await use(connection, "read_resource", uri=GLOSSARY)
        assert server.methods.count("resources/list") == 1
        monkeypatch.setattr(resources_module, "CACHE_SECONDS", 0.0)
        await use(connection, "read_resource", uri=GLOSSARY)
        assert server.methods.count("resources/list") == 2
        await connection.aclose()

    run(scenario)


def test_http_a_session_renewed_at_the_read_is_judged_again_on_the_new_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """C6: judged on session 1 (cached), the read meets 404; the URI is not
    read on session 2 until session 2's list allows it — here it does not."""
    server = FakeMcpServer()
    server.listed = [{"uri": GLOSSARY, "name": "glossary"}]
    server.resources = {GLOSSARY: answer(text_entry(GLOSSARY, "g"))}

    async def scenario() -> None:
        connection = await open_http(server)
        await use(connection, "list_resources")  # session 1's list
        server.forget_sessions()
        server.listed = []  # the new session lists nothing
        outcome = await use(connection, "read_resource", uri=GLOSSARY)
        assert outcome.status is ToolStatus.FAILED and "neither lists it" in text_of(outcome)
        assert server.resource_reads == []  # 404 on session 1; never asked on session 2
        assert server.initializations == 2  # the listing session and the renewed one (calls share the first)
        server.listed = [{"uri": GLOSSARY, "name": "glossary"}]
        server.forget_sessions()
        monkeypatch.setattr(resources_module, "CACHE_SECONDS", 0.0)  # session 2's empty list is not used again
        again = await use(connection, "read_resource", uri=GLOSSARY)  # its list meets 404: session 3 lists it
        assert again.status is ToolStatus.SUCCEEDED and server.resource_reads == [GLOSSARY]
        await connection.aclose()

    run(scenario)


def test_stdio_a_process_replaced_between_list_and_read_is_judged_again() -> None:
    """C6 on stdio: the process that was judged ends before the read; the
    read is not sent to the new process until its own list allows it."""
    launcher = FakeLauncher()
    launcher.listed_for = lambda index: [{"uri": GLOSSARY, "name": "g"}] if index == 1 else []
    launcher.resources = {GLOSSARY: answer(text_entry(GLOSSARY, "g"))}
    launcher.after_method["resources/list"] = lambda process: process.crash()  # once: process 1, right after its list

    async def scenario() -> None:
        connection = await open_stdio(launcher)
        outcome = await use(connection, "read_resource", uri=GLOSSARY)
        assert outcome.status is ToolStatus.FAILED and "neither lists it" in text_of(outcome)
        assert len(launcher.processes) == 3  # listing, the judged one, its replacement
        assert "resources/read" not in launcher.lines(1) + launcher.lines(2)
        await connection.aclose()

    run(scenario)


def test_stdio_after_a_replacement_the_new_process_is_read_and_says_so() -> None:
    launcher = FakeLauncher()
    launcher.listed = [{"uri": GLOSSARY, "name": "g"}]
    launcher.resources = {GLOSSARY: answer(text_entry(GLOSSARY, "g"))}

    async def scenario() -> None:
        connection = await open_stdio(launcher)
        assert (await use(connection, "read_resource", uri=GLOSSARY)).status is ToolStatus.SUCCEEDED
        launcher.processes[1].crash()
        await asyncio.sleep(0.05)
        outcome = await use(connection, "read_resource", uri=GLOSSARY)
        assert outcome.status is ToolStatus.SUCCEEDED and text_of(outcome).startswith(RESTART_NOTICE)
        assert launcher.lines(2).count("resources/list") == 1  # judged on its own list
        await connection.aclose()

    run(scenario)


def test_per_conversation_a_uri_one_process_lists_is_not_read_by_another_conversation() -> None:
    launcher = FakeLauncher()
    launcher.listed_for = lambda index: [{"uri": GLOSSARY, "name": "g"}] if index == 1 else []
    launcher.resources = {GLOSSARY: answer(text_entry(GLOSSARY, "g"))}

    async def scenario() -> None:
        connection = await open_stdio(launcher, processes={"scope": "per_conversation", "max": 4})
        assert (await use(connection, "read_resource", "conv-a", uri=GLOSSARY)).status is ToolStatus.SUCCEEDED
        refused = await use(connection, "read_resource", "conv-b", uri=GLOSSARY)
        assert refused.status is ToolStatus.FAILED
        assert "resources/read" not in launcher.lines(2)
        await connection.aclose()

    run(scenario)


# --- how they are offered ----------------------------------------------------------------------------


def test_effect_is_unknown_by_default_and_the_operators_otherwise() -> None:
    server = FakeMcpServer()

    async def scenario() -> None:
        connection = await open_http(server)
        assert {t.definition.effect_profile for t in connection.resource_tools} == {EffectProfile.UNKNOWN}
        assert connection.statuses[0].tool_ids == ("calliope/echo", "calliope/list_resources",
                                                   "calliope/read_resource")
        await connection.aclose()
        read = await open_http(server, resources={"effect": "read"})
        assert {t.definition.effect_profile for t in read.resource_tools} == {EffectProfile.READ}
        await read.aclose()
        off = await open_http(server, resources=False)
        assert off.resource_tools == [] and off.statuses[0].tool_ids == ("calliope/echo",)
        await off.aclose()
        undeclared = await open_http(server, declared=("tools",))  # the server offers no resources
        assert undeclared.resource_tools == [] and "resources/list" not in server.methods
        await undeclared.aclose()

    run(scenario)


def test_a_server_tool_of_the_same_name_wins() -> None:
    server = FakeMcpServer(tools=[{"name": "read_resource", "description": "its own",
                                   "inputSchema": {"type": "object"}}])

    async def scenario() -> None:
        connection = await open_http(server, tools={"read_resource": {"effect": "read"}})
        (status,) = connection.statuses
        assert status.tool_ids == ("calliope/read_resource", "calliope/list_resources")
        assert [t.definition.tool_id for t in connection.resource_tools] == ["calliope/list_resources"]
        assert "read_resource: the server's own Tool of that name is offered instead" in status.problems
        await connection.aclose()

    run(scenario)


def test_through_the_application_only_where_the_server_is_on(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    (root / "mcp.json").write_text(json.dumps({"servers": {"calliope": {"url": URL}}}))
    (root / "mcp.json").chmod(0o600)
    server = FakeMcpServer()
    server.capabilities = {"resources": {}}  # a server with resources and no Tools
    server.listed = [{"uri": GLOSSARY, "name": "glossary"}]
    server.resources = {GLOSSARY: answer(text_entry(GLOSSARY, "용어집"))}
    script = ScriptedAdapter([calls(("calliope/read_resource", {"uri": GLOSSARY})), final("read"),
                              calls(("calliope/read_resource", {"uri": GLOSSARY})), final("not read")])

    async def scenario() -> None:
        app = await Application.open(root, script, model_id="scripted-model", sandbox=False,
                                     mcp_transport=server.transport())
        try:
            scope = TrustedScope(OPERATOR, await app.create_conversation(OPERATOR))
            await app.set_tool_source(scope, "calliope", True)
            result = await app.runtime.driver.drive(scope, user("read it"))
            assert result.state is DriveState.COMPLETED
            (execution,) = await app.data.read_tool_executions(scope, result.run_ids[0])
            assert execution.status is ToolExecutionStatus.SUCCEEDED
            assert "용어집" in execution.model_result.blocks[0].text  # type: ignore[union-attr]
            assert "calliope/read_resource" in [d.tool_id for d in script.prepared_inputs[0].tool_catalog]
            await app.set_tool_source(scope, "calliope", False)
            result = await app.runtime.driver.drive(scope, user("again"))
            assert "calliope/read_resource" not in [d.tool_id for d in script.prepared_inputs[-1].tool_catalog]
            (execution,) = await app.data.read_tool_executions(scope, result.run_ids[0])
            assert execution.status is not ToolExecutionStatus.SUCCEEDED  # the Dispatcher refuses it
            assert server.resource_reads == [GLOSSARY]
        finally:
            await app.close()

    run(scenario)
