"""V3-P93: an input's interface is durable data, including wake admission."""

from __future__ import annotations

import asyncio
import json
from dataclasses import FrozenInstanceError, replace

import pytest
from testkit import SCOPE, data_for, new_store, restart

from himari.common import TextBlock
from himari.data import codec
from himari.data.api import (
    DataError, DataErrorCode, EventKind, InterfaceCapabilities, InterfaceKind, InterfaceSnapshot,
    MessageOrigin, OriginKind, UserMessage, Wake, WakeKind, WakeReason,
)

WEB = InterfaceSnapshot(InterfaceKind.WEB, InterfaceCapabilities(True, True))
DESK = InterfaceSnapshot(InterfaceKind.WINDOWS_COMPANION)
TRY = (WakeReason(WakeKind.TRY),)


def test_input_snapshot_survives_reopening_the_store() -> None:
    async def scenario() -> None:
        store = new_store()
        store.create_conversation(SCOPE)
        data = data_for(store)
        message = UserMessage((TextBlock("hello"),), origin=MessageOrigin(OriginKind.WEB), interface=WEB)
        await data.append_event(SCOPE, message)
        again = data_for(restart(store))
        (event,) = await again.read_events(SCOPE)
        assert event.payload == message
        assert isinstance(event.payload, UserMessage) and event.payload.interface == WEB

    asyncio.run(scenario())


def test_mismatched_input_is_refused_without_recording_anything() -> None:
    async def scenario() -> None:
        store = new_store()
        store.create_conversation(SCOPE)
        data = data_for(store)
        with pytest.raises(DataError) as refused:
            await data.append_event(SCOPE, UserMessage((TextBlock("x"),),
                                    origin=MessageOrigin(OriginKind.DEVICE, "d1"), interface=WEB))
        assert refused.value.code is DataErrorCode.INVALID_ARGUMENT
        assert await data.read_events(SCOPE) == ()

    asyncio.run(scenario())


def test_wake_snapshot_is_committed_with_its_event_and_key() -> None:
    async def scenario() -> None:
        store = new_store()
        store.create_conversation(SCOPE)
        data = data_for(store)
        with pytest.raises(DataError) as refused:
            await data.begin_wake(SCOPE, TRY, "d1", keys=("try:1",), interface=WEB)
        assert refused.value.code is DataErrorCode.INVALID_ARGUMENT
        assert await data.opened_wakes() == () and await data.read_events(SCOPE) == ()
        event = await data.begin_wake(SCOPE, TRY, "d1", keys=("try:1",), interface=DESK)
        again = data_for(restart(store))
        assert (await again.read_events(SCOPE))[0] == event
        assert isinstance(event.payload, Wake) and event.payload.interface == DESK
        assert (await again.read_wake(SCOPE.principal_id, event.payload.wake_id)).event_sequence == event.sequence

    asyncio.run(scenario())


@pytest.mark.parametrize("message", [
    UserMessage((TextBlock("legacy"),)),
    Wake("w1", TRY, "d1"),
])
def test_legacy_payloads_remain_readable_without_a_snapshot(message: UserMessage | Wake) -> None:
    kind = EventKind.USER_MESSAGE if isinstance(message, UserMessage) else EventKind.WAKE
    encoded = codec.payload(message)
    assert "interface" not in json.loads(encoded)
    assert codec.payload_from(kind, encoded) == message
    updated = replace(message, interface=WEB if isinstance(message, UserMessage) else DESK)
    decoded = codec.payload_from(kind, codec.payload(updated))
    assert isinstance(decoded, (UserMessage, Wake)) and decoded == updated


@pytest.mark.parametrize("change", [
    {"version": 2}, {"version": True}, {"kind": "invented"},
    {"capabilities": {"file_download": "true", "workspace_browser": False}},
    {"capabilities": {"file_download": True, "workspace_browser": 1}},
    {"kind": "unknown"},
])
def test_invalid_persisted_snapshots_fail_closed(change: dict[str, object]) -> None:
    encoded = json.loads(codec.payload(UserMessage((TextBlock("x"),), interface=WEB)))
    encoded["interface"].update(change)
    with pytest.raises(ValueError):
        codec.payload_from(EventKind.USER_MESSAGE, json.dumps(encoded))


def test_snapshot_and_capabilities_are_immutable() -> None:
    with pytest.raises(FrozenInstanceError):
        WEB.capabilities.file_download = False  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        WEB.version = 2  # type: ignore[misc]
