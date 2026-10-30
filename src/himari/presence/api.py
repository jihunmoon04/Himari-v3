"""Presence public contract (V3-P83 §2.2): a principal's devices and how they pair.

A device is the human side of the loop (§2.1): where the user talks from.
Its token is made by the device (§4.1) and only its SHA-256 is kept; the
pairing code is shown once by the Web.

``Presence`` (§5, §6.1) keeps, in memory only, each device's input activity
and whether it is connected, and decides the current device. ``EventHub``
(§8.1) is the principal-wide event stream the devices and the Web read;
each subscriber has a bounded queue, and one that falls behind is closed
(it reopens and starts again from ``hello``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from himari.common import DeepFrozen, JsonValue, PrincipalId
from himari.data.api import Device

PAIRING_TTL = timedelta(minutes=10)
TOUCH_INTERVAL = timedelta(minutes=10)  # how often a device's last_seen_at is written (§5)
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 32 letters: no I, O, 0, 1
CODE_LENGTH = 8
TOKEN_PREFIX = "hd_"
_TOKEN = re.compile(r"\Ahd_[A-Za-z0-9_-]{43}\Z")


def is_device_token(presented: str) -> bool:
    """Whether a bearer token claims to be a device's (by its prefix): such a
    token is checked only as one, never as the operator's."""
    return presented.startswith(TOKEN_PREFIX)


def token_shape_ok(token: str) -> bool:
    return bool(_TOKEN.match(token))


def normalize_code(raw: str) -> str | None:
    """A typed code as stored (case, hyphens and spaces ignored), or None
    when it cannot be one."""
    code = "".join(c for c in raw.upper() if c not in "- ")
    if len(code) != CODE_LENGTH or any(c not in CODE_ALPHABET for c in code):
        return None
    return code


def show_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


@dataclass(frozen=True)
class PairingCode(DeepFrozen):
    device: Device
    code: str  # as shown: "ABCD-EFGH"
    expires_at: datetime


@dataclass(frozen=True)
class DeviceCaller(DeepFrozen):
    principal: PrincipalId
    device: Device


class PairingRefused(Exception):
    """Why a pairing failed (appendix A.1): ``pairing_invalid``,
    ``pairing_expired``, ``pairing_used``, ``invalid_token``, ``token_in_use``
    (another device has it: make a new token) or ``pairing_busy`` (too many
    pairings wait already)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class Devices(Protocol):
    """V3-P83 §4. Data's errors (NOT_FOUND, INVALID_ARGUMENT,
    DUPLICATE_IDENTITY, LIMIT_EXCEEDED) pass through unchanged."""

    async def create(self, principal: PrincipalId, name: str) -> PairingCode:
        """A new device waiting for the returned code (the only live one)."""
        ...

    async def pair(self, raw_code: str, token: str) -> DeviceCaller:
        """§4.1: one at a time; a failure answers late. PairingRefused."""
        ...

    async def authenticate(self, token: str) -> DeviceCaller | None:
        """The paired, unrevoked device with this token; records that it was seen."""
        ...

    async def list(self, principal: PrincipalId) -> tuple[Device, ...]:
        """Paired devices and the one whose code is still live."""
        ...

    async def rename(self, principal: PrincipalId, device_id: str, name: str) -> Device: ...

    async def revoke(self, principal: PrincipalId, device_id: str) -> None: ...


# --- Presence and the event stream (§5, §6.1, §8.1) ------------------------------------------

SIGNAL_SECONDS = 120.0  # connected: its stream open and a signal within this
REOPEN_SECONDS = 30.0  # still connected this long after its stream closed (a reopen's gap)
CANDIDATE_SECONDS = 600.0  # the current device had input within this
SWITCH_SECONDS = 60.0  # another device takes over only once the current one had none for this
TICK_SECONDS = 15.0  # the current device is decided again this often: time alone makes the user away
MAX_IDLE_SECONDS = 30 * 24 * 3600  # a signal's idle_seconds is cut to this
MAX_STREAMS = 16  # event streams of one principal, devices and Web tabs together
QUEUE_EVENTS = 256  # what one subscriber may have waiting; past it the stream is closed


@dataclass(frozen=True)
class DeviceState(DeepFrozen):
    """A device as Presence knows it now; ``idle_seconds`` is None before its first signal."""

    device_id: str
    connected: bool
    idle_seconds: float | None
    time_zone: str | None


@dataclass(frozen=True)
class StreamEvent(DeepFrozen):
    name: str  # appendix A.3: activity, job, conversation, current, revoked, replaced
    data: JsonValue


class TooManyStreams(Exception):
    """The principal has MAX_STREAMS event streams open already."""


class Subscription(Protocol):
    """One open event stream."""

    @property
    def device_id(self) -> str | None: ...

    @property
    def finished(self) -> bool:
        """Closed and everything sent: the stream ends."""
        ...

    async def next(self, timeout: float) -> StreamEvent | None:
        """The next event; None when ``timeout`` passed first or it is finished."""
        ...


class EventHub(Protocol):
    def subscribe(self, principal: PrincipalId, device_id: str | None = None) -> Subscription:
        """A new stream (``device_id``: a device's, else the Web's). A device's
        earlier stream gets ``replaced`` and closes. TooManyStreams."""
        ...

    def unsubscribe(self, subscription: Subscription) -> None:
        """The stream ended (its reader is gone); idempotent."""
        ...

    def publish(self, principal: PrincipalId, name: str, data: JsonValue) -> None:
        """To every stream of the principal."""
        ...


class Presence(Protocol):
    def signal(self, principal: PrincipalId, device_id: str, idle_seconds: float, time_zone: str | None) -> None:
        """A device's input activity (§5): ``idle_seconds`` since the last input
        anywhere on it; ``time_zone`` an IANA name, or None when unknown."""
        ...

    def states(self, principal: PrincipalId) -> dict[str, DeviceState]:
        """The devices it heard of, by id."""
        ...

    def current(self, principal: PrincipalId) -> str | None:
        """The current device (§6.1); None: the user is away."""
        ...

    def revoked(self, principal: PrincipalId, device_id: str) -> None:
        """Forget the device, and end its streams with ``revoked`` (§8.1)."""
        ...
