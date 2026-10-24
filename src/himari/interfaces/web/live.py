"""The live output of a Conversation as Server-Sent Events (V3-P73 §6.1).

``GET /api/conversations/{id}/live`` sends what a streamed Run is writing:

- ``snapshot {run_id, text, tools, thinking, truncated, ended}``: the whole
  state, first and whenever the Run changed;
- ``idle {}``: there is no state;
- ``delta {run_id, text, tools, thinking?, truncated?}``: what to append;
- ``end {run_id}``: that Run's outcome is recorded;
- ``done {}``: the job has ended and everything was sent; the page closes
  its EventSource (otherwise it would reconnect).

A stream keeps a cursor of what it sent and, each time it wakes, compares the
Runner's accumulated state with it (§5.2): wakeups may coalesce, nothing is
lost, and when the Run changed it resynchronizes with a snapshot instead of
guessing. It sends a comment line when idle for ``HEARTBEAT_SECONDS`` and
ends after ``MAX_SECONDS`` (the production proxy cuts a response at 600 s;
the browser then reconnects and starts from a snapshot), or as soon as the
server begins to shut down (``closing``): uvicorn waits for open responses
before it stops the application, and a job may run for long.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from himari.common import RunId, TrustedScope
from himari.control.api import JobStatus, LiveOutput

HEARTBEAT_SECONDS = 15.0
MAX_SECONDS = 300.0
PER_PRINCIPAL = 8  # open streams of one principal
RETRY_MS = 1000  # how soon a browser reconnects after a stream ends without ``done``


class Slots:
    """Open live streams per principal, at most ``PER_PRINCIPAL``. A slot is
    taken in the same synchronous step as the check, before the response
    starts (PR #83 review B1: counting when the stream began let requests
    arriving together all pass), and given back exactly once."""

    def __init__(self) -> None:
        self._open: dict[Any, int] = {}

    def take(self, who: Any) -> Callable[[], None] | None:
        """A release for a new slot of ``who``; None when all are taken."""
        if self._open.get(who, 0) >= PER_PRINCIPAL:
            return None
        self._open[who] = self._open.get(who, 0) + 1
        released = False

        def release() -> None:
            nonlocal released
            if released:
                return
            released = True
            self._open[who] -= 1
            if not self._open[who]:
                del self._open[who]

        return release

    def open(self, who: Any) -> int:
        return self._open.get(who, 0)


class LiveSource(Protocol):
    """The Runner's live output (control.runner.BackgroundRunner)."""

    def live_output(self, scope: TrustedScope, *, text: bool = True) -> LiveOutput | None: ...

    def live_text(self, scope: TrustedScope, run_id: RunId, start: int) -> str | None: ...

    def live_version(self, scope: TrustedScope) -> int: ...

    async def wait_live(self, scope: TrustedScope, after_version: int, timeout: float) -> int: ...

    def status(self, scope: TrustedScope) -> JobStatus: ...


@dataclass
class Cursor:
    """What one stream has sent."""

    sent: bool = False  # anything at all (``idle`` counts)
    run_id: RunId | None = None
    chars: int = 0
    tools: int = 0
    thinking: bool = False
    truncated: bool = False
    ended: bool = False

    def restart(self, state: LiveOutput | None) -> None:
        """Sent from scratch: ``state`` whole, or ``idle`` for None."""
        self.sent = True
        self.run_id = None if state is None else state.run_id
        self.chars = 0 if state is None else state.chars
        self.tools = 0 if state is None else len(state.tools)
        self.thinking = state is not None and state.thinking
        self.truncated = state is not None and state.truncated
        self.ended = state is not None and state.ended


def frame(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


def step(source: LiveSource, scope: TrustedScope, cursor: Cursor) -> list[str]:
    """The events that bring a browser holding ``cursor`` up to the state now."""
    state = source.live_output(scope, text=False)
    if state is None:
        if cursor.sent and cursor.run_id is None:
            return []
        cursor.restart(None)
        return [frame("idle", {})]
    if state.run_id != cursor.run_id:
        whole = source.live_output(scope)
        assert whole is not None  # the same state: nothing runs between the two reads
        cursor.restart(whole)
        return [frame("snapshot", {"run_id": whole.run_id, "text": whole.text, "tools": list(whole.tools),
                                   "thinking": whole.thinking, "truncated": whole.truncated, "ended": whole.ended})]
    out: list[str] = []
    text = source.live_text(scope, state.run_id, cursor.chars) or ""
    tools = list(state.tools[cursor.tools:])
    change: dict[str, Any] = {}
    if state.thinking and not cursor.thinking:
        change["thinking"] = True
    if state.truncated and not cursor.truncated:
        change["truncated"] = True
    if text or tools or change:
        out.append(frame("delta", {"run_id": state.run_id, "text": text, "tools": tools, **change}))
        cursor.chars += len(text)
        cursor.tools += len(tools)
        cursor.thinking = state.thinking
        cursor.truncated = state.truncated
    if state.ended and not cursor.ended:
        out.append(frame("end", {"run_id": state.run_id}))
        cursor.ended = True
    return out


async def stream(source: LiveSource, scope: TrustedScope, closing: asyncio.Event, *,
                 clock: Callable[[], float] = time.monotonic, heartbeat: float = HEARTBEAT_SECONDS,
                 limit: float = MAX_SECONDS) -> AsyncIterator[str]:
    yield f"retry: {RETRY_MS}\n\n"
    cursor = Cursor()
    deadline = clock() + limit
    while not closing.is_set():
        version = source.live_version(scope)
        for event in step(source, scope, cursor):
            yield event
        if not source.status(scope).running and (cursor.run_id is None or cursor.ended):
            yield frame("done", {})
            return
        remaining = deadline - clock()
        if remaining <= 0:
            return  # the browser reconnects and starts from a snapshot
        change = asyncio.ensure_future(source.wait_live(scope, version, min(heartbeat, remaining)))
        stop = asyncio.ensure_future(closing.wait())
        try:
            await asyncio.wait((change, stop), return_when=asyncio.FIRST_COMPLETED)
        finally:
            change.cancel()
            stop.cancel()
        if not closing.is_set() and source.live_version(scope) == version and source.status(scope).running:
            yield ": keep-alive\n\n"
