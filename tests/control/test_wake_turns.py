"""V3-P87 §4: a server-opened turn — recorded under the driver lock only in a
settled conversation, its job short (WAKE_RUNS), and the user's message
winning over it (cancel, wait WAKE_YIELD_SECONDS, else BUSY)."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest
from testkit import SCOPE, calls, final, runtime, user

from himari.common import ASK_USER_TOOL_ID, TrustedScope
from himari.control import runner as runner_module
from himari.control.api import (ControlError, ControlErrorCode, DriveResult, DriveState, StopSignal,
                                TurnProgress)
from himari.control.runner import WAKE_RUNS, BackgroundRunner
from himari.common import EventId
from himari.data.api import ConversationEvent, EventKind, UserMessage, Wake, WakeKind, WakeReason
from himari.prompt.api import UserTurn

TRY = (WakeReason(WakeKind.TRY),)


def test_a_wake_turn_speaks_after_a_wake_event() -> None:
    rt, provider = runtime([final("오늘 면접 잘 봐!")])
    runner = BackgroundRunner(rt.driver)

    async def scenario() -> None:
        started = await runner.submit_wake(SCOPE, TRY, "d1")
        assert started.running and started.wake_id is not None
        done = await runner.wait(SCOPE)
        assert done.result is not None and done.result.state is DriveState.COMPLETED
        assert done.wake_id == started.wake_id
        wake, answer = await rt.data.read_events(SCOPE)
        assert wake.kind is EventKind.WAKE and isinstance(wake.payload, Wake)
        assert wake.payload.wake_id == started.wake_id and answer.kind is EventKind.ASSISTANT_FINAL

    asyncio.run(scenario())
    (sent,) = provider.prepared_inputs
    (turn,) = sent.conversation
    assert isinstance(turn, UserTurn) and turn.wake and "<wake>" in turn.blocks[-1].text  # type: ignore[union-attr]


def test_a_wake_turn_stops_at_its_run_cap() -> None:
    step = calls(("echo", {"text": "x"}))
    rt, provider = runtime([step] * (WAKE_RUNS + 2), max_runs_per_drive=16)
    runner = BackgroundRunner(rt.driver)

    async def scenario() -> None:
        await runner.submit_wake(SCOPE, TRY, "d1")
        done = await runner.wait(SCOPE)
        assert done.result is not None and done.result.state is DriveState.RUN_LIMIT_REACHED
        # the next ordinary turn has the whole limit again
        await runner.submit(SCOPE, user("go on"))
        await runner.wait(SCOPE)

    asyncio.run(scenario())
    assert len(provider.prepared_inputs) > WAKE_RUNS


def test_a_wake_opens_only_in_a_settled_conversation() -> None:
    """§4.1: a waiting question is not a place to speak first; nothing is recorded."""
    rt, _ = runtime([calls((ASK_USER_TOOL_ID, {"question": "어디로?"}))])
    runner = BackgroundRunner(rt.driver)

    async def scenario() -> None:
        await runner.submit(SCOPE, user("여행 계획"))
        await runner.wait(SCOPE)
        before = await rt.data.read_events(SCOPE)
        with pytest.raises(ControlError) as refused:
            await runner.submit_wake(SCOPE, TRY, "d1")
        assert refused.value.code is ControlErrorCode.NOT_SETTLED
        assert await rt.data.read_events(SCOPE) == before
        assert await rt.data.opened_wakes() == ()

    asyncio.run(scenario())


def test_a_wake_is_refused_while_a_job_runs() -> None:
    driver = WakeDriver()
    runner = BackgroundRunner(driver)  # type: ignore[arg-type]

    async def scenario() -> None:
        await runner.submit(SCOPE, None)
        with pytest.raises(ControlError) as refused:
            await runner.submit_wake(SCOPE, TRY, "d1")
        assert refused.value.code is ControlErrorCode.BUSY and driver.wakes == []
        driver.release.set()
        await runner.wait(SCOPE)

    asyncio.run(scenario())


class WakeDriver:
    """Accepts wakes; each resume waits for ``release`` or a cancel, unless ``stubborn``."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.stubborn = False
        self.wakes: list[Sequence[WakeReason]] = []
        self.accepted: list[UserMessage] = []
        self.limits: list[int | None] = []
        self.accepting: asyncio.Event | None = None  # set: accept_wake waits for it (a slow write)
        self.withdrawn: list[str] = []

    async def accept_wake(self, scope: TrustedScope, reasons: Sequence[WakeReason], device_id: str, *,
                          keys: Sequence[str] = (), reminders: Sequence[int] = ()) -> ConversationEvent:
        self.wakes.append(reasons)
        if self.accepting is not None:
            await self.accepting.wait()
        from datetime import UTC, datetime
        return ConversationEvent(EventId("e1"), scope.conversation_id, len(self.wakes), EventKind.WAKE,
                                 Wake(f"wake_{len(self.wakes)}", tuple(reasons), device_id), 1, datetime.now(UTC))

    async def accept(self, scope: TrustedScope, message: UserMessage) -> None:
        self.accepted.append(message)

    async def resume(self, scope: TrustedScope, *, stop: StopSignal | None = None,
                     progress: TurnProgress | None = None, max_runs: int | None = None) -> DriveResult:
        self.limits.append(max_runs)
        while not self.release.is_set():
            if stop is not None and stop.cancel_requested and not self.stubborn:
                return DriveResult(DriveState.CANCELLED)
            await asyncio.sleep(0.005)
        return DriveResult(DriveState.COMPLETED)

    async def cancel(self, scope: TrustedScope) -> DriveResult:
        return DriveResult(DriveState.IDLE)

    async def withdraw_wake(self, scope: TrustedScope, wake_id: str) -> None:
        self.withdrawn.append(wake_id)


def test_the_users_message_wins_over_a_wake() -> None:
    driver = WakeDriver()
    runner = BackgroundRunner(driver)  # type: ignore[arg-type]

    async def scenario() -> None:
        wake = await runner.submit_wake(SCOPE, TRY, "d1")
        await asyncio.sleep(0.01)
        assert driver.limits == [WAKE_RUNS]
        status = await runner.submit(SCOPE, user("잠깐, 이거 먼저"))
        assert status.running and status.wake_id is None and status.generation == wake.generation + 1
        assert [m.blocks for m in driver.accepted] == [user("잠깐, 이거 먼저").blocks]
        driver.release.set()
        await runner.wait(SCOPE)

    asyncio.run(scenario())


def test_a_wake_that_will_not_stop_leaves_the_message_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    """§4.3: still running after the wait, the message is refused BUSY and not stored."""
    monkeypatch.setattr(runner_module, "WAKE_YIELD_SECONDS", 0.05)
    driver = WakeDriver()
    driver.stubborn = True
    runner = BackgroundRunner(driver)  # type: ignore[arg-type]

    async def scenario() -> None:
        await runner.submit_wake(SCOPE, TRY, "d1")
        with pytest.raises(ControlError) as refused:
            await runner.submit(SCOPE, user("hello"))
        assert refused.value.code is ControlErrorCode.BUSY and driver.accepted == []
        driver.release.set()
        await runner.wait(SCOPE)

    asyncio.run(scenario())


def test_an_ordinary_job_is_never_cancelled_by_a_message() -> None:
    driver = WakeDriver()
    runner = BackgroundRunner(driver)  # type: ignore[arg-type]

    async def scenario() -> None:
        await runner.submit(SCOPE, None)
        with pytest.raises(ControlError) as refused:
            await runner.submit(SCOPE, user("hello"))
        assert refused.value.code is ControlErrorCode.BUSY
        assert runner.status(SCOPE).running  # not asked to stop
        driver.release.set()
        await runner.wait(SCOPE)

    asyncio.run(scenario())


def test_a_wake_alone_is_refused_while_another_conversation_works() -> None:
    """PR #156 review P2: §6.1 holds for every conversation of the principal."""
    from himari.common import ConversationId

    other = TrustedScope(SCOPE.principal_id, ConversationId("conv_other"))
    driver = WakeDriver()
    runner = BackgroundRunner(driver)  # type: ignore[arg-type]

    async def scenario() -> None:
        await runner.submit(other, None)
        with pytest.raises(ControlError) as refused:
            await runner.submit_wake(SCOPE, TRY, "d1", alone=True)
        assert refused.value.code is ControlErrorCode.BUSY and driver.wakes == []
        await runner.submit_wake(SCOPE, TRY, "d1")  # without ``alone`` (the operator's try checks it itself)
        driver.release.set()
        await runner.wait(other)
        await runner.wait(SCOPE)

    asyncio.run(scenario())


def test_a_job_started_elsewhere_while_the_wake_is_recorded_withdraws_it() -> None:
    """PR #156 re-review P2-1, as it interleaves: the other conversation's job starts
    during accept_wake; the wake job never starts and the wake is withdrawn."""
    from himari.common import ConversationId

    other = TrustedScope(SCOPE.principal_id, ConversationId("conv_other"))
    driver = WakeDriver()
    runner = BackgroundRunner(driver)  # type: ignore[arg-type]

    async def scenario() -> None:
        gate = driver.accepting = asyncio.Event()
        waking = asyncio.create_task(runner.submit_wake(SCOPE, TRY, "d1", alone=True))
        await asyncio.sleep(0.01)  # inside accept_wake now
        assert driver.wakes and runner.running(SCOPE.principal_id) == ()
        await runner.submit(other, user("이거 해 줘"))  # the user starts work elsewhere meanwhile
        gate.set()
        with pytest.raises(ControlError) as refused:
            await waking
        assert refused.value.code is ControlErrorCode.BUSY
        assert driver.withdrawn == ["wake_1"]
        assert runner.running(SCOPE.principal_id) == (other.conversation_id,)  # only the user's job
        assert not runner.status(SCOPE).running and driver.limits == [None]  # no wake job ever ran
        driver.release.set()
        await runner.wait(other)

    asyncio.run(scenario())
