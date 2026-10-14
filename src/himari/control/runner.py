"""Background ConversationRunner (CTRL-06) over a ConversationDriver."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import UTC, datetime

from himari.common import ConversationId, PrincipalId, TrustedScope
from himari.control.api import ControlError, ControlErrorCode, ConversationDriver, JobStatus
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
        previous = self._status.get(key, JobStatus(running=False))
        status = JobStatus(running=True, generation=previous.generation + 1, started_at=datetime.now(UTC))
        self._status[key] = status
        self._tasks[key] = asyncio.create_task(self._run(key, scope), name=f"drive:{scope.conversation_id}")
        return status

    async def _run(self, key: _Key, scope: TrustedScope) -> None:
        current = self._status[key]
        try:
            result = await self._driver.resume(scope)
        except asyncio.CancelledError:
            self._status[key] = replace(current, running=False, error="cancelled", finished_at=datetime.now(UTC))
            raise
        except Exception as error:
            log.exception("background job for %s failed", scope.conversation_id)
            self._status[key] = replace(current, running=False, error=_classify(error), finished_at=datetime.now(UTC))
        else:
            self._status[key] = replace(current, running=False, result=result, finished_at=datetime.now(UTC))

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
