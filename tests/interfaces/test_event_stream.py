"""V3-P83 §8.1, appendix A.3: the event stream's frames, and how it ends."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from himari.common import PrincipalId
from himari.control.api import JobStatus
from himari.interfaces.web import event_stream, live
from himari.presence.api import QUEUE_EVENTS
from himari.presence.hub import PresenceHub

ME = PrincipalId("operator")
HELLO = {"device": {"device_id": "d1", "name": "데스크톱"}, "current_device": None}


def parsed(frames: list[str]) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    for frame in frames:
        if frame.startswith("event: "):
            head, data = frame.strip().split("\n")
            out.append((head[len("event: "):], json.loads(data[len("data: "):])))
        else:
            out.append((frame.strip(), None))
    return out


def run(device: str | None, act: Callable[[PresenceHub, asyncio.Event], object], **options: Any
        ) -> list[tuple[str, Any]]:
    """The frames of one stream; ``act`` runs once it is open."""
    async def scenario() -> list[str]:
        hub, closing = PresenceHub(), asyncio.Event()
        subscription = hub.subscribe(ME, device)
        asyncio.get_running_loop().call_later(0.01, act, hub, closing)
        try:
            return [f async for f in event_stream.stream(subscription, HELLO, closing, **options)]
        finally:
            hub.unsubscribe(subscription)

    return parsed(asyncio.run(scenario()))


def test_hello_first_then_what_is_published_until_revoked() -> None:
    def act(hub: PresenceHub, closing: asyncio.Event) -> None:
        hub.publish(ME, "job", {"conversation": "c1", "state": "started", "origin": None})
        hub.publish(ME, "conversation", {"conversation": "c1"})
        asyncio.get_running_loop().call_later(0.05, hub.revoked, ME, "d1")  # once those were sent

    frames = run("d1", act, limit=5)
    assert frames[0] == ("retry: 1000", None) and frames[1] == ("hello", HELLO)
    assert [name for name, _ in frames[2:]] == ["job", "conversation", "revoked"]  # and it ends


def test_a_newer_stream_of_the_device_ends_this_one() -> None:
    frames = run("d1", lambda hub, closing: hub.subscribe(ME, "d1"), limit=5)
    assert frames[-1] == ("replaced", {})


def test_quiet_streams_keep_alive_and_end_at_their_limit() -> None:
    frames = run(None, lambda hub, closing: None, heartbeat=0.02, limit=0.1)
    assert [f for f, _ in frames[2:]] and all(f == ": keep-alive" for f, _ in frames[2:])


def test_shutting_down_ends_it() -> None:
    frames = run(None, lambda hub, closing: closing.set(), limit=5)
    assert [f for f, _ in frames] == ["retry: 1000", "hello"]


def test_a_stream_that_fell_behind_ends() -> None:
    def flood(hub: PresenceHub, closing: asyncio.Event) -> None:
        for n in range(QUEUE_EVENTS + 1):
            hub.publish(ME, "conversation", {"conversation": f"c{n}"})

    frames = run(None, flood, limit=5)
    assert len(frames) == 2 + QUEUE_EVENTS  # what was queued, then the end: it reopens and gets hello


def test_a_devices_live_stream_ends_when_revoked_and_wakes_as_a_client() -> None:
    """§8.1: revoking ends its live streams; §8.3: each wakeup is a connected client (seen)."""
    class Running:
        def live_output(self, scope: object, *, text: bool = True) -> None:
            return None

        def live_text(self, scope: object, run_id: object, start: int) -> None:
            return None

        def live_version(self, scope: object) -> int:
            return 0

        async def wait_live(self, scope: object, after_version: int, timeout: float) -> int:
            await asyncio.sleep(timeout)
            return 0

        def status(self, scope: object) -> JobStatus:
            return JobStatus(running=True)

    async def scenario() -> tuple[list[str], int]:
        woke = 0

        def awake() -> None:
            nonlocal woke
            woke += 1

        ended = asyncio.Event()
        asyncio.get_running_loop().call_later(0.1, ended.set)
        frames = [f async for f in live.stream(Running(), object(), asyncio.Event(),  # type: ignore[arg-type]
                                               heartbeat=0.02, limit=5, ended=ended, awake=awake)]
        return frames, woke

    frames, woke = asyncio.run(scenario())
    assert "event: idle\ndata: {}\n\n" in frames and "done" not in "".join(frames)  # ended, not finished
    assert woke >= 3
