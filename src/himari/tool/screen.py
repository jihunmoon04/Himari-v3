"""The screen Tool (V3-P91 §3): ``look_at_screen``.

Internal and read-only: it asks the turn's device for its screen now
(``ScreenSource``, which Bootstrap answers by asking the device) and returns
the image as a screen. A turn with no device (the Web, the CLI), a wake that
is not a free one, and every reason the device gives are a failed result the
model reads. Offered only to a model checked for it (``requires``).
"""

from __future__ import annotations

from himari.common import ConversationId, ModelToolResult, RunId, TextBlock, ToolId
from himari.tool.api import (
    EffectProfile,
    ResultImage,
    ScreenRefusal,
    ScreenSource,
    ToolContext,
    ToolDefinition,
    ToolInvocation,
    ToolOutcome,
    ToolStatus,
    TurnWake,
    WorkspaceRequirement,
)

LOOK_AT_SCREEN = ToolId("look_at_screen")
SCREEN_TOOL = "screen_tool"  # the model capability it requires (V3-P91 §9)

DESCRIPTION = (
    "See the user's screen now, on the computer this conversation is happening on. Use it when what the user "
    "asks or what you want to say depends on what is on their screen and you have no recent screen. The user "
    "sees that you looked."
)
CAPTURED = "Screen captured."
ALREADY_LOOKED = "You already looked at the screen in this step; it is in the earlier result."
NO_DEVICE = "The user is not at a device that can show the screen."
NOT_THIS_TURN = "Screen viewing is not available in this turn."
REFUSALS = {
    ScreenRefusal.AWAY: "The screen cannot be seen now (the computer is away or locked).",
    ScreenRefusal.TURNED_OFF: "The user turned off screen viewing.",
    ScreenRefusal.NOT_HERE: NOT_THIS_TURN,
    ScreenRefusal.CAPTURE_FAILED: "The screen could not be captured.",
    ScreenRefusal.TIMEOUT: "The screen did not arrive in time.",
}


def _failed(message: str) -> ToolOutcome:
    return ToolOutcome(ToolStatus.FAILED, ModelToolResult((TextBlock(message),), is_error=True))


class ScreenTool:
    def __init__(self, source: ScreenSource) -> None:
        self._source = source
        # §3.1: the Run each conversation last looked in; a Run's calls run one after another
        self._looked: dict[ConversationId, RunId] = {}

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            tool_id=LOOK_AT_SCREEN, description=DESCRIPTION,
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            workspace_requirement=WorkspaceRequirement.NONE, effect_profile=EffectProfile.READ,
            requires=SCREEN_TOOL)

    async def run(self, invocation: ToolInvocation, ctx: ToolContext) -> ToolOutcome:
        turn = ctx.turn
        if turn is None or turn.device_id is None:  # the Web, the CLI: no request goes to any device
            return _failed(NO_DEVICE)
        if turn.wake is TurnWake.OTHER:  # §8 (Q6): a wake with its own reasons has no need of it
            return _failed(NOT_THIS_TURN)
        if ctx.run_id is not None:
            if self._looked.get(ctx.scope.conversation_id) == ctx.run_id:  # §3.1: not the same screen twice
                return _failed(ALREADY_LOOKED)
            self._looked[ctx.scope.conversation_id] = ctx.run_id
        found = await self._source.look(ctx.scope, turn.device_id, free_wake=turn.wake is TurnWake.FREE)
        if isinstance(found, ScreenRefusal):
            return _failed(REFUSALS[found])
        return ToolOutcome(ToolStatus.SUCCEEDED, ModelToolResult((TextBlock(CAPTURED),)),
                           result_images=(ResultImage(found, screen=True),))
