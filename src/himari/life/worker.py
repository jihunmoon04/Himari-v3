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
model of the conversation a wake would go to when it is checked for free wakes (``MindLink``), else on the memory
model; which, and why, is recorded with the write. It thinks first; the thought is kept with the write, never
shown. It writes the latest date whose write time has come (L3), unless its diary is there; a missed date is written
at the next round, an earlier one never. §5.3: once a date's review applied, the memory model settles the facts its
diary newly holds (``settle``).

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

from himari.common import SELF_THINK_CHARS, ConversationId, PrincipalId, split_thought
from himari.data.api import (
    NO_PERSONA,
    BondSnapshot,
    BondStanding,
    DataError,
    DataErrorCode,
    HimariMood,
    InstructionScope,
    Instructions,
    LifeJob,
    LifeKind,
    LifeStore,
    LifeWriteLog,
    LifeWriteStart,
    Memory,
    PersistedStateBasis,
    Persona,
    RawNoResponse,
    RawReceived,
    RawUncertain,
    RecentEpisode,
    live_mood,
)
from himari.life import review, settle, work
from himari.life.api import ACTIVE_DAYS, ATTEMPTS_PER_DAY, LifeSettings, MindLink, MindModel, WriteOutcome
from himari.life.review import AnswerError, answer_text, day_bounds, reservation, self_answer, target_day
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
)
from himari.world.api import TopicSource, WeatherSource

log = logging.getLogger(__name__)

START_DELAY = timedelta(seconds=60)  # after startup before the first round writes (as memory passes wait)
MEMORY_MODEL = "the memory model"  # §4.1: the reason recorded when no link chooses (a memory-model call too)


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


@dataclasses.dataclass(frozen=True)
class _Mind:
    """§4.1: the model a self run uses, with its effort and why it was chosen."""

    model_id: str
    reasoning_effort: str | None
    reason: str


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
                 weather: WeatherSource | None = None, topics: TopicSource | None = None,
                 quiet: tuple[time, time] | None = None) -> None:
        self._data = data
        self._quiet = quiet  # character-work §4: no work plan in HIMARI's sleeping hours (S2: asleep then)
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
            ("work", self.work), ("review", self.write), ("settle", self.settle))
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
        """character-mind §4.3: the day's review of the latest date whose write time has come."""
        return await self._locked(principal, self._review)

    async def settle(self, principal: PrincipalId) -> WriteOutcome:
        """§5.3: the facts the latest reviewed date's diary settles."""
        return await self._locked(principal, self._settle)

    async def work(self, principal: PrincipalId) -> WriteOutcome:
        """character-work §4: the piece of the chosen persona's due work plan, if any."""
        return await self._locked(principal, self._work)

    async def mind_model(self, principal: PrincipalId) -> MindModel:
        """§4.1: the model a self run uses now (never None) and why: the link's choice, else the memory model."""
        mind = await self._mind(principal)
        return MindModel(mind.model_id, mind.reasoning_effort, mind.reason)

    async def _mind(self, principal: PrincipalId) -> _Mind:
        chosen = MindModel(None, reason=MEMORY_MODEL) if self._link is None else await self._link.mind_model(principal)
        if chosen.model_id is None:
            return _Mind(self.settings.model_id, self.settings.reasoning_effort, chosen.reason or MEMORY_MODEL)
        return _Mind(chosen.model_id, chosen.reasoning_effort, chosen.reason)

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
            himari_now=doing_now(life, local, self._quiet), weather=None if found is None else found.line())

    async def _prepared(self, principal: PrincipalId, asked: ModelInput, output_tokens: int,
                        now: datetime) -> tuple[NativePreparedRequest, int] | None:
        """The request and its reservation, or None when it would pass the daily budget."""
        request = self._provider.prepare_native_request(asked)
        reserve = reservation(request.body, output_tokens)
        if await self._data.life_usage(principal, self.budget_start(now)) + reserve > self.settings.daily_tokens:
            return None
        return request, reserve

    async def _review(self, principal: PrincipalId) -> WriteOutcome:
        now = self._clock()
        day = target_day(now, self._zone, self.settings.write_at)
        written = day.isoformat()
        chosen = await self._persona(principal)
        persona_id = chosen.persona_id
        entries = await self._data.list_life(principal, persona_id, since=(day - review.READ_DAYS).isoformat(),
                                             now=now)
        if any(e.kind is LifeKind.DIARY and e.day == written for e in entries):
            return WriteOutcome.NOTHING
        if await self._data.life_attempts(principal, persona_id, written, job=LifeJob.REVIEW) >= ATTEMPTS_PER_DAY:
            return WriteOutcome.GAVE_UP
        start, end = day_bounds(day, self._zone)
        episodes = await self._data.find_episodes(principal, (), start=start, end=end)
        found = None if self._weather is None else self._weather.weather_on(day)
        topics = () if self._topics is None else await self._topics.topics(day)  # character-world §3: once a date
        data = review.request_data(entries, episodes, day=day, now=now, zone=self._zone,
                                   weather=None if found is None else found.line(), topics=topics, quiet=self._quiet,
                                   makes=chosen.makes)
        mind = await self._mind(principal)
        options = RequestOptions(mind.model_id, mind.reasoning_effort, provider_key=self._provider.provider_key,
                                 max_output_tokens=review.REVIEW_MAX_OUTPUT_TOKENS)
        asked = self._composer.self_input(await self._self_inputs(principal, chosen, options, now), review.REVIEW_TASK,
                                          data)
        prepared = await self._prepared(principal, asked, review.REVIEW_MAX_OUTPUT_TOKENS, now)
        if prepared is None:
            return WriteOutcome.DEFERRED
        request, reserve = prepared
        write_id = await self._data.begin_life_write(principal, LifeWriteStart(
            persona_id, written, self._provider.provider_key, mind.model_id, request.body, request.content_type,
            reserve, job=LifeJob.REVIEW, model_reason=mind.reason))
        try:
            return await self._reviewed(write_id, request, day)
        except asyncio.CancelledError:
            raise  # a stop: the write stays open, the next start ends it as INTERRUPTED
        except Exception as error:
            await self._data.fail_life_write(write_id, type(error).__name__)
            raise

    async def _reviewed(self, write_id: str, request: NativePreparedRequest, day: date) -> WriteOutcome:
        body = await self._thought_and_json(write_id, request, "review")
        if body is None:
            return WriteOutcome.FAILED
        try:
            plan = review.parse_answer(body, day, self._quiet)
        except AnswerError as error:
            return await self._malformed(write_id, "review", error)
        now = self._clock()
        if plan.mood is not None:  # character-state §2: the mood at the day's end — or now, if that is still to come
            plan = dataclasses.replace(plan, mood_at=min(now, day_bounds(day, self._zone)[1]))
        try:
            entries = await self._data.apply_life_write(write_id, plan, now=now)
        except DataError as error:
            return await self._refused(write_id, "review", error)
        log.info("review %s wrote %d entries for %s", write_id, len(entries), day)
        return WriteOutcome.WRITTEN

    async def _settle(self, principal: PrincipalId) -> WriteOutcome:
        """§5.3: after the latest date's review applied, once: its diary's facts, on the memory model."""
        now = self._clock()
        day = target_day(now, self._zone, self.settings.write_at).isoformat()
        chosen = await self._data.active_persona(principal)
        persona_id = NO_PERSONA if chosen is None else chosen.persona_id
        if (not await self._data.life_done(principal, persona_id, day, LifeJob.REVIEW)
                or await self._data.life_done(principal, persona_id, day, LifeJob.SETTLE)):
            return WriteOutcome.NOTHING
        entries = await self._data.list_life(principal, persona_id, since=day, now=now)
        diary = settle.written_diary(entries, day)
        if diary is None:
            return WriteOutcome.NOTHING
        if await self._data.life_attempts(principal, persona_id, day, job=LifeJob.SETTLE) >= ATTEMPTS_PER_DAY:
            return WriteOutcome.GAVE_UP
        options = RequestOptions(self.settings.model_id, self.settings.reasoning_effort,
                                 provider_key=self._provider.provider_key,
                                 max_output_tokens=settle.SETTLE_MAX_OUTPUT_TOKENS)
        asked = review.model_input(settle.SETTLE_INSTRUCTIONS, settle.request_data(diary, entries), options,
                                   "life-settle-input")
        prepared = await self._prepared(principal, asked, settle.SETTLE_MAX_OUTPUT_TOKENS, now)
        if prepared is None:
            return WriteOutcome.DEFERRED
        request, reserve = prepared
        write_id = await self._data.begin_life_write(principal, LifeWriteStart(
            persona_id, day, self._provider.provider_key, self.settings.model_id, request.body, request.content_type,
            reserve, job=LifeJob.SETTLE, model_reason=MEMORY_MODEL))
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

    async def _work(self, principal: PrincipalId) -> WriteOutcome:
        now = self._clock()
        today = now.astimezone(self._zone).date()
        chosen = await self._persona(principal)
        persona_id = chosen.persona_id
        entries = await self._data.list_life(principal, persona_id, since=(today - timedelta(days=1)).isoformat(),
                                             now=now)
        plan = work.due_plan(entries, now, self._zone)
        if plan is None:
            return WriteOutcome.NOTHING
        if await self._data.work_attempts(principal, plan.entry_id, job=LifeJob.WORK_END) >= ATTEMPTS_PER_DAY:
            return WriteOutcome.GAVE_UP
        project = next(e for e in entries if e.entry_id == plan.arc_id)
        mood = live_mood(await self._data.read_mood(principal, persona_id), now)
        options = RequestOptions(self.settings.model_id, self.settings.reasoning_effort,
                                 provider_key=self._provider.provider_key,
                                 max_output_tokens=work.WORK_MAX_OUTPUT_TOKENS)
        asked = work.model_input(work.request_data(chosen.text, entries, plan, project, now=now, zone=self._zone,
                                                   mood=mood, makes=chosen.makes), options)
        prepared = await self._prepared(principal, asked, work.WORK_MAX_OUTPUT_TOKENS, now)
        if prepared is None:
            return WriteOutcome.DEFERRED
        request, reserve = prepared
        assert plan.day is not None  # a work plan has its date (work_plans)
        write_id = await self._data.begin_life_write(principal, LifeWriteStart(
            persona_id, plan.day, self._provider.provider_key, self.settings.model_id, request.body,
            request.content_type, reserve, plan_id=plan.entry_id, job=LifeJob.WORK_END, model_reason=MEMORY_MODEL))
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
            return await self._malformed(write_id, "worker", error)
        try:
            made = await self._data.apply_work_write(write_id, piece, now=self._clock())
        except DataError as error:
            return await self._refused(write_id, "worker", error)
        log.info("worker call %s made piece %d", write_id, made[0].entry_id)
        return WriteOutcome.WRITTEN

    async def _malformed(self, write_id: str, who: str, error: AnswerError) -> WriteOutcome:
        await self._data.fail_life_write(write_id, f"malformed answer: {error}")
        log.warning("%s call %s thrown away: %s", who, write_id, error)
        return WriteOutcome.FAILED

    async def _refused(self, write_id: str, who: str, error: DataError) -> WriteOutcome:
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

