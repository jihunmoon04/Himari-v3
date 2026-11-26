"""HIMARI's day keeper (character-day v0.1 §3, §5, §6, §18.7): it moves the sleep along and closes the days.

It runs as the server's periodic job ``day`` (every DAY_SECONDS, ``bootstrap.schedule``) and again for a principal
whenever a job of theirs ends (so HIMARI falls asleep right after the answer that said good night), for each
principal who sent a message in the last ACTIVE_DAYS, for the persona chosen now: it applies what ``day.due`` says —
places the nights' sleep slots (origin WRITER), marks a promise the user never came to missed (and lets HIMARI fall
asleep waiting for it), lets it fall asleep (its slot came, go_to_sleep was asked, it dozes off after being woken),
wakes it at its slot's end, and closes the day after enough unbroken sleep (§6): the day session is closed and what
it hands the next is kept, then the memory passes read it at once and the world decides the new day (``on_closed``).

It is also the Driver's ``DayGate`` (filled in by the composition root: Control never imports ``life``): a message to
the day session counts against a sleeping HIMARI and is met with nothing, a mumble or its waking (§5.5); a poke of
the app's character counts the same. Each principal's work here is one at a time (a lock). What changed is told
through callbacks (``on_sleep``: the sleep state for the event stream; ``on_session``: a session closed; ``on_asleep``:
it fell asleep — the diary may be due before 22:00, §7).
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from random import Random
from typing import Protocol
from zoneinfo import ZoneInfo

from himari.common import ConversationId, PrincipalId, TrustedScope
from himari.data.api import (
    NO_PERSONA,
    AsleepAnswer,
    ConversationEvent,
    ConversationSummary,
    DataError,
    DayBefore,
    DayState,
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
from himari.life.day import Step, Transition

log = logging.getLogger(__name__)

DAY_SECONDS = 60.0  # §18.7: the keeper's period
READ_BACK = timedelta(days=2)  # the schedule it reads, from this before today (a night begun yesterday)
SLEEPS_SHOWN = 14  # §12: the latest sleeps the Web shows
SLEEP_TEXT = "잠"  # a placed sleep slot's text
MISSED_REASON = "약속을 기다리다 잠듦"  # §5.4: why the sleep slot was pulled to now


class KeeperData(LifeStore, LifeWriteLog, SleepStore, Protocol):  # what the keeper reads and writes
    async def active_persona(self, principal: PrincipalId) -> Persona | None: ...

    async def current_day_session(self, principal: PrincipalId) -> ConversationSummary | None: ...

    async def close_day_session(self, principal: PrincipalId, conversation_id: ConversationId,
                                now: datetime) -> bool: ...

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
        self.busy = busy  # whether a job of the Conversation runs (never asleep mid-answer); set by the composition root
        self._locks: dict[PrincipalId, asyncio.Lock] = {}
        self._told: dict[PrincipalId, SleepState] = {}
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

    # the Runner's job events (JobListener): a job's end may let it fall asleep

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
        if not await self.day_session(scope):
            return None
        return await self.sleep_view(scope.principal_id)

    async def before(self, scope: TrustedScope) -> DayBefore | None:
        """§3.3: the handoff on the session's first day (its date is today), never its own day's."""
        session = await self._session_of(scope)
        if session is None or session.day_session != self._clock().astimezone(self._zone).date().isoformat():
            return None
        persona_id = await self._persona(scope.principal_id)
        state = await self._data.read_day_state(scope.principal_id, persona_id)
        if state.before is None or state.before.day == session.day_session:
            return None
        return day.told_before(state.before, await self._data.recent_sleeps(scope.principal_id, persona_id,
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

    async def _woken(self, principal: PrincipalId, persona_id: int, sleep: SleepRecord, plans: Sequence[LifeEntry],
                     state: DayState, now: datetime) -> None:
        """§5.2, §5.5: the user woke it — the sleep ends; near its slot's end it is up for the day (the slot ends
        now)."""
        await self._data.end_sleep(principal, sleep.sleep_id, at=now, how=WokeBy.USER)
        await self._data.write_day_state(principal, dataclasses.replace(state, woken_at=now))
        slot = day.slot_of_night(plans, day.current_night(sleep.fell_at.astimezone(self._zone)), self._zone)
        if slot is not None and day.up_for_the_day(slot, now, self._zone):
            span = plan_span(slot, self._zone)
            assert span is not None
            minutes = max(5, int((now - span[0]).total_seconds() // 60))
            await self._write_sleep(principal, persona_id, slot, span[0], minutes, slot.reason, now)

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
            closed: list[TrustedScope | None] = []
            for transition in found:
                try:
                    await self._apply(principal, persona_id, transition, plans, state, session, now, closed)
                except DataError as error:
                    log.warning("the day keeper could not %s for %s: %s", transition.step.value, principal, error)
                if transition.step in (Step.FALL, Step.WAKE, Step.CLOSE):
                    sleep = await self._data.current_sleep(principal, persona_id)
                    state = await self._data.read_day_state(principal, persona_id)
                    plans = await self._plans(principal, persona_id, now)
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
            busy = [s for e in plans if e.live_plan and e.plan_kind is not PlanKind.SLEEP
                    and (s := plan_span(e, self._zone)) is not None]
            placed = day.place_sleep(transition.night, self._rng, self._zone, busy)
            await self._data.write_plan(principal, persona_id, PlanWrite(
                placed.day, placed.time_of_day, placed.minutes, PlanKind.SLEEP, SLEEP_TEXT),
                origin=LifeOrigin.WRITER, zone=self.time_zone, now=now)
        elif transition.step is Step.MISS:
            assert transition.entry is not None
            await self._data.mark_plan_missed(principal, transition.entry.entry_id, now=now)
            pull = transition.pull
            if pull is not None and (span := plan_span(pull, self._zone)) is not None:
                start = now.astimezone(self._zone).replace(second=0, microsecond=0)
                await self._write_sleep(principal, persona_id, pull, start,
                                        int((span[1] - start).total_seconds() // 60), MISSED_REASON, now)
        elif transition.step is Step.FALL:
            assert transition.how is not None
            await self._data.open_sleep(principal, persona_id, at=now, how=transition.how)
            await self._data.write_day_state(principal, dataclasses.replace(
                await self._data.read_day_state(principal, persona_id), sleep_requested_at=None))
            log.info("HIMARI fell asleep for %s (%s)", principal, transition.how.value)
            if self.on_asleep is not None:
                self.on_asleep(principal)
        elif transition.step is Step.WAKE:
            assert transition.sleep is not None
            await self._data.end_sleep(principal, transition.sleep.sleep_id, at=now, how=WokeBy.SCHEDULE)
            log.info("HIMARI woke for %s", principal)
        elif transition.step is Step.CLOSE:
            assert transition.sleep is not None
            closed.append(await self._close(principal, persona_id, transition.sleep, plans, session, now))

    async def _write_sleep(self, principal: PrincipalId, persona_id: int, slot: LifeEntry, start: datetime,
                           minutes: int, reason: str | None, now: datetime) -> None:
        local = start.astimezone(self._zone)
        await self._data.write_plan(principal, persona_id, PlanWrite(
            local.date().isoformat(), local.strftime("%H:%M"), max(5, min(720, minutes)), PlanKind.SLEEP, slot.text,
            slot.entry_id, reason=reason), origin=LifeOrigin.WRITER, zone=self.time_zone, now=now)

    async def _close(self, principal: PrincipalId, persona_id: int, sleep: SleepRecord, plans: Sequence[LifeEntry],
                     session: ConversationSummary | None, now: datetime) -> TrustedScope | None:
        """§6 steps 1 and 2 (no LLM): the open day session closed, with what it hands the next; the closing noted
        (the next nights' slots are placed at each look). The passes and the world follow (``on_closed``)."""
        before = None
        scope = None
        if session is not None and session.day_session is not None:
            scope = TrustedScope(principal, session.conversation_id)
            events = await self._data.read_events(scope)
            before = day.day_before(session.day_session, events, plans, sleep, session.created_at, now, self._zone)
            if await self._data.close_day_session(principal, session.conversation_id, now):
                log.info("closed %s's day session %s", principal, session.conversation_id)
                if self.on_session is not None:
                    self.on_session(principal, dataclasses.replace(session, closed_at=now), True)
        await self._data.note_closing(principal, persona_id, sleep.sleep_id, at=now, before=before)
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
