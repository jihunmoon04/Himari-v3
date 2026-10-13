"""Prompt context loader: the only part of Prompt that does I/O (V3-043).

P0 does not call ``EnvironmentPlugin.observe`` yet; observations are empty, so
connection status renders as unknown rather than guessed.
"""

from __future__ import annotations

from collections.abc import Sequence

from himari.common import TrustedScope
from himari.data.api import ConversationLog, EventKind, StateQuery, ToolExecutionRecord, TriggerRef
from himari.prompt.api import PromptInputs, RequestOptions
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
        for event in events:
            if event.kind is EventKind.ASSISTANT_TOOL_CALLS and event.origin_run_id is not None:
                executions.extend(await self._log.read_tool_executions(scope, event.origin_run_id))
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
        )
