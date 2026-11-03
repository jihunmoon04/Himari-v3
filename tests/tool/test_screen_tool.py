"""V3-P91 §3: the screen Tool — where the turn speaks decides whether it asks at all."""

from __future__ import annotations

import asyncio

import pytest

from himari.common import CallId, ConversationId, PrincipalId, TextBlock, TrustedScope
from himari.tool.api import (
    EffectProfile,
    ScreenRefusal,
    ToolContext,
    ToolInvocation,
    ToolStatus,
    TurnSource,
    TurnWake,
)
from himari.tool.registry import ToolRegistry
from himari.tool.screen import (
    CAPTURED,
    LOOK_AT_SCREEN,
    NO_DEVICE,
    NOT_THIS_TURN,
    REFUSALS,
    SCREEN_TOOL,
    ScreenTool,
)

SCOPE = TrustedScope(PrincipalId("operator"), ConversationId("c1"))
CALL = ToolInvocation(LOOK_AT_SCREEN, CallId("c0"), {})


class Source:
    def __init__(self, answer: bytes | ScreenRefusal = b"\x89PNG...") -> None:
        self.answer = answer
        self.asked: list[tuple[str, bool]] = []

    async def look(self, scope: TrustedScope, device_id: str, *, free_wake: bool = False) -> bytes | ScreenRefusal:
        self.asked.append((device_id, free_wake))
        return self.answer


def run(tool: ScreenTool, turn: TurnSource | None):  # type: ignore[no-untyped-def]
    return asyncio.run(tool.run(CALL, ToolContext(scope=SCOPE, artifacts=None, turn=turn)))  # type: ignore[arg-type]


def text(outcome) -> str:  # type: ignore[no-untyped-def]
    (block,) = outcome.model_result.blocks
    assert isinstance(block, TextBlock)
    return block.text


def test_it_reads_and_is_offered_only_to_a_model_with_the_capability() -> None:
    tool = ScreenTool(Source())
    assert tool.definition.effect_profile is EffectProfile.READ and tool.definition.requires == SCREEN_TOOL
    registry = ToolRegistry([tool])
    assert [d.tool_id for d in registry.catalog(frozenset())] == []
    assert [d.tool_id for d in registry.catalog(frozenset(), capabilities=frozenset({SCREEN_TOOL}))] == [LOOK_AT_SCREEN]


def test_the_turns_device_is_asked_and_its_screen_is_a_screen() -> None:
    source = Source(b"png bytes")
    outcome = run(ScreenTool(source), TurnSource("d1"))
    assert outcome.status is ToolStatus.SUCCEEDED and text(outcome) == CAPTURED
    (image,) = outcome.result_images
    assert image.data == b"png bytes" and image.screen
    assert source.asked == [("d1", False)]


@pytest.mark.parametrize("turn", [None, TurnSource(None), TurnSource(None, TurnWake.FREE)])
def test_a_turn_with_no_device_asks_nothing(turn: TurnSource | None) -> None:
    source = Source()
    outcome = run(ScreenTool(source), turn)
    assert outcome.status is ToolStatus.FAILED and text(outcome) == NO_DEVICE and source.asked == []


def test_a_wake_looks_only_when_it_is_a_free_one() -> None:
    source = Source()
    outcome = run(ScreenTool(source), TurnSource("d1", TurnWake.OTHER))
    assert outcome.status is ToolStatus.FAILED and text(outcome) == NOT_THIS_TURN and source.asked == []
    assert run(ScreenTool(source), TurnSource("d1", TurnWake.FREE)).status is ToolStatus.SUCCEEDED
    assert source.asked == [("d1", True)]  # the source checks the free wake's own setting


@pytest.mark.parametrize("refusal", list(ScreenRefusal))
def test_each_refusal_is_a_sentence_for_the_model(refusal: ScreenRefusal) -> None:
    outcome = run(ScreenTool(Source(refusal)), TurnSource("d1"))
    assert outcome.status is ToolStatus.FAILED and text(outcome) == REFUSALS[refusal]
    assert outcome.model_result.is_error and outcome.result_images == ()
