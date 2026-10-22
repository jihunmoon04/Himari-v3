"""Tool Dispatcher: implements ``ToolDispatcher`` (TOOL-02/04).

P0 scheduling is sequential in original ordinal order; parallel scheduling
(TOOL-03) comes later without changing the port. Rules:

- Only never-claimed ``PENDING`` calls run, so the same method is the resume path.
- Environment-provided and External Tools with ``effect_profile != READ`` →
  ``claim_tool_execution`` before ``run`` (V3-045, V3-P52). Internal Tools never
  claim: their only effects are the ToolOutcome, committed atomically with the
  result (V3-042, Data v0.2 §6).
- A handler exception after a claim means the effect is uncertain. It is never
  turned into FAILED and never re-run. With ``unknown_outcome=REPORT_TO_MODEL``
  (default) the call becomes UNKNOWN with an "outcome unknown" result and the
  model verifies the state before deciding to retry (V3-047). With
  ``REQUIRE_USER`` it stays uncertain, **no further call of the group runs**
  and the group is BLOCKED. Calls left UNKNOWN by restart cleanup get the same
  treatment on the next dispatch, judged by the policy **persisted at claim
  time** (an unknown policy counts as REQUIRE_USER), never by the current
  registry. Without a claim nothing happened, so the exception is FAILED.
- A handler exception is never shown to the model verbatim (Q-39): the model
  gets a generic message; type and text go to ``raw_result`` only.
- Unknown tool, an External Tool whose source is not enabled for this
  Conversation (``ExternalAccess``; V3-P53), arguments failing
  ``input_schema``, an Environment Tool whose kind is not active in this
  Conversation (even with no Workspace requirement: catalog exposure is
  not authorization), or a missing/revoked/mismatched Binding → DENIED
  result to the model; the handler never runs. The group's result names
  these calls and the rule (``refusals``, V3-P62 §4); the records stay as
  they are (a handler's own DENIED is never among them).
- External source check and its concurrency contract (V3-P53): the source
  is checked before validation (which may take seconds in a child
  process) and **again after it, as the last step before the claim and
  ``run``**. A source turned off before that last check denies the call; a
  call that passed it has started and is not stopped by a later change
  (as with any Tool already running).
- ``ask_user`` (suspending) is never run; it stays PENDING.
- Pause and cancel (V3-P55): ``pause`` stops the group before the next
  call starts (PAUSED); ``cancel_group`` closes never-started calls with a
  CANCELLED result and settles claimed ones as uncertain.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import replace

from himari.common import (
    AttachmentRef,
    BindingId,
    CallId,
    CallRef,
    ContentBlock,
    ModelToolResult,
    RunId,
    TextBlock,
    ToolCall,
    TrustedScope,
    UnknownOutcomePolicy,
)
from himari.data.api import (
    ArtifactStore,
    BindingAuthority,
    ConversationLog,
    ConversationWorkspace,
    DataError,
    DataErrorCode,
    ToolCompletion,
    ToolExecutionRecord,
    ToolExecutionStatus,
    WorkspaceState,
)
from himari.tool.access import NoExternalAccess
from himari.tool.api import (
    MAX_RESULT_IMAGES,
    Completion,
    ConversationRecord,
    EffectProfile,
    ExternalAccess,
    GroupDispatchResult,
    GroupState,
    RefusalRule,
    ToolContext,
    ToolDefinition,
    ToolInvocation,
    ToolOrigin,
    ToolOutcome,
    ToolStatus,
    ResultImageStore,
    WorkspaceRequirement,
)
from himari.tool.registry import ToolRegistry
from himari.tool.schema import validate, validate_isolated

log = logging.getLogger(__name__)

_STATUS = {
    ToolStatus.SUCCEEDED: ToolExecutionStatus.SUCCEEDED,
    ToolStatus.FAILED: ToolExecutionStatus.FAILED,
    ToolStatus.DENIED: ToolExecutionStatus.DENIED,
    ToolStatus.CANCELLED: ToolExecutionStatus.CANCELLED,
}


# Why Data refused a call's presented files (V3-P66 §7.1), as the model is told.
_FILES_REFUSED = {
    DataErrorCode.BLOB_MISSING: "its stored copy is gone",
    DataErrorCode.LIMIT_EXCEEDED: "too much was presented in the last hour",
}

UNKNOWN_OUTCOME_RESULT = ModelToolResult(
    blocks=(
        TextBlock(
            "Outcome unknown: this call was interrupted before its result was recorded, "
            "so its effect may or may not have been applied. Do not assume it failed or "
            "succeeded. Check the current state with other tools before retrying it."
        ),
    ),
    is_error=True,
)


def _uncertain(record: ToolExecutionRecord) -> bool:
    """Claimed, no definite result, nothing told to the model yet."""
    return record.model_result is None and record.status in (
        ToolExecutionStatus.EXECUTION_CLAIMED,
        ToolExecutionStatus.UNKNOWN,
    )


def _blocks(record: ToolExecutionRecord) -> bool:
    """Uncertain and not reportable by its persisted policy (None → REQUIRE_USER)."""
    return _uncertain(record) and record.unknown_outcome is not UnknownOutcomePolicy.REPORT_TO_MODEL


class _Denied(Exception):
    """A refusal before running, with the rule that made it (V3-P62 §4)."""

    def __init__(self, message: str, rule: RefusalRule = RefusalRule.BINDING) -> None:
        super().__init__(message)
        self.rule = rule


def _error(status: ToolStatus, message: str) -> ToolOutcome:
    return ToolOutcome(status, ModelToolResult((TextBlock(message),), is_error=True))


class Dispatcher:
    def __init__(
        self,
        registry: ToolRegistry,
        log: ConversationLog,
        artifacts: ArtifactStore,
        workspaces: WorkspaceState,
        external_access: ExternalAccess | None = None,  # None: no External source is enabled
        images: ResultImageStore | None = None,  # None: Tool images become lines (V3-P67 §4.2)
    ) -> None:
        self._registry = registry
        self._log = log
        self._artifacts = artifacts
        self._workspaces = workspaces
        self._external_access = external_access if external_access is not None else NoExternalAccess()
        self._images = images

    async def dispatch_group(
        self,
        scope: TrustedScope,
        run_id: RunId,
        calls: Sequence[ToolCall],
        *,
        pause: Callable[[], bool] | None = None,
    ) -> GroupDispatchResult:
        count = len(await self._log.read_tool_executions(scope, run_id))
        paused = False
        refusals: dict[CallId, RefusalRule] = {}
        for index in range(count):
            # Re-read each step: the authoritative state, in original ordinal order.
            records = await self._log.read_tool_executions(scope, run_id)
            await self._report_reportable(scope, records)
            if any(_blocks(r) for r in records):
                break  # REQUIRE_USER uncertainty: no further effects in this group
            record = records[index]
            if record.status is not ToolExecutionStatus.PENDING:
                continue
            if pause is not None and not self._suspending(record) and pause():
                paused = True  # nothing more starts; the rest stays PENDING for resume
                break
            rule = await self._run_one(scope, record)
            if rule is not None:
                refusals[record.call.call_id] = rule
        records = await self._log.read_tool_executions(scope, run_id)
        await self._report_reportable(scope, records)
        state = self._group_state(run_id, await self._log.read_tool_executions(scope, run_id))
        if paused and state.state is not GroupState.BLOCKED_UNKNOWN:
            return GroupDispatchResult(run_id, GroupState.PAUSED, refusals=refusals)
        return replace(state, refusals=refusals)

    async def cancel_group(self, scope: TrustedScope, run_id: RunId, reason: str) -> GroupDispatchResult:
        result = ModelToolResult((TextBlock(reason),), is_error=True)
        for record in await self._log.read_tool_executions(scope, run_id):
            if record.status is ToolExecutionStatus.PENDING:
                handler = self._registry.get(record.tool_id)
                record_event = handler is None or handler.definition.conversation_record is ConversationRecord.FULL
                await self._commit(scope, record.call, ToolOutcome(ToolStatus.CANCELLED, result), record_event)
        records = await self._log.read_tool_executions(scope, run_id)
        await self._report_reportable(scope, records)
        return self._group_state(run_id, await self._log.read_tool_executions(scope, run_id))

    def _suspending(self, record: ToolExecutionRecord) -> bool:
        handler = self._registry.get(record.tool_id)
        return handler is not None and handler.definition.completion is Completion.SUSPENDING

    async def _report_reportable(self, scope: TrustedScope, records: Sequence[ToolExecutionRecord]) -> None:
        for record in records:
            if _uncertain(record) and record.unknown_outcome is UnknownOutcomePolicy.REPORT_TO_MODEL:
                # Event recording follows the claim-time policy kept by Data,
                # never the current registry.
                await self._log.commit_unknown_outcome(scope, record.call, UNKNOWN_OUTCOME_RESULT)

    async def _run_one(self, scope: TrustedScope, record: ToolExecutionRecord) -> RefusalRule | None:
        """Runs one call; the rule if the Dispatcher refused it before running."""
        handler = self._registry.get(record.tool_id)
        if handler is None:
            await self._commit(scope, record.call, _error(ToolStatus.DENIED, f"unknown tool {record.tool_id}"), True)
            return RefusalRule.UNKNOWN_TOOL
        d = handler.definition
        if d.completion is Completion.SUSPENDING:
            return None  # stays PENDING; Control turns this into WAITING_FOR_USER
        record_event = d.conversation_record is ConversationRecord.FULL
        try:
            await self._check_source(scope, d)
            if d.origin is ToolOrigin.EXTERNAL:  # an untrusted schema runs only in a killable child
                violations = await validate_isolated(record.arguments, d.input_schema)
            else:
                violations = validate(record.arguments, d.input_schema)
            if violations:
                raise _Denied("invalid arguments: " + "; ".join(violations), RefusalRule.INVALID_ARGUMENTS)
            binding = await self._resolve_binding(scope, d, record)
            workspace = (
                ConversationWorkspace(binding.workspace_id, binding.cwd_relative)
                if binding is not None and binding.cwd_relative is not None
                else None
            )
            await self._check_source(scope, d)  # again, last: it may have been turned off while validating
        except _Denied as denied:
            await self._commit(scope, record.call, _error(ToolStatus.DENIED, str(denied)), record_event)
            return denied.rule
        claimed = d.origin is not ToolOrigin.INTERNAL and d.effect_profile is not EffectProfile.READ
        if claimed:
            await self._log.claim_tool_execution(
                scope, record.call, unknown_outcome=d.unknown_outcome, record_event=record_event
            )
        ctx = ToolContext(scope=scope, artifacts=self._artifacts, binding=binding, workspace=workspace)
        invocation = ToolInvocation(record.tool_id, record.call.call_id, record.arguments)
        try:
            outcome = await handler.run(invocation, ctx)
        except Exception as exc:  # noqa: BLE001 — uncertainty is decided by the claim
            if claimed:
                # For the operator: which Tool, and the kind of failure (Q-39: never its text).
                log.warning("%s raised %s after its claim: its outcome is unknown", record.tool_id, type(exc).__name__)
                return None  # uncertain; reported or blocking per the persisted policy
            # Q-39: exception text may carry host paths or secrets; the model
            # only learns that the tool failed internally.
            outcome = ToolOutcome(
                ToolStatus.FAILED,
                ModelToolResult((TextBlock("The tool failed with an internal error."),), is_error=True),
                raw_result={"error_type": type(exc).__name__, "error": str(exc)},
            )
        await self._commit(scope, record.call, outcome, record_event)
        return None

    async def _check_source(self, scope: TrustedScope, d: ToolDefinition) -> None:
        if d.external_source is not None and d.external_source not in await self._external_access.enabled_sources(
            scope
        ):
            raise _Denied(f"tool source {d.external_source} is not enabled in this conversation",
                          RefusalRule.SOURCE_DISABLED)

    async def _resolve_binding(
        self, scope: TrustedScope, d: ToolDefinition, record: ToolExecutionRecord
    ) -> BindingAuthority | None:
        activation = None
        if d.environment_kind is not None:
            activation = await self._workspaces.read_activation(scope, d.environment_kind)
            if activation is None:
                raise _Denied(f"environment {d.environment_kind} is not active in this conversation",
                              RefusalRule.ENVIRONMENT_INACTIVE)
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

    async def _keep_images(self, scope: TrustedScope, call: CallRef, outcome: ToolOutcome) -> ToolOutcome:
        """V3-P67 §4.2: each returned image kept and staged, an attachment at
        the end of the result for it — or a line saying why not."""
        if not outcome.result_images:
            return outcome
        blocks: list[ContentBlock] = list(outcome.model_result.blocks)
        for number, image in enumerate(outcome.result_images[:MAX_RESULT_IMAGES], start=1):
            kept = (await self._images.keep(scope, call, image, number) if self._images is not None
                    else "[an image was returned but could not be kept]")
            blocks.append(TextBlock(kept) if isinstance(kept, str) else kept)
        extra = len(outcome.result_images) - MAX_RESULT_IMAGES
        if extra > 0:
            blocks.append(TextBlock(f"[{extra} more images were returned; only the first {MAX_RESULT_IMAGES} are kept]"))
        return replace(outcome, model_result=replace(outcome.model_result, blocks=tuple(blocks)), result_images=())

    async def _commit(
        self, scope: TrustedScope, call: CallRef, outcome: ToolOutcome, record_event: bool
    ) -> None:
        outcome = await self._keep_images(scope, call, outcome)
        ran = outcome.status in (ToolStatus.SUCCEEDED, ToolStatus.FAILED)
        completion = ToolCompletion(
            status=_STATUS[outcome.status],
            model_result=outcome.model_result,
            raw_result=outcome.raw_result,
            artifact_mutations=outcome.artifact_mutations,
            record_event=record_event,
            app_view=outcome.app_view if ran else None,  # only a call that ran has a view (V3-P58)
            presented_files=outcome.presented_files if outcome.status is ToolStatus.SUCCEEDED else (),
        )
        try:
            await self._log.commit_tool_outcome(scope, call, completion)
            return
        except DataError as first:
            error = first
        if completion.app_view is not None and error.code is DataErrorCode.INVALID_ARGUMENT:
            # A view Data refuses must not cost the call its definite result:
            # the commit is atomic, so commit the result without it.
            completion = replace(completion, app_view=None)
            try:
                await self._log.commit_tool_outcome(scope, call, completion)
                return
            except DataError as again:
                error = again
        if completion.presented_files and error.code in _FILES_REFUSED:
            # V3-P66 §7.1 (review B3): the result says the files were given;
            # without them it would be a false success, so the call fails.
            await self._commit_unpresented(scope, call, completion, error)
            return
        raise error

    async def _commit_unpresented(self, scope: TrustedScope, call: CallRef, completion: ToolCompletion,
                                  error: DataError) -> None:
        reason = _FILES_REFUSED[error.code]
        failed = replace(
            completion, status=ToolExecutionStatus.FAILED, presented_files=(), artifact_mutations=(),
            model_result=ModelToolResult((TextBlock(f"The file could not be kept for the user ({reason}). "
                                                    "Nothing was presented."),), is_error=True),
            raw_result={"presented": False, "refused": error.code.value})
        try:
            await self._log.commit_tool_outcome(scope, call, failed)
        except DataError as again:
            if failed.app_view is None or again.code is not DataErrorCode.INVALID_ARGUMENT:
                raise
            await self._log.commit_tool_outcome(scope, call, replace(failed, app_view=None))

    @staticmethod
    def _group_state(run_id: RunId, records: Sequence[ToolExecutionRecord]) -> GroupDispatchResult:
        uncertain = tuple(r.call for r in records if _blocks(r))
        if uncertain:
            return GroupDispatchResult(run_id, GroupState.BLOCKED_UNKNOWN, unknown_calls=uncertain)
        pending = [r for r in records if r.status is ToolExecutionStatus.PENDING]
        if pending:
            # Only a suspending call can remain PENDING after dispatch (V3-P15: it is alone).
            return GroupDispatchResult(run_id, GroupState.WAITING_FOR_USER, waiting_call=pending[0].call)
        return GroupDispatchResult(run_id, GroupState.COMPLETED)
