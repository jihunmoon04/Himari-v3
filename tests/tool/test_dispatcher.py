from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace

from testkit import FAKE, SCOPE, RecordingTool, text_result

from himari.common import CallId, RawRepresentation, RunId, ToolCall, ToolId, WorkspaceId
from himari.data.api import (
    AssistantToolCalls,
    PromptBasis,
    RawReceived,
    RunPreparation,
    ToolExecutionRecord,
    ToolExecutionStatus,
)
from himari.data.memory import InMemoryData, InMemoryStore
from himari.tool.api import (
    AppView,
    EffectProfile,
    GroupState,
    ToolContext,
    ToolDefinition,
    ToolInvocation,
    ToolOutcome,
    ToolStatus,
    UiResource,
    WorkspaceRequirement,
)
from himari.tool.access import ConfiguredExternalAccess
from himari.tool.dispatcher import Dispatcher
from himari.tool.registry import ToolRegistry


class StatusProbe:
    """Records its own ToolExecution status as seen from inside ``run``."""

    def __init__(self, data: InMemoryData, tool_id: str, *, kind: str | None, source: str | None = None,
                 effect: EffectProfile = EffectProfile.SCOPED_WRITE) -> None:
        self._data = data
        self.definition = ToolDefinition(
            tool_id=ToolId(tool_id),
            description="probe",
            input_schema={"type": "object"},
            workspace_requirement=WorkspaceRequirement.DEFAULT if kind else WorkspaceRequirement.NONE,
            effect_profile=effect,
            environment_kind=FAKE if kind else None,
            external_source=source,
        )
        self.seen: list[ToolExecutionStatus] = []
        self.run_id: RunId | None = None

    async def run(self, invocation: ToolInvocation, ctx: ToolContext) -> ToolOutcome:
        assert self.run_id is not None
        records = await self._data.read_tool_executions(ctx.scope, self.run_id)
        self.seen.extend(r.status for r in records if r.call.call_id == invocation.call_id)
        return ToolOutcome(ToolStatus.SUCCEEDED, text_result("ok"))


def test_only_effectful_environment_and_external_tools_are_claimed_before_running() -> None:
    async def scenario() -> None:
        store = InMemoryStore()
        store.create_conversation(SCOPE)
        store.activate_environment(SCOPE, FAKE, WorkspaceId("ws"))
        data = InMemoryData(store)
        internal = StatusProbe(data, "write_note", kind=None)
        env = StatusProbe(data, "fake/write", kind="fake")
        # V3-P52: an External server's effects are as invisible to HIMARI as
        # an Environment's, so the same claim rule applies; READ is never claimed.
        external = StatusProbe(data, "remote/write", kind=None, source="remote")
        external_read = StatusProbe(data, "remote/read", kind=None, source="remote", effect=EffectProfile.READ)
        dispatcher = Dispatcher(ToolRegistry([internal, env, external, external_read]), data, data, data,
                                ConfiguredExternalAccess({"remote": True}, data))

        run_id = await data.prepare_run(
            SCOPE, RunPreparation(b"", "t", RawRepresentation.ADAPTER_SERIALIZED, (), PromptBasis(0, 0), "p", "m")
        )
        await data.mark_dispatch(SCOPE, run_id)
        await data.record_raw_outcome(SCOPE, run_id, RawReceived(b"", "t"))
        group = (
            ToolCall(CallId("a"), 0, ToolId("write_note"), {}),
            ToolCall(CallId("b"), 1, ToolId("fake/write"), {}),
            ToolCall(CallId("c"), 2, ToolId("nope"), {}),
            ToolCall(CallId("d"), 3, ToolId("remote/write"), {}),
            ToolCall(CallId("e"), 4, ToolId("remote/read"), {}),
        )
        await data.project_model_output(SCOPE, run_id, AssistantToolCalls(group))
        internal.run_id = env.run_id = external.run_id = external_read.run_id = run_id

        result = await dispatcher.dispatch_group(SCOPE, run_id, group)

        assert result.state is GroupState.COMPLETED
        assert internal.seen == [ToolExecutionStatus.PENDING]
        assert env.seen == [ToolExecutionStatus.EXECUTION_CLAIMED]
        assert external.seen == [ToolExecutionStatus.EXECUTION_CLAIMED]
        assert external_read.seen == [ToolExecutionStatus.PENDING]
        records = await data.read_tool_executions(SCOPE, run_id)
        assert records[2].status is ToolExecutionStatus.DENIED  # unknown tool

    asyncio.run(scenario())


async def _one_external_call(dispatcher: Dispatcher, data: InMemoryData, probe: StatusProbe) -> ToolExecutionStatus:
    run_id = await data.prepare_run(
        SCOPE, RunPreparation(b"", "t", RawRepresentation.ADAPTER_SERIALIZED, (), PromptBasis(0, 0), "p", "m")
    )
    await data.mark_dispatch(SCOPE, run_id)
    await data.record_raw_outcome(SCOPE, run_id, RawReceived(b"", "t"))
    group = (ToolCall(CallId("a"), 0, probe.definition.tool_id, {}),)
    await data.project_model_output(SCOPE, run_id, AssistantToolCalls(group))
    probe.run_id = run_id
    await dispatcher.dispatch_group(SCOPE, run_id, group)
    return (await data.read_tool_executions(SCOPE, run_id))[0].status


def test_an_external_tool_runs_only_while_its_source_is_enabled_for_the_conversation() -> None:
    """V3-P53: the Dispatcher asks ExternalAccess right before each call, so
    a source turned off after the model saw its Tools denies the call."""

    async def scenario() -> None:
        store = InMemoryStore()
        store.create_conversation(SCOPE)
        data = InMemoryData(store)
        probe = StatusProbe(data, "remote/read", kind=None, source="remote", effect=EffectProfile.READ)
        registry = ToolRegistry([probe])

        unconfigured = Dispatcher(registry, data, data, data)  # no ExternalAccess: nothing is enabled
        assert await _one_external_call(unconfigured, data, probe) is ToolExecutionStatus.DENIED
        assert probe.seen == []

        dispatcher = Dispatcher(registry, data, data, data, ConfiguredExternalAccess({"remote": False}, data))
        assert await _one_external_call(dispatcher, data, probe) is ToolExecutionStatus.DENIED  # off by default
        await data.set_external_setting(SCOPE, "remote", True)
        assert await _one_external_call(dispatcher, data, probe) is ToolExecutionStatus.SUCCEEDED
        await data.set_external_setting(SCOPE, "remote", False)
        assert await _one_external_call(dispatcher, data, probe) is ToolExecutionStatus.DENIED
        assert len(probe.seen) == 1
        result = (await data.read_events(SCOPE))[-1].payload
        assert "not enabled in this conversation" in result.model_result.blocks[0].text  # type: ignore[union-attr]

    asyncio.run(scenario())


def test_the_catalog_shows_external_tools_of_enabled_sources_only() -> None:
    store = InMemoryStore()
    data = InMemoryData(store)
    registry = ToolRegistry([StatusProbe(data, "remote/read", kind=None, source="remote"),
                             StatusProbe(data, "other/read", kind=None, source="other"),
                             StatusProbe(data, "write_note", kind=None)])
    assert [d.tool_id for d in registry.catalog(frozenset())] == ["write_note"]
    assert [d.tool_id for d in registry.catalog(frozenset(), frozenset({"remote"}))] == ["remote/read", "write_note"]
    assert registry.external_sources() == {"remote", "other"}


# --- Tool views (MCP Apps, V3-P58) ---------------------------------------------

VIEW = UiResource.create("remote", "ui://remote/view", "<p>view</p>")


def dispatch_one(outcome: ToolOutcome, *, effect: EffectProfile = EffectProfile.SCOPED_WRITE,
                 ) -> tuple[InMemoryData, ToolExecutionRecord]:
    """One External call whose handler returns ``outcome``; the data and its record."""
    async def scenario() -> tuple[InMemoryData, ToolExecutionRecord]:
        store = InMemoryStore()
        store.create_conversation(SCOPE)
        data = InMemoryData(store)
        tool = RecordingTool("remote/view", external_source="remote", effect=effect,
                             behavior=lambda invocation, ctx: outcome)
        dispatcher = Dispatcher(ToolRegistry([tool]), data, data, data, ConfiguredExternalAccess({"remote": True}, data))
        run_id = await data.prepare_run(
            SCOPE, RunPreparation(b"", "t", RawRepresentation.ADAPTER_SERIALIZED, (), PromptBasis(0, 0), "p", "m"))
        await data.mark_dispatch(SCOPE, run_id)
        await data.record_raw_outcome(SCOPE, run_id, RawReceived(b"", "t"))
        call = ToolCall(CallId("a"), 0, ToolId("remote/view"), {})
        await data.project_model_output(SCOPE, run_id, AssistantToolCalls((call,)))
        await dispatcher.dispatch_group(SCOPE, run_id, (call,))
        (record,) = await data.read_tool_executions(SCOPE, run_id)
        return data, record

    return asyncio.run(scenario())


def test_a_view_is_committed_with_the_result() -> None:
    data, record = dispatch_one(ToolOutcome(ToolStatus.SUCCEEDED, text_result("ok"), app_view=AppView(VIEW, {"n": 1})))
    assert (record.status, record.ui_digest) == (ToolExecutionStatus.SUCCEEDED, VIEW.digest)
    stored = asyncio.run(data.read_app_view(SCOPE, record.call))
    assert stored is not None and stored.view == AppView(VIEW, {"n": 1})


def test_a_view_on_a_call_that_did_not_run_is_dropped() -> None:
    _, record = dispatch_one(ToolOutcome(ToolStatus.DENIED, text_result("no"), app_view=AppView(VIEW, {})))
    assert (record.status, record.ui_digest) == (ToolExecutionStatus.DENIED, None)


def test_a_view_data_refuses_does_not_cost_the_call_its_result() -> None:
    forged = replace(VIEW, html="<p>not what the digest says</p>")
    _, record = dispatch_one(ToolOutcome(ToolStatus.SUCCEEDED, text_result("ok"), app_view=AppView(forged, {})))
    # Claimed (SCOPED_WRITE), yet definite: not UNKNOWN because of the view.
    assert (record.status, record.model_result, record.ui_digest) == (ToolExecutionStatus.SUCCEEDED, text_result("ok"), None)


# --- presented files (V3-P66 §7.1) ------------------------------------------------


def _blob(store: InMemoryStore, content: bytes):  # type: ignore[no-untyped-def]
    async def chunks() -> AsyncIterator[bytes]:
        yield content
    return asyncio.run(store.blobs.put(SCOPE.principal_id, chunks(), 1000))


def test_presented_files_are_committed_with_the_result() -> None:
    from himari.tool.api import PresentedFile
    store_for_blob = InMemoryStore()
    blob = _blob(store_for_blob, b"x")
    # A blob of another store is missing in the dispatching one: refused below.
    data, record = dispatch_one(ToolOutcome(ToolStatus.SUCCEEDED, text_result("Presented x to the user."),
                                            presented_files=(PresentedFile(blob, "x", "text/plain", "/workspace/x"),)))
    # review B3: never a success without the file
    assert record.status is ToolExecutionStatus.FAILED
    assert record.model_result is not None and record.model_result.is_error
    assert "could not be kept" in record.model_result.blocks[0].text  # type: ignore[union-attr]
    assert asyncio.run(data.read_call_files(SCOPE, [record.call])) == {}


def test_presented_files_recorded_when_the_blob_is_there() -> None:
    from himari.tool.api import PresentedFile
    blob_holder: dict[str, object] = {}

    def behavior(invocation, ctx):  # type: ignore[no-untyped-def]
        return ToolOutcome(ToolStatus.SUCCEEDED, text_result("given"),
                           presented_files=(PresentedFile(blob_holder["blob"], "x", "text/plain", "/workspace/x"),))  # type: ignore[arg-type]

    async def scenario() -> None:
        store = InMemoryStore()
        store.create_conversation(SCOPE)
        data = InMemoryData(store)

        async def chunks() -> AsyncIterator[bytes]:
            yield b"bytes"
        blob_holder["blob"] = await store.blobs.put(SCOPE.principal_id, chunks(), 100)
        tool = RecordingTool("remote/give", external_source="remote", behavior=behavior)
        dispatcher = Dispatcher(ToolRegistry([tool]), data, data, data, ConfiguredExternalAccess({"remote": True}, data))
        run_id = await data.prepare_run(
            SCOPE, RunPreparation(b"", "t", RawRepresentation.ADAPTER_SERIALIZED, (), PromptBasis(0, 0), "p", "m"))
        await data.mark_dispatch(SCOPE, run_id)
        await data.record_raw_outcome(SCOPE, run_id, RawReceived(b"", "t"))
        call = ToolCall(CallId("a"), 0, ToolId("remote/give"), {})
        await data.project_model_output(SCOPE, run_id, AssistantToolCalls((call,)))
        await dispatcher.dispatch_group(SCOPE, run_id, (call,))
        (record,) = await data.read_tool_executions(SCOPE, run_id)
        assert record.status is ToolExecutionStatus.SUCCEEDED, record
        files = await data.read_call_files(SCOPE, [record.call])
        assert [(f.name, f.version) for f in files[record.call]] == [("x", 1)]

    asyncio.run(scenario())
