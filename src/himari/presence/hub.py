"""Presence and the event stream (V3-P83 §5, §6.1, §8.1), in memory only.

One ``PresenceHub`` per server: it is both ``Presence`` and ``EventHub``,
because whether a device is connected depends on its open stream. The
current device is decided again on every signal, every stream opened or
closed, and every ``TICK_SECONDS`` (``run``): when it changes, every stream
of the principal gets ``current``.

Activity signals (V3-P88): each signal is the device's whole snapshot
(§2.1) and replaces what was kept. A locked device is never current; a
signal marked ``after`` (an unlock or a wake) makes its device current
past the switch delay (§3) as soon as it can be — at once, or when its
stream opens if the signal came first (PR #149 review B2) — and that is
kept as the principal's return, like a device that becomes current after
it was gone (the fallback). An activity that ends — another category, or
none in front — is kept as the one before for BEFORE_SECONDS (§4.4);
unknown or locked is not an end. A device gone (its last signal too old,
or its stream closed past the reopen gap) loses all its signals, the one
before too (review C1). Whatever the Web's Devices screen shows —
signals, connected, current — changes the principal's revision (review
B1), told to the Web streams only as ``devices`` with no content, at most
every NOTICE_SECONDS but never losing the last change (§8.1).
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from himari.common import JsonValue, PrincipalId
from himari.presence.api import (
    BEFORE_SECONDS,
    CANDIDATE_SECONDS,
    MAX_IDLE_SECONDS,
    MAX_STREAMS,
    NOTICE_SECONDS,
    QUEUE_EVENTS,
    REOPEN_SECONDS,
    SIGNAL_SECONDS,
    SWITCH_SECONDS,
    TICK_SECONDS,
    ActivitySignal,
    DeviceState,
    EndedActivity,
    MediaSignal,
    Returned,
    Session,
    Signals,
    StreamEvent,
    TooManyStreams,
)


class _Subscription:
    def __init__(self, principal: PrincipalId, device_id: str | None) -> None:
        self.principal = principal
        self._device_id = device_id
        self._events: deque[StreamEvent] = deque()
        self._wake = asyncio.Event()
        self.ended = False

    @property
    def device_id(self) -> str | None:
        return self._device_id

    @property
    def finished(self) -> bool:
        return self.ended and not self._events

    def push(self, event: StreamEvent) -> bool:
        """False when its queue is full (the hub then ends it)."""
        if self.ended:
            return True
        if len(self._events) >= QUEUE_EVENTS:
            return False
        self._events.append(event)
        self._wake.set()
        return True

    def end(self, last: StreamEvent | None = None, *, drop: bool = False) -> None:
        """``drop``: what still waits is not sent, only ``last`` (a revoked device, PR #124 review N1)."""
        if self.ended:
            return
        if drop:
            self._events.clear()
        if last is not None:
            self._events.append(last)  # the one event past the limit: why it ends
        self.ended = True
        self._wake.set()

    async def next(self, timeout: float) -> StreamEvent | None:
        if not self._events and not self.ended:
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout)
            except TimeoutError:
                pass
        return self._events.popleft() if self._events else None


@dataclass
class _Ended:
    category: str
    app: str | None
    lasted_seconds: float
    ended_at: float


@dataclass
class _Device:
    signal_at: float | None = None  # when its last signal came
    input_at: float | None = None  # its last input: the signal's time less its idle_seconds
    time_zone: str | None = None
    closed_at: float | None = None  # when its last stream closed (None: never opened, or open now)
    # V3-P88: the last snapshot (§2.1), cleared when the device is gone
    locked: bool = False
    activity: ActivitySignal | None = None
    activity_at: float | None = None  # when ``activity`` came (its for_seconds counts on from there)
    media: MediaSignal | None = None
    mic_or_camera: bool | None = None
    before: _Ended | None = None  # §4.4
    gone: bool = True  # its signals were cleared since it was last heard of (or it never was)
    back: bool = True  # gone since it was last current: becoming current is a return ("reconnect")
    after: tuple[str, float] | None = None  # an unlock or wake not yet taken (review B2): which, when
    seen: tuple[bool, bool] = (False, False)  # connected and current as the Web was last told


def _shown(device: _Device) -> tuple[object, ...]:
    """What the Web shows of a device's signals, but for the running time (counted when read)."""
    activity = None if device.activity is None else replace(device.activity, for_seconds=0.0)
    before = None if device.before is None else (device.before.category, device.before.app, device.before.ended_at)
    return (device.locked, activity, device.media, device.mic_or_camera, before)


class PresenceHub:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 time_zones: Callable[[str], bool] | None = None,
                 wall: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._clock = clock
        self._wall = wall
        self._time_zones = time_zones  # whether a name is a known zone; None: any name
        self._subscriptions: dict[PrincipalId, list[_Subscription]] = {}
        self._devices: dict[PrincipalId, dict[str, _Device]] = {}
        self._current: dict[PrincipalId, str] = {}
        self._epoch = secrets.token_hex(8)  # V3-P88 §8.1: new on every start
        self._revisions: dict[PrincipalId, int] = {}
        self._noticed: dict[PrincipalId, tuple[float, int]] = {}  # the last notice: when, which revision
        self._pending: set[PrincipalId] = set()  # a notice is due at the window's end
        self._returned: dict[PrincipalId, Returned] = {}

    # --- EventHub ------------------------------------------------------------------------

    def subscribe(self, principal: PrincipalId, device_id: str | None = None) -> _Subscription:
        streams = self._subscriptions.setdefault(principal, [])
        replaced = [s for s in streams if device_id is not None and s.device_id == device_id]
        if len(streams) - len(replaced) >= MAX_STREAMS:
            raise TooManyStreams
        for old in replaced:  # one stream per device (§6.1)
            old.end(StreamEvent("replaced", {}))
            streams.remove(old)
        subscription = _Subscription(principal, device_id)
        streams.append(subscription)
        if device_id is not None:
            self._device(principal, device_id).closed_at = None
            self._decide(principal)
        return subscription

    def unsubscribe(self, subscription: object) -> None:
        assert isinstance(subscription, _Subscription)
        subscription.end()
        streams = self._subscriptions.get(subscription.principal, [])
        if subscription not in streams:
            return  # replaced already, or ended by the hub
        streams.remove(subscription)
        if not streams:
            del self._subscriptions[subscription.principal]
        if subscription.device_id is not None and not self._open(subscription.principal, subscription.device_id):
            self._device(subscription.principal, subscription.device_id).closed_at = self._clock()
            self._decide(subscription.principal)

    def publish(self, principal: PrincipalId, name: str, data: JsonValue) -> None:
        self._push(principal, StreamEvent(name, data), web_only=False)

    def publish_web(self, principal: PrincipalId, name: str, data: JsonValue) -> None:
        self._push(principal, StreamEvent(name, data), web_only=True)

    def _push(self, principal: PrincipalId, event: StreamEvent, *, web_only: bool) -> None:
        for subscription in list(self._subscriptions.get(principal, ())):
            if web_only and subscription.device_id is not None:
                continue
            if not subscription.push(event):  # fallen behind: closed, it reopens and gets hello
                self.unsubscribe(subscription)

    def open_streams(self, principal: PrincipalId) -> int:
        return len(self._subscriptions.get(principal, ()))

    # --- Presence ------------------------------------------------------------------------

    def signal(self, principal: PrincipalId, device_id: str, idle_seconds: float, time_zone: str | None,
               signals: Signals | None = None) -> None:
        now = self._clock()
        snapshot = signals or Signals()
        device = self._device(principal, device_id)
        shown = _shown(device)
        device.signal_at = now
        device.input_at = now - min(max(idle_seconds, 0.0), MAX_IDLE_SECONDS)
        if time_zone is not None and (self._time_zones is None or self._time_zones(time_zone)):
            device.time_zone = time_zone  # an unknown name is ignored (§5)
        self._take(device, snapshot, now)
        device.gone = False
        if snapshot.after is not None and not device.locked:
            device.after = (snapshot.after.value, now)  # taken when the device can be current (review B2)
        elif device.locked:
            device.after = None
        if _shown(device) != shown:
            self._changed(principal)
        self._decide(principal)

    def _take(self, device: _Device, snapshot: Signals, now: float) -> None:
        """§2.1: the snapshot replaces what was kept; §4.4: an ended activity is kept as the one before."""
        device.locked = snapshot.session is Session.LOCKED
        if device.locked:  # nothing is in front of a locked screen; not an end either
            device.activity = device.activity_at = device.media = device.mic_or_camera = None
            return
        old, old_at = device.activity, device.activity_at
        new = snapshot.activity if snapshot.activity_known else None
        if old is not None and old_at is not None and snapshot.activity_known and (
                new is None or new.category != old.category):
            device.before = _Ended(old.category, old.app, old.for_seconds + (now - old_at), now)
        device.activity = new
        device.activity_at = now if new is not None else None
        device.media = snapshot.media if snapshot.media_known else None
        device.mic_or_camera = snapshot.mic_or_camera

    def states(self, principal: PrincipalId) -> dict[str, DeviceState]:
        now = self._clock()
        return {device_id: self._state(principal, device_id, d, now)
                for device_id, d in self._devices.get(principal, {}).items()}

    def _state(self, principal: PrincipalId, device_id: str, d: _Device, now: float) -> DeviceState:
        activity = d.activity
        if activity is not None and d.activity_at is not None:
            activity = replace(activity, for_seconds=activity.for_seconds + (now - d.activity_at))
        before = None
        if d.before is not None and now - d.before.ended_at <= BEFORE_SECONDS:
            before = EndedActivity(d.before.category, d.before.app, d.before.lasted_seconds, now - d.before.ended_at)
        return DeviceState(device_id, self._connected(principal, device_id, now),
                           None if d.input_at is None else now - d.input_at, d.time_zone,
                           locked=d.locked, activity=activity, media=d.media, mic_or_camera=d.mic_or_camera,
                           before=before)

    def current(self, principal: PrincipalId) -> str | None:
        return self._current.get(principal)

    @property
    def signals_epoch(self) -> str:
        return self._epoch

    def signals_revision(self, principal: PrincipalId) -> int:
        return self._revisions.get(principal, 0)

    def returned(self, principal: PrincipalId) -> Returned | None:
        return self._returned.get(principal)

    def revoked(self, principal: PrincipalId, device_id: str) -> None:
        for subscription in [s for s in self._subscriptions.get(principal, ()) if s.device_id == device_id]:
            subscription.end(StreamEvent("revoked", {}), drop=True)  # nothing more reaches it
            self._subscriptions[principal].remove(subscription)
        if not self._subscriptions.get(principal, True):
            del self._subscriptions[principal]
        if self._devices.get(principal, {}).pop(device_id, None) is not None:
            self._changed(principal)
        if (returned := self._returned.get(principal)) is not None and returned.device_id == device_id:
            del self._returned[principal]
        self._decide(principal)

    def tick(self) -> None:
        """Decide every principal's current device again (time passing makes the
        user away, and a device gone), and send a notice that waits."""
        for principal in list(self._devices):
            self._decide(principal)
        for principal in list(self._pending):
            self._notice(principal)

    async def run(self) -> None:
        """``tick`` every TICK_SECONDS until cancelled."""
        while True:
            await asyncio.sleep(TICK_SECONDS)
            self.tick()

    # --- inside ----------------------------------------------------------------------------

    def _device(self, principal: PrincipalId, device_id: str) -> _Device:
        return self._devices.setdefault(principal, {}).setdefault(device_id, _Device())

    def _open(self, principal: PrincipalId, device_id: str) -> bool:
        return any(s.device_id == device_id for s in self._subscriptions.get(principal, ()))

    def _connected(self, principal: PrincipalId, device_id: str, now: float) -> bool:
        """§5: its stream open (or closed under REOPEN_SECONDS ago) and a signal within SIGNAL_SECONDS."""
        device = self._devices.get(principal, {}).get(device_id)
        if device is None or device.signal_at is None or now - device.signal_at > SIGNAL_SECONDS:
            return False
        return self._open(principal, device_id) or (
            device.closed_at is not None and now - device.closed_at <= REOPEN_SECONDS)

    def _gone(self, principal: PrincipalId, device: _Device, now: float) -> bool:
        """V3-P88 §2.1: no fresh signal, or its stream closed past the reopen gap. A
        device whose signal came before its stream first opened is not gone (review B2)."""
        if device.signal_at is None or now - device.signal_at > SIGNAL_SECONDS:
            return True
        return device.closed_at is not None and now - device.closed_at > REOPEN_SECONDS

    def _decide(self, principal: PrincipalId) -> None:
        """§6.1: among connected, unlocked devices with input within
        CANDIDATE_SECONDS, the latest input — but the current one stays until
        it had none for SWITCH_SECONDS, unless a candidate has an unlock or a
        wake not yet taken (V3-P88 §3): it takes over, and that is the return.
        None of them: away. A change is published. A device gone loses its
        signals (§2.1); one that was gone and becomes current has come back."""
        now = self._clock()
        devices = self._devices.get(principal, {})
        for device in devices.values():
            if not device.gone and self._gone(principal, device, now):
                self._forget_signals(principal, device)
            if device.after is not None and now - device.after[1] > SIGNAL_SECONDS:
                device.after = None  # never became current while it was fresh
            if device.before is not None and now - device.before.ended_at > BEFORE_SECONDS:
                device.before = None
        connected = {device_id for device_id in devices if self._connected(principal, device_id, now)}
        inputs = {device_id: d.input_at for device_id, d in devices.items()
                  if d.input_at is not None and now - d.input_at <= CANDIDATE_SECONDS and not d.locked
                  and device_id in connected}
        was = self._current.get(principal)
        chosen = max(inputs, key=lambda device_id: (inputs[device_id], device_id), default=None)
        marked = [device_id for device_id in inputs if devices[device_id].after is not None]
        if marked:
            chosen = max(marked, key=lambda device_id: devices[device_id].after[1])  # type: ignore[index]
        elif was in inputs and chosen != was and now - inputs[was] <= SWITCH_SECONDS:
            chosen = was  # no flapping between two computers used by turns
        if chosen is not None:
            device = devices[chosen]
            if device.after is not None:
                self._returned[principal] = Returned(chosen, self._wall(), device.after[0])
                device.after = None
            elif device.back and chosen != was:
                self._returned[principal] = Returned(chosen, self._wall(), "reconnect")
            device.back = False
        for device_id in connected - {chosen}:
            devices[device_id].back = False  # heard again but not where the user is: not a return later
        if chosen is None:
            self._current.pop(principal, None)
        else:
            self._current[principal] = chosen
        if chosen != was:
            self.publish(principal, "current", {"device_id": chosen})
        seen_changed = False
        for device_id, device in devices.items():
            seen = (device_id in connected, device_id == chosen)
            if seen != device.seen:
                device.seen, seen_changed = seen, True
        if seen_changed:
            self._changed(principal)  # review B1: connected and current are on the Devices screen too
        if not devices:
            self._devices.pop(principal, None)

    def _forget_signals(self, principal: PrincipalId, device: _Device) -> None:
        """Gone (§2.1): every signal is cleared, the activity before too (review C1); not an end (§4.4)."""
        shown = _shown(device)
        device.gone = device.back = True
        device.locked = False
        device.activity = device.activity_at = device.media = device.mic_or_camera = None
        device.before = device.after = None
        if _shown(device) != shown:
            self._changed(principal)

    def _changed(self, principal: PrincipalId) -> None:
        self._revisions[principal] = self._revisions.get(principal, 0) + 1
        timed = principal in self._pending  # its notice waits already, with a timer
        self._pending.add(principal)
        last = self._noticed.get(principal)
        wait = 0.0 if last is None else NOTICE_SECONDS - (self._clock() - last[0])
        if wait <= 0:
            self._notice(principal)
            return
        if timed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop (a test): the next tick sends it
        loop.call_later(wait, self._notice, principal)

    def _notice(self, principal: PrincipalId) -> None:
        """§8.1 (review N3): the latest revision, once the window allows; a
        later call (the timer or a tick) sends what is still due."""
        if principal not in self._pending:
            return
        revision = self._revisions.get(principal, 0)
        last = self._noticed.get(principal)
        now = self._clock()
        if last is not None and now - last[0] < NOTICE_SECONDS:
            return  # its timer, or a tick, comes back
        self._pending.discard(principal)
        if last is not None and last[1] == revision:
            return
        self._noticed[principal] = (now, revision)
        self.publish_web(principal, "devices", {"epoch": self._epoch, "revision": revision})
