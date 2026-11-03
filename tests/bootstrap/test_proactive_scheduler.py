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
from himari.presence.api import After, Returned, Session, Signals, Sleep
from himari.proactive import ProactiveSettings
from himari.provider.api import NormalizedFinal
from himari.provider.scripted import ScriptedAdapter

ME = PrincipalId("operator")
ON = ProactiveSettings(enabled=True, quiet_start=None, quiet_end=None)


def answers(*texts: str) -> ScriptedAdapter:
    return ScriptedAdapter([NormalizedFinal((TextBlock(t),)) for t in texts], companion=True)


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
            assert status["today"] == 0 and status["daily"] == 6  # V3-P90 §7.4: free wakes only
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


def test_a_failed_wake_counts_only_when_its_turn_left_output(tmp_path: Path) -> None:
    """PR #156 re-review P2-2: tool calls then a failure count toward the gap; a silent failure does not."""
    from himari.common import CallId, ToolCall, ToolId
    from himari.data.api import WakeStatus
    from himari.provider.api import NormalizedToolCalls

    async def scenario() -> None:
        calls = NormalizedToolCalls((ToolCall(CallId("c0"), 0, ToolId("nowhere/tool"), {}),), (TextBlock("찾아볼게"),))
        app = await opened(tmp_path / "data", ScriptedAdapter([NormalizedFinal((TextBlock("응"),)), calls], companion=True))
        try:
            device = await paired(app)
            app.hub.subscribe(ME, device)
            app.hub.signal(ME, device, 0, "Asia/Seoul")
            scope = TrustedScope(ME, await app.create_conversation(ME))
            await app.send(scope, "안녕", origin=MessageOrigin(OriginKind.WEB))
            await app.runner.wait(scope)
            _, status = await app.try_wake(ME)
            await settled(app, scope)
            loud = await app.data.read_wake(ME, status.wake_id or "")
            assert loud.status is WakeStatus.FAILED  # its tool calls, then the script ran out
            scheduler = app.scheduler
            assert scheduler is not None
            situation, _ = await scheduler.situation(ME)
            assert loud.wake_id in situation.said
            silent = await app.data.begin_wake(scope, (WakeReason(WakeKind.LOOP_DUE, memory_id=1),), device)
            await app.data.end_wake(ME, silent.payload.wake_id, WakeStatus.FAILED)  # type: ignore[union-attr]
            situation, _ = await scheduler.situation(ME)
            assert silent.payload.wake_id not in situation.said and loud.wake_id in situation.said  # type: ignore[union-attr]
        finally:
            await app.close()

    asyncio.run(scenario())


def test_a_reminder_is_told_while_the_user_talks_and_is_delivered(tmp_path: Path) -> None:
    """§5.3: right after the user's own turn, past the talking rule; delivered once answered."""
    async def scenario() -> None:
        adapter = answers("응", "참, 약 먹을 시간이야!")
        app = await opened(tmp_path / "data", adapter)
        try:
            device = await paired(app)
            app.hub.subscribe(ME, device)
            app.hub.signal(ME, device, 0, "Asia/Seoul")
            scope = TrustedScope(ME, await app.create_conversation(ME))
            await app.send(scope, "오늘 너무 피곤하다", origin=MessageOrigin(OriginKind.WEB))
            await app.runner.wait(scope)
            at = parse_note_date((datetime.now(UTC) - timedelta(minutes=1)).isoformat(timespec="minutes"), "UTC")
            assert at is not None
            note = await app.data.save_memory(ME, MemoryCategory.LOOP, "사용자는 저녁 약을 먹어야 한다",
                                              dates=NoteDates(remind_at=at[0], remind_instant=at[1]))
            scheduler = app.scheduler
            assert scheduler is not None
            look = await scheduler.look(ME)
            assert look.outcome == "spoke", look
            await settled(app, scope)
            wake = (await app.data.read_events(scope))[-2]
            assert isinstance(wake.payload, Wake)
            assert wake.payload.reasons == (WakeReason(WakeKind.REMINDER, memory_id=note.memory_id),)
            drawn = adapter.prepared_inputs[-1].conversation[-1].blocks[-1].text  # type: ignore[union-attr]
            assert '"kind": "reminder"' in drawn and at[0] in drawn  # its time, read when drawn
            told = await app.data.read_memory(ME, note.memory_id)
            assert told.remind_state == "delivered" and told.reminded_at is not None
            assert (await scheduler.look(ME)).decision.reasons == ()
            status = await app.proactive_status(ME)
            assert status["today"] == 0  # a reminder is not counted toward the day
        finally:
            await app.close()

    asyncio.run(scenario())


def asleep_signals(sleep: Sleep | None = Sleep.IDLE) -> Signals:
    return Signals(session=Session.ACTIVE, sleep_known=True, sleep=sleep)


async def told_while_asleep(app: Application, adapter: ScriptedAdapter) -> tuple[str, TrustedScope, int]:
    """V3-P90 §5.1: the character sleeps (idle); a reminder comes due; it is told and marked told."""
    device = await paired(app)
    app.hub.subscribe(ME, device)
    app.hub.signal(ME, device, 0, "Asia/Seoul", asleep_signals(None))
    scope = TrustedScope(ME, await app.create_conversation(ME))
    await app.send(scope, "나 잠깐 나갔다 올게", origin=MessageOrigin(OriginKind.WEB))
    await app.runner.wait(scope)
    app.hub.signal(ME, device, 700, "Asia/Seoul", asleep_signals())
    at = parse_note_date((datetime.now(UTC) - timedelta(minutes=1)).isoformat(timespec="minutes"), "UTC")
    assert at is not None
    note = await app.data.save_memory(ME, MemoryCategory.LOOP, "사용자는 저녁 약을 먹어야 한다",
                                      dates=NoteDates(remind_at=at[0], remind_instant=at[1]))
    scheduler = app.scheduler
    assert scheduler is not None
    look = await scheduler.look(ME)
    assert look.outcome == "spoke", look
    await settled(app, scope)
    wake = (await app.data.read_events(scope))[-2]
    assert isinstance(wake.payload, Wake)
    assert wake.payload.reasons == (WakeReason(WakeKind.REMINDER, memory_id=note.memory_id, asleep=True),)
    assert (await app.data.read_memory(ME, note.memory_id)).remind_state == "told"
    return device, scope, note.memory_id


def test_a_reminder_told_while_asleep_is_seen_at_a_return_soon_after(tmp_path: Path) -> None:
    async def scenario() -> None:
        adapter = answers("다녀와!", "약 먹을 시간이야!")
        app = await opened(tmp_path / "data", adapter)
        try:
            device, _, memory_id = await told_while_asleep(app, adapter)
            app.hub.signal(ME, device, 0, "Asia/Seoul", asleep_signals(None))  # the user comes to the bubble
            scheduler = app.scheduler
            assert scheduler is not None
            for _ in range(50):
                if ME in scheduler._returns:
                    break
                await asyncio.sleep(0.01)
            look = await scheduler.look(ME)
            assert look.decision.reasons == () and look.decision.seen == (memory_id,)
            assert (await app.data.read_memory(ME, memory_id)).remind_state == "delivered"
        finally:
            await app.close()

    asyncio.run(scenario())


def test_a_reminder_told_while_asleep_is_seen_by_a_message_in_its_conversation(tmp_path: Path) -> None:
    async def scenario() -> None:
        adapter = answers("다녀와!", "약 먹을 시간이야!", "응 잘했어")
        app = await opened(tmp_path / "data", adapter)
        try:
            _, scope, memory_id = await told_while_asleep(app, adapter)
            other = TrustedScope(ME, await app.create_conversation(ME))
            await app.send(other, "다른 얘기", origin=MessageOrigin(OriginKind.WEB))
            await app.runner.wait(other)
            scheduler = app.scheduler
            assert scheduler is not None
            await scheduler.look(ME)
            assert (await app.data.read_memory(ME, memory_id)).remind_state == "told"  # elsewhere: not seen
            await app.send(scope, "먹었어", origin=MessageOrigin(OriginKind.WEB))
            await app.runner.wait(scope)
            await scheduler.look(ME)
            assert (await app.data.read_memory(ME, memory_id)).remind_state == "delivered"
        finally:
            await app.close()

    asyncio.run(scenario())


def working_signals(minutes: float) -> Signals:
    from himari.presence.api import TaskSignal

    return Signals(session=Session.ACTIVE, sleep_known=True, task=TaskSignal("work", minutes * 60))


def test_a_free_wake_that_passes_leaves_nothing_but_a_count(tmp_path: Path) -> None:
    """V3-P90 §7: fifteen minutes into a task the model is asked; a pass is never seen, and counts."""
    async def scenario() -> None:
        adapter = answers("응", "[[pass]]")
        app = await opened(tmp_path / "data", adapter)
        try:
            device = await paired(app)
            app.hub.subscribe(ME, device)
            scope = TrustedScope(ME, await app.create_conversation(ME))
            await app.send(scope, "일 시작한다", origin=MessageOrigin(OriginKind.WEB))
            await app.runner.wait(scope)
            app.hub.signal(ME, device, 0, "Asia/Seoul", working_signals(16))
            scheduler = app.scheduler
            assert scheduler is not None
            # 27 minutes into the task, the message 16 minutes ago (past talking): step 1 is due
            scheduler._clock = lambda: datetime.now(UTC) + timedelta(minutes=16)
            app.hub.signal(ME, device, 0, "Asia/Seoul", working_signals(27))
            look = await scheduler.look(ME)
            assert look.outcome == "spoke" and look.decision.reasons[0].kind is WakeKind.FREE, look
            await settled(app, scope)
            kinds = [e.kind for e in await app.data.read_events(scope)]
            assert EventKind.WAKE not in kinds  # passed: it never was
            status = await app.proactive_status(ME)
            assert status["today"] == 0 and status["attempts"] == 1
        finally:
            await app.close()

    asyncio.run(scenario())


def test_a_task_longer_than_the_wakes_read_keeps_its_steps(tmp_path: Path) -> None:
    """PR #170 review P2-1: a task 27 hours on, its two free wakes older than WAKES_READ — the step is
    still the third (counted back to the task's start), not the first again under a taken key."""
    from himari.data.api import WakeStatus
    from himari.proactive.rules import _minute

    async def scenario() -> None:
        adapter = answers("응", "[[pass]]")
        app = await opened(tmp_path / "data", adapter)
        try:
            device = await paired(app)
            app.hub.subscribe(ME, device)
            scope = TrustedScope(ME, await app.create_conversation(ME))
            await app.send(scope, "일 시작한다", origin=MessageOrigin(OriginKind.WEB))
            await app.runner.wait(scope)
            later = datetime.now(UTC) + timedelta(hours=27)
            cycle = later - timedelta(hours=27, minutes=20)  # the task started 20 minutes before those wakes
            for step in (1, 2):
                event = await app.data.begin_wake(scope, (WakeReason(WakeKind.FREE, group="work", minutes=step,
                                                                     step=step),), device,
                                                  keys=(f"free:{device}:{_minute(cycle)}:{step}",))
                assert isinstance(event.payload, Wake)
                await app.data.end_wake(ME, event.payload.wake_id, WakeStatus.DROPPED)  # passed, unseen
            scheduler = app.scheduler
            assert scheduler is not None
            scheduler._clock = lambda: later
            app.hub.signal(ME, device, 0, "Asia/Seoul", working_signals(27 * 60 + 20))
            look = await scheduler.look(ME)
            assert look.outcome == "spoke" and look.decision.reasons[0].step == 3, look
            await settled(app, scope)
        finally:
            await app.close()

    asyncio.run(scenario())


def test_a_free_wake_that_speaks_is_shown_and_answered(tmp_path: Path) -> None:
    async def scenario() -> None:
        adapter = answers("응", "한참 했네, 물 한 잔 어때?")
        app = await opened(tmp_path / "data", adapter)
        try:
            device = await paired(app)
            app.hub.subscribe(ME, device)
            scope = TrustedScope(ME, await app.create_conversation(ME))
            await app.send(scope, "일 시작한다", origin=MessageOrigin(OriginKind.WEB))
            await app.runner.wait(scope)
            scheduler = app.scheduler
            assert scheduler is not None
            scheduler._clock = lambda: datetime.now(UTC) + timedelta(minutes=16)
            app.hub.signal(ME, device, 0, "Asia/Seoul", working_signals(27))
            assert (await scheduler.look(ME)).outcome == "spoke"
            await settled(app, scope)
            wake, answer = (await app.data.read_events(scope))[-2:]
            assert isinstance(wake.payload, Wake) and answer.kind is EventKind.ASSISTANT_FINAL
            record = await app.data.read_wake(ME, wake.payload.wake_id)
            assert record.status.value == "answered"
            assert "A free wake" in "\n".join(adapter.prepared_inputs[-1].stable_prefix)  # its line, for a checked model
            status = await app.proactive_status(ME)
            assert status["today"] == 1 and status["attempts"] == 1
        finally:
            await app.close()

    asyncio.run(scenario())
