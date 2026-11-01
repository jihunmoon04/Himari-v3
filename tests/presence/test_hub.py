"""V3-P83 §5, §6.1, §8.1: who is connected, the current device, and the event stream."""

from __future__ import annotations

import asyncio

import pytest

from himari.common import PrincipalId
from himari.presence.api import MAX_STREAMS, QUEUE_EVENTS, StreamEvent, TooManyStreams
from himari.presence.hub import PresenceHub

ME, OTHER = PrincipalId("operator"), PrincipalId("someone")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def drained(subscription: object) -> list[StreamEvent]:
    async def take() -> list[StreamEvent]:
        out = []
        while (event := await subscription.next(0)) is not None:  # type: ignore[attr-defined]
            out.append(event)
        return out

    return asyncio.run(take())


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def hub(clock: Clock) -> PresenceHub:
    return PresenceHub(clock=clock, time_zones=lambda name: name in {"Asia/Seoul", "Europe/London"})


def test_connected_needs_an_open_stream_and_a_recent_signal(hub: PresenceHub, clock: Clock) -> None:
    hub.signal(ME, "d1", 0, None)
    assert not hub.states(ME)["d1"].connected and hub.current(ME) is None  # no stream: not connected
    stream = hub.subscribe(ME, "d1")
    assert hub.states(ME)["d1"].connected and hub.current(ME) == "d1"
    clock.now += 121  # no signal for over 120 s: a half-open connection counts as gone
    hub.tick()
    assert not hub.states(ME)["d1"].connected and hub.current(ME) is None
    hub.signal(ME, "d1", 0, None)
    hub.unsubscribe(stream)
    clock.now += 30  # a reopen's gap
    assert hub.states(ME)["d1"].connected
    clock.now += 1
    assert not hub.states(ME)["d1"].connected


def test_the_latest_input_is_current_but_not_flapping(hub: PresenceHub, clock: Clock) -> None:
    web = hub.subscribe(ME)
    hub.subscribe(ME, "desk"), hub.subscribe(ME, "laptop")
    hub.signal(ME, "desk", 0, None)
    hub.signal(ME, "laptop", 5, None)  # its last input was 5 s ago: older
    assert hub.current(ME) == "desk"
    clock.now += 30
    hub.signal(ME, "laptop", 0, None)  # newer, but the desk had input only 30 s ago
    assert hub.current(ME) == "desk"
    clock.now += 31
    hub.tick()  # the desk had none for 61 s: the laptop takes over
    assert hub.current(ME) == "laptop"
    assert [e for e in drained(web) if e.name == "current"] == [
        StreamEvent("current", {"device_id": "desk"}), StreamEvent("current", {"device_id": "laptop"})]


def test_ten_minutes_without_input_is_away(hub: PresenceHub, clock: Clock) -> None:
    web = hub.subscribe(ME)
    hub.subscribe(ME, "d1")
    hub.signal(ME, "d1", 590, None)
    assert hub.current(ME) == "d1"
    clock.now += 11
    hub.signal(ME, "d1", 601, None)  # the signal comes, the input does not
    assert hub.current(ME) is None
    assert [e for e in drained(web) if e.name != "devices"][-1] == StreamEvent("current", {"device_id": None})


def test_idle_seconds_are_cut_and_unknown_zones_ignored(hub: PresenceHub) -> None:
    hub.signal(ME, "d1", 10**9, "Asia/Seoul")
    hub.signal(ME, "d1", -5, "Mars/Olympus")
    state = hub.states(ME)["d1"]
    assert state.idle_seconds == 0 and state.time_zone == "Asia/Seoul"


def test_one_stream_per_device(hub: PresenceHub) -> None:
    first = hub.subscribe(ME, "d1")
    second = hub.subscribe(ME, "d1")
    assert drained(first) == [StreamEvent("replaced", {})] and first.finished
    assert not second.finished and hub.open_streams(ME) == 1
    hub.unsubscribe(first)  # its reader ends later: the new one stays
    assert hub.open_streams(ME) == 1


def test_a_principal_has_at_most_sixteen_streams(hub: PresenceHub) -> None:
    streams = [hub.subscribe(ME) for _ in range(MAX_STREAMS)]
    with pytest.raises(TooManyStreams):
        hub.subscribe(ME)
    hub.subscribe(OTHER)  # another principal's are their own
    hub.unsubscribe(streams[0])
    hub.subscribe(ME, "d1")


def test_a_device_replacing_its_own_stream_is_not_refused_when_full(hub: PresenceHub) -> None:
    for _ in range(MAX_STREAMS - 1):
        hub.subscribe(ME)
    hub.subscribe(ME, "d1")
    hub.subscribe(ME, "d1")  # it takes its own place


def test_a_stream_that_falls_behind_is_closed(hub: PresenceHub) -> None:
    slow, quick = hub.subscribe(ME), hub.subscribe(ME)
    for n in range(QUEUE_EVENTS):
        hub.publish(ME, "conversation", {"conversation": f"c{n}"})
    assert len(drained(quick)) == QUEUE_EVENTS
    hub.publish(ME, "conversation", {"conversation": "one more"})
    assert len(drained(slow)) == QUEUE_EVENTS and slow.finished  # it reopens and starts from hello
    assert not quick.finished and hub.open_streams(ME) == 1


def test_publishing_goes_to_the_principals_streams_only(hub: PresenceHub) -> None:
    mine, theirs = hub.subscribe(ME), hub.subscribe(OTHER)
    hub.publish(ME, "job", {"conversation": "c1", "state": "started"})
    assert [e.name for e in drained(mine)] == ["job"] and drained(theirs) == []


def test_revoking_ends_the_devices_streams_and_forgets_it(hub: PresenceHub) -> None:
    web, device = hub.subscribe(ME), hub.subscribe(ME, "d1")
    hub.signal(ME, "d1", 0, None)
    hub.revoked(ME, "d1")
    assert drained(device)[-1] == StreamEvent("revoked", {}) and device.finished
    assert hub.current(ME) is None and "d1" not in hub.states(ME)
    assert drained(web)[-1] == StreamEvent("current", {"device_id": None})


def test_next_waits_for_an_event() -> None:
    async def scenario() -> None:
        hub = PresenceHub()
        stream = hub.subscribe(ME)
        assert await stream.next(0.01) is None
        asyncio.get_running_loop().call_later(0.01, hub.publish, ME, "activity", {})
        assert await stream.next(1) == StreamEvent("activity", {})

    asyncio.run(scenario())


def test_a_revoked_device_gets_nothing_that_was_still_waiting(hub: PresenceHub) -> None:
    """PR #124 review N1: revoking ends access at once — queued events are dropped, only "revoked" goes."""
    device = hub.subscribe(ME, "d1")
    hub.publish(ME, "conversation", {"conversation": "c1"})
    hub.publish(ME, "job", {"conversation": "c1", "state": "started", "origin": None})
    hub.revoked(ME, "d1")
    assert drained(device) == [StreamEvent("revoked", {})] and device.finished


def test_a_replaced_stream_still_gets_what_was_waiting(hub: PresenceHub) -> None:
    first = hub.subscribe(ME, "d1")
    hub.publish(ME, "conversation", {"conversation": "c1"})
    hub.subscribe(ME, "d1")
    assert [e.name for e in drained(first)] == ["conversation", "replaced"]
