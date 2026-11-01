"""V3-P87 §4.4 (PR #153 review P2-1): a restart in the middle of a wake's tool
group. The group is closed: calls that never started are not run, finished
ones keep their effect, and the wake fails. A new wake opens only once the
conversation is settled; an unknown effect that needs the user keeps it shut."""

from __future__ import annotations

import asyncio

import pytest
from testkit import FAKE, SCOPE, RecordingTool, SimulatedCrash, calls, final, restart, runtime, user

from himari.bootstrap.proactive import CUT_REASON, settle_cut_wakes
from himari.common import UnknownOutcomePolicy, WorkspaceId
from himari.control.api import ControlError, ControlErrorCode, DriveState
from himari.control.driver import Driver
from himari.data.api import ToolExecutionStatus, WakeKind, WakeReason, WakeStatus
from himari.tool.api import EffectProfile, WorkspaceRequirement

TRY = (WakeReason(WakeKind.TRY),)


def tool(name: str, effect: EffectProfile = EffectProfile.SCOPED_WRITE,
         policy: UnknownOutcomePolicy | None = None) -> RecordingTool:
    made = RecordingTool(f"fake/{name}", effect=effect, environment_kind=FAKE,
                         workspace=WorkspaceRequirement.DEFAULT)
    if policy is not None:
        import dataclasses

        made.definition = dataclasses.replace(made.definition, unknown_outcome=policy)
    return made


def settle(rt: object) -> int:
    data, driver = rt.data, rt.driver  # type: ignore[attr-defined]
    assert isinstance(driver, Driver)
    return asyncio.run(settle_cut_wakes(data, driver))  # the in-memory backend keeps the same contract


def test_a_restart_closes_the_cut_group_without_running_what_never_started() -> None:
    first = tool("first")  # finished before the crash: its effect stays
    second = tool("second", EffectProfile.READ)  # dies before its claim
    third = tool("third")  # never started
    second.crash_next = True
    tools = [first, second, third]
    group = calls(("fake/first", {}), ("fake/second", {}), ("fake/third", {}))
    rt, _ = runtime([group], env_tools=tools)
    rt.store.activate_environment(SCOPE, FAKE, WorkspaceId("ws_1"))
    wake = asyncio.run(rt.driver.accept_wake(SCOPE, TRY, "d1"))
    with pytest.raises(SimulatedCrash):
        asyncio.run(rt.driver.resume(SCOPE, max_runs=3))

    rt2, provider2 = runtime([final("왔어?")], store=restart(rt.store), env_tools=tools)
    assert settle(rt2) == 1
    record = asyncio.run(rt2.data.read_wake(SCOPE.principal_id, wake.payload.wake_id))  # type: ignore[union-attr]
    assert record.status is WakeStatus.FAILED
    assert (len(first.invocations), len(second.invocations), third.invocations) == (1, 1, [])
    run_id = asyncio.run(rt2.data.read_events(SCOPE))[1].origin_run_id
    assert run_id is not None
    executions = {str(e.call.call_id): e for e in asyncio.run(rt2.data.read_tool_executions(SCOPE, run_id))}
    assert executions["c0"].status is ToolExecutionStatus.SUCCEEDED
    for closed in ("c1", "c2"):
        assert executions[closed].status is ToolExecutionStatus.CANCELLED
        assert CUT_REASON in executions[closed].model_result.blocks[0].text  # type: ignore[union-attr]
    assert provider2.sent == []  # settling runs no model

    again = asyncio.run(rt2.driver.accept_wake(SCOPE, TRY, "d1"))  # settled: a new wake opens
    asyncio.run(rt2.driver.resume(SCOPE, max_runs=3))
    assert third.invocations == [] and len(provider2.sent) == 1
    assert again.payload.wake_id != wake.payload.wake_id  # type: ignore[union-attr]


def test_an_unknown_effect_that_needs_the_user_keeps_new_wakes_out() -> None:
    pay = tool("pay", EffectProfile.UNKNOWN, UnknownOutcomePolicy.REQUIRE_USER)
    pay.crash_next = True  # dies after its claim: the effect is unknown
    rt, _ = runtime([calls(("fake/pay", {}))], env_tools=[pay])
    rt.store.activate_environment(SCOPE, FAKE, WorkspaceId("ws_1"))
    wake = asyncio.run(rt.driver.accept_wake(SCOPE, TRY, "d1"))
    with pytest.raises(SimulatedCrash):
        asyncio.run(rt.driver.resume(SCOPE, max_runs=3))

    rt2, provider2 = runtime([], store=restart(rt.store), env_tools=[pay])
    assert settle(rt2) == 1
    record = asyncio.run(rt2.data.read_wake(SCOPE.principal_id, wake.payload.wake_id))  # type: ignore[union-attr]
    assert record.status is WakeStatus.FAILED
    assert len(pay.invocations) == 1  # never run again
    with pytest.raises(ControlError) as refused:
        asyncio.run(rt2.driver.accept_wake(SCOPE, TRY, "d1"))
    assert refused.value.code is ControlErrorCode.NOT_SETTLED
    assert asyncio.run(rt2.data.opened_wakes()) == () and provider2.sent == []


def test_a_group_the_users_turn_left_keeps_new_wakes_out() -> None:
    """Stopped between Runs (here at its Run limit), the user's work waits for the user, not for a wake."""
    look = tool("look", EffectProfile.READ)
    rt, _ = runtime([calls(("fake/look", {}))], env_tools=[look], max_runs_per_drive=1)
    rt.store.activate_environment(SCOPE, FAKE, WorkspaceId("ws_1"))
    assert asyncio.run(rt.driver.drive(SCOPE, user("look"))).state is DriveState.RUN_LIMIT_REACHED
    with pytest.raises(ControlError) as refused:
        asyncio.run(rt.driver.accept_wake(SCOPE, TRY, "d1"))
    assert refused.value.code is ControlErrorCode.NOT_SETTLED


def test_a_group_an_ended_wake_left_does_not_keep_the_next_one_out() -> None:
    """A wake stopped at its Run cap with its last group unanswered; once ended, the next wake shows those results."""
    look = tool("look", EffectProfile.READ)
    rt, provider = runtime([calls(("fake/look", {}))] * 3 + [final("아까 보던 거 다 봤어")], env_tools=[look])
    rt.store.activate_environment(SCOPE, FAKE, WorkspaceId("ws_1"))
    wake = asyncio.run(rt.driver.accept_wake(SCOPE, TRY, "d1"))
    assert asyncio.run(rt.driver.resume(SCOPE, max_runs=3)).state is DriveState.RUN_LIMIT_REACHED
    with pytest.raises(ControlError):  # still opened: its job has not been ended
        asyncio.run(rt.driver.accept_wake(SCOPE, TRY, "d1"))
    asyncio.run(rt.data.end_wake(SCOPE.principal_id, wake.payload.wake_id, WakeStatus.FAILED))  # type: ignore[union-attr]
    asyncio.run(rt.driver.accept_wake(SCOPE, TRY, "d1"))
    assert asyncio.run(rt.driver.resume(SCOPE, max_runs=3)).state is DriveState.COMPLETED
    assert len(look.invocations) == 3



def test_a_wake_that_said_something_keeps_its_keys() -> None:
    """Part 2: only a wake cut before any word gives its keys back."""
    first = tool("first")
    first.crash_next = True
    rt, _ = runtime([calls(("fake/first", {}))], env_tools=[first])
    rt.store.activate_environment(SCOPE, FAKE, WorkspaceId("ws_1"))
    asyncio.run(rt.driver.accept_wake(SCOPE, TRY, "d1", keys=("loop:m1:x",)))
    with pytest.raises(SimulatedCrash):
        asyncio.run(rt.driver.resume(SCOPE, max_runs=3))
    rt2, _ = runtime([], store=restart(rt.store), env_tools=[first])
    assert settle(rt2) == 1
    assert asyncio.run(rt2.data.taken_keys(SCOPE.principal_id, ("loop:m1:x",))) == frozenset({"loop:m1:x"})


def test_a_wake_cut_before_a_word_is_tried_again_after_the_restart() -> None:
    """PR #156 review P1: restart → failed → keys back → a new wake opens → the model answers.
    The next turn draws both wakes, the new one last."""
    from himari.prompt.api import UserTurn

    loop_key = ("loop:m1:2026-11-04T14:00+09:00",)
    rt, provider = runtime([SimulatedCrash("provider call")])
    first = asyncio.run(rt.driver.accept_wake(SCOPE, TRY, "d1", keys=loop_key))
    with pytest.raises(SimulatedCrash):
        asyncio.run(rt.driver.resume(SCOPE, max_runs=3))

    rt2, provider2 = runtime([final("면접 어땠어?")], store=restart(rt.store))
    assert settle(rt2) == 1
    record = asyncio.run(rt2.data.read_wake(SCOPE.principal_id, first.payload.wake_id))  # type: ignore[union-attr]
    assert record.status is WakeStatus.FAILED
    assert asyncio.run(rt2.data.taken_keys(SCOPE.principal_id, loop_key)) == frozenset()
    second = asyncio.run(rt2.driver.accept_wake(SCOPE, TRY, "d1", keys=loop_key))  # no longer NOT_SETTLED
    result = asyncio.run(rt2.driver.resume(SCOPE, max_runs=3))
    assert result.state is DriveState.COMPLETED
    drawn = provider2.prepared_inputs[-1].conversation
    assert [isinstance(p, UserTurn) and p.wake for p in drawn] == [True, True]
    assert asyncio.run(rt2.data.taken_keys(SCOPE.principal_id, loop_key)) == frozenset(loop_key)
    kinds = [e.kind.value for e in asyncio.run(rt2.data.read_events(SCOPE))]
    assert kinds == ["wake", "wake", "assistant_final"]
    assert second.payload.wake_id != first.payload.wake_id  # type: ignore[union-attr]


def test_an_opened_wake_or_a_users_message_still_keeps_a_new_wake_out() -> None:
    rt, _ = runtime([])
    asyncio.run(rt.driver.accept_wake(SCOPE, TRY, "d1"))
    with pytest.raises(ControlError) as refused:  # opened: its job runs (or will be settled)
        asyncio.run(rt.driver.accept_wake(SCOPE, TRY, "d1"))
    assert refused.value.code is ControlErrorCode.NOT_SETTLED
    rt2, _ = runtime([])
    asyncio.run(rt2.data.append_event(SCOPE, user("hi")))
    with pytest.raises(ControlError) as refused:  # the user's message waits for its answer
        asyncio.run(rt2.driver.accept_wake(SCOPE, TRY, "d1"))
    assert refused.value.code is ControlErrorCode.NOT_SETTLED
