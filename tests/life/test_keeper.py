"""The day's keeper (character-day v0.1 §5, §6, §18.7): it places the nights' sleep slots, lets HIMARI fall asleep at
its slot (never mid-answer, at once after go_to_sleep), counts the messages and pokes a sleeping HIMARI meets
(nothing, a mumble, awake), lets it doze off again, wakes it at the slot's end, marks a promise missed and lets it
sleep waiting, and closes the day after three unbroken hours — the session closed with its handoff, the passes and
the world told — handing the next session the day before on its first day."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from random import Random
from zoneinfo import ZoneInfo

import pytest
from testkit import data_for, new_store

from himari.common import ConversationId, PrincipalId, TextBlock, TrustedScope
from himari.data.api import (
    AsleepAnswer,
    DataError,
    DataErrorCode,
    DayState,
    FellBy,
    LifeKind,
    LifeOrigin,
    MessageOrigin,
    OriginKind,
    PlanKind,
    PlanState,
    PlanWrite,
    SleepState,
    UserMessage,
    WokeBy,
)
from himari.life.keeper import DayKeeper

ME = PrincipalId("alice")
ZONE = "Asia/Seoul"
SEOUL = ZoneInfo(ZONE)
WEB = MessageOrigin(OriginKind.WEB)


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=SEOUL)


class Clock:
    def __init__(self, start: str) -> None:
        self.now = at(start)

    def __call__(self) -> datetime:
        return self.now

    def go(self, when: str) -> None:
        self.now = at(when)


async def kept(clock: Clock, *, busy: bool = False) -> tuple[DayKeeper, object, TrustedScope, list[object]]:
    data = data_for(new_store())
    told: list[object] = []
    keeper = DayKeeper(data, time_zone=ZONE, clock=clock, rng=Random(0), busy=lambda scope: busy)  # type: ignore[arg-type]
    keeper.on_sleep = lambda principal, state: told.append(state)
    keeper.on_session = lambda principal, summary, closed: told.append(("session", summary.conversation_id, closed))

    async def closed(principal: PrincipalId, scope: TrustedScope | None) -> None:
        told.append(("closed", None if scope is None else scope.conversation_id))

    keeper.on_closed = closed
    for day, start, minutes in (("2026-11-27", "00:30", 460), ("2026-11-27", "23:30", 480)):
        await data.write_plan(ME, 0, PlanWrite(day, start, minutes, PlanKind.SLEEP, "잠"),  # type: ignore[attr-defined]
                              origin=LifeOrigin.WRITER, zone=ZONE, now=clock.now)
    session = await data.open_day_session(ME, "2026-11-26", "11월 26일 (목)")  # type: ignore[attr-defined]
    return keeper, data, TrustedScope(ME, session), told


def test_it_falls_asleep_at_its_slot_but_never_mid_answer_and_the_state_is_told() -> None:
    async def scenario() -> None:
        clock = Clock("2026-11-26 23:00")
        keeper, data, scope, told = await kept(clock, busy=True)
        await keeper.look(ME)  # the first look: its state kept, not told
        assert (await keeper.sleep_view(ME)).state is SleepState.AWAKE and told == []
        clock.go("2026-11-27 00:05")
        await keeper.look(ME)
        assert told == [SleepState.DROWSY]
        assert (await keeper.view(scope)).minutes_to_sleep == 25  # type: ignore[union-attr]
        clock.go("2026-11-27 00:31")
        await keeper.look(ME)
        assert (await data.current_sleep(ME, 0)) is None  # a job runs in the day session: it waits  # type: ignore[attr-defined]
        keeper.busy = lambda scope: False
        await keeper.look(ME)
        sleep = await data.current_sleep(ME, 0)  # type: ignore[attr-defined]
        assert sleep is not None and (sleep.fell_at, sleep.how_fell) == (clock.now, FellBy.SCHEDULE)
        slot = next(e for e in await data.list_life(ME, 0, since="2026-11-26")  # type: ignore[attr-defined]
                    if e.sleep_slot and e.time_of_day == "00:30")
        assert sleep.slot_id == slot.entry_id  # §5.1: the slot it fell asleep in, kept with the sleep
        assert told == [SleepState.DROWSY, SleepState.ASLEEP]
        other = TrustedScope(ME, ConversationId("conv_other"))
        assert await keeper.view(other) is None and await keeper.answer(other, WEB) is None  # not the day's

    asyncio.run(scenario())


def test_a_sleeping_himari_meets_messages_with_nothing_a_mumble_then_wakes_and_dozes_off_again() -> None:
    async def scenario() -> None:
        clock = Clock("2026-11-27 00:31")
        keeper, data, scope, told = await kept(clock)
        await keeper.look(ME)
        answers = []
        for minute in ("01:00", "01:01", "01:02"):
            clock.go(f"2026-11-27 {minute}")
            answers.append(await keeper.answer(scope, WEB))
        assert answers == [AsleepAnswer.NONE, AsleepAnswer.MUMBLE, AsleepAnswer.WAKE]
        assert told[-2:] == [SleepState.HALF_ASLEEP, SleepState.AWAKE]
        woken = await data.current_sleep(ME, 0)  # type: ignore[attr-defined]
        assert (woken.woke_at, woken.how_woke, woken.messages, woken.mumbles) == (clock.now, WokeBy.USER, 3, 1)
        assert await keeper.answer(scope, WEB) is None  # awake now
        assert await keeper.answer(scope, MessageOrigin(OriginKind.WAKE, "d1")) is None  # a wake is no message
        view = await keeper.view(scope)
        assert view is not None and (view.state, view.woken_at) == (SleepState.AWAKE, clock.now)
        clock.go("2026-11-27 01:15")
        await keeper.look(ME)
        assert (await data.current_sleep(ME, 0)).asleep is False  # type: ignore[attr-defined]  # not 20 quiet minutes
        clock.go("2026-11-27 01:22")
        await keeper.look(ME)
        again = await data.current_sleep(ME, 0)  # type: ignore[attr-defined]
        assert again.asleep and again.fell_at == clock.now and again.sleep_id != woken.sleep_id
        clock.go("2026-11-27 01:23")
        assert await keeper.poke(ME) is AsleepAnswer.NONE  # deep again: a poke counts as a message would
        assert (await data.current_sleep(ME, 0)).messages == 0  # type: ignore[attr-defined]  # but is no message

    asyncio.run(scenario())


def test_woken_within_an_hour_of_its_slots_end_it_is_up_for_the_day() -> None:
    async def scenario() -> None:
        clock = Clock("2026-11-27 04:00")
        keeper, data, scope, told = await kept(clock)
        await keeper.look(ME)  # asleep (its slot came while it was not looked at)
        clock.go("2026-11-27 07:30")
        assert await keeper.poke(ME) is AsleepAnswer.MUMBLE  # light sleep now
        assert await keeper.poke(ME) is AsleepAnswer.WAKE
        slot = next(e for e in await data.list_life(ME, 0, since="2026-11-26")  # type: ignore[attr-defined]
                    if e.sleep_slot and e.day == "2026-11-27" and e.time_of_day == "00:30")
        assert slot.minutes == 420  # 00:30 to 07:30 now
        clock.go("2026-11-27 08:00")
        await keeper.look(ME)
        assert not (await data.current_sleep(ME, 0)).asleep  # type: ignore[attr-defined]  # it stays up

    asyncio.run(scenario())


def test_three_unbroken_hours_close_the_day_and_hand_the_next_session_the_day_before() -> None:
    async def scenario() -> None:
        clock = Clock("2026-11-26 22:40")
        keeper, data, scope, told = await kept(clock)
        await data.append_event(scope, UserMessage((TextBlock("잘 자"),), origin=WEB))  # type: ignore[attr-defined]
        clock.go("2026-11-27 00:31")
        await keeper.look(ME)
        clock.go("2026-11-27 03:31")
        await keeper.look(ME)
        assert ("closed", scope.conversation_id) in told and ("session", scope.conversation_id, True) in told
        assert (await data.read_conversation(scope)).closed_at == clock.now  # type: ignore[attr-defined]
        assert (await data.current_sleep(ME, 0)).closed_day  # type: ignore[attr-defined]
        state = await data.read_day_state(ME, 0)  # type: ignore[attr-defined]
        assert state.last_closing_at == clock.now and state.before is not None
        assert [(w.who, w.text) for w in state.before.last_words] == [("user", "잘 자")]
        clock.go("2026-11-27 08:10")
        await keeper.look(ME)
        woke = await data.current_sleep(ME, 0)  # type: ignore[attr-defined]
        assert (woke.woke_at, woke.how_woke) == (clock.now, WokeBy.SCHEDULE)
        nxt = await data.open_day_session(ME, "2026-11-27", "11월 27일 (금)")  # type: ignore[attr-defined]
        before = await keeper.before(TrustedScope(ME, nxt))
        assert before is not None and before.day == "2026-11-26" and before.woke_at == clock.now
        clock.go("2026-11-28 09:00")  # not its first day any more
        assert await keeper.before(TrustedScope(ME, nxt)) is None

    asyncio.run(scenario())


def test_a_day_with_no_session_still_closes_and_the_world_is_told() -> None:
    async def scenario() -> None:
        clock = Clock("2026-11-27 00:31")
        keeper, data, scope, told = await kept(clock)
        await data.close_day_session(ME, scope.conversation_id, clock.now)  # type: ignore[attr-defined]
        await keeper.look(ME)
        clock.go("2026-11-27 03:31")
        await keeper.look(ME)
        assert ("closed", None) in told
        assert (await data.read_day_state(ME, 0)).before is None  # type: ignore[attr-defined]

    asyncio.run(scenario())


def test_a_missed_promise_is_marked_and_it_falls_asleep_waiting_for_it() -> None:
    async def scenario() -> None:
        clock = Clock("2026-11-26 22:40")
        keeper, data, scope, told = await kept(clock)
        slot = next(e for e in await data.list_life(ME, 0, since="2026-11-26")  # type: ignore[attr-defined]
                    if e.sleep_slot and e.time_of_day == "00:30")
        await data.write_plan(ME, 0, PlanWrite("2026-11-27", "05:00", 380, PlanKind.SLEEP, "잠", slot.entry_id,  # type: ignore[attr-defined]
                                               reason="방송 보고 잘래"), origin=LifeOrigin.HIMARI, zone=ZONE, now=clock.now)
        promise = await data.write_plan(ME, 0, PlanWrite("2026-11-27", "03:00", 120, PlanKind.TOGETHER, "방송"),  # type: ignore[attr-defined]
                                        origin=LifeOrigin.HIMARI, zone=ZONE, now=clock.now)
        clock.go("2026-11-27 03:30")
        await keeper.look(ME)
        plans = {e.entry_id: e for e in await data.list_life(ME, 0, since="2026-11-26")}  # type: ignore[attr-defined]
        assert plans[promise.entry_id].plan_state is PlanState.MISSED
        assert (plans[slot.entry_id].time_of_day, plans[slot.entry_id].reason) == ("03:30", "약속을 기다리다 잠듦")
        sleep = await data.current_sleep(ME, 0)  # type: ignore[attr-defined]
        assert sleep is not None and sleep.asleep and sleep.how_fell is FellBy.MISSED

    asyncio.run(scenario())


def test_go_to_sleep_is_kept_until_the_turn_ends_then_it_sleeps() -> None:
    async def scenario() -> None:
        clock = Clock("2026-11-26 22:10")
        keeper, data, scope, told = await kept(clock, busy=True)
        slot = next(e for e in await data.list_life(ME, 0, since="2026-11-26")  # type: ignore[attr-defined]
                    if e.sleep_slot and e.time_of_day == "00:30")
        await data.write_day_state(ME, DayState(0, sleep_requested_at=clock.now,  # type: ignore[attr-defined]
                                                sleep_requested_slot=slot.entry_id))
        await keeper.look(ME)
        assert await data.current_sleep(ME, 0) is None  # type: ignore[attr-defined]
        keeper.busy = lambda scope: False
        keeper.job_ended(scope, None)  # the turn's job ended: a look at once
        await asyncio.sleep(0.05)
        await keeper.aclose()
        sleep = await data.current_sleep(ME, 0)  # type: ignore[attr-defined]
        assert sleep is not None and (sleep.how_fell, sleep.slot_id) == (FellBy.TOOL, slot.entry_id)
        state = await data.read_day_state(ME, 0)  # type: ignore[attr-defined]
        assert (state.sleep_requested_at, state.sleep_requested_slot) == (None, None)

    asyncio.run(scenario())


def test_the_nights_with_no_slot_get_one_placed() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        keeper = DayKeeper(data, time_zone=ZONE, clock=lambda: at("2026-11-26 12:00"), rng=Random(3))  # type: ignore[arg-type]
        await keeper.look(ME)
        slots = [e for e in await data.list_life(ME, 0) if e.kind is LifeKind.PLAN]  # type: ignore[attr-defined]
        assert len(slots) == 2 and all(e.plan_kind is PlanKind.SLEEP and e.origin is LifeOrigin.WRITER for e in slots)
        await keeper.look(ME)  # never twice
        assert len([e for e in await data.list_life(ME, 0) if e.kind is LifeKind.PLAN]) == 2  # type: ignore[attr-defined]

    asyncio.run(scenario())


def test_dates_move_with_the_clock() -> None:
    assert at("2026-11-27 00:30") - at("2026-11-26 23:30") == timedelta(hours=1)


def test_the_closing_waits_for_the_day_sessions_job_taking_no_new_work_then_closes_in_one_step() -> None:
    """§6 (review 3, 5): a job runs in the day session as the three hours come — the session is fenced (no new
    message, Run, wake or compaction: DAY_CLOSED) and stays open; once the job ends the keeper closes it, its sleep
    marked and its handoff kept together, told after."""
    async def scenario() -> None:
        clock = Clock("2026-11-26 22:40")
        keeper, data, scope, told = await kept(clock)
        await data.append_event(scope, UserMessage((TextBlock("잘 자"),), origin=WEB))  # type: ignore[attr-defined]
        other = TrustedScope(ME, await data.create_conversation(ME))  # type: ignore[attr-defined]
        clock.go("2026-11-27 00:31")
        await keeper.look(ME)
        clock.go("2026-11-27 03:31")
        keeper.busy = lambda s: s == scope  # a mumble's job, say
        await keeper.look(ME)
        assert (await data.read_conversation(scope)).closed_at is None  # type: ignore[attr-defined]
        assert keeper.closing(scope) and not keeper.closing(other)
        with pytest.raises(DataError, check=lambda e: e.code is DataErrorCode.DAY_CLOSED):
            await keeper.admit(scope)
        await keeper.admit(other)  # any other conversation takes work as before
        assert not (await data.current_sleep(ME, 0)).closed_day  # type: ignore[attr-defined]
        keeper.busy = lambda s: False
        keeper.job_ended(scope, None)  # the job settled: a look at once
        await asyncio.sleep(0.05)
        await keeper.aclose()
        assert (await data.read_conversation(scope)).closed_at == clock.now  # type: ignore[attr-defined]
        assert (await data.current_sleep(ME, 0)).closed_day  # type: ignore[attr-defined]
        state = await data.read_day_state(ME, 0)  # type: ignore[attr-defined]
        assert state.before is not None and state.before.conversation_id == scope.conversation_id
        assert [(w.who, w.text) for w in state.before.last_words] == [("user", "잘 자")]
        assert ("session", scope.conversation_id, True) in told and ("closed", scope.conversation_id) in told
        with pytest.raises(DataError, check=lambda e: e.code is DataErrorCode.DAY_CLOSED):
            await keeper.admit(scope)  # closed: refused from now on, here and in Data

    asyncio.run(scenario())


def test_a_restart_after_the_slots_end_wakes_it_at_that_end_and_counts_no_more() -> None:
    """§5.2, §6 (review 4): the server off from 01:00 to 11:00 — back, HIMARI woke at its slot's end (08:10)."""
    async def scenario() -> None:
        clock = Clock("2026-11-27 00:31")
        keeper, data, scope, told = await kept(clock)
        await keeper.look(ME)
        clock.go("2026-11-27 11:00")
        again = DayKeeper(data, time_zone=ZONE, clock=clock, rng=Random(0))  # a new process
        await again.look(ME)
        woke = await data.current_sleep(ME, 0)  # type: ignore[attr-defined]
        assert (woke.woke_at, woke.how_woke, woke.closed_day) == (at("2026-11-27 08:10"), WokeBy.SCHEDULE, True)

    asyncio.run(scenario())


def test_the_handoff_reaches_a_session_opened_the_same_date_after_the_closing() -> None:
    """§3.3 (review 11): a session opened at 00:20 closes at 03:30; the next, opened the same date, is handed it —
    found by the closed session's id, not its date."""
    async def scenario() -> None:
        clock = Clock("2026-11-27 00:20")
        keeper, data, scope, told = await kept(clock)
        await data.close_day_session(ME, scope.conversation_id, clock.now)  # type: ignore[attr-defined]
        first = await data.open_day_session(ME, "2026-11-27", "11월 27일 (금)")  # type: ignore[attr-defined]
        await data.append_event(TrustedScope(ME, first), UserMessage((TextBlock("늦었다, 자자"),), origin=WEB))  # type: ignore[attr-defined]
        clock.go("2026-11-27 00:31")
        await keeper.look(ME)
        clock.go("2026-11-27 03:31")
        await keeper.look(ME)
        assert (await data.read_conversation(TrustedScope(ME, first))).closed_at == clock.now  # type: ignore[attr-defined]
        clock.go("2026-11-27 09:00")
        await keeper.look(ME)
        second = await data.open_day_session(ME, "2026-11-27", "11월 27일 (금)")  # type: ignore[attr-defined]
        before = await keeper.before(TrustedScope(ME, second))
        assert before is not None and before.conversation_id == first and before.day == "2026-11-27"
        assert [w.text for w in before.last_words] == ["늦었다, 자자"]

    asyncio.run(scenario())


def test_a_turn_of_the_day_session_finds_tonights_slot_placed_on_first_need() -> None:
    """§4.2 (review 13): a principal first here at night — the turn's look at the sleep places the nights' slots
    (go_to_sleep then only moves tonight's)."""
    async def scenario() -> None:
        data = data_for(new_store())
        keeper = DayKeeper(data, time_zone=ZONE, clock=lambda: at("2026-11-26 22:30"), rng=Random(5))  # type: ignore[arg-type]
        scope = TrustedScope(ME, await data.open_day_session(ME, "2026-11-26", "11월 26일 (목)"))  # type: ignore[attr-defined]
        view = await keeper.view(scope)
        assert view is not None and view.tonight is not None and view.tonight.origin is LifeOrigin.WRITER
        slots = [e for e in await data.list_life(ME, 0, since="2026-11-26") if e.sleep_slot]  # type: ignore[attr-defined]
        assert len(slots) == 2

    asyncio.run(scenario())


def test_a_missed_promise_pulls_the_sleep_only_past_a_slot_still_between() -> None:
    """§5.4 (review 7): missed at 03:30, the sleep put off to 05:00 for it starts at 04:30, after HIMARI's own 04:00
    slot — it falls asleep then, waiting, and the sleep says so."""
    async def scenario() -> None:
        clock = Clock("2026-11-26 22:40")
        keeper, data, scope, told = await kept(clock)
        slot = next(e for e in await data.list_life(ME, 0, since="2026-11-26")  # type: ignore[attr-defined]
                    if e.sleep_slot and e.time_of_day == "00:30")
        await data.write_plan(ME, 0, PlanWrite("2026-11-27", "05:00", 380, PlanKind.SLEEP, "잠", slot.entry_id,  # type: ignore[attr-defined]
                                               reason="방송 보고 잘래"), origin=LifeOrigin.HIMARI, zone=ZONE, now=clock.now)
        await data.write_plan(ME, 0, PlanWrite("2026-11-27", "03:00", 120, PlanKind.TOGETHER, "방송"),  # type: ignore[attr-defined]
                              origin=LifeOrigin.HIMARI, zone=ZONE, now=clock.now)
        await data.write_plan(ME, 0, PlanWrite("2026-11-27", "04:00", 30, PlanKind.OWN, "정리"),  # type: ignore[attr-defined]
                              origin=LifeOrigin.HIMARI, zone=ZONE, now=clock.now)
        clock.go("2026-11-27 03:30")
        await keeper.look(ME)
        moved = next(e for e in await data.list_life(ME, 0, since="2026-11-26")  # type: ignore[attr-defined]
                     if e.entry_id == slot.entry_id)
        assert (moved.time_of_day, moved.minutes, moved.reason) == ("04:30", 410, "약속을 기다리다 잠듦")
        assert await data.current_sleep(ME, 0) is None  # type: ignore[attr-defined]
        clock.go("2026-11-27 04:30")
        await keeper.look(ME)
        sleep = await data.current_sleep(ME, 0)  # type: ignore[attr-defined]
        assert sleep is not None and (sleep.how_fell, sleep.slot_id) == (FellBy.MISSED, slot.entry_id)

    asyncio.run(scenario())


def test_a_message_after_the_slots_end_finds_himari_woken_on_time() -> None:
    """Second review 3: dozed at 05:12 in a slot ending 08:10, two messages at 08:13 before the keeper's next look
    — the sleep ended at 08:10 (on schedule, 2h58m: no closing), and both are met awake."""
    async def scenario() -> None:
        clock = Clock("2026-11-27 05:12")
        keeper, data, scope, told = await kept(clock)
        await keeper.look(ME)
        fell = await data.current_sleep(ME, 0)  # type: ignore[attr-defined]
        assert fell is not None and fell.asleep and fell.fell_at == clock.now
        clock.go("2026-11-27 08:13")
        assert await keeper.answer(scope, WEB) is None and await keeper.answer(scope, WEB) is None
        woke = await data.current_sleep(ME, 0)  # type: ignore[attr-defined]
        assert (woke.sleep_id, woke.woke_at, woke.how_woke, woke.messages) == (
            fell.sleep_id, at("2026-11-27 08:10"), WokeBy.SCHEDULE, 0)
        await keeper.look(ME)
        assert not (await data.current_sleep(ME, 0)).closed_day  # type: ignore[attr-defined]
        assert (await data.read_conversation(scope)).closed_at is None  # type: ignore[attr-defined]
        clock.go("2026-11-28 08:30")  # a turn reads it the same way: its slot's end, not now
        dozed = await data.open_sleep(ME, 0, at=at("2026-11-28 06:00"), how=FellBy.SCHEDULE,  # type: ignore[attr-defined]
                                      slot_id=fell.slot_id)
        await keeper.view(scope)
        assert (await data.current_sleep(ME, 0)).woke_at == dozed.fell_at  # type: ignore[attr-defined]  # (ended)

    asyncio.run(scenario())


def test_an_active_sleeps_slot_end_never_moves_before_now() -> None:
    """Second review 2: dozed at 06:30 in a 00:30–08:10 slot, the owner's edit at 07:00 to end it at 05:30 is
    refused (its end moves only to now or later); a later end is taken, and the wake follows it."""
    async def scenario() -> None:
        clock = Clock("2026-11-27 06:30")
        keeper, data, scope, told = await kept(clock)
        state = await data.read_day_state(ME, 0)  # type: ignore[attr-defined]
        await data.write_day_state(ME, DayState(0, woken_at=at("2026-11-27 06:00"),  # type: ignore[attr-defined]
                                                last_closing_at=state.last_closing_at or at("2026-11-27 03:00")))
        await keeper.look(ME)
        sleep = await data.current_sleep(ME, 0)  # type: ignore[attr-defined]
        assert sleep is not None and sleep.asleep
        slot = next(e for e in await data.list_life(ME, 0, since="2026-11-26")  # type: ignore[attr-defined]
                    if e.entry_id == sleep.slot_id)
        clock.go("2026-11-27 07:00")
        with pytest.raises(DataError, check=lambda e: e.code is DataErrorCode.PLAN_REFUSED and "asleep" in e.message):
            await data.write_plan(ME, 0, PlanWrite("2026-11-27", "00:30", 300, PlanKind.SLEEP, "잠",  # type: ignore[attr-defined]
                                                   slot.entry_id), origin=LifeOrigin.USER, zone=ZONE, now=clock.now)
        with pytest.raises(DataError, check=lambda e: e.code is DataErrorCode.INVALID_ARGUMENT):
            await data.end_sleep(ME, sleep.sleep_id, at=at("2026-11-27 05:30"), how=WokeBy.SCHEDULE)  # type: ignore[attr-defined]
        await data.write_plan(ME, 0, PlanWrite("2026-11-27", "00:30", 450, PlanKind.SLEEP, "밤샘",  # type: ignore[attr-defined]
                                               slot.entry_id), origin=LifeOrigin.USER, zone=ZONE, now=clock.now)
        moved = next(e for e in await data.list_life(ME, 0, since="2026-11-26")  # type: ignore[attr-defined]
                     if e.entry_id == slot.entry_id)
        assert (moved.minutes, moved.text) == (450, "잠")  # second review 7: its text stays, only times move
        clock.go("2026-11-27 08:00")
        await keeper.look(ME)
        woke = await data.current_sleep(ME, 0)  # type: ignore[attr-defined]
        assert (woke.woke_at, woke.how_woke) == (at("2026-11-27 08:00"), WokeBy.SCHEDULE)
        assert woke.woke_at >= woke.fell_at

    asyncio.run(scenario())
