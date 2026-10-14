"""Tool registry: implements ``ToolCatalog`` (TOOL-01)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from himari.common import ASK_USER_TOOL_ID, EnvironmentKind, ToolId, UnknownOutcomePolicy, tool_id_problem
from himari.tool.api import Completion, ToolDefinition, ToolHandler, ToolOrigin, WorkspaceRequirement
from himari.tool.schema import check_tool_input_schema


class ToolRegistry:
    def __init__(self, handlers: Iterable[ToolHandler] = (), *, allow_require_user: bool = False) -> None:
        """``allow_require_user`` stays False in P0: there is no user-resolution
        API yet, so a REQUIRE_USER Tool could block a Conversation forever."""
        self._allow_require_user = allow_require_user
        self._handlers: dict[ToolId, ToolHandler] = {}
        self._namespaces: dict[str, ToolOrigin] = {}  # id prefix → the origin of the Tools using it
        for handler in handlers:
            self.register(handler)

    def register(self, handler: ToolHandler) -> None:
        d = handler.definition
        if d.tool_id in self._handlers:
            raise ValueError(f"duplicate tool_id {d.tool_id}")
        problem = tool_id_problem(d.tool_id)
        if problem is not None:  # every Provider must be able to name it (V3-P52)
            raise ValueError(f"{d.tool_id!r}: tool id {problem}")
        if d.environment_kind is not None and d.external_source is not None:
            raise ValueError(f"{d.tool_id}: a Tool is Environment-provided or External, not both")
        namespace = d.environment_kind if d.environment_kind is not None else d.external_source
        if (namespace is None) == ("/" in d.tool_id):
            raise ValueError(f"{d.tool_id}: '<namespace>/<name>' iff Environment-provided or External")
        if namespace is not None:
            if not d.tool_id.startswith(f"{namespace}/"):
                raise ValueError(f"{d.tool_id}: prefix must be its environment_kind or external_source {namespace}")
            owner = self._namespaces.get(namespace)
            if owner is not None and owner is not d.origin:
                raise ValueError(f"{d.tool_id}: namespace {namespace} already belongs to {owner.value} Tools")
        if d.origin is ToolOrigin.EXTERNAL and d.workspace_requirement is not WorkspaceRequirement.NONE:
            raise ValueError(f"{d.tool_id}: an External Tool cannot require a Workspace")
        if d.completion is Completion.SUSPENDING and d.tool_id != ASK_USER_TOOL_ID:
            raise ValueError(f"{d.tool_id}: only ask_user may be suspending (P0)")
        if d.unknown_outcome is UnknownOutcomePolicy.REQUIRE_USER and not self._allow_require_user:
            raise ValueError(f"{d.tool_id}: REQUIRE_USER Tools are disabled (no user-resolution API in P0)")
        check_tool_input_schema(d.input_schema)
        self._handlers[d.tool_id] = handler
        if namespace is not None:
            self._namespaces[namespace] = d.origin

    def get(self, tool_id: ToolId) -> ToolHandler | None:
        return self._handlers.get(tool_id)

    def external_sources(self) -> frozenset[str]:
        return frozenset(h.definition.external_source for h in self._handlers.values()
                         if h.definition.external_source is not None)

    def catalog(
        self, active_kinds: frozenset[EnvironmentKind], enabled_sources: frozenset[str] = frozenset()
    ) -> Sequence[ToolDefinition]:
        visible = [
            d
            for d in (h.definition for h in self._handlers.values())
            if (d.environment_kind is None or d.environment_kind in active_kinds)
            and (d.external_source is None or d.external_source in enabled_sources)
        ]
        return tuple(sorted(visible, key=lambda d: d.tool_id))
