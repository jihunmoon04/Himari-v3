"""Write locks on real paths (V3-P74 §9.1, review B1).

Two holders conflict when any of their paths is the same as, or an
ancestor of, one of the other's. A request gets all its paths at once
(no deadlock), when they overlap neither what is held nor any request that
came before it and still waits: overlapping requests go in arrival order,
others pass. Reads take no lock.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager


def _under(path: str, root: str) -> bool:
    root = root.rstrip("/") or "/"
    return path == root or root == "/" or path.startswith(root + "/")


def overlap(a: Sequence[str], b: Sequence[str]) -> bool:
    return any(_under(x, y) or _under(y, x) for x in a for y in b)


class PathLocks:
    """Waiting needs no lock of its own and releasing never awaits, so a
    cancelled holder always gives its paths back."""

    def __init__(self) -> None:
        self._held: list[tuple[str, ...]] = []
        self._waiting: list[tuple[object, tuple[str, ...]]] = []  # (ticket, paths) in arrival order
        self._changed = asyncio.Event()

    def _free(self, ticket: object, paths: tuple[str, ...]) -> bool:
        if any(overlap(paths, h) for h in self._held):
            return False
        for other, waiting in self._waiting:
            if other is ticket:
                return True
            if overlap(paths, waiting):
                return False
        return False

    def _notify(self) -> None:
        self._changed.set()
        self._changed = asyncio.Event()

    @asynccontextmanager
    async def hold(self, paths: Sequence[str], timeout: float | None = None) -> AsyncIterator[None]:
        """TimeoutError if not given within ``timeout`` seconds."""
        entry = tuple(paths)
        ticket = object()
        self._waiting.append((ticket, entry))
        try:
            async with asyncio.timeout(timeout):
                while not self._free(ticket, entry):
                    await self._changed.wait()
            self._held.append(entry)  # no await since the check: nothing else ran in between
        finally:
            self._waiting = [w for w in self._waiting if w[0] is not ticket]
            self._notify()
        try:
            yield
        finally:
            self._held.remove(entry)
            self._notify()
