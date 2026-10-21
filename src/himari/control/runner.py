"""Background ConversationRunner (CTRL-06) over a ConversationDriver.

Each job's drive reports its usage after every Run (``TurnProgress``,
V3-P62 §7); the latest report is the job's ``turn_usage``.

Closing (V3-P65 §7.1): ``close`` stops a Conversation from taking work —
every later submit, and every start after an input already being accepted,
is refused with CLOSED — then waits for those inputs to be accepted and for
a running job, cancelled, to end. The runner checks no permission: it is
told to close only a Conversation whose owner was checked. With
``unattended_runs``, a drive that has run that many Runs while no client
read the Conversation (``seen`` within ``seen_seconds``) is asked to pause,
with the reason (§5). Seen means a connected client, not a person watching.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime

from himari.common import ConversationId, PrincipalId, TrustedScope
from himari.control.api import (
    ControlError,
    ControlErrorCode,
    ConversationDriver,
    DriveResult,
    JobStatus,
    StopSignal,
    TurnProgress,
    TurnUsage,
)
from himari.data.api import DataError, UserMessage

log = logging.getLogger(__name__)

type _Key = tuple[PrincipalId, ConversationId]


def _classify(error: BaseException) -> str:
    if isinstance(error, (ControlError, DataError)):
        return str(error.code.value)
    return "internal_error"


UNATTENDED_REASON = "paused: no client was reading this conversation"
SEEN_SECONDS = 60.0  # V3-P62 O2


class _JobProgress:
    """The TurnProgress of one job: hands every report to the runner."""

    def __init__(self, runner: BackgroundRunner, key: _Key, signal: StopSignal) -> None:
        self._runner, self._key, self._signal = runner, key, signal

    def report(self, usage: TurnUsage) -> None:
        self._runner._reported(self._key, self._signal, usage)


class BackgroundRunner:
    def __init__(self, driver: ConversationDriver, *, unattended_runs: int | None = None,
                 seen_seconds: float = SEEN_SECONDS, clock: Callable[[], float] = time.monotonic) -> None:
        if unattended_runs is not None and unattended_runs < 1:
            raise ValueError("unattended_runs is positive (or None: never pause for it)")
        self._driver = driver
        self._unattended_runs = unattended_runs
        self._seen_seconds = seen_seconds
        self._clock = clock
        self._status: dict[_Key, JobStatus] = {}
        self._tasks: dict[_Key, asyncio.Task[None]] = {}
        self._signals: dict[_Key, StopSignal] = {}  # of the running (or last) job
        self._seen: dict[ConversationId, float] = {}
        self._runs: dict[_Key, int] = {}  # Runs reported by the running job
        self._unattended: dict[_Key, int] = {}  # of them, run while no client read the Conversation
        self._closed: set[ConversationId] = set()  # V3-P65: taking no work
        self._admitting: dict[ConversationId, int] = {}  # inputs inside driver.accept
        self._admitted: dict[ConversationId, asyncio.Event] = {}  # set when the last one is done

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
            if self._running_in(scope.conversation_id):  # cannot happen: accept holds the driver lock
                raise ControlError(ControlErrorCode.BUSY, scope.conversation_id)
        # Closed meanwhile: the input stays recorded, unanswered; _start refuses.
        return self._start(scope, lambda s, stop, progress: self._driver.resume(s, stop=stop, progress=progress),
                           "drive")

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
        for kept in (self._status, self._tasks, self._signals, self._runs, self._unattended):
            kept.pop(key, None)
        self._seen.pop(scope.conversation_id, None)

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

    def _start(self, scope: TrustedScope,
               job: Callable[[TrustedScope, StopSignal, TurnProgress], Awaitable[DriveResult]],
               name: str) -> JobStatus:
        """Synchronous: nothing closes the Conversation between this check and the start."""
        self._check_open(scope)
        key = self._key(scope)
        previous = self._status.get(key, JobStatus(running=False))
        status = JobStatus(running=True, generation=previous.generation + 1, started_at=datetime.now(UTC))
        self._status[key] = status
        signal = self._signals[key] = StopSignal()
        self.seen(scope)  # the client that started it is connected
        self._runs[key] = 0
        progress = _JobProgress(self, key, signal)
        self._tasks[key] = asyncio.create_task(self._run(key, scope, lambda s: job(s, signal, progress)),
                                               name=f"{name}:{scope.conversation_id}")
        return status

    def _reported(self, key: _Key, signal: StopSignal, usage: TurnUsage) -> None:
        if self._signals.get(key) is not signal:
            return  # a report of an earlier job
        self._status[key] = replace(self._status[key], turn_usage=usage)
        new_runs = usage.runs - self._runs.get(key, 0)
        self._runs[key] = usage.runs
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

    def status(self, scope: TrustedScope) -> JobStatus:
        return self._status.get(self._key(scope), JobStatus(running=False))

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
