"""Background ConversationRunner (CTRL-06) over a ConversationDriver.

Each job's drive reports its usage after every Run (``TurnProgress``,
V3-P62 §7); the latest report is the job's ``turn_usage``.

Closing (V3-P65 §7.1): ``close`` stops a Conversation from taking work —
every later submit, and every start after an input already being accepted,
is refused with CLOSED — then waits for those inputs to be accepted and for
a running job, cancelled, to end. The runner checks no permission: it is
told to close only a Conversation whose owner was checked.

Live output (V3-P73 §5.2): what a job's streamed Run writes is kept per
Conversation as one accumulated state — the authoritative value a display
reads, never a queue of pieces. ``live_output`` gives the state,
``live_text`` the text from a character on, and ``wait_live`` waits for a
change past a version or the job's end. Wakeups may coalesce; a reader that compares the state
with what it has sent loses nothing. An ended Run's state stays until the
next Run of the job starts, the next job starts, or the Conversation is
forgotten.

Listening (V3-P83 §2.2): a ``JobListener`` is told when an input is
accepted, a job starts (with its message's origin), a Run ends and a job ends.

The day (character-day §3.2, §6): ``day_closing`` (set by the composition root)
says a day session is closed or fenced for its closing; a resume or a
compaction is then refused DataError DAY_CLOSED, checked with no await before
its job starts (a message's and a wake's admission is the Driver's, under the
count of inputs being taken, which the day's keeper sees in ``working``).
"""

from __future__ import annotations

import asyncio
import bisect
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime

from himari.common import ConversationId, PrincipalId, RunId, TrustedScope
from himari.control.api import (
    LIVE_TEXT_CHARS,
    ControlError,
    ControlErrorCode,
    ConversationDriver,
    DriveResult,
    JobListener,
    JobStatus,
    LiveOutput,
    StopSignal,
    TurnProgress,
    TurnUsage,
)
from himari.data.api import (
    AsleepAnswer, DataError, DataErrorCode, MessageOrigin, OriginKind, UserMessage, Wake, WakeReason,
)
from himari.provider.api import OutputDelta

log = logging.getLogger(__name__)

type _Key = tuple[PrincipalId, ConversationId]


def _classify(error: BaseException) -> str:
    if isinstance(error, (ControlError, DataError)):
        return str(error.code.value)
    return "internal_error"


WAKE_RUNS = 3  # V3-P87 §4.2: a server-opened turn stays short
FREE_RUNS = 2  # V3-P90 §7.3: a free wake — one to look (the screen tool, later), one to speak
WAKE_YIELD_SECONDS = 10.0  # §4.3: how long a message waits for the wake job it cancels


class _Live:
    """One Run's accumulated output: pieces with their end offsets, so a
    reader takes the text from any character without joining it all."""

    def __init__(self, run_id: RunId) -> None:
        self.run_id = run_id
        self.pieces: list[str] = []
        self.ends: list[int] = []  # the text's length after each piece
        self.tools: list[str] = []
        self.thinking = False
        self.truncated = False
        self.ended = False

    @property
    def chars(self) -> int:
        return self.ends[-1] if self.ends else 0

    def add(self, delta: OutputDelta) -> None:
        if delta.text and not self.truncated:  # once cut, nothing after: no holes in the text
            if self.chars + len(delta.text) > LIVE_TEXT_CHARS:
                self.truncated = True  # display only: the raw response has it all
            else:
                self.pieces.append(delta.text)
                self.ends.append(self.chars + len(delta.text))
        if delta.tool is not None:
            self.tools.append(delta.tool)
        if delta.thinking:
            self.thinking = True

    def text_from(self, start: int) -> str:
        first = bisect.bisect_right(self.ends, start)
        if first >= len(self.pieces):
            return ""
        offset = start - (self.ends[first - 1] if first else 0)
        return self.pieces[first][offset:] + "".join(self.pieces[first + 1:])


class _JobProgress:
    """The TurnProgress (and OutputProgress, V3-P73) of one job: hands every
    report to the runner."""

    def __init__(self, runner: BackgroundRunner, key: _Key, signal: StopSignal) -> None:
        self._runner, self._key, self._signal = runner, key, signal

    def report(self, usage: TurnUsage) -> None:
        self._runner._reported(self._key, self._signal, usage)

    def reveal(self) -> None:
        """V3-P90 §7.3: the free wake decided to speak (Data says so already): show its job now."""
        self._runner._reveal(self._key, self._signal)

    def output_started(self, run_id: RunId) -> None:
        self._runner._live_changed(self._key, self._signal, run_id, lambda live: None, start=True)

    def output(self, run_id: RunId, delta: OutputDelta) -> None:
        self._runner._live_changed(self._key, self._signal, run_id, lambda live: live.add(delta))

    def output_ended(self, run_id: RunId) -> None:
        def end(live: _Live) -> None:
            live.ended = True

        self._runner._live_changed(self._key, self._signal, run_id, end)


class BackgroundRunner:
    def __init__(self, driver: ConversationDriver, *, listener: JobListener | None = None) -> None:
        self._driver = driver
        self._listener = listener
        self._status: dict[_Key, JobStatus] = {}
        self._tasks: dict[_Key, asyncio.Task[None]] = {}
        self._signals: dict[_Key, StopSignal] = {}  # of the running (or last) job
        self._runs: dict[_Key, int] = {}  # Runs reported by the running job
        self._closed: set[ConversationId] = set()  # V3-P65: taking no work
        self._wake_jobs: dict[_Key, str] = {}  # V3-P87: the running (or last) job's wake, by Conversation
        self._origins: dict[_Key, MessageOrigin | None] = {}  # the running job's origin, for a later reveal
        self._admitting: dict[ConversationId, int] = {}  # inputs inside driver.accept
        self._admitted: dict[ConversationId, asyncio.Event] = {}  # set when the last one is done
        self._live: dict[_Key, _Live] = {}  # V3-P73 §5.2: the latest streamed Run of the job
        self._live_versions: dict[_Key, int] = {}
        self._live_changes: dict[_Key, asyncio.Event] = {}  # set (and dropped) at the next change
        self.day_closing: Callable[[TrustedScope], bool] = lambda scope: False  # character-day §6, set by bootstrap

    @staticmethod
    def _key(scope: TrustedScope) -> _Key:
        return (scope.principal_id, scope.conversation_id)

    def _running_in(self, conversation_id: ConversationId) -> bool:
        return any(key[1] == conversation_id and not task.done() for key, task in self._tasks.items())

    def working(self, scope: TrustedScope) -> bool:
        """character-day §6: a job runs in the Conversation, or an input (a message, a wake) is being taken into it
        — the day's keeper never closes a day session under either."""
        return self._running_in(scope.conversation_id) or bool(self._admitting.get(scope.conversation_id))

    def _check_day(self, scope: TrustedScope) -> None:
        if self.day_closing(scope):
            raise DataError(DataErrorCode.DAY_CLOSED, scope.conversation_id)

    async def submit(self, scope: TrustedScope, message: UserMessage | None) -> JobStatus:
        self._check_open(scope)
        # character-day §6: a day closing or closed takes no work — a resume starts below with no await (nothing
        # closes the day between); a message is refused before it cancels a wake the fence lets settle (second
        # review 9; the Driver's admission checks again, durably)
        self._check_day(scope)
        if message is not None:
            await self._yield_wake(scope)
        if self._running_in(scope.conversation_id):
            raise ControlError(ControlErrorCode.BUSY, scope.conversation_id)
        if message is not None:
            self._admit(scope.conversation_id)
            try:
                event = await self._driver.accept(scope, message)  # durable before anything is reported
            finally:
                self._admit(scope.conversation_id, -1)
            self._tell(lambda listener: listener.accepted(scope))
            if self._running_in(scope.conversation_id):  # cannot happen: accept holds the driver lock
                raise ControlError(ControlErrorCode.BUSY, scope.conversation_id)
            asleep = (event.payload.while_asleep if event is not None and isinstance(event.payload, UserMessage)
                      else None)
            if asleep is AsleepAnswer.NONE:  # character-day §5.5: HIMARI only stirred — recorded, no job
                # a receipt of its own: idle, no job (an earlier job's id or result is not this message's)
                return JobStatus(running=False, generation=self.status(scope).generation, asleep=asleep)
            # Closed meanwhile: the input stays recorded, unanswered; _start refuses.
            return replace(self._start(scope, lambda s, stop, progress: self._driver.resume(
                s, stop=stop, progress=progress), "drive", message.origin), asleep=asleep)
        return self._start(scope, lambda s, stop, progress: self._driver.resume(s, stop=stop, progress=progress),
                           "drive", None)

    async def _yield_wake(self, scope: TrustedScope) -> None:
        """V3-P87 §4.3 (the user wins): a running wake job is asked to cancel and
        given WAKE_YIELD_SECONDS to end; still running, the message is refused
        BUSY by the caller, never accepted beside it (one job per Conversation)."""
        key = self._key(scope)
        task = self._tasks.get(key)
        if task is None or task.done() or key not in self._wake_jobs:
            return
        self.cancel(scope)
        await asyncio.wait([task], timeout=WAKE_YIELD_SECONDS)  # never cancels the task itself

    async def submit_wake(self, scope: TrustedScope, reasons: Sequence[WakeReason], device_id: str, *,
                          keys: Sequence[str] = (), reminders: Sequence[int] = (), alone: bool = False,
                          free: bool = False) -> JobStatus:
        """V3-P87 §4.1: record the wake (NOT_SETTLED, DataError DUPLICATE_IDENTITY /
        INVALID_STATE_TRANSITION as the driver and Data refuse) and start its
        job, spoken on ``device_id``, at most WAKE_RUNS Runs. BUSY while a job
        runs or an input is being accepted — with ``alone`` (§6.1, PR #156 review
        P2), in any of the principal's Conversations; CLOSED in the trash. These
        checks run before the first await: the caller's own checks just before
        the call hold with them."""
        self._check_open(scope)
        if self._running_in(scope.conversation_id) or self._admitting.get(scope.conversation_id):
            raise ControlError(ControlErrorCode.BUSY, scope.conversation_id)
        if alone and self.running(scope.principal_id):
            raise ControlError(ControlErrorCode.BUSY, scope.conversation_id)
        self._admit(scope.conversation_id)
        try:
            event = await self._driver.accept_wake(scope, reasons, device_id, keys=keys, reminders=reminders)
        finally:
            self._admit(scope.conversation_id, -1)
        assert isinstance(event.payload, Wake)
        refusal = (ControlErrorCode.CLOSED if scope.conversation_id in self._closed
                   else ControlErrorCode.BUSY if alone and self.running(scope.principal_id) else None)
        if refusal is not None:
            # PR #156 re-review P2-1: while the wake was recorded, a job started in another of
            # the principal's Conversations (or this one is being closed). The user wins: the
            # wake never starts; it is withdrawn (cancelled, its keys back: it said nothing).
            # No await from this check to _start below, so a wake job never runs beside
            # another job of the principal.
            await self._driver.withdraw_wake(scope, event.payload.wake_id)
            if not free:
                self._tell(lambda listener: listener.accepted(scope))  # its Event is there: the views read again
            raise ControlError(refusal, scope.conversation_id)
        if not free:  # V3-P90 §7.3: a free wake's Event is not shown until it speaks
            self._tell(lambda listener: listener.accepted(scope))
        key = self._key(scope)
        self._wake_jobs[key] = event.payload.wake_id
        wake_id = event.payload.wake_id
        return self._start(scope, lambda s, stop, progress: self._driver.resume_wake(
            s, wake_id, stop=stop, progress=progress, max_runs=FREE_RUNS if free else WAKE_RUNS, free=free),
                           "wake", MessageOrigin(OriginKind.WAKE, device_id), wake_id=wake_id, free=free)

    def _check_open(self, scope: TrustedScope) -> None:
        if scope.conversation_id in self._closed:
            raise ControlError(ControlErrorCode.CLOSED, scope.conversation_id)

    def _admit(self, conversation_id: ConversationId, change: int = 1) -> None:
        count = self._admitting.get(conversation_id, 0) + change
        if count:
            self._admitting[conversation_id] = count
            self._admitted.setdefault(conversation_id, asyncio.Event())
        else:  # none left: wake whoever waits in close, keep nothing
            self._admitting.pop(conversation_id, None)
            self._admitted.pop(conversation_id).set()

    async def close(self, scope: TrustedScope, timeout: float) -> None:
        """V3-P65 §7.1: take no more work for this Conversation, wait for the
        inputs being accepted, then cancel a running job and wait for it to
        end. TimeoutError after ``timeout`` (it stays closed: ``reopen``)."""
        conversation_id = scope.conversation_id
        self._closed.add(conversation_id)

        async def settle() -> None:
            while self._admitting.get(conversation_id):  # its event exists while any is inside
                await self._admitted[conversation_id].wait()
            if self._running(self._key(scope)):
                self.cancel(scope)
                await self.wait(scope)  # never cancels the job itself

        await asyncio.wait_for(settle(), timeout)

    def reopen(self, scope: TrustedScope) -> None:
        """Take work again (a restore, or a trash that did not happen)."""
        self._closed.discard(scope.conversation_id)

    def forget(self, scope: TrustedScope) -> None:
        """Drop what is kept of the Conversation's last job (it is in the
        trash or gone); whether it is closed is unchanged."""
        key = self._key(scope)
        if self._running(key):
            raise ControlError(ControlErrorCode.BUSY, scope.conversation_id)
        for kept in (self._status, self._tasks, self._signals, self._runs, self._live, self._live_versions,
                     self._wake_jobs):
            kept.pop(key, None)
        self._notify_live(key)

    async def submit_compaction(self, scope: TrustedScope) -> JobStatus:
        if self._running_in(scope.conversation_id):
            raise ControlError(ControlErrorCode.BUSY, scope.conversation_id)
        self._check_day(scope)  # character-day §3.2: a closed day is not compacted
        return self._start(scope, lambda s, stop, progress: self._driver.compact(s, stop=stop, progress=progress),
                           "compact")

    def cancel(self, scope: TrustedScope) -> JobStatus:
        key = self._key(scope)
        if self._running(key):
            signal = self._signals[key]
            signal.cancel()
            self._status[key] = replace(self._status[key], stop_requested=signal.request)
        return self.status(scope)

    def _running(self, key: _Key) -> bool:
        task = self._tasks.get(key)
        return task is not None and not task.done()

    def _tell(self, call: Callable[[JobListener], None]) -> None:
        if self._listener is None:
            return
        try:
            call(self._listener)
        except Exception:  # noqa: BLE001 — the event stream is not the job
            log.exception("a job listener failed")

    def _start(self, scope: TrustedScope,
               job: Callable[[TrustedScope, StopSignal, TurnProgress], Awaitable[DriveResult]],
               name: str, origin: MessageOrigin | None = None, *, wake_id: str | None = None,
               free: bool = False) -> JobStatus:
        """Synchronous: nothing closes the Conversation between this check and the start. ``free``
        (V3-P90 §7.3): a free wake's job — hidden, told to no one until it reveals itself."""
        self._check_open(scope)
        key = self._key(scope)
        if wake_id is None:
            self._wake_jobs.pop(key, None)
        previous = self._status.get(key, JobStatus(running=False))
        status = JobStatus(running=True, generation=previous.generation + 1, started_at=datetime.now(UTC),
                           wake_id=wake_id, hidden=free, free=free)
        self._status[key] = status
        signal = self._signals[key] = StopSignal()
        if self._live.pop(key, None) is not None:  # the last job's output is not this one's
            self._bump_live(key)
        self._runs[key] = 0
        progress = _JobProgress(self, key, signal)
        self._tasks[key] = asyncio.create_task(self._run(key, scope, lambda s: job(s, signal, progress)),
                                               name=f"{name}:{scope.conversation_id}")
        self._origins[key] = origin
        if not free:
            self._tell(lambda listener: listener.job_started(scope, origin, status))
        return status

    def _reveal(self, key: _Key, signal: StopSignal) -> None:
        """V3-P90 §7.3: a hidden job shows itself — its job event goes now, its Event is read."""
        status = self._status.get(key)
        if self._signals.get(key) is not signal or status is None or not status.hidden:
            return
        status = self._status[key] = replace(status, hidden=False)
        scope, origin = TrustedScope(*key), self._origins.get(key)
        self._tell(lambda listener: listener.accepted(scope))
        self._tell(lambda listener: listener.job_started(scope, origin, status))

    def _reported(self, key: _Key, signal: StopSignal, usage: TurnUsage) -> None:
        if self._signals.get(key) is not signal:
            return  # a report of an earlier job
        self._status[key] = replace(self._status[key], turn_usage=usage)
        new_runs = usage.runs - self._runs.get(key, 0)
        self._runs[key] = usage.runs
        if new_runs and not self._status[key].hidden:  # V3-P90 §7.3: a hidden job tells nothing
            self._tell(lambda listener: listener.run_ended(TrustedScope(*key)))

    # --- live output (V3-P73 §5.2) ----------------------------------------------------

    def _live_changed(self, key: _Key, signal: StopSignal, run_id: RunId, change: Callable[[_Live], None], *,
                      start: bool = False) -> None:
        if self._signals.get(key) is not signal:
            return  # an earlier job's
        if start:
            self._live[key] = _Live(run_id)
        live = self._live.get(key)
        if live is None or live.run_id != run_id:
            return  # a piece of a Run that is not the latest
        change(live)
        self._bump_live(key)

    def _bump_live(self, key: _Key) -> None:
        self._live_versions[key] = self._live_versions.get(key, 0) + 1
        self._notify_live(key)

    def _notify_live(self, key: _Key) -> None:
        change = self._live_changes.pop(key, None)
        if change is not None:
            change.set()

    def live_version(self, scope: TrustedScope) -> int:
        return self._live_versions.get(self._key(scope), 0)

    def live_output(self, scope: TrustedScope, *, text: bool = True) -> LiveOutput | None:
        """The Conversation's live state; None when there is none. With
        ``text=False`` the text is left out (``chars`` still says its length)."""
        key = self._key(scope)
        live = self._live.get(key)
        if live is None:
            return None
        return LiveOutput(live.run_id, live.text_from(0) if text else "", tuple(live.tools), live.thinking,
                          live.truncated, live.ended, self._live_versions.get(key, 0), live.chars)

    def live_text(self, scope: TrustedScope, run_id: RunId, start: int) -> str | None:
        """The live text of ``run_id`` from character ``start`` on; None when
        the live state is no longer that Run's."""
        live = self._live.get(self._key(scope))
        if live is None or live.run_id != run_id:
            return None
        return live.text_from(start)

    async def wait_live(self, scope: TrustedScope, after_version: int, timeout: float) -> int:
        """Wait until the live version is past ``after_version``, the job
        ends, or ``timeout`` seconds pass; the version then. A reader checks
        what changed itself: a return promises nothing."""
        key = self._key(scope)
        if self._live_versions.get(key, 0) == after_version:
            change = self._live_changes.setdefault(key, asyncio.Event())
            try:
                await asyncio.wait_for(change.wait(), timeout)
            except TimeoutError:
                pass
        return self._live_versions.get(key, 0)

    async def _run(self, key: _Key, scope: TrustedScope,
                   job: Callable[[TrustedScope], Awaitable[DriveResult]]) -> None:
        try:
            result = await job(scope)
        except asyncio.CancelledError:
            self._status[key] = replace(self._status[key], running=False, error="cancelled",
                                        finished_at=datetime.now(UTC))
            raise
        except Exception as error:
            log.exception("background job for %s failed", scope.conversation_id)
            self._status[key] = replace(self._status[key], running=False, error=_classify(error),
                                        finished_at=datetime.now(UTC))
        else:
            self._status[key] = replace(self._status[key], running=False, result=result,
                                        finished_at=datetime.now(UTC))
        finally:
            self._notify_live(key)  # a reader of the live output learns the job ended
            status = self._status[key]
            self._tell(lambda listener: listener.job_ended(scope, status))

    def status(self, scope: TrustedScope) -> JobStatus:
        return self._status.get(self._key(scope), JobStatus(running=False))

    def shown_status(self, scope: TrustedScope) -> JobStatus:
        """What the views are told (V3-P90 §7.3): a hidden job is not running for them."""
        status = self.status(scope)
        return replace(status, running=False) if status.hidden and status.running else status

    def running(self, principal: PrincipalId) -> Sequence[ConversationId]:
        return tuple(key[1] for key, task in self._tasks.items() if key[0] == principal and not task.done())

    def shown_running(self, principal: PrincipalId) -> Sequence[ConversationId]:
        """``running`` as the views are told: without a hidden job (V3-P90 §7.3)."""
        return tuple(cid for cid in self.running(principal)
                     if not self._status.get((principal, cid), JobStatus(running=False)).hidden)

    async def wait(self, scope: TrustedScope) -> JobStatus:
        task = self._tasks.get(self._key(scope))
        if task is not None and not task.done():
            await asyncio.wait([task])  # never cancels the job when the waiter is cancelled
        return self.status(scope)

    async def shutdown(self, grace_seconds: float) -> None:
        running = [task for task in self._tasks.values() if not task.done()]
        if not running:
            return
        _, pending = await asyncio.wait(running, timeout=grace_seconds)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending)
