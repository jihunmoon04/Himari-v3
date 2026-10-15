"""Tool views (MCP Apps, V3-P58) in the Data port, run against every backend."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from testkit import FOREIGN, SCOPE, data_for, new_store, text_result

from himari.common import CallId, CallRef, JsonValue, RawRepresentation, ToolCall, ToolId
from himari.data.api import (
    MAX_APP_PAYLOAD_BYTES,
    MAX_UI_HTML_BYTES,
    AppView,
    ArtifactMutation,
    AssistantToolCalls,
    DataError,
    DataErrorCode,
    PromptBasis,
    RawReceived,
    RunPreparation,
    ToolCompletion,
    ToolExecutionStatus,
    UiCsp,
    UiResource,
)
from himari.data.memory import InMemoryData
from himari.data.sqlite import SqliteData

RESOURCE = UiResource.create("calliope", "ui://calliope/map", "<!DOCTYPE html><p>map</p>",
                             UiCsp(connect_domains=("https://api.example.com",)), prefers_border=True)


def fresh() -> InMemoryData | SqliteData:
    store = new_store()
    store.create_conversation(SCOPE)
    return data_for(store)


async def projected_group(data: InMemoryData | SqliteData, *tool_ids: str) -> CallRef:
    run_id = await data.prepare_run(
        SCOPE,
        RunPreparation(b"{}", "application/json", RawRepresentation.ADAPTER_SERIALIZED, (), PromptBasis(0, 0), "p", "m"),
    )
    await data.mark_dispatch(SCOPE, run_id)
    await data.record_raw_outcome(SCOPE, run_id, RawReceived(b"{}", "application/json"))
    group = tuple(ToolCall(CallId(f"c{i}"), i, ToolId(t), {}) for i, t in enumerate(tool_ids))
    await data.project_model_output(SCOPE, run_id, AssistantToolCalls(group))
    return CallRef(run_id, CallId("c0"))


PAYLOAD: JsonValue = {"content": [{"type": "text", "text": "Seoul 21°C"}], "structuredContent": {"temp": 21}}


def done(status: ToolExecutionStatus = ToolExecutionStatus.SUCCEEDED, view: AppView | None = None,
         **kw: object) -> ToolCompletion:
    return ToolCompletion(status, text_result("Seoul 21°C"), app_view=view, **kw)  # type: ignore[arg-type]


def code_of(error: pytest.ExceptionInfo[DataError]) -> DataErrorCode:
    return error.value.code


def test_a_view_is_stored_with_the_result_and_read_back() -> None:
    async def scenario() -> None:
        data = fresh()
        call = await projected_group(data, "calliope/map")
        assert await data.read_app_view(SCOPE, call) is None  # still running
        await data.commit_tool_outcome(SCOPE, call, done(view=AppView(RESOURCE, PAYLOAD)))
        (record,) = await data.read_tool_executions(SCOPE, call.run_id)
        assert record.ui_digest == RESOURCE.digest
        stored = await data.read_app_view(SCOPE, call)
        assert stored is not None
        assert (stored.call, stored.tool_id, stored.status) == (call, "calliope/map", ToolExecutionStatus.SUCCEEDED)
        assert stored.view == AppView(RESOURCE, PAYLOAD)  # html, csp, prefers_border and payload, exactly

    asyncio.run(scenario())


def test_a_failed_call_that_ran_may_keep_its_view() -> None:
    async def scenario() -> None:
        data = fresh()
        call = await projected_group(data, "calliope/map")
        await data.commit_tool_outcome(SCOPE, call, done(ToolExecutionStatus.FAILED, AppView(RESOURCE, {"isError": True})))
        stored = await data.read_app_view(SCOPE, call)
        assert stored is not None and stored.status is ToolExecutionStatus.FAILED

    asyncio.run(scenario())


def test_a_call_without_a_view_reads_none() -> None:
    async def scenario() -> None:
        data = fresh()
        call = await projected_group(data, "echo")
        await data.commit_tool_outcome(SCOPE, call, done())
        (record,) = await data.read_tool_executions(SCOPE, call.run_id)
        assert record.ui_digest is None
        assert await data.read_app_view(SCOPE, call) is None

    asyncio.run(scenario())


def test_reading_a_view_is_scoped_to_the_conversation() -> None:
    async def scenario() -> None:
        data = fresh()
        call = await projected_group(data, "calliope/map")
        await data.commit_tool_outcome(SCOPE, call, done(view=AppView(RESOURCE, PAYLOAD)))
        with pytest.raises(DataError) as foreign:
            await data.read_app_view(FOREIGN, call)
        assert code_of(foreign) is DataErrorCode.NOT_AUTHORIZED
        with pytest.raises(DataError) as missing:
            await data.read_app_view(SCOPE, CallRef(call.run_id, CallId("nope")))
        assert code_of(missing) is DataErrorCode.NOT_FOUND

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [ToolExecutionStatus.CANCELLED, ToolExecutionStatus.DENIED])
def test_a_call_that_did_not_run_cannot_have_a_view(status: ToolExecutionStatus) -> None:
    async def scenario() -> None:
        data = fresh()
        call = await projected_group(data, "calliope/map")
        with pytest.raises(DataError) as error:
            await data.commit_tool_outcome(SCOPE, call, done(status, AppView(RESOURCE, PAYLOAD)))
        assert code_of(error) is DataErrorCode.INVALID_ARGUMENT
        (record,) = await data.read_tool_executions(SCOPE, call.run_id)
        assert record.status is ToolExecutionStatus.PENDING  # nothing committed

    asyncio.run(scenario())


@pytest.mark.parametrize("bad", [
    replace(RESOURCE, html="<p>changed after the digest</p>"),  # digest no longer matches
    UiResource.create("calliope", "https://calliope/map", "<p>"),  # not ui://
    UiResource.create("bad/source", "ui://x", "<p>"),
    UiResource.create("calliope", "ui://x", "x" * (MAX_UI_HTML_BYTES + 1)),
    UiResource.create("calliope", "ui://x", "가" * (MAX_UI_HTML_BYTES // 3 + 1)),  # counted in UTF-8 bytes
])
def test_a_malformed_view_is_refused_and_nothing_is_committed(bad: UiResource) -> None:
    async def scenario() -> None:
        data = fresh()
        call = await projected_group(data, "calliope/map")
        events_before = len(await data.read_events(SCOPE))
        with pytest.raises(DataError) as error:
            await data.commit_tool_outcome(SCOPE, call, done(view=AppView(bad, PAYLOAD)))
        assert code_of(error) is DataErrorCode.INVALID_ARGUMENT
        (record,) = await data.read_tool_executions(SCOPE, call.run_id)
        assert record.status is ToolExecutionStatus.PENDING
        assert len(await data.read_events(SCOPE)) == events_before

    asyncio.run(scenario())


def test_a_payload_over_the_limit_is_refused() -> None:
    async def scenario() -> None:
        data = fresh()
        call = await projected_group(data, "calliope/map")
        big = {"structuredContent": {"blob": "x" * MAX_APP_PAYLOAD_BYTES}}
        with pytest.raises(DataError) as error:
            await data.commit_tool_outcome(SCOPE, call, done(view=AppView(RESOURCE, big)))
        assert code_of(error) is DataErrorCode.INVALID_ARGUMENT

    asyncio.run(scenario())


def test_a_view_rolls_back_with_the_rest_of_the_commit() -> None:
    async def scenario() -> None:
        data = fresh()
        first = await projected_group(data, "note", "calliope/map")
        await data.commit_tool_outcome(SCOPE, first, done(artifact_mutations=(ArtifactMutation("note", "v1"),)))
        (note,) = await data.list_artifacts(SCOPE)
        second = CallRef(first.run_id, CallId("c1"))
        stale = ArtifactMutation("note", "v2", artifact_id=note.artifact_id, expected_revision=99)
        with pytest.raises(DataError) as error:
            await data.commit_tool_outcome(SCOPE, second, done(view=AppView(RESOURCE, PAYLOAD),
                                                                artifact_mutations=(stale,)))
        assert code_of(error) is DataErrorCode.VERSION_CONFLICT
        assert await data.read_app_view(SCOPE, second) is None
        records = await data.read_tool_executions(SCOPE, first.run_id)
        assert records[1].status is ToolExecutionStatus.PENDING and records[1].ui_digest is None

    asyncio.run(scenario())


def test_one_view_serves_many_calls() -> None:
    async def scenario() -> None:
        data = fresh()
        first = await projected_group(data, "calliope/map", "calliope/map")
        second = CallRef(first.run_id, CallId("c1"))
        await data.commit_tool_outcome(SCOPE, first, done(view=AppView(RESOURCE, {"n": 1})))
        await data.commit_tool_outcome(SCOPE, second, done(view=AppView(RESOURCE, {"n": 2})))
        one, two = await data.read_app_view(SCOPE, first), await data.read_app_view(SCOPE, second)
        assert one is not None and two is not None
        assert one.view.resource == two.view.resource == RESOURCE
        assert (one.view.payload, two.view.payload) == ({"n": 1}, {"n": 2})  # each call keeps its own

    asyncio.run(scenario())
