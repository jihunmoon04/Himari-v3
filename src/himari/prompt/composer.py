"""Pure prompt composition (PROMPT-01/03, V3-043, CPP §6.1).

No I/O, no clock, no randomness: same ``PromptInputs`` → same ``ModelInput``.
P0 has no compaction yet, so the whole history is the exact tail and the
snapshot is not consulted.
"""

from __future__ import annotations

from collections.abc import Mapping

from himari.common import CallId, CallRef, ModelToolResult, RunId
from himari.data.api import (
    AssistantFinal,
    AssistantToolCalls,
    PromptBasis,
    ToolExecutionRecord,
    ToolResultRecord,
    UserMessage,
)
from himari.prompt.api import (
    AssistantTurn,
    CurrentStateProjection,
    HistoryPart,
    ModelInput,
    PromptError,
    PromptErrorCode,
    PromptInputs,
    ToolGroupTurn,
    UserTurn,
)


class Composer:
    def compose(self, inputs: PromptInputs) -> ModelInput:
        executions: Mapping[CallRef, ToolExecutionRecord] = {
            e.call: e for e in inputs.tool_executions
        }
        history: list[HistoryPart] = []
        for event in sorted(inputs.events, key=lambda e: e.sequence):
            match event.payload:
                case UserMessage(answers_call=None, blocks=blocks):
                    history.append(UserTurn(event.event_id, blocks))
                case UserMessage():
                    pass  # rendered once, as its ask_user call's result (V3-P18)
                case AssistantFinal(blocks=blocks):
                    history.append(AssistantTurn(event.event_id, blocks))
                case AssistantToolCalls():
                    if event.origin_run_id is None:
                        raise PromptError(PromptErrorCode.INCOMPLETE_HISTORY, f"{event.event_id} has no run")
                    history.append(self._group(event.origin_run_id, event.payload, executions))
                case ToolResultRecord():
                    pass  # rendered inside its group, in ordinal order
        basis = inputs.state_basis
        return ModelInput(
            stable_prefix=inputs.instructions,
            tool_catalog=inputs.tool_catalog,
            conversation=tuple(history),
            current_state=CurrentStateProjection(basis=basis, observations=inputs.observations),
            request_options=inputs.request_options,
            trigger_refs=inputs.trigger_refs,
            prompt_basis=PromptBasis(
                event_sequence=max((e.sequence for e in inputs.events), default=0),
                state_revision=basis.state_revision,
                artifact_revisions={a.artifact_id: a.revision for a in basis.artifacts},
            ),
        )

    @staticmethod
    def _group(
        run_id: RunId,
        payload: AssistantToolCalls,
        executions: Mapping[CallRef, ToolExecutionRecord],
    ) -> ToolGroupTurn:
        calls = tuple(sorted(payload.calls, key=lambda c: c.ordinal))
        results: dict[CallId, ModelToolResult] = {}
        for call in calls:
            record = executions.get(CallRef(run_id, call.call_id))
            if record is None or record.model_result is None:
                status = "missing" if record is None else record.status.value
                raise PromptError(
                    PromptErrorCode.INCOMPLETE_HISTORY, f"{run_id}/{call.call_id} is {status}"
                )
            results[call.call_id] = record.model_result
        return ToolGroupTurn(run_id, payload.commentary, calls, results)
