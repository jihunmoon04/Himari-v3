"""HIMARI's day (character-day v0.1 §3, §5, §6, §18.3): the pure rules of its sleep and schedule. No I/O, no clock,
no randomness of its own — every function takes the time and the random source it needs.

- ``place_sleep``: a night's sleep slot when none is there (§5.2): its start from 23:00 to 02:00 in ten minutes, its
  end from 07:00 to 10:00 but at least five hours on and by 12:00, never over the slots already that night.
- ``place_diary``: the night's diary slot beside its sleep slot (character-diary §3): from 21:00 to DIARY_BEFORE
  before the sleep in ten minutes, never already past; none when no room is left before the sleep.
- ``sleep_view``: what a turn and the Web see of the sleep now (§5.1): awake, drowsy from DROWSY_MINUTES before the
  slot, asleep (deep for DEEP_HOURS) — half asleep is only a mumble's turn, which the Driver knows.
- ``answer_message``: how a message (or a poke) meets a sleeping HIMARI (§5.5): deep sleep — nothing, a mumble, awake;
  light sleep — a mumble, awake; quiet WAKE_RESET and the count starts again.
- ``due``: what the day's keeper does now (§5.2, §5.4, §6): place the nights' slots, mark a promise missed (and sleep
  waiting for it, when the sleep was put off for it), fall asleep (the slot came, go_to_sleep was asked, or it dozes
  off again after being woken), wake at the end of the slot it sleeps in, close the day after CLOSING_SLEEP of
  unbroken sleep (CLOSING_SHORT past CLOSING_SAFE) — once no job of the day session runs.
- ``day_before``: what the closed day session hands the next (§3.3).
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from enum import Enum
from random import Random

from himari.common import ConversationId, TextBlock, split_marks
from himari.data.api import (
    DIARY_EARLIEST,
    DIARY_MINUTES,
    LAST_WORDS_CHARS,
    MAX_SLEEP_MINUTES,
    MIN_SLEEP_MINUTES,
    NIGHT_ENDS,
    NIGHT_STARTS,
    SLEEP_ENDS_FROM,
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
    diary_night,
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
NO_SLOT_SLEEP = timedelta(minutes=(MIN_SLEEP_MINUTES + MAX_SLEEP_MINUTES) // 2)  # a sleep with no slot wakes after this
PUSH_SLACK = timedelta(minutes=30)  # §5.4: a sleep slot starting this soon after a promise (and what runs on from it)
MISSED_REASON = "약속을 기다리다 잠듦"  # §5.4: why the sleep slot was pulled forward (it falls asleep waiting)
WEEKDAYS_KO = "월화수목금토일"
DIARY_FROM = time(21, 0)  # character-diary §3: a placed diary slot starts from 21:00 ...
DIARY_BEFORE = timedelta(minutes=40)  # ... to this before the night's sleep slot, in ten minutes


def session_title(day: date) -> str:
    """§3.1: a day session's title, the date it was opened: "11월 27일 (금)"."""
    return f"{day.month}월 {day.day}일 ({WEEKDAYS_KO[day.weekday()]})"


def current_night(now: datetime) -> date:
    """§4.4: the night ``now`` (its zone) is in, or the one ahead in the day (today's)."""
    return night_of(now) or now.date()


# --- placing a sleep slot (§5.2) ---------------------------------------------------------------


@dataclass(frozen=True)
class PlacedSlot:
    """A slot to place (a sleep or a diary slot): its start's date and time (HH:MM) and its length."""

    day: str
    time_of_day: str
    minutes: int


PlacedSleep = PlacedSlot


def _steps(first: datetime, last: datetime) -> list[datetime]:
    out, at = [], first
    while at <= last:
        out.append(at)
        at += timedelta(minutes=STEP_MINUTES)
    return out


def place_sleep(night: date, rng: Random, zone: tzinfo,
                busy: Sequence[tuple[datetime, datetime]] = (), *,
                starts: tuple[time, time] = SLEEP_STARTS, ends: tuple[time, time] = SLEEP_ENDS) -> PlacedSleep | None:
    """§5.2, §4.4: the night's sleep slot — a start drawn from 23:00 to 02:00 and an end from 07:00 to 10:00 (ten-
    minute steps), the end at least MIN_SLEEP_MINUTES after the start and by 12:00. ``busy``: the night's other
    slots, which it never covers: a start that would is pushed after them (in ten minutes, to 05:00 at the latest),
    the end comes before the next one; with no room later, the latest start back from the drawn one (to 21:00).
    When no span within the limits fits (a slot over 05:00 leaves none), the same search with the end allowed before
    05:00 (it gets up for that slot); None: not even that (the night stays without a slot until room is made).
    character-routine §13.2: ``starts`` and ``ends`` are the sleep routine's ranges (a start from 21:00 is on the
    night's date, one before 05:00 on the morning's)."""
    morning = night + timedelta(days=1)

    def on_night(at: time) -> datetime:
        return datetime.combine(night if at >= NIGHT_STARTS else morning, at, tzinfo=zone)

    drawn = rng.choice(_steps(on_night(starts[0]), on_night(starts[1])) or [on_night(starts[0])])
    wanted_end = rng.choice(_steps(datetime.combine(morning, ends[0], tzinfo=zone),
                                   datetime.combine(morning, ends[1], tzinfo=zone))
                            or [datetime.combine(morning, ends[0], tzinfo=zone)])
    least = timedelta(minutes=MIN_SLEEP_MINUTES)
    by = datetime.combine(morning, LATEST_END, tzinfo=zone)
    ends_from = datetime.combine(morning, SLEEP_ENDS_FROM, tzinfo=zone)

    def fit(start: datetime, strict: bool) -> datetime | None:
        if any(b[0] < start + least and start < b[1] for b in busy):
            return None
        cap = min([by, start + timedelta(minutes=MAX_SLEEP_MINUTES)] + [b[0] for b in busy if b[1] > start])
        end = min(max(wanted_end, start + least), cap)
        return end if end >= start + least and (end >= ends_from or not strict) else None

    later = _steps(drawn, datetime.combine(morning, NIGHT_ENDS, tzinfo=zone))
    earlier = list(reversed(_steps(datetime.combine(night, NIGHT_STARTS, tzinfo=zone), drawn)))[1:]
    for strict in (True, False):
        for start in later + earlier:
            end = fit(start, strict)
            if end is not None:
                return PlacedSleep(start.date().isoformat(), start.strftime("%H:%M"),
                                   int((end - start).total_seconds() // 60))
    return None


def _ceil(moment: datetime) -> datetime:
    moment = moment.replace(second=0, microsecond=0)
    return moment + timedelta(minutes=(-moment.minute) % STEP_MINUTES)


def _ceil_up(moment: datetime) -> datetime:
    """``moment`` rounded up to the next ten minutes (a moment past a whole minute counts as the next one)."""
    whole = moment.replace(second=0, microsecond=0)
    if whole < moment:
        whole += timedelta(minutes=1)
    return _ceil(whole)


# --- placing a diary slot (character-diary §3) ----------------------------------------------------


def place_diary(night: date, sleep_start: datetime, rng: Random, zone: tzinfo, now: datetime, *,
                start_from: time = DIARY_FROM, minutes: tuple[int, int] = (DIARY_MINUTES, DIARY_MINUTES)
                ) -> PlacedSlot | None:
    """character-diary §3, §16.3: the night's diary slot, DIARY_MINUTES long, beside its sleep slot starting at
    ``sleep_start`` — a start drawn from 21:00 to DIARY_BEFORE before the sleep (ten-minute steps); when that range
    is empty (the sleep starts before 21:40), DIARY_BEFORE before the sleep but never before DIARY_EARLIEST. Never
    already past: a start before ``now`` moves to ``now`` rounded up to ten minutes. None when it would then end
    after the sleep starts (the diary is written as HIMARI falls asleep). character-routine §13.2: ``start_from`` and
    ``minutes`` (a length drawn in ten minutes) are the diary routine's."""
    sleep_start = sleep_start.astimezone(zone)
    soonest = _ceil_up(now.astimezone(zone))
    length = rng.choice(_lengths(*minutes))
    first = datetime.combine(night, start_from, tzinfo=zone)
    last = sleep_start - DIARY_BEFORE
    choices = [s for s in _steps(first, last) if s >= soonest]
    if choices:
        start = rng.choice(choices)
    else:
        start = max(min(last, first), datetime.combine(night, DIARY_EARLIEST, tzinfo=zone), soonest)
    if start + timedelta(minutes=length) > sleep_start:
        return None
    return PlacedSlot(start.date().isoformat(), start.strftime("%H:%M"), length)


def _lengths(low: int, high: int) -> list[int]:
    """Lengths from ``low`` to ``high`` minutes in ten-minute steps (``low`` itself first; ``high`` when it is off)."""
    out = list(range(low, high + 1, STEP_MINUTES))
    return out if out[-1] == high else [*out, high]


def place_routine(day: date, start_range: tuple[time, time], minutes: tuple[int, int], rng: Random, zone: tzinfo,
                  now: datetime, sleeps: Sequence[tuple[datetime, datetime]]) -> PlacedSlot | None:
    """character-routine §13.2: a routine's slot on ``day`` — a start drawn in ten minutes from its range (today:
    only from now on) and a length from its range, never over a sleep slot (``sleeps``); None when no start fits."""
    length = rng.choice(_lengths(*minutes))
    soonest = _ceil_up(now.astimezone(zone))
    starts = [s for s in _steps(datetime.combine(day, start_range[0], tzinfo=zone),
                                datetime.combine(day, start_range[1], tzinfo=zone)) if s >= soonest]
    rng.shuffle(starts)
    for start in starts:
        end = start + timedelta(minutes=length)
        if not any(b[0] < end and start < b[1] for b in sleeps):
            return PlacedSlot(start.date().isoformat(), start.strftime("%H:%M"), length)
    return None


def diary_of_night(plans: Sequence[LifeEntry], night: date, zone: tzinfo) -> LifeEntry | None:
    """character-diary §3: the live diary slot of ``night``, if any."""
    found = sorted(((s[0], e) for e in plans if e.diary_slot and (s := plan_span(e, zone)) is not None
                    and diary_night(s[0].astimezone(zone)) == night), key=lambda f: (f[0], f[1].entry_id))
    return found[0][1] if found else None


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


def slot_of(plans: Sequence[LifeEntry], sleep: SleepRecord) -> LifeEntry | None:
    """§5.1: the sleep slot ``sleep`` sleeps in — by the identity kept with it (never the night of its fall time: a
    doze at 06:20 or a fall at 05:00:30 is still in that night's slot); None with none (or no longer live)."""
    return next((e for e in plans if sleep.slot_id is not None and e.entry_id == sleep.slot_id and e.sleep_slot),
                None)


def wakes_at(plans: Sequence[LifeEntry], sleep: SleepRecord, zone: tzinfo) -> datetime:
    """§5.2: when ``sleep`` is due to end — its slot's end (never before it fell asleep); with no slot,
    NO_SLOT_SLEEP after it fell asleep."""
    slot = slot_of(plans, sleep)
    span = None if slot is None else plan_span(slot, zone)
    return max(span[1], sleep.fell_at) if span is not None else sleep.fell_at + NO_SLOT_SLEEP


def deep(sleep: SleepRecord, now: datetime) -> bool:
    """§5.1: deep within DEEP_HOURS after it fell asleep (a mumble does not change it)."""
    return now - sleep.fell_at < timedelta(hours=DEEP_HOURS)


def sleep_view(now: datetime, slot: LifeEntry | None, sleep: SleepRecord | None, state: DayState,
               zone: tzinfo) -> SleepView:
    """§5.1, §8: asleep while a sleep is open (deep or light); else drowsy from DROWSY_MINUTES before ``slot`` (the
    one ahead) starts until it falls asleep (an answer under way at the slot's start is given drowsy); else awake —
    awake inside the slot when the user woke it in it (``woken_at``: just then)."""
    if sleep is not None and sleep.asleep:
        return SleepView(SleepState.ASLEEP, deep(sleep, now), tonight=slot, last=sleep)
    span = None if slot is None else plan_span(slot, zone)
    woken = None
    if sleep is not None and sleep.woke_at is not None and state.woken_at is not None \
            and sleep.woke_at == state.woken_at and now - sleep.woke_at < UP_NEAR_END:
        woken = sleep.woke_at
    if span is not None and span[0] - timedelta(minutes=DROWSY_MINUTES) <= now < span[1] \
            and not (state.woken_at is not None and state.woken_at >= span[0]):
        left = max(0, math.ceil((span[0] - now).total_seconds() / 60))
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
    # a promise the user never came to (``entry``); ``pull``: the night's sleep slot, to start at ``at``
    MISS = "miss"
    FALL = "fall"  # it falls asleep (``how``; ``entry``: the slot it sleeps in)
    WAKE = "wake"  # its slot ended: it wakes (``sleep``) at ``at`` (the slot's end, also when looked at later)
    CLOSE = "close"  # the day is closed in this sleep (``sleep``)
    # the day closes in this sleep (``sleep``) once the day session's job settles: no new work is taken meanwhile
    FENCE = "fence"


@dataclass(frozen=True)
class Transition:
    step: Step
    night: date | None = None
    entry: LifeEntry | None = None
    how: FellBy | None = None
    sleep: SleepRecord | None = None
    pull: LifeEntry | None = None
    at: datetime | None = None


def closing_at(sleep: SleepRecord, state: DayState) -> datetime:
    """§6: the moment ``sleep``, unbroken from its fall, closes the day — CLOSING_SLEEP on, or CLOSING_SHORT on once
    CLOSING_SAFE has passed since the last closing (the shorter counts only from then: hours awake before it do
    not make an earlier short sleep enough)."""
    long = sleep.fell_at + CLOSING_SLEEP
    if state.last_closing_at is None:
        return long
    return min(long, max(sleep.fell_at + CLOSING_SHORT, state.last_closing_at + CLOSING_SAFE))


def closes(sleep: SleepRecord | None, state: DayState, now: datetime, until: datetime | None = None) -> bool:
    """§6: the day closes in ``sleep`` — begun after the last closing, not closed in yet, and unbroken until its
    ``closing_at`` within the sleep: to when it ended (a mumble breaks nothing, a wake ends it), else to now but
    never past ``until`` (its scheduled wake: a restart counts no sleep after it)."""
    if sleep is None or sleep.closed_day:
        return False
    if state.last_closing_at is not None and sleep.fell_at <= state.last_closing_at:
        return False
    end = sleep.woke_at or (now if until is None else min(now, until))
    return closing_at(sleep, state) <= end


def pushed_past(promise: tuple[datetime, datetime], start: datetime, others: Sequence[tuple[datetime, datetime]]) -> bool:
    """§5.4: whether a sleep slot starting at ``start`` was put off for the promise — it starts after the promise,
    no later than PUSH_SLACK after the promise's end or the end of the slots (``others``) that run on from it
    without a gap."""
    end = promise[1]
    while True:
        running = [o[1] for o in others if o[0] <= end < o[1]]
        if not running:
            break
        end = max(running)
    return promise[0] < start <= _ceil(end) + PUSH_SLACK


def _missed_pull(promise: LifeEntry, together: tuple[datetime, datetime], slot: LifeEntry | None,
                 span: tuple[datetime, datetime] | None, plans: Sequence[LifeEntry], now: datetime,
                 zone: tzinfo) -> datetime | None:
    """§5.4: where a missed promise pulls the night's sleep slot — only one put off past it (``pushed_past``), only
    in the night before the slot starts; to now, or after any other live slot still between (never over one).
    None: no pull."""
    if slot is None or span is None or night_of(now.astimezone(zone)) is None or not now < span[0]:
        return None
    others = [s for e in plans if e.live_plan and e.plan_kind not in (PlanKind.SLEEP, PlanKind.DIARY)
              and e.entry_id != promise.entry_id and (s := plan_span(e, zone)) is not None]  # the diary holds none back
    if not pushed_past(together, span[0], others):
        return None
    between = [o[1] for o in others if o[0] < span[0] and o[1] > now]
    target = now.replace(second=0, microsecond=0) if not between else _ceil(max(between))
    return target if target < span[0] else None


def due(now: datetime, plans: Sequence[LifeEntry], sleep: SleepRecord | None, state: DayState, zone: tzinfo, *,
        running: bool, last_user_at: datetime | None = None,
        last_activity_at: datetime | None = None) -> list[Transition]:
    """§5.2, §5.4, §6, §18.3: what the keeper does at ``now`` (in ``zone``), in order. ``plans``: the persona's
    schedule (live or not); ``sleep``: its latest; ``running``: a job of the day session is under way (it never
    falls asleep mid-answer, and the day is never closed under it: FENCE until it settles); ``last_user_at``: the
    user's last message; ``last_activity_at``: the day session's last word (after being woken, it dozes off once
    DOZE_MINUTES pass without one)."""
    local = now.astimezone(zone)
    out: list[Transition] = []
    night = current_night(local)
    for wanted in (night, night + timedelta(days=1)):  # §4.2: tonight's and the next night's
        if slot_of_night(plans, wanted, zone) is None:
            out.append(Transition(Step.PLACE, night=wanted))
    closing = Step.FENCE if running else Step.CLOSE
    if sleep is not None and sleep.asleep:
        ends = wakes_at(plans, sleep, zone)  # its own slot's end (the identity it fell asleep with)
        if closes(sleep, state, now, ends):
            out.append(Transition(closing, sleep=sleep))
        if now >= ends:  # looked at late (a restart): it woke at the slot's end, not now
            out.append(Transition(Step.WAKE, sleep=sleep, at=ends))
        return out
    if closes(sleep, state, now):  # the server was off when it would have: the record says it came
        out.append(Transition(closing, sleep=sleep))
    slot = tonight(plans, now, zone)
    span = None if slot is None else plan_span(slot, zone)
    falls: Transition | None = None
    pulled = False
    for entry in sorted((e for e in plans if e.live_plan and e.plan_kind is PlanKind.TOGETHER),
                        key=lambda e: (e.day or "", e.time_of_day or "", e.entry_id)):
        together = plan_span(entry, zone)
        if together is None or not (now - MISSED_LOOKBACK <= together[0] and together[0] + MISSED_AFTER <= now):
            continue
        if last_user_at is not None and last_user_at >= together[0]:
            continue
        # §5.4: the night's sleep was put off for it — it falls asleep waiting (now, or after a slot still between)
        target = None if pulled else _missed_pull(entry, together, slot, span, plans, now, zone)
        out.append(Transition(Step.MISS, entry=entry, pull=None if target is None else slot, at=target))
        pulled = pulled or target is not None
        if target is not None and target <= now:
            falls = Transition(Step.FALL, entry=slot, how=FellBy.MISSED)
    if running:
        return out
    if falls is not None:
        out.append(falls)
        return out
    asked = state.sleep_requested_at
    if asked is not None and now - asked < REQUEST_KEPT and (sleep is None or asked > sleep.fell_at):
        # second review 1: go_to_sleep is for the slot it moved — never another (a later night's) after a delay
        bound = next((e for e in plans if e.entry_id == state.sleep_requested_slot and e.sleep_slot), None)
        bound_span = None if bound is None else plan_span(bound, zone)
        if bound is not None and bound_span is not None and bound_span[1] > now:
            out.append(Transition(Step.FALL, entry=bound, how=FellBy.TOOL))
            return out
    if span is not None and span[0] <= now < span[1]:
        woken = state.woken_at is not None and state.woken_at >= span[0]
        quiet_since = max((t for t in (state.woken_at, last_activity_at) if t is not None), default=None)
        if not woken or quiet_since is None or now - quiet_since >= timedelta(minutes=DOZE_MINUTES):
            assert slot is not None
            # a slot a missed promise pulled forward to a later slot's end: it fell asleep waiting all the same
            how = (FellBy.MISSED if slot.reason == MISSED_REASON and slot.origin is LifeOrigin.WRITER and not woken
                   else FellBy.SCHEDULE)
            out.append(Transition(Step.FALL, entry=slot, how=how))
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
               since: datetime, now: datetime, zone: tzinfo,
               conversation_id: ConversationId | None = None) -> DayBefore:
    """§3.3, §6 step 1: what the closed session (``conversation_id``, its date ``day``, its ``events``) hands the
    next — its last words, the promises of its time (from ``since``) kept and missed, how HIMARI fell asleep in
    ``sleep`` and why the slot it slept in was put off, when it was (HIMARI's reason)."""
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
    slot = slot_of(plans, sleep)
    delayed = slot.reason if slot is not None and slot.origin is LifeOrigin.HIMARI and slot.reason else None
    return DayBefore(day, last_words(events, zone), tuple(kept), tuple(missed), sleep.fell_at, sleep.how_fell,
                     delayed, sleep_id=sleep.sleep_id, conversation_id=conversation_id)


def told_before(before: DayBefore, sleeps: Sequence[SleepRecord]) -> DayBefore:
    """§3.3: the handoff as a turn is told it — with how often the user woke HIMARI that night and when it got up
    (``sleeps``: the latest, newest first, from the closing sleep on)."""
    night = [s for s in sleeps if before.fell_at is not None and s.fell_at >= before.fell_at]
    woken = sum(1 for s in night if s.how_woke is WokeBy.USER)
    ended = [s.woke_at for s in night if s.woke_at is not None]
    return dataclasses.replace(before, woken=woken, woke_at=max(ended, default=None))
