"""Tool views through the Application (V3-P58 §3.5, §3.7, §6.1): which state a
call's view is in, who may see it, and what it may reach."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from mcp_fake import ECHO_SCHEMA, URL, VIEW_URI, FakeMcpServer, view_entry
from testkit import text_result

from himari.bootstrap.application import AppViewRefused, Application
from himari.bootstrap.operator import OPERATOR
from himari.common import CallId, CallRef, RawRepresentation, ToolCall, ToolId, TrustedScope, UnknownOutcomePolicy
from himari.data.api import (
    AppView,
    AssistantToolCalls,
    PromptBasis,
    RawReceived,
    RunPreparation,
    ToolCompletion,
    ToolExecutionStatus,
    UiCsp,
)
from himari.provider.scripted import ScriptedAdapter

DECLARED = UiCsp(connect_domains=("https://api.example.com", "https://declared-only.example.com"),
                 resource_domains=("https://cdn.example.com",))
ALLOWED = {"connect_domains": ["https://api.example.com", "https://allowed-only.example.com"]}


def fake_server() -> FakeMcpServer:
    server = FakeMcpServer([{"name": "echo", "description": "Echo with a view.", "inputSchema": ECHO_SCHEMA,
                             "_meta": {"ui": {"resourceUri": VIEW_URI}}}])
    server.resources = {VIEW_URI: {"contents": [view_entry(_meta={"ui": {"csp": {
        "connectDomains": list(DECLARED.connect_domains), "resourceDomains": list(DECLARED.resource_domains)}}})]}}
    return server


def with_app(tmp_path: Path, scenario: Callable[[Application, TrustedScope], Awaitable[None]], *,
             ui: dict[str, Any] | None = ALLOWED, default: str = "on") -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    entry: dict[str, Any] = {"url": URL, "default": default, "tools": {"echo": {"effect": "scoped_write"}}}
    if ui is not None:
        entry["ui"] = ui
    (root / "mcp.json").write_text(json.dumps({"servers": {"calliope": entry}}))
    (root / "mcp.json").chmod(0o600)

    async def run() -> None:
        app = await Application.open(root, ScriptedAdapter([]), model_id="scripted-model", sandbox=False,
                                     mcp_transport=fake_server().transport())
        try:
            cid = await app.create_conversation(OPERATOR)
            await scenario(app, TrustedScope(OPERATOR, cid))
        finally:
            await app.close()

    asyncio.run(run())


async def project(app: Application, scope: TrustedScope, tool_id: str = "calliope/echo") -> CallRef:
    """A model call recorded and not yet run (PENDING)."""
    data = app.data
    run_id = await data.prepare_run(
        scope, RunPreparation(b"{}", "application/json", RawRepresentation.ADAPTER_SERIALIZED, (), PromptBasis(0, 0),
                              "p", "m"))
    await data.mark_dispatch(scope, run_id)
    await data.record_raw_outcome(scope, run_id, RawReceived(b"{}", "application/json"))
    call = ToolCall(CallId("c0"), 0, ToolId(tool_id), {"text": "hi"})
    await data.project_model_output(scope, run_id, AssistantToolCalls((call,)))
    return CallRef(run_id, CallId("c0"))


async def refused(app: Application, scope: TrustedScope, call: CallRef) -> str:
    with pytest.raises(AppViewRefused) as error:
        await app.app_view(scope, call)
    return error.value.code


def test_a_running_call_shows_the_view_read_at_connect(tmp_path: Path) -> None:
    async def scenario(app: Application, scope: TrustedScope) -> None:
        call = await project(app, scope)
        state = await app.app_view(scope, call)
        assert (state.state, state.source, state.tool_id, state.status) == (
            "running", "calliope", "calliope/echo", ToolExecutionStatus.PENDING)
        assert state.resource is app.mcp.ui_for("calliope/echo") and state.result is None
        assert dict(state.arguments) == {"text": "hi"}
        assert state.tool == {"name": "echo", "description": "Echo with a view.", "inputSchema": ECHO_SCHEMA}

    with_app(tmp_path, scenario)


def test_the_csp_is_what_the_server_declares_and_the_operator_allows(tmp_path: Path) -> None:
    async def scenario(app: Application, scope: TrustedScope) -> None:
        state = await app.app_view(scope, await project(app, scope))
        assert state.resource is not None and state.resource.csp == DECLARED  # kept as declared
        # declared-only and allowed-only both drop out; resources: nothing allowed
        assert state.csp == UiCsp(connect_domains=("https://api.example.com",))

    with_app(tmp_path, scenario)


def test_a_finished_call_shows_its_stored_view_and_result(tmp_path: Path) -> None:
    async def scenario(app: Application, scope: TrustedScope) -> None:
        call = await project(app, scope)
        resource = app.mcp.ui_for("calliope/echo")
        assert resource is not None
        await app.data.commit_tool_outcome(scope, call, ToolCompletion(
            ToolExecutionStatus.SUCCEEDED, text_result("ok"), app_view=AppView(resource, {"structuredContent": {"n": 1}})))
        state = await app.app_view(scope, call)
        assert (state.state, state.status, state.result) == ("done", ToolExecutionStatus.SUCCEEDED,
                                                            {"structuredContent": {"n": 1}})
        assert state.resource == resource

    with_app(tmp_path, scenario)


@pytest.mark.parametrize("status, outcome", [
    (ToolExecutionStatus.CANCELLED, "not_run"),
    (ToolExecutionStatus.DENIED, "not_run"),
    (ToolExecutionStatus.SUCCEEDED, "view_unavailable"),  # PR #27 re-review N1: never "cancelled"
    (ToolExecutionStatus.FAILED, "view_unavailable"),
])
def test_a_call_that_ended_without_a_view_says_how_it_ended(tmp_path: Path, status: ToolExecutionStatus,
                                                            outcome: str) -> None:
    async def scenario(app: Application, scope: TrustedScope) -> None:
        call = await project(app, scope)
        await app.data.commit_tool_outcome(scope, call, ToolCompletion(status, text_result("Not run: stopped.")))
        state = await app.app_view(scope, call)
        assert (state.state, state.status, state.outcome) == ("ended", status, outcome)
        assert state.resource is None and state.reason == "Not run: stopped."

    with_app(tmp_path, scenario)


def test_a_result_too_large_for_the_view_says_so(tmp_path: Path) -> None:
    async def scenario(app: Application, scope: TrustedScope) -> None:
        call = await project(app, scope)
        await app.data.commit_tool_outcome(scope, call, ToolCompletion(
            ToolExecutionStatus.SUCCEEDED, text_result("ok"), raw_result={"ui_problem": "the result is larger than 1 MiB"}))
        state = await app.app_view(scope, call)
        assert (state.status, state.outcome, state.reason) == (
            ToolExecutionStatus.SUCCEEDED, "view_unavailable", "the result is larger than 1 MiB")

    with_app(tmp_path, scenario)


def test_an_unknown_outcome_is_neither_a_result_nor_cancelled(tmp_path: Path) -> None:
    async def scenario(app: Application, scope: TrustedScope) -> None:
        call = await project(app, scope)
        await app.data.claim_tool_execution(scope, call, unknown_outcome=UnknownOutcomePolicy.REPORT_TO_MODEL,
                                            record_event=True)
        assert (await app.app_view(scope, call)).state == "running"  # claimed: still running
        await app.data.commit_unknown_outcome(scope, call, text_result("Outcome unknown."))
        state = await app.app_view(scope, call)
        assert (state.state, state.status, state.outcome) == ("ended", ToolExecutionStatus.UNKNOWN, "unknown")

    with_app(tmp_path, scenario)


def test_a_source_off_in_this_conversation_shows_no_view(tmp_path: Path) -> None:
    """PR #27 re-review N2: a call the Dispatcher will deny never runs its view."""
    async def scenario(app: Application, scope: TrustedScope) -> None:
        call = await project(app, scope)
        assert await app.view_flags(scope, [call.run_id]) == {call: (True, False)}
        await app.set_tool_source(scope, "calliope", False)
        assert await refused(app, scope, call) == "source_disabled"
        assert await app.view_flags(scope, [call.run_id]) == {}
        await app.set_tool_source(scope, "calliope", True)
        resource = app.mcp.ui_for("calliope/echo")
        assert resource is not None
        await app.data.commit_tool_outcome(scope, call, ToolCompletion(
            ToolExecutionStatus.SUCCEEDED, text_result("ok"), app_view=AppView(resource, {})))
        assert await app.view_flags(scope, [call.run_id]) == {call: (True, True)}
        await app.set_tool_source(scope, "calliope", False)
        assert await refused(app, scope, call) == "source_disabled"  # stored views too

    with_app(tmp_path, scenario)


def test_a_source_off_by_default_shows_no_view_until_turned_on(tmp_path: Path) -> None:
    async def scenario(app: Application, scope: TrustedScope) -> None:
        call = await project(app, scope)
        assert await refused(app, scope, call) == "source_disabled"
        await app.set_tool_source(scope, "calliope", True)
        assert (await app.app_view(scope, call)).state == "running"

    with_app(tmp_path, scenario, default="off")


def test_a_server_whose_views_are_not_shown_has_none(tmp_path: Path) -> None:
    async def scenario(app: Application, scope: TrustedScope) -> None:
        call = await project(app, scope)
        assert not app.views_configured
        assert await refused(app, scope, call) == "app_view_disabled"
        assert await app.view_flags(scope, [call.run_id]) == {}

    with_app(tmp_path, scenario, ui=None)


@pytest.mark.parametrize("tool_id", ["inside/read_file", "elsewhere/echo", "echo"])
def test_a_call_of_a_tool_without_views_has_none(tmp_path: Path, tool_id: str) -> None:
    async def scenario(app: Application, scope: TrustedScope) -> None:
        call = await project(app, scope, tool_id)
        assert await refused(app, scope, call) == "no_app_view"
        assert await refused(app, scope, CallRef(call.run_id, CallId("nope"))) == "no_app_view"

    with_app(tmp_path, scenario)
