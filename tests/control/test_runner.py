"""BackgroundRunner (CTRL-06)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest
from testkit import SCOPE, calls, final, runtime, user

from himari.common import ASK_USER_TOOL_ID, ConversationId, PrincipalId, TrustedScope
from himari.control.api import (ControlError, ControlErrorCode, DriveResult, DriveState, JobStatus, StopRequest,
                                StopSignal, TurnProgress, TurnUsage)
from himari.control.runner import BackgroundRunner
from himari.data.api import DataError, DataErrorCode, MessageOrigin, OriginKind, UserMessage

FOREIGN = TrustedScope(PrincipalId("mallory"), SCOPE.conversation_id)


class GatedDriver:
    """Each call waits for ``release`` and then returns or raises ``outcome``."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.outcome: DriveResult | BaseException = DriveResult(DriveState.COMPLETED)
        self.calls: list[tuple[TrustedScope, UserMessage | None]] = []  # accepted inputs, then None per resume
        self.accept_error: BaseException | None = None
        self.compactions: list[TrustedScope] = []
        self.signals: list[StopSignal | None] = []  # the stop signal each job was given
        self.cancels: list[TrustedScope] = []

    async def _call(self, scope: TrustedScope, message: UserMessage | None) -> DriveResult:
        self.calls.append((scope, message))
        await self.release.wait()
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    async def drive(self, scope: TrustedScope, message: UserMessage, *, stop: StopSignal | None = None,
                     progress: TurnProgress | None = None) -> DriveResult:
        raise AssertionError("the runner accepts, then resumes")

    async def accept(self, scope: TrustedScope, message: UserMessage) -> None:
        if self.accept_error is not None:
            raise self.accept_error
        self.calls.append((scope, message))

    async def resume(self, scope: TrustedScope, *, stop: StopSignal | None = None,
                     progress: TurnProgress | None = None) -> DriveResult:
        self.signals.append(stop)
        return await self._call(scope, None)

    async def compact(self, scope: TrustedScope, *, stop: StopSignal | None = None,
                     progress: TurnProgress | None = None) -> DriveResult:
        self.compactions.append(scope)
        self.signals.append(stop)
        return await self._call(scope, None)

    async def cancel(self, scope: TrustedScope) -> DriveResult:
        self.cancels.append(scope)
        return DriveResult(DriveState.CANCELLED)


def test_submit_returns_at_once_and_one_job_runs_per_conversation() -> None:
    async def scenario() -> None:
        driver = GatedDriver()
        runner = BackgroundRunner(driver)
        started = await runner.submit(SCOPE, user("hi"))
        assert started.running and started.generation == 1 and started.started_at is not None
        assert driver.calls == [(SCOPE, user("hi"))]  # committed before submit returned
        await asyncio.sleep(0)
        assert driver.calls == [(SCOPE, user("hi")), (SCOPE, None)]
        with pytest.raises(ControlError) as busy:
            await runner.submit(SCOPE, user("again"))
        assert busy.value.code is ControlErrorCode.BUSY
        with pytest.raises(ControlError):
            await runner.submit(FOREIGN, user("same conversation, other principal"))
        other = TrustedScope(SCOPE.principal_id, ConversationId("conv_2"))
        assert (await runner.submit(other, None)).running  # other Conversations are independent

        driver.release.set()
        done = await runner.wait(SCOPE)
        assert not done.running and done.result == DriveResult(DriveState.COMPLETED) and done.error is None
        assert done.finished_at is not None and done.generation == 1
        assert (await runner.submit(SCOPE, None)).generation == 2  # resume once idle
        await runner.wait(SCOPE)
        assert driver.calls[-1] == (SCOPE, None)

    asyncio.run(scenario())


def test_running_lists_the_principals_jobs_under_way() -> None:
    """V3-P80 §3.2: the Conversations with a job under way now, of any kind, the principal's only."""
    async def scenario() -> None:
        driver = GatedDriver()
        runner = BackgroundRunner(driver)
        alice = SCOPE.principal_id
        assert runner.running(alice) == ()
        other = TrustedScope(alice, ConversationId("conv_2"))
        await runner.submit(SCOPE, user("hi"))
        await runner.submit_compaction(other)  # a compaction is a job too
        await asyncio.sleep(0)
        assert sorted(runner.running(alice)) == ["conv_1", "conv_2"]
        assert runner.running(FOREIGN.principal_id) == ()
        driver.release.set()
        await runner.wait(SCOPE)
        await runner.wait(other)
        assert runner.running(alice) == ()

    asyncio.run(scenario())


def test_another_principal_sees_nothing() -> None:
    async def scenario() -> None:
        driver = GatedDriver()
        driver.release.set()
        runner = BackgroundRunner(driver)
        await runner.submit(SCOPE, user("private"))
        await runner.wait(SCOPE)
        assert runner.status(SCOPE).result is not None
        assert runner.status(FOREIGN) == runner.status(TrustedScope(FOREIGN.principal_id, ConversationId("x")))
        assert runner.status(FOREIGN).generation == 0 and runner.status(FOREIGN).result is None

    asyncio.run(scenario())


@pytest.mark.parametrize("raised, error", [
    (DataError(DataErrorCode.NOT_AUTHORIZED, "conv_1"), "not_authorized"),
    (ControlError(ControlErrorCode.BUSY), "busy"),
    (RuntimeError("/srv/secret/path exploded"), "internal_error"),
])
def test_failed_jobs_report_only_a_classification(raised: Exception, error: str) -> None:
    async def scenario() -> None:
        driver = GatedDriver()
        driver.outcome = raised
        driver.release.set()
        runner = BackgroundRunner(driver)
        await runner.submit(SCOPE, user("x"))
        status = await runner.wait(SCOPE)
        assert not status.running and status.error == error and status.result is None
        assert "secret" not in repr(status)

    asyncio.run(scenario())


def test_cancelling_a_waiter_does_not_cancel_the_job() -> None:
    async def scenario() -> None:
        driver = GatedDriver()
        runner = BackgroundRunner(driver)
        await runner.submit(SCOPE, user("x"))
        waiter = asyncio.create_task(runner.wait(SCOPE))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert runner.status(SCOPE).running
        driver.release.set()
        assert (await runner.wait(SCOPE)).result is not None

    asyncio.run(scenario())


def test_shutdown_lets_quick_jobs_finish_and_cancels_the_rest() -> None:
    async def scenario() -> None:
        quick, slow = GatedDriver(), GatedDriver()
        quick_runner, slow_runner = BackgroundRunner(quick), BackgroundRunner(slow)
        await quick_runner.submit(SCOPE, user("q"))
        await slow_runner.submit(SCOPE, user("s"))
        asyncio.get_running_loop().call_later(0.05, quick.release.set)
        await quick_runner.shutdown(grace_seconds=2)
        assert quick_runner.status(SCOPE).result is not None
        await slow_runner.shutdown(grace_seconds=0.05)
        assert slow_runner.status(SCOPE).error == "cancelled" and not slow_runner.status(SCOPE).running
        await slow_runner.shutdown(grace_seconds=0)  # nothing left: returns at once

    asyncio.run(scenario())


def test_real_driver_question_and_answer_through_the_runner() -> None:
    rt, provider = runtime([calls((ASK_USER_TOOL_ID, {"question": "Which city?"})), final("Seoul it is")])
    runner = BackgroundRunner(rt.driver)

    async def scenario() -> None:
        await runner.submit(SCOPE, user("plan a trip"))
        asked = await runner.wait(SCOPE)
        assert asked.result is not None and asked.result.state is DriveState.WAITING_FOR_USER
        await runner.submit(SCOPE, user("Seoul"))
        answered = await runner.wait(SCOPE)
        assert answered.result is not None and answered.result.state is DriveState.COMPLETED
        assert answered.generation == 2
        events = await rt.data.read_events(SCOPE)
        assert events[-1].payload.blocks[0].text == "Seoul it is"  # type: ignore[union-attr]

    asyncio.run(scenario())
    assert len(provider.sent) == 2


# --- PR #12 review B1: an accepted message is committed before submit returns -----------


def test_submit_commits_the_input_before_returning() -> None:
    rt, provider = runtime([calls((ASK_USER_TOOL_ID, {"question": "Which city?"})), final("Seoul it is")])
    runner = BackgroundRunner(rt.driver)

    async def scenario() -> None:
        await runner.submit(SCOPE, user("plan a trip"))
        events = await rt.data.read_events(SCOPE)  # no await has let the job run yet
        assert [e.payload for e in events] == [user("plan a trip")] and provider.sent == []
        await runner.wait(SCOPE)
        await runner.submit(SCOPE, user("Seoul"))
        answered = (await rt.data.read_events(SCOPE))[-2:]
        assert answered[0].payload.answers_call is not None  # type: ignore[union-attr]  # T7a, committed already
        assert (await runner.wait(SCOPE)).result.state is DriveState.COMPLETED  # type: ignore[union-attr]

    asyncio.run(scenario())


def test_a_refused_input_starts_no_job_and_records_nothing() -> None:
    rt, _ = runtime([final("x")])
    runner = BackgroundRunner(rt.driver)

    async def scenario() -> None:
        with pytest.raises(DataError) as exc:
            await runner.submit(FOREIGN, user("not mine"))
        assert exc.value.code is DataErrorCode.NOT_AUTHORIZED
        assert runner.status(FOREIGN).generation == 0 and await rt.data.read_events(SCOPE) == ()

    asyncio.run(scenario())


def test_a_compaction_runs_in_the_background_and_is_busy_like_any_job() -> None:
    async def scenario() -> None:
        driver = GatedDriver()
        runner = BackgroundRunner(driver)
        started = await runner.submit_compaction(SCOPE)
        assert started.running and started.generation == 1
        with pytest.raises(ControlError) as busy:
            await runner.submit(SCOPE, None)
        assert busy.value.code is ControlErrorCode.BUSY
        with pytest.raises(ControlError):
            await runner.submit_compaction(SCOPE)
        driver.outcome = DriveResult(DriveState.COMPACTED)
        driver.release.set()
        done = await runner.wait(SCOPE)
        assert done.result == DriveResult(DriveState.COMPACTED) and driver.compactions == [SCOPE]

    asyncio.run(scenario())


def test_pause_and_cancel_reach_the_running_job_through_its_signal() -> None:
    async def scenario() -> None:
        driver = GatedDriver()
        runner = BackgroundRunner(driver)
        assert runner.request_pause(SCOPE) == JobStatus(running=False)  # nothing running: nothing to do
        await runner.submit(SCOPE, None)
        await asyncio.sleep(0)
        (signal,) = driver.signals
        assert signal is not None and signal.request is None

        paused = runner.request_pause(SCOPE)
        assert signal.pause_requested and not signal.cancel_requested
        assert paused.running and paused.stop_requested is StopRequest.PAUSE
        cancelling = runner.cancel(SCOPE)  # a cancel overrides the pause
        assert signal.cancel_requested and cancelling.stop_requested is StopRequest.CANCEL
        assert driver.cancels == []  # the running job stops itself; no separate cancel job

        driver.outcome = DriveResult(DriveState.CANCELLED)
        driver.release.set()
        done = await runner.wait(SCOPE)
        assert done.result == DriveResult(DriveState.CANCELLED) and done.stop_requested is StopRequest.CANCEL

        driver.release.clear()
        await runner.submit(SCOPE, None)
        await asyncio.sleep(0)
        assert driver.signals[-1] is not signal and not driver.signals[-1].pause_requested  # a fresh signal per job
        driver.release.set()
        await runner.wait(SCOPE)

    asyncio.run(scenario())


def test_cancel_with_no_job_running_closes_what_was_left_waiting() -> None:
    async def scenario() -> None:
        driver = GatedDriver()
        runner = BackgroundRunner(driver)
        assert runner.cancel(SCOPE) == JobStatus(running=False)  # a stop request never closes anything
        assert driver.cancels == []
        started = await runner.submit_cancel(SCOPE)
        assert started.running and started.generation == 1
        done = await runner.wait(SCOPE)
        assert driver.cancels == [SCOPE] and done.result == DriveResult(DriveState.CANCELLED)
        assert done.stop_requested is None
        await runner.submit(SCOPE, None)
        with pytest.raises(ControlError) as busy:
            await runner.submit_cancel(SCOPE)
        assert busy.value.code is ControlErrorCode.BUSY
        driver.release.set()
        await runner.wait(SCOPE)

    asyncio.run(scenario())


# --- closing a Conversation for the trash (V3-P65 §7.1) ------------------------------------


class GatedAccept(GatedDriver):
    """``accept`` waits for ``accepting`` too: an input still being accepted."""

    def __init__(self) -> None:
        super().__init__()
        self.accepting = asyncio.Event()
        self.in_accept = asyncio.Event()

    async def accept(self, scope: TrustedScope, message: UserMessage) -> None:
        self.in_accept.set()
        await self.accepting.wait()
        await super().accept(scope, message)


def closed(error: pytest.ExceptionInfo[ControlError]) -> bool:
    return error.value.code is ControlErrorCode.CLOSED


def test_b2_close_waits_for_an_input_being_accepted_and_no_job_starts_after() -> None:
    async def scenario() -> None:
        driver = GatedAccept()
        runner = BackgroundRunner(driver)
        submitting = asyncio.create_task(runner.submit(SCOPE, user("sent just before")))
        await driver.in_accept.wait()
        closing = asyncio.create_task(runner.close(SCOPE, timeout=5))
        await asyncio.sleep(0.02)
        assert not closing.done()  # waits for the input inside accept
        driver.accepting.set()
        with pytest.raises(ControlError) as refused:
            await submitting
        assert closed(refused)
        await closing
        assert driver.calls == [(SCOPE, user("sent just before"))]  # recorded, and no job was started
        assert not runner.status(SCOPE).running

    asyncio.run(scenario())


def test_close_cancels_the_running_job_and_waits_for_it() -> None:
    async def scenario() -> None:
        driver = GatedDriver()
        runner = BackgroundRunner(driver)
        await runner.submit(SCOPE, None)
        await asyncio.sleep(0)
        closing = asyncio.create_task(runner.close(SCOPE, timeout=5))
        await asyncio.sleep(0.02)
        (signal,) = driver.signals
        assert signal is not None and signal.request is StopRequest.CANCEL and not closing.done()
        driver.release.set()  # the job ends at the cancel
        await closing
        assert not runner.status(SCOPE).running

    asyncio.run(scenario())


def test_a_closed_conversation_takes_no_work_until_reopened() -> None:
    async def scenario() -> None:
        driver = GatedDriver()
        driver.release.set()
        runner = BackgroundRunner(driver)
        await runner.close(SCOPE, timeout=5)
        submits: list[Callable[[], Awaitable[JobStatus]]] = [
            lambda: runner.submit(SCOPE, user("x")), lambda: runner.submit(SCOPE, None),
            lambda: runner.submit_compaction(SCOPE), lambda: runner.submit_cancel(SCOPE)]
        for submit in submits:
            with pytest.raises(ControlError) as refused:
                await submit()
            assert closed(refused)
        assert driver.calls == [] and driver.cancels == []  # nothing accepted, nothing run
        other = TrustedScope(SCOPE.principal_id, ConversationId("conv_2"))
        assert (await runner.submit(other, None)).running  # other Conversations are not affected
        runner.reopen(SCOPE)
        assert (await runner.submit(SCOPE, None)).running

    asyncio.run(scenario())


def test_close_gives_up_at_its_timeout_and_stays_closed() -> None:
    async def scenario() -> None:
        driver = GatedDriver()  # never released: the job ignores the cancel
        runner = BackgroundRunner(driver)
        await runner.submit(SCOPE, None)
        await asyncio.sleep(0)
        with pytest.raises(TimeoutError):
            await runner.close(SCOPE, timeout=0.05)
        assert runner.status(SCOPE).running  # the job itself was not cancelled by the timeout
        with pytest.raises(ControlError) as refused:
            await runner.submit(SCOPE, None)
        assert closed(refused)
        runner.reopen(SCOPE)
        driver.release.set()
        await runner.wait(SCOPE)

    asyncio.run(scenario())


def test_forget_drops_the_last_job_but_not_a_running_one() -> None:
    async def scenario() -> None:
        driver = GatedDriver()
        runner = BackgroundRunner(driver)
        await runner.submit(SCOPE, None)
        await asyncio.sleep(0)
        with pytest.raises(ControlError) as busy:
            runner.forget(SCOPE)
        assert busy.value.code is ControlErrorCode.BUSY
        driver.release.set()
        assert (await runner.wait(SCOPE)).generation == 1
        runner.forget(SCOPE)
        assert runner.status(SCOPE) == JobStatus(running=False)

    asyncio.run(scenario())


# --- holding a Conversation while its file changes are reviewed (V3-P76 §6) ----------------


def busy(error: pytest.ExceptionInfo[ControlError]) -> bool:
    return error.value.code is ControlErrorCode.BUSY


def test_a_held_conversation_starts_no_work_and_nothing_is_cancelled() -> None:
    async def scenario() -> None:
        driver = GatedDriver()
        driver.release.set()
        runner = BackgroundRunner(driver)
        with runner.hold(SCOPE):
            submits: list[Callable[[], Awaitable[JobStatus]]] = [
                lambda: runner.submit(SCOPE, user("x")), lambda: runner.submit(SCOPE, None),
                lambda: runner.submit_compaction(SCOPE), lambda: runner.submit_cancel(SCOPE)]
            for submit in submits:
                with pytest.raises(ControlError) as refused:
                    await submit()
                assert busy(refused)
            with pytest.raises(ControlError) as again:
                with runner.hold(SCOPE):
                    pass
            assert busy(again)
            other = TrustedScope(SCOPE.principal_id, ConversationId("conv_2"))
            assert (await runner.submit(other, None)).running  # other Conversations are not held
            await asyncio.sleep(0)
        assert driver.calls[0] == (other, None) and driver.cancels == []  # nothing accepted here
        assert (await runner.submit(SCOPE, user("after"))).running

    asyncio.run(scenario())


def test_hold_is_refused_while_a_job_runs_or_an_input_is_being_accepted() -> None:
    async def scenario() -> None:
        driver = GatedAccept()
        runner = BackgroundRunner(driver)
        submitting = asyncio.create_task(runner.submit(SCOPE, user("sent")))
        await driver.in_accept.wait()
        with pytest.raises(ControlError) as accepting:
            with runner.hold(SCOPE):
                pass
        assert busy(accepting)
        driver.accepting.set()
        await submitting
        with pytest.raises(ControlError) as running:
            with runner.hold(SCOPE):
                pass
        (signal,) = driver.signals
        assert busy(running) and signal is not None and signal.request is None  # not stopped
        driver.release.set()
        await runner.wait(SCOPE)
        with runner.hold(SCOPE):
            pass
        await runner.close(SCOPE, timeout=5)
        with pytest.raises(ControlError) as shut:
            with runner.hold(SCOPE):
                pass
        assert closed(shut)

    asyncio.run(scenario())


class Listening:
    """A JobListener that records what it is told (V3-P83 §2.2)."""

    def __init__(self, fail: bool = False) -> None:
        self.told: list[tuple[str, object]] = []
        self.fail = fail

    def _note(self, what: str, detail: object = None) -> None:
        self.told.append((what, detail))
        if self.fail:
            raise RuntimeError("a listener that fails")

    def job_started(self, scope: TrustedScope, origin: MessageOrigin | None) -> None:
        self._note("started", origin)

    def accepted(self, scope: TrustedScope) -> None:
        self._note("accepted")

    def run_ended(self, scope: TrustedScope) -> None:
        self._note("run")

    def job_ended(self, scope: TrustedScope, status: JobStatus) -> None:
        self._note("ended", None if status.result is None else status.result.state)


class Reporting(GatedDriver):
    """Two Runs reported, then the outcome."""

    async def resume(self, scope: TrustedScope, *, stop: StopSignal | None = None,
                     progress: TurnProgress | None = None) -> DriveResult:
        assert progress is not None
        progress.report(TurnUsage(runs=1))
        progress.report(TurnUsage(runs=1, input_tokens=5))  # the same Run's later report: no new Run
        progress.report(TurnUsage(runs=2))
        return await super().resume(scope, stop=stop, progress=progress)


@pytest.mark.parametrize("fail", [False, True])
def test_a_listener_is_told_as_the_job_goes(fail: bool) -> None:
    """A failing listener is logged; the job goes on as if none were there."""
    async def scenario() -> None:
        driver, listener = Reporting(), Listening(fail)
        driver.release.set()
        runner = BackgroundRunner(driver, listener=listener)
        origin = MessageOrigin(OriginKind.DEVICE, "d1")
        await runner.submit(SCOPE, UserMessage(user("hi").blocks, origin=origin))
        status = await runner.wait(SCOPE)
        assert status.result is not None and status.result.state is DriveState.COMPLETED
        await runner.submit_compaction(SCOPE)
        await runner.wait(SCOPE)
        assert listener.told == [("accepted", None), ("started", origin), ("run", None), ("run", None),
                                 ("ended", DriveState.COMPLETED),
                                 ("started", None), ("ended", DriveState.COMPLETED)]  # a compaction's: no origin

    asyncio.run(scenario())


def test_an_input_accepted_while_closing_is_told_but_no_job_starts() -> None:
    async def scenario() -> None:
        driver, listener = GatedAccept(), Listening()
        runner = BackgroundRunner(driver, listener=listener)
        submitting = asyncio.create_task(runner.submit(SCOPE, user("sent just before")))
        await driver.in_accept.wait()
        closing = asyncio.create_task(runner.close(SCOPE, timeout=5))
        await asyncio.sleep(0.02)
        driver.accepting.set()
        with pytest.raises(ControlError):
            await submitting
        await closing
        assert listener.told == [("accepted", None)]  # re-review C2: "started" only from _start

    asyncio.run(scenario())
