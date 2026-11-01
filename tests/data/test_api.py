from __future__ import annotations

from collections.abc import Callable
from types import ModuleType

from himari.data import api


def test_dtos_are_frozen(assert_api_dataclasses_frozen: Callable[[ModuleType], None]) -> None:
    assert_api_dataclasses_frozen(api)


def test_event_kinds_follow_ask_user_model() -> None:
    # V3-044: question is an ask_user call; assistant_question is retired.
    assert {k.value for k in api.EventKind} == {
        "user_message",
        "assistant_final",
        "assistant_tool_calls",
        "tool_result",
        "wake",  # V3-P87 §3: a server-opened turn's trigger
    }


def test_p0_durability_marks_are_declared() -> None:
    # V3-045: crash-duplication protection is persisted state, not the lock.
    assert api.RunStatus.DISPATCH_CLAIMED in api.RunStatus
    assert api.RunStatus.OUTCOME_UNKNOWN in api.RunStatus
    assert api.ToolExecutionStatus.EXECUTION_CLAIMED in api.ToolExecutionStatus
    assert api.ToolExecutionStatus.UNKNOWN in api.ToolExecutionStatus


def test_data_error_carries_code() -> None:
    err = api.DataError(api.DataErrorCode.ALREADY_COMMITTED, "call answered")
    assert err.code is api.DataErrorCode.ALREADY_COMMITTED
    assert "already_committed" in str(err)
