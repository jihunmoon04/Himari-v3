"""Presence public contract (V3-P83 §2.2): a principal's devices and how they pair.

A device is the human side of the loop (§2.1): where the user talks from.
Its token is made by the device (§4.1) and only its SHA-256 is kept; the
pairing code is shown once by the Web. Part 2 adds the current device and
the event stream to this contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from himari.common import DeepFrozen, PrincipalId
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
