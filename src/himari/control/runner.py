"""Background ConversationRunner (CTRL-06) over a ConversationDriver.

Each job's drive reports its usage after every Run (``TurnProgress``,
V3-P62 §7); the latest report is the job's ``turn_usage``.

Closing (V3-P65 §7.1): ``close`` stops a Conversation from taking work —
every later submit, and every start after an input already being accepted,
is refused with CLOSED — then waits for those inputs to be accepted and for
a running job, cancelled, to end. The runner checks no permission: it is
told to close only a Conversation whose owner was checked.

Holding (V3-P76 §6): ``hold`` keeps a Conversation from starting work
while the owner accepts or reverts its file changes — refused with BUSY
when a job runs or an input is being accepted, and every submit and start
meanwhile is refused with BUSY; nothing is cancelled. With
``unattended_runs``, a drive that has run that many Runs while no client
read the Conversation (``seen`` within ``seen_seconds``) is asked to pause,
with the reason (§5). Seen means a connected client, not a person watching.

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
"""

from __future__ import annotations

import asyncio
import bisect
import logging
import time
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
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
from himari.data.api import DataError, MessageOrigin, UserMessage
from himari.provider.api import OutputDelta

log = logging.getLogger(__name__)

type _Key = tuple[PrincipalId, ConversationId]


def _classify(error: BaseException) -> str:
    if isinstance(error, (ControlError, DataError)):
        return str(error.code.value)
    return "internal_error"


UNATTENDED_REASON = "paused: no client was reading this conversation"
SEEN_SECONDS = 60.0  # V3-P62 O2


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

    def output_started(self, run_id: RunId) -> None:
        self._runner._live_changed(self._key, self._signal, run_id, lambda live: None, start=True)

    def output(self, run_id: RunId, delta: OutputDelta) -> None:
        self._runner._live_changed(self._key, self._signal, run_id, lambda live: live.add(delta))

    def output_ended(self, run_id: RunId) -> None:
        def end(live: _Live) -> None:
            live.ended = True

        self._runner._live_changed(self._key, self._signal, run_id, end)


class BackgroundRunner:
    def __init__(self, driver: ConversationDriver, *, unattended_runs: int | None = None,
                 seen_seconds: float = SEEN_SECONDS, clock: Callable[[], float] = time.monotonic,
                 listener: JobListener | None = None) -> None:
        if unattended_runs is not None and unattended_runs < 1:
            raise ValueError("unattended_runs is positive (or None: never pause for it)")
        self._driver = driver
        self._unattended_runs = unattended_runs
        self._seen_seconds = seen_seconds
        self._clock = clock
        self._listener = listener
        self._status: dict[_Key, JobStatus] = {}
        self._tasks: dict[_Key, asyncio.Task[None]] = {}
        self._signals: dict[_Key, StopSignal] = {}  # of the running (or last) job
        self._seen: dict[ConversationId, float] = {}
        self._runs: dict[_Key, int] = {}  # Runs reported by the running job
        self._unattended: dict[_Key, int] = {}  # of them, run while no client read the Conversation
        self._closed: set[ConversationId] = set()  # V3-P65: taking no work
        self._held: set[ConversationId] = set()  # V3-P76: starting no work for now
        self._admitting: dict[ConversationId, int] = {}  # inputs inside driver.accept
        self._admitted: dict[ConversationId, asyncio.Event] = {}  # set when the last one is done
        self._live: dict[_Key, _Live] = {}  # V3-P73 §5.2: the latest streamed Run of the job
        self._live_versions: dict[_Key, int] = {}
        self._live_changes: dict[_Key, asyncio.Event] = {}  # set (and dropped) at the next change

    @staticmethod
    def _key(scope: TrustedScope) -> _Key:
        return (scope.principal_id, scope.conversation_id)

    def _running_in(self, conversation_id: ConversationId) -> bool:
        return any(key[1] == conversation_id and not task.done() for key, task in self._tasks.items())

    async def submit(self, scope: TrustedScope, message: UserMessage | None) -> JobStatus:
        self._check_open(scope)
        if self._running_in(scope.conversation_id):
            raise ControlError(ControlErrorCode.BUSY, scope.conversation_id)
        if message is not None:
            self._admit(scope.conversation_id)
            try:
                await self._driver.accept(scope, message)  # durable before anything is reported
            finally:
                self._admit(scope.conversation_id, -1)
            self._tell(lambda listener: listener.accepted(scope))
            if self._running_in(scope.conversation_id):  # cannot happen: accept holds the driver lock
                raise ControlError(ControlErrorCode.BUSY, scope.conversation_id)
        # Closed meanwhile: the input stays recorded, unanswered; _start refuses.
        return self._start(scope, lambda s, stop, progress: self._driver.resume(s, stop=stop, progress=progress),
                           "drive", None if message is None else message.origin)

    def _check_open(self, scope: TrustedScope) -> None:
        if scope.conversation_id in self._closed:
            raise ControlError(ControlErrorCode.CLOSED, scope.conversation_id)
        if scope.conversation_id in self._held:
            raise ControlError(ControlErrorCode.BUSY, scope.conversation_id)

    @contextmanager
    def hold(self, scope: TrustedScope) -> Iterator[None]:
        """V3-P76 §6: no work starts in this Conversation inside. BUSY if a
        job runs, an input is being accepted, or it is held already; CLOSED
        if it is closed. Synchronous: no job starts between the check and the hold."""
        conversation_id = scope.conversation_id
        self._check_open(scope)
        if self._running_in(conversation_id) or self._admitting.get(conversation_id):
            raise ControlError(ControlErrorCode.BUSY, conversation_id)
        self._held.add(conversation_id)
        try:
            yield
        finally:
            self._held.discard(conversation_id)

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
        for kept in (self._status, self._tasks, self._signals, self._runs, self._unattended, self._live,
                     self._live_versions):
            kept.pop(key, None)
        self._seen.pop(scope.conversation_id, None)
        self._notify_live(key)

    async def submit_compaction(self, scope: TrustedScope) -> JobStatus:
        if self._running_in(scope.conversation_id):
            raise ControlError(ControlErrorCode.BUSY, scope.conversation_id)
        return self._start(scope, lambda s, stop, progress: self._driver.compact(s, stop=stop, progress=progress),
                           "compact")

    def request_pause(self, scope: TrustedScope, reason: str | None = None) -> JobStatus:
        return self._signal(scope, lambda signal: signal.pause(reason))

    def cancel(self, scope: TrustedScope) -> JobStatus:
        return self._signal(scope, StopSignal.cancel)

    def seen(self, scope: TrustedScope) -> None:
        """A client read this Conversation (V3-P62 §5)."""
        self._seen[scope.conversation_id] = self._clock()
        self._unattended[self._key(scope)] = 0

    async def submit_cancel(self, scope: TrustedScope) -> JobStatus:
        if self._running_in(scope.conversation_id):
            raise ControlError(ControlErrorCode.BUSY, scope.conversation_id)
        return self._start(scope, lambda s, _stop, _progress: self._driver.cancel(s), "cancel")

    def _signal(self, scope: TrustedScope, ask: Callable[[StopSignal], None]) -> JobStatus:
        key = self._key(scope)
        if self._running(key):
            signal = self._signals[key]
            ask(signal)
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
               name: str, origin: MessageOrigin | None = None) -> JobStatus:
        """Synchronous: nothing closes the Conversation between this check and the start."""
        self._check_open(scope)
        key = self._key(scope)
        previous = self._status.get(key, JobStatus(running=False))
        status = JobStatus(running=True, generation=previous.generation + 1, started_at=datetime.now(UTC))
        self._status[key] = status
        signal = self._signals[key] = StopSignal()
        self.seen(scope)  # the client that started it is connected
        if self._live.pop(key, None) is not None:  # the last job's output is not this one's
            self._bump_live(key)
        self._runs[key] = 0
        progress = _JobProgress(self, key, signal)
        self._tasks[key] = asyncio.create_task(self._run(key, scope, lambda s: job(s, signal, progress)),
                                               name=f"{name}:{scope.conversation_id}")
        self._tell(lambda listener: listener.job_started(scope, origin))
        return status

    def _reported(self, key: _Key, signal: StopSignal, usage: TurnUsage) -> None:
        if self._signals.get(key) is not signal:
            return  # a report of an earlier job
        self._status[key] = replace(self._status[key], turn_usage=usage)
        new_runs = usage.runs - self._runs.get(key, 0)
        self._runs[key] = usage.runs
        if new_runs:
            self._tell(lambda listener: listener.run_ended(TrustedScope(*key)))
        if self._unattended_runs is None:
            return
        seen = self._seen.get(key[1])
        if seen is not None and self._clock() - seen <= self._seen_seconds:
            self._unattended[key] = 0
            return
        self._unattended[key] = self._unattended.get(key, 0) + new_runs
        if self._unattended[key] >= self._unattended_runs and not signal.pause_requested:
            signal.pause(UNATTENDED_REASON)
            self._status[key] = replace(self._status[key], stop_requested=signal.request)

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

    def running(self, principal: PrincipalId) -> Sequence[ConversationId]:
        return tuple(key[1] for key, task in self._tasks.items() if key[0] == principal and not task.done())

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
