"""DeviceRegistry (V3-P83 §4.1): codes, pairing, tokens."""

from __future__ import annotations

import asyncio
import base64
import secrets
import time
from datetime import UTC, datetime, timedelta

import pytest

from himari.common import PrincipalId
from himari.data.memory import InMemoryData, InMemoryStore
from himari.presence.api import CODE_ALPHABET, PAIRING_TTL, PairingRefused, normalize_code, token_shape_ok
from himari.presence.devices import DeviceRegistry

ME = PrincipalId("alice")


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def token() -> str:
    return "hd_" + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


def registry(clock: Clock | None = None, delay: float = 0.0) -> tuple[DeviceRegistry, InMemoryData, Clock]:
    clock = clock or Clock()
    data = InMemoryData(InMemoryStore())
    return DeviceRegistry(data, clock=clock, failure_delay=delay), data, clock


def refusal(error: pytest.ExceptionInfo[PairingRefused]) -> str:
    return error.value.code


def test_a_code_pairs_a_device_whose_token_then_signs_it_in() -> None:
    async def scenario() -> None:
        devices, data, clock = registry()
        made = await devices.create(ME, "데스크톱")
        assert len(made.code) == 9 and made.code[4] == "-" and made.expires_at == clock.now + PAIRING_TTL
        assert all(c in CODE_ALPHABET for c in made.code.replace("-", ""))
        mine = token()
        paired = await devices.pair(made.code.lower().replace("-", " "), mine)  # typed loosely
        assert (paired.principal, paired.device.device_id) == (ME, "d1")
        again = await devices.pair(made.code, mine)  # a lost answer, sent again
        assert again.device.device_id == "d1"
        found = await devices.authenticate(mine)
        assert found is not None and found.principal == ME
        assert await devices.authenticate(token()) is None
        assert await devices.authenticate("hd_short") is None
        stored, = await data.list_devices(ME)
        assert stored.last_seen_at == clock.now  # touched on the first sign-in

    asyncio.run(scenario())


def test_last_seen_is_written_at_most_every_ten_minutes() -> None:
    async def scenario() -> None:
        devices, data, clock = registry()
        made = await devices.create(ME, "Desk")
        mine = token()
        await devices.pair(made.code, mine)
        await devices.authenticate(mine)
        first = clock.now
        clock.now += timedelta(minutes=9)
        await devices.authenticate(mine)
        assert (await data.list_devices(ME))[0].last_seen_at == first
        clock.now += timedelta(minutes=1)
        await devices.authenticate(mine)
        assert (await data.list_devices(ME))[0].last_seen_at == clock.now

    asyncio.run(scenario())


def test_refusals_name_their_reason() -> None:
    async def scenario() -> None:
        devices, _, clock = registry()
        made = await devices.create(ME, "Desk")
        for bad in ("hd_" + "A" * 43, "hd_short", "xx_" + token()[3:], token() + "x"):  # uniform bytes, shapes
            with pytest.raises(PairingRefused) as error:
                await devices.pair(made.code, bad)
            assert refusal(error) == "invalid_token", bad
        with pytest.raises(PairingRefused) as error:
            await devices.pair("not a code", token())
        assert refusal(error) == "pairing_invalid"
        await devices.pair(made.code, token())
        with pytest.raises(PairingRefused) as error:
            await devices.pair(made.code, token())  # someone else, after it paired
        assert refusal(error) == "pairing_used"
        late = await devices.create(ME, "Laptop")
        clock.now += PAIRING_TTL
        with pytest.raises(PairingRefused) as error:
            await devices.pair(late.code, token())
        assert refusal(error) == "pairing_expired"

    asyncio.run(scenario())


def test_five_wrong_codes_end_the_live_code() -> None:
    async def scenario() -> None:
        devices, _, _ = registry()
        made = await devices.create(ME, "Desk")
        wrong = "".join("A" if c != "A" else "B" for c in made.code.replace("-", ""))
        for _ in range(5):
            with pytest.raises(PairingRefused):
                await devices.pair(wrong, token())
        with pytest.raises(PairingRefused) as error:
            await devices.pair(made.code, token())
        assert refusal(error) == "pairing_invalid"
        assert await devices.list(ME) == ()

    asyncio.run(scenario())


def test_concurrent_wrong_guesses_each_wait_out_the_delay() -> None:
    async def scenario() -> None:
        devices, _, _ = registry(delay=0.05)
        await devices.create(ME, "Desk")
        started = time.monotonic()
        results = await asyncio.gather(*(devices.pair("AAAA-AAAA", token()) for _ in range(4)),
                                       return_exceptions=True)
        assert all(isinstance(r, PairingRefused) for r in results)
        assert time.monotonic() - started >= 0.2  # one at a time, not four delays at once

    asyncio.run(scenario())


def test_the_list_hides_an_ended_code_and_a_revoked_device_signs_in_no_more() -> None:
    async def scenario() -> None:
        devices, _, clock = registry()
        made = await devices.create(ME, "Desk")
        mine = token()
        await devices.pair(made.code, mine)
        await devices.create(ME, "Laptop")
        assert [d.name for d in await devices.list(ME)] == ["Desk", "Laptop"]
        clock.now += PAIRING_TTL
        assert [d.name for d in await devices.list(ME)] == ["Desk"]
        await devices.revoke(ME, "d1")
        assert await devices.authenticate(mine) is None
        assert await devices.list(ME) == ()

    asyncio.run(scenario())


def test_code_and_token_shapes() -> None:
    assert normalize_code("abcd-efgh") == "ABCDEFGH"
    assert normalize_code("ABCD EFG") is None and normalize_code("ABCD-EFG0") is None  # 0 is not in the alphabet
    assert token_shape_ok(token()) and not token_shape_ok("hd_" + "=" * 43)
