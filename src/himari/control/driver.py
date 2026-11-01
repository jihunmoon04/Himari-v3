"""Conversation driver: the P0 Run loop (CTRL-01, CPP §3).

The per-Conversation ``asyncio.Lock`` only prevents two drivers inside this
live process. Crash duplication is prevented by Data's persisted marks
(``mark_dispatch`` before send, Dispatcher claims before effects) and by
``recover_interrupted`` at the start of every drive (V3-045).

Context budget (PROMPT-04): with a ``ContextBudget``, every Run's input is
estimated first. At ``compact_at`` the older history is summarized by one
tool-less COMPACTION Run (raw request/response kept like any Run) and the
summary is committed as the latest snapshot; the input is then composed
again. A summarizing request is itself never sent over ``limit_tokens``;
when the whole prefix does not fit one, it is folded in several
compactions (at most ``MAX_COMPACTIONS_PER_RUN`` before a Run), and a
part too large for one request is summarized in a chain of calls. If
compaction fails, the Run still goes ahead when its input fits the
budget's ``ceiling_tokens`` (the model's window less the room kept for its
answer); an input estimated over it is never sent (REQUEST_REJECTED).

Pause and cancel (V3-P55) come from the ``StopSignal`` of the job. A pause
is checked at the safe boundaries — before each Run, each summarizing call
and each Tool call starts — so the model call or Tool call under way
finishes and is recorded; the drive ends PAUSED, never-started calls stay
PENDING for ``resume``, and a compaction stopped between its calls commits
nothing. A cancel interrupts the model call or Tool call under way: an
interrupted model call is recorded as OUTCOME_UNKNOWN and never resent (it
may have been processed), an interrupted Tool call is settled by
``cancel_group`` (never-started → CANCELLED, claimed → uncertain), and the
drive ends CANCELLED. A cancel is checked once more right before each
commit of a model's output — its projection, a compaction snapshot — and
before a group's result is returned: a response that arrived together with
the cancel is kept as raw but never committed. A commit that passed that
check is atomic (Data commits never yield) and completes; the cancel
applies after it. Stop requests outrank the Run limit.

A turn (V3-P62) has no Run limit unless one is set (``max_runs_per_drive``).
It is guarded instead by a token budget (``turn_budget``, checked before
every request — model Runs and summarizing calls — against what the drive
used, the request's estimate and an output reserve: BUDGET_REACHED), by
progress (``no_progress_runs`` Runs in a row that brought nothing new:
NO_PROGRESS; nothing is ever kept from running) and, through the Runner, by
a pause when no client reads the Conversation. Stop requests outrank them
all. The drive's state (``control.turn``) is told to the model as data in the
runtime state, and to the Runner after every Run (``TurnProgress``). Every
request carries a per-Conversation ``session_key`` (OpenRouter
``session_id``), so the provider's cache can serve the growing history.

The model (V3-P64 §5) is fixed for a turn: once the Conversation's lock is
held, its stored choice is read and resolved against the ``ModelCatalog``
into one ``ResolvedModel`` — request options, context budget, output
reserve — which every Run and summarizing call of the turn uses. A choice
changed meanwhile applies from the next turn. ``context_status`` resolves
without the lock: it shows what the next turn would use.

Streaming (V3-P73 §5): with ``streaming``, a Run that answers the user asks
for a stream (summarizing calls never do); the Provider streams it only for
a model that declares it. A streamed Run gets a ``RawCapture`` the Driver
owns and an ``OutputProgress`` sink (when the job's progress is one).
However the invocation ends — a cancel, an exception, shutdown — the Driver
reads the capture only after the Provider's task has ended and records what
arrived with the outcome, in one ``record_raw_outcome``; ``output_ended``
follows it. Only a process crash loses the part that arrived.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from himari.common import ASK_USER_TOOL_ID, CallRef, ConversationId, RunId, TrustedScope
from himari.control.api import (
    ContextStatus,
    ControlError,
    ControlErrorCode,
    DriveResult,
    DriveState,
    ModelCatalog,
    OutputProgress,
    PresenceSource,
    ResolvedModel,
    StopSignal,
    TurnPresence,
    TurnProgress,
)
from himari.control.turn import Turn
from himari.data.api import (
    AssistantFinal,
    DataError,
    DataErrorCode,
    ConversationEvent,
    AssistantToolCalls,
    CompletedGroupRef,
    ConversationLog,
    ConversationModelSettings,
    EventKind,
    EventRef,
    InstructionScope,
    InstructionsStore,
    Memory,
    RecentEpisode,
    MemoryStore,
    MessageOrigin,
    RawNoResponse,
    RawReceived,
    RawRunOutcome,
    RawUncertain,
    RecoveryReport,
    RunPreparation,
    RunPurpose,
    ToolExecutionStatus,
    ToolResultRecord,
    TriggerRef,
    UserMessage,
    Wake,
    WakeReason,
    WakeStore,
    WakeStatus,
)
from himari.prompt.api import (
    RECENT_DAYS,
    RECENT_EPISODES,
    CompactionPlan,
    CompactionPlanner,
    ContextBudget,
    ModelInput,
    PromptComposer,
    PromptContextLoader,
    PromptError,
    PromptErrorCode,
    PromptInputs,
    RequestOptions,
)
from himari.provider.api import (
    NativeInvocationOutcome,
    NativeNoResponse,
    NativePreparedRequest,
    NativeReceived,
    NativeUncertain,
    NormalizedFinal,
    NormalizedModelOutput,
    OutputDelta,
    ProviderAdapter,
    ProviderError,
    RawCapture,
)
from himari.tool.api import GroupDispatchResult, GroupState, ToolDispatcher


MAX_COMPACTIONS_PER_RUN = 4
CANCELLED_BY_USER = "cancelled_by_user"  # the error code of a model call interrupted by a cancel
SHUTDOWN = "shutdown"  # of a streamed model call the server stopped (V3-P73 §5.3)

# Model-facing results of calls that never ran (V3-P55).
CANCELLED_RESULT = "Not run: the user cancelled before this call started."
REDIRECTED_RESULT = "Not run: the user sent a new message before this call started."
DISMISSED_RESULT = "The user dismissed this question without answering it."


class _Cancelled(Exception):
    """A cancel interrupted the step under way (internal to the driver)."""

    def __init__(self, run_id: RunId | None = None) -> None:
        super().__init__(CANCELLED_BY_USER)
        self.run_id = run_id


class _Paused(Exception):
    """A pause stopped a compaction between its calls (internal to the driver)."""


class _BudgetReached(Exception):
    """The next request would pass the turn's token budget (internal to the driver)."""


NO_PROGRESS_RUNS = 3  # V3-P62 O3
PAUSED_COMPACTING = "paused between summarizing calls; nothing was committed"


def sent_from(events: Sequence[ConversationEvent]) -> MessageOrigin | None:
    """V3-P83 §6.2 (re-review C1): the origin of the last user message that no
    assistant Event follows — a question's answer too, which its call's result
    follows in the same transaction. None: there is none (a resume after an
    answer), or it came before origins were recorded."""
    for event in sorted(events, key=lambda e: e.sequence, reverse=True):
        if event.kind in (EventKind.ASSISTANT_FINAL, EventKind.ASSISTANT_TOOL_CALLS):
            return None
        if isinstance(event.payload, UserMessage):
            return event.payload.origin
    return None


def session_key(scope: TrustedScope) -> str:
    """V3-P62 §9 B1: an opaque, stable key per Conversation (never its id)."""
    return hashlib.sha256(f"himari-session:{scope.conversation_id}".encode()).hexdigest()[:32]


class _UserContext(NamedTuple):
    """What the loader takes about the user and the turn's start, in its parameter order."""

    instructions: str  # V3-P69
    memories: tuple[Memory, ...] | None  # V3-P71; None: memory is off
    now: datetime | None  # V3-P83 §6.3; None: the Driver has no time zone
    episodes: tuple[RecentEpisode, ...] = ()  # V3-P85 §8: of other Conversations, lately
    episode_count: int = 0
    persona: str = ""  # V3-P89 §4: the companion persona, read with the instructions


class Driver:
    def __init__(
        self,
        log: ConversationLog,
        loader: PromptContextLoader,
        composer: PromptComposer,
        provider: ProviderAdapter,
        dispatcher: ToolDispatcher,
        models: ModelCatalog,
        *,
        settings: ConversationModelSettings | None = None,  # None: every Conversation uses the default model
        user_instructions: InstructionsStore | None = None,  # None: no user instructions (V3-P69)
        wakes: WakeStore | None = None,  # V3-P87: server-opened turns; None: none are accepted
        memories: MemoryStore | None = None,  # None: memory is off (V3-P71)
        max_runs_per_drive: int | None = 16,
        planner: CompactionPlanner | None = None,
        turn_budget: int | None = None,
        no_progress_runs: int | None = None,  # None: no NO_PROGRESS stop (V3-P62 §4 turns it on with O3)
        streaming: bool = False,  # V3-P73: answering Runs ask for a stream
        time_zone: str | None = None,  # V3-P83 §6.4: the zone the turn's time is shown in; None: not shown
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        presence: PresenceSource | None = None,  # V3-P83 §6.2: where the user is; None: not told
    ) -> None:
        if any((planner is None) != (offer.budget is None) for offer in models.offers):
            raise ValueError("a context budget needs a compaction planner and vice versa, for every model")
        if max_runs_per_drive is None and turn_budget is None:
            raise ValueError("a turn needs a token budget or a Run limit (V3-P62)")
        if turn_budget is not None and planner is None:
            raise ValueError("a turn token budget needs a context budget: it uses its request estimates")
        if any(v is not None and v < 1 for v in (max_runs_per_drive, turn_budget, no_progress_runs)):
            raise ValueError("Run limits, budgets and the no-progress count are positive")
        self._log = log
        self._loader = loader
        self._composer = composer
        self._provider = provider
        self._dispatcher = dispatcher
        self._models = models
        self._settings = settings
        self._user_instructions = user_instructions
        self._memories = memories
        self._max_runs = max_runs_per_drive
        self._wakes = wakes
        self._limits: dict[ConversationId, int | None] = {}  # this drive's Run limit (V3-P87 §4.2)
        self._turn_budget = turn_budget
        self._no_progress_runs = no_progress_runs
        self._planner = planner
        self._streaming = streaming
        self._zone = None if time_zone is None else ZoneInfo(time_zone)
        self._clock = clock
        self._presence = presence
        # Where the user is, read at the turn's first request — after its message
        # is recorded and the restart cleanup is done (V3-P83 §6.3).
        self._turn_presences: dict[ConversationId, TurnPresence] = {}  # of the drive holding the lock
        self._outputs: dict[ConversationId, OutputProgress | None] = {}  # of the drive holding the lock
        self._locks: dict[ConversationId, asyncio.Lock] = {}
        self._signals: dict[ConversationId, StopSignal] = {}  # of the drive holding the lock
        self._turns: dict[ConversationId, Turn] = {}  # of the drive holding the lock
        self._turn_models: dict[ConversationId, ResolvedModel] = {}  # of the drive holding the lock
        # The user's instructions and notes, read once when the turn started (V3-P69 §5,
        # V3-P71 §5.4): one turn, one text, and Conversations running at once each keep their own.
        self._turn_users: dict[ConversationId, _UserContext] = {}  # of the drive holding the lock

    async def drive(self, scope: TrustedScope, message: UserMessage, *, stop: StopSignal | None = None,
                    progress: TurnProgress | None = None) -> DriveResult:
        async with self._exclusive(scope, stop, progress):
            return await self._drive_locked(scope, message)

    async def resume(self, scope: TrustedScope, *, stop: StopSignal | None = None,
                     progress: TurnProgress | None = None, max_runs: int | None = None) -> DriveResult:
        """``max_runs``: a tighter Run limit for this drive (V3-P87 §4.2: a wake's turn)."""
        async with self._exclusive(scope, stop, progress, max_runs):
            return await self._resume_locked(scope)

    async def accept_wake(self, scope: TrustedScope, reasons: Sequence[WakeReason], device_id: str, *,
                          keys: Sequence[str] = (), reminders: Sequence[int] = ()) -> ConversationEvent:
        if self._wakes is None:
            raise ControlError(ControlErrorCode.NOT_SETTLED, "this server keeps no wakes")
        async with self._exclusive(scope):
            _, stop = await self._prepare(scope)
            triggers = await self._next_triggers(scope)
            unanswered = any(isinstance(t, EventRef) for t in triggers)  # a user message or a wake (§6.1)
            if (stop is not None or unanswered or await self._log.find_waiting_call(scope) is not None
                    or await self._unfinished_group(scope) is not None
                    or (triggers and not await self._left_by_a_wake(scope))):
                raise ControlError(ControlErrorCode.NOT_SETTLED, scope.conversation_id)
            return await self._wakes.begin_wake(scope, reasons, device_id, keys=keys, reminders=reminders)

    async def _left_by_a_wake(self, scope: TrustedScope) -> bool:
        """V3-P87 §6.1, §13 (PR #153 review P2-1): a finished tool group the model has
        not answered blocks a new wake unless an ended wake's turn left it (closed at
        a restart, or stopped at its Run cap): the next turn shows its results. Left
        by the user's turn (e.g. paused between Runs), it is the user's work: blocked."""
        assert self._wakes is not None
        events = await self._log.read_events(scope)
        last = next((e for e in reversed(events) if isinstance(e.payload, (UserMessage, Wake))), None)
        if last is None or not isinstance(last.payload, Wake):
            return False
        record = await self._wakes.read_wake(scope.principal_id, last.payload.wake_id)
        return record.status is not WakeStatus.OPENED

    async def close_cut_group(self, scope: TrustedScope, reason: str) -> bool:
        async with self._exclusive(scope):
            await self._prepare(scope)
            if await self._unfinished_group(scope) is None:
                return False
            await self._close_latest_group(scope, reason)
            return True

    async def cancel(self, scope: TrustedScope) -> DriveResult:
        async with self._exclusive(scope):
            recovery, stop = await self._prepare(scope)
            if stop is not None:
                return stop
            waiting = await self._log.find_waiting_call(scope)
            if waiting is not None:
                group: GroupDispatchResult | None = await self._dispatcher.cancel_group(
                    scope, waiting.run_id, DISMISSED_RESULT)
            else:
                group = await self._close_latest_group(scope, CANCELLED_RESULT)
            if group is None:
                return DriveResult(DriveState.IDLE, recovery=recovery)
            if group.state is GroupState.BLOCKED_UNKNOWN:
                return DriveResult(DriveState.RECOVERY_REQUIRED, unknown_calls=group.unknown_calls, recovery=recovery)
            return DriveResult(DriveState.CANCELLED, recovery=recovery)

    async def compact(self, scope: TrustedScope, *, stop: StopSignal | None = None,
                      progress: TurnProgress | None = None) -> DriveResult:
        async with self._exclusive(scope, stop, progress):
            recovery, halted = await self._prepare(scope)
            if halted is not None:
                return halted
            budget = self._turn_model(scope).offer.budget
            if self._planner is None or budget is None:
                return DriveResult(DriveState.NOTHING_TO_COMPACT, recovery=recovery, detail="no context budget")
            waiting = await self._log.find_waiting_call(scope)
            if waiting is not None:  # the open group cannot be folded yet
                return DriveResult(DriveState.WAITING_FOR_USER, waiting_call=waiting, recovery=recovery,
                                   detail="answer the question before compacting")
            try:
                inputs = await self._situated(scope, await self._loader.load(
                    scope, (), self._options_for(scope), *self._turn_user(scope)))
                model_input = self._compose(scope, inputs)
            except PromptError as exc:
                state = (DriveState.RECOVERY_REQUIRED if exc.code is PromptErrorCode.INCOMPLETE_HISTORY
                         else DriveState.REQUEST_REJECTED)
                return DriveResult(state, recovery=recovery, detail=str(exc))
            plan = self._planner.plan_now(inputs, model_input, budget)
            if plan is None:
                return DriveResult(DriveState.NOTHING_TO_COMPACT, recovery=recovery,
                                   detail="nothing older than the latest turn, or too large to summarize")
            compactions: list[RunId] = []
            try:
                failure = await self._compact(scope, plan, self._planner, budget, compactions)
            except _Cancelled:
                return DriveResult(DriveState.CANCELLED, recovery=recovery, compaction_run_ids=tuple(compactions),
                                   detail="cancelled while summarizing; nothing was committed")
            except _Paused:
                return DriveResult(DriveState.PAUSED, recovery=recovery, compaction_run_ids=tuple(compactions),
                                   detail=self._paused_detail(scope, PAUSED_COMPACTING))
            except _BudgetReached as reached:
                return DriveResult(DriveState.BUDGET_REACHED, recovery=recovery,
                                   compaction_run_ids=tuple(compactions), detail=f"{reached}; nothing was committed")
            return DriveResult(
                DriveState.COMPACTION_FAILED if failure is not None else DriveState.COMPACTED,
                recovery=recovery, detail=failure or "", compaction_run_ids=tuple(compactions),
            )

    async def context_status(self, scope: TrustedScope) -> ContextStatus | None:
        """CTRL-07: the size estimate the next Run would start from, and the
        latest compaction, for the model the next turn would use (V3-P64 §5).
        Read-only; no lock (display may be a moment stale)."""
        model = await self._resolve_model(scope)
        budget = model.offer.budget
        if self._planner is None or budget is None:
            return None
        # The next turn's context: today's instructions and notes, not a running turn's (V3-P69 §5).
        inputs = await self._situated(scope, await self._loader.load(scope, (), self._request_options(scope, model),
                                                                     *await self._read_user(scope)), turn=False)
        summary = self._planner.summary_of(inputs.snapshot)
        snapshot = inputs.snapshot if summary is not None else None
        tokens: int | None = None
        measured = False
        try:
            estimate = self._planner.estimate(inputs, self._composer.compose(inputs))
            tokens, measured = estimate.tokens, estimate.measured
        except PromptError:
            pass  # e.g. a Tool group still running: no size to show now
        return ContextStatus(
            estimated_tokens=tokens,
            measured=measured,
            budget_tokens=budget.limit_tokens,
            compact_at_tokens=int(budget.compact_at * budget.limit_tokens),
            hard_limit_tokens=budget.ceiling_tokens,
            summary=summary,
            covered_through_sequence=snapshot.covers_through_sequence if snapshot is not None else None,
            compactions=snapshot.revision if snapshot is not None else 0,
        )

    async def accept(self, scope: TrustedScope, message: UserMessage) -> None:
        """Record one user input durably — T7a if an ask_user call is waiting,
        else T1 — after restart cleanup and projection of stored responses,
        without running any Tool or calling the Provider. ``resume`` then
        continues from it (CTRL-06: an interface may report acceptance only
        once the input is committed)."""
        async with self._exclusive(scope):
            await self._prepare(scope)  # a stop is reported by the following resume
            waiting = await self._log.find_waiting_call(scope)
            if waiting is not None:
                await self._log.answer_pending_call(scope, waiting, message)  # T7a
            else:
                await self._close_latest_group(scope, REDIRECTED_RESULT)  # blocked calls: resume reports them
                await self._log.append_event(scope, message)  # T1

    @contextlib.asynccontextmanager
    async def _exclusive(self, scope: TrustedScope, stop: StopSignal | None = None,
                         progress: TurnProgress | None = None, max_runs: int | None = None) -> AsyncIterator[None]:
        lock = self._locks.setdefault(scope.conversation_id, asyncio.Lock())
        if lock.locked():
            raise ControlError(ControlErrorCode.BUSY, scope.conversation_id)
        async with lock:
            model = await self._resolve_model(scope)  # once per turn (V3-P64 §5)
            user = await self._read_user(scope)  # the user's instructions and notes, and the time (V3-P69 §5)
            self._turn_models[scope.conversation_id] = model
            self._turn_users[scope.conversation_id] = user
            self._signals[scope.conversation_id] = stop if stop is not None else StopSignal()
            limit = self._max_runs if max_runs is None else (
                max_runs if self._max_runs is None else min(max_runs, self._max_runs))
            self._limits[scope.conversation_id] = limit
            self._turns[scope.conversation_id] = Turn(self._turn_budget, limit, progress,
                                                      model.offer.max_output_tokens)
            self._outputs[scope.conversation_id] = progress if isinstance(progress, OutputProgress) else None
            try:
                yield
            finally:
                del self._outputs[scope.conversation_id]
                del self._signals[scope.conversation_id]
                del self._turns[scope.conversation_id]
                self._limits.pop(scope.conversation_id, None)
                del self._turn_models[scope.conversation_id]
                del self._turn_users[scope.conversation_id]
                self._turn_presences.pop(scope.conversation_id, None)

    def _stop(self, scope: TrustedScope) -> StopSignal:
        return self._signals[scope.conversation_id]

    def _turn(self, scope: TrustedScope) -> Turn:
        return self._turns[scope.conversation_id]

    async def _resolve_model(self, scope: TrustedScope) -> ResolvedModel:
        """The model a turn starting now would use: the stored choice,
        resolved against the catalog. Authorizes like any read."""
        setting = await self._settings.read_model_setting(scope) if self._settings is not None else None
        return self._models.resolve(setting)

    async def _read_user(self, scope: TrustedScope) -> _UserContext:
        text = persona = ""
        if self._user_instructions is not None:
            text = (await self._user_instructions.read_instructions(scope.principal_id, InstructionScope.USER)).text
            persona = (await self._user_instructions.read_instructions(
                scope.principal_id, InstructionScope.COMPANION_PERSONA)).text
        now = self._clock()
        if self._memories is None:
            return _UserContext(text, None, None if self._zone is None else now.astimezone(self._zone), persona=persona)
        notes = await self._memories.list_memories(scope.principal_id)
        episodes = await self._memories.recent_episodes(scope.principal_id, since=now - RECENT_DAYS,
                                                        besides=scope.conversation_id, limit=RECENT_EPISODES)
        count = await self._memories.count_episodes(scope.principal_id)
        return _UserContext(text, notes, None if self._zone is None else now.astimezone(self._zone), episodes, count,
                            persona)

    async def _situated(self, scope: TrustedScope, inputs: PromptInputs, *, turn: bool = True) -> PromptInputs:
        """``inputs`` with where the user is (V3-P83 §6.2) and the time in the
        zone of the device the message came from (§6.4) — read once per turn
        (``turn``), or now for the next turn's context."""
        if self._presence is None:
            return inputs
        found = self._turn_presences.get(scope.conversation_id) if turn else None
        if found is None:
            found = await self._presence.read(scope.principal_id, sent_from(inputs.events))
            if turn:
                self._turn_presences[scope.conversation_id] = found
        now = inputs.now
        if now is not None and found.time_zone is not None:
            try:
                now = now.astimezone(ZoneInfo(found.time_zone))
            except (ValueError, ZoneInfoNotFoundError):
                pass  # the server's zone, then
        return dataclasses.replace(inputs, presence=found.view, now=now)

    def _turn_user(self, scope: TrustedScope) -> _UserContext:
        return self._turn_users[scope.conversation_id]

    def _turn_model(self, scope: TrustedScope) -> ResolvedModel:
        return self._turn_models[scope.conversation_id]

    def _request_options(self, scope: TrustedScope, model: ResolvedModel) -> RequestOptions:
        capabilities = self._provider.describe_capabilities(model.model_id)
        return RequestOptions(model.model_id, model.reasoning_effort, provider_key=self._provider.provider_key,
                              session_key=session_key(scope), image_input=capabilities.image_input,
                              screen_points=capabilities.image_input and capabilities.screen_points)

    def _options_for(self, scope: TrustedScope) -> RequestOptions:
        return self._request_options(scope, self._turn_model(scope))

    def _compose(self, scope: TrustedScope, inputs: PromptInputs) -> ModelInput:
        """The model's input, with the turn's state as data (V3-P62 §8) — only
        under a turn budget, where the system instructions carry its rule."""
        model_input = self._composer.compose(inputs)
        if self._turn_budget is None:
            return model_input
        return dataclasses.replace(model_input, turn_state=self._turn(scope).state())

    def _paused_detail(self, scope: TrustedScope, what: str = "") -> str:
        """A pause's detail: its reason when it was not the user's own (V3-P62 §5)."""
        reason = self._stop(scope).reason
        return "; ".join(part for part in (reason, what) if part)

    def _invocation(self, prepared: NativePreparedRequest, run_id: RunId, capture: RawCapture | None,
                    live: OutputProgress | None) -> Callable[[], Awaitable[NativeInvocationOutcome]]:
        """The one send of a Run; a streamed one with its capture and sink."""
        if not prepared.streamed:
            return lambda: self._provider.invoke_prepared(prepared)

        def show(delta: OutputDelta) -> None:
            if live is not None:
                live.output(run_id, delta)

        return lambda: self._provider.invoke_prepared(prepared, show, capture)

    async def _interruptible[T](self, scope: TrustedScope, start: Callable[[], Awaitable[T]]) -> T:
        """Await ``start()`` unless a cancel arrives first; then cancel it,
        wait until it has cleaned up (a Sandbox kills and reaps its process
        tree) and raise ``_Cancelled``. A job cancelled from outside
        (shutdown) cancels it too."""
        signal = self._stop(scope)
        if signal.cancel_requested:
            raise _Cancelled()
        work = asyncio.ensure_future(start())
        stopper = asyncio.ensure_future(signal.cancelled())
        try:
            await asyncio.wait((work, stopper), return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            work.cancel()
            await asyncio.wait([work])
            raise
        finally:
            stopper.cancel()
        if work.done():
            return work.result()  # finished first: the cancel is noticed at the next boundary
        work.cancel()
        await asyncio.wait([work])
        if not work.cancelled():
            work.exception()  # retrieved: it failed while being cancelled
        raise _Cancelled()

    async def _drive_locked(self, scope: TrustedScope, message: UserMessage) -> DriveResult:
        recovery, stop = await self._prepare(scope)
        triggers: tuple[TriggerRef, ...]
        waiting = await self._log.find_waiting_call(scope)
        if waiting is not None:
            await self._log.answer_pending_call(scope, waiting, message)  # T7a
            triggers = (CompletedGroupRef(waiting.run_id),)
        else:
            group = await self._close_latest_group(scope, REDIRECTED_RESULT) if stop is None else None
            event = await self._log.append_event(scope, message)  # T1: input is never dropped
            if group is not None and group.state is GroupState.BLOCKED_UNKNOWN:
                return DriveResult(DriveState.RECOVERY_REQUIRED, unknown_calls=group.unknown_calls, recovery=recovery)
            triggers = (EventRef(event.event_id),)
        if stop is not None:
            return stop
        return await self._run_loop(scope, triggers, recovery)

    async def _resume_locked(self, scope: TrustedScope) -> DriveResult:
        recovery, stop = await self._prepare(scope)
        if stop is not None:
            return stop
        waiting = await self._log.find_waiting_call(scope)
        if waiting is not None:
            return DriveResult(DriveState.WAITING_FOR_USER, waiting_call=waiting, recovery=recovery)
        group = await self._resume_pending_group(scope)
        if group is not None and group.state is not GroupState.COMPLETED:
            return self._group_stop(group, lambda state, **kw: DriveResult(state, recovery=recovery, **kw),
                                    self._paused_detail(scope))
        triggers = await self._next_triggers(scope)
        if not triggers:
            return DriveResult(DriveState.IDLE, recovery=recovery)
        return await self._run_loop(scope, triggers, recovery)

    async def _prepare(self, scope: TrustedScope) -> tuple[RecoveryReport, DriveResult | None]:
        """Restart cleanup (T-R), then project responses that were stored but
        not projected before a crash — re-normalizing the stored bytes, never
        calling the Provider again. Returns a stop result if one cannot be
        projected safely."""
        recovery = await self._log.recover_interrupted(scope)
        for run_id in recovery.runs_received_unprojected:
            problem = await self._project_stored_response(scope, run_id)
            if problem is not None:
                return recovery, DriveResult(DriveState.RECOVERY_REQUIRED, recovery=recovery, detail=problem)
        return recovery, None

    async def _project_stored_response(self, scope: TrustedScope, run_id: RunId) -> str | None:
        run = await self._log.read_run(scope, run_id)
        if run.provider_key != self._provider.provider_key:
            return f"{run_id}: stored {run.provider_key} response cannot be read by {self._provider.provider_key}"
        raw = await self._log.read_raw_response(scope, run_id)
        try:
            normalized = self._provider.normalize(NativeReceived(raw.body, raw.content_type, raw.usage))
        except ProviderError as exc:
            await self._log.record_projection_failure(scope, run_id, exc.code.value)
            return None  # like a live invalid output: raw kept, nothing enters history
        await self._project(scope, run_id, normalized)
        return None

    async def _project(
        self, scope: TrustedScope, run_id: RunId, normalized: NormalizedModelOutput
    ) -> ConversationEvent:
        if isinstance(normalized, NormalizedFinal):
            output: AssistantFinal | AssistantToolCalls = AssistantFinal(normalized.blocks)
        else:
            output = AssistantToolCalls(normalized.calls, normalized.commentary)
        projection = await self._log.project_model_output(
            scope, run_id, output, continuation=normalized.continuation
        )
        return projection.event

    async def _next_triggers(self, scope: TrustedScope) -> tuple[TriggerRef, ...]:
        """What the next Run would answer, from the persisted tail: the
        latest tool group (if the tail follows one) and every ordinary user
        message after the last assistant output — an input accepted before
        an unfinished group completed still triggers the Run. Empty if the
        last assistant output is a FINAL with nothing after it."""
        events = await self._log.read_events(scope)
        triggers: list[TriggerRef] = []
        for event in reversed(events):
            match event.payload:
                case AssistantFinal():
                    break
                case AssistantToolCalls() if event.origin_run_id is not None:
                    triggers.append(CompletedGroupRef(event.origin_run_id))
                    break
                case UserMessage(answers_call=None) | Wake():  # V3-P87 §3.2: a wake is a trigger too
                    triggers.append(EventRef(event.event_id))
        return tuple(reversed(triggers))

    async def _unfinished_group(self, scope: TrustedScope) -> tuple[RunId, AssistantToolCalls] | None:
        """The latest group if it has never-claimed PENDING calls or uncertain
        ones the model was not told about; a lone waiting ``ask_user`` is
        not unfinished work (it waits for the user)."""
        events = await self._log.read_events(scope)
        last_group = next(
            (e for e in reversed(events) if e.kind is EventKind.ASSISTANT_TOOL_CALLS), None
        )
        if last_group is None or last_group.origin_run_id is None:
            return None
        if not isinstance(last_group.payload, AssistantToolCalls):
            return None
        records = await self._log.read_tool_executions(scope, last_group.origin_run_id)
        unfinished = [
            r
            for r in records
            if r.status is ToolExecutionStatus.PENDING
            or (r.status is ToolExecutionStatus.UNKNOWN and r.model_result is None)
        ]
        if not unfinished or (len(records) == 1 and records[0].tool_id == ASK_USER_TOOL_ID):
            return None
        return last_group.origin_run_id, last_group.payload

    async def _resume_pending_group(self, scope: TrustedScope) -> GroupDispatchResult | None:
        """Finish the latest group: run never-claimed PENDING calls (D20) and
        report UNKNOWN ones to the model (V3-047). A pause stops it before
        the next call (PAUSED); a cancel settles it (``_dispatch``)."""
        group = await self._unfinished_group(scope)
        if group is None:
            return None
        return await self._dispatch(scope, *group)

    async def _close_latest_group(self, scope: TrustedScope, reason: str) -> GroupDispatchResult | None:
        """V3-P55: settle the latest group without running anything more —
        never-started calls get a CANCELLED result saying ``reason``."""
        group = await self._unfinished_group(scope)
        if group is None:
            return None
        return await self._dispatcher.cancel_group(scope, group[0], reason)

    async def _dispatch(self, scope: TrustedScope, run_id: RunId, payload: AssistantToolCalls) -> GroupDispatchResult:
        """Run a group's calls, stopping before the next one on a pause; a
        cancel interrupts the call under way and closes the rest (COMPLETED
        or BLOCKED_UNKNOWN: the next boundary sees the cancel)."""
        signal = self._stop(scope)
        try:
            group = await self._interruptible(
                scope, lambda: self._dispatcher.dispatch_group(scope, run_id, payload.calls,
                                                               pause=lambda: signal.pause_requested))
        except _Cancelled:
            return await self._dispatcher.cancel_group(scope, run_id, CANCELLED_RESULT)
        if signal.cancel_requested:  # arrived as the group stopped (e.g. PAUSED): close what did not start
            return await self._dispatcher.cancel_group(scope, run_id, CANCELLED_RESULT)
        return group

    @staticmethod
    def _group_stop(group: GroupDispatchResult, build: Callable[..., DriveResult], paused: str = "") -> DriveResult:
        """The result of a drive stopped by a group that is not COMPLETED."""
        if group.state is GroupState.BLOCKED_UNKNOWN:
            return build(DriveState.RECOVERY_REQUIRED, unknown_calls=group.unknown_calls)
        if group.state is GroupState.WAITING_FOR_USER:
            return build(DriveState.WAITING_FOR_USER, waiting_call=group.waiting_call)
        return build(DriveState.PAUSED, detail=paused)

    async def _run_loop(
        self, scope: TrustedScope, triggers: tuple[TriggerRef, ...], recovery: RecoveryReport
    ) -> DriveResult:
        run_ids: list[RunId] = []
        compactions: list[RunId] = []

        def result(
            state: DriveState,
            *,
            final_event: ConversationEvent | None = None,
            waiting_call: CallRef | None = None,
            unknown_calls: tuple[CallRef, ...] = (),
            detail: str = "",
        ) -> DriveResult:
            return DriveResult(
                state,
                run_ids=tuple(run_ids),
                final_event=final_event,
                waiting_call=waiting_call,
                unknown_calls=unknown_calls,
                recovery=recovery,
                detail=detail,
                compaction_run_ids=tuple(compactions),
            )

        signal = self._stop(scope)
        turn = self._turn(scope)

        def boundary() -> DriveResult | None:
            """A safe boundary: nothing is under way, nothing new has started."""
            if signal.cancel_requested:
                return result(DriveState.CANCELLED)
            if signal.pause_requested:
                return result(DriveState.PAUSED, detail=self._paused_detail(scope))
            return None

        while True:
            if (stopped := boundary()) is not None:  # a stop outranks every limit
                return stopped
            limit = self._limits.get(scope.conversation_id, self._max_runs)
            if limit is not None and turn.steps >= limit:
                return result(DriveState.RUN_LIMIT_REACHED, detail=f"{turn.steps} steps")
            if self._no_progress_runs is not None and turn.empty_streak >= self._no_progress_runs:
                return result(DriveState.NO_PROGRESS,
                              detail=f"the last {turn.empty_streak} steps brought nothing new")
            try:
                inputs = await self._situated(scope, await self._loader.load(
                    scope, triggers, self._options_for(scope), *self._turn_user(scope)))
                model_input = self._compose(scope, inputs)
                model_input, refusal, estimate = await self._fit_budget(scope, triggers, inputs, model_input,
                                                                        compactions)
            except PromptError as exc:
                state = (
                    DriveState.RECOVERY_REQUIRED
                    if exc.code is PromptErrorCode.INCOMPLETE_HISTORY
                    else DriveState.REQUEST_REJECTED
                )
                return result(state, detail=str(exc))
            except _Cancelled:
                return result(DriveState.CANCELLED, detail="cancelled while summarizing; nothing was committed")
            except _Paused:
                return result(DriveState.PAUSED, detail=self._paused_detail(scope, PAUSED_COMPACTING))
            except _BudgetReached as reached:
                return result(DriveState.BUDGET_REACHED, detail=f"{reached}; nothing was committed")
            if refusal is not None:
                return result(DriveState.REQUEST_REJECTED, detail=refusal)
            if (stopped := boundary()) is not None:  # e.g. paused while compacting
                return stopped
            reserve = turn.reserve()
            if estimate is not None and not turn.fits(estimate, reserve):
                return result(DriveState.BUDGET_REACHED,
                              detail=f"the turn used {turn.used} tokens; the next request (about {estimate} in, "
                                     f"{reserve} kept for its answer) would pass the budget of {turn.budget}")
            if self._streaming:  # V3-P73 §5.1: this Run answers the user
                options = dataclasses.replace(model_input.request_options, stream=True)
                model_input = dataclasses.replace(model_input, request_options=options)
            try:
                prepared = self._provider.prepare_native_request(model_input)
            except ProviderError as exc:
                return result(DriveState.REQUEST_REJECTED, detail=str(exc))

            run_id = await self._log.prepare_run(
                scope,
                RunPreparation(
                    raw_request_body=prepared.body,
                    raw_content_type=prepared.content_type,
                    raw_representation=prepared.raw_representation,
                    trigger_refs=triggers,
                    prompt_basis=model_input.prompt_basis,
                    provider_key=self._provider.provider_key,
                    model_id=model_input.request_options.model_id,  # what was prepared, so what is sent
                ),
            )
            run_ids.append(run_id)
            await self._log.mark_dispatch(scope, run_id)  # T3: persisted before the one send
            capture = RawCapture() if prepared.streamed else None
            live = self._outputs.get(scope.conversation_id) if prepared.streamed else None
            if live is not None:
                live.output_started(run_id)
            try:
                try:
                    outcome = await self._interruptible(scope, self._invocation(prepared, run_id, capture, live))
                except _Cancelled:
                    await self._log.record_raw_outcome(scope, run_id, _uncertain(CANCELLED_BY_USER, capture))
                    turn.charge(None, estimate or 0, reserve, step=True)
                    return result(DriveState.CANCELLED,
                                  detail="cancelled while the model was answering; it may have been processed and "
                                         "is not resent")
                except asyncio.CancelledError:
                    if capture is not None:  # V3-P73 §5.3: shutdown keeps what arrived; else restart recovery
                        with contextlib.suppress(Exception):
                            await self._log.record_raw_outcome(scope, run_id, _uncertain(SHUTDOWN, capture))
                    raise
                except Exception as exc:  # noqa: BLE001 — adapter error after send began
                    outcome = NativeUncertain(type(exc).__name__)
                await self._log.record_raw_outcome(scope, run_id, _raw(outcome, capture))
            finally:
                if live is not None:
                    live.output_ended(run_id)
            turn.charge(outcome.usage if isinstance(outcome, NativeReceived) else None, estimate or 0, reserve,
                        step=True)
            if isinstance(outcome, NativeReceived) and signal.cancel_requested:
                # Arrived together with the cancel: the raw response is kept, nothing enters history.
                await self._log.record_projection_failure(scope, run_id, CANCELLED_BY_USER)
                return result(DriveState.CANCELLED, detail="cancelled as the answer arrived; it was not used")
            if isinstance(outcome, NativeNoResponse):
                return result(DriveState.PROVIDER_FAILED, detail=outcome.error_code)
            if isinstance(outcome, NativeUncertain):
                return result(DriveState.OUTCOME_UNKNOWN, detail=outcome.error_code)

            try:
                normalized = self._provider.normalize(outcome)
            except ProviderError as exc:
                await self._log.record_projection_failure(scope, run_id, exc.code.value)
                return result(DriveState.INVALID_MODEL_OUTPUT, detail=str(exc))

            event = await self._project(scope, run_id, normalized)
            if isinstance(normalized, NormalizedFinal):
                return result(DriveState.COMPLETED, final_event=event)
            group = await self._dispatch(scope, run_id, AssistantToolCalls(normalized.calls, normalized.commentary))
            if group.state is not GroupState.COMPLETED:
                return self._group_stop(group, result, self._paused_detail(scope))
            effects = {str(d.tool_id): d.effect_profile for d in model_input.tool_catalog}
            turn.observe(await self._log.read_tool_executions(scope, run_id), group.refusals, effects)
            triggers = (CompletedGroupRef(run_id),)

    async def _fit_budget(
        self,
        scope: TrustedScope,
        triggers: tuple[TriggerRef, ...],
        inputs: PromptInputs,
        model_input: ModelInput,
        compactions: list[RunId],
    ) -> tuple[ModelInput, str | None, int | None]:
        """The input to send (after at most MAX_COMPACTIONS_PER_RUN
        compactions), or a refusal if it is still over the hard limit; and
        its estimated size (None without a context budget)."""
        budget = self._turn_model(scope).offer.budget
        if self._planner is None or budget is None:
            return model_input, None, None
        decision = self._planner.decide(inputs, model_input, budget)
        failure = None
        for _ in range(MAX_COMPACTIONS_PER_RUN):
            if decision.compaction is None:
                break
            failure = await self._compact(scope, decision.compaction, self._planner, budget, compactions)
            if failure is not None:
                break
            inputs = await self._situated(scope, await self._loader.load(
                scope, triggers, self._options_for(scope), *self._turn_user(scope)))
            model_input = self._compose(scope, inputs)
            decision = self._planner.decide(inputs, model_input, budget)
        estimate, ceiling = decision.estimate, budget.ceiling_tokens
        if estimate.tokens > ceiling:
            detail = f"context_budget_exceeded: about {estimate.tokens} tokens > {ceiling}"
            if failure is not None:
                detail += f" (compaction failed: {failure})"
            elif decision.compaction is None:
                detail += " (no compaction fits within the limit)"
            return model_input, detail, estimate.tokens
        return model_input, None, estimate.tokens

    async def _compact(
        self,
        scope: TrustedScope,
        plan: CompactionPlan,
        planner: CompactionPlanner,
        budget: ContextBudget,
        compactions: list[RunId],
    ) -> str | None:
        """One compaction: a chain of summarizing calls, each a COMPACTION
        Run (appended to ``compactions``). Only the last summary is
        committed. Returns a failure, or None on success."""
        summary = plan.previous_summary
        last = len(plan.pieces) - 1
        signal = self._stop(scope)
        for index in range(len(plan.pieces)):
            if signal.cancel_requested:
                raise _Cancelled()
            if signal.pause_requested:  # the call under way finished; the next one does not start
                raise _Paused()
            request = planner.step_request(plan, index, summary)
            size = planner.request_size(request)  # measured here, not taken from the planner
            if size > budget.limit_tokens:
                return f"compaction request of about {size} tokens exceeds the limit"
            turn = self._turn(scope)
            reserve = plan.summary_limit  # characters: at least as many tokens as the summary can take
            if not turn.fits(size, reserve):
                raise _BudgetReached(f"the turn used {turn.used} tokens; a summarizing call (about {size} in, "
                                     f"{reserve} kept for the summary) would pass the budget of {turn.budget}")
            try:
                run_id, failure, text = await self._summarize(scope, request, planner, plan, size, reserve)
            except _Cancelled as cancelled:
                if cancelled.run_id is not None:
                    compactions.append(cancelled.run_id)
                raise
            if run_id is not None:
                compactions.append(run_id)
            if failure is not None or run_id is None or text is None:
                return failure or "no summary"
            if index < last:
                await self._log.record_compaction_step(scope, run_id)
                summary = text
                continue
            if signal.cancel_requested:  # the last check before the atomic commit
                await self._log.record_projection_failure(scope, run_id, CANCELLED_BY_USER)
                raise _Cancelled()
            try:
                await self._log.commit_compaction(
                    scope,
                    run_id,
                    expected_revision=plan.expected_revision,
                    schema_version=planner.snapshot_schema_version,
                    body=planner.snapshot_body(text),
                    covers_through_sequence=plan.covers_through_sequence,
                    basis=plan.prompt_basis,
                )
            except DataError as exc:
                if exc.code not in (DataErrorCode.VERSION_CONFLICT, DataErrorCode.STALE_BASIS):
                    raise
                await self._log.record_projection_failure(scope, run_id, exc.code.value)
                return str(exc)
        return None

    async def _summarize(
        self, scope: TrustedScope, request: ModelInput, planner: CompactionPlanner, plan: CompactionPlan,
        size: int, reserve: int,
    ) -> tuple[RunId | None, str | None, str | None]:
        """One summarizing call as a COMPACTION Run: (Run, failure, summary)."""
        try:
            prepared = self._provider.prepare_native_request(request)
        except ProviderError as exc:
            return None, str(exc), None
        run_id = await self._log.prepare_run(
            scope,
            RunPreparation(
                raw_request_body=prepared.body,
                raw_content_type=prepared.content_type,
                raw_representation=prepared.raw_representation,
                trigger_refs=(),
                prompt_basis=request.prompt_basis,
                provider_key=self._provider.provider_key,
                model_id=request.request_options.model_id,
                purpose=RunPurpose.COMPACTION,
            ),
        )
        await self._log.mark_dispatch(scope, run_id)
        try:
            outcome = await self._interruptible(scope, lambda: self._provider.invoke_prepared(prepared))
        except _Cancelled:
            await self._log.record_raw_outcome(scope, run_id, RawUncertain(CANCELLED_BY_USER))
            self._turn(scope).charge(None, size, reserve, step=False)
            raise _Cancelled(run_id) from None
        except Exception as exc:  # noqa: BLE001 — adapter error after send began
            outcome = NativeUncertain(type(exc).__name__)
        await self._log.record_raw_outcome(scope, run_id, _raw(outcome))
        self._turn(scope).charge(outcome.usage if isinstance(outcome, NativeReceived) else None, size, reserve,
                                 step=False)
        if not isinstance(outcome, NativeReceived):
            return run_id, outcome.error_code, None  # no effects to reconcile: the next Run may try again
        try:
            normalized = self._provider.normalize(outcome)
            if not isinstance(normalized, NormalizedFinal):
                raise PromptError(PromptErrorCode.INVALID_SUMMARY, "the summary call returned tool calls")
            text = planner.summary_text(normalized.blocks, plan)
        except (ProviderError, PromptError) as exc:
            await self._log.record_projection_failure(scope, run_id, exc.code.value)
            return run_id, str(exc), None
        return run_id, None, text


def _raw(outcome: NativeReceived | NativeNoResponse | NativeUncertain,
         capture: RawCapture | None = None) -> RawRunOutcome:
    if isinstance(outcome, NativeReceived):
        return RawReceived(outcome.body, outcome.content_type, outcome.usage)
    if isinstance(outcome, NativeNoResponse):
        return RawNoResponse(outcome.error_code, outcome.body, outcome.content_type)
    return _uncertain(outcome.error_code, capture)


def _uncertain(error_code: str, capture: RawCapture | None) -> RawUncertain:
    """An uncertain outcome with what arrived of a stream, if anything (V3-P73 §5.3)."""
    body = capture.body() if capture is not None else None
    return RawUncertain(error_code, body, capture.content_type if body is not None and capture else "")
