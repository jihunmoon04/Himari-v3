"""Tool Dispatcher: implements ``ToolDispatcher`` (TOOL-02/04).

P0 scheduling is sequential in original ordinal order; parallel scheduling
(TOOL-03) comes later without changing the port. Rules:

- Only never-claimed ``PENDING`` calls run, so the same method is the resume path.
- Environment-provided Tools with ``effect_profile != READ`` → ``claim_tool_execution``
  before ``run`` (V3-045). Internal Tools never claim: their only effects are the
  ToolOutcome, committed atomically with the result (V3-042, Data v0.2 §6).
- A handler exception after a claim leaves the call ``EXECUTION_CLAIMED``: the
  effect is uncertain, the group is BLOCKED and restart cleanup marks it UNKNOWN.
  Without a claim nothing happened, so it is committed as FAILED.
- Unknown tool, arguments failing ``input_schema``, an Environment Tool whose
  kind is not active in this Conversation (even with no Workspace requirement:
  catalog exposure is not authorization), or a missing/revoked/mismatched
  Binding → DENIED result to the model; the handler never runs.
- ``ask_user`` (suspending) is never run; it stays PENDING.
"""

from __future__ import annotations

from collections.abc import Sequence

from himari.common import BindingId, CallRef, ModelToolResult, RunId, TextBlock, ToolCall, TrustedScope
from himari.data.api import (
    ArtifactStore,
    BindingAuthority,
    ConversationLog,
    DataError,
    ToolCompletion,
    ToolExecutionRecord,
    ToolExecutionStatus,
    WorkspaceState,
)
from himari.tool.api import (
    Completion,
    ConversationRecord,
    EffectProfile,
    GroupDispatchResult,
    GroupState,
    ToolContext,
    ToolDefinition,
    ToolInvocation,
    ToolOutcome,
    ToolStatus,
    WorkspaceRequirement,
)
from himari.tool.registry import ToolRegistry
from himari.tool.schema import validate

_STATUS = {
    ToolStatus.SUCCEEDED: ToolExecutionStatus.SUCCEEDED,
    ToolStatus.FAILED: ToolExecutionStatus.FAILED,
    ToolStatus.DENIED: ToolExecutionStatus.DENIED,
    ToolStatus.CANCELLED: ToolExecutionStatus.CANCELLED,
}


class _Denied(Exception):
    pass


def _error(status: ToolStatus, message: str) -> ToolOutcome:
    return ToolOutcome(status, ModelToolResult((TextBlock(message),), is_error=True))


class Dispatcher:
    def __init__(
        self,
        registry: ToolRegistry,
        log: ConversationLog,
        artifacts: ArtifactStore,
        workspaces: WorkspaceState,
    ) -> None:
        self._registry = registry
        self._log = log
        self._artifacts = artifacts
        self._workspaces = workspaces

    async def dispatch_group(
        self, scope: TrustedScope, run_id: RunId, calls: Sequence[ToolCall]
    ) -> GroupDispatchResult:
        records = await self._log.read_tool_executions(scope, run_id)  # authoritative, ordinal order
        for record in records:
            if record.status is ToolExecutionStatus.PENDING:
                await self._run_one(scope, record)
        return self._group_state(run_id, await self._log.read_tool_executions(scope, run_id))

    async def _run_one(self, scope: TrustedScope, record: ToolExecutionRecord) -> None:
        handler = self._registry.get(record.tool_id)
        if handler is None:
            await self._commit(scope, record.call, _error(ToolStatus.DENIED, f"unknown tool {record.tool_id}"), True)
            return
        d = handler.definition
        if d.completion is Completion.SUSPENDING:
            return  # stays PENDING; Control turns this into WAITING_FOR_USER
        record_event = d.conversation_record is ConversationRecord.FULL
        try:
            violations = validate(record.arguments, d.input_schema)
            if violations:
                raise _Denied("invalid arguments: " + "; ".join(violations))
            binding = await self._resolve_binding(scope, d, record)
        except _Denied as denied:
            await self._commit(scope, record.call, _error(ToolStatus.DENIED, str(denied)), record_event)
            return
        claimed = d.environment_kind is not None and d.effect_profile is not EffectProfile.READ
        if claimed:
            await self._log.claim_tool_execution(scope, record.call)
        ctx = ToolContext(scope=scope, artifacts=self._artifacts, binding=binding)
        invocation = ToolInvocation(record.tool_id, record.call.call_id, record.arguments)
        try:
            outcome = await handler.run(invocation, ctx)
        except Exception as exc:  # noqa: BLE001 — uncertainty is decided by the claim
            if claimed:
                return  # effect uncertain: stays EXECUTION_CLAIMED → UNKNOWN on recovery
            outcome = _error(ToolStatus.FAILED, f"{type(exc).__name__}: {exc}")
        await self._commit(scope, record.call, outcome, record_event)

    async def _resolve_binding(
        self, scope: TrustedScope, d: ToolDefinition, record: ToolExecutionRecord
    ) -> BindingAuthority | None:
        activation = None
        if d.environment_kind is not None:
            activation = await self._workspaces.read_activation(scope, d.environment_kind)
            if activation is None:
                raise _Denied(f"environment {d.environment_kind} is not active in this conversation")
        if d.workspace_requirement is WorkspaceRequirement.NONE:
            return None
        try:
            if record.workspace_binding_id is not None:
                binding = await self._workspaces.read_binding_authority(
                    scope, BindingId(record.workspace_binding_id)
                )
            elif d.workspace_requirement is WorkspaceRequirement.DEFAULT and d.environment_kind:
                found = await self._workspaces.resolve_default_binding(scope, d.environment_kind)
                if found is None:
                    raise _Denied("no default workspace binding")
                binding = found
            else:
                raise _Denied("workspace_binding_id is required")
        except DataError as exc:
            raise _Denied(f"binding unavailable: {exc.code.value}") from exc
        if binding.environment_kind != d.environment_kind or (
            activation is not None and binding.environment_instance_id != activation.environment_instance_id
        ):
            raise _Denied("binding does not belong to the active environment instance")
        return binding

    async def _commit(
        self, scope: TrustedScope, call: CallRef, outcome: ToolOutcome, record_event: bool
    ) -> None:
        await self._log.commit_tool_outcome(
            scope,
            call,
            ToolCompletion(
                status=_STATUS[outcome.status],
                model_result=outcome.model_result,
                raw_result=outcome.raw_result,
                artifact_mutations=outcome.artifact_mutations,
                record_event=record_event,
            ),
        )

    @staticmethod
    def _group_state(run_id: RunId, records: Sequence[ToolExecutionRecord]) -> GroupDispatchResult:
        uncertain = tuple(
            r.call
            for r in records
            if r.status in (ToolExecutionStatus.UNKNOWN, ToolExecutionStatus.EXECUTION_CLAIMED)
        )
        if uncertain:
            return GroupDispatchResult(run_id, GroupState.BLOCKED_UNKNOWN, unknown_calls=uncertain)
        pending = [r for r in records if r.status is ToolExecutionStatus.PENDING]
        if pending:
            # Only a suspending call can remain PENDING after dispatch (V3-P15: it is alone).
            return GroupDispatchResult(run_id, GroupState.WAITING_FOR_USER, waiting_call=pending[0].call)
        return GroupDispatchResult(run_id, GroupState.COMPLETED)
