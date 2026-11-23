"""Cancel (V3-P55, CTRL-04) end to end on both Data backends.

A cancel interrupts what is under way: an interrupted model call is recorded
as OUTCOME_UNKNOWN and never resent, never-started calls are closed with a
CANCELLED result. A new message redirects: it closes never-started calls.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from testkit import FAKE, SCOPE, RecordingTool, calls, final, runtime, text_result, user

from himari.bootstrap import Runtime
from himari.common import RunId, WorkspaceId
from himari.control.api import DriveState, StopSignal
from himari.data.api import AssistantToolCalls, RunStatus, ToolExecutionStatus
from himari.prompt.api import ContextBudget, ToolGroupTurn, UserTurn
from himari.prompt.compaction import COMPACTION_INSTRUCTIONS
from himari.provider.api import NativeInvocationOutcome, NativePreparedRequest, NativeReceived, OutputSink, RawCapture
from himari.provider.scripted import ScriptedAdapter, ScriptStep, encode_output
from himari.tool.api import EffectProfile, ToolContext, ToolInvocation, ToolOutcome, ToolStatus, WorkspaceRequirement


class GatedProvider(ScriptedAdapter):
    """Holds the next model call until ``release`` (once set, calls pass)."""

    def __init__(self, script: list[ScriptStep]) -> None:
        super().__init__(script)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.interrupted = 0

    async def invoke_prepared(self, request: NativePreparedRequest, sink: OutputSink | None = None,
                              capture: RawCapture | None = None) -> NativeInvocationOutcome:
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.interrupted += 1
            raise
        return await super().invoke_prepared(request, sink, capture)


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


async def _cut(rt: Runtime, slow: SlowTool) -> RunId:
    """A group a shutdown cut: the job's task is cancelled during its first call,
    which was never claimed; every call of the group is left PENDING."""
    job = asyncio.create_task(rt.driver.drive(SCOPE, user("go")))
    await slow.entered.wait()
    job.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await job
    group = next(e for e in reversed(await rt.data.read_events(SCOPE)) if isinstance(e.payload, AssistantToolCalls))
    assert group.origin_run_id is not None
    return group.origin_run_id


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


def test_cancel_when_idle_closes_a_cut_group() -> None:
    async def scenario() -> None:
        slow, later = SlowTool("slow"), RecordingTool("later")
        rt, _ = runtime([calls(("slow", {}), ("later", {})), final("new turn")], tools=[slow, later])
        assert (await rt.driver.cancel(SCOPE)).state is DriveState.IDLE  # nothing yet
        run_id = await _cut(rt, slow)
        assert [r.status for r in await _records(rt, run_id)] == [ToolExecutionStatus.PENDING] * 2

        assert (await rt.driver.cancel(SCOPE)).state is DriveState.CANCELLED
        assert [r.status for r in await _records(rt, run_id)] == [ToolExecutionStatus.CANCELLED] * 2
        assert slow.invocations == [] and later.invocations == []
        assert (await rt.driver.cancel(SCOPE)).state is DriveState.IDLE  # already closed

    asyncio.run(scenario())


def test_a_new_message_closes_the_pending_calls() -> None:
    async def scenario() -> None:
        slow, planned = SlowTool("slow"), RecordingTool("planned")
        rt, provider = runtime([calls(("slow", {}), ("planned", {})), final("redirected")], tools=[slow, planned])
        await _cut(rt, slow)

        await rt.driver.accept(SCOPE, user("do this instead"))
        result = await rt.driver.resume(SCOPE)

        assert result.state is DriveState.COMPLETED and planned.invocations == []
        group, message = provider.prepared_inputs[-1].conversation[-2:]
        assert isinstance(group, ToolGroupTurn) and isinstance(message, UserTurn)
        assert all(r.is_error and "sent a new message" in r.blocks[0].text  # type: ignore[union-attr]
                   for r in group.results.values())

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


# --- a cancel at the edges (PR #23 review B2–B4) ---------------------------------------------


class StopsDuring(ScriptedAdapter):
    """Cancels while answering the ``nth`` request that matches ``which`` — a
    cancel that arrives with the answer — then answers. Summarizing requests
    are answered "S1", "S2", …"""

    def __init__(self, script: list[ScriptStep], stop: StopSignal, nth: int = 1, which: str = "compaction") -> None:
        super().__init__(script)
        self.stop, self.nth, self.which = stop, nth, which
        self.seen = 0
        self.summaries = 0

    async def invoke_prepared(self, request: NativePreparedRequest, sink: OutputSink | None = None,
                              capture: RawCapture | None = None) -> NativeInvocationOutcome:
        compaction = self.prepared_inputs[-1].stable_prefix == (COMPACTION_INSTRUCTIONS,)
        if compaction == (self.which == "compaction"):
            self.seen += 1
            if self.seen == self.nth:
                self.stop.cancel()
        if not compaction:
            return await super().invoke_prepared(request, sink, capture)
        self.sent.append(request)
        self.summaries += 1
        return NativeReceived(encode_output(final(f"S{self.summaries}")), "application/json")


def test_b2_a_cancel_arriving_with_the_answer_commits_nothing() -> None:
    async def scenario() -> None:
        stop = StopSignal()
        summarizer = StopsDuring([final("a1"), final("a2")], stop)
        rt, _ = runtime(provider=summarizer, context_budget=ContextBudget(limit_tokens=1000))
        await rt.driver.drive(SCOPE, user("q1"))
        await rt.driver.drive(SCOPE, user("q2"))
        result = await rt.driver.compact(SCOPE, stop=stop)
        assert result.state is DriveState.CANCELLED and summarizer.summaries == 1
        assert await rt.data.get_latest_snapshot(SCOPE) is None
        run = await rt.data.read_run(SCOPE, result.compaction_run_ids[0])
        assert run.status is RunStatus.PROJECTION_FAILED and run.error_code == "cancelled_by_user"

        stop2 = StopSignal()
        turn = StopsDuring([final("an answer nobody waits for")], stop2, which="turn")
        rt2, _ = runtime(provider=turn)
        cancelled = await rt2.driver.drive(SCOPE, user("q"), stop=stop2)
        assert cancelled.state is DriveState.CANCELLED
        run = await rt2.data.read_run(SCOPE, cancelled.run_ids[0])
        assert run.status is RunStatus.PROJECTION_FAILED  # the raw answer is kept, never projected
        assert [e.kind.value for e in await rt2.data.read_events(SCOPE)] == ["user_message"]

    asyncio.run(scenario())


def test_b3_a_cancel_during_the_last_step_outranks_the_run_limit() -> None:
    async def scenario() -> None:
        slow = SlowTool("slow")
        rt, _ = runtime([calls(("slow", {})), final("never")], tools=[slow], max_runs_per_drive=1)
        stop = StopSignal()
        job = asyncio.create_task(rt.driver.drive(SCOPE, user("go"), stop=stop))
        await slow.entered.wait()
        stop.cancel()
        assert (await job).state is DriveState.CANCELLED  # not RUN_LIMIT_REACHED

    asyncio.run(scenario())


def test_b4_a_cancel_as_a_call_finishes_closes_the_calls_left() -> None:
    async def scenario() -> None:
        stop = StopSignal()

        def cancel_as_it_ends(invocation: ToolInvocation, ctx: ToolContext) -> ToolOutcome:
            stop.cancel()  # while this call finishes: it keeps its result, the next one never starts
            return ToolOutcome(ToolStatus.SUCCEEDED, text_result("done"))

        first = RecordingTool("first", behavior=cancel_as_it_ends)
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
