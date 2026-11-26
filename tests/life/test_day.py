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
           how_woke: WokeBy | None = None, closed: bool = False, how: FellBy = FellBy.SCHEDULE) -> SleepRecord:
    return SleepRecord(7, 1, at(fell), how, None if woke is None else at(woke), how_woke, count=count,
                       count_at=None if count_at is None else at(count_at), closed_day=closed)


SLEEP = plan(1, "2026-11-27 00:30", 460, PlanKind.SLEEP)  # 00:30-08:10, the night of 11-26
NEXT = plan(2, "2026-11-28 00:00", 480, PlanKind.SLEEP)  # the night of 11-27
AFTER = plan(5, "2026-11-28 23:30", 480, PlanKind.SLEEP)  # the night of 11-28


@pytest.mark.parametrize("seed", range(40))
def test_a_placed_sleep_slot_starts_23_to_02_and_ends_07_to_10_at_least_five_hours_on(seed: int) -> None:
    placed = day.place_sleep(date(2026, 11, 26), Random(seed), SEOUL)
    start = at(f"{placed.day} {placed.time_of_day}")
    end = start + timedelta(minutes=placed.minutes)
    assert at("2026-11-26 23:00") <= start <= at("2026-11-27 02:00") and start.minute % 10 == 0
    assert 300 <= placed.minutes <= 660 and end <= at("2026-11-27 12:00")
    assert at("2026-11-27 07:00") <= end or placed.minutes == 300


def test_a_placed_sleep_slot_goes_around_the_nights_promises() -> None:
    late = (at("2026-11-27 02:30"), at("2026-11-27 04:20"))  # a promise in the night: it sleeps after it
    placed = day.place_sleep(date(2026, 11, 26), Random(1), SEOUL, [late])
    assert (placed.day, placed.time_of_day) == ("2026-11-27", "04:20") and placed.minutes >= 300
    morning = (at("2026-11-27 08:00"), at("2026-11-27 09:00"))  # in the morning: it gets up before it
    for seed in range(20):
        placed = day.place_sleep(date(2026, 11, 26), Random(seed), SEOUL, [morning])
        end = at(f"{placed.day} {placed.time_of_day}") + timedelta(minutes=placed.minutes)
        assert end <= morning[0] or placed.minutes == 300


def test_drowsy_thirty_minutes_before_the_slot_asleep_deep_three_hours_then_light() -> None:
    state = DayState(1)
    view = day.sleep_view(at("2026-11-27 00:05"), SLEEP, None, state, SEOUL)
    assert (view.state, view.minutes_to_sleep) == (SleepState.DROWSY, 25)
    assert day.sleep_view(at("2026-11-26 23:59"), SLEEP, None, state, SEOUL).state is SleepState.AWAKE
    assert day.sleep_view(at("2026-11-27 01:00"), SLEEP, None, state, SEOUL).state is SleepState.AWAKE  # woken
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
    asked = DayState(1, sleep_requested_at=at("2026-11-26 22:03"))
    now = at("2026-11-26 22:04")
    assert day.due(now, [SLEEP, NEXT], None, asked, SEOUL, running=True) == []
    assert steps(day.due(now, [SLEEP, NEXT], None, asked, SEOUL, running=False)) == [(Step.FALL, FellBy.TOOL)]
    slept = asleep("2026-11-26 22:04", woke="2026-11-27 07:00", how_woke=WokeBy.SCHEDULE, closed=True)
    assert day.due(at("2026-11-26 23:00"), [SLEEP, NEXT], slept, asked, SEOUL, running=False) == []


def test_it_wakes_at_its_slots_end_and_closes_the_day_after_three_unbroken_hours() -> None:
    sleep = asleep("2026-11-27 00:30")
    state = DayState(1, last_closing_at=at("2026-11-26 03:00"))
    assert day.due(at("2026-11-27 03:29"), [SLEEP, NEXT], sleep, state, SEOUL, running=False) == []
    assert steps(day.due(at("2026-11-27 03:30"), [SLEEP, NEXT], sleep, state, SEOUL, running=False)) == [
        (Step.CLOSE, None)]
    closed = SleepRecord(7, 1, sleep.fell_at, FellBy.SCHEDULE, closed_day=True)
    assert steps(day.due(at("2026-11-27 08:10"), [SLEEP, NEXT, AFTER], closed, state, SEOUL, running=False)) == [
        (Step.WAKE, None)]
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
    assert day.due(at("2026-11-27 03:30"), plans, None, DayState(1), SEOUL, running=False,
                   last_user_at=at("2026-11-27 03:10")) == []  # the user came
    afternoon = plan(4, "2026-11-27 15:00", 60, PlanKind.TOGETHER)  # missed, but no sleep is pulled by day
    found = day.due(at("2026-11-27 15:30"), [moved, NEXT, AFTER, afternoon], None, DayState(1), SEOUL, running=False)
    assert steps(found) == [(Step.MISS, 4)] and found[0].pull is None


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
                            at("2026-11-27 08:00"), SEOUL)
    assert (before.kept, before.missed, before.delayed, before.how_fell) == (
        ("03:00 방송",), ("23:00 통화",), "방송 보고 잘래", FellBy.SCHEDULE)
    night = [SleepRecord(9, 1, at("2026-11-27 07:00"), FellBy.SCHEDULE, at("2026-11-27 11:20"), WokeBy.SCHEDULE),
             SleepRecord(8, 1, at("2026-11-27 05:00"), FellBy.SCHEDULE, at("2026-11-27 06:30"), WokeBy.USER)]
    told = day.told_before(before, night)
    assert (told.woken, told.woke_at) == (1, at("2026-11-27 11:20"))
    assert isinstance(told, DayBefore)


def test_a_session_is_titled_by_its_date_and_weekday() -> None:
    assert day.session_title(date(2026, 11, 27)) == "11월 27일 (금)"
    assert plan_span(SLEEP, SEOUL) == (at("2026-11-27 00:30"), at("2026-11-27 08:10"))
    assert day.tonight([SLEEP, NEXT], at("2026-11-27 09:00"), SEOUL) == NEXT
