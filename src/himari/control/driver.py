"""Conversation driver: the P0 Run loop (CTRL-01, CPP §3).

The per-Conversation ``asyncio.Lock`` only prevents two drivers inside this
live process. Crash duplication is prevented by Data's persisted marks
(``mark_dispatch`` before send, Dispatcher claims before effects) and by
``recover_interrupted`` at the start of every drive (V3-045).

Context budget (PROMPT-04): with a ``ContextBudget``, every Run's input is
estimated first. At ``compact_at`` the older history is summarized by one
tool-less COMPACTION Run (raw request/response kept like any Run) and the
summary is committed as the latest snapshot; the input is then composed
again. A summarizing request is itself never sent over ``limit_tokens``;
when the whole prefix does not fit one, it is folded in several
compactions (at most ``MAX_COMPACTIONS_PER_RUN`` before a Run), and a
part too large for one request is summarized in a chain of calls. If
compaction fails, the Run still goes ahead when its input fits the
budget's ``ceiling_tokens`` (the model's window); an input estimated over
it is never sent (REQUEST_REJECTED).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
from collections.abc import AsyncIterator

from himari.common import ASK_USER_TOOL_ID, CallRef, ConversationId, RunId, TrustedScope
from himari.control.api import ContextStatus, ControlError, ControlErrorCode, DriveResult, DriveState
from himari.data.api import (
    AssistantFinal,
    DataError,
    DataErrorCode,
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
    RunPurpose,
    ToolExecutionStatus,
    ToolResultRecord,
    TriggerRef,
    UserMessage,
)
from himari.prompt.api import (
    CompactionPlan,
    CompactionPlanner,
    ContextBudget,
    ModelInput,
    PromptComposer,
    PromptContextLoader,
    PromptError,
    PromptErrorCode,
    PromptInputs,
    RequestOptions,
)
from himari.provider.api import (
    NativeNoResponse,
    NativeReceived,
    NativeUncertain,
    NormalizedFinal,
    NormalizedModelOutput,
    ProviderAdapter,
    ProviderError,
)
from himari.tool.api import GroupState, ToolDispatcher


MAX_COMPACTIONS_PER_RUN = 4


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
        planner: CompactionPlanner | None = None,
        budget: ContextBudget | None = None,
    ) -> None:
        if (planner is None) != (budget is None):
            raise ValueError("a context budget needs a compaction planner and vice versa")
        self._log = log
        self._loader = loader
        self._composer = composer
        self._provider = provider
        self._dispatcher = dispatcher
        self._options = dataclasses.replace(request_options, provider_key=provider.provider_key)
        self._max_runs = max_runs_per_drive
        self._planner = planner
        self._budget = budget
        self._locks: dict[ConversationId, asyncio.Lock] = {}

    async def drive(self, scope: TrustedScope, message: UserMessage) -> DriveResult:
        async with self._exclusive(scope):
            return await self._drive_locked(scope, message)

    async def resume(self, scope: TrustedScope) -> DriveResult:
        async with self._exclusive(scope):
            return await self._resume_locked(scope)

    async def context_status(self, scope: TrustedScope) -> ContextStatus | None:
        """CTRL-07: the size estimate the next Run would start from, and the
        latest compaction. Read-only; no lock (display may be a moment stale)."""
        if self._planner is None or self._budget is None:
            return None
        inputs = await self._loader.load(scope, (), self._options)
        summary = self._planner.summary_of(inputs.snapshot)
        snapshot = inputs.snapshot if summary is not None else None
        tokens: int | None = None
        measured = False
        try:
            estimate = self._planner.estimate(inputs, self._composer.compose(inputs))
            tokens, measured = estimate.tokens, estimate.measured
        except PromptError:
            pass  # e.g. a Tool group still running: no size to show now
        budget = self._budget
        return ContextStatus(
            estimated_tokens=tokens,
            measured=measured,
            budget_tokens=budget.limit_tokens,
            compact_at_tokens=int(budget.compact_at * budget.limit_tokens),
            hard_limit_tokens=budget.ceiling_tokens,
            summary=summary,
            covered_through_sequence=snapshot.covers_through_sequence if snapshot is not None else None,
            compactions=snapshot.revision if snapshot is not None else 0,
        )

    async def accept(self, scope: TrustedScope, message: UserMessage) -> None:
        """Record one user input durably — T7a if an ask_user call is waiting,
        else T1 — after restart cleanup and projection of stored responses,
        without running any Tool or calling the Provider. ``resume`` then
        continues from it (CTRL-06: an interface may report acceptance only
        once the input is committed)."""
        async with self._exclusive(scope):
            await self._prepare(scope)  # a stop is reported by the following resume
            waiting = await self._log.find_waiting_call(scope)
            if waiting is not None:
                await self._log.answer_pending_call(scope, waiting, message)  # T7a
            else:
                await self._log.append_event(scope, message)  # T1

    @contextlib.asynccontextmanager
    async def _exclusive(self, scope: TrustedScope) -> AsyncIterator[None]:
        lock = self._locks.setdefault(scope.conversation_id, asyncio.Lock())
        if lock.locked():
            raise ControlError(ControlErrorCode.BUSY, scope.conversation_id)
        async with lock:
            yield

    async def _drive_locked(self, scope: TrustedScope, message: UserMessage) -> DriveResult:
        recovery, stop = await self._prepare(scope)
        triggers: tuple[TriggerRef, ...]
        waiting = await self._log.find_waiting_call(scope)
        if waiting is not None:
            await self._log.answer_pending_call(scope, waiting, message)  # T7a
            triggers = (CompletedGroupRef(waiting.run_id),)
        else:
            blocked = await self._resume_pending_group(scope) if stop is None else None
            event = await self._log.append_event(scope, message)  # T1: input is never dropped
            if blocked is not None:
                return DriveResult(DriveState.RECOVERY_REQUIRED, unknown_calls=blocked, recovery=recovery)
            triggers = (EventRef(event.event_id),)
        if stop is not None:
            return stop
        return await self._run_loop(scope, triggers, recovery)

    async def _resume_locked(self, scope: TrustedScope) -> DriveResult:
        recovery, stop = await self._prepare(scope)
        if stop is not None:
            return stop
        waiting = await self._log.find_waiting_call(scope)
        if waiting is not None:
            return DriveResult(DriveState.WAITING_FOR_USER, waiting_call=waiting, recovery=recovery)
        blocked = await self._resume_pending_group(scope)
        if blocked is not None:
            return DriveResult(DriveState.RECOVERY_REQUIRED, unknown_calls=blocked, recovery=recovery)
        triggers = await self._next_triggers(scope)
        if not triggers:
            return DriveResult(DriveState.IDLE, recovery=recovery)
        return await self._run_loop(scope, triggers, recovery)

    async def _prepare(self, scope: TrustedScope) -> tuple[RecoveryReport, DriveResult | None]:
        """Restart cleanup (T-R), then project responses that were stored but
        not projected before a crash — re-normalizing the stored bytes, never
        calling the Provider again. Returns a stop result if one cannot be
        projected safely."""
        recovery = await self._log.recover_interrupted(scope)
        for run_id in recovery.runs_received_unprojected:
            problem = await self._project_stored_response(scope, run_id)
            if problem is not None:
                return recovery, DriveResult(DriveState.RECOVERY_REQUIRED, recovery=recovery, detail=problem)
        return recovery, None

    async def _project_stored_response(self, scope: TrustedScope, run_id: RunId) -> str | None:
        run = await self._log.read_run(scope, run_id)
        if run.provider_key != self._provider.provider_key:
            return f"{run_id}: stored {run.provider_key} response cannot be read by {self._provider.provider_key}"
        raw = await self._log.read_raw_response(scope, run_id)
        try:
            normalized = self._provider.normalize(NativeReceived(raw.body, raw.content_type, raw.usage))
        except ProviderError as exc:
            await self._log.record_projection_failure(scope, run_id, exc.code.value)
            return None  # like a live invalid output: raw kept, nothing enters history
        await self._project(scope, run_id, normalized)
        return None

    async def _project(
        self, scope: TrustedScope, run_id: RunId, normalized: NormalizedModelOutput
    ) -> ConversationEvent:
        if isinstance(normalized, NormalizedFinal):
            output: AssistantFinal | AssistantToolCalls = AssistantFinal(normalized.blocks)
        else:
            output = AssistantToolCalls(normalized.calls, normalized.commentary)
        projection = await self._log.project_model_output(
            scope, run_id, output, continuation=normalized.continuation
        )
        return projection.event

    async def _next_triggers(self, scope: TrustedScope) -> tuple[TriggerRef, ...]:
        """What the next Run would answer, from the persisted tail: the
        latest tool group (if the tail follows one) and every ordinary user
        message after the last assistant output — an input accepted before
        an unfinished group completed still triggers the Run. Empty if the
        last assistant output is a FINAL with nothing after it."""
        events = await self._log.read_events(scope)
        triggers: list[TriggerRef] = []
        for event in reversed(events):
            match event.payload:
                case AssistantFinal():
                    break
                case AssistantToolCalls() if event.origin_run_id is not None:
                    triggers.append(CompletedGroupRef(event.origin_run_id))
                    break
                case UserMessage(answers_call=None):
                    triggers.append(EventRef(event.event_id))
        return tuple(reversed(triggers))

    async def _resume_pending_group(self, scope: TrustedScope) -> tuple[CallRef, ...] | None:
        """Finish the latest group: run never-claimed PENDING calls (D20) and
        report UNKNOWN ones to the model (V3-047). Returns the uncertain calls
        if a REQUIRE_USER Tool keeps the group blocked."""
        events = await self._log.read_events(scope)
        last_group = next(
            (e for e in reversed(events) if e.kind is EventKind.ASSISTANT_TOOL_CALLS), None
        )
        if last_group is None or last_group.origin_run_id is None:
            return None
        run_id = last_group.origin_run_id
        records = await self._log.read_tool_executions(scope, run_id)
        unfinished = [
            r
            for r in records
            if r.status is ToolExecutionStatus.PENDING
            or (r.status is ToolExecutionStatus.UNKNOWN and r.model_result is None)
        ]
        if not unfinished or (len(records) == 1 and records[0].tool_id == ASK_USER_TOOL_ID):
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
        compactions: list[RunId] = []

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
                compaction_run_ids=tuple(compactions),
            )

        for _ in range(self._max_runs):
            try:
                inputs = await self._loader.load(scope, triggers, self._options)
                model_input = self._composer.compose(inputs)
                model_input, refusal = await self._fit_budget(scope, triggers, inputs, model_input, compactions)
            except PromptError as exc:
                state = (
                    DriveState.RECOVERY_REQUIRED
                    if exc.code is PromptErrorCode.INCOMPLETE_HISTORY
                    else DriveState.REQUEST_REJECTED
                )
                return result(state, detail=str(exc))
            if refusal is not None:
                return result(DriveState.REQUEST_REJECTED, detail=refusal)
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

            event = await self._project(scope, run_id, normalized)
            if isinstance(normalized, NormalizedFinal):
                return result(DriveState.COMPLETED, final_event=event)
            group = await self._dispatcher.dispatch_group(scope, run_id, normalized.calls)
            if group.state is GroupState.WAITING_FOR_USER:
                return result(DriveState.WAITING_FOR_USER, waiting_call=group.waiting_call)
            if group.state is GroupState.BLOCKED_UNKNOWN:
                return result(DriveState.RECOVERY_REQUIRED, unknown_calls=group.unknown_calls)
            triggers = (CompletedGroupRef(run_id),)

        return result(DriveState.RUN_LIMIT_REACHED)

    async def _fit_budget(
        self,
        scope: TrustedScope,
        triggers: tuple[TriggerRef, ...],
        inputs: PromptInputs,
        model_input: ModelInput,
        compactions: list[RunId],
    ) -> tuple[ModelInput, str | None]:
        """The input to send (after at most MAX_COMPACTIONS_PER_RUN
        compactions), or a refusal if it is still over the hard limit."""
        if self._planner is None or self._budget is None:
            return model_input, None
        decision = self._planner.decide(inputs, model_input, self._budget)
        failure = None
        for _ in range(MAX_COMPACTIONS_PER_RUN):
            if decision.compaction is None:
                break
            failure = await self._compact(scope, decision.compaction, self._planner, self._budget, compactions)
            if failure is not None:
                break
            inputs = await self._loader.load(scope, triggers, self._options)
            model_input = self._composer.compose(inputs)
            decision = self._planner.decide(inputs, model_input, self._budget)
        estimate, ceiling = decision.estimate, self._budget.ceiling_tokens
        if estimate.tokens > ceiling:
            detail = f"context_budget_exceeded: about {estimate.tokens} tokens > {ceiling}"
            if failure is not None:
                detail += f" (compaction failed: {failure})"
            elif decision.compaction is None:
                detail += " (no compaction fits within the limit)"
            return model_input, detail
        return model_input, None

    async def _compact(
        self,
        scope: TrustedScope,
        plan: CompactionPlan,
        planner: CompactionPlanner,
        budget: ContextBudget,
        compactions: list[RunId],
    ) -> str | None:
        """One compaction: a chain of summarizing calls, each a COMPACTION
        Run (appended to ``compactions``). Only the last summary is
        committed. Returns a failure, or None on success."""
        summary = plan.previous_summary
        last = len(plan.pieces) - 1
        for index in range(len(plan.pieces)):
            request = planner.step_request(plan, index, summary)
            size = planner.request_size(request)  # measured here, not taken from the planner
            if size > budget.limit_tokens:
                return f"compaction request of about {size} tokens exceeds the limit"
            run_id, failure, text = await self._summarize(scope, request, planner, plan)
            if run_id is not None:
                compactions.append(run_id)
            if failure is not None or run_id is None or text is None:
                return failure or "no summary"
            if index < last:
                await self._log.record_compaction_step(scope, run_id)
                summary = text
                continue
            try:
                await self._log.commit_compaction(
                    scope,
                    run_id,
                    expected_revision=plan.expected_revision,
                    schema_version=planner.snapshot_schema_version,
                    body=planner.snapshot_body(text),
                    covers_through_sequence=plan.covers_through_sequence,
                    basis=plan.prompt_basis,
                )
            except DataError as exc:
                if exc.code not in (DataErrorCode.VERSION_CONFLICT, DataErrorCode.STALE_BASIS):
                    raise
                await self._log.record_projection_failure(scope, run_id, exc.code.value)
                return str(exc)
        return None

    async def _summarize(
        self, scope: TrustedScope, request: ModelInput, planner: CompactionPlanner, plan: CompactionPlan
    ) -> tuple[RunId | None, str | None, str | None]:
        """One summarizing call as a COMPACTION Run: (Run, failure, summary)."""
        try:
            prepared = self._provider.prepare_native_request(request)
        except ProviderError as exc:
            return None, str(exc), None
        run_id = await self._log.prepare_run(
            scope,
            RunPreparation(
                raw_request_body=prepared.body,
                raw_content_type=prepared.content_type,
                raw_representation=prepared.raw_representation,
                trigger_refs=(),
                prompt_basis=request.prompt_basis,
                provider_key=self._provider.provider_key,
                model_id=self._options.model_id,
                purpose=RunPurpose.COMPACTION,
            ),
        )
        await self._log.mark_dispatch(scope, run_id)
        try:
            outcome = await self._provider.invoke_prepared(prepared)
        except Exception as exc:  # noqa: BLE001 — adapter error after send began
            outcome = NativeUncertain(type(exc).__name__)
        await self._log.record_raw_outcome(scope, run_id, _raw(outcome))
        if not isinstance(outcome, NativeReceived):
            return run_id, outcome.error_code, None  # no effects to reconcile: the next Run may try again
        try:
            normalized = self._provider.normalize(outcome)
            if not isinstance(normalized, NormalizedFinal):
                raise PromptError(PromptErrorCode.INVALID_SUMMARY, "the summary call returned tool calls")
            text = planner.summary_text(normalized.blocks, plan)
        except (ProviderError, PromptError) as exc:
            await self._log.record_projection_failure(scope, run_id, exc.code.value)
            return run_id, str(exc), None
        return run_id, None, text


def _raw(outcome: NativeReceived | NativeNoResponse | NativeUncertain) -> RawRunOutcome:
    if isinstance(outcome, NativeReceived):
        return RawReceived(outcome.body, outcome.content_type, outcome.usage)
    if isinstance(outcome, NativeNoResponse):
        return RawNoResponse(outcome.error_code, outcome.body, outcome.content_type)
    return RawUncertain(outcome.error_code)
