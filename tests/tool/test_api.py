from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from types import ModuleType

from himari.common import (
    ASK_USER_TOOL_ID,
    ArtifactId,
    CallId,
    ConversationId,
    ModelToolResult,
    PrincipalId,
    TextBlock,
    ToolId,
    TrustedScope,
)
from himari.data.api import Artifact
from himari.tool import api
from himari.tool.api import (
    Completion,
    EffectProfile,
    ToolContext,
    ToolDefinition,
    ToolHandler,
    ToolInvocation,
    ToolOutcome,
    ToolStatus,
    WorkspaceRequirement,
)


def assert_handler_conforms(handler: object) -> None:
    """Minimal ToolHandler conformance; plugins reuse this (V3-040)."""
    assert isinstance(handler, ToolHandler)
    d = handler.definition
    assert d.tool_id
    if d.completion is Completion.SUSPENDING:
        assert d.tool_id == ASK_USER_TOOL_ID, "P0: only ask_user may suspend"
        assert d.effect_profile is EffectProfile.READ
    if d.environment_kind is not None:
        assert d.tool_id.startswith(f"{d.environment_kind}/")
    else:
        assert "/" not in d.tool_id
    if d.workspace_requirement is not WorkspaceRequirement.NONE:
        assert d.environment_kind is not None, "workspace Tools are Environment-provided"


class _NoArtifacts:
    async def read_artifact(self, scope: TrustedScope, artifact_id: ArtifactId) -> Artifact:
        raise KeyError(artifact_id)

    async def list_artifacts(
        self, scope: TrustedScope, *, kind: str | None = None
    ) -> Sequence[Artifact]:
        return ()


class EchoTool:
    definition = ToolDefinition(
        tool_id=ToolId("echo"),
        description="Echo the text argument.",
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
        workspace_requirement=WorkspaceRequirement.NONE,
        effect_profile=EffectProfile.READ,
    )

    async def run(self, invocation: ToolInvocation, ctx: ToolContext) -> ToolOutcome:
        text = invocation.arguments.get("text")
        return ToolOutcome(
            status=ToolStatus.SUCCEEDED,
            model_result=ModelToolResult(blocks=(TextBlock(str(text)),)),
        )


class AskUserTool:
    definition = ToolDefinition(
        tool_id=ASK_USER_TOOL_ID,
        description="Ask the user a question and wait for the answer.",
        input_schema={"type": "object", "properties": {"question": {"type": "string"}}},
        workspace_requirement=WorkspaceRequirement.NONE,
        effect_profile=EffectProfile.READ,
        completion=Completion.SUSPENDING,
    )

    async def run(self, invocation: ToolInvocation, ctx: ToolContext) -> ToolOutcome:
        raise AssertionError("suspending Tools are never run by the Dispatcher")


def test_dtos_are_frozen(assert_api_dataclasses_frozen: Callable[[ModuleType], None]) -> None:
    assert_api_dataclasses_frozen(api)


def test_example_handlers_conform() -> None:
    assert_handler_conforms(EchoTool())
    assert_handler_conforms(AskUserTool())


def test_handler_returns_outcome_without_ledger_access() -> None:
    scope = TrustedScope(PrincipalId("p1"), ConversationId("c1"))
    ctx = ToolContext(scope=scope, artifacts=_NoArtifacts())
    outcome = asyncio.run(
        EchoTool().run(ToolInvocation(ToolId("echo"), CallId("call_1"), {"text": "hi"}), ctx)
    )
    assert outcome.status is ToolStatus.SUCCEEDED
    assert outcome.model_result.blocks == (TextBlock("hi"),)
    assert outcome.artifact_mutations == ()
