"""Control public contract (CTRL-01, CTRL-03/04, CTRL-05, CTRL-06).

Source documents: docs/control-prompt-provider-v0.1.md §3, §5.1, §7;
docs/control-pause-v0.1.md; docs/control-run-budget-v0.1.md; docs/conversation-model-v0.1.md;
docs/decisions.md V3-024, V3-044, V3-045, V3-P55, V3-P62, V3-P64.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol, runtime_checkable

from himari.common import CallRef, ConversationId, DeepFrozen, PrincipalId, RunId, TrustedScope
from himari.data.api import ConversationEvent, MessageOrigin, ModelSetting, RecoveryReport, UserMessage, WakeReason
from himari.prompt.api import ContextBudget, PresenceView
from himari.provider.api import OutputDelta


class DriveState(Enum):
    COMPLETED = "completed"  # FINAL committed
    WAITING_FOR_USER = "waiting_for_user"  # ask_user pending; lock released
    RECOVERY_REQUIRED = "recovery_required"  # UNKNOWN effect / incomplete history blocks the next Run
    PROVIDER_FAILED = "provider_failed"  # certain: no response
    OUTCOME_UNKNOWN = "outcome_unknown"  # send uncertain; never resent
    INVALID_MODEL_OUTPUT = "invalid_model_output"  # raw kept, no effects
    REQUEST_REJECTED = "request_rejected"  # Prompt/Provider/context budget refused before send
    RUN_LIMIT_REACHED = "run_limit_reached"  # an optional Run limit (V3-P62 §6)
    BUDGET_REACHED = "budget_reached"  # the turn's token budget would be passed by the next request (V3-P62 §3)
    NO_PROGRESS = "no_progress"  # Runs in a row with nothing new (V3-P62 §4)
    IDLE = "idle"  # resume(): nothing to continue (last turn was FINAL)
    COMPACTED = "compacted"  # compact(): the older history is now a summary
    NOTHING_TO_COMPACT = "nothing_to_compact"  # compact(): no budget, or nothing older than the latest turn
    COMPACTION_FAILED = "compaction_failed"  # compact(): a summarizing call failed; nothing committed
    PAUSED = "paused"  # stopped at a safe boundary on request; never-started calls stay PENDING (resume runs them)
    CANCELLED = "cancelled"  # stopped now on request; never-started calls closed as CANCELLED


@dataclass(frozen=True)
class DriveResult(DeepFrozen):
    state: DriveState
    run_ids: tuple[RunId, ...] = ()
    final_event: ConversationEvent | None = None
    waiting_call: CallRef | None = None
    unknown_calls: tuple[CallRef, ...] = ()
    recovery: RecoveryReport | None = None
    detail: str = ""
    compaction_run_ids: tuple[RunId, ...] = ()  # summarizing calls made before the Runs


@dataclass(frozen=True)
class ContextStatus(DeepFrozen):
    """How large the next request would be and what compaction has done —
    for display only; it decides nothing.

    ``estimated_tokens`` is None when the history cannot be composed right
    now (e.g. a Tool group is still running). ``summary`` and
    ``covered_through_sequence`` describe the latest compaction: Events up
    to that sequence reach the model only through the summary."""

    estimated_tokens: int | None
    measured: bool
    budget_tokens: int
    compact_at_tokens: int
    hard_limit_tokens: int
    summary: str | None = None
    covered_through_sequence: int | None = None
    compactions: int = 0  # committed compactions (snapshot revision)


@dataclass(frozen=True)
class ModelOffer(DeepFrozen):
    """One model the operator offers (V3-P64 §3), with what a turn on it uses."""

    model_id: str
    default_effort: str | None = None
    efforts: frozenset[str] = frozenset()  # the efforts the Provider declares for it
    budget: ContextBudget | None = None  # None: no compaction or size check
    max_output_tokens: int | None = None  # caps a turn's output reserve (V3-P62)


@dataclass(frozen=True)
class ResolvedModel(DeepFrozen):
    """What one turn uses (V3-P64 §4.2): fixed when the turn starts.
    ``fallback`` says why a stored choice was not used as made."""

    offer: ModelOffer
    reasoning_effort: str | None
    fallback: str | None = None

    @property
    def model_id(self) -> str:
        return self.offer.model_id


@dataclass(frozen=True)
class ModelCatalog(DeepFrozen):
    """The offered models, in the operator's order, and the default one.
    Raises ValueError on duplicate ids, a default that is not offered, or a
    default effort the model does not declare (V3-P64 §3: refused at start,
    not before a send)."""

    offers: tuple[ModelOffer, ...]
    default_model: str

    def __post_init__(self) -> None:
        super().__post_init__()
        ids = [o.model_id for o in self.offers]
        if len(set(ids)) != len(ids):
            raise ValueError(f"a model is offered twice: {sorted({i for i in ids if ids.count(i) > 1})}")
        if self.default_model not in ids:
            raise ValueError(f"the default model {self.default_model!r} is not offered")
        for offer in self.offers:
            if offer.default_effort is not None and offer.default_effort not in offer.efforts:
                raise ValueError(f"the reasoning effort {offer.default_effort!r} is not supported by "
                                 f"{offer.model_id}")

    def offer(self, model_id: str) -> ModelOffer | None:
        return next((o for o in self.offers if o.model_id == model_id), None)

    @property
    def default(self) -> ModelOffer:
        offer = self.offer(self.default_model)
        assert offer is not None
        return offer

    def resolve(self, setting: ModelSetting | None) -> ResolvedModel:
        """A stored choice → the model and effort a turn uses. A model no
        longer offered falls back to the default model, an effort no longer
        declared to the model's default effort; ``fallback`` says so."""
        if setting is None:
            return ResolvedModel(self.default, self.default.default_effort)
        offer = self.offer(setting.model_id)
        if offer is None:
            default = self.default
            return ResolvedModel(default, default.default_effort,
                                 f"{setting.model_id} is no longer offered; using the default model {default.model_id}")
        effort = setting.reasoning_effort
        if effort is None:
            return ResolvedModel(offer, offer.default_effort)
        if effort not in offer.efforts:
            return ResolvedModel(offer, offer.default_effort,
                                 f"the reasoning effort {effort!r} is no longer offered for {offer.model_id}; "
                                 "using its default")
        return ResolvedModel(offer, effort)


class ControlErrorCode(Enum):
    BUSY = "busy"  # another driver holds this Conversation in this process
    CLOSED = "closed"  # the Conversation is being moved to the trash, or is there (V3-P65)
    # V3-P87 §6.1 (re-review P1): a wake opens only in a settled Conversation — no
    # waiting call, no unfinished tool group, no recovery needed, no unanswered trigger
    NOT_SETTLED = "not_settled"
    # V3-P87 §13 (owner 2026-11-01): the Conversation's model did not pass the speaking-first check
    MODEL_UNCHECKED = "model_unchecked"


class ControlError(Exception):
    def __init__(self, code: ControlErrorCode, message: str = "") -> None:
        super().__init__(f"{code.value}: {message}" if message else code.value)
        self.code = code


class StopRequest(Enum):
    PAUSE = "pause"
    CANCEL = "cancel"


class StopSignal:
    """A stop request for one running job (V3-P55), held by whoever started it.

    ``pause``: finish what is under way — the model call or the Tool call in
    progress — and start nothing new; the job ends PAUSED (or COMPLETED,
    WAITING_FOR_USER, … if it reached that first). ``cancel``: stop now; the
    model call or Tool call in progress is interrupted and the job ends
    CANCELLED. A cancel overrides a pause. Neither is persisted: P0 runs one
    process (V3-045), and the state left behind is read from Data."""

    def __init__(self) -> None:
        self._request: StopRequest | None = None
        self._reason: str | None = None
        self._cancelled = asyncio.Event()

    def pause(self, reason: str | None = None) -> None:
        """``reason``: why, when it is not the user's own Pause (V3-P62 §5);
        the first one asked is kept and ends up in the result's detail."""
        if self._request is None:
            self._request = StopRequest.PAUSE
            self._reason = reason

    @property
    def reason(self) -> str | None:
        return self._reason

    def cancel(self) -> None:
        self._request = StopRequest.CANCEL
        self._cancelled.set()

    @property
    def request(self) -> StopRequest | None:
        return self._request

    @property
    def pause_requested(self) -> bool:
        return self._request is not None

    @property
    def cancel_requested(self) -> bool:
        return self._request is StopRequest.CANCEL

    async def cancelled(self) -> None:
        await self._cancelled.wait()


@dataclass(frozen=True)
class TurnUsage(DeepFrozen):
    """What the running drive has used so far (V3-P62 §7): every Run's
    reported tokens (summarizing calls too), and the budget if there is one."""

    runs: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    budget: int | None = None
    max_runs: int | None = None


class TurnProgress(Protocol):
    """Told by the Driver after every Run of a drive (V3-P62 §7). It must not
    raise and the Driver does not depend on it: whoever started the drive
    (the Runner) only keeps the value."""

    def report(self, usage: TurnUsage) -> None: ...


@runtime_checkable
class OutputProgress(Protocol):
    """Told by the Driver what a streamed Run writes (V3-P73 §5.2), for
    display only; same rules as TurnProgress. ``output_ended`` follows every
    ``output_started``, after the Run's outcome is recorded, however it ended."""

    def output_started(self, run_id: RunId) -> None: ...

    def output(self, run_id: RunId, delta: OutputDelta) -> None: ...

    def output_ended(self, run_id: RunId) -> None: ...


LIVE_TEXT_CHARS = 512 * 1024  # of one Run's text kept for display (V3-P73 §5.2)


@dataclass(frozen=True)
class LiveOutput(DeepFrozen):
    """What a streamed Run of a Conversation's job has written so far
    (V3-P73 §5.2): display only, process memory, never a record. ``version``
    changes with every change of the Conversation's live state; ``ended``
    stays until the next Run starts or the next job does."""

    run_id: RunId
    text: str = ""
    tools: tuple[str, ...] = ()
    thinking: bool = False
    truncated: bool = False
    ended: bool = False
    version: int = 0
    chars: int = 0  # the text's length (also when the text was left out)


# The turn's rule for the system text (TURN_GUIDANCE) is the Prompt module's (V3-P89 §3.3).


class ConversationDriver(Protocol):
    async def drive(self, scope: TrustedScope, message: UserMessage, *, stop: StopSignal | None = None,
                    progress: TurnProgress | None = None) -> DriveResult:
        """Record one user input (T7a if an ask_user call is waiting, else T1;
        never-started calls of the latest group are closed first, as in
        ``accept``) and run the loop until FINAL, a wait, or a stop condition.
        Raises ControlError(BUSY) if the Conversation is already being driven."""
        ...

    async def resume(self, scope: TrustedScope, *, stop: StopSignal | None = None,
                     progress: TurnProgress | None = None,
                     max_runs: int | None = None) -> DriveResult:
        """Continue without new user input: restart cleanup, project stored
        responses, finish never-claimed PENDING calls, then start the next
        Run if one is due. Same BUSY rule as ``drive``."""
        ...

    async def accept(self, scope: TrustedScope, message: UserMessage) -> None:
        """Commit one user input (T7a if an ask_user call is waiting, else
        T1) after restart cleanup, without running Tools or the Provider;
        ``resume`` continues from it. A new message redirects (V3-P55):
        never-started calls of the latest group — left by a pause or an
        interruption — are first closed as CANCELLED, telling the model the
        user sent a new message instead. Same BUSY rule as ``drive``."""
        ...

    async def compact(self, scope: TrustedScope, *, stop: StopSignal | None = None,
                      progress: TurnProgress | None = None) -> DriveResult:
        """CTRL-08: compact now, whatever the size — everything older than the
        recent tail becomes the summary (one plan, possibly a chain of calls).
        Starts no Run. COMPACTED, NOTHING_TO_COMPACT, COMPACTION_FAILED, or
        WAITING_FOR_USER while a question is open. Same BUSY rule."""
        ...

    async def accept_wake(self, scope: TrustedScope, reasons: Sequence[WakeReason], device_id: str, *,
                          keys: Sequence[str] = (), reminders: Sequence[int] = ()) -> ConversationEvent:
        """V3-P87 §4.1, §6.1: record a wake (``WakeStore.begin_wake``) under this
        Conversation's lock, after restart cleanup, only when it is settled —
        else NOT_SETTLED, nothing written. ``resume`` then answers it."""
        ...

    async def resume_wake(self, scope: TrustedScope, wake_id: str, *, stop: StopSignal | None = None,
                          progress: TurnProgress | None = None, max_runs: int | None = None) -> DriveResult:
        """V3-P87: ``resume`` for a wake's turn, its model checked again for speaking first."""
        ...

    async def withdraw_wake(self, scope: TrustedScope, wake_id: str) -> None:
        """V3-P87 (PR #156 re-review P2-1): a wake recorded but never started —
        ended CANCELLED (its reminders go back) and its keys given back, as it
        said nothing."""
        ...

    async def close_cut_group(self, scope: TrustedScope, reason: str) -> bool:
        """V3-P87 §4.4 (re-review P1): settle an unfinished tool group left by a
        job a restart cut — calls never started are not run, finished ones keep
        their effects; whether there was one."""
        ...

    async def cancel(self, scope: TrustedScope) -> DriveResult:
        """V3-P55, for a Conversation that is not running: close what was
        left waiting — an open question (``ask_user``) or never-started
        calls of a paused or interrupted group — with a CANCELLED result, so
        the next message starts a new turn. CANCELLED if anything was
        closed, IDLE if nothing was waiting, RECOVERY_REQUIRED if uncertain
        calls still need a user. Same BUSY rule."""
        ...


@dataclass(frozen=True)
class JobStatus(DeepFrozen):
    """The background job of one Conversation, as its owner sees it.

    ``generation`` counts submitted jobs (0 = none since this process
    started). ``error`` is a short classification of a job that raised
    (e.g. ``"busy"``, ``"not_authorized"``, ``"internal_error"``); details
    stay in the server log. Status is process memory: after a restart the
    Conversation is idle and Data holds everything durable."""

    running: bool
    generation: int = 0
    result: DriveResult | None = None
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    stop_requested: StopRequest | None = None  # asked of the running job (V3-P55)
    turn_usage: TurnUsage | None = None  # the latest report of the job's drive (V3-P62 §7)
    wake_id: str | None = None  # V3-P87: the wake this job answers (a server-opened turn)

    @property
    def job_id(self) -> str | None:
        """V3-P84 §6.3: which job this is, opaque, compared for equality only;
        None before the first. ``generation`` starts again with the process,
        ``started_at`` does not, so no two jobs of a Conversation share one."""
        if self.started_at is None:
            return None
        return f"{self.generation}.{self.started_at.strftime('%Y%m%dT%H%M%S%f')}"


class JobListener(Protocol):
    """What the Runner tells as it goes (V3-P83 §2.2), for the event stream.
    Called synchronously; a listener that fails is logged, never the job's failure."""

    def job_started(self, scope: TrustedScope, origin: MessageOrigin | None, status: JobStatus) -> None:
        """Any job: a message's (``origin``: where it came from), a resume, a
        compaction, a cancel. ``status``: the new job's (V3-P84 §6.3)."""
        ...

    def accepted(self, scope: TrustedScope) -> None:
        """A user message was recorded (its job may still be refused)."""
        ...

    def run_ended(self, scope: TrustedScope) -> None: ...

    def job_ended(self, scope: TrustedScope, status: JobStatus) -> None: ...


@dataclass(frozen=True)
class TurnPresence(DeepFrozen):
    """Where the user is, for one turn (V3-P83 §6.2): what the model is told
    (None: nothing), and the zone of the device the message came from."""

    view: PresenceView | None = None
    time_zone: str | None = None


class PresenceSource(Protocol):
    async def read(self, principal: PrincipalId, sent_from: MessageOrigin | None) -> TurnPresence:
        """``sent_from``: the origin of the turn's message, which the Driver read from the log."""
        ...


class ContextInspector(Protocol):
    async def context_status(self, scope: TrustedScope) -> ContextStatus | None:
        """The current context status; None if no context budget is set.
        Read-only, takes no Conversation lock: it may be a moment stale."""
        ...


class ConversationRunner(Protocol):
    """CTRL-06: runs ``drive``/``resume`` in the background for interfaces
    (Web/CLI) that must not block on a whole loop. Status is kept per
    (principal, Conversation): another principal sees an idle Conversation."""

    async def submit(self, scope: TrustedScope, message: UserMessage | None) -> JobStatus:
        """Commit ``message`` (``accept``) and only then start ``resume`` in
        the background; with ``message`` None just start ``resume``. When
        this returns, the input is durable — a crash before the job runs
        loses nothing, and a later ``resume`` answers it. Raises
        ControlError(BUSY) while this Conversation has a running job, and
        any Data error of the commit (then no job is started)."""
        ...

    async def submit_compaction(self, scope: TrustedScope) -> JobStatus:
        """Start ``compact`` in the background. Same BUSY rule as ``submit``."""
        ...

    def request_pause(self, scope: TrustedScope, reason: str | None = None) -> JobStatus:
        """CTRL-03 (V3-P55): ask the running job to stop at the next safe
        boundary. Nothing to do when no job is running. ``reason``: why, for
        a pause the user did not ask for (V3-P62 §5)."""
        ...

    def seen(self, scope: TrustedScope) -> None:
        """A client read this Conversation (V3-P62 §5): a connected client,
        not a person watching. The runner pauses a drive that has run
        ``unattended_runs`` Runs without one."""
        ...

    def cancel(self, scope: TrustedScope) -> JobStatus:
        """V3-P55: ask the running job to stop now. Nothing to do when no job
        is running — never closes anything a finished job left waiting, so a
        late cancel cannot dismiss a question that just arrived."""
        ...

    async def submit_cancel(self, scope: TrustedScope) -> JobStatus:
        """V3-P55: start the driver's ``cancel`` in the background — close
        what was left waiting (a question, or never-started calls of a
        paused or interrupted group). Same BUSY rule as ``submit``."""
        ...

    def status(self, scope: TrustedScope) -> JobStatus: ...

    def running(self, principal: PrincipalId) -> Sequence[ConversationId]:
        """V3-P80 §3.2: the principal's Conversations with a job under way now (of any kind)."""
        ...

    async def wait(self, scope: TrustedScope) -> JobStatus:
        """Until the current job (if any) has finished."""
        ...

    async def shutdown(self, grace_seconds: float) -> None:
        """Let running jobs finish within the grace period, then cancel the
        rest (a cancelled job is recovered like a crash, T-R)."""
        ...
