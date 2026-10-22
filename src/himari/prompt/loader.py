"""Prompt context loader: the only part of Prompt that does I/O (V3-043).

P0 does not call ``EnvironmentPlugin.observe`` yet; observations are empty, so
connection status renders as unknown rather than guessed.

With a readable compaction snapshot only the Events after its coverage are
read, with the Runs and Tool records of those Events (Q-42): the composer
drops every part that starts at or before the coverage anyway, so the cost
of a request follows the exact tail, not the length of the Conversation.
A folded ToolCall group is not read again; it was complete when folded, and
a terminal result never changes. The snapshot is read first, so a
compaction committed meanwhile only leaves the Events read a superset of
what that snapshot needs.

``usage_mark`` is the input size the Provider reported for the latest
assistant output's Run (compaction Runs produce no Event and are skipped).
A Run folded into the snapshot never yields a usable one: it was composed
before that snapshot existed, under another snapshot revision.

Attachments (FILE-05, V3-P66 §6): the file records of every attachment the
loaded Events and Tool results refer to, read in one call.
"""

from __future__ import annotations

from collections.abc import Sequence

from himari.common import AttachmentRef, ContentBlock, FileId, ProviderContinuation, RunId, TrustedScope
from himari.data.api import (
    ConversationEvent,
    ConversationLog,
    EventKind,
    FileCatalog,
    FileRecord,
    StateQuery,
    ToolExecutionRecord,
    TriggerRef,
    view_state,
)
from himari.prompt.api import AttachmentInfo, PromptInputs, RequestOptions, SourceNote, UsageMark, attachment_key
from himari.prompt.compaction import read_summary
from himari.tool.api import ExternalAccess, ToolCatalog


class ContextLoader:
    def __init__(
        self,
        log: ConversationLog,
        state: StateQuery,
        catalog: ToolCatalog,
        instructions: Sequence[str],
        external_access: ExternalAccess | None = None,  # None: no External Tools in the catalog
        source_notes: Sequence[SourceNote] = (),  # every server's notes; only enabled ones are used (V3-P60)
        files: FileCatalog | None = None,  # None: attachments render as unavailable (V3-P66)
    ) -> None:
        self._files = files
        self._log = log
        self._state = state
        self._catalog = catalog
        self._instructions = tuple(instructions)
        self._external_access = external_access
        self._source_notes = tuple(sorted(source_notes, key=lambda n: n.source))

    async def load(
        self,
        scope: TrustedScope,
        trigger_refs: Sequence[TriggerRef],
        request_options: RequestOptions,
    ) -> PromptInputs:
        snapshot = await self._log.get_latest_snapshot(scope)
        events: tuple[ConversationEvent, ...] = ()
        if snapshot is not None and read_summary(snapshot) is not None:
            events = tuple(await self._log.read_events(scope, after_sequence=snapshot.covers_through_sequence))
            if not events:
                # Data lets a summary cover the last Event; until the next one
                # arrives the composer needs the whole history for the
                # latest sequence its basis records.
                events = tuple(await self._log.read_events(scope))
        else:
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
        attachments = await self._attachments(scope, events, executions)
        basis = await self._state.read_current_state_basis(scope)
        active = frozenset(a.environment_kind for a in basis.environments)
        enabled = frozenset() if self._external_access is None else await self._external_access.enabled_sources(scope)
        return PromptInputs(
            instructions=self._instructions,
            tool_catalog=tuple(self._catalog.catalog(active, enabled)),
            events=events,
            tool_executions=tuple(executions),
            snapshot=snapshot,
            state_basis=basis,
            observations=(),
            request_options=request_options,
            trigger_refs=tuple(trigger_refs),
            continuations=continuations,
            usage_mark=usage_mark,
            source_notes=tuple(n for n in self._source_notes if n.source in enabled),
            attachments=attachments,
        )

    async def _attachments(self, scope: TrustedScope, events: Sequence[ConversationEvent],
                           executions: Sequence[ToolExecutionRecord]) -> dict[str, AttachmentInfo]:
        blocks: list[ContentBlock] = []
        for event in events:
            blocks.extend(getattr(event.payload, "blocks", ()))
            blocks.extend(getattr(event.payload, "commentary", ()))
        for execution in executions:
            if execution.model_result is not None:
                blocks.extend(execution.model_result.blocks)
        refs = [(FileId(b.file_id), b.version) for b in blocks if isinstance(b, AttachmentRef)]
        if not refs or self._files is None:
            return {}
        records = await self._files.read_file_records(scope, refs)
        return {attachment_key(AttachmentRef(r.file_id, r.version)): _info(r) for r in records}


def _info(record: FileRecord) -> AttachmentInfo:
    view = record.view
    return AttachmentInfo(record.name, record.workspace_path, record.media_type, record.size,
                          view_state=view_state(record).value,
                          view_key=None if view is None else view.blob.key,
                          view_media_type=None if view is None else view.media_type,
                          view_width=0 if view is None else view.width, view_height=0 if view is None else view.height,
                          view_bytes=0 if view is None else view.blob.size)
