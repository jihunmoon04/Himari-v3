"""Control public contract (CTRL-01, CTRL-05, CTRL-06).

Source documents: docs/control-prompt-provider-v0.1.md §3, §5.1, §7;
docs/decisions.md V3-024, V3-044, V3-045.
"""

from __future__ import annotations

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


class ConversationDriver(Protocol):
    async def drive(self, scope: TrustedScope, message: UserMessage) -> DriveResult:
        """Record one user input (T7a if an ask_user call is waiting, else T1)
        and run the loop until FINAL, a wait, or a stop condition.
        Raises ControlError(BUSY) if the Conversation is already being driven."""
        ...

    async def resume(self, scope: TrustedScope) -> DriveResult:
        """Continue without new user input: restart cleanup, project stored
        responses, finish never-claimed PENDING calls, then start the next
        Run if one is due. Same BUSY rule as ``drive``."""
        ...

    async def accept(self, scope: TrustedScope, message: UserMessage) -> None:
        """Commit one user input (T7a if an ask_user call is waiting, else
        T1) after restart cleanup, without running Tools or the Provider;
        ``resume`` continues from it. Same BUSY rule as ``drive``."""
        ...

    async def compact(self, scope: TrustedScope) -> DriveResult:
        """CTRL-08: compact now, whatever the size — everything older than the
        recent tail becomes the summary (one plan, possibly a chain of calls).
        Starts no Run. COMPACTED, NOTHING_TO_COMPACT, COMPACTION_FAILED, or
        WAITING_FOR_USER while a question is open. Same BUSY rule."""
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

    def status(self, scope: TrustedScope) -> JobStatus: ...

    async def wait(self, scope: TrustedScope) -> JobStatus:
        """Until the current job (if any) has finished."""
        ...

    async def shutdown(self, grace_seconds: float) -> None:
        """Let running jobs finish within the grace period, then cancel the
        rest (a cancelled job is recovered like a crash, T-R)."""
        ...
