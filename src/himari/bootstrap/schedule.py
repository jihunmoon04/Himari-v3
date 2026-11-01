"""The server's periodic work (V3-P87 §7): named jobs, each on its own period.

``purge`` (the trash, files, memory cleanup: the old cleanup loop, same
period) and ``proactive`` (every 30 seconds, and soon after a return) run
here. A job that fails is logged; the others and its next round still run.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class _Job:
    name: str
    seconds: float
    run: Callable[[], Awaitable[None]]
    poked: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None


class Ticker:
    def __init__(self) -> None:
        self._jobs: dict[str, _Job] = {}
        self._timers: set[asyncio.TimerHandle] = set()

    def add(self, name: str, seconds: float, run: Callable[[], Awaitable[None]]) -> None:
        """A job run at once, then every ``seconds`` (or sooner when poked)."""
        assert name not in self._jobs, name
        self._jobs[name] = _Job(name, seconds, run)

    def start(self) -> None:
        for job in self._jobs.values():
            if job.task is None:
                job.task = asyncio.create_task(self._loop(job), name=f"ticker-{job.name}")

    def poke(self, name: str, after: float = 0.0) -> None:
        """Run ``name`` once more ``after`` seconds from now (its period starts again from there)."""
        job = self._jobs.get(name)
        if job is None:
            return
        if after <= 0:
            job.poked.set()
            return
        timer = asyncio.get_running_loop().call_later(after, self._poked, job)
        self._timers.add(timer)

    def _poked(self, job: _Job) -> None:
        self._timers = {t for t in self._timers if not t.cancelled() and t.when() > asyncio.get_running_loop().time()}
        job.poked.set()

    async def _loop(self, job: _Job) -> None:
        while True:
            job.poked.clear()
            try:
                await job.run()
            except Exception:  # noqa: BLE001 — the next round tries again; the other jobs go on
                log.exception("%s failed", job.name)
            try:
                await asyncio.wait_for(job.poked.wait(), job.seconds)
            except TimeoutError:
                pass

    async def close(self) -> None:
        for timer in self._timers:
            timer.cancel()
        self._timers.clear()
        tasks = [job.task for job in self._jobs.values() if job.task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for job in self._jobs.values():
            job.task = None
