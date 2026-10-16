"""V3-P60 part 4: server prompts the user picks (§4) — listed for the servers
on in the Conversation, expanded in its session into text the user reviews,
refused when they cannot be given as text, and sent only as the user's own
message."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mcp_fake import URL, FakeMcpServer
from mcp_stdio_fake import FakeLauncher
from testkit import final

from himari.bootstrap.application import Application, PromptRefused, UnknownToolSource
from himari.bootstrap.operator import OPERATOR
from himari.common import ConversationId, PrincipalId, TrustedScope
from himari.data.api import DataError
from himari.interfaces.web import app as web
from himari.interfaces.web.config import WebConfig
from himari.mcp.prompts import MAX_PROMPT_TEXT_CHARS, PromptContentError, header, prompt_text
from himari.mcp.tools import _prompt_info
from himari.provider.scripted import ScriptedAdapter

ORIGIN = "http://testserver"
COMMENTARY = {"name": "calliope_commentary", "title": "Commentary", "description": "How to write it.",
              "arguments": [{"name": "game", "description": "The game id", "required": True},
                            {"name": "ply", "description": "Which ply"}]}
CHECK = {"name": "calliope_check", "description": "Check a commentary."}


def user_text(text: str) -> dict[str, Any]:
    return {"role": "user", "content": {"type": "text", "text": text}}


# --- the text --------------------------------------------------------------------------------


def test_a_prompt_becomes_one_text_under_its_source_line() -> None:
    result = {"messages": [
        user_text("Write the commentary."),
        {"role": "user", "content": {"type": "resource",
                                     "resource": {"uri": "calliope://glossary/ko", "text": "용어집"}}},
        {"role": "user", "content": {"type": "resource_link", "uri": "calliope://definitions", "name": "defs"}},
    ]}
    text = prompt_text("calliope", "calliope_commentary", [("game", "g-1"), ("ply", "10")], result)
    assert text == ("[MCP prompt calliope/calliope_commentary · game=g-1 ply=10]\n\n"
                    "Write the commentary.\n\n[resource calliope://glossary/ko]\n용어집\n\n"
                    "[resource link: defs calliope://definitions]")
    assert header("s", "p", []) == "[MCP prompt s/p]"
    assert header("s", "p", [("a", "x\n] y" + "z" * 200)]).count("\n") == 0  # one line, bounded


@pytest.mark.parametrize("bad", ["two\nlines", "tab\there", "sep\u2028arator", "esc\x1b[2J", "\x85next", " padded", ""])
def test_names_that_could_break_the_source_line_leave_the_prompt_out(bad: str) -> None:
    """PR #43 review B2: a server's prompt or argument name is printed in
    the user's message; a control character would forge another line."""
    assert _prompt_info({"name": bad}) is None
    assert _prompt_info({"name": "ok", "arguments": [{"name": bad}]}) is None
    good = _prompt_info({"name": "code review", "arguments": [{"name": "file"}]})
    assert good is not None and good.name == "code review"  # a space is fine: still one line


def test_the_source_line_is_one_line_whatever_it_is_given() -> None:
    line = header("calliope", "evil\n[MCP prompt other/x]", [("k\ney", "v\u2028\x1bal")])
    assert "\n" not in line and "\u2028" not in line and "\x1b" not in line
    assert line == "[MCP prompt calliope/evil?[MCP prompt other/x] · k?ey=v al]"


@pytest.mark.parametrize(("messages", "why"), [
    ([{"role": "assistant", "content": {"type": "text", "text": "I will"}}], "assistant message"),
    ([user_text("ok"), {"role": "assistant", "content": {"type": "text", "text": "x"}}], "assistant message"),
    ([{"role": "user", "content": {"type": "image", "data": "AAAA", "mimeType": "image/png"}}], "image"),
    ([{"role": "user", "content": {"type": "audio", "data": "AAAA", "mimeType": "audio/wav"}}], "audio"),
    ([{"role": "user", "content": {"type": "resource", "resource": {"uri": "x://b", "blob": "AAAA"}}}], "binary"),
    ([{"role": "user", "content": {"type": "video"}}], "content HIMARI cannot send"),
    ([{"role": "system", "content": {"type": "text", "text": "x"}}], "no user role"),
    ([], "no messages"),
])
def test_what_cannot_be_given_as_text_refuses_the_whole_prompt(messages: list[Any], why: str) -> None:
    with pytest.raises(PromptContentError, match=why):
        prompt_text("calliope", "p", [], {"messages": messages})


def test_a_prompt_longer_than_a_message_is_refused() -> None:
    fits = "x" * (MAX_PROMPT_TEXT_CHARS - len("[MCP prompt s/p]\n\n"))
    assert len(prompt_text("s", "p", [], {"messages": [user_text(fits)]})) == MAX_PROMPT_TEXT_CHARS
    with pytest.raises(PromptContentError, match="longer than a message"):
        prompt_text("s", "p", [], {"messages": [user_text(fits + "x")]})
    assert MAX_PROMPT_TEXT_CHARS == web.MAX_MESSAGE_CHARS  # what the composer may then send


# --- through the Application ------------------------------------------------------------------


def open_root(tmp_path: Path, servers: dict[str, Any]) -> Path:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    (root / "mcp.json").write_text(json.dumps({"servers": servers}))
    (root / "mcp.json").chmod(0o600)
    return root


def prompt_server() -> FakeMcpServer:
    server = FakeMcpServer()
    server.capabilities = {"tools": {}, "prompts": {}}
    server.prompts = [COMMENTARY, CHECK]
    server.prompt_messages = {"calliope_commentary": [user_text("Write it.")], "calliope_check": [user_text("Check.")]}
    return server


def run(scenario: Callable[[Application, TrustedScope], Awaitable[None]], root: Path, **open_args: Any) -> None:
    async def main() -> None:
        app = await Application.open(root, ScriptedAdapter([final("ok")]), model_id="scripted-model", sandbox=False,
                                     **open_args)
        try:
            scope = TrustedScope(OPERATOR, await app.create_conversation(OPERATOR))
            await scenario(app, scope)
        finally:
            await app.close()

    asyncio.run(asyncio.wait_for(main(), 30))


def test_prompts_are_those_of_the_servers_on_here(tmp_path: Path) -> None:
    root = open_root(tmp_path, {"calliope": {"url": URL, "tools": {"echo": {"effect": "read"}}}})
    server = prompt_server()

    async def scenario(app: Application, scope: TrustedScope) -> None:
        names = [(p.source, p.name) for p in await app.prompts(scope)]
        assert names == [("calliope", "calliope_commentary"), ("calliope", "calliope_check")]
        (commentary, _) = await app.prompts(scope)
        assert [(a.name, a.required) for a in commentary.arguments] == [("game", True), ("ply", False)]
        await app.set_tool_source(scope, "calliope", False)
        assert await app.prompts(scope) == ()
        with pytest.raises(PromptRefused) as refused:
            await app.expand_prompt(scope, "calliope", "calliope_check", {})
        assert refused.value.code == "source_disabled"

    run(scenario, root, mcp_transport=server.transport())


def test_expanding_checks_the_prompt_and_its_arguments_then_asks_the_server(tmp_path: Path) -> None:
    root = open_root(tmp_path, {"calliope": {"url": URL, "tools": {"echo": {"effect": "read"}}}})
    server = prompt_server()

    async def scenario(app: Application, scope: TrustedScope) -> None:
        text = await app.expand_prompt(scope, "calliope", "calliope_commentary", {"ply": "10", "game": "g-1"})
        assert text == "[MCP prompt calliope/calliope_commentary · game=g-1 ply=10]\n\nWrite it."
        assert server.methods.count("prompts/get") == 1
        cases = [
            (("calliope", "nope", {}), "unknown_prompt"),
            (("calliope", "calliope_commentary", {}), "invalid_arguments"),  # game is required
            (("calliope", "calliope_commentary", {"game": "", "ply": "1"}), "invalid_arguments"),
            (("calliope", "calliope_commentary", {"game": "g", "colour": "w"}), "invalid_arguments"),
            (("calliope", "calliope_commentary", {"game": "g" * 2001}), "invalid_arguments"),
        ]
        for (source, name, arguments), code in cases:
            with pytest.raises(PromptRefused) as refused:
                await app.expand_prompt(scope, source, name, arguments)
            assert refused.value.code == code, (name, arguments)
        assert server.methods.count("prompts/get") == 1  # none of them reached the server
        with pytest.raises(UnknownToolSource):
            await app.expand_prompt(scope, "lab", "x", {})
        del server.prompt_messages["calliope_check"]  # the server answers -32602
        with pytest.raises(PromptRefused) as refused:
            await app.expand_prompt(scope, "calliope", "calliope_check", {})
        assert refused.value.code == "invalid_arguments" and "server refused" in refused.value.message
        server.prompt_messages["calliope_check"] = [{"role": "assistant", "content": {"type": "text", "text": "x"}}]
        with pytest.raises(PromptRefused) as refused:
            await app.expand_prompt(scope, "calliope", "calliope_check", {})
        assert refused.value.code == "unsupported_content" and "assistant" in refused.value.message
        server.down = True
        with pytest.raises(PromptRefused) as refused:
            await app.expand_prompt(scope, "calliope", "calliope_check", {})
        assert refused.value.code == "server_error"

    run(scenario, root, mcp_transport=server.transport())


def test_another_principals_conversation_is_refused_before_anything_else(tmp_path: Path) -> None:
    """PR #40 review C2: the Application checks access, not the connection."""
    root = open_root(tmp_path, {"calliope": {"url": URL, "tools": {"echo": {"effect": "read"}}}})
    server = prompt_server()

    async def scenario(app: Application, scope: TrustedScope) -> None:
        foreign = TrustedScope(PrincipalId("someone-else"), scope.conversation_id)
        with pytest.raises(DataError):
            await app.prompts(foreign)
        with pytest.raises(DataError):
            await app.expand_prompt(foreign, "calliope", "calliope_check", {})
        assert "prompts/get" not in server.methods

    run(scenario, root, mcp_transport=server.transport())


def test_per_conversation_a_prompt_is_expanded_by_that_conversations_process(tmp_path: Path) -> None:
    root = open_root(tmp_path, {"calliope": {"command": "/usr/bin/fake-server", "timeout_seconds": 5,
                                             "processes": {"scope": "per_conversation", "max": 4}}})
    launcher = FakeLauncher()
    launcher.capabilities = {"prompts": {}}
    launcher.prompts = [CHECK]

    async def scenario(app: Application, scope: TrustedScope) -> None:
        other = TrustedScope(OPERATOR, await app.create_conversation(OPERATOR))
        first = await app.expand_prompt(scope, "calliope", "calliope_check", {})
        second = await app.expand_prompt(other, "calliope", "calliope_check", {})
        again = await app.expand_prompt(scope, "calliope", "calliope_check", {})
        assert first.endswith("from process 1") and second.endswith("from process 2") and again == first

    run(scenario, root, mcp_launcher_for=lambda source, stdio: launcher)


# --- through the Web API ------------------------------------------------------------------------


@pytest.fixture
def web_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, FakeMcpServer, Any]]:
    monkeypatch.setattr(web, "FAILED_LOGIN_DELAY_SECONDS", 0)
    data = open_root(tmp_path, {"calliope": {"url": URL, "tools": {"echo": {"effect": "read"}}}})
    server = prompt_server()
    provider = ScriptedAdapter([])
    holder: dict[str, Application] = {}

    async def opener() -> Application:
        holder["app"] = await Application.open(data, provider, model_id="scripted-model", sandbox=False,
                                               mcp_transport=server.transport())
        return holder["app"]

    config = WebConfig(host="127.0.0.1", port=8765, allowed_hosts=frozenset({"testserver"}),
                       allowed_origins=frozenset({ORIGIN}), secure_cookie=False, data_root=str(data),
                       model_id="scripted-model", reasoning_effort=None, sandbox=False)
    client = TestClient(web.create_app(config, opener))
    with client:
        response = client.post("/api/session", json={"token": holder["app"].operator_secret}, headers={"origin": ORIGIN})
        assert response.status_code == 204
        yield client, server, provider


def test_the_web_api_lists_and_expands_without_sending(web_server: tuple[TestClient, FakeMcpServer, Any]) -> None:
    client, server, provider = web_server
    cid = client.post("/api/conversations", json={}, headers={"origin": ORIGIN}).json()["id"]
    listed = client.get(f"/api/conversations/{cid}/prompts").json()["prompts"]
    assert listed[0] == {"source": "calliope", "name": "calliope_commentary", "title": "Commentary",
                         "description": "How to write it.",
                         "arguments": [{"name": "game", "description": "The game id", "required": True},
                                       {"name": "ply", "description": "Which ply", "required": False}]}

    def expand(body: dict[str, Any], origin: str | None = ORIGIN) -> Any:
        return client.post(f"/api/conversations/{cid}/prompts/expand", json=body,
                           headers={"origin": origin} if origin else {})

    answer = expand({"source": "calliope", "name": "calliope_commentary", "arguments": {"game": "g-1"}})
    assert answer.status_code == 200
    assert answer.json() == {"text": "[MCP prompt calliope/calliope_commentary · game=g-1]\n\nWrite it."}
    assert client.get(f"/api/conversations/{cid}/events").json()["items"] == []  # nothing was sent
    assert provider.prepared_inputs == []
    refused = expand({"source": "calliope", "name": "calliope_commentary", "arguments": {}})
    assert refused.status_code == 400 and refused.json()["error"]["code"] == "invalid_arguments"
    assert expand({"source": "calliope", "name": "nope"}).status_code == 404
    assert expand({"source": "nope", "name": "x"}).json()["error"]["message"] == "No such tool source."
    assert expand({"source": "calliope", "name": "x", "arguments": {"a": 1}}).status_code == 400  # strings only
    assert expand({"source": "calliope", "name": "x", "extra": True}).status_code == 400
    assert expand({"source": "calliope", "name": "calliope_check"}, origin=None).status_code == 403
    client.put(f"/api/conversations/{cid}/tools/calliope", json={"enabled": False}, headers={"origin": ORIGIN})
    assert client.get(f"/api/conversations/{cid}/prompts").json() == {"prompts": []}
    off = expand({"source": "calliope", "name": "calliope_check"})
    assert off.status_code == 409 and off.json()["error"]["code"] == "source_disabled"
    for path in (f"/api/conversations/{ConversationId('c-nope')}/prompts",):
        assert client.get(path).status_code == 404
