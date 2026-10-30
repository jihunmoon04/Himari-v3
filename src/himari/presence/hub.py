"""Presence and the event stream (V3-P83 §5, §6.1, §8.1), in memory only.

One ``PresenceHub`` per server: it is both ``Presence`` and ``EventHub``,
because whether a device is connected depends on its open stream. The
current device is decided again on every signal, every stream opened or
closed, and every ``TICK_SECONDS`` (``run``): when it changes, every stream
of the principal gets ``current``.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from himari.common import JsonValue, PrincipalId
from himari.presence.api import (
    CANDIDATE_SECONDS,
    MAX_IDLE_SECONDS,
    MAX_STREAMS,
    QUEUE_EVENTS,
    REOPEN_SECONDS,
    SIGNAL_SECONDS,
    SWITCH_SECONDS,
    TICK_SECONDS,
    DeviceState,
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

    def end(self, last: StreamEvent | None = None) -> None:
        if self.ended:
            return
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
class _Device:
    signal_at: float | None = None  # when its last signal came
    input_at: float | None = None  # its last input: the signal's time less its idle_seconds
    time_zone: str | None = None
    closed_at: float | None = None  # when its last stream closed (None: never opened, or open now)


class PresenceHub:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 time_zones: Callable[[str], bool] | None = None) -> None:
        self._clock = clock
        self._time_zones = time_zones  # whether a name is a known zone; None: any name
        self._subscriptions: dict[PrincipalId, list[_Subscription]] = {}
        self._devices: dict[PrincipalId, dict[str, _Device]] = {}
        self._current: dict[PrincipalId, str] = {}

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
        event = StreamEvent(name, data)
        for subscription in list(self._subscriptions.get(principal, ())):
            if not subscription.push(event):  # fallen behind: closed, it reopens and gets hello
                self.unsubscribe(subscription)

    def open_streams(self, principal: PrincipalId) -> int:
        return len(self._subscriptions.get(principal, ()))

    # --- Presence ------------------------------------------------------------------------

    def signal(self, principal: PrincipalId, device_id: str, idle_seconds: float, time_zone: str | None) -> None:
        now = self._clock()
        device = self._device(principal, device_id)
        device.signal_at = now
        device.input_at = now - min(max(idle_seconds, 0.0), MAX_IDLE_SECONDS)
        if time_zone is not None and (self._time_zones is None or self._time_zones(time_zone)):
            device.time_zone = time_zone  # an unknown name is ignored (§5)
        self._decide(principal)

    def states(self, principal: PrincipalId) -> dict[str, DeviceState]:
        now = self._clock()
        return {device_id: DeviceState(device_id, self._connected(principal, device_id, now),
                                       None if d.input_at is None else now - d.input_at, d.time_zone)
                for device_id, d in self._devices.get(principal, {}).items()}

    def current(self, principal: PrincipalId) -> str | None:
        return self._current.get(principal)

    def revoked(self, principal: PrincipalId, device_id: str) -> None:
        for subscription in [s for s in self._subscriptions.get(principal, ()) if s.device_id == device_id]:
            subscription.end(StreamEvent("revoked", {}))
            self._subscriptions[principal].remove(subscription)
        if not self._subscriptions.get(principal, True):
            del self._subscriptions[principal]
        self._devices.get(principal, {}).pop(device_id, None)
        self._decide(principal)

    def tick(self) -> None:
        """Decide every principal's current device again (time passing makes the user away)."""
        for principal in list(self._devices):
            self._decide(principal)

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

    def _decide(self, principal: PrincipalId) -> None:
        """§6.1: among connected devices with input within CANDIDATE_SECONDS,
        the latest input — but the current one stays until it had none for
        SWITCH_SECONDS. None of them: away. A change is published."""
        now = self._clock()
        devices = self._devices.get(principal, {})
        inputs = {device_id: d.input_at for device_id, d in devices.items()
                  if d.input_at is not None and now - d.input_at <= CANDIDATE_SECONDS
                  and self._connected(principal, device_id, now)}
        was = self._current.get(principal)
        chosen = max(inputs, key=lambda device_id: (inputs[device_id], device_id), default=None)
        if was in inputs and chosen != was and now - inputs[was] <= SWITCH_SECONDS:
            chosen = was  # no flapping between two computers used by turns
        if chosen is None:
            self._current.pop(principal, None)
        else:
            self._current[principal] = chosen
        if chosen != was:
            self.publish(principal, "current", {"device_id": chosen})
        if not devices:
            self._devices.pop(principal, None)
