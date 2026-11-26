"""HIMARI's day, the pure rules (character-day v0.1 §5, §6, §16, §18.3): placing a sleep slot, the sleep as seen, the
wake count, what the keeper does when (falling asleep at the slot, by go_to_sleep, waiting for a missed promise, never
mid-answer; waking at the slot's end; dozing off again; closing the day), and the handoff."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from random import Random
from zoneinfo import ZoneInfo

import pytest

from himari.common import ConversationId, EventId, TextBlock
from himari.data.api import (
    AssistantFinal,
    AsleepAnswer,
    ConversationEvent,
    DayBefore,
    DayState,
    EventKind,
    FellBy,
    LifeEntry,
    LifeKind,
    LifeOrigin,
    PlanKind,
    PlanState,
    SleepRecord,
    SleepState,
    UserMessage,
    WokeBy,
    plan_span,
)
from himari.life import day
from himari.life.day import Step

SEOUL = ZoneInfo("Asia/Seoul")


def at(text: str) -> datetime:
    """A Seoul time, e.g. "2026-11-26 22:00"."""
    return datetime.fromisoformat(text).replace(tzinfo=SEOUL)


def plan(entry_id: int, when: str, minutes: int, kind: PlanKind = PlanKind.OWN, *, state: PlanState | None = None,
         origin: LifeOrigin = LifeOrigin.WRITER, reason: str | None = None, text: str = "일") -> LifeEntry:
    start = at(when)
    return LifeEntry(entry_id, 1, LifeKind.PLAN, start.date().isoformat(), text, origin, 1, start, start,
                     time_of_day=start.strftime("%H:%M"), minutes=minutes, plan_kind=kind, plan_state=state,
                     reason=reason)


def asleep(fell: str, *, count: int = 0, count_at: str | None = None, woke: str | None = None,
           how_woke: WokeBy | None = None, closed: bool = False, how: FellBy = FellBy.SCHEDULE,
           slot: int | None = 1) -> SleepRecord:
    return SleepRecord(7, 1, at(fell), how, None if woke is None else at(woke), how_woke, count=count,
                       count_at=None if count_at is None else at(count_at), closed_day=closed, slot_id=slot)


SLEEP = plan(1, "2026-11-27 00:30", 460, PlanKind.SLEEP)  # 00:30-08:10, the night of 11-26
NEXT = plan(2, "2026-11-28 00:00", 480, PlanKind.SLEEP)  # the night of 11-27
AFTER = plan(5, "2026-11-28 23:30", 480, PlanKind.SLEEP)  # the night of 11-28


@pytest.mark.parametrize("seed", range(40))
def test_a_placed_sleep_slot_starts_23_to_02_and_ends_07_to_10_at_least_five_hours_on(seed: int) -> None:
    placed = day.place_sleep(date(2026, 11, 26), Random(seed), SEOUL)
    assert placed is not None
    start = at(f"{placed.day} {placed.time_of_day}")
    end = start + timedelta(minutes=placed.minutes)
    assert at("2026-11-26 23:00") <= start <= at("2026-11-27 02:00") and start.minute % 10 == 0
    assert 300 <= placed.minutes <= 660 and end <= at("2026-11-27 12:00")
    assert at("2026-11-27 07:00") <= end or placed.minutes == 300


def test_a_placed_sleep_slot_goes_around_the_nights_promises() -> None:
    late = (at("2026-11-27 02:30"), at("2026-11-27 04:20"))  # a promise in the night: it sleeps after it
    placed = day.place_sleep(date(2026, 11, 26), Random(1), SEOUL, [late])
    assert placed is not None
    assert (placed.day, placed.time_of_day) == ("2026-11-27", "04:20") and placed.minutes >= 300
    morning = (at("2026-11-27 08:00"), at("2026-11-27 09:00"))  # in the morning: it gets up before it
    for seed in range(20):
        placed = day.place_sleep(date(2026, 11, 26), Random(seed), SEOUL, [morning])
        assert placed is not None
        end = at(f"{placed.day} {placed.time_of_day}") + timedelta(minutes=placed.minutes)
        assert end <= morning[0] or placed.minutes == 300


def span_of(placed: day.PlacedSleep | None) -> tuple[datetime, datetime]:
    assert placed is not None
    start = at(f"{placed.day} {placed.time_of_day}")
    return start, start + timedelta(minutes=placed.minutes)


@pytest.mark.parametrize("seed", range(30))
def test_a_placed_sleep_slot_never_covers_a_slot_of_the_night(seed: int) -> None:
    """§4.4 (review 7): around slots one after another the start is pushed past them all, keeping five hours; with a
    slot over 05:00 no span within the limits is left — it sleeps before it, getting up for it; a night full of
    slots gets none."""
    busy = [(at("2026-11-26 23:00"), at("2026-11-27 00:30")), (at("2026-11-27 00:30"), at("2026-11-27 01:40")),
            (at("2026-11-27 07:00"), at("2026-11-27 07:30"))]
    start, end = span_of(day.place_sleep(date(2026, 11, 26), Random(seed), SEOUL, busy))
    assert not any(b[0] < end and start < b[1] for b in busy)
    assert end - start >= timedelta(hours=5) and at("2026-11-27 05:00") <= end <= at("2026-11-27 07:00")
    over_five = [(at("2026-11-27 04:00"), at("2026-11-27 06:00"))]  # nothing within the limits fits
    start, end = span_of(day.place_sleep(date(2026, 11, 26), Random(seed), SEOUL, over_five))
    assert end <= at("2026-11-27 04:00") and end - start >= timedelta(hours=5)
    assert at("2026-11-26 21:00") <= start <= at("2026-11-26 23:00")
    full = [(at("2026-11-26 21:00"), at("2026-11-27 12:00"))]
    assert day.place_sleep(date(2026, 11, 26), Random(seed), SEOUL, full) is None


def test_drowsy_thirty_minutes_before_the_slot_asleep_deep_three_hours_then_light() -> None:
    state = DayState(1)
    view = day.sleep_view(at("2026-11-27 00:05"), SLEEP, None, state, SEOUL)
    assert (view.state, view.minutes_to_sleep) == (SleepState.DROWSY, 25)
    assert day.sleep_view(at("2026-11-26 23:59"), SLEEP, None, state, SEOUL).state is SleepState.AWAKE
    view = day.sleep_view(at("2026-11-27 01:00"), SLEEP, None, state, SEOUL)  # in the slot, not asleep yet
    assert (view.state, view.minutes_to_sleep) == (SleepState.DROWSY, 0)
    woken = DayState(1, woken_at=at("2026-11-27 00:45"))
    assert day.sleep_view(at("2026-11-27 01:00"), SLEEP, None, woken, SEOUL).state is SleepState.AWAKE  # woken in it
    sleep = asleep("2026-11-27 00:30")
    view = day.sleep_view(at("2026-11-27 03:29"), SLEEP, sleep, state, SEOUL)
    assert (view.state, view.deep) == (SleepState.ASLEEP, True)
    assert not day.sleep_view(at("2026-11-27 03:31"), SLEEP, sleep, state, SEOUL).deep


def test_the_wake_count_deep_none_mumble_wake_light_mumble_wake_and_ten_quiet_minutes_reset_it() -> None:
    sleep = asleep("2026-11-27 00:30")
    view = day.sleep_view(at("2026-11-27 01:00"), SLEEP, sleep, DayState(1), SEOUL)
    answers = []
    for minute in (1, 2, 3):
        now = at(f"2026-11-27 01:0{minute}")
        answers.append(day.answer_message(view, sleep, now))
        sleep = SleepRecord(7, 1, sleep.fell_at, sleep.how_fell, count=day.next_count(sleep, now), count_at=now)
    assert answers == [AsleepAnswer.NONE, AsleepAnswer.MUMBLE, AsleepAnswer.WAKE]
    quiet = SleepRecord(7, 1, at("2026-11-27 00:30"), FellBy.SCHEDULE, count=2, count_at=at("2026-11-27 01:00"))
    assert day.next_count(quiet, at("2026-11-27 01:10")) == 1  # ten minutes: deep again
    assert day.answer_message(view, quiet, at("2026-11-27 01:10")) is AsleepAnswer.NONE
    light = day.sleep_view(at("2026-11-27 04:00"), SLEEP, quiet, DayState(1), SEOUL)
    assert day.answer_message(light, quiet, at("2026-11-27 04:00")) is AsleepAnswer.MUMBLE
    assert day.answer_message(light, SleepRecord(7, 1, quiet.fell_at, FellBy.SCHEDULE, count=1,
                                                 count_at=at("2026-11-27 03:58")),
                              at("2026-11-27 04:00")) is AsleepAnswer.WAKE
    awake = day.sleep_view(at("2026-11-26 20:00"), SLEEP, None, DayState(1), SEOUL)
    assert day.answer_message(awake, None, at("2026-11-26 20:00")) is None


def steps(found: list[day.Transition]) -> list[tuple[Step, object]]:
    return [(t.step, t.how or t.night or (t.entry.entry_id if t.entry else None)) for t in found]


def test_it_falls_asleep_at_its_slot_but_never_mid_answer_and_places_the_next_nights() -> None:
    plans = [SLEEP, NEXT]
    now = at("2026-11-27 00:30")
    assert steps(day.due(now, plans, None, DayState(1), SEOUL, running=False)) == [(Step.FALL, FellBy.SCHEDULE)]
    assert day.due(now, plans, None, DayState(1), SEOUL, running=True) == []  # the answer goes on
    found = day.due(at("2026-11-26 12:00"), [], None, DayState(1), SEOUL, running=False)
    assert steps(found) == [(Step.PLACE, date(2026, 11, 26)), (Step.PLACE, date(2026, 11, 27))]
    assert steps(day.due(at("2026-11-27 03:00"), [NEXT], None, DayState(1), SEOUL, running=False)) == [
        (Step.PLACE, date(2026, 11, 26))]  # 03:00 is still the night of 11-26


def test_go_to_sleep_is_applied_once_the_turn_ends_and_only_once() -> None:
    asked = DayState(1, sleep_requested_at=at("2026-11-26 22:03"), sleep_requested_slot=1)
    now = at("2026-11-26 22:04")
    assert day.due(now, [SLEEP, NEXT], None, asked, SEOUL, running=True) == []
    found = day.due(now, [SLEEP, NEXT], None, asked, SEOUL, running=False)
    assert steps(found) == [(Step.FALL, FellBy.TOOL)] and found[0].entry == SLEEP
    slept = asleep("2026-11-26 22:04", woke="2026-11-27 07:00", how_woke=WokeBy.SCHEDULE, closed=True)
    assert day.due(at("2026-11-26 23:00"), [SLEEP, NEXT], slept, asked, SEOUL, running=False) == []


def test_go_to_sleep_only_ever_starts_the_slot_it_moved() -> None:
    """Second review 1: a request bound to a slot that ended (22:00–03:00, looked at 04:00 after a delay) starts
    nothing — never the next night's slot; one bound to no slot neither."""
    early = plan(1, "2026-11-26 22:00", 300, PlanKind.SLEEP)  # the keeper's fallback: up at 03:00 for a slot
    asked = DayState(1, sleep_requested_at=at("2026-11-27 02:50"), sleep_requested_slot=1)
    assert day.due(at("2026-11-27 04:00"), [early, NEXT], None, asked, SEOUL, running=False) == []
    loose = DayState(1, sleep_requested_at=at("2026-11-26 22:03"))
    assert day.due(at("2026-11-26 22:04"), [SLEEP, NEXT], None, loose, SEOUL, running=False) == []


def test_it_wakes_at_its_slots_end_and_closes_the_day_after_three_unbroken_hours() -> None:
    sleep = asleep("2026-11-27 00:30")
    state = DayState(1, last_closing_at=at("2026-11-26 03:00"))
    assert day.due(at("2026-11-27 03:29"), [SLEEP, NEXT], sleep, state, SEOUL, running=False) == []
    assert steps(day.due(at("2026-11-27 03:30"), [SLEEP, NEXT], sleep, state, SEOUL, running=False)) == [
        (Step.CLOSE, None)]
    closed = SleepRecord(7, 1, sleep.fell_at, FellBy.SCHEDULE, closed_day=True, slot_id=1)
    found = day.due(at("2026-11-27 08:10"), [SLEEP, NEXT, AFTER], closed, state, SEOUL, running=False)
    assert steps(found) == [(Step.WAKE, None)] and found[0].at == at("2026-11-27 08:10")
    # the server was off: the sleep ended (6 hours) with no closing — it is done when it is back
    ended = asleep("2026-11-27 00:30", woke="2026-11-27 08:10", how_woke=WokeBy.SCHEDULE)
    assert steps(day.due(at("2026-11-27 09:00"), [SLEEP, NEXT, AFTER], ended, state, SEOUL, running=False)) == [
        (Step.CLOSE, None)]
    # woken after two hours: no closing in that sleep; past 36 hours since the last, one hour is enough
    woken = asleep("2026-11-27 00:30", woke="2026-11-27 02:30", how_woke=WokeBy.USER)
    assert not day.closes(woken, state, at("2026-11-27 09:00"))
    late = DayState(1, last_closing_at=at("2026-11-25 12:00"))
    assert day.closes(asleep("2026-11-27 00:30"), late, at("2026-11-27 01:30"))
    assert not day.closes(asleep("2026-11-27 00:30"), late, at("2026-11-27 01:29"))
    assert not day.closes(asleep("2026-11-26 02:00"), state, at("2026-11-27 09:00"))  # before the last closing
    # a job of the day session runs as the hour comes: the day is fenced, closed once it settles
    found = day.due(at("2026-11-27 03:30"), [SLEEP, NEXT], sleep, state, SEOUL, running=True)
    assert steps(found) == [(Step.FENCE, None)]
    assert steps(day.due(at("2026-11-27 09:00"), [SLEEP, NEXT, AFTER], ended, state, SEOUL, running=True)) == [
        (Step.FENCE, None)]


def test_a_sleep_wakes_at_the_end_of_the_slot_it_fell_asleep_in_whatever_the_time_it_fell() -> None:
    """§5.1 (review 1): the slot is the sleep's own — woken at 06:00 in a 23:00–08:00 slot and dozing off at 06:20,
    it wakes at 08:00 (not the next night's end); a slot of 05:00 first looked at 05:00:30 is that night's too."""
    slot = plan(1, "2026-11-26 23:00", 540, PlanKind.SLEEP)
    tomorrow = plan(2, "2026-11-27 23:30", 480, PlanKind.SLEEP)
    state = DayState(1, woken_at=at("2026-11-27 06:00"), last_closing_at=at("2026-11-27 02:00"))
    woken = asleep("2026-11-27 00:00", woke="2026-11-27 06:00", how_woke=WokeBy.USER)
    found = day.due(at("2026-11-27 06:20"), [slot, tomorrow, AFTER], woken, state, SEOUL, running=False,
                    last_activity_at=at("2026-11-27 06:00"))
    assert steps(found) == [(Step.FALL, FellBy.SCHEDULE)]
    assert found[0].entry == slot  # it dozes off in that slot: the sleep opened will carry its id
    dozed = asleep("2026-11-27 06:20", slot=1)
    assert day.due(at("2026-11-27 07:59"), [slot, tomorrow, AFTER], dozed, state, SEOUL, running=False) == []
    woke = day.due(at("2026-11-27 08:00"), [slot, tomorrow, AFTER], dozed, state, SEOUL, running=False)
    assert steps(woke) == [(Step.WAKE, None)] and woke[0].at == at("2026-11-27 08:00")
    five = plan(3, "2026-11-27 05:00", 360, PlanKind.SLEEP, origin=LifeOrigin.HIMARI)
    found = day.due(at("2026-11-27 05:00:30"), [five, tomorrow, AFTER], None, DayState(1), SEOUL, running=False)
    assert found[-1].step is Step.FALL and found[-1].entry == five
    late = asleep("2026-11-27 05:00", slot=3)
    plans = [five, tomorrow, AFTER]
    assert [t.step for t in day.due(at("2026-11-27 11:00"), plans, late, DayState(1), SEOUL, running=False)] == [
        Step.CLOSE, Step.WAKE]
    assert [t.step for t in day.due(at("2026-11-27 10:59"), plans, late, DayState(1), SEOUL, running=False)] == [
        Step.CLOSE]
    lost = asleep("2026-11-27 01:00", slot=None)  # no slot at all: NO_SLOT_SLEEP on
    assert day.wakes_at([five], lost, SEOUL) == at("2026-11-27 01:00") + day.NO_SLOT_SLEEP


def test_a_restart_counts_no_sleep_past_the_scheduled_wake_and_the_short_closing_only_from_36_hours() -> None:
    """§6 (review 4): back at 11:00, a sleep that dozed off at 06:20 in a slot ending 08:00 woke at 08:00 — under
    three hours, no closing; one that fell at 00:30 closes and wakes at its slot's end. A 03:00–04:30 sleep never
    closes the day at 12:01 because 36 hours passed meanwhile; one still asleep when they pass does, after an hour."""
    slot = plan(1, "2026-11-26 23:00", 540, PlanKind.SLEEP)
    tomorrow = plan(2, "2026-11-27 23:30", 480, PlanKind.SLEEP)
    state = DayState(1, last_closing_at=at("2026-11-26 03:00"))
    dozed = asleep("2026-11-27 06:20")
    found = day.due(at("2026-11-27 11:00"), [slot, tomorrow, AFTER], dozed, state, SEOUL, running=False)
    assert steps(found) == [(Step.WAKE, None)] and found[0].at == at("2026-11-27 08:00")
    whole = asleep("2026-11-27 00:30")
    found = day.due(at("2026-11-27 11:00"), [slot, tomorrow, AFTER], whole, state, SEOUL, running=False)
    assert steps(found) == [(Step.CLOSE, None), (Step.WAKE, None)] and found[1].at == at("2026-11-27 08:00")
    late = DayState(1, last_closing_at=at("2026-11-26 00:01"))  # 36 hours on: 2026-11-27 12:01
    short = asleep("2026-11-27 03:00", woke="2026-11-27 04:30", how_woke=WokeBy.USER)
    assert not day.closes(short, late, at("2026-11-27 12:01"))
    assert day.due(at("2026-11-27 12:01"), [slot, tomorrow, AFTER], short, late, SEOUL, running=False) == []
    earlier = DayState(1, last_closing_at=at("2026-11-25 16:00"))  # 36 hours on: 2026-11-27 04:00, in that sleep
    assert day.closing_at(short, earlier) == at("2026-11-27 04:00") and day.closes(short, earlier,
                                                                                  at("2026-11-27 12:01"))


def test_after_being_woken_in_its_slot_it_dozes_off_after_twenty_quiet_minutes() -> None:
    woke = at("2026-11-27 02:00")
    state = DayState(1, woken_at=woke, last_closing_at=at("2026-11-26 03:00"))
    woken = asleep("2026-11-27 00:30", woke="2026-11-27 02:00", how_woke=WokeBy.USER)
    plans = [SLEEP, NEXT]
    assert day.due(at("2026-11-27 02:15"), plans, woken, state, SEOUL, running=False,
                   last_activity_at=at("2026-11-27 02:05")) == []
    assert day.due(at("2026-11-27 02:24"), plans, woken, state, SEOUL, running=False,
                   last_activity_at=at("2026-11-27 02:05")) == []
    assert steps(day.due(at("2026-11-27 02:25"), plans, woken, state, SEOUL, running=False,
                         last_activity_at=at("2026-11-27 02:05"))) == [(Step.FALL, FellBy.SCHEDULE)]
    view = day.sleep_view(at("2026-11-27 02:10"), SLEEP, woken, state, SEOUL)
    assert (view.state, view.woken_at) == (SleepState.AWAKE, woke)
    assert day.up_for_the_day(SLEEP, at("2026-11-27 07:20"), SEOUL)
    assert not day.up_for_the_day(SLEEP, at("2026-11-27 07:00"), SEOUL)


def test_a_promise_with_no_word_for_thirty_minutes_is_missed_and_it_sleeps_waiting_for_it() -> None:
    """§5.4: the sleep put off to 05:00 for a 03:00 promise; by 03:30 no word — missed, and it falls asleep."""
    moved = plan(1, "2026-11-27 05:00", 380, PlanKind.SLEEP, origin=LifeOrigin.HIMARI, reason="방송 보고 잘래")
    promise = plan(3, "2026-11-27 03:00", 120, PlanKind.TOGETHER, origin=LifeOrigin.HIMARI)
    plans = [moved, NEXT, promise]
    assert day.due(at("2026-11-27 03:29"), plans, None, DayState(1), SEOUL, running=False) == []
    found = day.due(at("2026-11-27 03:30"), plans, None, DayState(1), SEOUL, running=False)
    assert steps(found) == [(Step.MISS, 3), (Step.FALL, FellBy.MISSED)] and found[0].pull == moved
    assert found[0].at == at("2026-11-27 03:30")
    assert day.due(at("2026-11-27 03:30"), plans, None, DayState(1), SEOUL, running=False,
                   last_user_at=at("2026-11-27 03:10")) == []  # the user came
    afternoon = plan(4, "2026-11-27 15:00", 60, PlanKind.TOGETHER)  # missed, but no sleep is pulled by day
    found = day.due(at("2026-11-27 15:30"), [moved, NEXT, AFTER, afternoon], None, DayState(1), SEOUL, running=False)
    assert steps(found) == [(Step.MISS, 4)] and found[0].pull is None


def test_a_missed_promise_pulls_only_a_sleep_put_off_for_it_and_never_over_another_slot() -> None:
    """§5.4 (review 7): a 22:00 promise missed at 22:30 leaves a 01:30 sleep alone (not put off for it); a missed 03:00
    promise pulls the 05:00 sleep only to the end of a 04:00 slot still between — it falls asleep there, waiting."""
    natural = plan(1, "2026-11-27 01:30", 420, PlanKind.SLEEP)
    early = plan(3, "2026-11-26 22:00", 30, PlanKind.TOGETHER, origin=LifeOrigin.HIMARI)
    found = day.due(at("2026-11-26 22:30"), [natural, NEXT, early], None, DayState(1), SEOUL, running=False)
    assert steps(found) == [(Step.MISS, 3)] and found[0].pull is None
    moved = plan(1, "2026-11-27 05:00", 380, PlanKind.SLEEP, origin=LifeOrigin.HIMARI, reason="방송 보고 잘래")
    promise = plan(3, "2026-11-27 03:00", 120, PlanKind.TOGETHER, origin=LifeOrigin.HIMARI)
    own = plan(4, "2026-11-27 04:00", 30, PlanKind.OWN, origin=LifeOrigin.HIMARI)
    found = day.due(at("2026-11-27 03:30"), [moved, NEXT, promise, own], None, DayState(1), SEOUL, running=False)
    assert steps(found) == [(Step.MISS, 3)] and (found[0].pull, found[0].at) == (moved, at("2026-11-27 04:30"))
    pulled = plan(1, "2026-11-27 04:30", 410, PlanKind.SLEEP, reason=day.MISSED_REASON)  # as the keeper wrote it
    gone = plan(3, "2026-11-27 03:00", 120, PlanKind.TOGETHER, state=PlanState.MISSED)
    found = day.due(at("2026-11-27 04:30"), [pulled, NEXT, gone, own], None, DayState(1), SEOUL, running=False)
    assert steps(found) == [(Step.FALL, FellBy.MISSED)]
    wide = plan(1, "2026-11-27 06:00", 300, PlanKind.SLEEP, origin=LifeOrigin.HIMARI)  # 06:00 is no night start
    gap = plan(1, "2026-11-27 04:00", 360, PlanKind.SLEEP, origin=LifeOrigin.HIMARI)  # an hour after the promise
    short = plan(3, "2026-11-27 02:00", 60, PlanKind.TOGETHER, origin=LifeOrigin.HIMARI)
    assert day.due(at("2026-11-27 02:30"), [gap, NEXT, short], None, DayState(1), SEOUL,
                   running=False)[0].pull is None  # put off past it by more than PUSH_SLACK: its own reason
    assert wide.time_of_day == "06:00"


def test_the_handoff_keeps_the_last_words_without_thoughts_and_the_nights_promises() -> None:
    conv = ConversationId("conv_9")

    def event(sequence: int, when: str, payload: object, kind: EventKind) -> ConversationEvent:
        return ConversationEvent(EventId(f"e{sequence}"), conv, sequence, kind, payload, 1,  # type: ignore[arg-type]
                                 at(when).astimezone(UTC))

    events = [event(1, "2026-11-26 22:40", UserMessage((TextBlock("새벽 3시에 방송 같이 보자"),)), EventKind.USER_MESSAGE),
              event(2, "2026-11-26 22:41", AssistantFinal((TextBlock("[[think]]좋다[[/think]][[mood:happy]]좋아!"),)),
                    EventKind.ASSISTANT_FINAL),
              event(3, "2026-11-26 22:50", UserMessage((TextBlock("x" * 2000),)), EventKind.USER_MESSAGE)]
    words = day.last_words(events, SEOUL)
    assert [(w.at, w.who) for w in words] == [("22:50", "user")] and len(words[0].text) == 1500
    words = day.last_words(events[:2], SEOUL)
    assert [(w.at, w.who, w.text) for w in words] == [("22:40", "user", "새벽 3시에 방송 같이 보자"),
                                                       ("22:41", "himari", "좋아!")]
    moved = plan(1, "2026-11-27 05:00", 380, PlanKind.SLEEP, origin=LifeOrigin.HIMARI, reason="방송 보고 잘래")
    promises = [plan(3, "2026-11-27 03:00", 120, PlanKind.TOGETHER, text="방송"),
                plan(4, "2026-11-26 23:00", 30, PlanKind.TOGETHER, state=PlanState.MISSED, text="통화")]
    sleep = asleep("2026-11-27 05:00")
    before = day.day_before("2026-11-26", events[:2], [moved, *promises], sleep, at("2026-11-26 08:00"),
                            at("2026-11-27 08:00"), SEOUL, conv)
    assert (before.kept, before.missed, before.delayed, before.how_fell, before.conversation_id) == (
        ("03:00 방송",), ("23:00 통화",), "방송 보고 잘래", FellBy.SCHEDULE, conv)
    night = [SleepRecord(9, 1, at("2026-11-27 07:00"), FellBy.SCHEDULE, at("2026-11-27 11:20"), WokeBy.SCHEDULE),
             SleepRecord(8, 1, at("2026-11-27 05:00"), FellBy.SCHEDULE, at("2026-11-27 06:30"), WokeBy.USER)]
    told = day.told_before(before, night)
    assert (told.woken, told.woke_at) == (1, at("2026-11-27 11:20"))
    assert isinstance(told, DayBefore)


def test_a_session_is_titled_by_its_date_and_weekday() -> None:
    assert day.session_title(date(2026, 11, 27)) == "11월 27일 (금)"
    assert plan_span(SLEEP, SEOUL) == (at("2026-11-27 00:30"), at("2026-11-27 08:10"))
    assert day.tonight([SLEEP, NEXT], at("2026-11-27 09:00"), SEOUL) == NEXT
