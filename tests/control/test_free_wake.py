"""V3-P90 §7.3: a free wake's turn — hidden until it speaks (no job event, not working, its Event
not shown), a pass finalized with no Event, a tool call shows it first, the user winning drops it,
and the same verdict for a Run found at a restart."""

from __future__ import annotations

import asyncio

import pytest
from testkit import SCOPE, calls, final, runtime, user

from himari.common import TextBlock, TrustedScope
from himari.control.api import DriveState, JobStatus
from himari.control.driver import PASS, free_wake_verdict, without_pass
from himari.control.runner import FREE_RUNS, BackgroundRunner
from himari.data.api import AssistantFinal, EventKind, MessageOrigin, Wake, WakeKind, WakeReason, WakeStatus
from himari.provider.api import NormalizedFinal, NormalizedToolCalls

FREE = (WakeReason(WakeKind.FREE, group="work", minutes=16, step=1),)


class Told:
    """The runner's listener: what the views would be told."""

    def __init__(self) -> None:
        self.started: list[JobStatus] = []
        self.ended: list[JobStatus] = []
        self.accepts = 0

    def job_started(self, scope: TrustedScope, origin: MessageOrigin | None, status: JobStatus) -> None:
        self.started.append(status)

    def accepted(self, scope: TrustedScope) -> None:
        self.accepts += 1

    def run_ended(self, scope: TrustedScope) -> None:
        pass

    def job_ended(self, scope: TrustedScope, status: JobStatus) -> None:
        self.ended.append(status)


def free_runner(*script: object, tools: tuple[object, ...] = ()) -> tuple[object, BackgroundRunner, Told]:
    rt, _ = runtime(list(script), tools=tools)  # type: ignore[arg-type]
    told = Told()
    return rt, BackgroundRunner(rt.driver, listener=told), told


def test_a_pass_shows_nothing_and_leaves_no_answer() -> None:
    rt, runner, told = free_runner(final("[[mood:neutral]] [[pass]] "))

    async def scenario() -> None:
        started = await runner.submit_wake(SCOPE, FREE, "d1", free=True)
        assert started.hidden and runner.shown_status(SCOPE).running is False
        assert runner.shown_running(SCOPE.principal_id) == () and runner.running(SCOPE.principal_id)
        done = await runner.wait(SCOPE)
        assert done.result is not None and done.result.state is DriveState.COMPLETED and done.hidden
        wake = await rt.data.read_wake(SCOPE.principal_id, started.wake_id)  # type: ignore[attr-defined]
        assert wake.status is WakeStatus.PASSED
        assert await rt.data.read_events(SCOPE) == ()  # type: ignore[attr-defined]  # the wake never was

    asyncio.run(scenario())
    assert told.started == [] and told.accepts == 0  # no job event, no "conversation" to read


def test_a_free_wake_that_speaks_shows_itself_first_and_loses_its_pass() -> None:
    rt, runner, told = free_runner(final("[[pass]]한참 했네, 물 한 잔 어때?"))

    async def scenario() -> None:
        started = await runner.submit_wake(SCOPE, FREE, "d1", free=True)
        assert await rt.data.unshown_wakes(SCOPE) == frozenset({1})  # type: ignore[attr-defined]
        await runner.wait(SCOPE)
        wake = await rt.data.read_wake(SCOPE.principal_id, started.wake_id)  # type: ignore[attr-defined]
        assert wake.status is WakeStatus.SPEAKING  # shown; the bootstrap's listener ends it
        shown, answer = await rt.data.read_events(SCOPE)  # type: ignore[attr-defined]
        assert isinstance(shown.payload, Wake) and isinstance(answer.payload, AssistantFinal)
        assert answer.payload.blocks == (TextBlock("한참 했네, 물 한 잔 어때?"),)
        assert await rt.data.unshown_wakes(SCOPE) == frozenset()  # type: ignore[attr-defined]

    asyncio.run(scenario())
    assert [s.hidden for s in told.started] == [False] and told.accepts == 1 and not told.ended[-1].hidden


def test_a_tool_call_shows_it_before_the_tool_runs() -> None:
    from testkit import RecordingTool

    look = RecordingTool("look")
    rt, runner, told = free_runner(calls(("look", {})), final("[[pass]]"), tools=(look,))

    async def scenario() -> None:
        started = await runner.submit_wake(SCOPE, FREE, "d1", free=True)
        await runner.wait(SCOPE)
        wake = await rt.data.read_wake(SCOPE.principal_id, started.wake_id)  # type: ignore[attr-defined]
        assert wake.status is WakeStatus.SPEAKING and len(look.invocations) == 1
        kinds = [e.kind for e in await rt.data.read_events(SCOPE)]  # type: ignore[attr-defined]
        assert kinds[0] is EventKind.WAKE and EventKind.ASSISTANT_TOOL_CALLS in kinds  # no pass after showing

    asyncio.run(scenario())
    assert len(told.started) == 1


def test_a_free_wake_runs_two_runs_at_most() -> None:
    step = calls(("look", {}))
    from testkit import RecordingTool

    rt, runner, _ = free_runner(*([step] * (FREE_RUNS + 2)), tools=(RecordingTool("look"),))

    async def scenario() -> None:
        await runner.submit_wake(SCOPE, FREE, "d1", free=True)
        done = await runner.wait(SCOPE)
        assert done.result is not None and done.result.state is DriveState.RUN_LIMIT_REACHED

    asyncio.run(scenario())


def test_the_user_writing_before_it_speaks_drops_it_unseen() -> None:
    rt, runner, told = free_runner(final("[[pass]]"), final("응, 무슨 일이야?"))
    rt.driver._provider._first_delay = 0.0  # type: ignore[attr-defined]

    async def scenario() -> None:
        started = await runner.submit_wake(SCOPE, FREE, "d1", free=True)
        await runner.submit(SCOPE, user("저기"))  # the user wins
        await runner.wait(SCOPE)
        wake = await rt.data.read_wake(SCOPE.principal_id, started.wake_id)  # type: ignore[attr-defined]
        assert wake.status in (WakeStatus.PASSED, WakeStatus.OPENED)  # passed, or cut before its Run
        events = await rt.data.read_events(SCOPE)  # type: ignore[attr-defined]
        assert all(not isinstance(e.payload, Wake) or wake.status is WakeStatus.OPENED for e in events)

    asyncio.run(scenario())
    assert all(not s.hidden for s in told.started)  # only the user's job was told


@pytest.mark.parametrize(("output", "status", "live", "verdict"), [
    (NormalizedFinal((TextBlock("[[pass]]"),)), WakeStatus.OPENED, True, WakeStatus.PASSED),
    (NormalizedFinal((TextBlock("[[mood:happy]]  "),)), WakeStatus.OPENED, True, WakeStatus.PASSED),
    (NormalizedFinal((TextBlock("[[pass]] 아냐, 하나만"),)), WakeStatus.OPENED, True, None),
    (NormalizedFinal((TextBlock("[[pa"),)), WakeStatus.OPENED, True, None),
    (NormalizedFinal((TextBlock("한참 했네"),)), WakeStatus.OPENED, False, WakeStatus.DROPPED),  # found at a restart
    (NormalizedFinal((TextBlock("[[pass]]"),)), WakeStatus.OPENED, False, WakeStatus.PASSED),
    (NormalizedFinal((TextBlock("아무 말"),)), WakeStatus.PASSED, False, WakeStatus.PASSED),
    (NormalizedFinal((TextBlock("아무 말"),)), WakeStatus.DROPPED, True, WakeStatus.DROPPED),
    (NormalizedFinal((TextBlock("[[pass]]"),)), WakeStatus.SPEAKING, True, None),  # shown: never hidden again
    (NormalizedToolCalls(()), WakeStatus.OPENED, False, WakeStatus.DROPPED),
])
def test_the_verdict_is_the_same_live_and_at_a_restart(output: object, status: WakeStatus, live: bool,
                                                       verdict: WakeStatus | None) -> None:
    """PR #162 review P1-10: one function decides, live and for a Run found RECEIVED at a restart."""
    assert free_wake_verdict(output, status, live=live) == verdict  # type: ignore[arg-type]


def test_a_spoken_pass_mark_is_taken_out() -> None:
    assert without_pass(NormalizedFinal((TextBlock(f"{PASS}응"),))) == NormalizedFinal((TextBlock("응"),))
