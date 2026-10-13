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
from himari.tool.dispatcher import Dispatcher
from himari.tool.registry import ToolRegistry


class StatusProbe:
    """Records its own ToolExecution status as seen from inside ``run``."""

    def __init__(self, data: InMemoryData, tool_id: str, *, kind: str | None) -> None:
        self._data = data
        self.definition = ToolDefinition(
            tool_id=ToolId(tool_id),
            description="probe",
            input_schema={"type": "object"},
            workspace_requirement=WorkspaceRequirement.DEFAULT if kind else WorkspaceRequirement.NONE,
            effect_profile=EffectProfile.SCOPED_WRITE,
            environment_kind=FAKE if kind else None,
        )
        self.seen: list[ToolExecutionStatus] = []
        self.run_id: RunId | None = None

    async def run(self, invocation: ToolInvocation, ctx: ToolContext) -> ToolOutcome:
        assert self.run_id is not None
        records = await self._data.read_tool_executions(ctx.scope, self.run_id)
        self.seen.extend(r.status for r in records if r.call.call_id == invocation.call_id)
        return ToolOutcome(ToolStatus.SUCCEEDED, text_result("ok"))


def test_only_effectful_environment_tools_are_claimed_before_running() -> None:
    async def scenario() -> None:
        store = InMemoryStore()
        store.create_conversation(SCOPE)
        store.activate_environment(SCOPE, FAKE, WorkspaceId("ws"))
        data = InMemoryData(store)
        internal = StatusProbe(data, "write_note", kind=None)
        env = StatusProbe(data, "fake/write", kind="fake")
        dispatcher = Dispatcher(ToolRegistry([internal, env]), data, data, data)

        run_id = await data.prepare_run(
            SCOPE, RunPreparation(b"", "t", RawRepresentation.ADAPTER_SERIALIZED, (), PromptBasis(0, 0), "p", "m")
        )
        await data.mark_dispatch(SCOPE, run_id)
        await data.record_raw_outcome(SCOPE, run_id, RawReceived(b"", "t"))
        group = (
            ToolCall(CallId("a"), 0, ToolId("write_note"), {}),
            ToolCall(CallId("b"), 1, ToolId("fake/write"), {}),
            ToolCall(CallId("c"), 2, ToolId("nope"), {}),
        )
        await data.project_model_output(SCOPE, run_id, AssistantToolCalls(group))
        internal.run_id = env.run_id = run_id

        result = await dispatcher.dispatch_group(SCOPE, run_id, group)

        assert result.state is GroupState.COMPLETED
        assert internal.seen == [ToolExecutionStatus.PENDING]
        assert env.seen == [ToolExecutionStatus.EXECUTION_CLAIMED]
        records = await data.read_tool_executions(SCOPE, run_id)
        assert records[2].status is ToolExecutionStatus.DENIED  # unknown tool

    asyncio.run(scenario())
