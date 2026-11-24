from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import ModuleType

from himari.common import (
    CallId,
    ConversationId,
    ModelToolResult,
    PrincipalId,
    TextBlock,
    ToolId,
    TrustedScope,
)
from himari.tool import api
from himari.tool.api import (
    EffectProfile,
    ToolContext,
    ToolDefinition,
    ToolHandler,
    ToolInvocation,
    ToolOutcome,
    ToolStatus,
)


def assert_handler_conforms(handler: object) -> None:
    """Minimal ToolHandler conformance; plugins reuse this (V3-040)."""
    assert isinstance(handler, ToolHandler)
    d = handler.definition
    assert d.tool_id
    if d.environment_kind is not None:
        assert d.tool_id.startswith(f"{d.environment_kind}/")
    else:
        assert "/" not in d.tool_id


class EchoTool:
    definition = ToolDefinition(
        tool_id=ToolId("echo"),
        description="Echo the text argument.",
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
        effect_profile=EffectProfile.READ,
    )

    async def run(self, invocation: ToolInvocation, ctx: ToolContext) -> ToolOutcome:
        text = invocation.arguments.get("text")
        return ToolOutcome(
            status=ToolStatus.SUCCEEDED,
            model_result=ModelToolResult(blocks=(TextBlock(str(text)),)),
        )


def test_dtos_are_frozen(assert_api_dataclasses_frozen: Callable[[ModuleType], None]) -> None:
    assert_api_dataclasses_frozen(api)


def test_example_handlers_conform() -> None:
    assert_handler_conforms(EchoTool())


def test_handler_returns_outcome_without_ledger_access() -> None:
    scope = TrustedScope(PrincipalId("p1"), ConversationId("c1"))
    ctx = ToolContext(scope=scope)
    outcome = asyncio.run(
        EchoTool().run(ToolInvocation(ToolId("echo"), CallId("call_1"), {"text": "hi"}), ctx)
    )
    assert outcome.status is ToolStatus.SUCCEEDED
    assert outcome.model_result.blocks == (TextBlock("hi"),)
    assert outcome.raw_result is None
