"""V3-P87 part 2 (§5, §7): the scheduler over a real Application — a due loop
and a return become wakes, once; the devices' last inputs survive a restart;
the Ticker keeps its jobs apart."""

from __future__ import annotations

import asyncio
import base64
import secrets
from datetime import UTC, datetime, timedelta
from collections.abc import Callable
from pathlib import Path
from typing import Any

from himari.bootstrap.application import Application
from himari.bootstrap.proactive import INPUT_EVERY, Inputs, away_minutes
from himari.bootstrap.schedule import Ticker
from himari.common import PrincipalId, TextBlock, TrustedScope
from himari.data.api import (EventKind, MemoryCategory, MessageOrigin, NoteDates, OriginKind, Wake, WakeKind,
                             WakeReason, parse_note_date)
from himari.data.sqlite import SqliteData, SqliteStore
from himari.presence.api import After, Returned, Session, Signals
from himari.proactive import ProactiveSettings
from himari.provider.api import NormalizedFinal
from himari.provider.scripted import ScriptedAdapter

ME = PrincipalId("operator")
ON = ProactiveSettings(enabled=True, quiet_start=None, quiet_end=None)


def answers(*texts: str) -> ScriptedAdapter:
    return ScriptedAdapter([NormalizedFinal((TextBlock(t),)) for t in texts])


async def opened(root: Path, adapter: ScriptedAdapter, settings: ProactiveSettings = ON) -> Application:
    return await Application.open(root, adapter, model_id="scripted-model", sandbox=False, cleanup_seconds=None,
                                  proactive=settings)


async def paired(app: Application) -> str:
    made = await app.devices.create(ME, "데스크톱")
    token = "hd_" + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    return (await app.devices.pair(made.code, token)).device.device_id


async def settled(app: Application, scope: TrustedScope) -> None:
    await app.runner.wait(scope)
    for _ in range(50):  # the wake is ended by a task after its job
        if not await app.data.opened_wakes():
            return
        await asyncio.sleep(0.01)


def test_a_due_loop_is_spoken_of_once(tmp_path: Path) -> None:
    async def scenario() -> None:
        adapter = answers("응", "면접 어땠어?")
        app = await opened(tmp_path / "data", adapter)
        try:
            device = await paired(app)
            app.hub.subscribe(ME, device)
            app.hub.signal(ME, device, 0, "Asia/Seoul")
            scope = TrustedScope(ME, await app.create_conversation(ME))
            await app.send(scope, "내일 면접이야", origin=MessageOrigin(OriginKind.WEB))
            await app.runner.wait(scope)
            due = parse_note_date((datetime.now(UTC) - timedelta(hours=2)).isoformat(timespec="minutes"), "UTC")
            assert due is not None
            note = await app.data.save_memory(ME, MemoryCategory.LOOP, "사용자는 면접이 있다",
                                              dates=NoteDates(due_at=due[0], due_instant=due[1]))
            scheduler = app.scheduler
            assert scheduler is not None
            look = await scheduler.look(ME)  # the user spoke a moment ago
            assert look.outcome == "talking" and look.decision.held
            scheduler._clock = lambda: datetime.now(UTC) + timedelta(minutes=15)
            look = await scheduler.look(ME)
            assert look.outcome == "spoke", look
            await settled(app, scope)
            wake, answer = (await app.data.read_events(scope))[-2:]
            assert isinstance(wake.payload, Wake) and answer.kind is EventKind.ASSISTANT_FINAL
            assert wake.payload.reasons == (WakeReason(WakeKind.LOOP_DUE, memory_id=note.memory_id),)
            assert wake.payload.device_id == device
            assert "사용자는 면접이 있다" in adapter.prepared_inputs[-1].conversation[-1].blocks[-1].text  # type: ignore[union-attr]
            again = await scheduler.look(ME)
            assert again.outcome == "nothing" and again.decision.reasons == ()  # its key is taken
            status = await app.proactive_status(ME)
            assert status["today"] == 1 and status["daily"] == 4
            assert status["last_look"] == {"at": again.at, "outcome": "nothing", "held": []}
        finally:
            await app.close()

    asyncio.run(scenario())


def test_a_return_after_hours_away_is_greeted(tmp_path: Path) -> None:
    async def scenario() -> None:
        root = tmp_path / "data"
        app = await opened(root, answers("응"))
        device = await paired(app)
        scope = TrustedScope(ME, await app.create_conversation(ME))
        await app.send(scope, "잘 자", origin=MessageOrigin(OriginKind.WEB))
        await app.runner.wait(scope)
        await app.data.record_input(ME, device, datetime.now(UTC) - timedelta(hours=5))
        await app.close()
        six_hours_ago = (datetime.now(UTC) - timedelta(hours=6)).isoformat(timespec="microseconds")
        store = SqliteStore(root / "himari.db")
        store.connection.execute("UPDATE events SET created_at=?", (six_hours_ago,))
        store.connection.commit()
        store.close()

        app = await opened(root, answers("좋은 아침!"))  # the server restarted meanwhile
        try:
            app.hub.subscribe(ME, device)
            app.hub.signal(ME, device, 0, "Asia/Seoul", Signals(after=After.UNLOCK))
            scheduler = app.scheduler
            assert scheduler is not None
            for _ in range(50):
                if ME in scheduler._returns:
                    break
                await asyncio.sleep(0.01)
            returned = scheduler._returns[ME]
            assert returned.via == "unlock" and returned.away_minutes is not None and 299 <= returned.away_minutes <= 300
            assert (await scheduler.look(ME)).outcome == "nothing"  # the user's 20 seconds
            scheduler._clock = lambda: datetime.now(UTC) + timedelta(seconds=25)
            assert (await scheduler.look(ME)).outcome == "spoke"
            await settled(app, scope)
            wake = (await app.data.read_events(scope))[-2]
            assert isinstance(wake.payload, Wake) and wake.payload.reasons[0].kind is WakeKind.RETURNED
            assert (await scheduler.look(ME)).decision.reasons == ()  # greeted once
        finally:
            await app.close()

    asyncio.run(scenario())


def test_nothing_runs_with_speaking_first_off(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = await opened(tmp_path / "data", answers(), ProactiveSettings())
        try:
            assert app.scheduler is None and app.inputs is not None  # inputs are kept anyway
            assert app.hub.on_returned is None
            assert (await app.proactive_status(ME))["enabled"] is False
        finally:
            await app.close()

    asyncio.run(scenario())


def test_inputs_are_written_now_and_then_and_at_a_lock(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = SqliteStore(tmp_path / "himari.db")
        data = SqliteData(store)
        device = (await data.add_device(ME, "데스크톱", b"p" * 32, datetime.now(UTC) + timedelta(hours=1))).device_id
        inputs = Inputs(data)
        start = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)
        inputs.seen(ME, device, start, False)
        await inputs.aclose()
        assert (await data.read_inputs(ME))[device] == start
        inputs.seen(ME, device, start + timedelta(minutes=5), False)  # within INPUT_EVERY: kept here only
        await inputs.aclose()
        assert (await data.read_inputs(ME))[device] == start
        assert inputs.latest_now(ME) == (start + timedelta(minutes=5), True)
        inputs.seen(ME, device, start + timedelta(minutes=6), True)  # the screen locked: at once
        await inputs.aclose()
        assert (await data.read_inputs(ME))[device] == start + timedelta(minutes=6)
        inputs.seen(ME, device, start + timedelta(minutes=6) + INPUT_EVERY, False)
        await inputs.aclose()
        assert (await data.read_inputs(ME))[device] == start + timedelta(minutes=6) + INPUT_EVERY
        await data.record_input(ME, device, start)  # never moved back
        assert (await data.read_inputs(ME))[device] == start + timedelta(minutes=6) + INPUT_EVERY
        store.close()

    asyncio.run(scenario())


def test_away_minutes() -> None:
    at = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)
    returned = Returned("d1", at, "unlock")
    assert away_minutes(returned, at - timedelta(minutes=290, seconds=30)) == 290
    assert away_minutes(returned, None) is None
    assert away_minutes(returned, at + timedelta(seconds=1)) == 0


def test_the_ticker_keeps_its_jobs_apart() -> None:
    async def scenario() -> None:
        ticker = Ticker()
        runs: list[str] = []

        async def failing() -> None:
            runs.append("bad")
            raise RuntimeError("boom")

        async def good() -> None:
            runs.append("good")

        ticker.add("bad", 0.01, failing)
        ticker.add("good", 3600, good)
        ticker.start()
        await asyncio.sleep(0.05)
        assert runs.count("bad") >= 2 and runs.count("good") == 1  # the failure goes on, the other is not stopped
        ticker.poke("good")
        await asyncio.sleep(0.01)
        assert runs.count("good") == 2
        ticker.poke("good", after=0.02)
        await asyncio.sleep(0.05)
        assert runs.count("good") == 3
        await ticker.close()

    asyncio.run(scenario())


def test_a_loop_cut_by_a_restart_before_a_word_is_spoken_of_after_it(tmp_path: Path) -> None:
    """PR #156 review P1, over the scheduler: the wake's keys come back and the next look speaks."""
    async def scenario() -> None:
        root = tmp_path / "data"
        app = await opened(root, answers("응"))
        device = await paired(app)
        scope = TrustedScope(ME, await app.create_conversation(ME))
        await app.send(scope, "내일 면접이야", origin=MessageOrigin(OriginKind.WEB))
        await app.runner.wait(scope)
        due = parse_note_date((datetime.now(UTC) - timedelta(hours=2)).isoformat(timespec="minutes"), "UTC")
        assert due is not None
        note = await app.data.save_memory(ME, MemoryCategory.LOOP, "사용자는 면접이 있다",
                                          dates=NoteDates(due_at=due[0], due_instant=due[1]))
        key = f"loop:m{note.memory_id}:{due[0]}"
        reasons = (WakeReason(WakeKind.LOOP_DUE, memory_id=note.memory_id),)
        cut = await app.data.begin_wake(scope, reasons, device, keys=(key,))  # recorded; the server stops at once
        await app.close()

        adapter = answers("면접 어땠어?")
        app = await opened(root, adapter)
        try:
            assert (await app.data.read_wake(ME, cut.payload.wake_id)).status.value == "failed"  # type: ignore[union-attr]
            app.hub.subscribe(ME, device)
            app.hub.signal(ME, device, 0, "Asia/Seoul")
            scheduler = app.scheduler
            assert scheduler is not None
            scheduler._clock = lambda: datetime.now(UTC) + timedelta(minutes=15)
            look = await scheduler.look(ME)
            assert look.outcome == "spoke", look
            await settled(app, scope)
            kinds = [e.kind.value for e in await app.data.read_events(scope)]
            assert kinds[-3:] == ["wake", "wake", "assistant_final"]
            assert await app.data.taken_keys(ME, (key,)) == frozenset({key})
            assert (await scheduler.look(ME)).decision.reasons == ()
        finally:
            await app.close()

    asyncio.run(scenario())


def test_what_changed_after_the_look_read_it_is_checked_again(tmp_path: Path) -> None:
    """PR #156 review P2: a job started elsewhere, the device changed, or the user spoke
    between the look's reads and the wake: no wake, and the next look sees it."""
    async def scenario() -> None:
        app = await opened(tmp_path / "data", answers("응"))
        try:
            device = await paired(app)
            app.hub.subscribe(ME, device)
            app.hub.signal(ME, device, 0, "Asia/Seoul")
            scope = TrustedScope(ME, await app.create_conversation(ME))
            await app.send(scope, "내일 면접이야", origin=MessageOrigin(OriginKind.WEB))
            await app.runner.wait(scope)
            due = parse_note_date((datetime.now(UTC) - timedelta(hours=2)).isoformat(timespec="minutes"), "UTC")
            assert due is not None
            await app.data.save_memory(ME, MemoryCategory.LOOP, "사용자는 면접이 있다",
                                       dates=NoteDates(due_at=due[0], due_instant=due[1]))
            scheduler = app.scheduler
            assert scheduler is not None
            scheduler._clock = lambda: datetime.now(UTC) + timedelta(minutes=15)
            read = scheduler.situation

            def after_read(change: Callable[[], None]) -> None:
                async def situation(principal: PrincipalId) -> Any:
                    found = await read(principal)
                    change()
                    return found

                scheduler.situation = situation  # type: ignore[method-assign]

            runner = app.runner
            running = runner.running
            after_read(lambda: setattr(runner, "running", lambda principal: (scope.conversation_id,)))
            assert (await scheduler.look(ME)).outcome == "busy"  # a job started in another conversation
            runner.running = running  # type: ignore[method-assign]
            after_read(lambda: app.hub.signal(ME, device, 0, "Asia/Seoul", Signals(session=Session.LOCKED)))
            assert (await scheduler.look(ME)).outcome == "no_device"  # the screen locked
            app.hub.signal(ME, device, 0, "Asia/Seoul", Signals(after=After.UNLOCK))
            after_read(lambda: scheduler.__setattr__("_clock", lambda: datetime.now(UTC)))  # the user just spoke
            assert (await scheduler.look(ME)).outcome == "talking"
            assert await app.data.opened_wakes() == () and await app.data.wakes_since(ME, datetime.min.replace(
                tzinfo=UTC)) == ()
        finally:
            await app.close()

    asyncio.run(scenario())
