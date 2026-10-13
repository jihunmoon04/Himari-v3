from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from types import ModuleType

from himari.common import EnvironmentKind, ModelToolResult, TextBlock, ToolId, TrustedScope
from himari.data.api import BindingAuthority
from himari.environment import api
from himari.environment.api import BindingObservation, ConnectionStatus, EnvironmentPlugin
from himari.tool.api import (
    EffectProfile,
    ToolContext,
    ToolDefinition,
    ToolHandler,
    ToolInvocation,
    ToolOutcome,
    ToolStatus,
    WorkspaceRequirement,
)


def assert_plugin_conforms(plugin: object) -> None:
    """Minimal EnvironmentPlugin conformance; plugins reuse this (V3-040)."""
    assert isinstance(plugin, EnvironmentPlugin)
    tools = list(plugin.tools())
    assert tools, "an Environment provides at least one Tool"
    ids = [t.definition.tool_id for t in tools]
    assert len(set(ids)) == len(ids), "tool ids are unique"
    for t in tools:
        assert isinstance(t, ToolHandler)
        assert t.definition.environment_kind == plugin.kind
        assert t.definition.tool_id.startswith(f"{plugin.kind}/")
    assert [t.definition.tool_id for t in plugin.tools()] == ids, "tools() is fixed"


class _ListFiles:
    definition = ToolDefinition(
        tool_id=ToolId("fake/list_files"),
        description="List files in the bound Workspace.",
        input_schema={"type": "object"},
        workspace_requirement=WorkspaceRequirement.DEFAULT,
        effect_profile=EffectProfile.READ,
        environment_kind=EnvironmentKind("fake"),
    )

    async def run(self, invocation: ToolInvocation, ctx: ToolContext) -> ToolOutcome:
        assert ctx.binding is not None
        return ToolOutcome(ToolStatus.SUCCEEDED, ModelToolResult((TextBlock(""),)))


class FakeEnvironment:
    def __init__(self) -> None:
        self._tools = (_ListFiles(),)

    @property
    def kind(self) -> EnvironmentKind:
        return EnvironmentKind("fake")

    def tools(self) -> Sequence[ToolHandler]:
        return self._tools

    async def observe(
        self, scope: TrustedScope, bindings: Sequence[BindingAuthority]
    ) -> Sequence[BindingObservation]:
        now = datetime.now(UTC)
        return [BindingObservation(b.binding_id, ConnectionStatus.UNKNOWN, now) for b in bindings]


def test_dtos_are_frozen(assert_api_dataclasses_frozen: Callable[[ModuleType], None]) -> None:
    assert_api_dataclasses_frozen(api)


def test_fake_environment_conforms() -> None:
    assert_plugin_conforms(FakeEnvironment())
