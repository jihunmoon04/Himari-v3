"""Streaming through the Driver and the Runner (V3-P73 §5), on both Data
backends: which Runs stream, what the job's progress is told, and what
is recorded of a stream that ends early — on a cancel, a Provider error,
a broken stream and shutdown (§5.3: what arrived, with the outcome)."""

from __future__ import annotations

import asyncio
from typing import Any

from testkit import SCOPE, RecordingTool, calls, final, runtime, user

from himari.common import RunId
from himari.control.api import LIVE_TEXT_CHARS, DriveState, StopSignal, TurnUsage
from himari.control.driver import CANCELLED_BY_USER, SHUTDOWN
from himari.control.runner import BackgroundRunner
from himari.data.api import RunPurpose, RunStatus
from himari.prompt.api import ContextBudget
from himari.provider.api import NativeUncertain, OutputDelta
from himari.provider.scripted import ScriptedAdapter, ScriptStep, StreamCut

PIECES = ("Half ", "an ", "answer")


class Progress:
    """A job's progress that keeps what it is told, in order."""

    def __init__(self) -> None:
        self.told: list[tuple[Any, ...]] = []

    def report(self, usage: TurnUsage) -> None:
        pass

    def output_started(self, run_id: RunId) -> None:
        self.told.append(("start", run_id))

    def output(self, run_id: RunId, delta: OutputDelta) -> None:
        self.told.append(("out", run_id, delta))

    def output_ended(self, run_id: RunId) -> None:
        self.told.append(("end", run_id))

    def text(self) -> str:
        return "".join(t[2].text for t in self.told if t[0] == "out")


def streaming(script: list[ScriptStep], **options: Any) -> tuple[Any, ScriptedAdapter]:
    return runtime(provider=ScriptedAdapter(script, streaming=True), streaming=True, **options)


async def until(condition: Any) -> None:
    for _ in range(1000):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("never happened")


def test_an_answer_streams_to_the_jobs_progress_and_is_recorded_as_before() -> None:
    async def scenario() -> None:
        rt, adapter = streaming([final("Hello there, Seoul.")])
        progress = Progress()
        done = await rt.driver.drive(SCOPE, user("hi"), progress=progress)
        assert done.state is DriveState.COMPLETED and adapter.sent[0].streamed
        (run_id,) = done.run_ids
        assert progress.told[0] == ("start", run_id) and progress.told[-1] == ("end", run_id)
        assert progress.text() == "Hello there, Seoul."
        assert (await rt.data.read_run(SCOPE, run_id)).status is RunStatus.PROJECTED

    asyncio.run(scenario())


def test_streaming_needs_the_switch_and_a_model_that_declares_it() -> None:
    async def scenario() -> None:
        for rt, adapter in (runtime(provider=ScriptedAdapter([final("a")], streaming=True)),  # switch off
                            runtime(provider=ScriptedAdapter([final("a")]), streaming=True)):  # model does not
            progress = Progress()
            assert (await rt.driver.drive(SCOPE, user("hi"), progress=progress)).state is DriveState.COMPLETED
            assert not adapter.sent[0].streamed and progress.told == []

    asyncio.run(scenario())


def test_each_answering_run_streams_but_a_summarizing_call_never_does() -> None:
    async def scenario() -> None:
        rt, adapter = streaming([calls(("echo", {}), commentary="Looking."), final("first answer"),
                                 final("second answer"), final("SUMMARY")],
                                tools=[RecordingTool("echo")], context_budget=ContextBudget(limit_tokens=100_000))
        progress = Progress()
        await rt.driver.drive(SCOPE, user("go"), progress=progress)
        assert [t[2].tool for t in progress.told if t[0] == "out" and t[2].tool] == ["echo"]
        assert progress.text() == "Looking.first answer"
        await rt.driver.drive(SCOPE, user("again"))
        compacted = await rt.driver.compact(SCOPE)  # "Compact now"
        (summary,) = compacted.compaction_run_ids
        assert (await rt.data.read_run(SCOPE, summary)).purpose is RunPurpose.COMPACTION
        assert [s.streamed for s in adapter.sent] == [True, True, True, False]

    asyncio.run(scenario())


async def _cut_short(end: Any, act: Any = None) -> tuple[Any, Progress, Any]:
    rt, _ = streaming([StreamCut(PIECES, end)])
    progress, stop = Progress(), StopSignal()
    job = asyncio.create_task(rt.driver.drive(SCOPE, user("go"), stop=stop, progress=progress))
    if act is not None:
        await until(lambda: progress.text() == "".join(PIECES))
        act(stop, job)
    try:
        result = await job
    except asyncio.CancelledError:
        result = None
    return rt, progress, result


async def _kept(rt: Any, progress: Any) -> tuple[Any, Any]:
    (start, run_id), (end, ended) = progress.told[0], progress.told[-1]
    assert (start, end, ended) == ("start", "end", run_id)  # ended however it ended
    run = await rt.data.read_run(SCOPE, run_id)
    raw = await rt.data.read_raw_response(SCOPE, run_id)
    assert run.status is RunStatus.OUTCOME_UNKNOWN and raw.body == "".join(PIECES).encode()
    assert raw.content_type == "text/event-stream"
    return run, raw


def test_a_cancel_keeps_what_arrived_of_the_stream() -> None:
    async def scenario() -> None:
        rt, progress, result = await _cut_short(asyncio.Event(), lambda stop, job: stop.cancel())
        assert result.state is DriveState.CANCELLED
        run, _ = await _kept(rt, progress)
        assert run.error_code == CANCELLED_BY_USER

    asyncio.run(scenario())


def test_a_stream_the_provider_calls_incomplete_keeps_what_arrived() -> None:
    async def scenario() -> None:
        rt, progress, result = await _cut_short(NativeUncertain("stream_incomplete"))
        assert result.state is DriveState.OUTCOME_UNKNOWN
        run, _ = await _kept(rt, progress)
        assert run.error_code == "stream_incomplete"

    asyncio.run(scenario())


def test_a_provider_exception_keeps_what_arrived() -> None:
    async def scenario() -> None:
        rt, progress, result = await _cut_short(RuntimeError("adapter bug"))
        assert result.state is DriveState.OUTCOME_UNKNOWN
        run, _ = await _kept(rt, progress)
        assert run.error_code == "RuntimeError"

    asyncio.run(scenario())


def test_shutdown_keeps_what_arrived_and_still_stops_the_job() -> None:
    async def scenario() -> None:
        rt, progress, result = await _cut_short(asyncio.Event(), lambda stop, job: job.cancel())
        assert result is None  # the job was cancelled from outside, as at shutdown
        run, _ = await _kept(rt, progress)
        assert run.error_code == SHUTDOWN

    asyncio.run(scenario())


# --- the Runner's live state (§5.2) -------------------------------------------------------


def test_the_runner_keeps_one_accumulated_state_that_a_reader_cannot_miss() -> None:
    async def scenario() -> None:
        hold = asyncio.Event()
        rt, _ = streaming([StreamCut(PIECES, hold), final("second job")])
        runner = BackgroundRunner(rt.driver)
        assert runner.live_output(SCOPE) is None and runner.live_version(SCOPE) == 0
        await runner.submit(SCOPE, user("go"))
        await until(lambda: (runner.live_output(SCOPE) or None) is not None
                    and runner.live_output(SCOPE).text == "".join(PIECES))  # type: ignore[union-attr]
        live = runner.live_output(SCOPE)
        assert live is not None and not live.ended and live.version == runner.live_version(SCOPE)
        # A reader that wakes late reads the state, not the pieces: from where it was.
        assert runner.live_text(SCOPE, live.run_id, len("Half ")) == "an answer"
        assert runner.live_text(SCOPE, RunId("run_other"), 0) is None
        waiting = asyncio.create_task(runner.wait_live(SCOPE, live.version, timeout=5))
        hold.set()  # the stream ends (uncertain) and its outcome is recorded
        assert await waiting > live.version
        await runner.wait(SCOPE)
        ended = runner.live_output(SCOPE)
        assert ended is not None and ended.ended and ended.text == "".join(PIECES)  # kept after the job
        assert await runner.wait_live(SCOPE, ended.version, timeout=0.01) == ended.version  # nothing new
        await runner.submit(SCOPE, user("again"))  # the next job starts with nothing of the last
        await runner.wait(SCOPE)
        second = runner.live_output(SCOPE)
        assert second is not None and second.run_id != live.run_id and second.text == "second job"

    asyncio.run(scenario())


def test_live_text_past_its_limit_is_marked_cut() -> None:
    async def scenario() -> None:
        rt, _ = streaming([StreamCut(("x" * (LIVE_TEXT_CHARS - 1), "yy", "z"), NativeUncertain("stream_incomplete"))])
        runner = BackgroundRunner(rt.driver)
        await runner.submit(SCOPE, user("go"))
        await runner.wait(SCOPE)
        live = runner.live_output(SCOPE)
        assert live is not None and live.truncated and live.text == "x" * (LIVE_TEXT_CHARS - 1)  # nothing after the cut

    asyncio.run(scenario())
