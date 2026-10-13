"""Tool registry: implements ``ToolCatalog`` (TOOL-01)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from himari.common import ASK_USER_TOOL_ID, EnvironmentKind, ToolId
from himari.tool.api import Completion, ToolDefinition, ToolHandler
from himari.tool.schema import check_schema


class ToolRegistry:
    def __init__(self, handlers: Iterable[ToolHandler] = ()) -> None:
        self._handlers: dict[ToolId, ToolHandler] = {}
        for handler in handlers:
            self.register(handler)

    def register(self, handler: ToolHandler) -> None:
        d = handler.definition
        if d.tool_id in self._handlers:
            raise ValueError(f"duplicate tool_id {d.tool_id}")
        if (d.environment_kind is None) == ("/" in d.tool_id):
            raise ValueError(f"{d.tool_id}: '<kind>/<name>' iff Environment-provided")
        if d.environment_kind is not None and not d.tool_id.startswith(f"{d.environment_kind}/"):
            raise ValueError(f"{d.tool_id}: prefix must be its environment_kind {d.environment_kind}")
        if d.completion is Completion.SUSPENDING and d.tool_id != ASK_USER_TOOL_ID:
            raise ValueError(f"{d.tool_id}: only ask_user may be suspending (P0)")
        check_schema(d.input_schema)
        self._handlers[d.tool_id] = handler

    def get(self, tool_id: ToolId) -> ToolHandler | None:
        return self._handlers.get(tool_id)

    def catalog(self, active_kinds: frozenset[EnvironmentKind]) -> Sequence[ToolDefinition]:
        visible = [
            h.definition
            for h in self._handlers.values()
            if h.definition.environment_kind is None or h.definition.environment_kind in active_kinds
        ]
        return tuple(sorted(visible, key=lambda d: d.tool_id))
