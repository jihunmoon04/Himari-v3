"""V3-P90 §7.3: a free wake's turn — hidden until it speaks (no job event, not working, its Event
not shown), a pass finalized with no Event, a tool call shows it first, the user winning drops it,
and the same verdict for a Run found at a restart."""

from __future__ import annotations

import asyncio

import pytest
from testkit import SCOPE, calls, final, runtime, user

from himari.common import RunId, TextBlock, TrustedScope
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
    assert told.started[0].free and told.started[0].wake_id is not None  # V3-P90 §9: its job event says so


def test_a_tool_call_shows_it_before_the_tool_runs_and_a_pass_after_it_says_nothing() -> None:
    """PR #167 review P1-2: the Run after the tool round is the same wake's — its [[pass]] is never an answer."""
    from testkit import RecordingTool

    look = RecordingTool("look")
    rt, runner, told = free_runner(calls(("look", {})), final("[[pass]]"), tools=(look,))

    async def scenario() -> None:
        started = await runner.submit_wake(SCOPE, FREE, "d1", free=True)
        done = await runner.wait(SCOPE)
        assert done.result is not None and done.result.state is DriveState.INVALID_MODEL_OUTPUT  # a visible failure
        wake = await rt.data.read_wake(SCOPE.principal_id, started.wake_id)  # type: ignore[attr-defined]
        assert wake.status is WakeStatus.SPEAKING and len(look.invocations) == 1
        events = await rt.data.read_events(SCOPE)  # type: ignore[attr-defined]
        assert events[0].kind is EventKind.WAKE and not any(isinstance(e.payload, AssistantFinal) for e in events)

    asyncio.run(scenario())
    assert len(told.started) == 1


def test_after_a_tool_round_the_pass_mark_is_taken_out_of_the_answer() -> None:
    from testkit import RecordingTool

    rt, runner, _ = free_runner(calls(("look", {})), final("[[pass]] 날씨 좋네!"), tools=(RecordingTool("look"),))

    async def scenario() -> None:
        await runner.submit_wake(SCOPE, FREE, "d1", free=True)
        await runner.wait(SCOPE)
        answer = (await rt.data.read_events(SCOPE))[-1]  # type: ignore[attr-defined]
        assert isinstance(answer.payload, AssistantFinal) and answer.payload.blocks == (TextBlock(" 날씨 좋네!"),)

    asyncio.run(scenario())


def test_a_speaking_wake_is_in_progress_for_the_next_wake() -> None:
    """PR #167 review P1-1: shown, its job over with no answer, not ended yet (the race before the
    bootstrap's listener ends it) — it keeps the next wake out; ended, it does not."""
    from himari.control.api import ControlError, ControlErrorCode

    rt, _ = runtime([])

    async def scenario() -> None:
        event = await rt.data.begin_wake(SCOPE, FREE, "d1")
        assert isinstance(event.payload, Wake)
        wake_id = event.payload.wake_id
        assert await rt.data.reveal_wake(SCOPE.principal_id, wake_id)
        with pytest.raises(ControlError) as refused:
            await rt.driver.accept_wake(SCOPE, FREE, "d1")
        assert refused.value.code is ControlErrorCode.NOT_SETTLED
        await rt.data.end_wake(SCOPE.principal_id, wake_id, WakeStatus.FAILED)
        assert (await rt.driver.accept_wake(SCOPE, FREE, "d1")).kind is EventKind.WAKE

    asyncio.run(scenario())


def test_a_free_wake_withdrawn_before_it_spoke_is_dropped_unseen() -> None:
    """PR #167 review P2-5: the model turned out unchecked — never shown, so dropped, not cancelled."""
    from himari.provider.scripted import ScriptedAdapter

    adapter = ScriptedAdapter([final("x")], free_wake=False)
    rt, _ = runtime(provider=adapter)
    runner = BackgroundRunner(rt.driver)

    async def scenario() -> None:
        started = await runner.submit_wake(SCOPE, FREE, "d1", free=True)
        await runner.wait(SCOPE)
        assert started.wake_id is not None
        wake = await rt.data.read_wake(SCOPE.principal_id, started.wake_id)
        assert wake.status is WakeStatus.DROPPED and await rt.data.read_events(SCOPE) == ()

    asyncio.run(scenario())


def test_the_pass_mark_is_taken_out_of_a_stream_that_cuts_it() -> None:
    """PR #167 review P2-3."""
    from himari.control.driver import _PassFilter

    stream = _PassFilter()
    assert stream.feed("[[pa") == "" and stream.feed("ss]] 안녕") == " 안녕"
    assert stream.feed("다[") == "다" and stream.flush() == "["
    assert stream.feed("[[pass]][[pass]]응") == "응"


def test_it_speaks_as_soon_as_it_cannot_be_a_pass() -> None:
    """PR #167 review P2-4: shown while it streams — Data first, then the job, then what was held."""
    from himari.control.driver import _Held
    from himari.provider.api import OutputDelta

    order: list[str] = []

    class Progress:
        def report(self, usage: object) -> None: ...
        def output_started(self, run_id: object) -> None: order.append("started")
        def output(self, run_id: object, delta: OutputDelta) -> None: order.append(delta.text)
        def output_ended(self, run_id: object) -> None: order.append("ended")
        def reveal(self) -> None: order.append("job")

    async def in_data() -> bool:
        order.append("data")
        return True

    async def scenario() -> None:
        held = _Held(Progress(), "wake_1", in_data)
        held.output_started("r")  # type: ignore[arg-type]
        held.output("r", OutputDelta(text="[[mood:happy]][[pa"))  # type: ignore[arg-type]
        await asyncio.sleep(0)
        assert order == []  # could still be a pass
        held.output("r", OutputDelta(text="ss]]한참"))  # type: ignore[arg-type]
        await held.settled()
        held.output("r", OutputDelta(text=" 했네"))  # type: ignore[arg-type]
        assert order[:3] == ["data", "job", "started"] and "".join(order[3:]) == "[[mood:happy]]한참 했네"

    asyncio.run(scenario())


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


@pytest.mark.parametrize("pieces", [
    ("[[pass]]", " "),
    ("[[pass]]\n",),
    ("[[mood:happy]] [[pass]] ", "\n"),
    ("  [[pa", "ss]]", "  "),
])
def test_a_pass_with_spaces_around_it_is_never_shown(pieces: tuple[str, ...]) -> None:
    """PR #167 re-review P1-6: what is left of a pass but spaces never starts the showing."""
    from himari.control.driver import _Held
    from himari.provider.api import OutputDelta

    shown: list[str] = []

    async def in_data() -> bool:
        shown.append("data")
        return True

    async def scenario() -> None:
        held = _Held(None, "wake_1", in_data)
        for piece in pieces:
            held.output(RunId("r"), OutputDelta(text=piece))
        await held.settled()

    asyncio.run(scenario())
    assert shown == []



def test_a_users_answer_after_a_failed_free_wake_keeps_its_words() -> None:
    """PR #167 re-review P1-8: the wake ran out of Runs after a tool round; the user's next turn is the
    user's — a literal [[pass]] in its answer stays."""
    from testkit import RecordingTool

    step = calls(("look", {}))
    rt, runner, _ = free_runner(step, step, final("[[pass]]는 히마리가 조용히 있겠다는 표시야."),
                                tools=(RecordingTool("look"),))

    async def scenario() -> None:
        await runner.submit_wake(SCOPE, FREE, "d1", free=True)
        done = await runner.wait(SCOPE)
        assert done.result is not None and done.result.state is DriveState.RUN_LIMIT_REACHED
        await runner.submit(SCOPE, user("[[pass]]가 뭐야?"))
        await runner.wait(SCOPE)
        answer = (await rt.data.read_events(SCOPE))[-1]  # type: ignore[attr-defined]
        assert isinstance(answer.payload, AssistantFinal)
        assert answer.payload.blocks == (TextBlock("[[pass]]는 히마리가 조용히 있겠다는 표시야."),)

    asyncio.run(scenario())
