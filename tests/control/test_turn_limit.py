"""V3-P62, V3-P106: one turn guarded by one Run limit — the Runs it still
allows told to the model as data, the per-Conversation session key, the
Runner's usage."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any

import pytest
from testkit import SCOPE, RecordingTool, calls, final, runtime, text_result, user

from himari.common import ConversationId, EventId, TokenUsage, TrustedScope
from himari.control.api import DriveState, TurnUsage
from himari.control.driver import MAX_RUNS, session_key
from himari.control.runner import BackgroundRunner
from himari.control.turn import Turn
from himari.data import codec
from himari.data.api import ConversationEvent, EventKind, PersistedStateBasis
from himari.prompt.api import (TURN_GUIDANCE, ContextBudget, CurrentStateProjection, PromptInputs, RequestOptions,
                               UsageMark, state_block)
from himari.prompt.composer import Composer
from himari.prompt.compaction import Compactor, turn_state_tokens
from himari.provider.openrouter import OpenRouterAdapter, _usage
from himari.tool.api import ToolOutcome, ToolStatus

BIG = ContextBudget(limit_tokens=200_000)


def run(case: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(asyncio.wait_for(case(), 30))


class Counter:
    """A READ tool whose result changes every call."""

    def __init__(self) -> None:
        self.n = 0

    def __call__(self, invocation: Any, ctx: Any) -> ToolOutcome:
        self.n += 1
        return ToolOutcome(ToolStatus.SUCCEEDED, text_result(f"reading {self.n}"))


# -- the Run limit (§6) ------------------------------------------------------------------------------


def test_the_run_limit_counts_model_runs_and_says_how_many() -> None:
    probe = RecordingTool("probe", behavior=Counter())

    async def case() -> None:
        rt, _ = runtime([calls(("probe", {})) for _ in range(5)], tools=[probe], max_runs_per_drive=3)
        result = await rt.driver.drive(SCOPE, user("go"))
        assert result.state is DriveState.RUN_LIMIT_REACHED and result.detail == "3 steps"

    run(case)


def test_the_default_limit_and_no_turn_state_without_guidance() -> None:
    """build_runtime's default limit is the server's (12); without ``turn_guidance``
    nothing about the turn reaches the model. Only the session key is sent."""
    reader = RecordingTool("reader")

    async def case() -> None:
        rt, adapter = runtime([calls(("reader", {})) for _ in range(20)], tools=[reader], context_budget=BIG,
                              max_runs_per_drive=MAX_RUNS)
        result = await rt.driver.drive(SCOPE, user("go"))
        assert result.state is DriveState.RUN_LIMIT_REACHED and len(adapter.prepared_inputs) == MAX_RUNS == 12
        assert all(i.turn_state is None for i in adapter.prepared_inputs)
        assert all(TURN_GUIDANCE not in "\n".join(i.stable_prefix) for i in adapter.prepared_inputs)

    run(case)


def test_a_turn_needs_a_positive_limit() -> None:
    with pytest.raises(ValueError, match="Run limit is positive"):
        runtime(max_runs_per_drive=0)


# -- the turn's state, told as data (§8) ------------------------------------------------------------


def test_few_runs_left_and_the_last_are_told_as_data_and_the_rule_is_in_the_system_prompt() -> None:
    turn = Turn(max_runs=4)
    assert turn.state() is None  # 4 left: not low yet
    turn.charge(None, step=True)
    turn.charge(None, step=False)  # a summarizing call is no step
    state = turn.state()
    assert state is not None and (state.runs_left, state.last) == (3, False)
    turn.charge(None, step=True)
    turn.charge(None, step=True)
    state = turn.state()
    assert state is not None and (state.runs_left, state.last) == (1, True)
    block = state_block(CurrentStateProjection(PersistedStateBasis(0, 0, ())), (), state)
    assert block is not None
    data = json.loads(block.split("\n", 2)[2].rsplit("\n", 1)[0])
    assert data == {"turn_limit": {"runs_left": 1, "last": True}}
    assert "finish" not in block  # numbers and flags only; the rule is in the instructions
    probe = RecordingTool("probe", behavior=Counter())

    async def case() -> None:
        rt, adapter = runtime([calls(("probe", {})) for _ in range(4)], tools=[probe], max_runs_per_drive=4,
                              turn_guidance=True)
        assert (await rt.driver.drive(SCOPE, user("go"))).state is DriveState.RUN_LIMIT_REACHED
        told = [i.turn_state for i in adapter.prepared_inputs]
        assert told[0] is None and [(s.runs_left, s.last) for s in told[1:] if s is not None] == [
            (3, False), (2, False), (1, True)]
        assert TURN_GUIDANCE in "\n".join(adapter.prepared_inputs[0].stable_prefix)
        assert "budget" not in TURN_GUIDANCE and "pause" not in TURN_GUIDANCE and "ask_user" not in TURN_GUIDANCE

    run(case)


def test_the_measured_estimate_counts_the_turn_state() -> None:
    """Re-review B4: the turn's state changes every Run apart from the state's
    revision, so the measured estimate adds it whole."""
    events = (
        ConversationEvent(EventId("e1"), ConversationId("c"), 1, EventKind.USER_MESSAGE, user("q1"), 1,
                          datetime(2026, 1, 1, tzinfo=UTC)),
    )

    def prompt_inputs(mark: UsageMark | None) -> PromptInputs:
        return PromptInputs(instructions=("x",), tool_catalog=(), events=events, tool_executions=(), snapshot=None,
                            state_basis=PersistedStateBasis(1, 0, ()), observations={},
                            request_options=RequestOptions("m"), trigger_refs=(), usage_mark=mark)

    basis = Composer().compose(prompt_inputs(None)).prompt_basis
    measured = prompt_inputs(UsageMark(500, 1, None, basis.context_key, 0, state_key=basis.state_key))
    plain = Composer().compose(measured)
    turn = Turn(max_runs=3)
    turn.charge(None, step=True)
    told = dataclasses.replace(plain, turn_state=turn.state())
    without, with_state = Compactor().estimate(measured, plain), Compactor().estimate(measured, told)
    assert without.measured and with_state.measured
    assert with_state.tokens - without.tokens == turn_state_tokens(told) > 0


# -- usage (§7) ---------------------------------------------------------------------------------------


def test_usage_is_what_was_reported() -> None:
    turn = Turn(max_runs=5)
    turn.charge(TokenUsage(input_tokens=1_000, output_tokens=50, cached_input_tokens=600, cache_write_tokens=40),
                step=True)
    turn.charge(None, step=True)  # not reported: counted as a Run, no tokens
    turn.charge(TokenUsage(input_tokens=200, output_tokens=None), step=False)
    assert turn.usage() == TurnUsage(runs=3, input_tokens=1_200, output_tokens=50, cached_input_tokens=600,
                                     cache_write_tokens=40, max_runs=5)
    assert turn.steps == 2


def test_the_runner_keeps_the_latest_usage() -> None:
    probe = RecordingTool("probe", behavior=Counter())

    async def case() -> None:
        rt, _ = runtime([calls(("probe", {})) for _ in range(4)] + [final("done")], tools=[probe])
        runner = BackgroundRunner(rt.driver)
        await runner.submit(SCOPE, user("go"))
        status = await runner.wait(SCOPE)
        assert status.result is not None and status.result.state is DriveState.COMPLETED
        assert status.turn_usage is not None and (status.turn_usage.runs, status.turn_usage.max_runs) == (5, 16)

    run(case)


# -- session key and cache writes (§9) --------------------------------------------------------------------


def test_every_request_of_a_conversation_carries_its_own_session_key() -> None:
    other = TrustedScope(SCOPE.principal_id, ConversationId("conv_2"))

    async def case() -> None:
        probe = RecordingTool("probe", behavior=Counter())
        rt, adapter = runtime([calls(("probe", {})), final("a"), final("b")], tools=[probe], context_budget=BIG)
        rt.store.create_conversation(other)
        await rt.driver.drive(SCOPE, user("one"))
        await rt.driver.drive(other, user("two"))
        keys = [i.request_options.session_key for i in adapter.prepared_inputs]
        assert keys[0] == keys[1] == session_key(SCOPE) != keys[2] == session_key(other)
        assert str(SCOPE.conversation_id) not in keys[0]
        body = json.loads(OpenRouterAdapter("sk").prepare_native_request(adapter.prepared_inputs[0]).body)
        assert body["session_id"] == session_key(SCOPE)

    run(case)


def test_cache_writes_are_read_and_kept_and_older_rows_have_none() -> None:
    usage = _usage({"usage": {"prompt_tokens": 100, "completion_tokens": 5,
                              "prompt_tokens_details": {"cached_tokens": 60, "cache_write_tokens": 40}}})
    assert usage is not None and (usage.cached_input_tokens, usage.cache_write_tokens) == (60, 40)
    assert codec.usage_from(codec.usage(usage)) == usage
    old = json.dumps({"input_tokens": 1, "output_tokens": 2, "reasoning_tokens": None, "cached_input_tokens": None})
    assert codec.usage_from(old) == TokenUsage(1, 2, None, None, None)
