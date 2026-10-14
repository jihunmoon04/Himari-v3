"""BackgroundRunner (CTRL-06)."""

from __future__ import annotations

import asyncio

import pytest
from testkit import SCOPE, calls, final, runtime, user

from himari.common import ASK_USER_TOOL_ID, ConversationId, PrincipalId, TrustedScope
from himari.control.api import ControlError, ControlErrorCode, DriveResult, DriveState
from himari.control.runner import BackgroundRunner
from himari.data.api import DataError, DataErrorCode, UserMessage

FOREIGN = TrustedScope(PrincipalId("mallory"), SCOPE.conversation_id)


class GatedDriver:
    """Each call waits for ``release`` and then returns or raises ``outcome``."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.outcome: DriveResult | BaseException = DriveResult(DriveState.COMPLETED)
        self.calls: list[tuple[TrustedScope, UserMessage | None]] = []

    async def _call(self, scope: TrustedScope, message: UserMessage | None) -> DriveResult:
        self.calls.append((scope, message))
        await self.release.wait()
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    async def drive(self, scope: TrustedScope, message: UserMessage) -> DriveResult:
        return await self._call(scope, message)

    async def resume(self, scope: TrustedScope) -> DriveResult:
        return await self._call(scope, None)


def test_submit_returns_at_once_and_one_job_runs_per_conversation() -> None:
    async def scenario() -> None:
        driver = GatedDriver()
        runner = BackgroundRunner(driver)
        started = await runner.submit(SCOPE, user("hi"))
        assert started.running and started.generation == 1 and started.started_at is not None
        await asyncio.sleep(0)
        assert driver.calls == [(SCOPE, user("hi"))]
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
