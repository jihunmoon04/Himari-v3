"""The story writer runs (character-life v0.1 §3): when, for whom, and what a failure does.

It runs as the server's periodic job ``life`` (``bootstrap.schedule``): each
round, for every principal who sent a message in the last ACTIVE_DAYS, it
looks at the latest date whose write time has come (L3) and writes that date
for the persona chosen now, unless its diary is there. A missed date is
written at the next round; an earlier one is never filled in. A call whose
answer is out of shape is recorded and thrown away; the next round tries again,
ATTEMPTS_PER_DAY times a date. Every call's request is stored before it is
sent, and the tokens it may use are reserved against the principal's own daily
budget first (L4: apart from the memory passes').

character-world v0.1: the input also holds the weather where the user is for the
date written (as last read, from memory) and the day's topic titles (read once a
date, only when a call is about to be made), and the persona is told as a turn
tells it — its hidden lines only from the bond stage they wait for (W4).

character-work v0.1 §4 (K1): the same job also runs the worker. After the principal's
write, it looks for the chosen persona's work plan whose work time ended within
WORK_WINDOW and has no piece, and makes that piece with one call — the same model,
the same daily budget and the same record (a life write naming its plan), at most
ATTEMPTS_PER_DAY calls a plan. A plan past its window is not worked any more.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from himari.common import PrincipalId
from himari.data.api import (
    NO_PERSONA,
    BondStanding,
    HimariMood,
    DataError,
    DataErrorCode,
    LifeKind,
    LifeStore,
    LifeWriteLog,
    LifeWriteStart,
    Persona,
    RawNoResponse,
    RawReceived,
    RawUncertain,
    RecentEpisode,
    live_mood,
)
from himari.life import work
from himari.life.api import ACTIVE_DAYS, ATTEMPTS_PER_DAY, LifeSettings, WriteOutcome
from himari.life.writer import (
    READ_DAYS,
    WRITER_MAX_OUTPUT_TOKENS,
    AnswerError,
    answer_text,
    day_bounds,
    model_input,
    parse_answer,
    request_data,
    reservation,
    target_day,
)
from himari.prompt.api import RequestOptions, persona_for_stage
from himari.provider.api import (
    NativeNoResponse,
    NativePreparedRequest,
    NativeReceived,
    NormalizedFinal,
    ProviderAdapter,
    ProviderError,
)
from himari.world.api import TopicSource, WeatherSource

log = logging.getLogger(__name__)

START_DELAY = timedelta(seconds=60)  # after startup before the first round writes (as memory passes wait)


class LifeWriterData(LifeStore, LifeWriteLog, Protocol):  # what the writer reads and writes
    async def active_persona(self, principal: PrincipalId) -> Persona | None: ...

    async def find_episodes(self, principal: PrincipalId, words: Sequence[str], *, start: datetime | None = None,
                            end: datetime | None = None, limit: int = 200) -> tuple[RecentEpisode, ...]: ...

    async def bond_stage(self, principal: PrincipalId, persona_id: int) -> BondStanding: ...

    async def read_mood(self, principal: PrincipalId, persona_id: int) -> HimariMood | None: ...


class LifeWriter:
    """The ``life`` job (§3) and one principal's write."""

    def __init__(self, data: LifeWriterData, provider: ProviderAdapter, settings: LifeSettings, *,
                 clock: Callable[[], datetime] | None = None, weather: WeatherSource | None = None,
                 topics: TopicSource | None = None, quiet: tuple[time, time] | None = None) -> None:
        self._data = data
        self._quiet = quiet  # character-work §4: no work plan in HIMARI's sleeping hours
        self._provider = provider
        self.settings = settings
        self._weather = weather  # character-world §2: None: no weather in the input
        self._topics = topics  # §3: None: no topics (none configured, or HIMARI_LIFE_TOPICS=0)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._zone = ZoneInfo(settings.time_zone)
        self._principals: dict[PrincipalId, asyncio.Lock] = {}
        self._ready_at: datetime | None = None  # set by start(): rounds before it write nothing

    async def start(self) -> None:
        """Writes left open by a stop end as INTERRUPTED; rounds write from START_DELAY on."""
        interrupted = await self._data.interrupt_life_writes()
        if interrupted:
            log.warning("%d story writer call(s) were cut short by a stop; their dates are tried again", interrupted)
        self._ready_at = self._clock() + START_DELAY

    async def run(self) -> None:
        """One round of the job: every active principal's latest date."""
        now = self._clock()
        if self._ready_at is None or now < self._ready_at:
            return
        for principal in await self._data.life_principals(now - ACTIVE_DAYS):
            try:
                outcome = await self.write(principal)
            except Exception:  # noqa: BLE001 — one principal's trouble is not the others'
                log.exception("the story writer could not write for %s", principal)
                continue
            if outcome is WriteOutcome.DEFERRED:
                log.info("the story writer waits for %s: the daily budget is used", principal)
            try:  # character-work §4: after the write, so a day's plans are there first
                worked = await self.work(principal)
            except Exception:  # noqa: BLE001
                log.exception("the worker could not work for %s", principal)
                continue
            if worked is WriteOutcome.DEFERRED:
                log.info("the worker waits for %s: the daily budget is used", principal)

    def budget_start(self, now: datetime) -> datetime:
        """The start of ``now``'s day in the server's zone: the daily budget's."""
        return datetime.combine(now.astimezone(self._zone).date(), time(), tzinfo=self._zone).astimezone(UTC)

    async def write(self, principal: PrincipalId) -> WriteOutcome:
        """The latest date whose write time has come, for the persona chosen now."""
        lock = self._principals.setdefault(principal, asyncio.Lock())
        async with lock:
            return await self._write(principal)

    async def work(self, principal: PrincipalId) -> WriteOutcome:
        """character-work §4: the piece of the chosen persona's due work plan, if any."""
        lock = self._principals.setdefault(principal, asyncio.Lock())
        async with lock:
            return await self._work(principal)

    async def _persona(self, principal: PrincipalId) -> tuple[int, str]:
        """The chosen persona's id and text as a turn tells it (W4: at the stage as stored)."""
        chosen = await self._data.active_persona(principal)
        persona_id = NO_PERSONA if chosen is None else chosen.persona_id
        persona = "" if chosen is None else chosen.text
        if persona:
            persona = persona_for_stage(persona, (await self._data.bond_stage(principal, persona_id)).stage)
        return persona_id, persona

    async def _work(self, principal: PrincipalId) -> WriteOutcome:
        now = self._clock()
        today = now.astimezone(self._zone).date()
        chosen = await self._data.active_persona(principal)
        persona_id = NO_PERSONA if chosen is None else chosen.persona_id
        entries = await self._data.list_life(principal, persona_id, since=(today - timedelta(days=1)).isoformat())
        plan = work.due_plan(entries, now, self._zone)
        if plan is None:
            return WriteOutcome.NOTHING
        if await self._data.work_attempts(principal, plan.entry_id) >= ATTEMPTS_PER_DAY:
            return WriteOutcome.GAVE_UP
        project = next(e for e in entries if e.entry_id == plan.arc_id)
        persona_id, persona = await self._persona(principal)
        mood = live_mood(await self._data.read_mood(principal, persona_id), now)
        options = RequestOptions(self.settings.model_id, self.settings.reasoning_effort,
                                 provider_key=self._provider.provider_key,
                                 max_output_tokens=work.WORK_MAX_OUTPUT_TOKENS)
        asked = work.model_input(work.request_data(persona, entries, plan, project, now=now, zone=self._zone,
                                                   mood=mood), options)
        request = self._provider.prepare_native_request(asked)
        reserve = reservation(request.body, work.WORK_MAX_OUTPUT_TOKENS)
        if await self._data.life_usage(principal, self.budget_start(now)) + reserve > self.settings.daily_tokens:
            return WriteOutcome.DEFERRED
        assert plan.day is not None  # a work plan has its date (work_plans)
        write_id = await self._data.begin_life_write(principal, LifeWriteStart(
            persona_id, plan.day, self._provider.provider_key, self.settings.model_id, request.body,
            request.content_type, reserve, plan_id=plan.entry_id))
        try:
            return await self._make(write_id, request)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._data.fail_life_write(write_id, type(error).__name__)
            raise

    async def _make(self, write_id: str, request: NativePreparedRequest) -> WriteOutcome:
        text = await self._answer(write_id, request, "worker")
        if text is None:
            return WriteOutcome.FAILED
        try:
            piece = work.parse_piece(text)
        except AnswerError as error:
            await self._data.fail_life_write(write_id, f"malformed answer: {error}")
            log.warning("worker call %s thrown away: %s", write_id, error)
            return WriteOutcome.FAILED
        try:
            made = await self._data.apply_work_write(write_id, piece, now=self._clock())
        except DataError as error:
            if error.code not in (DataErrorCode.INVALID_ARGUMENT, DataErrorCode.TOO_LARGE):
                raise
            await self._data.fail_life_write(write_id, f"refused: {error.code.value}")
            log.warning("worker call %s thrown away: %s", write_id, error.code.value)
            return WriteOutcome.FAILED
        log.info("worker call %s made piece %d", write_id, made[0].entry_id)
        return WriteOutcome.WRITTEN

    async def _answer(self, write_id: str, request: NativePreparedRequest, who: str) -> str | None:
        """The call's answer text, recorded; None (recorded as failed) when there is none."""
        await self._data.mark_life_write_dispatched(write_id)
        outcome = await self._provider.invoke_prepared(request)
        if isinstance(outcome, NativeNoResponse):
            await self._data.record_life_write(write_id, RawNoResponse(outcome.error_code, outcome.body,
                                                                       outcome.content_type))
            log.warning("%s call %s got no answer (%s)", who, write_id, outcome.error_code)
            return None
        if not isinstance(outcome, NativeReceived):
            await self._data.record_life_write(write_id, RawUncertain(outcome.error_code))
            log.warning("%s call %s: outcome unknown (%s)", who, write_id, outcome.error_code)
            return None
        await self._data.record_life_write(write_id, RawReceived(outcome.body, outcome.content_type, outcome.usage))
        try:
            normalized = self._provider.normalize(outcome)
            if not isinstance(normalized, NormalizedFinal):
                raise AnswerError("tool calls instead of an answer")
            return answer_text(normalized.blocks)
        except (AnswerError, ProviderError) as error:
            await self._data.fail_life_write(write_id, f"malformed answer: {error}")
            log.warning("%s call %s thrown away: %s", who, write_id, error)
            return None

    async def _write(self, principal: PrincipalId) -> WriteOutcome:
        now = self._clock()
        day = target_day(now, self._zone, self.settings.write_at)
        written = day.isoformat()
        chosen = await self._data.active_persona(principal)
        persona_id = NO_PERSONA if chosen is None else chosen.persona_id
        entries = await self._data.list_life(principal, persona_id, since=(day - READ_DAYS).isoformat())
        if any(e.kind is LifeKind.DIARY and e.day == written for e in entries):
            return WriteOutcome.NOTHING
        if await self._data.life_attempts(principal, persona_id, written) >= ATTEMPTS_PER_DAY:
            return WriteOutcome.GAVE_UP
        start, end = day_bounds(day, self._zone)
        episodes = await self._data.find_episodes(principal, (), start=start, end=end)
        persona_id, persona = await self._persona(principal)  # character-world §5 (W4): as a turn tells it
        found = None if self._weather is None else self._weather.weather_on(day)
        topics = () if self._topics is None else await self._topics.topics(day)  # §3: once a date
        options = RequestOptions(self.settings.model_id, self.settings.reasoning_effort,
                                 provider_key=self._provider.provider_key, max_output_tokens=WRITER_MAX_OUTPUT_TOKENS)
        asked = model_input(request_data(persona, entries, episodes, day=day, now=now, zone=self._zone,
                                         weather=None if found is None else found.line(), topics=topics,
                                         quiet=self._quiet), options)
        request = self._provider.prepare_native_request(asked)
        reserve = reservation(request.body)
        if await self._data.life_usage(principal, self.budget_start(now)) + reserve > self.settings.daily_tokens:
            return WriteOutcome.DEFERRED
        write_id = await self._data.begin_life_write(principal, LifeWriteStart(
            persona_id, written, self._provider.provider_key, self.settings.model_id, request.body,
            request.content_type, reserve))
        try:
            return await self._call(write_id, request, day)
        except asyncio.CancelledError:
            raise  # a stop: the write stays open, the next start ends it as INTERRUPTED
        except Exception as error:
            await self._data.fail_life_write(write_id, type(error).__name__)
            raise

    async def _call(self, write_id: str, request: NativePreparedRequest, day: date) -> WriteOutcome:
        text = await self._answer(write_id, request, "story writer")
        if text is None:
            return WriteOutcome.FAILED
        try:
            plan = parse_answer(text, day, self._quiet)
        except AnswerError as error:
            await self._data.fail_life_write(write_id, f"malformed answer: {error}")
            log.warning("story writer call %s thrown away: %s", write_id, error)
            return WriteOutcome.FAILED
        now = self._clock()
        if plan.mood is not None:  # character-state §2: the mood at the day's end — or now, if that is still to come
            plan = dataclasses.replace(plan, mood_at=min(now, day_bounds(day, self._zone)[1]))
        try:
            entries = await self._data.apply_life_write(write_id, plan, now=now)
        except DataError as error:
            if error.code not in (DataErrorCode.INVALID_ARGUMENT, DataErrorCode.TOO_LARGE):
                raise
            await self._data.fail_life_write(write_id, f"refused: {error.code.value}")
            log.warning("story writer call %s thrown away: %s", write_id, error.code.value)
            return WriteOutcome.FAILED
        log.info("story writer call %s wrote %d entries for %s", write_id, len(entries), day)
        return WriteOutcome.WRITTEN
