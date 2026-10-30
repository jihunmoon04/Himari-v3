"""``Devices`` over Data's DeviceStore (V3-P83 §4.1)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
from collections.abc import Callable
from datetime import UTC, datetime

from himari.common import PrincipalId
from himari.data.api import Device, DeviceStore, PairingResult
from himari.presence.api import (
    CODE_ALPHABET,
    CODE_LENGTH,
    PAIRING_TTL,
    TOUCH_INTERVAL,
    DeviceCaller,
    PairingCode,
    PairingRefused,
    normalize_code,
    show_code,
    token_shape_ok,
)

FAILED_PAIRING_DELAY_SECONDS = 1.0  # as a failed sign-in (Web)

_REFUSED = {PairingResult.INVALID: "pairing_invalid", PairingResult.EXPIRED: "pairing_expired",
            PairingResult.USED: "pairing_used"}


def _hash(value: str) -> bytes:
    return hashlib.sha256(value.encode("ascii")).digest()


def _uniform(token: str) -> bool:
    """A token whose 32 random bytes are all one value: a broken generator (§9.1)."""
    raw = base64.urlsafe_b64decode(token[3:] + "=")
    return len(set(raw)) == 1


class DeviceRegistry:
    def __init__(self, store: DeviceStore, *, clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                 failure_delay: float = FAILED_PAIRING_DELAY_SECONDS) -> None:
        self._store = store
        self._clock = clock
        self._failure_delay = failure_delay
        self._pairing = asyncio.Lock()  # one pairing at a time: concurrent guesses wait out each delay

    async def create(self, principal: PrincipalId, name: str) -> PairingCode:
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        expires = self._clock() + PAIRING_TTL
        device = await self._store.add_device(principal, name, _hash(code), expires)
        return PairingCode(device, show_code(code), expires)

    async def pair(self, raw_code: str, token: str) -> DeviceCaller:
        if not token_shape_ok(token) or _uniform(token):
            raise PairingRefused("invalid_token")
        async with self._pairing:
            code = normalize_code(raw_code)
            now = self._clock()
            outcome = (await self._store.pair_device(_hash(code), _hash(token), now) if code is not None
                       else None)
            if outcome is not None and outcome.principal is not None and outcome.device is not None:
                return DeviceCaller(outcome.principal, outcome.device)
            result = PairingResult.INVALID if outcome is None else outcome.result
            if result is PairingResult.INVALID:
                await self._store.record_pairing_failure(now)
            await asyncio.sleep(self._failure_delay)
            raise PairingRefused(_REFUSED[result])

    async def authenticate(self, token: str) -> DeviceCaller | None:
        if not token_shape_ok(token):
            return None
        found = await self._store.device_for_token(_hash(token))
        if found is None:
            return None
        principal, device = found
        now = self._clock()
        if device.last_seen_at is None or now - device.last_seen_at >= TOUCH_INTERVAL:
            await self._store.touch_device(principal, device.device_id, now)
        return DeviceCaller(principal, device)

    async def list(self, principal: PrincipalId) -> tuple[Device, ...]:
        now = self._clock()
        return tuple(d for d in await self._store.list_devices(principal)
                     if d.paired_at is not None or (d.pairing_expires_at is not None and d.pairing_expires_at > now))

    async def rename(self, principal: PrincipalId, device_id: str, name: str) -> Device:
        return await self._store.rename_device(principal, device_id, name)

    async def revoke(self, principal: PrincipalId, device_id: str) -> None:
        await self._store.revoke_device(principal, device_id)
