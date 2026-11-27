"""HIMARI's routines (character-routine v0.1 §13): the built-in sleep and diary made on first read (only their range and
length edited, never deleted), the owner's own (trial with its last day, fixed, off; weekdays; HIMARI's say), their
limits said in sentences, a change clearing the slots nobody moved, the keeper placing them each day they are on, and
HIMARI doing with a routine's slot only what the routine lets it. On every backend."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from random import Random
from zoneinfo import ZoneInfo

import pytest
from testkit import SCOPE, data_for, new_store, restart

from himari.data.api import (
    DataError,
    DataErrorCode,
    LifeEntry,
    LifeKind,
    LifeOrigin,
    PlanKind,
    PlanState,
    PlanWrite,
    RoutineBuiltin,
    RoutineSay,
    RoutineState,
    RoutineWrite,
    ROUTINE_OFF_REASON,
)
from himari.data.memory import InMemoryData
from himari.data.sqlite import SqliteData
from himari.life.keeper import DayKeeper

ME = SCOPE.principal_id
ZONE = "Asia/Seoul"
SEOUL = ZoneInfo(ZONE)
NOW = datetime(2026, 11, 26, 12, 0, tzinfo=SEOUL)  # a Thursday noon


def refused(code: DataErrorCode, match: str = "") -> pytest.RaisesExc[DataError]:
    return pytest.raises(DataError, check=lambda e: e.code is code and match in e.message)


def dinner(**more: object) -> RoutineWrite:
    fields: dict[str, object] = {"name": "저녁 식사", "text": "저녁 먹는 중", "start_from": "18:30",
                                 "start_to": "19:30", "minutes_min": 40, "minutes_max": 40,
                                 "state": RoutineState.TRIAL, "trial_until": "2026-12-02"}
    fields.update(more)
    return RoutineWrite(**fields)  # type: ignore[arg-type]


async def routine_slots(data: InMemoryData | SqliteData, routine_id: int) -> list[LifeEntry]:
    return [e for e in await data.list_life(ME, 0, since="2026-11-20") if e.routine_id == routine_id]


def test_the_built_ins_are_made_on_first_read_and_only_their_range_and_length_change() -> None:
    async def scenario() -> None:
        store = new_store()
        data = data_for(store)
        sleep, diary = await data.routines_of(ME, 0, now=NOW)
        assert (sleep.builtin, sleep.start_from, sleep.start_to, sleep.end_from, sleep.end_to, sleep.state) == (
            RoutineBuiltin.SLEEP, "23:00", "02:00", "07:00", "10:00", RoutineState.FIXED)
        assert (diary.builtin, diary.start_from, diary.minutes_min, diary.minutes_max, diary.text) == (
            RoutineBuiltin.DIARY, "21:00", 30, 30, "일기 쓰기")
        data = data_for(restart(store))
        assert await data.routines_of(ME, 0, now=NOW) == (sleep, diary)  # made once
        changed = await data.edit_routine(ME, sleep.routine_id, RoutineWrite(
            "다른 이름", "x", "22:00", "01:00", 1, 1, 1, RoutineState.OFF, None, RoutineSay.NONE, "06:00", "09:00"),
            expected_revision=1, zone=ZONE, now=NOW)
        assert (changed.name, changed.state, changed.himari, changed.start_from, changed.end_to, changed.revision) == (
            "잠", RoutineState.FIXED, RoutineSay.MOVE, "22:00", "09:00", 2)  # only its ranges
        for wrong, said in ((("20:00", "01:00", "06:00", "09:00"), "starts between 21:00 and 05:00"),
                            (("23:00", "02:00", "04:00", "09:00"), "ends between 05:00 and 12:00"),
                            (("04:30", "05:00", "05:00", "05:10"), "5 to 11 hours apart"),
                            (("23:05", "02:00", "07:00", "10:00"), "ten minutes")):
            with refused(DataErrorCode.PLAN_REFUSED, said):
                await data.edit_routine(ME, sleep.routine_id, RoutineWrite(
                    "잠", "잠", wrong[0], wrong[1], 300, 660, end_from=wrong[2], end_to=wrong[3]),
                    expected_revision=2, zone=ZONE, now=NOW)
        with refused(DataErrorCode.PLAN_REFUSED, "10 to 60 minutes"):
            await data.edit_routine(ME, diary.routine_id, RoutineWrite("일기", "x", "21:00", "21:00", 5, 30),
                                    expected_revision=1, zone=ZONE, now=NOW)
        with refused(DataErrorCode.PLAN_REFUSED, "from 18:00"):
            await data.edit_routine(ME, diary.routine_id, RoutineWrite("일기", "x", "17:00", "17:00", 30, 30),
                                    expected_revision=1, zone=ZONE, now=NOW)
        with refused(DataErrorCode.PLAN_REFUSED, "never deleted"):
            await data.retire_routine(ME, diary.routine_id, expected_revision=1, zone=ZONE, now=NOW)

    asyncio.run(scenario())


def test_the_owners_routine_and_its_limits() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        made = await data.add_routine(ME, 0, dinner(name=" 저녁  식사 ", weekdays=31), zone=ZONE, now=NOW)
        assert (made.name, made.builtin, made.weekdays, made.state, made.trial_until, made.himari, made.revision) == (
            "저녁 식사", None, 31, RoutineState.TRIAL, "2026-12-02", RoutineSay.MOVE, 1)
        assert [r.routine_id for r in await data.routines_of(ME, 0, now=NOW)][-1] == made.routine_id
        for wrong, said in ((dinner(start_from="19:30", start_to="18:30"), "earliest first"),
                            (dinner(minutes_min=50, minutes_max=40), "at least its shortest"),
                            (dinner(weekdays=0), "at least one weekday"),
                            (dinner(trial_until="2026-11-25"), "1 to 60 days"),
                            (dinner(trial_until="2027-01-30"), "1 to 60 days"),
                            (dinner(trial_until=None), "last day")):
            with refused(DataErrorCode.PLAN_REFUSED, said):
                await data.add_routine(ME, 0, wrong, zone=ZONE, now=NOW)
        with refused(DataErrorCode.TOO_LARGE):
            await data.add_routine(ME, 0, dinner(name="가" * 41), zone=ZONE, now=NOW)
        with refused(DataErrorCode.VERSION_CONFLICT):
            await data.edit_routine(ME, made.routine_id, dinner(), expected_revision=2, zone=ZONE, now=NOW)
        later = NOW + timedelta(days=10)  # an ended trial keeps its day when edited
        kept = await data.edit_routine(ME, made.routine_id, dinner(start_from="18:00"), expected_revision=1,
                                       zone=ZONE, now=later)
        assert kept.trial_until == "2026-12-02" and kept.trial_ended(later.date())
        for n in range(17):  # 2 built-ins + dinner + 17 = 20
            await data.add_routine(ME, 0, dinner(name=f"일 {n}"), zone=ZONE, now=NOW)
        with refused(DataErrorCode.LIMIT_EXCEEDED):
            await data.add_routine(ME, 0, dinner(name="하나 더"), zone=ZONE, now=NOW)

    asyncio.run(scenario())


def test_the_keeper_places_a_routine_each_day_it_is_on_never_over_the_sleep() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        made = await data.add_routine(ME, 0, dinner(weekdays=1 << 3), zone=ZONE, now=NOW)  # Thursdays only
        keeper = DayKeeper(data, time_zone=ZONE, clock=lambda: NOW, rng=Random(1))
        await keeper.ensure_slots(ME)
        (slot,) = await routine_slots(data, made.routine_id)
        assert (slot.day, slot.plan_kind, slot.text, slot.origin, slot.minutes) == (
            "2026-11-26", PlanKind.OWN, "저녁 먹는 중", LifeOrigin.WRITER, 40)
        assert "18:30" <= (slot.time_of_day or "") <= "19:30"
        assert slot.routine is not None and slot.routine.trial
        await keeper.ensure_slots(ME)  # once a day
        assert len(await routine_slots(data, made.routine_id)) == 1
        sleep = next(r for r in await data.routines_of(ME, 0, now=NOW) if r.builtin is RoutineBuiltin.SLEEP)
        night = [e for e in await data.list_life(ME, 0, since="2026-11-26") if e.sleep_slot]
        assert night and all(e.routine_id == sleep.routine_id for e in night)
        late = await data.add_routine(ME, 0, dinner(name="야식", start_from="23:00", start_to="23:30", weekdays=127),
                                      zone=ZONE, now=NOW)
        await data.edit_routine(ME, sleep.routine_id, RoutineWrite(
            "잠", "잠", "22:00", "22:00", 300, 660, end_from="08:00", end_to="08:00"),
            expected_revision=1, zone=ZONE, now=NOW)  # the sleep from 22:00: no room for the night snack
        await keeper.ensure_slots(ME)
        assert await routine_slots(data, late.routine_id) == []
        tonight = [e for e in await data.list_life(ME, 0, since="2026-11-26") if e.sleep_slot]
        assert {e.time_of_day for e in tonight} == {"22:00"}  # placed again with the new range

    asyncio.run(scenario())


def test_a_change_places_the_unmoved_slots_again_and_off_cancels_them() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        made = await data.add_routine(ME, 0, dinner(state=RoutineState.FIXED), zone=ZONE, now=NOW)
        keeper = DayKeeper(data, time_zone=ZONE, clock=lambda: NOW, rng=Random(2))
        await keeper.ensure_slots(ME)
        today, tomorrow = sorted(await routine_slots(data, made.routine_id), key=lambda e: e.day or "")
        await data.write_plan(ME, 0, PlanWrite("2026-11-27", "20:00", 40, PlanKind.OWN, "저녁 먹는 중",
                                               entry_id=tomorrow.entry_id), origin=LifeOrigin.USER, zone=ZONE,
                              now=NOW)  # the owner moved tomorrow's
        changed = await data.edit_routine(ME, made.routine_id, dinner(state=RoutineState.FIXED, minutes_min=60,
                                                                      minutes_max=60),
                                          expected_revision=1, zone=ZONE, now=NOW)
        left = await routine_slots(data, made.routine_id)
        assert [e.entry_id for e in left] == [tomorrow.entry_id]  # today's unmoved one cleared; the moved one stays
        await keeper.ensure_slots(ME)
        again = [e for e in await routine_slots(data, made.routine_id) if e.day == "2026-11-26"]
        assert [e.minutes for e in again] == [60]
        await data.edit_routine(ME, made.routine_id, dinner(state=RoutineState.OFF), expected_revision=changed.revision,
                                zone=ZONE, now=NOW)
        states = {e.day: (e.plan_state, e.reason) for e in await routine_slots(data, made.routine_id)}
        assert states == {"2026-11-26": (PlanState.CANCELLED, ROUTINE_OFF_REASON),
                          "2026-11-27": (None, None)}  # the owner's moved one stays
        await keeper.ensure_slots(ME)  # off: nothing placed
        assert len(await routine_slots(data, made.routine_id)) == 2

    asyncio.run(scenario())


def test_himari_does_with_a_routines_slot_only_what_it_lets() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        keeper = DayKeeper(data, time_zone=ZONE, clock=lambda: NOW, rng=Random(4))
        made = {say: await data.add_routine(ME, 0, dinner(name=say.value, himari=say), zone=ZONE, now=NOW)
                for say in RoutineSay}
        await keeper.ensure_slots(ME)
        slot = {say: next(e for e in await routine_slots(data, r.routine_id) if e.day == "2026-11-26")
                for say, r in made.items()}

        def moved(say: RoutineSay) -> PlanWrite:
            e = slot[say]
            return PlanWrite("2026-11-26", "20:00", 40, PlanKind.TOGETHER, "딴 거", entry_id=e.entry_id,
                             reason="늦게 먹을래")

        with refused(DataErrorCode.PLAN_REFUSED, "leave it as it is"):
            await data.write_plan(ME, 0, moved(RoutineSay.NONE), origin=LifeOrigin.HIMARI, zone=ZONE, now=NOW)
        with refused(DataErrorCode.PLAN_REFUSED, "leave it as it is"):
            await data.cancel_plan(ME, slot[RoutineSay.NONE].entry_id, "안 먹어", origin=LifeOrigin.HIMARI,
                                   zone=ZONE, now=NOW)
        kept = await data.write_plan(ME, 0, moved(RoutineSay.MOVE), origin=LifeOrigin.HIMARI, zone=ZONE, now=NOW)
        assert (kept.time_of_day, kept.plan_kind, kept.text, kept.routine_id) == (  # only its times
            "20:00", PlanKind.OWN, "저녁 먹는 중", made[RoutineSay.MOVE].routine_id)
        with refused(DataErrorCode.PLAN_REFUSED, "you may move it, not cancel it"):
            await data.cancel_plan(ME, kept.entry_id, "안 먹어", origin=LifeOrigin.HIMARI, zone=ZONE, now=NOW)
        await data.cancel_plan(ME, slot[RoutineSay.MOVE_CANCEL].entry_id, "배불러", origin=LifeOrigin.HIMARI,
                               zone=ZONE, now=NOW)
        owners = PlanWrite("2026-11-26", "20:30", 40, PlanKind.OWN, "저녁 먹는 중",
                           entry_id=slot[RoutineSay.NONE].entry_id)
        await data.write_plan(ME, 0, owners, origin=LifeOrigin.USER, zone=ZONE, now=NOW)  # the owner may
        await data.retire_routine(ME, made[RoutineSay.NONE].routine_id, expected_revision=1, zone=ZONE, now=NOW)
        plain = next(e for e in await data.list_life(ME, 0, since="2026-11-26")
                     if e.entry_id == slot[RoutineSay.NONE].entry_id)
        assert plain.routine is None  # a deleted routine's slot is a plain one
        await data.cancel_plan(ME, plain.entry_id, "그냥", origin=LifeOrigin.HIMARI, zone=ZONE, now=NOW)

    asyncio.run(scenario())


def test_a_slot_is_written_by_the_keeper_only_for_a_routine() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        with refused(DataErrorCode.INVALID_ARGUMENT, "routine slots only"):
            await data.write_plan(ME, 0, PlanWrite("2026-11-26", "18:00", 40, PlanKind.OWN, "x"),
                                  origin=LifeOrigin.WRITER, zone=ZONE, now=NOW)
        made = await data.write_plan(ME, 0, PlanWrite("2026-11-26", "18:00", 40, PlanKind.OWN, "x", routine_id=7),
                                     origin=LifeOrigin.USER, zone=ZONE, now=NOW)
        assert made.routine_id is None and made.kind is LifeKind.PLAN  # only the keeper puts a slot on a routine

    asyncio.run(scenario())
