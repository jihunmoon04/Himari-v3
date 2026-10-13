"""Conversation driver: the P0 Run loop (CTRL-01, CPP §3).

The per-Conversation ``asyncio.Lock`` only prevents two drivers inside this
live process. Crash duplication is prevented by Data's persisted marks
(``mark_dispatch`` before send, Dispatcher claims before effects) and by
``recover_interrupted`` at the start of every drive (V3-045).
"""

from __future__ import annotations

import asyncio

from himari.common import ASK_USER_TOOL_ID, CallRef, ConversationId, RunId, TrustedScope
from himari.control.api import ControlError, ControlErrorCode, DriveResult, DriveState
from himari.data.api import (
    AssistantFinal,
    ConversationEvent,
    AssistantToolCalls,
    CompletedGroupRef,
    ConversationLog,
    EventKind,
    EventRef,
    RawNoResponse,
    RawReceived,
    RawRunOutcome,
    RawUncertain,
    RecoveryReport,
    RunPreparation,
    ToolExecutionStatus,
    TriggerRef,
    UserMessage,
)
from himari.prompt.api import (
    PromptComposer,
    PromptContextLoader,
    PromptError,
    PromptErrorCode,
    RequestOptions,
)
from himari.provider.api import (
    NativeNoResponse,
    NativeReceived,
    NativeUncertain,
    NormalizedFinal,
    ProviderAdapter,
    ProviderError,
)
from himari.tool.api import GroupState, ToolDispatcher


class Driver:
    def __init__(
        self,
        log: ConversationLog,
        loader: PromptContextLoader,
        composer: PromptComposer,
        provider: ProviderAdapter,
        dispatcher: ToolDispatcher,
        request_options: RequestOptions,
        *,
        max_runs_per_drive: int = 16,
    ) -> None:
        self._log = log
        self._loader = loader
        self._composer = composer
        self._provider = provider
        self._dispatcher = dispatcher
        self._options = request_options
        self._max_runs = max_runs_per_drive
        self._locks: dict[ConversationId, asyncio.Lock] = {}

    async def drive(self, scope: TrustedScope, message: UserMessage) -> DriveResult:
        lock = self._locks.setdefault(scope.conversation_id, asyncio.Lock())
        if lock.locked():
            raise ControlError(ControlErrorCode.BUSY, scope.conversation_id)
        async with lock:
            return await self._drive_locked(scope, message)

    async def _drive_locked(self, scope: TrustedScope, message: UserMessage) -> DriveResult:
        recovery = await self._log.recover_interrupted(scope)
        triggers: tuple[TriggerRef, ...]
        waiting = await self._log.find_waiting_call(scope)
        if waiting is not None:
            await self._log.answer_pending_call(scope, waiting, message)  # T7a
            triggers = (CompletedGroupRef(waiting.run_id),)
        else:
            blocked = await self._resume_pending_group(scope)
            event = await self._log.append_event(scope, message)  # T1
            if blocked is not None:
                return DriveResult(
                    DriveState.RECOVERY_REQUIRED, unknown_calls=blocked, recovery=recovery
                )
            triggers = (EventRef(event.event_id),)
        return await self._run_loop(scope, triggers, recovery)

    async def _resume_pending_group(self, scope: TrustedScope) -> tuple[CallRef, ...] | None:
        """Finish never-claimed PENDING calls of the latest group (D20).
        Returns the uncertain calls if the group is blocked."""
        events = await self._log.read_events(scope)
        last_group = next(
            (e for e in reversed(events) if e.kind is EventKind.ASSISTANT_TOOL_CALLS), None
        )
        if last_group is None or last_group.origin_run_id is None:
            return None
        run_id = last_group.origin_run_id
        records = await self._log.read_tool_executions(scope, run_id)
        statuses = {r.status for r in records}
        if ToolExecutionStatus.UNKNOWN in statuses:
            return tuple(r.call for r in records if r.status is ToolExecutionStatus.UNKNOWN)
        pending = [r for r in records if r.status is ToolExecutionStatus.PENDING]
        if not pending or (len(records) == 1 and records[0].tool_id == ASK_USER_TOOL_ID):
            return None
        if not isinstance(last_group.payload, AssistantToolCalls):
            return None
        result = await self._dispatcher.dispatch_group(scope, run_id, last_group.payload.calls)
        if result.state is GroupState.BLOCKED_UNKNOWN:
            return result.unknown_calls
        return None

    async def _run_loop(
        self, scope: TrustedScope, triggers: tuple[TriggerRef, ...], recovery: RecoveryReport
    ) -> DriveResult:
        run_ids: list[RunId] = []

        def result(
            state: DriveState,
            *,
            final_event: ConversationEvent | None = None,
            waiting_call: CallRef | None = None,
            unknown_calls: tuple[CallRef, ...] = (),
            detail: str = "",
        ) -> DriveResult:
            return DriveResult(
                state,
                run_ids=tuple(run_ids),
                final_event=final_event,
                waiting_call=waiting_call,
                unknown_calls=unknown_calls,
                recovery=recovery,
                detail=detail,
            )

        for _ in range(self._max_runs):
            try:
                inputs = await self._loader.load(scope, triggers, self._options)
                model_input = self._composer.compose(inputs)
            except PromptError as exc:
                state = (
                    DriveState.RECOVERY_REQUIRED
                    if exc.code is PromptErrorCode.INCOMPLETE_HISTORY
                    else DriveState.REQUEST_REJECTED
                )
                return result(state, detail=str(exc))
            try:
                prepared = self._provider.prepare_native_request(model_input)
            except ProviderError as exc:
                return result(DriveState.REQUEST_REJECTED, detail=str(exc))

            run_id = await self._log.prepare_run(
                scope,
                RunPreparation(
                    raw_request_body=prepared.body,
                    raw_content_type=prepared.content_type,
                    raw_representation=prepared.raw_representation,
                    trigger_refs=triggers,
                    prompt_basis=model_input.prompt_basis,
                    provider_key=self._provider.provider_key,
                    model_id=self._options.model_id,
                ),
            )
            run_ids.append(run_id)
            await self._log.mark_dispatch(scope, run_id)  # T3: persisted before the one send
            try:
                outcome = await self._provider.invoke_prepared(prepared)
            except Exception as exc:  # noqa: BLE001 — adapter error after send began
                outcome = NativeUncertain(type(exc).__name__)
            await self._log.record_raw_outcome(scope, run_id, _raw(outcome))
            if isinstance(outcome, NativeNoResponse):
                return result(DriveState.PROVIDER_FAILED, detail=outcome.error_code)
            if isinstance(outcome, NativeUncertain):
                return result(DriveState.OUTCOME_UNKNOWN, detail=outcome.error_code)

            try:
                normalized = self._provider.normalize(outcome)
            except ProviderError as exc:
                await self._log.record_projection_failure(scope, run_id, exc.code.value)
                return result(DriveState.INVALID_MODEL_OUTPUT, detail=str(exc))

            if isinstance(normalized, NormalizedFinal):
                projection = await self._log.project_model_output(
                    scope, run_id, AssistantFinal(normalized.blocks)
                )
                return result(DriveState.COMPLETED, final_event=projection.event)

            await self._log.project_model_output(
                scope, run_id, AssistantToolCalls(normalized.calls, normalized.commentary)
            )
            group = await self._dispatcher.dispatch_group(scope, run_id, normalized.calls)
            if group.state is GroupState.WAITING_FOR_USER:
                return result(DriveState.WAITING_FOR_USER, waiting_call=group.waiting_call)
            if group.state is GroupState.BLOCKED_UNKNOWN:
                return result(DriveState.RECOVERY_REQUIRED, unknown_calls=group.unknown_calls)
            triggers = (CompletedGroupRef(run_id),)

        return result(DriveState.RUN_LIMIT_REACHED)


def _raw(outcome: NativeReceived | NativeNoResponse | NativeUncertain) -> RawRunOutcome:
    if isinstance(outcome, NativeReceived):
        return RawReceived(outcome.body, outcome.content_type, outcome.usage)
    if isinstance(outcome, NativeNoResponse):
        return RawNoResponse(outcome.error_code)
    return RawUncertain(outcome.error_code)
