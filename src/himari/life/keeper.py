"""HIMARI's day keeper (character-day v0.1 §3, §5, §6, §18.7): it moves the sleep along and closes the days.

It runs as the server's periodic job ``day`` (every DAY_SECONDS, ``bootstrap.schedule``) and again for a principal
whenever a job of theirs ends (so HIMARI falls asleep right after the answer that said good night), for each
principal who sent a message in the last ACTIVE_DAYS, for the persona chosen now: it applies what ``day.due`` says —
places the nights' sleep slots (origin WRITER) and, beside each, the night's diary slot (character-diary §3; also for
a night whose sleep slot is there already, while there is room before it), marks a promise the user never came to
missed (and lets HIMARI fall asleep waiting for it), lets it fall asleep (its slot came, go_to_sleep was asked, it dozes off after being woken),
wakes it at the end of the slot it sleeps in, and closes the day after enough unbroken sleep (§6). A closing never
cuts a job of the day session: the session is fenced first (no new message, Run, wake or compaction is taken), the job
settles, and the next look closes it — the session, the sleep's mark and what it hands the next in one commit; then
the memory passes read it at once and the world decides the new day (``on_closed``; both also find it from what is
stored, so a restart in between loses neither).

It is also the Driver's ``DayGate`` (filled in by the composition root: Control never imports ``life``): a message to
the day session counts against a sleeping HIMARI and is met with nothing, a mumble or its waking (§5.5); a poke of
the app's character counts the same; work is admitted only to a day session that is neither closed nor fenced. Each
principal's work here is one at a time (a lock). What changed is told through callbacks (``on_sleep``: the sleep
state for the event stream; ``on_session``: a session closed; ``on_asleep``: it fell asleep — the diary is written
as it falls asleep when its slot has not come yet, character-diary §4; ``on_closed``: the day closed — the passes,
the world and the diary's amend, character-diary §5).
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from random import Random
from typing import Protocol
from zoneinfo import ZoneInfo

from himari.common import ConversationId, PrincipalId, TrustedScope
from himari.data.api import (
    DIARY_TEXT,
    NO_PERSONA,
    AsleepAnswer,
    ConversationEvent,
    ConversationSummary,
    DataError,
    DataErrorCode,
    DayBefore,
    DayState,
    FellBy,
    LifeEntry,
    LifeKind,
    LifeOrigin,
    LifeStore,
    LifeWriteLog,
    MessageOrigin,
    OriginKind,
    Persona,
    PlanKind,
    PlanWrite,
    SleepRecord,
    SleepState,
    SleepStore,
    SleepView,
    WokeBy,
    plan_span,
)
from himari.life import day
from himari.life.api import ACTIVE_DAYS
from himari.life.day import MISSED_REASON, Step, Transition

log = logging.getLogger(__name__)

DAY_SECONDS = 60.0  # §18.7: the keeper's period
READ_BACK = timedelta(days=2)  # the schedule it reads, from this before today (a night begun yesterday)
SLEEPS_SHOWN = 14  # §12: the latest sleeps the Web shows
SLEEP_TEXT = "잠"  # a placed sleep slot's text


class KeeperData(LifeStore, LifeWriteLog, SleepStore, Protocol):  # what the keeper reads and writes
    async def active_persona(self, principal: PrincipalId) -> Persona | None: ...

    async def current_day_session(self, principal: PrincipalId) -> ConversationSummary | None: ...

    async def read_conversation(self, scope: TrustedScope) -> ConversationSummary: ...

    async def read_events(self, scope: TrustedScope, *, after_sequence: int = 0,
                          limit: int | None = None) -> Sequence[ConversationEvent]: ...

    async def last_event(self, scope: TrustedScope) -> tuple[int, datetime] | None: ...

    async def last_user_message_at(self, principal: PrincipalId) -> datetime | None: ...


def wall_clock() -> datetime:
    """The keeper's clock when none is given (read at each call: the test suite holds it at noon)."""
    return datetime.now(UTC)


class DayKeeper:
    """The ``day`` job, the Driver's DayGate and the poke."""

    def __init__(self, data: KeeperData, *, time_zone: str, clock: Callable[[], datetime] | None = None,
                 rng: Random | None = None, busy: Callable[[TrustedScope], bool] = lambda scope: False) -> None:
        self._data = data
        self._zone = ZoneInfo(time_zone)
        self.time_zone = time_zone
        self._clock = clock or (lambda: wall_clock())
        self._rng = rng or Random()
        # whether a job of the Conversation runs or an input is being taken into it (never asleep mid-answer, never
        # closed under it); set by the composition root
        self.busy = busy
        self._locks: dict[PrincipalId, asyncio.Lock] = {}
        self._told: dict[PrincipalId, SleepState] = {}
        self._fenced: dict[PrincipalId, ConversationId] = {}  # §6: the day session closing once its job settles
        self._closed: set[ConversationId] = set()  # the day sessions this process closed (Data says so too)
        self._unplaced: set[tuple[PrincipalId, date]] = set()  # nights told to have no room for a slot (logged once)
        self._tasks: set[asyncio.Task[object]] = set()
        # set by the composition root
        self.on_sleep: Callable[[PrincipalId, SleepState], None] | None = None
        self.on_session: Callable[[PrincipalId, ConversationSummary, bool], None] | None = None
        self.on_asleep: Callable[[PrincipalId], None] | None = None
        self.on_closed: Callable[[PrincipalId, TrustedScope | None], Awaitable[None]] | None = None

    # --- the job ---------------------------------------------------------------------------

    async def run(self) -> None:
        """One round: every active principal."""
        for principal in await self._data.life_principals(self._clock() - ACTIVE_DAYS):
            try:
                await self.look(principal)
            except Exception:  # noqa: BLE001 — one principal's trouble is not the others'
                log.exception("the day keeper failed for %s", principal)

    def poke_soon(self, principal: PrincipalId) -> None:
        """A look in the background (after a job ended)."""
        task: asyncio.Task[object] = asyncio.get_running_loop().create_task(self._look_logged(principal))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _look_logged(self, principal: PrincipalId) -> object:
        try:
            return await self.look(principal)
        except Exception:  # noqa: BLE001 — the next round looks again
            log.exception("the day keeper failed for %s", principal)
            return None

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    # the Runner's job events (JobListener): a job's end may let it fall asleep, or close a fenced day

    def job_started(self, scope: TrustedScope, origin: object, status: object) -> None:
        pass

    def accepted(self, scope: TrustedScope) -> None:
        pass

    def run_ended(self, scope: TrustedScope) -> None:
        pass

    def job_ended(self, scope: TrustedScope, status: object) -> None:
        self.poke_soon(scope.principal_id)

    # --- reading -----------------------------------------------------------------------------

    def _lock(self, principal: PrincipalId) -> asyncio.Lock:
        return self._locks.setdefault(principal, asyncio.Lock())

    async def _persona(self, principal: PrincipalId) -> int:
        chosen = await self._data.active_persona(principal)
        return NO_PERSONA if chosen is None else chosen.persona_id

    async def _plans(self, principal: PrincipalId, persona_id: int, now: datetime) -> list[LifeEntry]:
        since = (now.astimezone(self._zone).date() - READ_BACK).isoformat()
        return [e for e in await self._data.list_life(principal, persona_id, since=since, now=now)
                if e.kind is LifeKind.PLAN]

    async def _session_of(self, scope: TrustedScope) -> ConversationSummary | None:
        current = await self._data.current_day_session(scope.principal_id)
        return current if current is not None and current.conversation_id == scope.conversation_id else None

    async def sleep_view(self, principal: PrincipalId) -> SleepView:
        """§5.1, §12: the chosen persona's sleep as seen now (the Web's status, the device's state)."""
        persona_id = await self._persona(principal)
        now = self._clock()
        plans = await self._plans(principal, persona_id, now)
        return day.sleep_view(now, day.tonight(plans, now, self._zone),
                              await self._data.current_sleep(principal, persona_id),
                              await self._data.read_day_state(principal, persona_id), self._zone)

    async def recent_sleeps(self, principal: PrincipalId) -> tuple[SleepRecord, ...]:
        return await self._data.recent_sleeps(principal, await self._persona(principal), limit=SLEEPS_SHOWN)

    # --- DayGate (control.api) -----------------------------------------------------------------

    async def day_session(self, scope: TrustedScope) -> bool:
        return await self._session_of(scope) is not None

    async def view(self, scope: TrustedScope) -> SleepView | None:
        """§8: the turn's sleep — the nights' slots placed first when missing (§4.2: on first need, before any
        Tool of the turn would move one)."""
        if not await self.day_session(scope):
            return None
        await self.ensure_slots(scope.principal_id)
        return await self.sleep_view(scope.principal_id)

    def closing(self, scope: TrustedScope) -> bool:
        """§3.2, §6, no await: the day session is fenced for its closing (it closes once its job settles: no new
        work is taken meanwhile) or was closed by this process. Data knows every closed one (``admit``)."""
        return self._fenced.get(scope.principal_id) == scope.conversation_id or scope.conversation_id in self._closed

    async def admit(self, scope: TrustedScope) -> None:
        """§3.2, §6: DataError DAY_CLOSED when ``scope`` is a day session closed, or fenced for its closing (the
        closing is then looked at once more). Any other Conversation is admitted as before."""
        if self.closing(scope):
            if scope.conversation_id not in self._closed:
                self.poke_soon(scope.principal_id)
            raise DataError(DataErrorCode.DAY_CLOSED, scope.conversation_id)
        summary = await self._data.read_conversation(scope)
        if summary.day_session is not None and summary.closed_at is not None:
            raise DataError(DataErrorCode.DAY_CLOSED, scope.conversation_id)

    async def before(self, scope: TrustedScope) -> DayBefore | None:
        """§3.3: the handoff on the session's first day (its date is today), from the session closed before it —
        named by its id (never by date: a session opened the same date after a closing gets it too)."""
        session = await self._session_of(scope)
        if session is None or session.day_session != self._clock().astimezone(self._zone).date().isoformat():
            return None
        persona_id = await self._persona(scope.principal_id)
        state = await self._data.read_day_state(scope.principal_id, persona_id)
        handed = state.before
        if handed is None or (handed.conversation_id == session.conversation_id if handed.conversation_id
                              is not None else handed.day == session.day_session):
            return None
        return day.told_before(handed, await self._data.recent_sleeps(scope.principal_id, persona_id,
                                                                      limit=SLEEPS_SHOWN))

    async def answer(self, scope: TrustedScope, origin: MessageOrigin | None) -> AsleepAnswer | None:
        """§5.5: a user message to the day session, counted against a sleeping HIMARI."""
        if origin is None or origin.kind is OriginKind.WAKE or not await self.day_session(scope):
            return None
        return await self._stirred(scope.principal_id, message=True)

    async def poke(self, principal: PrincipalId) -> AsleepAnswer | None:
        """§5.5, §11: the app's sleeping character clicked — counted as a message is; None while awake."""
        return await self._stirred(principal, message=False)

    async def _stirred(self, principal: PrincipalId, *, message: bool) -> AsleepAnswer | None:
        async with self._lock(principal):
            persona_id = await self._persona(principal)
            sleep = await self._data.current_sleep(principal, persona_id)
            if sleep is None or not sleep.asleep:
                return None
            now = self._clock()
            plans = await self._plans(principal, persona_id, now)
            if await self._woke_on_time(principal, sleep, plans, now):
                return None  # its slot had ended before this came: it is met awake
            state = await self._data.read_day_state(principal, persona_id)
            view = day.sleep_view(now, day.tonight(plans, now, self._zone), sleep, state, self._zone)
            answer = day.answer_message(view, sleep, now)
            if answer is None:
                return None
            await self._data.count_wake(principal, sleep.sleep_id, count=day.next_count(sleep, now), at=now,
                                        answer=answer, message=message)
            if answer is AsleepAnswer.WAKE:
                await self._woken(principal, persona_id, sleep, plans, state, now)
            self._tell(principal, SleepState.HALF_ASLEEP if answer is AsleepAnswer.MUMBLE
                       else SleepState.AWAKE if answer is AsleepAnswer.WAKE else SleepState.ASLEEP)
            return answer

    async def _woke_on_time(self, principal: PrincipalId, sleep: SleepRecord, plans: Sequence[LifeEntry],
                            now: datetime) -> bool:
        """§5.2, second review 3: an open sleep whose slot ended before the keeper's next look woke at that end —
        recorded so before a message or a poke is counted against it (or a turn reads it); whether it did."""
        if not sleep.asleep:
            return False
        ends = day.wakes_at(plans, sleep, self._zone)
        if now < ends:
            return False
        await self._data.end_sleep(principal, sleep.sleep_id, at=ends, how=WokeBy.SCHEDULE)
        log.info("HIMARI woke for %s (its slot had ended)", principal)
        self._tell(principal, SleepState.AWAKE)
        return True

    async def _woken(self, principal: PrincipalId, persona_id: int, sleep: SleepRecord, plans: Sequence[LifeEntry],
                     state: DayState, now: datetime) -> None:
        """§5.2, §5.5: the user woke it — the sleep ends; near the end of the slot it slept in, it is up for the day
        (that slot ends now)."""
        await self._data.end_sleep(principal, sleep.sleep_id, at=now, how=WokeBy.USER)
        await self._data.write_day_state(principal, dataclasses.replace(state, woken_at=now))
        slot = day.slot_of(plans, sleep)
        if slot is not None and day.up_for_the_day(slot, now, self._zone):
            span = plan_span(slot, self._zone)
            assert span is not None
            minutes = max(5, int((now - span[0]).total_seconds() // 60))
            await self._write_sleep(principal, persona_id, slot, span[0], minutes, slot.reason, now)

    # --- placing the nights' slots (§4.2, §5.2) ---------------------------------------------------

    async def ensure_slots(self, principal: PrincipalId) -> None:
        """§4.2: tonight's and the next night's sleep slots, placed now when missing (the keeper's rule, on first
        need: a turn of the day session starts with them), each with its diary slot (character-diary §3); an overdue
        scheduled wake recorded first."""
        async with self._lock(principal):
            persona_id = await self._persona(principal)
            now = self._clock()
            plans = await self._plans(principal, persona_id, now)
            sleep = await self._data.current_sleep(principal, persona_id)
            if sleep is not None:
                await self._woke_on_time(principal, sleep, plans, now)
            night = day.current_night(now.astimezone(self._zone))
            for wanted in (night, night + timedelta(days=1)):
                if day.slot_of_night(plans, wanted, self._zone) is None:
                    try:
                        await self._place(principal, persona_id, wanted, plans, now)
                    except DataError as error:
                        log.warning("the day keeper could not place a slot for %s: %s", principal, error)
                    plans = await self._plans(principal, persona_id, now)
            await self._place_diaries(principal, persona_id, now, plans)

    async def _place_diaries(self, principal: PrincipalId, persona_id: int, now: datetime,
                             plans: Sequence[LifeEntry]) -> None:
        """character-diary §3, §16.4: tonight's and the next night's diary slots beside their sleep slots, placed now
        when missing (the day it was deployed: a night with its sleep slot already), tonight's only while there is
        room from now on."""
        night = day.current_night(now.astimezone(self._zone))
        for wanted in (night, night + timedelta(days=1)):
            if day.diary_of_night(plans, wanted, self._zone) is not None:
                continue
            try:
                await self._place_diary(principal, persona_id, wanted, plans, now)
            except DataError as error:
                log.warning("the day keeper could not place a diary slot for %s: %s", principal, error)

    async def _place(self, principal: PrincipalId, persona_id: int, night: date, plans: Sequence[LifeEntry],
                     now: datetime) -> None:
        busy = [s for e in plans if e.live_plan and e.plan_kind not in (PlanKind.SLEEP, PlanKind.DIARY)
                and (s := plan_span(e, self._zone)) is not None]  # character-diary §16.2: the sleep may cover it
        placed = day.place_sleep(night, self._rng, self._zone, busy)
        if placed is None:  # §19.1: the night is full of slots — none until room is made (looked at again each time)
            if (principal, night) not in self._unplaced:
                self._unplaced.add((principal, night))
                log.warning("no room for the sleep slot of the night of %s for %s", night, principal)
            return
        self._unplaced.discard((principal, night))
        await self._data.write_plan(principal, persona_id, PlanWrite(
            placed.day, placed.time_of_day, placed.minutes, PlanKind.SLEEP, SLEEP_TEXT),
            origin=LifeOrigin.WRITER, zone=self.time_zone, now=now)
        await self._place_diary(principal, persona_id, night, await self._plans(principal, persona_id, now), now)

    async def _place_diary(self, principal: PrincipalId, persona_id: int, night: date, plans: Sequence[LifeEntry],
                           now: datetime) -> None:
        """character-diary §3: the night's diary slot beside its sleep slot (origin WRITER), unless it has one, has no
        sleep slot, or no room is left before the sleep (that night it writes the diary as it falls asleep)."""
        if day.diary_of_night(plans, night, self._zone) is not None:
            return
        slot = day.slot_of_night(plans, night, self._zone)
        span = None if slot is None else plan_span(slot, self._zone)
        if span is None:
            return
        placed = day.place_diary(night, span[0], self._rng, self._zone, now)
        if placed is None:
            return
        await self._data.write_plan(principal, persona_id, PlanWrite(
            placed.day, placed.time_of_day, placed.minutes, PlanKind.DIARY, DIARY_TEXT),
            origin=LifeOrigin.WRITER, zone=self.time_zone, now=now)

    # --- applying what is due ------------------------------------------------------------------

    async def look(self, principal: PrincipalId) -> list[Transition]:
        """What is due for the principal now, applied in order."""
        async with self._lock(principal):
            persona_id = await self._persona(principal)
            now = self._clock()
            state = await self._data.read_day_state(principal, persona_id)
            if state.last_closing_at is None:  # §6: the 36 hours count from the first look (a sleep after it closes)
                state = dataclasses.replace(state, last_closing_at=now - timedelta(seconds=1))
                await self._data.write_day_state(principal, state)
            plans = await self._plans(principal, persona_id, now)
            sleep = await self._data.current_sleep(principal, persona_id)
            session = await self._data.current_day_session(principal)
            scope = None if session is None else TrustedScope(principal, session.conversation_id)
            last = None if scope is None else await self._data.last_event(scope)
            found = day.due(now, plans, sleep, state, self._zone, running=scope is not None and self.busy(scope),
                            last_user_at=await self._data.last_user_message_at(principal),
                            last_activity_at=None if last is None else last[1])
            if not any(t.step in (Step.FENCE, Step.CLOSE) for t in found):
                self._fenced.pop(principal, None)  # nothing closes now: a stale fence lets work in again
            closed: list[TrustedScope | None] = []
            pull_failed = False
            for transition in found:
                if transition.step is Step.FALL and transition.how is FellBy.MISSED and pull_failed:
                    continue  # the slot was not pulled: it falls asleep when it comes
                try:
                    await self._apply(principal, persona_id, transition, plans, state, session, now, closed)
                except DataError as error:
                    log.warning("the day keeper could not %s for %s: %s", transition.step.value, principal, error)
                    pull_failed = pull_failed or (transition.step is Step.MISS and transition.pull is not None)
                if transition.step in (Step.FALL, Step.WAKE, Step.CLOSE, Step.MISS):
                    sleep = await self._data.current_sleep(principal, persona_id)
                    state = await self._data.read_day_state(principal, persona_id)
                    plans = await self._plans(principal, persona_id, now)
            if any(t.step is Step.PLACE for t in found):
                plans = await self._plans(principal, persona_id, now)
            await self._place_diaries(principal, persona_id, now, plans)  # character-diary §3: a night with no diary
            view = day.sleep_view(now, day.tonight(plans, now, self._zone), sleep, state, self._zone)
            self._tell(principal, view.state)
        for scope_closed in closed:  # outside the lock: the passes and the world take their time
            if self.on_closed is not None:
                await self.on_closed(principal, scope_closed)
        return found

    async def _apply(self, principal: PrincipalId, persona_id: int, transition: Transition,
                     plans: Sequence[LifeEntry], state: DayState, session: ConversationSummary | None, now: datetime,
                     closed: list[TrustedScope | None]) -> None:
        if transition.step is Step.PLACE:
            assert transition.night is not None
            await self._place(principal, persona_id, transition.night, plans, now)
        elif transition.step is Step.MISS:
            assert transition.entry is not None
            await self._data.mark_plan_missed(principal, transition.entry.entry_id, now=now)
            pull = transition.pull
            if pull is not None and transition.at is not None and (span := plan_span(pull, self._zone)) is not None:
                start = transition.at.astimezone(self._zone)
                await self._write_sleep(principal, persona_id, pull, start,
                                        int((span[1] - start).total_seconds() // 60), MISSED_REASON, now)
        elif transition.step is Step.FALL:
            assert transition.how is not None
            await self._data.open_sleep(principal, persona_id, at=now, how=transition.how,
                                        slot_id=None if transition.entry is None else transition.entry.entry_id)
            await self._data.write_day_state(principal, dataclasses.replace(
                await self._data.read_day_state(principal, persona_id), sleep_requested_at=None,
                sleep_requested_slot=None))
            log.info("HIMARI fell asleep for %s (%s)", principal, transition.how.value)
            if self.on_asleep is not None:
                self.on_asleep(principal)
        elif transition.step is Step.WAKE:
            assert transition.sleep is not None
            await self._data.end_sleep(principal, transition.sleep.sleep_id, at=transition.at or now,
                                       how=WokeBy.SCHEDULE)
            log.info("HIMARI woke for %s", principal)
        elif transition.step in (Step.FENCE, Step.CLOSE):
            assert transition.sleep is not None
            if session is not None:  # §6: no new work in the day session from now on
                if self._fenced.get(principal) != session.conversation_id:
                    log.info("fenced %s's day session %s for its closing", principal, session.conversation_id)
                self._fenced[principal] = session.conversation_id
                if transition.step is Step.FENCE or self.busy(TrustedScope(principal, session.conversation_id)):
                    return  # its job settles first; its end looks again
            closed.append(await self._close(principal, persona_id, transition.sleep, plans, session, now))
            self._fenced.pop(principal, None)

    async def _write_sleep(self, principal: PrincipalId, persona_id: int, slot: LifeEntry, start: datetime,
                           minutes: int, reason: str | None, now: datetime) -> None:
        local = start.astimezone(self._zone)
        await self._data.write_plan(principal, persona_id, PlanWrite(
            local.date().isoformat(), local.strftime("%H:%M"), max(5, min(720, minutes)), PlanKind.SLEEP, slot.text,
            slot.entry_id, reason=reason), origin=LifeOrigin.WRITER, zone=self.time_zone, now=now)

    async def _close(self, principal: PrincipalId, persona_id: int, sleep: SleepRecord, plans: Sequence[LifeEntry],
                     session: ConversationSummary | None, now: datetime) -> TrustedScope | None:
        """§6 steps 1 and 2 (no LLM): the day session (fenced, its job settled) read to its last event, then closed
        with what it hands the next and the closing noted, in one commit; told after it. The next nights' slots are
        placed at each look; the passes and the world follow (``on_closed``)."""
        before = None
        scope = None
        if session is not None and session.day_session is not None:
            scope = TrustedScope(principal, session.conversation_id)
            events = await self._data.read_events(scope)
            before = day.day_before(session.day_session, events, plans, sleep, session.created_at, now, self._zone,
                                    session.conversation_id)
        closed = await self._data.note_closing(principal, persona_id, sleep.sleep_id, at=now, before=before,
                                               conversation_id=None if scope is None else scope.conversation_id)
        if scope is not None:
            self._closed.add(scope.conversation_id)
        if closed and session is not None:
            log.info("closed %s's day session %s", principal, session.conversation_id)
            if self.on_session is not None:
                self.on_session(principal, dataclasses.replace(session, closed_at=now), True)
        return scope

    def _tell(self, principal: PrincipalId, state: SleepState) -> None:
        """A change of the sleep, told (the first state seen is only kept: a stream's hello says it)."""
        before = self._told.get(principal)
        self._told[principal] = state
        if before is state or before is None:
            return
        if self.on_sleep is not None:
            try:
                self.on_sleep(principal, state)
            except Exception:  # noqa: BLE001 — telling is not the day
                log.exception("could not tell the sleep of %s", principal)
