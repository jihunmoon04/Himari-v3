"""V3-P88: activity signals in the presence hub — the whole-snapshot rule,
locks and unlocks, the return record, the activity before, and the Web's
``devices`` notices."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from himari.common import PrincipalId
from himari.presence.api import (
    BEFORE_SECONDS,
    NOTICE_SECONDS,
    ActivitySignal,
    After,
    MediaSignal,
    Session,
    Signals,
    StreamEvent,
)
from himari.presence.hub import PresenceHub

ME = PrincipalId("operator")
WALL = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
CODING = ActivitySignal("coding", "Visual Studio Code", 2700.0, False)
GAME = ActivitySignal("game", "Some Game", 600.0, True)
SONG = MediaSignal("밤양갱", "비비", "Spotify")


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


def full(activity: ActivitySignal | None = CODING, media: MediaSignal | None = SONG, mic: bool | None = False,
         session: Session = Session.ACTIVE, after: After | None = None) -> Signals:
    return Signals(session=session, activity_known=True, activity=activity, media_known=True, media=media,
                   mic_or_camera=mic, after=after)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def hub(clock: Clock) -> PresenceHub:
    return PresenceHub(clock=clock, wall=lambda: WALL)


def test_a_signal_replaces_the_whole_snapshot(hub: PresenceHub, clock: Clock) -> None:
    hub.subscribe(ME, "d1")
    hub.signal(ME, "d1", 0, None, full())
    state = hub.states(ME)["d1"]
    assert state.activity == CODING and state.media == SONG and state.mic_or_camera is False
    clock.now += 60
    assert hub.states(ME)["d1"].activity.for_seconds == 2760  # type: ignore[union-attr]  # counted to now
    # the user turned media off in the app: the field is absent, and the old song goes
    hub.signal(ME, "d1", 0, None, Signals(session=Session.ACTIVE, activity_known=True, activity=CODING,
                                          mic_or_camera=False))
    state = hub.states(ME)["d1"]
    assert state.media is None and state.activity is not None
    hub.signal(ME, "d1", 0, None, full(media=None))  # null: seen, nothing plays
    assert hub.states(ME)["d1"].media is None
    hub.signal(ME, "d1", 0, None)  # an old app: everything unknown
    state = hub.states(ME)["d1"]
    assert (state.activity, state.media, state.mic_or_camera, state.locked) == (None, None, None, False)


def test_a_locked_device_is_away_at_once_and_its_activity_is_dropped(hub: PresenceHub) -> None:
    stream = hub.subscribe(ME, "d1")
    hub.signal(ME, "d1", 0, None, full())
    assert hub.current(ME) == "d1"
    drained(stream)
    hub.signal(ME, "d1", 0, None, full(session=Session.LOCKED))  # what came with the lock is ignored
    state = hub.states(ME)["d1"]
    assert state.locked and state.activity is None and state.media is None and state.mic_or_camera is None
    assert hub.current(ME) is None  # no ten minutes' wait
    assert StreamEvent("current", {"device_id": None}) in drained(stream)
    assert hub.states(ME)["d1"].before is None  # a lock is not the end of the activity


def test_an_unlock_takes_over_at_once_and_is_the_return(hub: PresenceHub, clock: Clock) -> None:
    hub.subscribe(ME, "a")
    hub.subscribe(ME, "b")
    hub.signal(ME, "a", 0, None, full())
    hub.signal(ME, "a", 0, None, full(session=Session.LOCKED))
    hub.signal(ME, "b", 0, None, full())
    assert hub.current(ME) == "b"
    clock.now += 5
    hub.signal(ME, "b", 0, None, full())  # b had input 5 s ago: the switch delay would keep it
    hub.signal(ME, "a", 0, None, full(after=After.UNLOCK))
    assert hub.current(ME) == "a"
    assert hub.returned(ME) is not None and (hub.returned(ME).device_id, hub.returned(ME).via) == ("a", "unlock")  # type: ignore[union-attr]
    clock.now += 5
    hub.signal(ME, "b", 0, None, full())  # afterwards the usual rule again: a had input 5 s ago
    assert hub.current(ME) == "a"


def test_a_long_sleep_is_seen_by_its_after_mark(hub: PresenceHub, clock: Clock) -> None:
    hub.subscribe(ME, "d1")
    hub.signal(ME, "d1", 0, None, full(session=Session.LOCKED))
    clock.now += 4 * 3600  # asleep: no signal, the server forgot the lock
    hub.tick()
    assert not hub.states(ME)["d1"].connected and not hub.states(ME)["d1"].locked
    hub.subscribe(ME, "d1")
    hub.signal(ME, "d1", 0, None, full(after=After.RESUME))
    assert hub.current(ME) == "d1"
    assert hub.returned(ME).via == "resume"  # type: ignore[union-attr]


def test_a_device_back_without_its_mark_is_a_reconnect(hub: PresenceHub, clock: Clock) -> None:
    hub.subscribe(ME, "d1")
    hub.signal(ME, "d1", 0, None, full())
    assert hub.returned(ME).via == "reconnect"  # type: ignore[union-attr]  # first heard of since the start
    clock.now += 600
    hub.tick()
    assert hub.current(ME) is None  # gone
    hub.subscribe(ME, "d1")
    clock.now += 1
    hub.signal(ME, "d1", 0, None, full())
    assert hub.returned(ME).via == "reconnect"  # type: ignore[union-attr]


def test_staying_current_is_no_return(hub: PresenceHub, clock: Clock) -> None:
    hub.subscribe(ME, "d1")
    hub.signal(ME, "d1", 0, None, full())
    first = hub.returned(ME)
    clock.now += 60
    hub.signal(ME, "d1", 0, None, full())
    assert hub.returned(ME) is first


def test_a_device_gone_loses_its_signals(hub: PresenceHub, clock: Clock) -> None:
    hub.subscribe(ME, "d1")
    hub.signal(ME, "d1", 0, None, full())
    clock.now += 121
    hub.tick()
    state = hub.states(ME)["d1"]
    assert (state.activity, state.media, state.mic_or_camera) == (None, None, None)
    assert state.before is None  # gone is not the end of the activity


def test_an_activity_that_ends_is_kept_as_the_one_before(hub: PresenceHub, clock: Clock) -> None:
    hub.subscribe(ME, "d1")
    hub.signal(ME, "d1", 0, None, full(activity=GAME))
    clock.now += 60
    hub.signal(ME, "d1", 0, None, full(activity=ActivitySignal("game", "Some Game", 660.0, True)))  # same: no end
    assert hub.states(ME)["d1"].before is None
    clock.now += 60
    hub.signal(ME, "d1", 0, None, full(activity=None))  # the game closed, nothing in front (review N4)
    before = hub.states(ME)["d1"].before
    assert before is not None and before.category == "game" and before.lasted_seconds == 720
    clock.now += BEFORE_SECONDS + 1
    hub.signal(ME, "d1", 0, None, full(activity=CODING))
    assert hub.states(ME)["d1"].before is None  # forgotten after half an hour


def test_an_unknown_activity_is_not_an_end(hub: PresenceHub) -> None:
    hub.subscribe(ME, "d1")
    hub.signal(ME, "d1", 0, None, full(activity=GAME))
    hub.signal(ME, "d1", 0, None, Signals(session=Session.ACTIVE))  # the app stopped collecting it
    state = hub.states(ME)["d1"]
    assert state.activity is None and state.before is None


def test_devices_notices_go_to_the_web_only_with_no_content(hub: PresenceHub, clock: Clock) -> None:
    web, device = hub.subscribe(ME), hub.subscribe(ME, "d1")
    hub.signal(ME, "d1", 0, None, full())
    clock.now += NOTICE_SECONDS
    hub.tick()  # the signal and the device becoming current: one notice now, the latest at the window's end
    notices = [e for e in drained(web) if e.name == "devices"]
    assert notices[-1] == StreamEvent("devices", {"epoch": hub.signals_epoch, "revision": hub.signals_revision(ME)})
    assert all(set(e.data) == {"epoch", "revision"} for e in notices)  # type: ignore[arg-type]
    assert not [e for e in drained(device) if e.name == "devices"]


def test_notices_are_coalesced_but_the_last_change_is_told(hub: PresenceHub, clock: Clock) -> None:
    web = hub.subscribe(ME)
    hub.subscribe(ME, "d1")
    hub.signal(ME, "d1", 0, None, full())
    drained(web)
    clock.now += 1
    hub.signal(ME, "d1", 0, None, full(media=None))
    clock.now += 1
    hub.signal(ME, "d1", 0, None, full(activity=GAME))
    assert not [e for e in drained(web) if e.name == "devices"]  # within the window
    clock.now += NOTICE_SECONDS
    hub.tick()  # the window's end (a timer in a running loop)
    notices = [e for e in drained(web) if e.name == "devices"]
    assert notices == [StreamEvent("devices", {"epoch": hub.signals_epoch, "revision": hub.signals_revision(ME)})]
    clock.now += NOTICE_SECONDS
    hub.tick()
    assert not [e for e in drained(web) if e.name == "devices"]  # nothing new: nothing sent


def test_the_running_time_alone_is_no_change(hub: PresenceHub, clock: Clock) -> None:
    hub.subscribe(ME, "d1")
    hub.signal(ME, "d1", 0, None, full())
    revision = hub.signals_revision(ME)
    clock.now += 60
    hub.signal(ME, "d1", 0, None, full(activity=ActivitySignal("coding", "Visual Studio Code", 2760.0, False)))
    assert hub.signals_revision(ME) == revision


def test_each_start_has_its_own_epoch(clock: Clock) -> None:
    assert PresenceHub(clock=clock).signals_epoch != PresenceHub(clock=clock).signals_epoch


def test_a_revoked_device_takes_its_return_with_it(hub: PresenceHub) -> None:
    hub.subscribe(ME, "d1")
    hub.signal(ME, "d1", 0, None, full())
    hub.revoked(ME, "d1")
    assert hub.returned(ME) is None and "d1" not in hub.states(ME)


def test_connected_and_current_changes_reach_the_web_without_signals(hub: PresenceHub, clock: Clock) -> None:
    """PR #149 review B1: a device with no signals that goes away still changes what the Devices screen shows."""
    web = hub.subscribe(ME)
    hub.subscribe(ME, "d1")
    hub.signal(ME, "d1", 0, None)  # an old app: no signals at all
    before = hub.signals_revision(ME)
    clock.now += 121
    hub.tick()
    assert not hub.states(ME)["d1"].connected and hub.signals_revision(ME) > before
    clock.now += NOTICE_SECONDS
    hub.tick()
    assert [e for e in drained(web) if e.name == "devices"][-1].data == {
        "epoch": hub.signals_epoch, "revision": hub.signals_revision(ME)}


def test_an_unlock_signalled_before_the_stream_opens_is_still_the_return(hub: PresenceHub, clock: Clock) -> None:
    """PR #149 review B2: the app's presence and event loops start apart; the mark waits for the stream."""
    hub.subscribe(ME, "b")
    hub.signal(ME, "b", 0, None, full())
    hub.signal(ME, "a", 0, None, full(after=After.RESUME))  # no stream yet: not connected
    assert hub.current(ME) == "b" and hub.states(ME)["a"].activity == CODING  # kept, not wiped
    clock.now += 5
    hub.signal(ME, "b", 0, None, full())
    hub.subscribe(ME, "a")  # the stream opens: a takes over past the switch delay
    assert hub.current(ME) == "a"
    assert hub.returned(ME) is not None and (hub.returned(ME).device_id, hub.returned(ME).via) == ("a", "resume")  # type: ignore[union-attr]


def test_a_mark_that_never_became_current_expires(hub: PresenceHub, clock: Clock) -> None:
    hub.subscribe(ME, "b")
    hub.signal(ME, "b", 0, None, full())
    hub.signal(ME, "a", 0, None, full(after=After.UNLOCK))
    clock.now += 125
    hub.signal(ME, "b", 0, None, full())
    hub.signal(ME, "a", 0, None, full())
    hub.subscribe(ME, "a")
    assert hub.current(ME) == "b"  # the old mark is not taken any more


def test_gone_clears_the_activity_before_too(hub: PresenceHub, clock: Clock) -> None:
    """PR #149 review C1."""
    hub.subscribe(ME, "d1")
    hub.signal(ME, "d1", 0, None, full(activity=GAME))
    clock.now += 60
    hub.signal(ME, "d1", 0, None, full(activity=None))
    assert hub.states(ME)["d1"].before is not None
    clock.now += 121
    hub.tick()
    hub.subscribe(ME, "d1")
    hub.signal(ME, "d1", 0, None, full(activity=CODING))
    assert hub.states(ME)["d1"].before is None


def test_the_activity_before_expiring_is_told_to_the_web(hub: PresenceHub, clock: Clock) -> None:
    """PR #149 re-review P2: GET /api/devices stops showing it, so the open screen must hear."""
    hub.subscribe(ME, "d1")
    hub.signal(ME, "d1", 0, None, full(activity=GAME))
    clock.now += 60
    hub.signal(ME, "d1", 0, None, full(activity=None))
    shown = hub.signals_revision(ME)
    for _ in range(int(BEFORE_SECONDS // 50) + 1):  # the app keeps signalling; ticks pass
        clock.now += 50
        hub.signal(ME, "d1", 0, None, full(activity=None))
        hub.tick()
    assert hub.states(ME)["d1"].before is None and hub.signals_revision(ME) > shown


def test_a_return_is_told_before_the_input_that_came_with_it(hub: PresenceHub, clock: Clock) -> None:
    """V3-P87 §5.2, §7: the scheduler measures the time away up to the input before the return."""
    told: list[tuple[object, ...]] = []
    hub.on_returned = lambda principal, returned: told.append(("returned", returned.device_id, returned.via))
    hub.on_input = lambda principal, device_id, at, locked: told.append(("input", device_id, at, locked))
    hub.subscribe(ME, "d1")
    hub.signal(ME, "d1", 30, None, full(session=Session.LOCKED))
    assert told == [("input", "d1", WALL - timedelta(seconds=30), True)]
    clock.now += 3600
    told.clear()
    hub.signal(ME, "d1", 0, None, full(after=After.UNLOCK))
    assert told == [("returned", "d1", "unlock"), ("input", "d1", WALL, False)]
    assert hub.present() == (ME,)
