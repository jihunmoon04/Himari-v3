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


def test_read_presented_files_filters_before_pagination_and_excludes_hidden_wakes() -> None:
    from collections.abc import AsyncIterator
    from testkit import FAKE, text_result
    from himari.common import (
        AttachmentRef, CallId, CallRef, EventId, FileId, RawRepresentation, ToolCall, ToolId, TrustedScope, WorkspaceId,
    )
    from himari.data.api import (
        NO_IMAGE, AssistantToolCalls, EventRef, InitialActivation, PresentedFile, PromptBasis, RawReceived,
        RunPreparation, ToolCompletion, ToolExecutionStatus, WakeStatus,
    )

    async def chunks(part: bytes) -> AsyncIterator[bytes]:
        yield part

    async def scenario() -> None:
        data = data_for(new_store())
        cid = await data.create_conversation(
            SCOPE.principal_id, title="c1", activations=(InitialActivation(FAKE, WorkspaceId("ws")),)
        )
        other_cid = await data.create_conversation(
            SCOPE.principal_id, title="c2", activations=(InitialActivation(FAKE, WorkspaceId("ws")),)
        )
        scope = TrustedScope(SCOPE.principal_id, cid)
        other_scope = TrustedScope(SCOPE.principal_id, other_cid)

        async def make_call(target: TrustedScope, trigger_event_id: EventId | None = None) -> CallRef:
            refs = () if trigger_event_id is None else (EventRef(trigger_event_id),)
            run_id = await data.prepare_run(
                target,
                RunPreparation(b"{}", "application/json", RawRepresentation.ADAPTER_SERIALIZED,
                               refs, PromptBasis(0, 0), "p", "m"),
            )
            await data.mark_dispatch(target, run_id)
            await data.record_raw_outcome(target, run_id, RawReceived(b"{}", "application/json"))
            await data.project_model_output(
                target, run_id, AssistantToolCalls((ToolCall(CallId("c0"), 0, ToolId("inside/present_file"), {}),))
            )
            return CallRef(run_id, CallId("c0"))

        # 1. Valid public presented file v1 on "/workspace/out.txt"
        b_v1 = await data.blobs.put(scope.principal_id, chunks(b"v1-bytes"), 1 << 20)
        call_v1 = await make_call(scope)
        await data.commit_tool_outcome(
            scope, call_v1,
            ToolCompletion(ToolExecutionStatus.SUCCEEDED, text_result("ok"),
                           presented_files=(PresentedFile(b_v1, "out.txt", "text/plain", "/workspace/out.txt"),)),
        )
        v1_rec = (await data.read_call_files(scope, (call_v1,)))[call_v1][0]

        # 2. Noise between valid items to verify pre-pagination filtering:
        # 2a. Attached upload
        b_up = await data.blobs.put(scope.principal_id, chunks(b"upload"), 1 << 20)
        up_rec = await data.stage_upload(scope, b_up, "up.txt", "text/plain")
        await data.append_event(
            scope,
            UserMessage((TextBlock("see"), AttachmentRef(up_rec.file_id, 1)), origin=MessageOrigin(OriginKind.WEB), interface=WEB),
        )

        # 2b. Tool image file (origin=TOOL)
        b_tool = await data.blobs.put(scope.principal_id, chunks(b"tool-img"), 1 << 20)
        call_tool = await make_call(scope)
        tool_rec = await data.stage_tool_file(scope, call_tool, b_tool, "img.png", "image/png", NO_IMAGE)
        await data.commit_tool_outcome(scope, call_tool, ToolCompletion(ToolExecutionStatus.SUCCEEDED, text_result("ok")))

        # 2c. Presented file whose ToolResult event was not recorded (record_event=False)
        b_no_ev = await data.blobs.put(scope.principal_id, chunks(b"no-event"), 1 << 20)
        call_no_ev = await make_call(scope)
        await data.commit_tool_outcome(
            scope, call_no_ev,
            ToolCompletion(ToolExecutionStatus.SUCCEEDED, text_result("ok"), record_event=False,
                           presented_files=(PresentedFile(b_no_ev, "noev.txt", "text/plain", "/workspace/noev.txt"),)),
        )
        no_ev_rec = (await data.read_call_files(scope, (call_no_ev,)))[call_no_ev][0]

        # 2d. Presented file from an OPENED (unshown) free wake held run
        wake_open_ev = await data.begin_wake(scope, TRY, "d1", keys=("k:open",), interface=DESK)
        assert isinstance(wake_open_ev.payload, Wake)
        call_open = await make_call(scope, wake_open_ev.event_id)
        await data.hold_wake_run(scope, wake_open_ev.payload.wake_id, call_open.run_id)
        b_open = await data.blobs.put(scope.principal_id, chunks(b"open-wake"), 1 << 20)
        await data.commit_tool_outcome(
            scope, call_open,
            ToolCompletion(ToolExecutionStatus.SUCCEEDED, text_result("ok"),
                           presented_files=(PresentedFile(b_open, "open.txt", "text/plain", "/workspace/open.txt"),)),
        )
        open_rec = (await data.read_call_files(scope, (call_open,)))[call_open][0]
        # End the opened wake as PASSED so we can start another wake
        await data.end_wake(scope.principal_id, wake_open_ev.payload.wake_id, WakeStatus.PASSED)

        # 2e. Presented file from a DROPPED free wake
        wake_drop_ev = await data.begin_wake(scope, TRY, "d1", keys=("k:drop",), interface=DESK)
        assert isinstance(wake_drop_ev.payload, Wake)
        call_drop = await make_call(scope, wake_drop_ev.event_id)
        await data.hold_wake_run(scope, wake_drop_ev.payload.wake_id, call_drop.run_id)
        b_drop = await data.blobs.put(scope.principal_id, chunks(b"drop-wake"), 1 << 20)
        await data.commit_tool_outcome(
            scope, call_drop,
            ToolCompletion(ToolExecutionStatus.SUCCEEDED, text_result("ok"),
                           presented_files=(PresentedFile(b_drop, "drop.txt", "text/plain", "/workspace/drop.txt"),)),
        )
        drop_rec = (await data.read_call_files(scope, (call_drop,)))[call_drop][0]
        await data.end_wake(scope.principal_id, wake_drop_ev.payload.wake_id, WakeStatus.DROPPED)

        # 2f. Presented file in another conversation
        b_other = await data.blobs.put(other_scope.principal_id, chunks(b"other-conv"), 1 << 20)
        call_other = await make_call(other_scope)
        await data.commit_tool_outcome(
            other_scope, call_other,
            ToolCompletion(ToolExecutionStatus.SUCCEEDED, text_result("ok"),
                           presented_files=(PresentedFile(b_other, "other.txt", "text/plain", "/workspace/other.txt"),)),
        )
        other_rec = (await data.read_call_files(other_scope, (call_other,)))[call_other][0]

        # 3. Valid public presented file v2 on "/workspace/out.txt" (same path -> version 2 of v1_rec.file_id)
        b_v2 = await data.blobs.put(scope.principal_id, chunks(b"v2-bytes-longer"), 1 << 20)
        call_v2 = await make_call(scope)
        await data.commit_tool_outcome(
            scope, call_v2,
            ToolCompletion(ToolExecutionStatus.SUCCEEDED, text_result("ok"),
                           presented_files=(PresentedFile(b_v2, "out.txt", "text/plain", "/workspace/out.txt"),)),
        )
        v2_rec = (await data.read_call_files(scope, (call_v2,)))[call_v2][0]
        assert v2_rec.file_id == v1_rec.file_id and v2_rec.version == 2

        # Pagination filters out all non-public/non-presented files BEFORE slicing:
        page1 = await data.read_presented_files(scope, limit=1, offset=0)
        assert [(r.file_id, r.version) for r in page1] == [(v1_rec.file_id, 2)]

        page2 = await data.read_presented_files(scope, limit=1, offset=1)
        assert [(r.file_id, r.version) for r in page2] == [(v1_rec.file_id, 1)]

        page3 = await data.read_presented_files(scope, limit=1, offset=2)
        assert list(page3) == []

        got_v2 = await data.read_presented_file(scope, v1_rec.file_id, 2)
        got_v1 = await data.read_presented_file(scope, v1_rec.file_id, 1)
        assert got_v2 is not None and got_v2.size == len(b"v2-bytes-longer")
        assert got_v1 is not None and got_v1.size == len(b"v1-bytes")

        for forbidden_id in (
            up_rec.file_id, tool_rec.file_id, no_ev_rec.file_id,
            open_rec.file_id, drop_rec.file_id, other_rec.file_id,
        ):
            assert await data.read_presented_file(scope, forbidden_id, 1) is None

    asyncio.run(scenario())
