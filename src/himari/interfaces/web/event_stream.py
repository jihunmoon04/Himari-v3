"""The principal's event stream as Server-Sent Events (V3-P83 §8.1, appendix A.3).

``GET /api/events`` sends ``hello`` first — the whole state; a reader
replaces its own with it — then what ``EventHub`` publishes: ``activity``,
``job``, ``conversation``, ``current``, and for a device ``revoked`` or
``replaced`` (after which the stream ends). There is no replay: a reader
that reconnects starts again from ``hello``. A comment line comes every
``HEARTBEAT_SECONDS``; the stream ends after ``MAX_SECONDS`` (the reader
reopens at once), or as soon as the server begins to shut down.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

from himari.common import thaw
from himari.interfaces.web.live import frame
from himari.presence.api import Subscription

HEARTBEAT_SECONDS = 15.0
MAX_SECONDS = 300.0
RETRY_MS = 1000
ENDING = frozenset({"revoked", "replaced"})  # the stream ends after these


async def stream(subscription: Subscription, hello: dict[str, Any], closing: asyncio.Event, *,
                 clock: Callable[[], float] = time.monotonic, heartbeat: float | None = None,
                 limit: float | None = None) -> AsyncIterator[str]:
    """``heartbeat`` and ``limit``: HEARTBEAT_SECONDS and MAX_SECONDS, read when it starts."""
    heartbeat = HEARTBEAT_SECONDS if heartbeat is None else heartbeat
    yield f"retry: {RETRY_MS}\n\n"
    yield frame("hello", hello)
    deadline = clock() + (MAX_SECONDS if limit is None else limit)
    while not closing.is_set():
        remaining = deadline - clock()
        if remaining <= 0:
            return
        upcoming = asyncio.ensure_future(subscription.next(min(heartbeat, remaining)))
        stop = asyncio.ensure_future(closing.wait())
        try:
            await asyncio.wait((upcoming, stop), return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop.cancel()
        if not upcoming.done():
            upcoming.cancel()
            return  # closing
        event = upcoming.result()
        if event is not None:
            yield frame(event.name, thaw(event.data))  # type: ignore[arg-type]
            if event.name in ENDING:
                return
        elif subscription.finished:
            return  # fallen behind, or ended by the hub
        else:
            yield ": keep-alive\n\n"
