from __future__ import annotations

from collections.abc import Callable
from types import ModuleType

from himari.common import CallId, ModelToolResult, RunId, TextBlock, ToolCall, ToolId
from himari.prompt import api
from himari.prompt.api import ToolGroupTurn


def test_dtos_are_frozen(assert_api_dataclasses_frozen: Callable[[ModuleType], None]) -> None:
    assert_api_dataclasses_frozen(api)


def test_tool_group_turn_pairs_results_by_call_id() -> None:
    calls = (
        ToolCall(CallId("a"), 0, ToolId("echo"), {}),
        ToolCall(CallId("b"), 1, ToolId("echo"), {}),
    )
    # Completion order (b before a) does not matter; rendering follows ordinal.
    results = {
        CallId("b"): ModelToolResult((TextBlock("B"),)),
        CallId("a"): ModelToolResult((TextBlock("A"),)),
    }
    turn = ToolGroupTurn(RunId("r1"), (), calls, results)
    assert [turn.results[c.call_id].blocks[0] for c in turn.calls] == [
        TextBlock("A"),
        TextBlock("B"),
    ]
