"""Prompt context loader: the only part of Prompt that does I/O (V3-043).

P0 does not call ``EnvironmentPlugin.observe`` yet; observations are empty, so
connection status renders as unknown rather than guessed.

``usage_mark`` is the input size the Provider reported for the latest
assistant output's Run (compaction Runs produce no Event and are skipped).
"""

from __future__ import annotations

from collections.abc import Sequence

from himari.common import ProviderContinuation, RunId, TrustedScope
from himari.data.api import ConversationLog, EventKind, StateQuery, ToolExecutionRecord, TriggerRef
from himari.prompt.api import PromptInputs, RequestOptions, UsageMark
from himari.tool.api import ToolCatalog


class ContextLoader:
    def __init__(
        self,
        log: ConversationLog,
        state: StateQuery,
        catalog: ToolCatalog,
        instructions: Sequence[str],
    ) -> None:
        self._log = log
        self._state = state
        self._catalog = catalog
        self._instructions = tuple(instructions)

    async def load(
        self,
        scope: TrustedScope,
        trigger_refs: Sequence[TriggerRef],
        request_options: RequestOptions,
    ) -> PromptInputs:
        events = tuple(await self._log.read_events(scope))
        executions: list[ToolExecutionRecord] = []
        continuations: dict[RunId, ProviderContinuation] = {}
        usage_mark: UsageMark | None = None
        for event in events:
            if event.origin_run_id is None:
                continue
            if event.kind is EventKind.ASSISTANT_TOOL_CALLS:
                executions.extend(await self._log.read_tool_executions(scope, event.origin_run_id))
            if event.kind in (EventKind.ASSISTANT_TOOL_CALLS, EventKind.ASSISTANT_FINAL):
                run = await self._log.read_run(scope, event.origin_run_id)
                if run.continuation is not None:
                    continuations[event.origin_run_id] = run.continuation
                if run.usage is not None and run.usage.input_tokens is not None:
                    used = run.prompt_basis
                    usage_mark = UsageMark(run.usage.input_tokens, used.event_sequence, used.snapshot_revision,
                                           used.context_key, used.state_revision, run.provider_key)
        basis = await self._state.read_current_state_basis(scope)
        active = frozenset(a.environment_kind for a in basis.environments)
        return PromptInputs(
            instructions=self._instructions,
            tool_catalog=tuple(self._catalog.catalog(active)),
            events=events,
            tool_executions=tuple(executions),
            snapshot=await self._log.get_latest_snapshot(scope),
            state_basis=basis,
            observations=(),
            request_options=request_options,
            trigger_refs=tuple(trigger_refs),
            continuations=continuations,
            usage_mark=usage_mark,
        )
