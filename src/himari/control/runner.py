"""Background ConversationRunner (CTRL-06) over a ConversationDriver."""

from __future__ import annotations

import asyncio
import logging
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
)
from himari.data.api import DataError, UserMessage

log = logging.getLogger(__name__)

type _Key = tuple[PrincipalId, ConversationId]


def _classify(error: BaseException) -> str:
    if isinstance(error, (ControlError, DataError)):
        return str(error.code.value)
    return "internal_error"


class BackgroundRunner:
    def __init__(self, driver: ConversationDriver) -> None:
        self._driver = driver
        self._status: dict[_Key, JobStatus] = {}
        self._tasks: dict[_Key, asyncio.Task[None]] = {}
        self._signals: dict[_Key, StopSignal] = {}  # of the running (or last) job

    @staticmethod
    def _key(scope: TrustedScope) -> _Key:
        return (scope.principal_id, scope.conversation_id)

    def _running_in(self, conversation_id: ConversationId) -> bool:
        return any(key[1] == conversation_id and not task.done() for key, task in self._tasks.items())

    async def submit(self, scope: TrustedScope, message: UserMessage | None) -> JobStatus:
        key = self._key(scope)
        if self._running_in(scope.conversation_id):
            raise ControlError(ControlErrorCode.BUSY, scope.conversation_id)
        if message is not None:
            await self._driver.accept(scope, message)  # durable before anything is reported
            if self._running_in(scope.conversation_id):  # cannot happen: accept holds the driver lock
                raise ControlError(ControlErrorCode.BUSY, scope.conversation_id)
        return self._start(scope, lambda s, stop: self._driver.resume(s, stop=stop), "drive")

    async def submit_compaction(self, scope: TrustedScope) -> JobStatus:
        if self._running_in(scope.conversation_id):
            raise ControlError(ControlErrorCode.BUSY, scope.conversation_id)
        return self._start(scope, lambda s, stop: self._driver.compact(s, stop=stop), "compact")

    def request_pause(self, scope: TrustedScope) -> JobStatus:
        return self._signal(scope, StopSignal.pause)

    def cancel(self, scope: TrustedScope) -> JobStatus:
        return self._signal(scope, StopSignal.cancel)

    async def submit_cancel(self, scope: TrustedScope) -> JobStatus:
        if self._running_in(scope.conversation_id):
            raise ControlError(ControlErrorCode.BUSY, scope.conversation_id)
        return self._start(scope, lambda s, _stop: self._driver.cancel(s), "cancel")

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

    def _start(self, scope: TrustedScope, job: Callable[[TrustedScope, StopSignal], Awaitable[DriveResult]],
               name: str) -> JobStatus:
        key = self._key(scope)
        previous = self._status.get(key, JobStatus(running=False))
        status = JobStatus(running=True, generation=previous.generation + 1, started_at=datetime.now(UTC))
        self._status[key] = status
        signal = self._signals[key] = StopSignal()
        self._tasks[key] = asyncio.create_task(self._run(key, scope, lambda s: job(s, signal)),
                                               name=f"{name}:{scope.conversation_id}")
        return status

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
