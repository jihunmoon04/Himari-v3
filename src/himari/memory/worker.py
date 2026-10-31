"""Memory passes run (V3-P85 §4): when, one at a time per principal, and what a failure does.

A Conversation is read once its job ended and it stayed quiet for the idle
time (a timer per Conversation, started by the Runner's job events), and by
a scan of Data's persistent state at startup and every hour, so a timer lost
to a restart loses nothing (§4.1). A pass starts only on a settled
Conversation — no job running here, nothing unfinished in Data — and Data
checks again when it applies the result (§4.4). One pass at a time per
principal, two in the server (§4.8). Every call's request is stored before
it is sent, and the tokens it may use are reserved against the principal's
daily budget first.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, time, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from himari.common import ConversationId, PrincipalId, TrustedScope
from himari.data.api import (
    ConversationEvent,
    DataError,
    DataErrorCode,
    MemoryCategory,
    MemoryPassLog,
    MemoryStore,
    PassKind,
    PassStart,
    PassStatus,
    RawNoResponse,
    RawReceived,
    RawUncertain,
)
from himari.memory.api import ActivityProbe, MemoryPassSettings, PassOutcome
from himari.memory.passes import (
    PASS_MAX_OUTPUT_TOKENS,
    AnswerError,
    answer_text,
    model_input,
    next_part,
    parse_answer,
    plan_episode,
    plan_items,
    request_data,
    reservation,
    shown_notes,
    waits,
)
from himari.prompt.api import ModelInput, RequestOptions
from himari.provider.api import NativeNoResponse, NativeReceived, NormalizedFinal, ProviderAdapter, ProviderError

log = logging.getLogger(__name__)

RETRY_DELAY = timedelta(minutes=30)  # §4.5: after a failure, not before
START_DELAY = 60.0  # seconds after startup before the first scan (§4.1)
SCAN_PAGE = 50
PASSES_AT_ONCE = 2  # in the server (§4.8)
MAX_PARTS_PER_READ = 20  # parts one read goes through before it lets others in
CALLS_PER_PASS = 2  # a malformed answer is asked once more (§4.3)


class MemoryPassData(MemoryStore, MemoryPassLog, Protocol):
    async def read_events(self, scope: TrustedScope, *, after_sequence: int = 0,
                          limit: int | None = None) -> Sequence[ConversationEvent]: ...


class MemoryPasses:
    """The Runner's job listener (V3-P83 §2.2) for memory, and the pass runner."""

    def __init__(self, data: MemoryPassData, provider: ProviderAdapter, settings: MemoryPassSettings,
                 probe: ActivityProbe, *, clock: Callable[[], datetime] | None = None,
                 zone_for: Callable[[TrustedScope], Awaitable[str]] | None = None) -> None:
        self._data = data
        self._provider = provider
        self._settings = settings
        self._probe = probe
        self._clock = clock or (lambda: datetime.now(UTC))
        self._zone_for = zone_for
        self._timers: dict[ConversationId, asyncio.TimerHandle] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._principals: dict[PrincipalId, asyncio.Lock] = {}
        self._slots = asyncio.Semaphore(PASSES_AT_ONCE)
        self._closed = False
        self._scanning: asyncio.Task[int] | None = None

    # --- the Runner's job events (JobListener) ------------------------------------

    def job_started(self, scope: TrustedScope, origin: object, status: object) -> None:
        self._cancel(scope)

    def accepted(self, scope: TrustedScope) -> None:
        self._cancel(scope)

    def run_ended(self, scope: TrustedScope) -> None:
        pass

    def job_ended(self, scope: TrustedScope, status: object) -> None:
        """§4.1: the quiet starts; a pass follows the idle time unless a job starts."""
        if self._closed:
            return
        self._cancel(scope)
        loop = asyncio.get_running_loop()
        self._timers[scope.conversation_id] = loop.call_later(
            self._settings.idle.total_seconds(), self._spawn, scope)

    def _cancel(self, scope: TrustedScope) -> None:
        timer = self._timers.pop(scope.conversation_id, None)
        if timer is not None:
            timer.cancel()

    def _spawn(self, scope: TrustedScope) -> None:
        self._timers.pop(scope.conversation_id, None)
        if not self._closed:
            self._track(asyncio.create_task(self._read_logged(scope), name=f"memory-pass-{scope.conversation_id}"))

    async def _read_logged(self, scope: TrustedScope) -> None:
        """A timer's read: nobody awaits it, so its trouble is logged here."""
        try:
            await self.read(scope)
        except Exception:  # noqa: BLE001 — the next scan reads it again
            log.exception("memory pass of %s failed", scope.conversation_id)

    def _track(self, task: asyncio.Task[Any]) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # --- lifecycle --------------------------------------------------------------------

    async def start(self) -> None:
        """§4.5: passes left RUNNING by a stop count as failures; the first scan follows."""
        interrupted = await self._data.interrupt_running_passes(self._clock() + RETRY_DELAY)
        if interrupted:
            log.warning("%d memory pass(es) were cut short by a stop; their parts are read again later", interrupted)
        self._track(asyncio.create_task(self._scan_later(), name="memory-pass-scan"))

    async def _scan_later(self) -> None:
        await asyncio.sleep(START_DELAY)
        await self.scan()

    async def close(self) -> None:
        self._closed = True
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def scan_soon(self) -> None:
        """A scan in the background, unless one is running (the hourly cleanup's)."""
        if self._closed or (self._scanning is not None and not self._scanning.done()):
            return
        self._scanning = asyncio.create_task(self.scan(), name="memory-pass-scan")
        self._track(self._scanning)

    async def scan(self) -> int:
        """§4.1: every Conversation with work, a page at a time, to the end;
        how many passes were applied."""
        applied = 0
        after: ConversationId | None = None
        while not self._closed:
            now = self._clock()
            page = await self._data.memory_due(quiet_before=now - self._settings.idle, now=now, after=after,
                                               limit=SCAN_PAGE)
            for due in page:
                try:
                    outcomes = await self.read(due.scope)
                except Exception:  # noqa: BLE001 — one Conversation's trouble is not the scan's
                    log.exception("memory pass of %s failed", due.scope.conversation_id)
                    continue
                applied += outcomes.count(PassOutcome.APPLIED)
            if len(page) < SCAN_PAGE:
                return applied
            after = page[-1].scope.conversation_id
        return applied

    # --- reading a Conversation ----------------------------------------------------

    async def read(self, scope: TrustedScope) -> list[PassOutcome]:
        """Live passes over the Conversation's unread parts, one after another,
        while each applies (a long part is cut in several, §4.1)."""
        lock = self._principals.setdefault(scope.principal_id, asyncio.Lock())
        outcomes: list[PassOutcome] = []
        async with lock, self._slots:
            for _ in range(MAX_PARTS_PER_READ):
                outcome = await self._live_pass(scope)
                outcomes.append(outcome)
                if outcome is not PassOutcome.APPLIED:
                    break
        return outcomes

    def _day_start(self, now: datetime) -> datetime:
        zone = ZoneInfo(self._settings.time_zone)
        return datetime.combine(now.astimezone(zone).date(), time(), tzinfo=zone).astimezone(UTC)

    async def _live_pass(self, scope: TrustedScope) -> PassOutcome:
        if self._closed:
            return PassOutcome.NOTHING
        if self._probe.running(scope):
            return PassOutcome.BUSY
        now = self._clock()
        try:
            if await self._data.unfinished_work(scope):
                return PassOutcome.BUSY
            cursor = await self._data.read_memory_cursor(scope)
            if cursor.retry_after is not None and cursor.retry_after > now:
                return PassOutcome.WAITING
            events = await self._data.read_events(scope, after_sequence=cursor.through_sequence)
        except DataError as error:
            if error.code in (DataErrorCode.NOT_AUTHORIZED, DataErrorCode.NOT_FOUND):
                return PassOutcome.NOTHING  # gone or trashed
            raise
        if not events:
            return PassOutcome.NOTHING
        if now - events[-1].created_at < self._settings.idle:
            return PassOutcome.WAITING
        part = next_part(events, cursor.through_sequence)
        if part is None:
            return PassOutcome.NOTHING
        notes = await self._data.list_memories(scope.principal_id)
        if waits(part, open_loops=any(n.category is MemoryCategory.LOOP for n in notes), now=now):
            return PassOutcome.WAITING
        previous = await self._data.last_episode(scope)
        shown = shown_notes(notes)
        zone = self._settings.time_zone if self._zone_for is None else await self._zone_for(scope)
        options = RequestOptions(self._settings.model_id, self._settings.reasoning_effort,
                                 provider_key=self._provider.provider_key, max_output_tokens=PASS_MAX_OUTPUT_TOKENS)
        asked = model_input(request_data(part, shown, previous, now=now, zone=zone, kind=PassKind.LIVE), options)
        reserve = reservation(self._provider.prepare_native_request(asked).body)
        if await self._data.pass_usage(scope.principal_id, self._day_start(now)) + reserve > self._settings.daily_tokens:
            return PassOutcome.DEFERRED
        try:
            pass_id = await self._data.begin_pass(scope, PassStart(
                PassKind.LIVE, cursor.through_sequence, part.through_sequence, events[-1].sequence,
                self._provider.provider_key, self._settings.model_id, part.omitted))
        except DataError as error:
            if error.code is DataErrorCode.NOT_FOUND:
                return PassOutcome.NOTHING
            if error.code is DataErrorCode.INVALID_STATE_TRANSITION:
                return PassOutcome.BUSY
            raise
        try:
            answer = await self._ask(scope, pass_id, asked, reserve)
        except (AnswerError, _CallFailed) as error:
            return await self._failed(pass_id, str(error))
        except asyncio.CancelledError:
            raise  # a stop: RUNNING stays, the next start counts it (§4.5)
        except DataError as error:
            if error.code is DataErrorCode.NOT_FOUND:  # deleted for good meanwhile, the pass with it
                return PassOutcome.NOTHING
            await self._failed(pass_id, type(error).__name__)
            raise
        except Exception as error:
            await self._failed(pass_id, type(error).__name__)
            raise
        items = plan_items(answer, part, shown, kind=PassKind.LIVE, now=now, zone=zone)
        episode = plan_episode(answer, part, previous, kind=PassKind.LIVE)
        result = await self._data.apply_pass(pass_id, items, episode, now=self._clock())
        if result.status is PassStatus.APPLIED:
            return PassOutcome.APPLIED
        log.info("memory pass %s of %s discarded: %s", pass_id, scope.conversation_id, result.reason)
        return PassOutcome.DISCARDED

    async def _failed(self, pass_id: str, error: str) -> PassOutcome:
        try:
            status = await self._data.fail_pass(pass_id, error, self._clock() + RETRY_DELAY)
        except DataError as gone:
            if gone.code is not DataErrorCode.NOT_FOUND:
                raise
            return PassOutcome.NOTHING  # its Conversation was deleted for good meanwhile
        log.warning("memory pass %s failed (%s)%s", pass_id, error,
                    "; its part is skipped" if status is PassStatus.SKIPPED else "")
        return PassOutcome.SKIPPED if status is PassStatus.SKIPPED else PassOutcome.FAILED

    async def _ask(self, scope: TrustedScope, pass_id: str, asked: ModelInput, reserve: int) -> dict[str, Any]:
        """At most CALLS_PER_PASS calls; the second only after a malformed answer
        and within the budget. Each request is stored before it is sent."""
        problem = "no call"
        for attempt in range(CALLS_PER_PASS):
            if attempt:
                used = await self._data.pass_usage(scope.principal_id, self._day_start(self._clock()))
                if used + reserve > self._settings.daily_tokens:
                    raise _CallFailed("daily budget")
            request = self._provider.prepare_native_request(asked)
            call = await self._data.prepare_pass_call(pass_id, request.body, request.content_type, reserve)
            await self._data.mark_pass_call_dispatched(pass_id, call)
            outcome = await self._provider.invoke_prepared(request)
            if isinstance(outcome, NativeReceived):
                await self._data.record_pass_call(pass_id, call, RawReceived(outcome.body, outcome.content_type,
                                                                             outcome.usage))
                try:
                    normalized = self._provider.normalize(outcome)
                    if not isinstance(normalized, NormalizedFinal):
                        raise AnswerError("tool calls instead of an answer")
                    return parse_answer(answer_text(normalized.blocks))
                except (AnswerError, ProviderError) as error:
                    problem = f"malformed answer: {error}"
                    continue
            if isinstance(outcome, NativeNoResponse):
                await self._data.record_pass_call(pass_id, call, RawNoResponse(outcome.error_code, outcome.body,
                                                                               outcome.content_type))
            else:
                await self._data.record_pass_call(pass_id, call, RawUncertain(outcome.error_code))
            raise _CallFailed(outcome.error_code)
        raise AnswerError(problem)


class _CallFailed(Exception):
    """The Provider gave no usable answer."""
