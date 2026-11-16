"""Blocking work in a worker thread that never outlives its caller."""

from __future__ import annotations

import asyncio
from collections.abc import Callable


async def run_blocking[T](function: Callable[..., T], *args: object) -> T:
    """Run ``function(*args)`` in a worker thread and wait until it has
    finished, even if the caller is cancelled (repeatedly) meanwhile: a
    thread cannot be stopped, so returning early would release the
    Workspace lock — or close a descriptor — while it still works. A
    cancellation that arrived is re-raised once the thread is done."""
    work = asyncio.ensure_future(asyncio.to_thread(function, *args))
    interrupted = False
    while not work.done():
        try:
            await asyncio.wait([work])  # never cancels ``work`` itself
        except asyncio.CancelledError:
            interrupted = True
    if interrupted:
        raise asyncio.CancelledError
    return work.result()
