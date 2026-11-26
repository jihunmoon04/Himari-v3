"""HIMARI's day in Data (character-day v0.1 §3, §4, §5, §18.2): the schedule's slots and their limits (one sleep slot a
night, from 21:00 to 05:00, 5 to 11 hours, ending 05:00 to 12:00; nothing over it; today to seven days on; never a past
slot; twenty a day for HIMARI), cancelled and missed slots kept, the sleeps and the keeper's state, and the day
sessions (one open at a time, closed when trashed, read at once by memory passes). On every backend."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from testkit import SCOPE, data_for, new_store, restart, user

from himari.common import ConversationId, PrincipalId, RawRepresentation, TrustedScope
from himari.data.api import (
    MAX_HIMARI_PLAN_WRITES,
    AsleepAnswer,
    PromptBasis,
    RunPreparation,
    WakeKind,
    WakeReason,
    DataError,
    DataErrorCode,
    DayBefore,
    DayLine,
    DayState,
    FellBy,
    LifeKind,
    LifeOrigin,
    LifePlan,
    LifeWriteStart,
    PlanChange,
    PlanKind,
    PlanOp,
    PlanState,
    PlanWrite,
    RawReceived,
    WokeBy,
)

ALICE = SCOPE.principal_id
ZONE = "Asia/Seoul"
NOW = datetime(2026, 11, 26, 13, 0, tzinfo=UTC)  # 22:00 in Seoul, a Thursday: tonight is the night of 11-26


async def chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


def refused(code: DataErrorCode, match: str = "") -> pytest.RaisesExc[DataError]:
    return pytest.raises(DataError, check=lambda e: e.code is code and match in e.message)


def slot(day: str, at: str, minutes: int, kind: PlanKind = PlanKind.OWN, text: str = "일", *,
         entry_id: int | None = None, arc_id: int | None = None, reason: str | None = None) -> PlanWrite:
    return PlanWrite(day, at, minutes, kind, text, entry_id, arc_id, reason)


def test_the_keeper_places_a_sleep_slot_and_himari_only_moves_it_within_its_night() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        sleep = await data.write_plan(ALICE, 1, slot("2026-11-27", "00:30", 460, PlanKind.SLEEP, "잠"),
                                      origin=LifeOrigin.WRITER, zone=ZONE, now=NOW)
        assert (sleep.kind, sleep.plan_kind, sleep.minutes, sleep.origin) == (
            LifeKind.PLAN, PlanKind.SLEEP, 460, LifeOrigin.WRITER)
        with refused(DataErrorCode.PLAN_REFUSED, "only moved, never made"):
            await data.write_plan(ALICE, 1, slot("2026-11-26", "23:00", 480, PlanKind.SLEEP),
                                  origin=LifeOrigin.HIMARI, zone=ZONE, now=NOW)
        with refused(DataErrorCode.PLAN_REFUSED, "has its sleep slot already"):
            await data.write_plan(ALICE, 1, slot("2026-11-26", "23:00", 480, PlanKind.SLEEP),
                                  origin=LifeOrigin.WRITER, zone=ZONE, now=NOW)
        moved = slot("2026-11-27", "05:00", 380, PlanKind.SLEEP, "잠", entry_id=sleep.entry_id, reason="방송 보고 잘래")
        for bad, why in ((slot("2026-11-27", "05:10", 370, PlanKind.SLEEP, entry_id=sleep.entry_id), "starts between"),
                         (slot("2026-11-27", "01:00", 240, PlanKind.SLEEP, entry_id=sleep.entry_id), "5 to 11 hours"),
                         (slot("2026-11-26", "23:30", 300, PlanKind.SLEEP, entry_id=sleep.entry_id), "ends between"),
                         (slot("2026-11-26", "21:30", 600, PlanKind.SLEEP, entry_id=sleep.entry_id), "in the past"),
                         (slot("2026-11-27", "23:00", 480, PlanKind.SLEEP, entry_id=sleep.entry_id), "stays in its"),
                         (slot("2026-11-27", "01:00", 480, PlanKind.OWN, entry_id=sleep.entry_id), "stays a sleep")):
            with refused(DataErrorCode.PLAN_REFUSED, why):
                await data.write_plan(ALICE, 1, bad, origin=LifeOrigin.HIMARI, zone=ZONE, now=NOW)
        kept = await data.write_plan(ALICE, 1, moved, origin=LifeOrigin.HIMARI, zone=ZONE, now=NOW)
        assert (kept.entry_id, kept.day, kept.time_of_day, kept.minutes, kept.reason, kept.origin, kept.revision) == (
            sleep.entry_id, "2026-11-27", "05:00", 380, "방송 보고 잘래", LifeOrigin.HIMARI, 2)
        with refused(DataErrorCode.PLAN_REFUSED, "cannot be cancelled or deleted"):
            await data.retire_life(ALICE, sleep.entry_id, expected_revision=2)
        with refused(DataErrorCode.PLAN_REFUSED, "cannot be cancelled"):
            await data.cancel_plan(ALICE, sleep.entry_id, "싫어", origin=LifeOrigin.HIMARI, zone=ZONE, now=NOW)
        # character-day §4.2 (review 13): the generic life edit never rewrites a sleep slot — it is only moved
        with refused(DataErrorCode.PLAN_REFUSED, "only moved"):
            await data.edit_life(ALICE, sleep.entry_id, "밤샘", expected_revision=2)

    asyncio.run(scenario())


def test_a_slot_over_the_sleep_slot_is_refused_until_the_sleep_moves() -> None:
    """§5.3: a promise at 03:00 is refused over the sleep slot; once the slot moves to 05:00 it is written."""
    async def scenario() -> None:
        store = new_store()
        data = data_for(store)
        sleep = await data.write_plan(ALICE, 1, slot("2026-11-27", "00:30", 460, PlanKind.SLEEP, "잠"),
                                      origin=LifeOrigin.WRITER, zone=ZONE, now=NOW)
        together = slot("2026-11-27", "03:00", 120, PlanKind.TOGETHER, "방송 같이 보기")
        with refused(DataErrorCode.PLAN_REFUSED, f"overlaps your sleep slot (00:30–08:10, id {sleep.entry_id})"):
            await data.write_plan(ALICE, 1, together, origin=LifeOrigin.HIMARI, zone=ZONE, now=NOW)
        await data.write_plan(ALICE, 1, slot("2026-11-27", "05:00", 380, PlanKind.SLEEP, "잠",
                                             entry_id=sleep.entry_id, reason="방송 보고 잘래"),
                              origin=LifeOrigin.HIMARI, zone=ZONE, now=NOW)
        made = await data.write_plan(ALICE, 1, together, origin=LifeOrigin.HIMARI, zone=ZONE, now=NOW)
        assert (made.plan_kind, made.minutes, made.time_of_day) == (PlanKind.TOGETHER, 120, "03:00")
        with refused(DataErrorCode.PLAN_REFUSED, "would cover the slot"):  # the sleep may not come back over it
            await data.write_plan(ALICE, 1, slot("2026-11-27", "01:00", 420, PlanKind.SLEEP, "잠",
                                                 entry_id=sleep.entry_id), origin=LifeOrigin.HIMARI, zone=ZONE,
                                  now=NOW)
        with refused(DataErrorCode.PLAN_REFUSED, "Only HIMARI makes a together slot, when it sets a time with you"):
            await data.write_plan(ALICE, 1, slot("2026-11-28", "15:00", 60, PlanKind.TOGETHER),
                                  origin=LifeOrigin.USER, zone=ZONE, now=NOW)
        data = data_for(restart(store))
        listed = [e for e in await data.list_life(ALICE, 1, now=NOW) if e.kind is LifeKind.PLAN]
        assert {(e.plan_kind, e.time_of_day) for e in listed} == {(PlanKind.SLEEP, "05:00"),
                                                                    (PlanKind.TOGETHER, "03:00")}

    asyncio.run(scenario())


def test_a_slot_is_dated_today_to_seven_days_on_never_past_and_a_work_slot_names_an_open_project() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        for bad, why in ((slot("2026-12-04", "10:00", 60), "to 7 days on"),
                         (slot("2026-11-25", "10:00", 60), "from today"),
                         (slot("2026-11-26", "20:00", 60), "already past"),
                         (slot("2026-11-27", "10:00", 4), "5 to 720 minutes"),
                         (slot("2026-11-27", "10:00", 60, PlanKind.WORK, arc_id=9), "no open project 9"),
                         (slot("2026-11-27", "10:00", 60, PlanKind.WORK), "names an open project")):
            with refused(DataErrorCode.PLAN_REFUSED, why):
                await data.write_plan(ALICE, 1, bad, origin=LifeOrigin.HIMARI, zone=ZONE, now=NOW)
        own = await data.write_plan(ALICE, 1, slot("2026-12-03", "10:00", 90, text="산책"), origin=LifeOrigin.USER,
                                    zone=ZONE, now=NOW)
        assert (own.origin, own.minutes, own.plan_kind) == (LifeOrigin.USER, 90, PlanKind.OWN)
        later = NOW + timedelta(days=8)
        with refused(DataErrorCode.PLAN_REFUSED, "is over"):
            await data.write_plan(ALICE, 1, slot("2026-12-04", "10:00", 90, entry_id=own.entry_id),
                                  origin=LifeOrigin.HIMARI, zone=ZONE, now=later)

    asyncio.run(scenario())


def test_a_cancelled_or_missed_slot_is_kept_with_why_and_never_changed_again() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        walk = await data.write_plan(ALICE, 1, slot("2026-11-27", "15:00", 60, text="산책"),
                                     origin=LifeOrigin.HIMARI, zone=ZONE, now=NOW)
        promise = await data.write_plan(ALICE, 1, slot("2026-11-27", "16:00", 60, PlanKind.TOGETHER, "게임"),
                                        origin=LifeOrigin.HIMARI, zone=ZONE, now=NOW)
        with refused(DataErrorCode.INVALID_ARGUMENT):
            await data.cancel_plan(ALICE, walk.entry_id, " ", origin=LifeOrigin.HIMARI, zone=ZONE, now=NOW)
        cancelled = await data.cancel_plan(ALICE, walk.entry_id, "비가 와서", origin=LifeOrigin.HIMARI, zone=ZONE,
                                           now=NOW)
        assert (cancelled.plan_state, cancelled.reason, cancelled.revision) == (PlanState.CANCELLED, "비가 와서", 2)
        with refused(DataErrorCode.NOT_FOUND):
            await data.mark_plan_missed(ALICE, walk.entry_id, now=NOW)  # cancelled: no longer a live slot
        own = await data.write_plan(ALICE, 1, slot("2026-11-27", "18:00", 60, text="그림"), origin=LifeOrigin.HIMARI,
                                    zone=ZONE, now=NOW)
        with refused(DataErrorCode.INVALID_ARGUMENT):
            await data.mark_plan_missed(ALICE, own.entry_id, now=NOW)  # only a together slot is missed
        missed = await data.mark_plan_missed(ALICE, promise.entry_id, now=NOW)
        assert missed.plan_state is PlanState.MISSED
        with refused(DataErrorCode.NOT_FOUND):
            await data.write_plan(ALICE, 1, slot("2026-11-27", "17:00", 60, entry_id=walk.entry_id),
                                  origin=LifeOrigin.HIMARI, zone=ZONE, now=NOW)
        states = {e.entry_id: e.plan_state for e in await data.list_life(ALICE, 1, now=NOW)}
        assert states == {walk.entry_id: PlanState.CANCELLED, promise.entry_id: PlanState.MISSED, own.entry_id: None}

    asyncio.run(scenario())


def test_himari_changes_at_most_twenty_slots_a_day_each_counted_once() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        made = [await data.write_plan(ALICE, 1, slot("2026-11-28", f"{10 + i // 6:02d}:{i % 6 * 10:02d}", 10,
                                                     text=f"일 {i}"), origin=LifeOrigin.HIMARI, zone=ZONE, now=NOW)
                for i in range(MAX_HIMARI_PLAN_WRITES)]
        again = await data.write_plan(ALICE, 1, slot("2026-11-28", "18:00", 30, text="다시", entry_id=made[0].entry_id),
                                      origin=LifeOrigin.HIMARI, zone=ZONE, now=NOW)  # the same slot again: counted
        assert again.revision == 2
        with refused(DataErrorCode.PLAN_REFUSED, f"changed {MAX_HIMARI_PLAN_WRITES} slots today"):
            await data.write_plan(ALICE, 1, slot("2026-11-28", "19:00", 30), origin=LifeOrigin.HIMARI, zone=ZONE,
                                  now=NOW)
        await data.write_plan(ALICE, 1, slot("2026-11-28", "19:00", 30), origin=LifeOrigin.USER, zone=ZONE,
                              now=NOW)  # the user's are not counted
        await data.write_plan(ALICE, 1, slot("2026-11-28", "20:00", 30), origin=LifeOrigin.HIMARI, zone=ZONE,
                              now=NOW + timedelta(days=1))  # the next day starts again

    asyncio.run(scenario())


def test_a_review_leaves_out_alone_a_plan_over_the_sleep_slot_and_keeps_lengths_and_kinds() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        await data.write_plan(ALICE, 1, slot("2026-11-27", "23:30", 480, PlanKind.SLEEP, "잠"),
                              origin=LifeOrigin.WRITER, zone=ZONE, now=NOW)
        write = await data.begin_life_write(ALICE, LifeWriteStart(1, "2026-11-26", "p", "m", b"{}",
                                                                  "application/json", 10, conversation_id=None))
        await data.record_life_write(write, RawReceived(b"{}", "application/json"))
        written = await data.apply_life_write(write, LifePlan(
            "일기", (("2026-11-27", "늦게까지 그리기"), ("2026-11-27", "산책")), times=("23:00", "15:00"),
            plan_minutes=(120, None)), now=NOW, zone=ZONE)
        plans = [e for e in written if e.kind is LifeKind.PLAN]
        assert [(e.text, e.minutes, e.plan_kind) for e in plans] == [("산책", 60, PlanKind.OWN)]

    asyncio.run(scenario())


def test_go_to_sleep_starts_tonights_slot_now_and_asks_the_keeper_to_sleep() -> None:
    """§4.3, §18.6: a schedule Tool's change through the call's result — go_to_sleep moves the night's slot to now
    (rounded up to ten minutes, its end kept) and marks ``sleep_requested_at``; before 21:00 it is refused."""
    async def scenario() -> None:
        data = data_for(new_store())
        sleep = await data.write_plan(ALICE, 1, slot("2026-11-27", "00:30", 460, PlanKind.SLEEP, "잠"),
                                      origin=LifeOrigin.WRITER, zone=ZONE, now=NOW)
        from himari.data.sqlite import SqliteData
        at = NOW + timedelta(minutes=3)  # 22:03
        if isinstance(data, SqliteData):
            with data._tx() as tx:  # noqa: SLF001 — the change as the call's commit applies it
                data._apply_plan_change(tx, ALICE, PlanChange(PlanOp.SLEEP_NOW, 1, ZONE, at, reason="잘 자"))
        else:
            data._stage_plan_change(ALICE, PlanChange(PlanOp.SLEEP_NOW, 1, ZONE, at, reason="잘 자"))()
        moved = next(e for e in await data.list_life(ALICE, 1, now=at) if e.entry_id == sleep.entry_id)
        assert (moved.day, moved.time_of_day, moved.minutes, moved.reason, moved.origin) == (
            "2026-11-26", "22:10", 600, "잘 자", LifeOrigin.HIMARI)
        state = await data.read_day_state(ALICE, 1)
        assert (state.sleep_requested_at, state.sleep_requested_slot) == (at, sleep.entry_id)  # second review 1
        with refused(DataErrorCode.PLAN_REFUSED, "from 21:00"):
            if isinstance(data, SqliteData):
                with data._tx() as tx:  # noqa: SLF001
                    data._apply_plan_change(tx, ALICE, PlanChange(PlanOp.SLEEP_NOW, 1, ZONE, NOW - timedelta(hours=3)))
            else:
                data._stage_plan_change(ALICE, PlanChange(PlanOp.SLEEP_NOW, 1, ZONE, NOW - timedelta(hours=3)))

    asyncio.run(scenario())


def test_go_to_sleep_with_no_slot_tonight_never_makes_one() -> None:
    """§4.2 (review 13): the keeper places the night's slot on first need; the Tool only moves it — with none there
    it is refused and nothing is written."""
    async def scenario() -> None:
        data = data_for(new_store())
        from himari.data.sqlite import SqliteData
        change = PlanChange(PlanOp.SLEEP_NOW, 1, ZONE, NOW + timedelta(minutes=3), reason="잘 자")
        with refused(DataErrorCode.PLAN_REFUSED, "no sleep slot"):
            if isinstance(data, SqliteData):
                with data._tx() as tx:  # noqa: SLF001
                    data._apply_plan_change(tx, ALICE, change)
            else:
                data._stage_plan_change(ALICE, change)
        assert not [e for e in await data.list_life(ALICE, 1, now=NOW) if e.kind is LifeKind.PLAN]
        assert (await data.read_day_state(ALICE, 1)).sleep_requested_at is None

    asyncio.run(scenario())


def apply_change(data: object, change: PlanChange) -> None:
    """A schedule Tool's change as the call's commit applies it."""
    from himari.data.sqlite import SqliteData
    if isinstance(data, SqliteData):
        with data._tx() as tx:  # noqa: SLF001
            data._apply_plan_change(tx, ALICE, change)
    else:
        data._stage_plan_change(ALICE, change)()  # type: ignore[attr-defined]


def test_go_to_sleep_in_a_slot_already_over_is_refused_and_asks_nothing() -> None:
    """Second review 1: the night's slot was placed early (22:00–03:00); at 04:00 go_to_sleep is refused — never the
    next night's slot — and no request is kept."""
    async def scenario() -> None:
        data = data_for(new_store())
        early = await data.write_plan(ALICE, 1, slot("2026-11-26", "22:00", 300, PlanKind.SLEEP, "잠"),
                                      origin=LifeOrigin.WRITER, zone=ZONE, now=NOW - timedelta(hours=1))
        await data.write_plan(ALICE, 1, slot("2026-11-27", "23:30", 480, PlanKind.SLEEP, "잠"),
                              origin=LifeOrigin.WRITER, zone=ZONE, now=NOW)  # the next night's
        at = NOW + timedelta(hours=6)  # 04:00 in Seoul
        with refused(DataErrorCode.PLAN_REFUSED, "is over"):
            apply_change(data, PlanChange(PlanOp.SLEEP_NOW, 1, ZONE, at, reason="졸려"))
        state = await data.read_day_state(ALICE, 1)
        assert (state.sleep_requested_at, state.sleep_requested_slot) == (None, None)
        kept = next(e for e in await data.list_life(ALICE, 1, now=at) if e.entry_id == early.entry_id)
        assert (kept.time_of_day, kept.minutes, kept.revision) == ("22:00", 300, 1)

    asyncio.run(scenario())


def test_a_wake_never_precedes_its_fall_and_a_slot_slept_in_never_ends_before_now() -> None:
    """Second review 2: a sleep's end before it fell asleep is INVALID_ARGUMENT; while HIMARI sleeps in its slot, the
    slot's end moves only to now or later (PLAN_REFUSED), for anyone."""
    async def scenario() -> None:
        data = data_for(new_store())
        night = await data.write_plan(ALICE, 1, slot("2026-11-27", "00:30", 460, PlanKind.SLEEP, "잠"),
                                      origin=LifeOrigin.WRITER, zone=ZONE, now=NOW)
        fell = NOW + timedelta(hours=8, minutes=30)  # 06:30 in Seoul, dozed off again after being woken
        sleep = await data.open_sleep(ALICE, 1, at=fell, how=FellBy.SCHEDULE, slot_id=night.entry_id)
        with refused(DataErrorCode.INVALID_ARGUMENT, "at or after it began"):
            await data.end_sleep(ALICE, sleep.sleep_id, at=fell - timedelta(minutes=1), how=WokeBy.SCHEDULE)
        assert (await data.current_sleep(ALICE, 1)).asleep  # type: ignore[union-attr]
        now = fell + timedelta(minutes=30)  # 07:00
        for origin in (LifeOrigin.USER, LifeOrigin.HIMARI, LifeOrigin.WRITER):
            with refused(DataErrorCode.PLAN_REFUSED, "asleep in this slot"):
                await data.write_plan(ALICE, 1, slot("2026-11-27", "00:30", 300, PlanKind.SLEEP, "잠",
                                                     entry_id=night.entry_id), origin=origin, zone=ZONE, now=now)
        moved = await data.write_plan(ALICE, 1, slot("2026-11-27", "00:30", 390, PlanKind.SLEEP, "잠",
                                                     entry_id=night.entry_id), origin=LifeOrigin.USER, zone=ZONE,
                                      now=now)  # to now: taken
        assert moved.minutes == 390
        woke = await data.end_sleep(ALICE, sleep.sleep_id, at=now, how=WokeBy.SCHEDULE)
        assert woke.woke_at == now >= woke.fell_at

    asyncio.run(scenario())


def test_the_user_never_turns_a_slot_into_together_and_a_sleep_slot_moves_without_its_text() -> None:
    """Second review 7, shared by both stores: a user's own slot never becomes together (a together slot HIMARI made
    stays one when the user moves it); a sleep slot's text stays as it is — only its times move."""
    async def scenario() -> None:
        data = data_for(new_store())
        own = await data.write_plan(ALICE, 1, slot("2026-11-28", "15:00", 60, text="산책"), origin=LifeOrigin.USER,
                                    zone=ZONE, now=NOW)
        with refused(DataErrorCode.PLAN_REFUSED, "Only HIMARI makes a together slot"):
            await data.write_plan(ALICE, 1, slot("2026-11-28", "15:00", 60, PlanKind.TOGETHER, "산책",
                                                 entry_id=own.entry_id), origin=LifeOrigin.USER, zone=ZONE, now=NOW)
        promise = await data.write_plan(ALICE, 1, slot("2026-11-28", "18:00", 60, PlanKind.TOGETHER, "게임"),
                                        origin=LifeOrigin.HIMARI, zone=ZONE, now=NOW)
        later = await data.write_plan(ALICE, 1, slot("2026-11-28", "19:00", 60, PlanKind.TOGETHER, "게임",
                                                     entry_id=promise.entry_id), origin=LifeOrigin.USER, zone=ZONE,
                                      now=NOW)
        assert (later.plan_kind, later.time_of_day) == (PlanKind.TOGETHER, "19:00")
        night = await data.write_plan(ALICE, 1, slot("2026-11-27", "00:30", 460, PlanKind.SLEEP, "잠"),
                                      origin=LifeOrigin.WRITER, zone=ZONE, now=NOW)
        for origin, text, at in ((LifeOrigin.USER, "밤샘", "00:30"), (LifeOrigin.USER, "밤샘", "01:00"),
                                 (LifeOrigin.HIMARI, "늦잠", "01:30")):
            moved = await data.write_plan(ALICE, 1, slot("2026-11-27", at, 420, PlanKind.SLEEP, text,
                                                         entry_id=night.entry_id), origin=origin, zone=ZONE, now=NOW)
            assert (moved.text, moved.time_of_day, moved.minutes) == ("잠", at, 420)

    asyncio.run(scenario())


def test_a_sleep_counts_the_messages_and_ends_once_and_the_keeper_state_is_kept() -> None:
    async def scenario() -> None:
        store = new_store()
        data = data_for(store)
        sleep = await data.open_sleep(ALICE, 1, at=NOW, how=FellBy.SCHEDULE, slot_id=42)
        assert sleep.slot_id == 42  # §5.1 (review 1): the slot it sleeps in, kept with it
        with refused(DataErrorCode.INVALID_STATE_TRANSITION):
            await data.open_sleep(ALICE, 1, at=NOW, how=FellBy.TOOL)
        await data.count_wake(ALICE, sleep.sleep_id, count=1, at=NOW + timedelta(minutes=5),
                              answer=AsleepAnswer.NONE, message=True)
        await data.count_wake(ALICE, sleep.sleep_id, count=2, at=NOW + timedelta(minutes=6),
                              answer=AsleepAnswer.MUMBLE, message=False)  # a poke
        counted = await data.count_wake(ALICE, sleep.sleep_id, count=3, at=NOW + timedelta(minutes=7),
                                        answer=AsleepAnswer.WAKE, message=True)
        assert (counted.messages, counted.mumbles, counted.count, counted.first_message_at,
                counted.last_message_at) == (2, 1, 3, NOW + timedelta(minutes=5), NOW + timedelta(minutes=7))
        woke = await data.end_sleep(ALICE, sleep.sleep_id, at=NOW + timedelta(minutes=7), how=WokeBy.USER)
        assert (woke.woke_at, woke.how_woke, woke.asleep) == (NOW + timedelta(minutes=7), WokeBy.USER, False)
        with refused(DataErrorCode.INVALID_STATE_TRANSITION):
            await data.end_sleep(ALICE, sleep.sleep_id, at=NOW, how=WokeBy.SCHEDULE)
        again = await data.open_sleep(ALICE, 1, at=NOW + timedelta(minutes=30), how=FellBy.SCHEDULE)
        before = DayBefore("2026-11-26", (DayLine("21:58", "user", "잘 자"),), kept=("03:00 방송",),
                           fell_at=again.fell_at, how_fell=FellBy.SCHEDULE, delayed="방송 보고 잘래",
                           sleep_id=again.sleep_id, conversation_id=ConversationId("conv_9"))
        assert not await data.note_closing(ALICE, 1, again.sleep_id, at=NOW + timedelta(hours=4), before=before)
        with refused(DataErrorCode.INVALID_STATE_TRANSITION):  # once a sleep
            await data.note_closing(ALICE, 1, again.sleep_id, at=NOW + timedelta(hours=5), before=None)
        await data.write_day_state(ALICE, DayState(2, woken_at=NOW))
        data = data_for(restart(store))
        assert [(s.sleep_id, s.slot_id) for s in await data.recent_sleeps(ALICE, 1, limit=14)] == [
            (again.sleep_id, None), (sleep.sleep_id, 42)]
        assert (await data.current_sleep(ALICE, 1)).closed_day  # type: ignore[union-attr]
        state = await data.read_day_state(ALICE, 1)
        assert (state.last_closing_at, state.before) == (NOW + timedelta(hours=4), before)
        assert (await data.read_day_state(ALICE, 2)).woken_at == NOW
        assert await data.current_sleep(ALICE, 3) is None
        with refused(DataErrorCode.NOT_FOUND):
            await data.end_sleep(PrincipalId("mallory"), again.sleep_id, at=NOW, how=WokeBy.USER)

    asyncio.run(scenario())


def test_a_day_session_is_one_open_at_a_time_closed_when_trashed_and_read_at_once() -> None:
    async def scenario() -> None:
        store = new_store()
        data = data_for(store)
        first = await data.open_day_session(ALICE, "2026-11-26", "11월 26일 (목)")
        with refused(DataErrorCode.DUPLICATE_IDENTITY):
            await data.open_day_session(ALICE, "2026-11-27", "11월 27일 (금)")
        current = await data.current_day_session(ALICE)
        assert current is not None and (current.conversation_id, current.title, current.day_session,
                                        current.closed_at) == (first, "11월 26일 (목)", "2026-11-26", None)
        scope = TrustedScope(ALICE, first)
        await data.append_event(scope, user("안녕"))
        quiet = datetime.now(UTC) - timedelta(minutes=10)
        assert not [d for d in await data.memory_due(quiet_before=quiet, now=datetime.now(UTC))]  # not quiet yet
        assert await data.close_day_session(ALICE, first, NOW)
        assert not await data.close_day_session(ALICE, first, NOW)
        assert [d.scope for d in await data.memory_due(quiet_before=quiet, now=datetime.now(UTC))] == [scope]
        assert (await data.read_conversation(scope)).closed_at == NOW
        second = await data.open_day_session(ALICE, "2026-11-27", "11월 27일 (금)")
        await data.trash_conversation(TrustedScope(ALICE, second))
        assert await data.current_day_session(ALICE) is None
        third = await data.open_day_session(ALICE, "2026-11-27", "11월 27일 (금)")
        assert third not in (first, second)
        data = data_for(restart(store))
        assert (await data.current_day_session(ALICE)).conversation_id == third  # type: ignore[union-attr]
        listed = {s.conversation_id: s.day_session for s in await data.list_conversations(ALICE)}
        assert listed[first] == "2026-11-26" and listed[third] == "2026-11-27"
        assert ConversationId(first) in listed

    asyncio.run(scenario())


def preparation() -> RunPreparation:
    return RunPreparation(b"{}", "application/json", RawRepresentation.ADAPTER_SERIALIZED, (), PromptBasis(0, 0), "p",
                          "m")


def test_the_closing_is_one_step_and_a_closed_day_takes_no_message_run_or_wake() -> None:
    """§6 step 1, §3.2 (review 2, 5): the session closed, the sleep marked and the handoff kept in one commit; after it
    the day session refuses — in the write's own transaction — a message, a Run and a wake (DAY_CLOSED), and is
    still read."""
    async def scenario() -> None:
        store = new_store()
        data = data_for(store)
        day = await data.open_day_session(ALICE, "2026-11-26", "11월 26일 (목)")
        scope = TrustedScope(ALICE, day)
        await data.append_event(scope, user("안녕"))
        sleep = await data.open_sleep(ALICE, 1, at=NOW, how=FellBy.SCHEDULE)
        before = DayBefore("2026-11-26", (DayLine("22:00", "user", "안녕"),), sleep_id=sleep.sleep_id,
                           conversation_id=day)
        at = NOW + timedelta(hours=3)
        assert await data.note_closing(ALICE, 1, sleep.sleep_id, at=at, before=before, conversation_id=day)
        data = data_for(restart(store))
        assert (await data.read_conversation(scope)).closed_at == at
        assert (await data.current_sleep(ALICE, 1)).closed_day  # type: ignore[union-attr]
        state = await data.read_day_state(ALICE, 1)
        assert (state.last_closing_at, state.before) == (at, before)
        blob = await data.blobs.put(ALICE, chunks(b"screen"), 100)
        for write in (lambda: data.append_event(scope, user("늦었다")),
                      lambda: data.prepare_run(scope, preparation()),
                      lambda: data.begin_wake(scope, (WakeReason(WakeKind.TRY),), "d1"),
                      lambda: data.stage_upload(scope, blob, "screen.png", "image/png")):  # second review 8
            with refused(DataErrorCode.DAY_CLOSED):
                await write()
        assert len(await data.read_events(scope)) == 1  # read as it was
        assert await data.count_staged(scope) == 0
        other = await data.create_conversation(ALICE)  # any other conversation is written as before
        await data.append_event(TrustedScope(ALICE, other), user("여기는"))

    asyncio.run(scenario())


def test_the_keeper_never_writes_a_sleep_slot_over_a_live_slot() -> None:
    """§4.4 (review 7): the keeper's placing and moving go through the same overlap check as everyone's."""
    async def scenario() -> None:
        data = data_for(new_store())
        await data.write_plan(ALICE, 1, slot("2026-11-27", "04:00", 120, text="해돋이"), origin=LifeOrigin.HIMARI,
                              zone=ZONE, now=NOW)
        with refused(DataErrorCode.PLAN_REFUSED, "would cover the slot"):
            await data.write_plan(ALICE, 1, slot("2026-11-27", "05:00", 300, PlanKind.SLEEP, "잠"),
                                  origin=LifeOrigin.WRITER, zone=ZONE, now=NOW)
        placed = await data.write_plan(ALICE, 1, slot("2026-11-26", "23:00", 300, PlanKind.SLEEP, "잠"),
                                       origin=LifeOrigin.WRITER, zone=ZONE, now=NOW)  # it gets up for it
        with refused(DataErrorCode.PLAN_REFUSED, "would cover the slot"):
            await data.write_plan(ALICE, 1, slot("2026-11-26", "23:30", 300, PlanKind.SLEEP, "잠",
                                                 entry_id=placed.entry_id), origin=LifeOrigin.WRITER, zone=ZONE,
                                  now=NOW)

    asyncio.run(scenario())
