"""HIMARI's day (character-day v0.1 §3, §5, §6, §18.3): the pure rules of its sleep and schedule. No I/O, no clock,
no randomness of its own — every function takes the time and the random source it needs.

- ``place_sleep``: a night's sleep slot when none is there (§5.2): its start from 23:00 to 02:00 in ten minutes, its
  end from 07:00 to 10:00 but at least five hours on and by 12:00, around the slots already that night.
- ``sleep_view``: what a turn and the Web see of the sleep now (§5.1): awake, drowsy from DROWSY_MINUTES before the
  slot, asleep (deep for DEEP_HOURS) — half asleep is only a mumble's turn, which the Driver knows.
- ``answer_message``: how a message (or a poke) meets a sleeping HIMARI (§5.5): deep sleep — nothing, a mumble, awake;
  light sleep — a mumble, awake; quiet WAKE_RESET and the count starts again.
- ``due``: what the day's keeper does now (§5.2, §5.4, §6): place the nights' slots, mark a promise missed (and sleep
  waiting for it), fall asleep (the slot came, go_to_sleep was asked, or it dozes off again after being woken), wake
  at the slot's end, close the day after CLOSING_SLEEP of unbroken sleep (CLOSING_SHORT past CLOSING_SAFE).
- ``day_before``: what the closed day session hands the next (§3.3).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from enum import Enum
from random import Random

from himari.common import TextBlock, split_marks
from himari.data.api import (
    LAST_WORDS_CHARS,
    MAX_SLEEP_MINUTES,
    MIN_SLEEP_MINUTES,
    AssistantFinal,
    AsleepAnswer,
    ConversationEvent,
    DayBefore,
    DayLine,
    DayState,
    FellBy,
    LifeEntry,
    LifeOrigin,
    PlanKind,
    PlanState,
    SleepRecord,
    SleepState,
    SleepView,
    UserMessage,
    WokeBy,
    night_of,
    plan_span,
)

SLEEP_STARTS = (time(23, 0), time(2, 0))  # §5.2: a placed sleep slot starts in this range, in ten minutes
SLEEP_ENDS = (time(7, 0), time(10, 0))  # and ends in this one (at least MIN_SLEEP_MINUTES on, by 12:00)
LATEST_END = time(12, 0)
STEP_MINUTES = 10
DROWSY_MINUTES = 30  # §5.1: drowsy this long before the sleep slot
DEEP_HOURS = 3  # §5.1: deep sleep this long after it fell asleep (again from the start after being woken)
WAKE_RESET = timedelta(minutes=10)  # §5.5: quiet this long, the wake count starts again
DOZE_MINUTES = 20  # §5.2: woken in its slot, it sleeps again after this long without a word
UP_NEAR_END = timedelta(hours=1)  # §5.2: woken within this of the slot's end, it is up for the day
MISSED_AFTER = timedelta(minutes=30)  # §5.4: a promise with no word from the user this long after it starts
MISSED_LOOKBACK = timedelta(hours=12)  # a promise older than this is left as it is (the server was off)
REQUEST_KEPT = timedelta(hours=2)  # go_to_sleep asked longer ago than this (and not slept) is let go
CLOSING_SLEEP = timedelta(hours=3)  # §6: unbroken sleep this long closes the day
CLOSING_SAFE = timedelta(hours=36)  # past this since the last closing ...
CLOSING_SHORT = timedelta(hours=1)  # ... an hour of it does
WEEKDAYS_KO = "월화수목금토일"


def session_title(day: date) -> str:
    """§3.1: a day session's title, the date it was opened: "11월 27일 (금)"."""
    return f"{day.month}월 {day.day}일 ({WEEKDAYS_KO[day.weekday()]})"


def current_night(now: datetime) -> date:
    """§4.4: the night ``now`` (its zone) is in, or the one ahead in the day (today's)."""
    return night_of(now) or now.date()


# --- placing a sleep slot (§5.2) ---------------------------------------------------------------


@dataclass(frozen=True)
class PlacedSleep:
    """A sleep slot to place: its start's date and time (HH:MM) and its length."""

    day: str
    time_of_day: str
    minutes: int


def _steps(first: datetime, last: datetime) -> list[datetime]:
    out, at = [], first
    while at <= last:
        out.append(at)
        at += timedelta(minutes=STEP_MINUTES)
    return out


def place_sleep(night: date, rng: Random, zone: tzinfo,
                busy: Sequence[tuple[datetime, datetime]] = ()) -> PlacedSleep:
    """§5.2: the night's sleep slot — a start drawn from 23:00 to 02:00 and an end from 07:00 to 10:00 (ten-minute
    steps), the end at least MIN_SLEEP_MINUTES after the start and by 12:00. ``busy``: the night's other slots; a
    start that would cover one moves after it (to 05:00 at the latest), the end with it."""
    morning = night + timedelta(days=1)
    start = rng.choice(_steps(datetime.combine(night, SLEEP_STARTS[0], tzinfo=zone),
                              datetime.combine(morning, SLEEP_STARTS[1], tzinfo=zone)))
    end = rng.choice(_steps(datetime.combine(morning, SLEEP_ENDS[0], tzinfo=zone),
                            datetime.combine(morning, SLEEP_ENDS[1], tzinfo=zone)))
    latest_start = datetime.combine(morning, time(5, 0), tzinfo=zone)
    least = timedelta(minutes=MIN_SLEEP_MINUTES)
    for _ in range(len(busy) + 1):
        covered = [b for b in busy if b[0] < max(end, start + least) and start < b[1]]
        if not covered:
            break
        first = min(covered)
        if first[0] >= start + least:  # in the morning: it gets up before it
            end = first[0]
        else:  # in the night: it sleeps after it
            start = min(_ceil(max(b[1] for b in covered if b[0] < start + least)), latest_start)
    end = min(max(end, start + timedelta(minutes=MIN_SLEEP_MINUTES)),
              datetime.combine(morning, LATEST_END, tzinfo=zone), start + timedelta(minutes=MAX_SLEEP_MINUTES))
    return PlacedSleep(start.date().isoformat(), start.strftime("%H:%M"), int((end - start).total_seconds() // 60))


def _ceil(moment: datetime) -> datetime:
    moment = moment.replace(second=0, microsecond=0)
    return moment + timedelta(minutes=(-moment.minute) % STEP_MINUTES)


# --- the sleep as seen now (§5.1) ----------------------------------------------------------------


def sleep_slots(plans: Sequence[LifeEntry], zone: tzinfo) -> list[tuple[datetime, datetime, LifeEntry]]:
    """The live sleep slots among ``plans`` with their spans in ``zone``, earliest first."""
    found = [(s[0], s[1], e) for e in plans if e.sleep_slot and (s := plan_span(e, zone)) is not None]
    return sorted(found, key=lambda f: (f[0], f[2].entry_id))


def tonight(plans: Sequence[LifeEntry], now: datetime, zone: tzinfo) -> LifeEntry | None:
    """§8: the sleep slot a turn speaks of — the first live one that has not ended at ``now``."""
    return next((e for start, end, e in sleep_slots(plans, zone) if end > now), None)


def slot_of_night(plans: Sequence[LifeEntry], night: date, zone: tzinfo) -> LifeEntry | None:
    """The live sleep slot of ``night``, if any."""
    return next((e for start, end, e in sleep_slots(plans, zone) if night_of(start.astimezone(zone)) == night), None)


def deep(sleep: SleepRecord, now: datetime) -> bool:
    """§5.1: deep within DEEP_HOURS after it fell asleep (a mumble does not change it)."""
    return now - sleep.fell_at < timedelta(hours=DEEP_HOURS)


def sleep_view(now: datetime, slot: LifeEntry | None, sleep: SleepRecord | None, state: DayState,
               zone: tzinfo) -> SleepView:
    """§5.1, §8: asleep while a sleep is open (deep or light); else drowsy from DROWSY_MINUTES before ``slot`` (the
    one ahead) starts; else awake — and awake inside the slot when the user woke it (``woken_at``: just then)."""
    if sleep is not None and sleep.asleep:
        return SleepView(SleepState.ASLEEP, deep(sleep, now), tonight=slot, last=sleep)
    span = None if slot is None else plan_span(slot, zone)
    woken = None
    if sleep is not None and sleep.woke_at is not None and state.woken_at is not None \
            and sleep.woke_at == state.woken_at and now - sleep.woke_at < UP_NEAR_END:
        woken = sleep.woke_at
    if span is not None and span[0] - timedelta(minutes=DROWSY_MINUTES) <= now < span[0]:
        left = math.ceil((span[0] - now).total_seconds() / 60)
        return SleepView(SleepState.DROWSY, minutes_to_sleep=left, tonight=slot, last=sleep, woken_at=woken)
    return SleepView(SleepState.AWAKE, tonight=slot, last=sleep, woken_at=woken)


def next_count(sleep: SleepRecord, now: datetime) -> int:
    """§5.5: the wake count after one more message or poke — the stored one unless WAKE_RESET has passed since."""
    count = sleep.count if sleep.count_at is not None and now - sleep.count_at < WAKE_RESET else 0
    return count + 1


def answer_message(view: SleepView, sleep: SleepRecord | None, now: datetime) -> AsleepAnswer | None:
    """§5.5: None while awake (or drowsy); asleep, by the count (``next_count``): deep sleep 1 nothing, 2 a mumble, 3
    awake; light sleep 1 a mumble, 2 awake."""
    if view.state is not SleepState.ASLEEP or sleep is None or not sleep.asleep:
        return None
    count = next_count(sleep, now)
    if deep(sleep, now):
        return AsleepAnswer.NONE if count == 1 else AsleepAnswer.MUMBLE if count == 2 else AsleepAnswer.WAKE
    return AsleepAnswer.MUMBLE if count == 1 else AsleepAnswer.WAKE


def up_for_the_day(slot: LifeEntry | None, now: datetime, zone: tzinfo) -> bool:
    """§5.2: woken within UP_NEAR_END of its slot's end, it is up (the slot's end moves to now)."""
    span = None if slot is None else plan_span(slot, zone)
    return span is not None and span[0] <= now < span[1] and span[1] - now < UP_NEAR_END


# --- what the keeper does now (§5.2, §5.4, §6) ---------------------------------------------------


class Step(Enum):
    PLACE = "place"  # a night has no sleep slot: place one (``night``)
    MISS = "miss"  # a promise the user never came to (``entry``); ``pull``: the night's sleep starts now
    FALL = "fall"  # it falls asleep (``how``; ``entry``: the slot it sleeps in)
    WAKE = "wake"  # its slot ended: it wakes (``sleep``)
    CLOSE = "close"  # the day is closed in this sleep (``sleep``)


@dataclass(frozen=True)
class Transition:
    step: Step
    night: date | None = None
    entry: LifeEntry | None = None
    how: FellBy | None = None
    sleep: SleepRecord | None = None
    pull: LifeEntry | None = None


def closing_after(state: DayState, now: datetime) -> timedelta:
    """§6: unbroken sleep this long closes the day — CLOSING_SLEEP, or CLOSING_SHORT past CLOSING_SAFE since the
    last closing."""
    late = state.last_closing_at is not None and now - state.last_closing_at > CLOSING_SAFE
    return CLOSING_SHORT if late else CLOSING_SLEEP


def closes(sleep: SleepRecord | None, state: DayState, now: datetime) -> bool:
    """§6: the day closes in ``sleep`` — begun after the last closing, not closed in yet, unbroken long enough (to
    now, or to when it ended: a mumble breaks nothing, a wake ends the sleep)."""
    if sleep is None or sleep.closed_day:
        return False
    if state.last_closing_at is not None and sleep.fell_at <= state.last_closing_at:
        return False
    return (sleep.woke_at or now) - sleep.fell_at >= closing_after(state, now)


def due(now: datetime, plans: Sequence[LifeEntry], sleep: SleepRecord | None, state: DayState, zone: tzinfo, *,
        running: bool, last_user_at: datetime | None = None,
        last_activity_at: datetime | None = None) -> list[Transition]:
    """§5.2, §5.4, §6, §18.3: what the keeper does at ``now`` (in ``zone``), in order. ``plans``: the persona's
    schedule (live or not); ``sleep``: its latest; ``running``: a job of the day session is under way (it never
    falls asleep mid-answer); ``last_user_at``: the user's last message; ``last_activity_at``: the day session's
    last word (after being woken, it dozes off once DOZE_MINUTES pass without one)."""
    local = now.astimezone(zone)
    out: list[Transition] = []
    night = current_night(local)
    for wanted in (night, night + timedelta(days=1)):  # §4.2: tonight's and the next night's
        if slot_of_night(plans, wanted, zone) is None:
            out.append(Transition(Step.PLACE, night=wanted))
    if sleep is not None and sleep.asleep:
        if closes(sleep, state, now):
            out.append(Transition(Step.CLOSE, sleep=sleep))
        slot = slot_of_night(plans, night_of(sleep.fell_at.astimezone(zone)) or local.date(), zone)
        span = None if slot is None else plan_span(slot, zone)
        ends = span[1] if span is not None else sleep.fell_at + timedelta(minutes=(MIN_SLEEP_MINUTES
                                                                                   + MAX_SLEEP_MINUTES) // 2)
        if now >= ends:
            out.append(Transition(Step.WAKE, sleep=sleep))
        return out
    if closes(sleep, state, now):  # the server was off when it would have: the record says it came
        out.append(Transition(Step.CLOSE, sleep=sleep))
    slot = tonight(plans, now, zone)
    span = None if slot is None else plan_span(slot, zone)
    falls: Transition | None = None
    for entry in sorted((e for e in plans if e.live_plan and e.plan_kind is PlanKind.TOGETHER),
                        key=lambda e: (e.day or "", e.time_of_day or "", e.entry_id)):
        together = plan_span(entry, zone)
        if together is None or not (now - MISSED_LOOKBACK <= together[0] and together[0] + MISSED_AFTER <= now):
            continue
        if last_user_at is not None and last_user_at >= together[0]:
            continue
        # §5.4: the night's sleep was put off for it — it falls asleep waiting (now within the night's start)
        pull = (slot if span is not None and span[0] > together[0] and now < span[0]
                and night_of(local) is not None else None)
        out.append(Transition(Step.MISS, entry=entry, pull=pull))
        if pull is not None and falls is None:
            falls = Transition(Step.FALL, entry=pull, how=FellBy.MISSED)
    if running:
        return out
    if falls is not None:
        out.append(falls)
        return out
    asked = state.sleep_requested_at
    if asked is not None and now - asked < REQUEST_KEPT and (sleep is None or asked > sleep.fell_at):
        out.append(Transition(Step.FALL, entry=slot, how=FellBy.TOOL))
        return out
    if span is not None and span[0] <= now < span[1]:
        woken = state.woken_at is not None and state.woken_at >= span[0]
        quiet_since = max((t for t in (state.woken_at, last_activity_at) if t is not None), default=None)
        if not woken or quiet_since is None or now - quiet_since >= timedelta(minutes=DOZE_MINUTES):
            out.append(Transition(Step.FALL, entry=slot, how=FellBy.SCHEDULE))
    return out


# --- the handoff (§3.3) ---------------------------------------------------------------------


def _said(blocks: Sequence[object]) -> str:
    text = "".join(b.text for b in blocks if isinstance(b, TextBlock))
    return " ".join(split_marks(text, complete=True)[0].split())  # no thought, no marks, one line


def last_words(events: Sequence[ConversationEvent], zone: tzinfo,
               limit: int = LAST_WORDS_CHARS) -> tuple[DayLine, ...]:
    """§3.3: the user's and HIMARI's last words (answers' visible text, no thoughts), newest kept within ``limit``
    together (one that does not fit whole is cut at its start), oldest first, each with its time."""
    kept: list[DayLine] = []
    room = limit
    for event in sorted(events, key=lambda e: e.sequence, reverse=True):
        payload = event.payload
        if isinstance(payload, UserMessage):
            who, text = "user", _said(payload.blocks)
        elif isinstance(payload, AssistantFinal):
            who, text = "himari", _said(payload.blocks)
        else:
            continue
        if not text:
            continue
        if room <= 1:
            break
        if len(text) > room:
            text = "…" + text[len(text) - room + 1:]
        kept.append(DayLine(event.created_at.astimezone(zone).strftime("%H:%M"), who, text))
        room -= len(text)
    return tuple(reversed(kept))


def day_before(day: str, events: Sequence[ConversationEvent], plans: Sequence[LifeEntry], sleep: SleepRecord,
               since: datetime, now: datetime, zone: tzinfo) -> DayBefore:
    """§3.3, §6 step 1: what the closed session (its date ``day``, its ``events``) hands the next — its last words,
    the promises of its time (from ``since``) kept and missed, how HIMARI fell asleep in ``sleep`` and why its sleep
    slot was put off, when it was (HIMARI's reason)."""
    kept: list[str] = []
    missed: list[str] = []
    for entry in sorted(plans, key=lambda e: (e.day or "", e.time_of_day or "", e.entry_id)):
        span = plan_span(entry, zone)
        if entry.plan_kind is not PlanKind.TOGETHER or span is None or not since <= span[0] <= now:
            continue
        if entry.plan_state is PlanState.MISSED:
            missed.append(f"{entry.time_of_day} {entry.text}")
        elif entry.plan_state is None:
            kept.append(f"{entry.time_of_day} {entry.text}")
    slot = slot_of_night(plans, night_of(sleep.fell_at.astimezone(zone)) or sleep.fell_at.astimezone(zone).date(),
                         zone)
    delayed = slot.reason if slot is not None and slot.origin is LifeOrigin.HIMARI and slot.reason else None
    return DayBefore(day, last_words(events, zone), tuple(kept), tuple(missed), sleep.fell_at, sleep.how_fell,
                     delayed, sleep_id=sleep.sleep_id)


def told_before(before: DayBefore, sleeps: Sequence[SleepRecord]) -> DayBefore:
    """§3.3: the handoff as a turn is told it — with how often the user woke HIMARI that night and when it got up
    (``sleeps``: the latest, newest first, from the closing sleep on)."""
    night = [s for s in sleeps if before.fell_at is not None and s.fell_at >= before.fell_at]
    woken = sum(1 for s in night if s.how_woke is WokeBy.USER)
    ended = [s.woke_at for s in night if s.woke_at is not None]
    return DayBefore(before.day, before.last_words, before.kept, before.missed, before.fell_at, before.how_fell,
                     before.delayed, woken, max(ended, default=None), before.sleep_id)
