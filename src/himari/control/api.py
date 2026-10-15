"""Control public contract (CTRL-01, CTRL-03/04, CTRL-05, CTRL-06).

Source documents: docs/control-prompt-provider-v0.1.md §3, §5.1, §7;
docs/control-pause-v0.1.md; docs/decisions.md V3-024, V3-044, V3-045, V3-P55.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol

from himari.common import CallRef, DeepFrozen, RunId, TrustedScope
from himari.data.api import ConversationEvent, RecoveryReport, UserMessage


class DriveState(Enum):
    COMPLETED = "completed"  # FINAL committed
    WAITING_FOR_USER = "waiting_for_user"  # ask_user pending; lock released
    RECOVERY_REQUIRED = "recovery_required"  # UNKNOWN effect / incomplete history blocks the next Run
    PROVIDER_FAILED = "provider_failed"  # certain: no response
    OUTCOME_UNKNOWN = "outcome_unknown"  # send uncertain; never resent
    INVALID_MODEL_OUTPUT = "invalid_model_output"  # raw kept, no effects
    REQUEST_REJECTED = "request_rejected"  # Prompt/Provider/context budget refused before send
    RUN_LIMIT_REACHED = "run_limit_reached"
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


class ControlErrorCode(Enum):
    BUSY = "busy"  # another driver holds this Conversation in this process


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
        self._cancelled = asyncio.Event()

    def pause(self) -> None:
        if self._request is None:
            self._request = StopRequest.PAUSE

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


class ConversationDriver(Protocol):
    async def drive(self, scope: TrustedScope, message: UserMessage, *, stop: StopSignal | None = None) -> DriveResult:
        """Record one user input (T7a if an ask_user call is waiting, else T1;
        never-started calls of the latest group are closed first, as in
        ``accept``) and run the loop until FINAL, a wait, or a stop condition.
        Raises ControlError(BUSY) if the Conversation is already being driven."""
        ...

    async def resume(self, scope: TrustedScope, *, stop: StopSignal | None = None) -> DriveResult:
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

    async def compact(self, scope: TrustedScope, *, stop: StopSignal | None = None) -> DriveResult:
        """CTRL-08: compact now, whatever the size — everything older than the
        recent tail becomes the summary (one plan, possibly a chain of calls).
        Starts no Run. COMPACTED, NOTHING_TO_COMPACT, COMPACTION_FAILED, or
        WAITING_FOR_USER while a question is open. Same BUSY rule."""
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

    def request_pause(self, scope: TrustedScope) -> JobStatus:
        """CTRL-03 (V3-P55): ask the running job to stop at the next safe
        boundary. Nothing to do when no job is running."""
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

    async def wait(self, scope: TrustedScope) -> JobStatus:
        """Until the current job (if any) has finished."""
        ...

    async def shutdown(self, grace_seconds: float) -> None:
        """Let running jobs finish within the grace period, then cancel the
        rest (a cancelled job is recovered like a crash, T-R)."""
        ...
