"""Pure prompt composition (PROMPT-01/03, V3-043, CPP §6.1).

No I/O, no clock, no randomness: same ``PromptInputs`` → same ``ModelInput``.
With a compaction snapshot (``compaction.read_summary``) the history is its
summary followed by every part that starts after the coverage; without one
the whole history is exact. Either way every ToolCall group is checked for
complete results, folded or not.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from himari.common import CallId, CallRef, ModelToolResult, RunId, TextBlock
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
    CompressedHistory,
    CurrentStateProjection,
    HistoryPart,
    ModelInput,
    PromptError,
    PromptErrorCode,
    PromptInputs,
    ToolGroupTurn,
    UserTurn,
)
from himari.prompt.compaction import anchors, context_key, part_anchor, read_summary


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
                    continuation = inputs.continuations.get(event.origin_run_id) if event.origin_run_id else None
                    history.append(AssistantTurn(event.event_id, blocks, continuation))
                case AssistantToolCalls():
                    if event.origin_run_id is None:
                        raise PromptError(PromptErrorCode.INCOMPLETE_HISTORY, f"{event.event_id} has no run")
                    group = self._group(event.origin_run_id, event.payload, executions)
                    history.append(replace(group, continuation=inputs.continuations.get(event.origin_run_id)))
                case ToolResultRecord():
                    pass  # rendered inside its group, in ordinal order
        summary = read_summary(inputs.snapshot)
        snapshot_revision = None
        if summary is not None and inputs.snapshot is not None:
            covers = inputs.snapshot.covers_through_sequence
            by_event, by_run = anchors(inputs.events)
            tail = [p for p in history if part_anchor(p, by_event, by_run) > covers]
            history = [CompressedHistory((TextBlock(summary),), covers), *tail]
            snapshot_revision = inputs.snapshot.revision
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
                snapshot_revision=snapshot_revision,
                artifact_revisions={a.artifact_id: a.revision for a in basis.artifacts},
                context_key=context_key(inputs),
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
