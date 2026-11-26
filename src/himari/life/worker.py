"""HIMARI's life runs (character-life v0.1 §3, character-mind v0.1 §4, §5.3, §14.5): when, for whom, and what a
failure does.

It runs as the server's periodic job ``life`` (``bootstrap.schedule``): each round, for every principal who sent a
message in the last ACTIVE_DAYS, one after another for the persona chosen now (one principal at a time, and at most
one call of each job a round): the work (character-work §4), the day's review and the facts settled from its diary.
Every call is a life write: its request is stored before it is sent, and the tokens it may use are reserved against
the principal's own daily budget first (L4: apart from the memory passes'). A call whose answer is out of shape is
recorded and thrown away; the next round tries again, ATTEMPTS_PER_DAY times a job and date (a work plan's: a plan).

character-mind §4.1, §4.3: the day's review is HIMARI's own — a self run: the system text and the runtime state a
turn has (``SelfComposer.self_input``), no history and no Tools, the review's task and data as the user turn, on the
day session's model (character-day §10; with none, the default model) (``MindLink``); which, and why, is recorded
with the write. character-day §7: it is the diary written in the day session's context — the request the session's
next turn would send (``MindLink.session_input``, ``SelfComposer.session_input``) with the task as one more user turn,
the day's episodes left out (the conversation is there) — and, with no day session for that date, a request over the
model's limit or one that cannot be composed, the self run as before. It thinks first; the thought is kept with the
write, never shown. character-diary §4: it writes tonight's diary at the first round after the night's diary slot
starts, or as HIMARI falls asleep first (the keeper wakes it), or — a night with no slot — at the write time or as it
falls asleep, whichever comes first (``review.diary_due``), unless its diary is there; a missed date is written at
the next round, an earlier one never. It is told the day's events whose time has passed before it fell asleep, the
night before's after it fell asleep then (carried over) and those inside that night's sleeps (``while_asleep``); its
write keeps what its request was made of (the session's last event, the moment). A closing that ended a night with no
diary (every try failed, the server was off) has that diary written in the closed day session's context, found from
what is stored, with tries of its own (§5).

character-diary §5: as the day session closes (the keeper's closing wakes the job ``amend``; the round looks again),
the last diary written in it is amended once — only when something new came between its writing and HIMARI falling
asleep (``review.amend_due``), never the user's own diary — in the closed session's context with AMEND_TASK: the whole
diary again (or nothing to add) and the mood if it changed; a closed session in the Trash is not read (the amend is
skipped). A request that cannot be composed is recorded as a failed try. §6: the memory model settles the facts each
diary newly holds (``settle``) once a closing ended its date and its amend is done, not needed, the user's or given up
(every such date of the last SETTLE_DAYS, the earliest first); without the day keeper (``LifeSettings.keeper``),
right after the review as before. A diary or an amend applied in a session is told (``on_diary``).

character-mind §7: before them all, once a day after the day was closed (character-day §6: the sleep's closing that
date; else from noon) and before the day ends (character-diary §7), the world decides on the memory model what
happens to HIMARI that day (0 to 2 events, at any hour): the review is told those whose time has passed, the work
those whose time has come, and a turn those whose time has passed (the runtime state's himari_life.events).

character-day §10, §18.7: a call whose job (for the date; a work step: for its plan) last ended with no answer at
all, and may be tried again (``retryable``), goes to the backup model (``LifeSettings.backup_model_id``), once a try.

character-world v0.1: the review is told the weather where the user is for the date (as last read, from memory) and
the day's topic titles (read once a date, only when a call is about to be made), and the persona as a turn tells it
— its hidden lines only from the bond stage they wait for (W4).

character-work v0.1 §4 (K1): the worker looks for the chosen persona's work plan whose work time ended within
WORK_WINDOW and has no piece, and makes that piece with one call on the memory model, at most ATTEMPTS_PER_DAY calls
a plan. A plan past its window is not worked any more.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from himari.common import SELF_THINK_CHARS, ConversationId, JsonValue, PrincipalId, TrustedScope, split_thought
from himari.data.api import (
    NO_PERSONA,
    AssistantFinal,
    BondSnapshot,
    BondStanding,
    ConversationEvent,
    DataError,
    DataErrorCode,
    DayState,
    HimariMood,
    InstructionScope,
    Instructions,
    LifeEntry,
    LifeJob,
    LifeKind,
    LifeOrigin,
    LifeStore,
    LifeWrite,
    LifeWriteLog,
    LifeWriteStart,
    LifeWriteStatus,
    Memory,
    PersistedStateBasis,
    Persona,
    RawNoResponse,
    RawReceived,
    RawUncertain,
    RecentEpisode,
    SleepRecord,
    UserMessage,
    live_mood,
)
from himari.life import review, settle, work, world
from himari.life.api import (
    ACTIVE_DAYS,
    ATTEMPTS_PER_DAY,
    LifeSettings,
    MindLink,
    MindModel,
    SessionInput,
    WriteOutcome,
)
from himari.life.day import last_words
from himari.life.review import (
    BYTES_PER_TOKEN,
    AnswerError,
    answer_text,
    day_bounds,
    diary_due,
    diary_of,
    reservation,
    self_answer,
)
from himari.prompt.api import (
    LIFE_READ_DAYS,
    RECENT_DAYS,
    RECENT_EPISODES,
    ModelInput,
    PromptInputs,
    RequestOptions,
    SelfComposer,
    SystemSpec,
    doing_now,
    persona_for_stage,
)
from himari.provider.api import (
    NativeNoResponse,
    NativePreparedRequest,
    NativeReceived,
    NormalizedFinal,
    ProviderAdapter,
    ProviderError,
    retryable,
)
from himari.world.api import TopicSource, WeatherSource

log = logging.getLogger(__name__)

START_DELAY = timedelta(seconds=60)  # after startup before the first round writes (as memory passes wait)
MEMORY_MODEL = "the memory model"  # §4.1: the reason recorded when no link chooses (a memory-model call too)
SLEEPS_READ = 5  # character-diary §4, §6: the latest sleeps read (the night before's, the closing's)
SETTLE_SLEEPS = 20  # §6: the latest sleeps settling reads (the closings of the last SETTLE_DAYS)
SETTLE_DAYS = 3  # §6: a closed date's facts are settled this many days back at most (the earliest first)
RECOVER_DAYS = 2  # §5: a closing's missing diary is written after it this many days back at most
WRITES_READ = 60  # §5: the latest life writes read to count a closing's recovery tries


class LifeWriterData(LifeStore, LifeWriteLog, Protocol):  # what the life runs read and write
    async def active_persona(self, principal: PrincipalId) -> Persona | None: ...

    async def find_episodes(self, principal: PrincipalId, words: Sequence[str], *, start: datetime | None = None,
                            end: datetime | None = None, limit: int = 200) -> tuple[RecentEpisode, ...]: ...

    async def bond_stage(self, principal: PrincipalId, persona_id: int) -> BondStanding: ...

    async def read_mood(self, principal: PrincipalId, persona_id: int) -> HimariMood | None: ...

    # character-mind §4.1: what a turn's system text and runtime state are made of
    async def read_instructions(self, principal: PrincipalId, scope: InstructionScope) -> Instructions: ...

    async def list_memories(self, principal: PrincipalId) -> tuple[Memory, ...]: ...

    async def recent_episodes(self, principal: PrincipalId, *, since: datetime, besides: ConversationId | None,
                              limit: int) -> tuple[RecentEpisode, ...]: ...

    async def count_episodes(self, principal: PrincipalId) -> int: ...

    async def read_bond(self, principal: PrincipalId, persona_id: int, *, now: datetime,
                        zone: str) -> BondSnapshot: ...

    # character-day §6, §7: the keeper's state (the last closing) and HIMARI's latest sleep
    async def read_day_state(self, principal: PrincipalId, persona_id: int) -> DayState: ...

    async def current_sleep(self, principal: PrincipalId, persona_id: int) -> SleepRecord | None: ...

    async def recent_sleeps(self, principal: PrincipalId, persona_id: int, *,
                            limit: int) -> tuple[SleepRecord, ...]: ...

    # character-diary §5: what was said in the closed day session after the diary
    async def read_events(self, scope: TrustedScope, *, after_sequence: int = 0,
                          limit: int | None = None) -> Sequence[ConversationEvent]: ...


@dataclasses.dataclass(frozen=True)
class _Mind:
    """§4.1: the model a self run uses, with its effort and why it was chosen."""

    model_id: str
    reasoning_effort: str | None
    reason: str


@dataclasses.dataclass(frozen=True)
class _AmendCase:
    """character-diary §5: the diary the last closing may amend — the last one written in the closed day session
    (``write``, of ``day``), as it stands (``diary``), from its writing (``since``) to HIMARI falling asleep
    (``until``), with what came meanwhile (``new``) and what that was read from."""

    day: str
    write: LifeWrite
    conversation: ConversationId
    since: datetime
    until: datetime
    diary: LifeEntry | None
    entries: tuple[LifeEntry, ...]
    events: tuple[ConversationEvent, ...]
    new: bool
    readable: bool = True  # the closed session can still be read (not in the Trash): else the amend is skipped


@dataclasses.dataclass(frozen=True)
class _Chosen:
    """The persona chosen now, as a turn tells it."""

    persona_id: int
    text: str  # W4: at the stage as stored ("" without one)
    makes: str  # character-persona-fields §4: its field ``makes`` at that stage ("" when not filled)


class LifeWriter:
    """The ``life`` job and one principal's calls."""

    def __init__(self, data: LifeWriterData, provider: ProviderAdapter, settings: LifeSettings, *,
                 composer: SelfComposer, instructions: Sequence[str] = (), system: SystemSpec | None = None,
                 link: MindLink | None = None, clock: Callable[[], datetime] | None = None,
                 weather: WeatherSource | None = None, topics: TopicSource | None = None) -> None:
        self._data = data
        self._provider = provider
        self.settings = settings
        # character-mind §4.1: a self run's system text is a turn's — the operator's base and these sections
        self._composer = composer
        self._instructions = tuple(instructions)
        self._system = system if system is not None else SystemSpec(companion=True, life=True)
        self._link = link
        self._weather = weather  # character-world §2: None: no weather in the input
        self._topics = topics  # §3: None: no topics (none configured, or HIMARI_LIFE_TOPICS=0)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._zone = ZoneInfo(settings.time_zone)
        self._principals: dict[PrincipalId, asyncio.Lock] = {}
        self._ready_at: datetime | None = None  # set by start(): rounds before it write nothing
        # character-diary §10: a diary (or its amend) applied in a day session's context — the composition root tells
        # the stream (the open timeline reads it again)
        self.on_diary: Callable[[PrincipalId, ConversationId], None] | None = None

    async def start(self) -> None:
        """Writes left open by a stop end as INTERRUPTED; rounds write from START_DELAY on."""
        interrupted = await self._data.interrupt_life_writes()
        if interrupted:
            log.warning("%d life call(s) were cut short by a stop; they are tried again", interrupted)
        self._ready_at = self._clock() + START_DELAY

    async def run(self) -> None:
        """One round of the job: every active principal's jobs, in order (character-mind §14.5)."""
        now = self._clock()
        if self._ready_at is None or now < self._ready_at:
            return
        jobs: tuple[tuple[str, Callable[[PrincipalId], Awaitable[WriteOutcome]]], ...] = (
            ("world", self.world), ("work_start", self.work_start), ("work_end", self.work), ("review", self.write),
            ("amend", self.amend), ("settle", self.settle))
        for principal in await self._data.life_principals(now - ACTIVE_DAYS):
            for name, job in jobs:
                try:
                    outcome = await job(principal)
                except Exception:  # noqa: BLE001 — one principal's or job's trouble is not the others'
                    log.exception("the life job %s failed for %s", name, principal)
                    continue
                if outcome is WriteOutcome.DEFERRED:
                    log.info("the life job %s waits for %s: the daily budget is used", name, principal)

    def budget_start(self, now: datetime) -> datetime:
        """The start of ``now``'s day in the server's zone: the daily budget's."""
        return datetime.combine(now.astimezone(self._zone).date(), time(), tzinfo=self._zone).astimezone(UTC)

    async def _locked(self, principal: PrincipalId,
                      job: Callable[[PrincipalId], Awaitable[WriteOutcome]]) -> WriteOutcome:
        lock = self._principals.setdefault(principal, asyncio.Lock())
        async with lock:
            return await job(principal)

    async def write(self, principal: PrincipalId) -> WriteOutcome:
        """character-mind §4.3, character-diary §4: the day's review of the latest date whose diary is due."""
        return await self._locked(principal, self._review)

    async def amend(self, principal: PrincipalId) -> WriteOutcome:
        """character-diary §5: the diary of the day session the last closing closed, amended once if anything new
        came after it."""
        return await self._locked(principal, self._amend)

    async def world(self, principal: PrincipalId) -> WriteOutcome:
        """§7: what happens to HIMARI today, once a day after its day was closed (character-day §6) or noon."""
        return await self._locked(principal, self._world)

    def next_world(self, now: datetime, closed_at: datetime | None = None) -> datetime:
        """§8: when the world is next decided at the latest (today's start if still ahead, else tomorrow's noon);
        character-day §6: a closing (``closed_at``, the last) brings that date's on."""
        local = now.astimezone(self._zone)
        start = world.world_start(local.date(), self._zone, closed_at)
        return start if local < start else world.world_start(local.date() + timedelta(days=1), self._zone)

    async def settle(self, principal: PrincipalId) -> WriteOutcome:
        """§5.3: the facts the latest reviewed date's diary settles (character-diary §6: once that day is closed)."""
        return await self._locked(principal, self._settle)

    async def work_start(self, principal: PrincipalId) -> WriteOutcome:
        """§4.4: the start step of the chosen persona's work plan that just started, if any."""
        return await self._locked(principal, self._work_start)

    async def work(self, principal: PrincipalId) -> WriteOutcome:
        """character-work §4, character-mind §4.4: the end step (the piece) of the chosen persona's due work plan."""
        return await self._locked(principal, self._work)

    async def mind_model(self, principal: PrincipalId) -> MindModel:
        """§4.1: the model a self run uses now (never None) and why: the link's choice, else the memory model."""
        mind = await self._mind(principal)
        return MindModel(mind.model_id, mind.reasoning_effort, mind.reason)

    async def _mind(self, principal: PrincipalId) -> _Mind:
        """character-day §10, §18.7: the day session's model; without one, the default model (the memory model
        when no default is given)."""
        chosen = MindModel(None, reason=MEMORY_MODEL) if self._link is None else await self._link.mind_model(principal)
        if chosen.model_id is None:
            if self.settings.default_model_id is not None:
                return _Mind(self.settings.default_model_id, self.settings.default_effort,
                             chosen.reason or "the default model")
            return _Mind(self.settings.model_id, self.settings.reasoning_effort, chosen.reason or MEMORY_MODEL)
        return _Mind(chosen.model_id, chosen.reasoning_effort, chosen.reason)

    async def _model(self, principal: PrincipalId, persona_id: int, day: str, job: LifeJob, *,
                     plan_id: int | None = None, mind: _Mind | None = None) -> _Mind:
        """character-day §10, §18.7: a call's model — ``mind`` (None: the memory model), or the backup model when
        the same job's last try (for the date; a work step: for its plan) got no answer at all and may be retried."""
        chosen = mind or _Mind(self.settings.model_id, self.settings.reasoning_effort, MEMORY_MODEL)
        if self.settings.backup_model_id is None:
            return chosen
        last = await self._data.last_life_write(principal, persona_id, day, job=job, plan_id=plan_id)
        if last is None or last.status is not LifeWriteStatus.FAILED or not last.error or not retryable(last.error):
            return chosen
        return _Mind(self.settings.backup_model_id, self.settings.backup_effort,
                     f"the backup model (the last try got no answer: {last.error[:80]})")

    async def _persona(self, principal: PrincipalId) -> _Chosen:
        """The chosen persona's id and text as a turn tells it (W4: at the stage as stored), and its field
        ``makes``."""
        chosen = await self._data.active_persona(principal)
        persona_id = NO_PERSONA if chosen is None else chosen.persona_id
        persona = "" if chosen is None else chosen.text
        makes = "" if chosen is None else chosen.fields.get("makes", "")
        if persona:
            stage = (await self._data.bond_stage(principal, persona_id)).stage
            persona = persona_for_stage(persona, stage)
            makes = persona_for_stage(makes, stage).strip()  # a marked line in it waits for its stage too (review)
        return _Chosen(persona_id, persona, makes)

    async def _self_inputs(self, principal: PrincipalId, chosen: _Chosen, options: RequestOptions,
                           now: datetime) -> PromptInputs:
        """§4.1: what a turn starting now would be composed from — the system text's parts (the base, the
        sections, the persona, the user's own text) and the runtime state (the notes and recent episodes,
        the life with the kept thoughts, the bond, the mood, what HIMARI is doing, the weather) — with no
        history and no Tools."""
        local = now.astimezone(self._zone)
        persona_id = chosen.persona_id
        life = await self._data.list_life(principal, persona_id, since=(local.date() - LIFE_READ_DAYS).isoformat(),
                                          now=now)
        memories = episodes = None
        count = 0
        if self._system.memory:
            memories = await self._data.list_memories(principal)
            episodes = await self._data.recent_episodes(principal, since=now - RECENT_DAYS, besides=None,
                                                        limit=RECENT_EPISODES)
            count = await self._data.count_episodes(principal)
        found = None if self._weather is None else self._weather.weather_on(local.date())
        return PromptInputs(
            instructions=self._instructions, tool_catalog=(), events=(), tool_executions=(), snapshot=None,
            state_basis=PersistedStateBasis(0, 0, ()), observations={}, request_options=options, trigger_refs=(),
            user_instructions=(await self._data.read_instructions(principal, InstructionScope.USER)).text,
            system=self._system, companion_persona=chosen.text, memories=memories, episodes=episodes or (),
            episode_count=count, now=local, time_zone=self.settings.time_zone, life=life,
            bond=await self._data.read_bond(principal, persona_id, now=now, zone=self.settings.time_zone),
            mood=live_mood(await self._data.read_mood(principal, persona_id), now),
            himari_now=doing_now(life, local), weather=None if found is None else found.line())

    async def _prepared(self, principal: PrincipalId, asked: ModelInput, output_tokens: int,
                        now: datetime) -> tuple[NativePreparedRequest, int] | None:
        """The request and its reservation, or None when it would pass the daily budget."""
        request = self._provider.prepare_native_request(asked)
        reserve = reservation(request.body, output_tokens)
        if await self._data.life_usage(principal, self.budget_start(now)) + reserve > self.settings.daily_tokens:
            return None
        return request, reserve

    async def _due(self, principal: PrincipalId, persona_id: int, now: datetime,
                   sleeps: Sequence[SleepRecord]) -> date:
        """character-diary §4, §16.3: the latest date whose diary is due (its diary slot, HIMARI's latest fall)."""
        local = now.astimezone(self._zone)
        plans = [e for e in await self._data.list_life(principal, persona_id,
                                                       since=(local.date() - timedelta(days=2)).isoformat(), now=now)
                 if e.kind is LifeKind.PLAN]
        return diary_due(now, self._zone, self.settings.write_at, plans, sleeps[0].fell_at if sleeps else None)

    def _slept(self, sleeps: Sequence[SleepRecord], day: date) -> list[tuple[datetime, datetime]]:
        """character-diary §7: the night before ``day``'s sleeps that ended, each [fell, woke) (never merged: the
        awake time between them was lived awake)."""
        before = day - timedelta(days=1)
        return sorted((s.fell_at, s.woke_at) for s in sleeps
                      if s.woke_at is not None and diary_of(s.fell_at, self._zone) == before)

    async def _carried_from(self, principal: PrincipalId, persona_id: int, day: date,
                            sleeps: Sequence[SleepRecord]) -> datetime | None:
        """character-diary §7: from when the day before's events belong to ``day``'s diary — when HIMARI first fell
        asleep that night (its diary had them up to there), else, with no sleep that night, the moment the day
        before's diary read its data up to; None: neither is known."""
        fell = self._fell_tonight(sleeps, day - timedelta(days=1))
        if fell is not None:
            return fell
        last = await self._data.last_life_write(principal, persona_id, (day - timedelta(days=1)).isoformat(),
                                                job=LifeJob.REVIEW)
        if last is None or last.status is not LifeWriteStatus.APPLIED:
            return None
        return last.basis_at or last.finished_at

    def _fell_tonight(self, sleeps: Sequence[SleepRecord], day: date) -> datetime | None:
        """character-diary §7: when HIMARI fell asleep in ``day``'s night (its first sleep then); None: not yet."""
        return min((s.fell_at for s in sleeps if diary_of(s.fell_at, self._zone) == day), default=None)

    async def _review(self, principal: PrincipalId) -> WriteOutcome:
        now = self._clock()
        chosen = await self._persona(principal)
        persona_id = chosen.persona_id
        sleeps = await self._data.recent_sleeps(principal, persona_id, limit=SLEEPS_READ)
        recovered = await self._recover(principal, chosen, sleeps, now)  # character-diary §5: after a closing
        if recovered is not None:
            return recovered
        day = await self._due(principal, persona_id, now, sleeps)
        written = day.isoformat()
        entries = await self._data.list_life(principal, persona_id, since=(day - review.READ_DAYS).isoformat(),
                                             now=now)
        if any(e.kind is LifeKind.DIARY and e.day == written for e in entries):
            return WriteOutcome.NOTHING
        if await self._data.life_attempts(principal, persona_id, written, job=LifeJob.REVIEW) >= ATTEMPTS_PER_DAY:
            return WriteOutcome.GAVE_UP
        # character-day §7: in the day session's context
        return await self._draft(principal, chosen, day, entries, sleeps, await self._session(principal, written),
                                 None, now)

    async def _recover(self, principal: PrincipalId, chosen: _Chosen, sleeps: Sequence[SleepRecord],
                       now: datetime) -> WriteOutcome | None:
        """character-diary §5, §16.4: the last closing ended a night with no diary (every evening try failed, or the
        server was off) — that date's diary is written in the closed session's context, from what is stored (a
        restart days later still finds it, within RECOVER_DAYS), with ATTEMPTS_PER_DAY tries of its own after the
        closing (the writes read after it). None: nothing to recover (or its tries are spent): the usual review."""
        persona_id = chosen.persona_id
        state = await self._data.read_day_state(principal, persona_id)
        before = state.before
        closed_at = state.last_closing_at
        if before is None or before.conversation_id is None or before.fell_at is None or closed_at is None:
            return None
        day = diary_of(before.fell_at, self._zone)
        written = day.isoformat()
        if day < now.astimezone(self._zone).date() - timedelta(days=RECOVER_DAYS):
            return None
        entries = await self._data.list_life(principal, persona_id, since=(day - review.READ_DAYS).isoformat(),
                                             now=now)
        if any(e.kind is LifeKind.DIARY and e.day == written for e in entries):
            return None
        tries = [w for w in await self._data.list_life_writes(principal, limit=WRITES_READ)
                 if w.job is LifeJob.REVIEW and w.persona_id == persona_id and w.day == written
                 and w.basis_at is not None and w.basis_at >= closed_at]
        if len(tries) >= ATTEMPTS_PER_DAY:
            return None
        session = None if self._link is None else await self._session_of(principal, before.conversation_id)
        if session is not None and session.day > written:
            session = None
        # the closed session resolved first: its own model (the backup after a try with no answer at all)
        first = None if session is None else _Mind(session.inputs.request_options.model_id,
                                                   session.inputs.request_options.reasoning_effort,
                                                   "the day session's model")
        log.info("the closing of %s's day left no diary of %s: written now", principal, written)
        return await self._draft(principal, chosen, day, entries, sleeps, session, first, now)

    async def _draft(self, principal: PrincipalId, chosen: _Chosen, day: date, entries: Sequence[LifeEntry],
                     sleeps: Sequence[SleepRecord], session: SessionInput | None, first: _Mind | None,
                     now: datetime) -> WriteOutcome:
        """character-mind §4.3, character-day §7, character-diary §4: one call writing ``day``'s diary — in
        ``session``'s context when it can be composed there (the day's episodes left out: the conversation is
        there), else a self run with them. Told the day's events up to when HIMARI fell asleep, the night before's
        carried over and its sleep's (``while_asleep``); its write keeps what it was made of (the session's last
        event, ``now``) for the amend. A request that cannot be composed is recorded as a failed try."""
        persona_id = chosen.persona_id
        written = day.isoformat()
        start, end = day_bounds(day, self._zone)
        found = None if self._weather is None else self._weather.weather_on(day)
        topics = () if self._topics is None else await self._topics.topics(day)  # character-world §3: once a date
        mind = await self._model(principal, persona_id, written, LifeJob.REVIEW,
                                 mind=first or await self._mind(principal))
        options = RequestOptions(mind.model_id, mind.reasoning_effort, provider_key=self._provider.provider_key,
                                 max_output_tokens=review.REVIEW_MAX_OUTPUT_TOKENS)
        fell = self._fell_tonight(sleeps, day)
        events = review.passed_events(entries, day, now if fell is None else min(now, fell), self._slept(sleeps, day),
                                      self._zone, since=await self._carried_from(principal, persona_id, day, sleeps))

        def data(episodes: Sequence[RecentEpisode]) -> dict[str, JsonValue]:
            return review.request_data(entries, episodes, day=day, now=now, zone=self._zone,
                                       weather=None if found is None else found.line(), topics=topics,
                                       makes=chosen.makes, events=events)

        conversation: ConversationId | None = None
        try:
            asked = None if session is None else self._in_session(session, options, data(()))
            if asked is not None and session is not None:
                conversation = session.conversation_id
            if asked is None:
                episodes = await self._data.find_episodes(principal, (), start=start, end=end)
                asked = self._composer.self_input(await self._self_inputs(principal, chosen, options, now),
                                                  review.REVIEW_TASK, data(episodes))
            prepared = await self._prepared(principal, asked, review.REVIEW_MAX_OUTPUT_TOKENS, now)
        except (DataError, asyncio.CancelledError):
            raise
        except Exception as error:  # noqa: BLE001 — never pending for ever: a try, recorded
            return await self._uncomposed(principal, persona_id, written, LifeJob.REVIEW, error, conversation, now)
        if prepared is None:
            return WriteOutcome.DEFERRED
        request, reserve = prepared
        held = None if conversation is None or session is None else max(
            (e.sequence for e in session.inputs.events), default=0)
        write_id = await self._data.begin_life_write(principal, LifeWriteStart(
            persona_id, written, self._provider.provider_key, mind.model_id, request.body, request.content_type,
            reserve, job=LifeJob.REVIEW, model_reason=mind.reason if conversation is None
            else f"{mind.reason}, in the day session", conversation_id=conversation, basis_sequence=held,
            basis_at=now))
        try:
            outcome = await self._reviewed(write_id, request, day, review.sleep_spans(entries, self._zone))
        except asyncio.CancelledError:
            raise  # a stop: the write stays open, the next start ends it as INTERRUPTED
        except Exception as error:
            await self._data.fail_life_write(write_id, type(error).__name__)
            raise
        if outcome is WriteOutcome.WRITTEN and conversation is not None:
            self._tell_diary(principal, conversation)
        return outcome

    async def _uncomposed(self, principal: PrincipalId, persona_id: int, day: str, job: LifeJob, error: Exception,
                          conversation: ConversationId | None, now: datetime) -> WriteOutcome:
        """A request that could not be composed: recorded as a failed try (nothing sent, nothing charged), so the
        job's ATTEMPTS_PER_DAY end it rather than leaving it due for ever."""
        log.warning("the life job %s for %s (%s) could not be composed: %s", job.value, principal, day, error)
        write_id = await self._data.begin_life_write(principal, LifeWriteStart(
            persona_id, day, self._provider.provider_key, "", b"", "", 0, job=job, model_reason="not composed",
            conversation_id=conversation, basis_at=now))
        await self._data.fail_life_write(write_id, f"not composed: {type(error).__name__}: {error}"[:200])
        return WriteOutcome.FAILED

    def _tell_diary(self, principal: PrincipalId, conversation: ConversationId) -> None:
        if self.on_diary is not None:
            try:
                self.on_diary(principal, conversation)
            except Exception:  # noqa: BLE001 — telling is not the diary
                log.exception("could not tell the diary of %s", principal)

    async def _session(self, principal: PrincipalId, written: str) -> SessionInput | None:
        """character-day §7, §18.7: the open day session to write ``written``'s diary in — one opened that date or
        before (a session opened later is another day's); None without one, or when it cannot be read now."""
        if self._link is None:
            return None
        try:
            session = await self._link.session_input(principal)
        except Exception:  # noqa: BLE001 — the diary is written without it
            log.exception("could not read %s's day session for the diary", principal)
            return None
        return None if session is None or session.day > written else session

    async def _session_of(self, principal: PrincipalId, conversation: ConversationId) -> SessionInput | None:
        """character-diary §5: that day session's next turn (closed too); None when it cannot be read now."""
        assert self._link is not None
        try:
            return await self._link.session_input(principal, conversation)
        except Exception:  # noqa: BLE001 — written without it
            log.exception("could not read %s's day session %s", principal, conversation)
            return None

    def _in_session(self, session: SessionInput, options: RequestOptions, data: dict[str, JsonValue], *,
                    task: str = review.REVIEW_TASK,
                    output_tokens: int = review.REVIEW_MAX_OUTPUT_TOKENS) -> ModelInput | None:
        """character-day §7: the diary's request in the session's context — its next turn's, with this call's model
        and output limit, the task one more user turn; None when it cannot be composed or would pass the model's
        limit (``limit_tokens``). character-diary §5: the amend's too (its ``task``)."""
        told = dataclasses.replace(session.inputs.request_options, model_id=options.model_id,
                                   reasoning_effort=options.reasoning_effort, provider_key=options.provider_key,
                                   max_output_tokens=options.max_output_tokens, stream=False)
        try:
            asked = self._composer.session_input(dataclasses.replace(session.inputs, request_options=told), task,
                                                 data)
            body = self._provider.prepare_native_request(asked).body
        except Exception as error:  # noqa: BLE001 — the diary is written without it
            log.warning("the diary is not written in day session %s: %s", session.conversation_id, error)
            return None
        if session.limit_tokens is not None and (len(body) / BYTES_PER_TOKEN + output_tokens > session.limit_tokens):
            log.info("the diary is not written in day session %s: over the model's limit", session.conversation_id)
            return None
        return asked

    async def _reviewed(self, write_id: str, request: NativePreparedRequest, day: date,
                        sleeps: Sequence[tuple[datetime, datetime]] = ()) -> WriteOutcome:
        body = await self._thought_and_json(write_id, request, "review")
        if body is None:
            return WriteOutcome.FAILED
        dropped: list[str] = []
        try:
            plan = review.parse_answer(body, day, sleeps, self._zone, dropped)
        except AnswerError as error:
            return await self._malformed(write_id, "review", error)
        if dropped:  # character-day §18.7: a plan over the sleep is left out alone, and recorded
            log.info("review %s left out %d plan(s) over HIMARI's sleep: %s", write_id, len(dropped),
                     "; ".join(t[:60] for t in dropped))
        now = self._clock()
        if plan.mood is not None:  # character-state §2: the mood at the day's end — or now, if that is still to come
            plan = dataclasses.replace(plan, mood_at=min(now, day_bounds(day, self._zone)[1]))
        try:
            entries = await self._data.apply_life_write(write_id, plan, now=now, zone=self.settings.time_zone)
        except DataError as error:
            return await self._refused(write_id, "review", error)
        log.info("review %s wrote %d entries for %s", write_id, len(entries), day)
        return WriteOutcome.WRITTEN

    async def _world(self, principal: PrincipalId) -> WriteOutcome:
        """§7: today's events on the memory model, once a date, after the day was closed (character-day §6: the
        sleep's closing that date, else noon) and before the review."""
        now = self._clock()
        active = await self._data.active_persona(principal)
        state = await self._data.read_day_state(principal, NO_PERSONA if active is None else active.persona_id)
        day = world.due_day(now, self._zone, state.last_closing_at)
        if day is None:
            return WriteOutcome.NOTHING
        written = day.isoformat()
        chosen = await self._persona(principal)
        persona_id = chosen.persona_id
        if await self._data.life_done(principal, persona_id, written, LifeJob.WORLD):
            return WriteOutcome.NOTHING
        if await self._data.life_attempts(principal, persona_id, written, job=LifeJob.WORLD) >= ATTEMPTS_PER_DAY:
            return WriteOutcome.GAVE_UP
        entries = await self._data.list_life(principal, persona_id,
                                             since=(day - timedelta(days=world.WORLD_DAYS)).isoformat(), now=now)
        found = None if self._weather is None else self._weather.weather_on(day)
        topics = () if self._topics is None else await self._topics.topics(day)
        mind = await self._model(principal, persona_id, written, LifeJob.WORLD)
        options = RequestOptions(mind.model_id, mind.reasoning_effort, provider_key=self._provider.provider_key,
                                 max_output_tokens=world.WORLD_MAX_OUTPUT_TOKENS)
        asked = review.model_input(world.WORLD_INSTRUCTIONS, world.request_data(
            chosen.text, entries, day=day, makes=chosen.makes, weather=None if found is None else found.line(),
            topics=topics, zone=self._zone), options, "life-world-input")
        prepared = await self._prepared(principal, asked, world.WORLD_MAX_OUTPUT_TOKENS, now)
        if prepared is None:
            return WriteOutcome.DEFERRED
        request, reserve = prepared
        write_id = await self._data.begin_life_write(principal, LifeWriteStart(
            persona_id, written, self._provider.provider_key, mind.model_id, request.body,
            request.content_type, reserve, job=LifeJob.WORLD, model_reason=mind.reason))
        try:
            text = await self._answer(write_id, request, "world")
            if text is None:
                return WriteOutcome.FAILED
            try:  # character-diary §7: at any hour (in its sleep, only what happens without it)
                events = world.parse_events(text)
            except AnswerError as error:
                return await self._malformed(write_id, "world", error)
            try:
                kept = await self._data.add_events(write_id, events, now=self._clock())
            except DataError as error:
                return await self._refused(write_id, "world", error)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._data.fail_life_write(write_id, type(error).__name__)
            raise
        log.info("world %s made %d event(s) for %s", write_id, len(kept), written)
        return WriteOutcome.WRITTEN

    async def _amend_case(self, principal: PrincipalId, persona_id: int, now: datetime) -> _AmendCase | None:
        """character-diary §5, §16.4: the diary the last closing may amend — the last one written in the day session
        it closed (``DayState.before``), from its writing to HIMARI falling asleep; None: the closing closed no
        session, or no diary was written in it."""
        state = await self._data.read_day_state(principal, persona_id)
        before = state.before
        if before is None or before.conversation_id is None:
            return None
        written = [w for w in await self._data.session_writes(principal, before.conversation_id)
                   if w.persona_id == persona_id and w.job is LifeJob.REVIEW and w.status is LifeWriteStatus.APPLIED
                   and w.finished_at is not None]
        until = before.fell_at or state.last_closing_at
        if not written or until is None:
            return None
        write = written[-1]
        assert write.finished_at is not None
        since = write.basis_at or write.finished_at  # what its request was made of (P1-6: never its end)
        day = date.fromisoformat(write.day)
        entries = await self._data.list_life(principal, persona_id, since=(day - timedelta(days=1)).isoformat(),
                                             now=now)
        diary = settle.written_diary(entries, write.day)
        try:
            events = tuple(await self._data.read_events(TrustedScope(principal, before.conversation_id)))
        except DataError as error:  # in the Trash (or gone): never read past its boundary — the amend is skipped
            if error.code not in (DataErrorCode.NOT_FOUND, DataErrorCode.NOT_AUTHORIZED):
                raise
            return _AmendCase(write.day, write, before.conversation_id, since, until, diary, tuple(entries), (),
                              False, readable=False)
        return _AmendCase(write.day, write, before.conversation_id, since, until, diary, tuple(entries), events,
                          review.amend_due(events, entries, since, until, self._zone,
                                           after_sequence=write.basis_sequence))

    async def _amend_pending(self, principal: PrincipalId, persona_id: int, case: _AmendCase) -> bool:
        """character-diary §5, §6: the amend is still to be made — something new came, the diary is there and not
        the user's, and no amend was applied or given up yet."""
        if not case.readable or not case.new or case.diary is None or case.diary.origin is LifeOrigin.USER:
            return False
        if await self._data.life_done(principal, persona_id, case.day, LifeJob.AMEND):
            return False
        return await self._data.life_attempts(principal, persona_id, case.day, job=LifeJob.AMEND) < ATTEMPTS_PER_DAY

    async def _amend(self, principal: PrincipalId) -> WriteOutcome:
        """character-diary §5: the closed day session's last diary amended once, in that session's context, when
        something new came after it before HIMARI fell asleep; never the user's diary."""
        now = self._clock()
        chosen = await self._persona(principal)
        persona_id = chosen.persona_id
        case = await self._amend_case(principal, persona_id, now)
        if case is not None and not case.readable:
            log.info("the amend of %s's diary of %s is skipped: its day session is not readable", principal, case.day)
        if case is None or not case.readable or case.diary is None or not case.new \
                or case.diary.origin is LifeOrigin.USER:
            return WriteOutcome.NOTHING
        if await self._data.life_done(principal, persona_id, case.day, LifeJob.AMEND):
            return WriteOutcome.NOTHING
        if await self._data.life_attempts(principal, persona_id, case.day, job=LifeJob.AMEND) >= ATTEMPTS_PER_DAY:
            return WriteOutcome.GAVE_UP
        session = None if self._link is None else await self._session_of(principal, case.conversation)
        # the diary's model: the day session's own (the backup after a try with no answer at all)
        first = (await self._mind(principal) if session is None else
                 _Mind(session.inputs.request_options.model_id, session.inputs.request_options.reasoning_effort,
                       "the day session's model"))
        mind = await self._model(principal, persona_id, case.day, LifeJob.AMEND, mind=first)
        options = RequestOptions(mind.model_id, mind.reasoning_effort, provider_key=self._provider.provider_key,
                                 max_output_tokens=review.AMEND_MAX_OUTPUT_TOKENS)
        try:
            data = review.amend_data(case.diary, case.entries, since=case.since, until=case.until, zone=self._zone)
            asked = None if session is None else self._in_session(session, options, data, task=review.AMEND_TASK,
                                                                  output_tokens=review.AMEND_MAX_OUTPUT_TOKENS)
            if asked is None:  # no context to amend in: a self run told what was said meanwhile
                said = [e for e in case.events if review.said_after(e, case.since, case.until,
                                                                    case.write.basis_sequence)]
                data = review.amend_data(case.diary, case.entries, since=case.since, until=case.until,
                                         zone=self._zone, said=[{"at": line.at, "who": line.who, "text": line.text}
                                                                for line in last_words(said, self._zone)])
                asked = self._composer.self_input(await self._self_inputs(principal, chosen, options, now),
                                                  review.AMEND_TASK, data)
            prepared = await self._prepared(principal, asked, review.AMEND_MAX_OUTPUT_TOKENS, now)
        except (DataError, asyncio.CancelledError):
            raise
        except Exception as error:  # noqa: BLE001 — never pending for ever: a try, recorded
            return await self._uncomposed(principal, persona_id, case.day, LifeJob.AMEND, error, case.conversation,
                                          now)
        if prepared is None:
            return WriteOutcome.DEFERRED
        request, reserve = prepared
        write_id = await self._data.begin_life_write(principal, LifeWriteStart(
            persona_id, case.day, self._provider.provider_key, mind.model_id, request.body, request.content_type,
            reserve, job=LifeJob.AMEND, model_reason=mind.reason if session is None
            else f"{mind.reason}, in the day session", conversation_id=case.conversation))
        try:
            body = await self._thought_and_json(write_id, request, "amend")
            if body is None:
                return WriteOutcome.FAILED
            try:
                plan = review.parse_amend(body)
            except AnswerError as error:
                return await self._malformed(write_id, "amend", error)
            try:
                kept = await self._data.apply_life_amend(write_id, case.day, plan.diary, mood=plan.mood,
                                                         mood_reason=plan.mood_reason, now=self._clock())
            except DataError as error:
                return await self._refused(write_id, "amend", error)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._data.fail_life_write(write_id, type(error).__name__)
            raise
        log.info("amend %s %s the diary of %s", write_id, "rewrote" if kept is not None else "left", case.day)
        if kept is not None:
            self._tell_diary(principal, case.conversation)
        return WriteOutcome.WRITTEN

    async def _settle_dates(self, principal: PrincipalId, persona_id: int, now: datetime) -> list[str]:
        """§5.3, character-diary §6: the dates whose facts may be settled now, earliest first. With the day keeper:
        every date of the last SETTLE_DAYS a closing has ended (a closing in a sleep of its night or later), each
        found from what is stored — not only the latest due one (a session over two nights ends both) — unless the
        last closing's amend of that date is still to be made. Without it: the latest date whose diary is due."""
        if not self.settings.keeper:
            sleeps = await self._data.recent_sleeps(principal, persona_id, limit=SLEEPS_READ)
            return [(await self._due(principal, persona_id, now, sleeps)).isoformat()]
        sleeps = await self._data.recent_sleeps(principal, persona_id, limit=SETTLE_SLEEPS)
        closed = [diary_of(s.fell_at, self._zone) for s in sleeps if s.closed_day]
        if not closed:
            return []
        today = now.astimezone(self._zone).date()
        dates = [today - timedelta(days=back) for back in range(SETTLE_DAYS, -1, -1)]
        case: _AmendCase | None = None
        read = False
        found: list[str] = []
        for day in (d for d in dates if any(c >= d for c in closed)):
            written = day.isoformat()
            if (not await self._data.life_done(principal, persona_id, written, LifeJob.REVIEW)
                    or await self._data.life_done(principal, persona_id, written, LifeJob.SETTLE)):
                continue
            if not read:
                case, read = await self._amend_case(principal, persona_id, now), True
            if case is not None and case.day == written and await self._amend_pending(principal, persona_id, case):
                continue
            found.append(written)
        return found

    async def _settle(self, principal: PrincipalId) -> WriteOutcome:
        """§5.3: once a date's review applied, once: its diary's facts, on the memory model. character-diary §6: with
        the day keeper, once that day was closed and its amend settled (``_settle_dates``); without it, at once."""
        now = self._clock()
        chosen = await self._data.active_persona(principal)
        persona_id = NO_PERSONA if chosen is None else chosen.persona_id
        outcome = WriteOutcome.NOTHING
        for day in await self._settle_dates(principal, persona_id, now):
            if (not await self._data.life_done(principal, persona_id, day, LifeJob.REVIEW)
                    or await self._data.life_done(principal, persona_id, day, LifeJob.SETTLE)):
                continue
            entries = await self._data.list_life(principal, persona_id, since=day, now=now)
            if settle.written_diary(entries, day) is None:
                continue
            if await self._data.life_attempts(principal, persona_id, day, job=LifeJob.SETTLE) >= ATTEMPTS_PER_DAY:
                outcome = WriteOutcome.GAVE_UP
                continue
            return await self._settle_day(principal, persona_id, day, entries, now)
        return outcome

    async def _settle_day(self, principal: PrincipalId, persona_id: int, day: str, entries: Sequence[LifeEntry],
                          now: datetime) -> WriteOutcome:
        diary = settle.written_diary(entries, day)
        assert diary is not None
        mind = await self._model(principal, persona_id, day, LifeJob.SETTLE)
        options = RequestOptions(mind.model_id, mind.reasoning_effort, provider_key=self._provider.provider_key,
                                 max_output_tokens=settle.SETTLE_MAX_OUTPUT_TOKENS)
        asked = review.model_input(settle.SETTLE_INSTRUCTIONS, settle.request_data(diary, entries), options,
                                   "life-settle-input")
        prepared = await self._prepared(principal, asked, settle.SETTLE_MAX_OUTPUT_TOKENS, now)
        if prepared is None:
            return WriteOutcome.DEFERRED
        request, reserve = prepared
        write_id = await self._data.begin_life_write(principal, LifeWriteStart(
            persona_id, day, self._provider.provider_key, mind.model_id, request.body, request.content_type,
            reserve, job=LifeJob.SETTLE, model_reason=mind.reason))
        try:
            text = await self._answer(write_id, request, "settle")
            if text is None:
                return WriteOutcome.FAILED
            try:
                facts = settle.parse_facts(text)
            except AnswerError as error:
                return await self._malformed(write_id, "settle", error)
            try:
                added = await self._data.apply_settle_write(write_id, facts, now=self._clock())
            except DataError as error:
                return await self._refused(write_id, "settle", error)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._data.fail_life_write(write_id, type(error).__name__)
            raise
        log.info("settle %s kept %d fact(s) of %s", write_id, len(added), day)
        return WriteOutcome.WRITTEN

    async def _work_start(self, principal: PrincipalId) -> WriteOutcome:
        """§4.4: within WORK_START_WINDOW of a work plan's start, HIMARI begins: a self run leaving a draft."""
        return await self._work_step(principal, LifeJob.WORK_START)

    async def _work(self, principal: PrincipalId) -> WriteOutcome:
        """§4.4: once the work time ended, HIMARI finishes the piece: a self run from the draft, if any."""
        return await self._work_step(principal, LifeJob.WORK_END)

    async def _work_step(self, principal: PrincipalId, job: LifeJob) -> WriteOutcome:
        now = self._clock()
        local = now.astimezone(self._zone)
        chosen = await self._persona(principal)
        persona_id = chosen.persona_id
        entries = await self._data.list_life(principal, persona_id,
                                             since=(local.date() - timedelta(days=1)).isoformat(), now=now)
        start = job is LifeJob.WORK_START
        due = (work.starts_due if start else work.ends_due)(entries, now, self._zone)
        if not due:
            return WriteOutcome.NOTHING
        # review P1-7: the earliest due plan with tries left; one given up never blocks the next
        plan = None
        for candidate in due:
            if await self._data.work_attempts(principal, candidate.entry_id, job=job) < ATTEMPTS_PER_DAY:
                plan = candidate
                break
        if plan is None:
            return WriteOutcome.GAVE_UP
        project = next(e for e in entries if e.entry_id == plan.arc_id)
        draft = None if start else work.draft_of(entries, plan)
        remarks: tuple[str, ...] = ()
        if not start and self._link is not None:  # §4.4: what the user said while HIMARI worked
            began = draft.created_at if draft is not None else work.plan_start(plan, self._zone)
            if began is not None:
                remarks = tuple(await self._link.remarks(principal, began, now))
        data = work.step_data(entries, plan, project, now=now, zone=self._zone, makes=chosen.makes,
                              events=world.passed_events(entries, local), draft=draft, remarks=remarks)
        tokens = work.START_MAX_OUTPUT_TOKENS if start else work.WORK_MAX_OUTPUT_TOKENS
        assert plan.day is not None  # a work plan has its date (work_plans)
        mind = await self._model(principal, persona_id, plan.day, job, plan_id=plan.entry_id,
                                 mind=await self._mind(principal))
        options = RequestOptions(mind.model_id, mind.reasoning_effort, provider_key=self._provider.provider_key,
                                 max_output_tokens=tokens)
        asked = self._composer.self_input(await self._self_inputs(principal, chosen, options, now),
                                          work.WORK_START_TASK if start else work.WORK_END_TASK, data)
        prepared = await self._prepared(principal, asked, tokens, now)
        if prepared is None:
            return WriteOutcome.DEFERRED
        request, reserve = prepared
        write_id = await self._data.begin_life_write(principal, LifeWriteStart(
            persona_id, plan.day, self._provider.provider_key, mind.model_id, request.body, request.content_type,
            reserve, plan_id=plan.entry_id, job=job, model_reason=mind.reason))
        try:
            # review P0-3: the draft as read; a user's edit meanwhile makes the piece stale
            return await self._worked(write_id, request, job,
                                      None if draft is None else (draft.entry_id, draft.revision))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._data.fail_life_write(write_id, type(error).__name__)
            raise

    async def _worked(self, write_id: str, request: NativePreparedRequest, job: LifeJob,
                      draft_read: tuple[int, int] | None = None) -> WriteOutcome:
        who = job.value
        body = await self._thought_and_json(write_id, request, who)
        if body is None:
            return WriteOutcome.FAILED
        try:
            made: tuple[LifeEntry, ...]
            if job is LifeJob.WORK_START:
                made = (await self._data.apply_work_start(write_id, work.parse_start(body), now=self._clock()),)
            else:
                made = await self._data.apply_work_write(write_id, work.parse_piece(body), now=self._clock(),
                                                         draft_read=draft_read)
        except AnswerError as error:
            return await self._malformed(write_id, who, error)
        except DataError as error:
            return await self._refused(write_id, who, error)
        log.info("%s call %s wrote piece %d", who, write_id, made[0].entry_id)
        return WriteOutcome.WRITTEN

    async def _malformed(self, write_id: str, who: str, error: AnswerError) -> WriteOutcome:
        await self._data.fail_life_write(write_id, f"malformed answer: {error}")
        log.warning("%s call %s thrown away: %s", who, write_id, error)
        return WriteOutcome.FAILED

    async def _refused(self, write_id: str, who: str, error: DataError) -> WriteOutcome:
        if error.code is DataErrorCode.VERSION_CONFLICT:  # review P0-3: stale; the next round reads it again
            await self._data.fail_life_write(write_id, "stale: the draft changed")
            log.info("%s call %s thrown away: the draft changed meanwhile", who, write_id)
            return WriteOutcome.FAILED
        if error.code not in (DataErrorCode.INVALID_ARGUMENT, DataErrorCode.TOO_LARGE):
            raise error
        await self._data.fail_life_write(write_id, f"refused: {error.code.value}")
        log.warning("%s call %s thrown away: %s", who, write_id, error.code.value)
        return WriteOutcome.FAILED

    async def _thought_and_json(self, write_id: str, request: NativePreparedRequest, who: str) -> str | None:
        """§4.1, §14.5: a self run's answer — its thought kept with the write, its JSON's text returned;
        None (recorded as failed) when there is no answer or no JSON after the thought."""
        text = await self._answer(write_id, request, who)
        if text is None:
            return None
        try:
            thought, body = self_answer(text)
        except AnswerError as error:
            thought = split_thought(text, limit=SELF_THINK_CHARS)[0]
            if thought is not None:
                await self._data.note_life_thought(write_id, thought)
            await self._malformed(write_id, who, error)
            return None
        if thought is not None:
            await self._data.note_life_thought(write_id, thought)
        return body

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

