"""V3-P62: one turn without a Run limit — the token budget checked before
every request, progress that stops only on clear evidence, the turn's state
told to the model as data, the per-Conversation session key, the Runner's
usage and unattended pause."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any

import pytest
from testkit import SCOPE, RecordingTool, calls, final, runtime, text_result, user

from himari.common import (AttachmentRef, CallId, CallRef, ConversationId, EventId, ModelToolResult, RunId,
                           TextBlock, TokenUsage, ToolId, TrustedScope)
from himari.control.api import TURN_GUIDANCE, DriveState, StopSignal, TurnUsage
from himari.control.driver import PAUSED_COMPACTING, session_key
from himari.control.runner import UNATTENDED_REASON, BackgroundRunner
from himari.control.turn import MIN_OUTPUT_RESERVE, Turn
from himari.data import codec
from himari.data.api import (AssistantToolCalls, ConversationEvent, EventKind, PersistedStateBasis, PromptBasis,
                             RawReceived, RunPreparation, ToolExecutionRecord, ToolExecutionStatus)
from himari.prompt.api import (ContextBudget, CurrentStateProjection, PromptInputs, RequestOptions, UsageMark,
                               state_block)
from himari.prompt.composer import Composer
from himari.prompt.compaction import Compactor, turn_state_tokens
from himari.provider.openrouter import OpenRouterAdapter, _usage
from himari.provider.scripted import Measured
from himari.tool.api import EffectProfile, RefusalRule, ToolOutcome, ToolStatus

BIG = ContextBudget(limit_tokens=200_000)


def run(case: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(asyncio.wait_for(case(), 30))


class Counter:
    """A READ tool whose result changes every call (always something new)."""

    def __init__(self) -> None:
        self.n = 0

    def __call__(self, invocation: Any, ctx: Any) -> ToolOutcome:
        self.n += 1
        return ToolOutcome(ToolStatus.SUCCEEDED, text_result(f"reading {self.n}"))


# -- no Run limit ------------------------------------------------------------------------------------


def test_without_a_run_limit_a_turn_goes_on_past_sixteen_runs() -> None:
    probe = RecordingTool("probe", behavior=Counter())
    script = [calls(("probe", {})) for _ in range(20)] + [final("done")]

    async def case() -> None:
        rt, adapter = runtime(script, tools=[probe], context_budget=BIG, max_runs_per_drive=None,
                              turn_budget=10_000_000)
        result = await rt.driver.drive(SCOPE, user("go"))
        assert result.state is DriveState.COMPLETED and len(result.run_ids) == 21

    run(case)


def test_a_run_limit_when_set_counts_model_runs_and_says_how_many() -> None:
    probe = RecordingTool("probe", behavior=Counter())

    async def case() -> None:
        rt, _ = runtime([calls(("probe", {})) for _ in range(5)], tools=[probe], max_runs_per_drive=3)
        result = await rt.driver.drive(SCOPE, user("go"))
        assert result.state is DriveState.RUN_LIMIT_REACHED and result.detail == "3 steps"

    run(case)


def test_the_defaults_keep_todays_policy() -> None:
    """Review C2: with build_runtime's defaults (a limit of 16, no budget) a
    READ tool repeating its result runs to the limit, and nothing new reaches
    the model: no turn state, no guidance. Only the session key is sent."""
    reader = RecordingTool("reader")  # always "reader ok"

    async def case() -> None:
        rt, adapter = runtime([calls(("reader", {})) for _ in range(20)], tools=[reader], context_budget=BIG)
        result = await rt.driver.drive(SCOPE, user("go"))
        assert result.state is DriveState.RUN_LIMIT_REACHED and len(adapter.prepared_inputs) == 16
        assert all(i.turn_state is None for i in adapter.prepared_inputs)
        assert all(TURN_GUIDANCE not in i.stable_prefix for i in adapter.prepared_inputs)
        assert len(reader.invocations) == 16

    run(case)


def test_a_turn_needs_a_budget_or_a_limit() -> None:
    with pytest.raises(ValueError, match="token budget or a Run limit"):
        runtime(context_budget=BIG, max_runs_per_drive=None)
    with pytest.raises(ValueError, match="needs a context budget"):
        runtime(turn_budget=1000)


# -- the token budget (§3) ------------------------------------------------------------------------------


def test_the_budget_is_checked_before_the_request_and_continue_gives_a_new_one() -> None:
    probe = RecordingTool("probe", behavior=Counter())
    # 5,000 in + 100 out, then the next request would need its estimate + 8,192 kept for its answer.
    script = [Measured(calls(("probe", {})), 5_000, 100) for _ in range(3)] + [final("done")]

    async def case() -> None:
        rt, adapter = runtime(script, tools=[probe], context_budget=BIG, max_runs_per_drive=None,
                              turn_budget=15_000)
        result = await rt.driver.drive(SCOPE, user("go"))
        assert result.state is DriveState.BUDGET_REACHED and len(adapter.prepared_inputs) == 1
        assert "would pass the budget of 15000" in result.detail and "used 5100" in result.detail
        again = await rt.driver.resume(SCOPE)  # Continue: a new budget
        assert again.state is DriveState.BUDGET_REACHED and len(adapter.prepared_inputs) == 2

    run(case)


def test_a_summarizing_call_is_checked_too_and_commits_nothing() -> None:
    async def case() -> None:
        small = ContextBudget(limit_tokens=1000)
        rt, adapter = runtime([Measured(final("first answer"), 900), final("SUMMARY"), final("second")],
                              context_budget=small, max_runs_per_drive=None, turn_budget=1_000_000)
        await rt.driver.drive(SCOPE, user("first question"))
        tight, adapter2 = runtime([final("SUMMARY")], store=rt.store, context_budget=small,
                                  max_runs_per_drive=None, turn_budget=200)
        result = await tight.driver.drive(SCOPE, user("second question"))
        assert result.state is DriveState.BUDGET_REACHED
        assert "a summarizing call" in result.detail and result.detail.endswith("nothing was committed")
        assert adapter2.prepared_inputs == [] and await tight.data.get_latest_snapshot(SCOPE) is None

    run(case)


def test_the_output_reserve() -> None:
    turn = Turn(budget=100_000, max_runs=None, model_max_output=12_000)
    assert turn.reserve() == MIN_OUTPUT_RESERVE
    turn.charge(TokenUsage(input_tokens=1_000, output_tokens=5_000), 900, MIN_OUTPUT_RESERVE, step=True)
    assert turn.reserve() == 10_000  # twice the largest output
    turn.charge(TokenUsage(input_tokens=1_000, output_tokens=7_000), 900, 10_000, step=True)
    assert turn.reserve() == 12_000  # at most the model's own maximum
    unreported = Turn(budget=100_000, max_runs=None)
    unreported.charge(None, 3_000, 8_192, step=True)  # charged its estimate and reserve
    assert (unreported.input_tokens, unreported.output_tokens) == (3_000, 8_192)
    assert unreported.fits(80_000, 8_192) and not unreported.fits(80_809, 8_192)


# -- the turn's state, told as data (§8) ------------------------------------------------------------


def test_low_and_last_are_told_as_data_and_the_rule_is_in_the_system_prompt() -> None:
    # A request is expected to cost the previous input + the 8,192 reserve; low under 3 of them, last under 2.
    turn = Turn(budget=60_000, max_runs=None)
    assert turn.state() is None  # nothing before the first Run
    turn.charge(TokenUsage(input_tokens=5_000, output_tokens=100), 0, 0, step=True)
    assert turn.state() is None  # left 54,900 ≥ 3 × 13,192
    turn.charge(TokenUsage(input_tokens=10_000, output_tokens=100), 0, 0, step=True)
    state = turn.state()  # left 44,800: < 3 × 18,192, ≥ 2 × 18,192
    assert state is not None and state.budget is not None and state.budget.low and not state.budget.last
    turn.charge(TokenUsage(input_tokens=10_000, output_tokens=100), 0, 0, step=True)
    state = turn.state()  # left 34,700 < 2 × 18,192: this request fits (≥ 18,192), the next likely not
    assert state is not None and state.budget is not None and state.budget.last and state.budget.left == 34_700
    assert turn.fits(10_000, turn.reserve())  # review C1: `last` is told on a request that is still sent
    block = state_block(CurrentStateProjection(PersistedStateBasis(0, 0, (), (), ())), (), state)
    assert block is not None
    data = json.loads(block.split("\n", 2)[2].rsplit("\n", 1)[0])
    assert data == {"turn_budget": {"used": 25_300, "budget": 60_000, "left": 34_700, "low": True, "last": True}}
    assert "finish" not in block  # numbers and flags only; the rule is in the instructions
    limited = Turn(budget=None, max_runs=4)
    limited.charge(None, 0, 0, step=True)
    assert limited.state() is not None and limited.state().budget.runs_left == 3  # type: ignore[union-attr]

    async def case() -> None:
        rt, adapter = runtime([final("ok")], context_budget=BIG, max_runs_per_drive=None, turn_budget=1_000_000)
        await rt.driver.drive(SCOPE, user("hi"))
        assert adapter.prepared_inputs[0].stable_prefix[-1] == TURN_GUIDANCE
        plain, adapter2 = runtime([final("ok")], context_budget=BIG)
        await plain.driver.drive(SCOPE, user("hi"))
        assert TURN_GUIDANCE not in adapter2.prepared_inputs[0].stable_prefix

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
                            state_basis=PersistedStateBasis(1, 0, (), (), ()), observations=(),
                            request_options=RequestOptions("m"), trigger_refs=(), usage_mark=mark)

    key = Composer().compose(prompt_inputs(None)).prompt_basis.context_key
    measured = prompt_inputs(UsageMark(500, 1, None, key, 0))
    plain = Composer().compose(measured)
    turn = Turn(budget=30_000, max_runs=None)
    for _ in range(3):
        turn.charge(TokenUsage(input_tokens=10_000, output_tokens=100), 0, 0, step=True)
    told = dataclasses.replace(plain, turn_state=turn.state())
    without, with_state = Compactor().estimate(measured, plain), Compactor().estimate(measured, told)
    assert without.measured and with_state.measured
    assert with_state.tokens - without.tokens == turn_state_tokens(told) > 0


# -- progress (§4) ------------------------------------------------------------------------------------


def record(tool: str, status: ToolExecutionStatus, result: ModelToolResult | None, call: str = "c0",
           arguments: dict[str, Any] | None = None) -> ToolExecutionRecord:
    return ToolExecutionRecord(CallRef(RunId("r"), CallId(call)), 0, ToolId(tool), arguments or {"q": 1}, status,
                               result)


OK = ToolExecutionStatus.SUCCEEDED
READ, WRITE = EffectProfile.READ, EffectProfile.SCOPED_WRITE


def test_only_clear_evidence_counts_as_nothing_new() -> None:
    same = text_result("the same")
    effects = {"r": READ, "w": WRITE}

    def streak(*groups: tuple[list[ToolExecutionRecord], dict[str, RefusalRule]]) -> int:
        turn = Turn(budget=None, max_runs=10)
        for records, refusals in groups:
            turn.observe(records, refusals, effects)  # type: ignore[arg-type]
        return turn.empty_streak

    assert streak(([record("r", OK, same)], {}), ([record("r", OK, same)], {}), ([record("r", OK, same)], {})) == 2
    # a write tool's same success is not "nothing new" (re-review N2)
    assert streak(([record("w", OK, same)], {}), ([record("w", OK, same)], {}), ([record("w", OK, same)], {})) == 0
    # one new result in the Run breaks the streak
    assert streak(([record("r", OK, same)], {}), ([record("r", OK, same), record("r", OK, text_result("new"), "c1")],
                                                  {})) == 0
    # the same text with another attachment or is_error is a new result (re-review R2)
    other = ModelToolResult((TextBlock("the same"), AttachmentRef("f1", 2)))
    flagged = ModelToolResult((TextBlock("the same"),), is_error=True)
    assert streak(([record("r", OK, same)], {}), ([record("r", OK, other)], {}), ([record("r", OK, flagged)], {})) == 0
    denied = text_result("unknown tool x", is_error=True)
    by_input = {"c0": RefusalRule.UNKNOWN_TOOL}
    assert streak(*[([record("x", ToolExecutionStatus.DENIED, denied)], by_input)] * 3) == 2
    # a state-dependent refusal never counts, even for a READ tool (§17)
    by_state = {"c0": RefusalRule.SOURCE_DISABLED}
    off = text_result("tool source s is not enabled", is_error=True)
    assert streak(*[([record("r", ToolExecutionStatus.DENIED, off)], by_state)] * 3) == 0
    # a handler's own DENIED of a READ tool with the same result is the READ rule
    assert streak(*[([record("r", ToolExecutionStatus.DENIED, denied)], {})] * 3) == 2
    # unknown outcomes say nothing
    assert streak(*[([record("r", ToolExecutionStatus.UNKNOWN, None)], {})] * 3) == 0


def test_repeats_are_told_with_their_effect() -> None:
    turn = Turn(budget=None, max_runs=10)
    same = text_result("x")
    for _ in range(3):
        turn.observe([record("w", OK, same)], {}, {"w": WRITE})
    state = turn.state()
    assert state is not None and [(r.tool_id, r.times, r.effect) for r in state.repeated] == [
        ("w", 2, "scoped_write"), ("w", 3, "scoped_write")]


def test_no_progress_stops_a_read_loop_but_never_a_write_loop() -> None:
    reader = RecordingTool("reader")  # always "reader ok"
    writer = RecordingTool("writer", effect=WRITE)

    async def case() -> None:
        rt, adapter = runtime([calls(("reader", {})) for _ in range(10)], tools=[reader], context_budget=BIG,
                              max_runs_per_drive=None, turn_budget=10_000_000, no_progress_runs=3)
        result = await rt.driver.drive(SCOPE, user("go"))
        assert result.state is DriveState.NO_PROGRESS and len(adapter.prepared_inputs) == 4
        assert result.detail == "the last 3 steps brought nothing new"
        told = adapter.prepared_inputs[-1].turn_state
        assert told is not None and told.repeated[-1].tool_id == "reader"
        rt2, adapter2 = runtime([calls(("writer", {})) for _ in range(6)] + [final("done")], tools=[writer],
                                context_budget=BIG, max_runs_per_drive=None, turn_budget=10_000_000,
                                no_progress_runs=3)
        assert (await rt2.driver.drive(SCOPE, user("go"))).state is DriveState.COMPLETED
        assert len(writer.invocations) == 6  # every call ran

    run(case)


def test_a_disabled_source_is_judged_every_call_and_never_counts() -> None:
    remote = RecordingTool("lab/look", external_source="lab")

    async def case() -> None:
        rt, adapter = runtime([calls(("lab/look", {})) for _ in range(5)] + [final("done")], tools=[remote],
                              context_budget=BIG, max_runs_per_drive=None, turn_budget=10_000_000,
                              no_progress_runs=3, external_defaults={"lab": False})
        task = asyncio.create_task(rt.driver.drive(SCOPE, user("go")))
        result = await task
        assert result.state is DriveState.COMPLETED  # five DENIED calls, no NO_PROGRESS
        assert remote.invocations == []
        await rt.data.set_external_setting(SCOPE, "lab", True)
        adapter.extend([calls(("lab/look", {})), final("again")])
        assert (await rt.driver.drive(SCOPE, user("now"))).state is DriveState.COMPLETED
        assert len(remote.invocations) == 1  # nothing was kept from running by its history

    run(case)


# -- the Dispatcher's refusals (§4) ----------------------------------------------------------------------


def test_the_dispatcher_returns_its_own_refusals_and_leaves_records_alone() -> None:
    def upstream(invocation: Any, ctx: Any) -> ToolOutcome:
        return ToolOutcome(ToolStatus.DENIED, text_result("rate limited", is_error=True),
                           raw_result={"refused_by": "upstream_server", "retry_after": 30})

    strict = RecordingTool("strict")
    strict.definition = dataclasses.replace(
        strict.definition, input_schema={"type": "object", "properties": {"n": {"type": "integer"}},
                                         "required": ["n"], "additionalProperties": False})
    handler_denies = RecordingTool("upstream", behavior=upstream)

    async def case() -> None:
        rt, _ = runtime(tools=[strict, handler_denies])
        await rt.data.append_event(SCOPE, user("go"))
        group = calls(("nope", {}), ("strict", {"n": "x"}), ("upstream", {}))
        run_id = await rt.data.prepare_run(SCOPE, RunPreparation(b"{}", "application/json", None, (),  # type: ignore[arg-type]
                                                                 PromptBasis(0, 0), "scripted", "m"))
        await rt.data.mark_dispatch(SCOPE, run_id)
        await rt.data.record_raw_outcome(SCOPE, run_id, RawReceived(b"{}", "application/json", None))
        await rt.data.project_model_output(SCOPE, run_id, AssistantToolCalls(group.calls))
        dispatched = await rt.driver._dispatcher.dispatch_group(SCOPE, run_id, group.calls)
        assert dict(dispatched.refusals) == {"c0": RefusalRule.UNKNOWN_TOOL, "c1": RefusalRule.INVALID_ARGUMENTS}
        records = {str(r.call.call_id): r for r in await rt.data.read_tool_executions(SCOPE, run_id)}
        assert records["c2"].status is ToolExecutionStatus.DENIED  # the handler's own: not a refusal of the Dispatcher
        assert records["c2"].raw_result == {"refused_by": "upstream_server", "retry_after": 30}  # kept as given

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


# -- pause reasons (§5, C3; §17 note 2) --------------------------------------------------------------------


def test_a_pause_carries_its_reason_also_when_compacting() -> None:
    async def case() -> None:
        small = ContextBudget(limit_tokens=1000)
        rt, adapter = runtime([Measured(final("first answer"), 900), final("SUMMARY")], context_budget=small)
        await rt.driver.drive(SCOPE, user("first question"))
        await rt.data.append_event(SCOPE, user("second"))
        stop = StopSignal()
        stop.pause("paused: no client was reading this conversation")
        stop.pause("a later reason")  # the first one asked stays
        result = await rt.driver.compact(SCOPE, stop=stop)
        assert result.state is DriveState.PAUSED
        assert result.detail == f"paused: no client was reading this conversation; {PAUSED_COMPACTING}"
        user_pause = StopSignal()
        user_pause.pause()
        assert (await rt.driver.compact(SCOPE, stop=user_pause)).detail == PAUSED_COMPACTING
        stopped = StopSignal()
        stopped.pause("why")
        adapter.extend([final("x")])
        assert (await rt.driver.resume(SCOPE, stop=stopped)).detail == "why"

    run(case)


# -- the Runner (§5, §7) --------------------------------------------------------------------------------------


def test_the_runner_keeps_the_latest_usage_and_pauses_when_nobody_reads() -> None:
    now = [0.0]
    probe = RecordingTool("probe", behavior=Counter())

    async def case() -> None:
        rt, _ = runtime([calls(("probe", {})) for _ in range(30)] + [final("done")], tools=[probe],
                        context_budget=BIG, max_runs_per_drive=None, turn_budget=10_000_000)
        runner = BackgroundRunner(rt.driver, unattended_runs=5, seen_seconds=60, clock=lambda: now[0])
        await runner.submit(SCOPE, user("go"))
        now[0] = 1_000.0  # the client is gone
        status = await runner.wait(SCOPE)
        assert status.result is not None and status.result.state is DriveState.PAUSED
        assert status.result.detail == UNATTENDED_REASON
        assert status.turn_usage is not None and status.turn_usage.runs == 5
        assert isinstance(status.turn_usage, TurnUsage) and status.turn_usage.budget == 10_000_000

    run(case)


def test_a_reading_client_keeps_the_runner_going() -> None:
    now = [0.0]
    probe = RecordingTool("probe", behavior=Counter())

    async def case() -> None:
        rt, _ = runtime([calls(("probe", {})) for _ in range(12)] + [final("done")], tools=[probe],
                        context_budget=BIG, max_runs_per_drive=None, turn_budget=10_000_000)
        runner = BackgroundRunner(rt.driver, unattended_runs=5, seen_seconds=60, clock=lambda: now[0])
        await runner.submit(SCOPE, user("go"))
        status = await runner.wait(SCOPE)  # the clock never moves: always within 60 s of the start
        assert status.result is not None and status.result.state is DriveState.COMPLETED
        assert status.turn_usage is not None and status.turn_usage.runs == 13

    run(case)
