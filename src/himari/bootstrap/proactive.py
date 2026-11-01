"""Server-opened turns, part 1 (V3-P87 §4, §11): the settings, ending each wake
when its job ends, and settling the wakes a restart cut.

The triggers, the throttling rules and the scheduler come in part 2; until
then a wake is opened only by the operator's ``try`` (Application.try_wake).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

from himari.common import PrincipalId, TrustedScope
from himari.control.api import ControlError, DriveState, JobStatus
from himari.control.driver import Driver
from himari.control.runner import BackgroundRunner
from himari.data.api import (
    AssistantFinal,
    AssistantToolCalls,
    ConversationEvent,
    MessageOrigin,
    UserMessage,
    WakeRecord,
    WakeStatus,
    ConversationSummary,
    DataError,
    MemoryCategory,
    Wake,
    WakeKind,
)
from himari.data.sqlite import SqliteData
from himari.presence.api import Devices, Returned
from himari.presence.hub import PresenceHub
from himari.proactive import ProactiveSettings as ProactiveSettings  # re-exported for the interfaces
from himari.proactive import (
    RETURN_WAIT,
    TALKING,
    Decision,
    Return,
    Situation,
    candidate_keys,
    candidates,
    decide,
    local_midnight,
    spoke,
)

log = logging.getLogger(__name__)

CUT_REASON = "the server restarted while HIMARI was speaking first"  # §4.4: a cut wake's group is closed


class ProactiveRefused(Exception):
    """Why ``try`` opened no wake (§11): ``proactive_off``, ``no_device``,
    ``no_conversation``, or a Runner refusal (``busy``, ``closed``, ``not_settled``)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


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
    user's turn is not the wake's, review of part 1) or the next wake. And
    whether that message answered one of the wake's questions."""
    own: list[ConversationEvent] = []
    for event in after:
        if isinstance(event.payload, UserMessage):
            return own, event.payload.answers_call is not None
        if isinstance(event.payload, Wake):  # the next wake's turn is its own (re-review P2-2)
            return own, False
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
    """§4.4 at startup: a wake with a final answer or a waiting question after it is
    ANSWERED; otherwise its unfinished tool group, if any, is closed — calls never
    started are not run, finished ones keep their effects — and it FAILS (its
    reminders go back to scheduled). None is resumed: no device is current this
    early, so §6.1 would not pass; a wake cut before it said anything gives its
    keys back instead, and the scheduler speaks again if a trigger is still in its
    window. How many were settled."""
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
            if outcome is WakeStatus.FAILED and not after:  # cut before a word: its triggers may come again
                await data.release_keys(record.principal, record.wake_id)
            settled += 1
        except Exception:  # noqa: BLE001 — one Conversation is not the others
            log.exception("could not settle wake %s", record.wake_id)
    return settled


# --- part 2: the inputs, the returns and the scheduler (§5.2, §7) ----------------------

INPUT_EVERY = timedelta(minutes=10)  # §3.3: a device's last input is written at most this often (and when locked)
PROACTIVE_SECONDS = 30.0  # §7: the scheduler's period
WAKES_READ = timedelta(hours=26)  # enough for the gap and the device's day (§6.2)
TAIL_EVENTS = 40  # §6.1: how far back the most recent conversation is read for an unanswered wake


class Inputs:
    """§5.2: each device's last input. Presence tells every signal's (``seen``);
    the latest is kept here and written to Data at most every INPUT_EVERY, at
    once when the screen locks, so the time away is measured across a restart."""

    def __init__(self, data: SqliteData) -> None:
        self._data = data
        self._latest: dict[PrincipalId, dict[str, datetime]] = {}  # in this process, every signal's
        self._written: dict[PrincipalId, dict[str, datetime]] = {}
        self._loading: dict[PrincipalId, asyncio.Task[dict[str, datetime]]] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def load(self, principal: PrincipalId) -> asyncio.Task[dict[str, datetime]]:
        """What Data had recorded before this process heard anything (read once)."""
        task = self._loading.get(principal)
        if task is None:
            async def read() -> dict[str, datetime]:
                found = dict(await self._data.read_inputs(principal))
                written = self._written.setdefault(principal, {})
                for device_id, at in found.items():
                    written[device_id] = max(at, written.get(device_id, at))
                return found

            task = self._loading[principal] = asyncio.get_running_loop().create_task(read())
        return task

    def seen(self, principal: PrincipalId, device_id: str, at: datetime, locked: bool) -> None:
        """Presence's ``on_input``."""
        latest = self._latest.setdefault(principal, {})
        latest[device_id] = max(at, latest.get(device_id, at))
        self._spawn(self._write(principal, device_id, at, locked))

    async def _write(self, principal: PrincipalId, device_id: str, at: datetime, locked: bool) -> None:
        await self.load(principal)
        written = self._written.setdefault(principal, {})
        last = written.get(device_id)
        if last is not None and (at <= last or (not locked and at - last < INPUT_EVERY)):
            return
        written[device_id] = at
        await self._data.record_input(principal, device_id, at)

    def latest_now(self, principal: PrincipalId) -> tuple[datetime | None, bool]:
        """The latest input known in this process, and whether Data's were read already."""
        task = self._loading.get(principal)
        loaded = task is not None and task.done() and not task.cancelled() and task.exception() is None
        values = list(self._latest.get(principal, {}).values()) + (
            list(self._written.get(principal, {}).values()) if loaded else [])
        return max(values, default=None), loaded

    def _spawn(self, work: Awaitable[None]) -> None:
        async def guarded() -> None:
            try:
                await work
            except Exception:  # noqa: BLE001 — the next signal writes again
                log.exception("could not record a device's input")

        task = asyncio.get_running_loop().create_task(guarded())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def aclose(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)


def away_minutes(returned: Returned, latest: datetime | None) -> int | None:
    """§5.2: from the latest input before the return; None when none is known."""
    if latest is None or latest >= returned.at:
        return None if latest is None else 0
    return int((returned.at - latest).total_seconds() // 60)


@dataclass(frozen=True)
class Look:
    """The scheduler's last look at a principal (for the status, §11)."""

    at: datetime
    decision: Decision
    outcome: str  # "spoke", "nothing", or why it could not (a rule, "busy", "not_settled", …)


class ProactiveScheduler:
    """§7: every PROACTIVE_SECONDS (and soon after a return), for each principal with a
    current device, read a ``Situation``, ask ``decide`` and open the wake it says."""

    def __init__(self, data: SqliteData, hub: PresenceHub, devices: Devices, runner: BackgroundRunner,
                 inputs: Inputs, settings: ProactiveSettings, server_zone: str, *,
                 zones: frozenset[str] = frozenset(), clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                 poke: Callable[[float], None] = lambda after: None) -> None:
        self._data, self._hub, self._devices, self._runner = data, hub, devices, runner
        self._inputs = inputs
        self.settings = settings
        self._zone = server_zone
        self._zones = zones
        self._clock = clock
        self._poke = poke
        self._returns: dict[PrincipalId, Return] = {}
        self.looks: dict[PrincipalId, Look] = {}

    # --- a return (Presence's on_returned) ---

    def returned(self, principal: PrincipalId, returned: Returned) -> None:
        """Told before the signal's own input: the time away ends at the input before it."""
        latest, loaded = self._inputs.latest_now(principal)
        if loaded:
            self._returns[principal] = Return(returned.device_id, returned.at, returned.via,
                                              away_minutes(returned, latest))
        else:  # the first signal since the server started: what Data recorded before it
            task = self._inputs.load(principal)

            def keep(done: asyncio.Task[dict[str, datetime]]) -> None:
                if done.cancelled() or done.exception() is not None:
                    return
                known = max([*done.result().values(), *([latest] if latest is not None else [])], default=None)
                self._returns[principal] = Return(returned.device_id, returned.at, returned.via,
                                                  away_minutes(returned, known))

            task.add_done_callback(keep)
        self._poke(RETURN_WAIT.total_seconds() + 1)  # §5.2: look again once the user had their 20 seconds

    # --- the job ---

    async def run(self) -> None:
        if not self.settings.enabled:
            return
        for principal in self._hub.present():
            try:
                await self.look(principal)
            except Exception:  # noqa: BLE001 — one principal is not the others
                log.exception("could not look at %s", principal)

    async def situation(self, principal: PrincipalId) -> tuple[Situation, ConversationSummary | None]:
        now = self._clock()
        device_id = self._hub.current(principal)
        device = None if device_id is None else self._hub.states(principal).get(device_id)
        zone = device.time_zone if device is not None and device.time_zone in self._zones else self._zone
        recent = await self._data.most_recent_user_conversation(principal)
        unanswered = recent is not None and await self._unanswered(TrustedScope(principal, recent.conversation_id),
                                                                   recent.last_sequence)
        loops = tuple(m for m in await self._data.list_memories(principal) if m.category is MemoryCategory.LOOP)
        wakes = await self._data.wakes_since(principal, now - WAKES_READ)
        return Situation(
            now=now, settings=self.settings, zone=zone, device_id=device_id, device=device,
            quiet_until=await self._devices.quiet(principal),
            last_user_message_at=await self._data.last_user_message_at(principal),
            working=bool(self._runner.running(principal)), unanswered_wake=unanswered,
            wakes=wakes, loops=loops, returned=self._returns.get(principal),
            said=await self._said(principal, wakes)), recent

    async def _said(self, principal: PrincipalId, wakes: Sequence[WakeRecord]) -> frozenset[str]:
        """PR #156 re-review P2-2: of the failed wakes, those whose turn left assistant
        output (a final answer, progress or tool calls) before the next message or
        wake. One that cannot be read counts as having spoken."""
        said: set[str] = set()
        for record in wakes:
            if record.status is not WakeStatus.FAILED:
                continue
            try:
                after = await self._data.read_events(TrustedScope(principal, record.conversation_id),
                                                     after_sequence=record.event_sequence, limit=TAIL_EVENTS)
            except DataError:
                said.add(record.wake_id)
                continue
            own, _ = wake_turn(after)
            if any(isinstance(e.payload, (AssistantFinal, AssistantToolCalls)) for e in own) or len(own) == TAIL_EVENTS:
                said.add(record.wake_id)
        return frozenset(said)

    async def look(self, principal: PrincipalId) -> Look:
        situation, recent = await self.situation(principal)
        found, expired, _ = candidates(situation)
        taken = await self._data.taken_keys(principal, candidate_keys(found, expired))
        decision = decide(situation, taken)
        if decision.expired:
            await self._data.expire_keys(principal, decision.expired)
        outcome = decision.blocked or "nothing"
        if decision.reasons and situation.device_id is not None:
            outcome = await self._open(principal, recent, decision, situation.device_id)
        look = self.looks[principal] = Look(situation.now, decision, outcome)
        return look

    async def _open(self, principal: PrincipalId, recent: ConversationSummary | None, decision: Decision,
                    device_id: str) -> str:
        if recent is None:
            return "no_conversation"
        scope = TrustedScope(principal, recent.conversation_id)
        # PR #156 review P2: what may have changed since the look read it, checked again —
        # the last await is here, so these checks and the Runner's (no job of the
        # principal anywhere, before its first await) hold together.
        said = await self._data.last_user_message_at(principal)
        only_reminders = all(r.kind is WakeKind.REMINDER for r in decision.reasons)
        if not only_reminders and said is not None and self._clock() - said < TALKING:
            return "talking"
        if self._hub.current(principal) != device_id:
            return "no_device"
        try:
            await self._runner.submit_wake(scope, decision.reasons, device_id, keys=decision.keys,
                                           reminders=decision.reminders, alone=True)
        except ControlError as error:  # busy, closed, not settled: the next look tries again
            return error.code.value
        except DataError as error:  # a key taken meanwhile, a reminder changed meanwhile
            log.info("wake not opened in %s: %s", recent.conversation_id, error.code.value)
            return error.code.value
        log.info("HIMARI speaks first in %s: %s", recent.conversation_id,
                 ", ".join(r.kind.value for r in decision.reasons))
        return "spoke"

    async def _unanswered(self, scope: TrustedScope, last_sequence: int) -> bool:
        """§6.1: the conversation ends with a wake turn's final answer the user did not answer."""
        events = await self._data.read_events(scope, after_sequence=max(0, last_sequence - TAIL_EVENTS))
        if not events or not isinstance(events[-1].payload, AssistantFinal):
            return False
        opener = next((e for e in reversed(events) if isinstance(e.payload, (UserMessage, Wake))), None)
        return opener is not None and isinstance(opener.payload, Wake)

    # --- the status (§11) ---

    async def status(self, principal: PrincipalId) -> dict[str, object]:
        situation, _ = await self.situation(principal)
        midnight = local_midnight(situation.now, situation.zone)
        today = [w for w in situation.wakes if w.created_at >= midnight and spoke(w, situation.said)
                 and not all(r.kind is WakeKind.REMINDER for r in w.reasons)]
        look = self.looks.get(principal)
        _, _, upcoming = candidates(situation)
        return {
            "today": len(today), "daily": self.settings.daily,
            "last_wake_at": max((w.created_at for w in situation.wakes), default=None),
            "next_loop_at": upcoming,
            "last_look": None if look is None else {
                "at": look.at, "outcome": look.outcome,
                "held": [c.reason.kind.value for c in look.decision.held]},
        }
