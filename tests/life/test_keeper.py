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

from testkit import data_for, new_store

from himari.common import ConversationId, PrincipalId, TextBlock, TrustedScope
from himari.data.api import (
    AsleepAnswer,
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
        await data.write_day_state(ME, DayState(0, sleep_requested_at=clock.now))  # type: ignore[attr-defined]
        await keeper.look(ME)
        assert await data.current_sleep(ME, 0) is None  # type: ignore[attr-defined]
        keeper.busy = lambda scope: False
        keeper.job_ended(scope, None)  # the turn's job ended: a look at once
        await asyncio.sleep(0.05)
        await keeper.aclose()
        sleep = await data.current_sleep(ME, 0)  # type: ignore[attr-defined]
        assert sleep is not None and sleep.how_fell is FellBy.TOOL
        assert (await data.read_day_state(ME, 0)).sleep_requested_at is None  # type: ignore[attr-defined]

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
