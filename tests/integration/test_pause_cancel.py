"""Pause and cancel (V3-P55, CTRL-03/04) end to end on both Data backends.

A pause lets the model call or Tool call under way finish and starts
nothing new; never-started calls stay PENDING for ``resume``. A cancel
interrupts what is under way: an interrupted model call is recorded as
OUTCOME_UNKNOWN and never resent, never-started calls are closed with a
CANCELLED result. A new message redirects: it closes never-started calls.
"""

from __future__ import annotations

import asyncio
from typing import Any

from testkit import FAKE, SCOPE, RecordingTool, calls, final, runtime, text_result, user

from himari.common import WorkspaceId
from himari.control.api import DriveState, StopSignal
from himari.data.api import RunStatus, ToolExecutionStatus
from himari.prompt.api import ContextBudget, ToolGroupTurn, UserTurn
from himari.prompt.compaction import COMPACTION_INSTRUCTIONS
from himari.provider.api import NativeInvocationOutcome, NativePreparedRequest, NativeReceived
from himari.provider.scripted import Measured, ScriptedAdapter, ScriptStep, encode_output
from himari.tool.api import EffectProfile, ToolContext, ToolInvocation, ToolOutcome, ToolStatus, WorkspaceRequirement


class GatedProvider(ScriptedAdapter):
    """Holds the next model call until ``release`` (once set, calls pass)."""

    def __init__(self, script: list[ScriptStep]) -> None:
        super().__init__(script)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.interrupted = 0

    async def invoke_prepared(self, request: NativePreparedRequest) -> NativeInvocationOutcome:
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.interrupted += 1
            raise
        return await super().invoke_prepared(request)


class SlowTool(RecordingTool):
    """Holds each call until ``release`` (once set, calls pass)."""

    def __init__(self, tool_id: str, **kwargs: Any) -> None:
        super().__init__(tool_id, **kwargs)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.interrupted = 0

    async def run(self, invocation: ToolInvocation, ctx: ToolContext) -> ToolOutcome:
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.interrupted += 1
            raise
        return await super().run(invocation, ctx)


async def _records(rt: Any, run_id: Any) -> list[Any]:
    return list(await rt.data.read_tool_executions(SCOPE, run_id))


def test_a_pause_during_the_model_call_lets_it_finish_and_starts_no_tool() -> None:
    async def scenario() -> None:
        first, second = RecordingTool("first"), RecordingTool("second")
        provider = GatedProvider([calls(("first", {}), ("second", {})), final("done")])
        rt, _ = runtime(provider=provider, tools=[first, second])
        stop = StopSignal()
        job = asyncio.create_task(rt.driver.drive(SCOPE, user("go"), stop=stop))
        await provider.entered.wait()
        stop.pause()
        provider.release.set()
        paused = await job

        assert paused.state is DriveState.PAUSED and len(paused.run_ids) == 1
        assert first.invocations == [] and second.invocations == []
        statuses = [r.status for r in await _records(rt, paused.run_ids[0])]
        assert statuses == [ToolExecutionStatus.PENDING, ToolExecutionStatus.PENDING]

        resumed = await rt.driver.resume(SCOPE)
        assert resumed.state is DriveState.COMPLETED
        assert len(first.invocations) == 1 and len(second.invocations) == 1

    asyncio.run(scenario())


def test_a_pause_during_a_tool_call_finishes_it_and_keeps_the_rest_pending() -> None:
    async def scenario() -> None:
        slow, after = SlowTool("slow"), RecordingTool("after")
        rt, provider = runtime([calls(("slow", {}), ("after", {})), final("done")], tools=[slow, after])
        stop = StopSignal()
        job = asyncio.create_task(rt.driver.drive(SCOPE, user("go"), stop=stop))
        await slow.entered.wait()
        stop.pause()
        slow.release.set()
        paused = await job

        assert paused.state is DriveState.PAUSED
        assert len(slow.invocations) == 1 and slow.interrupted == 0 and after.invocations == []
        statuses = [r.status for r in await _records(rt, paused.run_ids[0])]
        assert statuses == [ToolExecutionStatus.SUCCEEDED, ToolExecutionStatus.PENDING]
        assert len(provider.sent) == 1

        resumed = await rt.driver.resume(SCOPE)
        assert resumed.state is DriveState.COMPLETED
        assert len(slow.invocations) == 1 and len(after.invocations) == 1  # nothing ran twice

    asyncio.run(scenario())


def test_a_pause_does_not_hide_a_final_answer_or_a_question() -> None:
    async def scenario() -> None:
        provider = GatedProvider([final("answer")])
        rt, _ = runtime(provider=provider)
        stop = StopSignal()
        job = asyncio.create_task(rt.driver.drive(SCOPE, user("q"), stop=stop))
        await provider.entered.wait()
        stop.pause()
        provider.release.set()
        assert (await job).state is DriveState.COMPLETED

        asking = GatedProvider([calls(("ask_user", {"question": "which?"}))])
        rt2, _ = runtime(provider=asking)
        stop2 = StopSignal()
        job2 = asyncio.create_task(rt2.driver.drive(SCOPE, user("q"), stop=stop2))
        await asking.entered.wait()
        stop2.pause()
        asking.release.set()
        assert (await job2).state is DriveState.WAITING_FOR_USER

    asyncio.run(scenario())


def test_a_cancel_interrupts_the_model_call_records_it_and_never_resends() -> None:
    async def scenario() -> None:
        provider = GatedProvider([final("late answer"), final("fresh answer")])
        rt, _ = runtime(provider=provider)
        stop = StopSignal()
        job = asyncio.create_task(rt.driver.drive(SCOPE, user("q"), stop=stop))
        await provider.entered.wait()
        stop.cancel()
        cancelled = await job

        assert cancelled.state is DriveState.CANCELLED and provider.interrupted == 1
        run = await rt.data.read_run(SCOPE, cancelled.run_ids[0])
        assert run.status is RunStatus.OUTCOME_UNKNOWN and run.error_code == "cancelled_by_user"
        assert len(provider.sent) == 0  # interrupted before the scripted send completed; nothing resent
        assert [e.kind.value for e in await rt.data.read_events(SCOPE)] == ["user_message"]

        provider.release.set()
        again = await rt.driver.resume(SCOPE)  # the unanswered message: a new Run, on request
        assert again.state is DriveState.COMPLETED and cancelled.run_ids[0] not in again.run_ids

    asyncio.run(scenario())


def test_a_cancel_interrupts_a_tool_call_and_closes_the_rest() -> None:
    async def scenario() -> None:
        slow, after = SlowTool("slow"), RecordingTool("after")
        rt, provider = runtime([calls(("slow", {}), ("after", {})), final("understood")], tools=[slow, after])
        stop = StopSignal()
        job = asyncio.create_task(rt.driver.drive(SCOPE, user("go"), stop=stop))
        await slow.entered.wait()
        stop.cancel()
        cancelled = await job

        assert cancelled.state is DriveState.CANCELLED
        assert slow.interrupted == 1 and slow.invocations == [] and after.invocations == []
        records = await _records(rt, cancelled.run_ids[0])
        assert [r.status for r in records] == [ToolExecutionStatus.CANCELLED] * 2
        assert all("cancelled" in r.model_result.blocks[0].text for r in records)
        assert len(provider.sent) == 1  # no Run after the cancel

        nxt = await rt.driver.drive(SCOPE, user("ok, stop there"))
        assert nxt.state is DriveState.COMPLETED
        group = provider.prepared_inputs[-1].conversation[-2]
        assert isinstance(group, ToolGroupTurn) and all(r.is_error for r in group.results.values())

    asyncio.run(scenario())


def test_a_cancelled_write_is_reported_as_unknown_never_rerun() -> None:
    async def scenario() -> None:
        write = SlowTool("fake/write", effect=EffectProfile.SCOPED_WRITE, environment_kind=FAKE,
                         workspace=WorkspaceRequirement.DEFAULT)
        rt, provider = runtime([calls(("fake/write", {})), final("checked")], env_tools=[write])
        rt.store.activate_environment(SCOPE, FAKE, WorkspaceId("ws_1"))
        stop = StopSignal()
        job = asyncio.create_task(rt.driver.drive(SCOPE, user("write it"), stop=stop))
        await write.entered.wait()
        stop.cancel()
        cancelled = await job

        assert cancelled.state is DriveState.CANCELLED and write.interrupted == 1
        (record,) = await _records(rt, cancelled.run_ids[0])
        assert record.status is ToolExecutionStatus.UNKNOWN  # claimed: the effect may have happened
        assert "Outcome unknown" in record.model_result.blocks[0].text
        write.release.set()
        assert (await rt.driver.resume(SCOPE)).state is DriveState.COMPLETED
        assert write.invocations == []  # never run again

    asyncio.run(scenario())


def test_cancel_when_idle_closes_a_paused_group_or_a_question() -> None:
    async def scenario() -> None:
        tool = RecordingTool("later")
        provider = GatedProvider([calls(("later", {})), final("new turn")])
        rt, _ = runtime(provider=provider, tools=[tool])
        assert (await rt.driver.cancel(SCOPE)).state is DriveState.IDLE  # nothing yet
        stop = StopSignal()
        job = asyncio.create_task(rt.driver.drive(SCOPE, user("go"), stop=stop))
        await provider.entered.wait()
        stop.pause()
        provider.release.set()
        paused = await job
        assert paused.state is DriveState.PAUSED

        assert (await rt.driver.cancel(SCOPE)).state is DriveState.CANCELLED
        (record,) = await _records(rt, paused.run_ids[0])
        assert record.status is ToolExecutionStatus.CANCELLED and tool.invocations == []
        assert (await rt.driver.cancel(SCOPE)).state is DriveState.IDLE  # already closed

        asking, _ = runtime([calls(("ask_user", {"question": "which?"})), final("fresh")])
        assert (await asking.driver.drive(SCOPE, user("q"))).state is DriveState.WAITING_FOR_USER
        assert (await asking.driver.cancel(SCOPE)).state is DriveState.CANCELLED
        assert await asking.data.find_waiting_call(SCOPE) is None
        answered = await asking.driver.drive(SCOPE, user("something else"))
        assert answered.state is DriveState.COMPLETED
        last = (await asking.data.read_events(SCOPE))[-2]
        assert last.payload == user("something else")  # a new turn (T1), not an answer to the question

    asyncio.run(scenario())


def test_a_new_message_while_paused_closes_the_pending_calls() -> None:
    async def scenario() -> None:
        tool = RecordingTool("planned")
        provider = GatedProvider([calls(("planned", {})), final("redirected")])
        rt, _ = runtime(provider=provider, tools=[tool])
        stop = StopSignal()
        job = asyncio.create_task(rt.driver.drive(SCOPE, user("go"), stop=stop))
        await provider.entered.wait()
        stop.pause()
        provider.release.set()
        assert (await job).state is DriveState.PAUSED

        await rt.driver.accept(SCOPE, user("do this instead"))
        result = await rt.driver.resume(SCOPE)

        assert result.state is DriveState.COMPLETED and tool.invocations == []
        group, message = provider.prepared_inputs[-1].conversation[-2:]
        assert isinstance(group, ToolGroupTurn) and isinstance(message, UserTurn)
        (closed,) = group.results.values()
        assert closed.is_error and "sent a new message" in closed.blocks[0].text  # type: ignore[union-attr]

    asyncio.run(scenario())


def test_a_cancel_during_manual_compaction_commits_nothing() -> None:
    async def scenario() -> None:
        provider = GatedProvider([final("a1"), final("a2"), final("SUMMARY")])
        rt, _ = runtime(provider=provider, context_budget=ContextBudget(limit_tokens=1000))
        provider.release.set()
        await rt.driver.drive(SCOPE, user("q1"))
        await rt.driver.drive(SCOPE, user("q2"))
        provider.release.clear()
        provider.entered.clear()
        stop = StopSignal()
        job = asyncio.create_task(rt.driver.compact(SCOPE, stop=stop))
        await provider.entered.wait()
        stop.cancel()
        result = await job

        assert result.state is DriveState.CANCELLED and len(result.compaction_run_ids) == 1
        assert await rt.data.get_latest_snapshot(SCOPE) is None
        run = await rt.data.read_run(SCOPE, result.compaction_run_ids[0])
        assert run.status is RunStatus.OUTCOME_UNKNOWN

    asyncio.run(scenario())


# --- stop requests at the edges (PR #23 review B1–B4) ---------------------------------------------


class StopsDuring(ScriptedAdapter):
    """Calls ``act(stop)`` while answering the ``nth`` request that matches
    ``which`` — a pause or cancel that arrives with the answer — then
    answers. Summarizing requests are answered "S1", "S2", …"""

    def __init__(self, script: list[ScriptStep], stop: StopSignal, act: str, nth: int = 1,
                 which: str = "compaction") -> None:
        super().__init__(script)
        self.stop, self.act, self.nth, self.which = stop, act, nth, which
        self.seen = 0
        self.summaries = 0

    async def invoke_prepared(self, request: NativePreparedRequest) -> NativeInvocationOutcome:
        compaction = self.prepared_inputs[-1].stable_prefix == (COMPACTION_INSTRUCTIONS,)
        if compaction == (self.which == "compaction"):
            self.seen += 1
            if self.seen == self.nth:
                getattr(self.stop, self.act)()
        if not compaction:
            return await super().invoke_prepared(request)
        self.sent.append(request)
        self.summaries += 1
        return NativeReceived(encode_output(final(f"S{self.summaries}")), "application/json")


async def _chain_ready(adapter: ScriptedAdapter) -> Any:
    """A Conversation whose next compaction is a chain of several calls."""
    rt, _ = runtime(provider=adapter, context_budget=ContextBudget(limit_tokens=2000))
    await rt.driver.drive(SCOPE, user("가" * 1700))
    return rt


def test_b1_a_pause_between_summarizing_calls_stops_the_chain_and_commits_nothing() -> None:
    async def scenario() -> None:
        stop = StopSignal()
        manual = StopsDuring([Measured(final("a1"), 1700)], stop, "pause")
        rt = await _chain_ready(manual)
        result = await rt.driver.compact(SCOPE, stop=stop)
        assert result.state is DriveState.PAUSED and manual.summaries == 1  # the second call never started
        assert await rt.data.get_latest_snapshot(SCOPE) is None

        stop2 = StopSignal()
        auto = StopsDuring([Measured(final("a1"), 1700), final("done")], stop2, "pause")
        rt2 = await _chain_ready(auto)
        paused = await rt2.driver.drive(SCOPE, user("q2"), stop=stop2)  # compacts before the Run
        assert paused.state is DriveState.PAUSED and paused.run_ids == () and auto.summaries == 1
        assert await rt2.data.get_latest_snapshot(SCOPE) is None
        resumed = await rt2.driver.resume(SCOPE)
        assert resumed.state is DriveState.COMPLETED and await rt2.data.get_latest_snapshot(SCOPE) is not None

    asyncio.run(scenario())


def test_b2_a_cancel_arriving_with_the_answer_commits_nothing() -> None:
    async def scenario() -> None:
        stop = StopSignal()
        summarizer = StopsDuring([final("a1"), final("a2")], stop, "cancel")
        rt, _ = runtime(provider=summarizer, context_budget=ContextBudget(limit_tokens=1000))
        await rt.driver.drive(SCOPE, user("q1"))
        await rt.driver.drive(SCOPE, user("q2"))
        result = await rt.driver.compact(SCOPE, stop=stop)
        assert result.state is DriveState.CANCELLED and summarizer.summaries == 1
        assert await rt.data.get_latest_snapshot(SCOPE) is None
        run = await rt.data.read_run(SCOPE, result.compaction_run_ids[0])
        assert run.status is RunStatus.PROJECTION_FAILED and run.error_code == "cancelled_by_user"

        stop2 = StopSignal()
        turn = StopsDuring([final("an answer nobody waits for")], stop2, "cancel", which="turn")
        rt2, _ = runtime(provider=turn)
        cancelled = await rt2.driver.drive(SCOPE, user("q"), stop=stop2)
        assert cancelled.state is DriveState.CANCELLED
        run = await rt2.data.read_run(SCOPE, cancelled.run_ids[0])
        assert run.status is RunStatus.PROJECTION_FAILED  # the raw answer is kept, never projected
        assert [e.kind.value for e in await rt2.data.read_events(SCOPE)] == ["user_message"]

    asyncio.run(scenario())


def test_b3_a_stop_during_the_last_step_outranks_the_run_limit() -> None:
    async def scenario() -> None:
        slow = SlowTool("slow")
        rt, _ = runtime([calls(("slow", {})), final("never")], tools=[slow], max_runs_per_drive=1)
        stop = StopSignal()
        job = asyncio.create_task(rt.driver.drive(SCOPE, user("go"), stop=stop))
        await slow.entered.wait()
        stop.cancel()
        assert (await job).state is DriveState.CANCELLED

        last = SlowTool("last")
        rt2, _ = runtime([calls(("last", {})), final("next time")], tools=[last], max_runs_per_drive=1)
        stop2 = StopSignal()
        job2 = asyncio.create_task(rt2.driver.drive(SCOPE, user("go"), stop=stop2))
        await last.entered.wait()
        stop2.pause()
        last.release.set()
        assert (await job2).state is DriveState.PAUSED  # not RUN_LIMIT_REACHED
        assert (await rt2.driver.resume(SCOPE)).state is DriveState.COMPLETED

    asyncio.run(scenario())


def test_b4_a_cancel_right_after_a_pause_closes_the_calls_left() -> None:
    async def scenario() -> None:
        stop = StopSignal()

        def pause_then_cancel(invocation: ToolInvocation, ctx: ToolContext) -> ToolOutcome:
            stop.pause()
            stop.cancel()  # both while this call finishes: the group stops PAUSED, then the cancel lands
            return ToolOutcome(ToolStatus.SUCCEEDED, text_result("done"))

        first = RecordingTool("first", behavior=pause_then_cancel)
        second = RecordingTool("second")
        rt, _ = runtime([calls(("first", {}), ("second", {})), final("never")], tools=[first, second])
        result = await rt.driver.drive(SCOPE, user("go"), stop=stop)

        assert result.state is DriveState.CANCELLED and second.invocations == []
        statuses = [r.status for r in await _records(rt, result.run_ids[0])]
        assert statuses == [ToolExecutionStatus.SUCCEEDED, ToolExecutionStatus.CANCELLED]  # nothing left for Continue

    asyncio.run(scenario())


def test_a_cancel_while_a_writes_images_are_kept_keeps_its_definite_result() -> None:
    """V3-P67 (PR #62 review B1): the write ran and returned; a cancel while
    its image copies are made never makes it uncertain."""
    import io

    from PIL import Image

    from himari.data.api import PreparedView
    from himari.tool.api import ResultImage

    out = io.BytesIO()
    Image.new("RGB", (4, 3)).save(out, "PNG")

    class Hanging:
        available = True

        def __init__(self) -> None:
            self.entered = asyncio.Event()

        async def make(self, original: bytes) -> PreparedView | str:
            self.entered.set()
            await asyncio.Event().wait()
            return "never"

    async def scenario() -> None:
        views = Hanging()
        write = RecordingTool("fake/shot", effect=EffectProfile.SCOPED_WRITE, environment_kind=FAKE,
                              workspace=WorkspaceRequirement.DEFAULT,
                              behavior=lambda i, c: ToolOutcome(ToolStatus.SUCCEEDED, text_result("shot"),
                                                                result_images=(ResultImage(out.getvalue(), "image/png"),)))
        rt, _ = runtime([calls(("fake/shot", {})), final("done")], env_tools=[write], image_views=views)
        rt.store.activate_environment(SCOPE, FAKE, WorkspaceId("ws_1"))
        stop = StopSignal()
        job = asyncio.create_task(rt.driver.drive(SCOPE, user("take it"), stop=stop))
        await views.entered.wait()
        stop.cancel()
        cancelled = await job

        assert cancelled.state is DriveState.CANCELLED
        (record,) = await _records(rt, cancelled.run_ids[0])
        assert record.status is ToolExecutionStatus.SUCCEEDED  # committed; cancel_group left it alone
        assert record.model_result.blocks[-1].text == "[an image was returned but not kept: the call was cancelled]"
        assert len(write.invocations) == 1

    asyncio.run(scenario())
