"""DeviceStore, message origins and the most recent conversation (V3-P83 §4.2), on every backend."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from testkit import SCOPE, data_for, new_store, restart, user

from himari.common import ConversationId, PrincipalId, TextBlock, TrustedScope
from himari.data.api import (
    MAX_DEVICES,
    PAIRING_FAILURE_LIMIT,
    DataError,
    DataErrorCode,
    MessageOrigin,
    OriginKind,
    PairingResult,
    UserMessage,
)

ME = SCOPE.principal_id
SOON = timedelta(minutes=10)


def code(error: pytest.ExceptionInfo[DataError]) -> DataErrorCode:
    return error.value.code


def test_a_device_is_paired_once_with_its_code_and_found_by_its_token_after_a_restart() -> None:
    async def scenario() -> None:
        store = new_store()
        data = data_for(store)
        now = datetime.now(UTC)
        added = await data.add_device(ME, "  데스크톱 ", b"code-1", now + SOON)
        assert (added.device_id, added.name, added.paired_at) == ("d1", "데스크톱", None)
        assert await data.device_for_token(b"tok-1") is None  # not paired yet

        paired = await data.pair_device(b"code-1", b"tok-1", now)
        assert paired.result is PairingResult.PAIRED and paired.principal == ME
        assert paired.device is not None and paired.device.paired_at is not None

        again = await data.pair_device(b"code-1", b"tok-1", now)  # the answer was lost: the same answer
        assert again.result is PairingResult.RESENT and again.device == paired.device
        other = await data.pair_device(b"code-1", b"tok-2", now)  # anyone else with the code
        assert other.result is PairingResult.USED
        assert (await data.pair_device(b"code-1", b"tok-1", now + SOON)).result is PairingResult.EXPIRED
        assert (await data.pair_device(b"nope", b"tok-3", now)).result is PairingResult.INVALID

        data = data_for(restart(store))
        found = await data.device_for_token(b"tok-1")
        assert found is not None and found[0] == ME and found[1].device_id == "d1"
        assert [d.device_id for d in await data.list_devices(ME)] == ["d1"]

    asyncio.run(scenario())


def test_a_revoked_device_never_comes_back_through_its_token_or_kept_code() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        now = datetime.now(UTC)
        await data.add_device(ME, "Desk", b"code-1", now + SOON)
        await data.pair_device(b"code-1", b"tok-1", now)
        await data.revoke_device(ME, "d1")
        assert await data.device_for_token(b"tok-1") is None
        assert await data.list_devices(ME) == ()
        for token in (b"tok-1", b"tok-2"):  # within the code's ten minutes (review r2 B1)
            assert (await data.pair_device(b"code-1", token, now)).result is PairingResult.INVALID
        with pytest.raises(DataError) as error:
            await data.revoke_device(ME, "d1")
        assert code(error) is DataErrorCode.NOT_FOUND
        again = await data.add_device(ME, "Desk", b"code-2", now + SOON)  # its name and not its id are free again
        assert again.device_id == "d2"

    asyncio.run(scenario())


def test_a_new_code_replaces_the_unpaired_one_and_names_are_checked() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        now = datetime.now(UTC)
        await data.add_device(ME, "Desk", b"code-1", now + SOON)
        await data.add_device(ME, "Laptop", b"code-2", now + SOON)  # one live code at a time
        assert [d.name for d in await data.list_devices(ME)] == ["Laptop"]
        assert (await data.pair_device(b"code-1", b"tok-1", now)).result is PairingResult.INVALID
        await data.pair_device(b"code-2", b"tok-2", now)
        for name, expected in (("laptop", DataErrorCode.DUPLICATE_IDENTITY), ("WEB", DataErrorCode.INVALID_ARGUMENT),
                               ("cli", DataErrorCode.INVALID_ARGUMENT), ("", DataErrorCode.INVALID_ARGUMENT),
                               ("a\nb", DataErrorCode.INVALID_ARGUMENT), ("x" * 41, DataErrorCode.INVALID_ARGUMENT)):
            with pytest.raises(DataError) as error:
                await data.add_device(ME, name, b"code-3", now + SOON)
            assert code(error) is expected, name
        renamed = await data.rename_device(ME, "d2", "LAPTOP")  # its own name, case changed: fine
        assert renamed.name == "LAPTOP"
        await data.add_device(ME, "Desk", b"code-4", now + SOON)
        await data.pair_device(b"code-4", b"tok-4", now)
        with pytest.raises(DataError) as error:
            await data.rename_device(ME, "d3", "laptop")
        assert code(error) is DataErrorCode.DUPLICATE_IDENTITY
        with pytest.raises(DataError) as error:
            await data.rename_device(PrincipalId("mallory"), "d3", "Mine")
        assert code(error) is DataErrorCode.NOT_FOUND

    asyncio.run(scenario())


def test_failures_end_a_live_unpaired_code_but_never_a_paired_one() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        now = datetime.now(UTC)
        await data.add_device(ME, "Desk", b"code-1", now + SOON)
        await data.pair_device(b"code-1", b"tok-1", now)
        await data.add_device(ME, "Laptop", b"code-2", now + SOON)
        for _ in range(PAIRING_FAILURE_LIMIT - 1):
            await data.record_pairing_failure(now)
        assert (await data.pair_device(b"code-2", b"tok-2", now)).result is PairingResult.PAIRED
        await data.add_device(ME, "Tablet", b"code-3", now + SOON)
        for _ in range(PAIRING_FAILURE_LIMIT):
            await data.record_pairing_failure(now)
        assert (await data.pair_device(b"code-3", b"tok-3", now)).result is PairingResult.INVALID
        assert [d.name for d in await data.list_devices(ME)] == ["Desk", "Laptop"]  # paired ones untouched
        assert (await data.pair_device(b"code-1", b"tok-1", now)).result is PairingResult.RESENT

    asyncio.run(scenario())


def test_a_token_is_one_devices_and_the_device_limit_holds() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        now = datetime.now(UTC)
        await data.add_device(ME, "Desk", b"code-1", now + SOON)
        await data.pair_device(b"code-1", b"tok-1", now)
        await data.add_device(ME, "Laptop", b"code-2", now + SOON)
        taken = await data.pair_device(b"code-2", b"tok-1", now)  # another device's token (review C1)
        assert taken.result is PairingResult.TOKEN_IN_USE and taken.device is None
        assert (await data.pair_device(b"code-2", b"tok-2", now)).result is PairingResult.PAIRED  # nothing changed
        await data.revoke_device(ME, "d2")
        for n in range(MAX_DEVICES - 1):
            await data.add_device(ME, f"pc {n}", f"c{n}".encode(), now + SOON)
            await data.pair_device(f"c{n}".encode(), f"t{n}".encode(), now)
        with pytest.raises(DataError) as error:
            await data.add_device(ME, "one more", b"c-last", now + SOON)
        assert code(error) is DataErrorCode.LIMIT_EXCEEDED

    asyncio.run(scenario())


def test_touch_records_when_a_device_was_seen() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        now = datetime.now(UTC)
        await data.add_device(ME, "Desk", b"code-1", now + SOON)
        await data.pair_device(b"code-1", b"tok-1", now)
        await data.touch_device(ME, "d1", now)
        await data.touch_device(ME, "d9", now)  # unknown: nothing
        device, = await data.list_devices(ME)
        assert device.last_seen_at == now

    asyncio.run(scenario())


def test_a_message_keeps_its_origin_and_an_answer_too() -> None:
    async def scenario() -> None:
        store = new_store()
        store.create_conversation(SCOPE)
        data = data_for(store)
        device = MessageOrigin(OriginKind.DEVICE, "d1")
        await data.append_event(SCOPE, UserMessage((TextBlock("hi"),), origin=device))
        await data.append_event(SCOPE, UserMessage((TextBlock("web"),), origin=MessageOrigin(OriginKind.WEB)))
        await data.append_event(SCOPE, user("old style"))
        data = data_for(restart(store))
        origins = [e.payload.origin for e in await data.read_events(SCOPE)]  # type: ignore[union-attr]
        assert origins == [device, MessageOrigin(OriginKind.WEB), None]
        for bad in (MessageOrigin(OriginKind.DEVICE), MessageOrigin(OriginKind.CLI, "d1"),
                    MessageOrigin(OriginKind.DEVICE, "")):
            with pytest.raises(DataError) as error:
                await data.append_event(SCOPE, UserMessage((TextBlock("x"),), origin=bad))
            assert code(error) is DataErrorCode.INVALID_ARGUMENT

    asyncio.run(scenario())


def test_the_most_recent_user_conversation_skips_the_trash_and_other_principals() -> None:
    async def scenario() -> None:
        store = new_store()
        first, second = SCOPE, TrustedScope(ME, ConversationId("conv_2"))
        foreign = TrustedScope(PrincipalId("mallory"), ConversationId("conv_3"))
        for scope in (first, second, foreign):
            store.create_conversation(scope)
        data = data_for(store)
        assert await data.most_recent_user_conversation(ME) is None
        await data.append_event(first, user("one"))
        await data.append_event(second, user("two"))
        await data.append_event(foreign, user("theirs"))
        recent = await data.most_recent_user_conversation(ME)
        assert recent is not None and recent.conversation_id == second.conversation_id
        await data.append_event(first, user("one again"))
        recent = await data.most_recent_user_conversation(ME)
        assert recent is not None and recent.conversation_id == first.conversation_id
        await data.trash_conversation(first)
        recent = await data.most_recent_user_conversation(ME)
        assert recent is not None and recent.conversation_id == second.conversation_id

    asyncio.run(scenario())
