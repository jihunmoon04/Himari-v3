"""Server-opened turns, part 1 (V3-P87 §4, §11): the settings, ending each wake
when its job ends, and settling the wakes a restart cut.

The triggers, the throttling rules and the scheduler come in part 2; until
then a wake is opened only by the operator's ``try`` (Application.try_wake).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import time

from himari.common import TrustedScope
from himari.control.api import DriveState, JobStatus
from himari.control.driver import Driver
from himari.data.api import (
    AssistantFinal,
    AssistantToolCalls,
    ConversationEvent,
    MessageOrigin,
    UserMessage,
    WakeRecord,
    WakeStatus,
)
from himari.data.sqlite import SqliteData

log = logging.getLogger(__name__)

CUT_REASON = "the server restarted while HIMARI was speaking first"  # §4.4: a cut wake's group is closed


class ProactiveRefused(Exception):
    """Why ``try`` opened no wake (§11): ``proactive_off``, ``no_device``,
    ``no_conversation``, or a Runner refusal (``busy``, ``closed``, ``not_settled``)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ProactiveSettings:
    """V3-P87 §11, with the owner's numbers (§15; changed 2026-11-01: gap 30 min, focus 30 min)."""

    enabled: bool = False  # HIMARI_PROACTIVE
    quiet_start: time | None = time(23, 0)  # HIMARI_PROACTIVE_QUIET "23:00-08:00"; None: no quiet hours
    quiet_end: time | None = time(8, 0)
    gap_minutes: int = 30
    daily: int = 4
    return_hours: int = 4
    focus_minutes: int = 30
    game_minutes: int = 30

    def describe(self) -> str:
        """The startup line and the status (§11)."""
        if not self.enabled:
            return "off (HIMARI_PROACTIVE=0)"
        quiet = "no quiet hours" if self.quiet_start is None or self.quiet_end is None else \
            f"quiet {self.quiet_start:%H:%M}-{self.quiet_end:%H:%M}"
        return f"on ({quiet}, gap {self.gap_minutes}m, {self.daily} a day)"


def parse_quiet(value: str | None) -> tuple[time | None, time | None]:
    """``"23:00-08:00"``; unset: those defaults; empty: no quiet hours. ValueError otherwise."""
    if value is None:
        return time(23, 0), time(8, 0)
    if not value.strip():
        return None, None
    start, dash, end = value.strip().partition("-")
    if not dash:
        raise ValueError("quiet hours are HH:MM-HH:MM")
    return time.fromisoformat(start.strip()), time.fromisoformat(end.strip())


def wake_turn(after: Sequence[ConversationEvent]) -> tuple[list[ConversationEvent], bool]:
    """The wake's own Events: those after it up to the user's next message (the
    user's turn is not the wake's, review of part 1). And whether that message
    answered one of the wake's questions."""
    own: list[ConversationEvent] = []
    for event in after:
        if isinstance(event.payload, UserMessage):
            return own, event.payload.answers_call is not None
        own.append(event)
    return own, False


def wake_outcome(status: JobStatus | None, after: Sequence[ConversationEvent],
                 waiting: bool = False) -> tuple[WakeStatus, bool]:
    """§4.2: ANSWERED when the wake's turn gave a final answer, or (``asked``) left an
    ask_user question; CANCELLED when it was cancelled before saying anything (the
    user won, §4.3); else FAILED. The wake job's own result decides first (``status``;
    None at a restart), then its own Events (``after``: from the wake on; cut at the
    user's next message) and, with no message after it, a question still ``waiting``."""
    own, answered_question = wake_turn(after)
    state = None if status is None or status.result is None else status.result.state
    if state is DriveState.COMPLETED or any(isinstance(e.payload, AssistantFinal) for e in own):
        return WakeStatus.ANSWERED, False
    if state is DriveState.WAITING_FOR_USER or answered_question or (waiting and len(own) == len(after)):
        return WakeStatus.ANSWERED, True
    cancelled = state is DriveState.CANCELLED or (status is not None and status.error == "cancelled")
    if cancelled and not any(isinstance(e.payload, AssistantToolCalls) for e in own):
        return WakeStatus.CANCELLED, False
    return WakeStatus.FAILED, False


async def end_wake_of(data: SqliteData, scope: TrustedScope, wake_id: str, status: JobStatus | None) -> WakeRecord:
    record = await data.read_wake(scope.principal_id, wake_id)
    after = await data.read_events(scope, after_sequence=record.event_sequence)
    waiting = await data.find_waiting_call(scope) is not None
    outcome, asked = wake_outcome(status, after, waiting)
    return await data.end_wake(scope.principal_id, wake_id, outcome, asked=asked)


class WakeEvents:
    """The part of the Runner's listener that ends each wake with its job (§4.2)."""

    def __init__(self, data: SqliteData) -> None:
        self._data = data
        self._tasks: set[asyncio.Task[None]] = set()

    def job_started(self, scope: TrustedScope, origin: MessageOrigin | None, status: JobStatus) -> None:
        pass

    def accepted(self, scope: TrustedScope) -> None:
        pass

    def run_ended(self, scope: TrustedScope) -> None:
        pass

    def job_ended(self, scope: TrustedScope, status: JobStatus) -> None:
        if status.wake_id is None:
            return
        task = asyncio.get_running_loop().create_task(self._end(scope, status.wake_id, status))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _end(self, scope: TrustedScope, wake_id: str, status: JobStatus) -> None:
        try:
            await end_wake_of(self._data, scope, wake_id, status)
        except Exception:  # noqa: BLE001 — a wake left opened is settled at the next start
            log.exception("could not end wake %s", wake_id)

    async def aclose(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)


async def settle_cut_wakes(data: SqliteData, driver: Driver) -> int:
    """§4.4 at startup (part 1 resumes none): a wake with a final answer or a
    waiting question after it is ANSWERED; otherwise its unfinished tool group,
    if any, is closed — calls never started are not run, finished ones keep
    their effects — and it FAILS (its reminders go back to scheduled). How many."""
    settled = 0
    for record in await data.opened_wakes():
        scope = TrustedScope(record.principal, record.conversation_id)
        try:
            after = await data.read_events(scope, after_sequence=record.event_sequence)
            waiting = await data.find_waiting_call(scope) is not None
            outcome, asked = wake_outcome(None, after, waiting)
            if outcome is not WakeStatus.ANSWERED:
                if await driver.close_cut_group(scope, CUT_REASON):
                    log.info("closed the tool group a restart cut in wake %s", record.wake_id)
                outcome = WakeStatus.FAILED
            await data.end_wake(record.principal, record.wake_id, outcome, asked=asked)
            settled += 1
        except Exception:  # noqa: BLE001 — one Conversation is not the others
            log.exception("could not settle wake %s", record.wake_id)
    return settled
