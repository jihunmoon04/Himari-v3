from __future__ import annotations

import asyncio

from testkit import FAKE, SCOPE, text_result

from himari.common import CallId, RawRepresentation, RunId, ToolCall, ToolId, WorkspaceId
from himari.data.api import (
    AssistantToolCalls,
    PromptBasis,
    RawReceived,
    RunPreparation,
    ToolExecutionStatus,
)
from himari.data.memory import InMemoryData, InMemoryStore
from himari.tool.api import (
    EffectProfile,
    GroupState,
    ToolContext,
    ToolDefinition,
    ToolInvocation,
    ToolOutcome,
    ToolStatus,
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
