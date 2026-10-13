"""The suspending ``ask_user`` Tool (CTRL-05, V3-044)."""

from __future__ import annotations

from himari.common import ASK_USER_TOOL_ID
from himari.tool.api import (
    Completion,
    EffectProfile,
    ToolContext,
    ToolDefinition,
    ToolInvocation,
    ToolOutcome,
    WorkspaceRequirement,
)


class AskUserTool:
    definition = ToolDefinition(
        tool_id=ASK_USER_TOOL_ID,
        description=(
            "Ask the user a question and wait for the answer. Use only when you "
            "cannot proceed without it. Must be the only call in its turn."
        ),
        input_schema={
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"],
        },
        workspace_requirement=WorkspaceRequirement.NONE,
        effect_profile=EffectProfile.READ,
        completion=Completion.SUSPENDING,
    )

    async def run(self, invocation: ToolInvocation, ctx: ToolContext) -> ToolOutcome:
        raise RuntimeError("ask_user is suspending; the Dispatcher never runs it")
