"""V3-P84 §4–§5: a device's screen, staged and attached once, never placed in
the Workspace, and what the model is told of it."""

from __future__ import annotations

import asyncio
import base64
import secrets
import struct
import zlib
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from himari.bootstrap.application import SCREEN_MAX_BYTES, Application, ScreenRefused
from himari.common import AttachmentRef, CallId, FileId, PrincipalId, TextBlock, ToolCall, ToolId, TrustedScope, thaw
from himari.data.api import ALREADY_SENT, DataError, DataErrorCode, MessageOrigin, OriginKind, UserMessage
from himari.presence.api import Session, Signals, Subscription
from himari.prompt.api import SCREEN_MOVE_NOTE, SCREEN_NOTE, ToolGroupTurn, UserTurn
from himari.provider.api import NormalizedFinal, NormalizedToolCalls
from himari.provider.scripted import ScriptedAdapter
from himari.tool.screen import NO_DEVICE

ME = PrincipalId("operator")
DESK = MessageOrigin(OriginKind.DEVICE, "d1")


def png(width: int = 4, height: int = 3) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    rows = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


async def body(data: bytes) -> AsyncIterator[bytes]:
    yield data


async def opened(tmp_path: Path, adapter: ScriptedAdapter, **options: object) -> Application:
    return await Application.open(tmp_path / "data", adapter, model_id="scripted-model", sandbox=False,
                                  cleanup_seconds=None, **options)  # type: ignore[arg-type]


def answers(n: int = 1, **options: bool) -> ScriptedAdapter:
    return ScriptedAdapter([NormalizedFinal((TextBlock("응"),)) for _ in range(n)], **options)


def test_a_screen_is_named_here_attached_once_and_never_placed(tmp_path: Path) -> None:
    async def scenario() -> None:
        adapter = answers(image_input=True, screen_points=True)
        app = await opened(tmp_path, adapter)
        try:
            scope = TrustedScope(ME, await app.create_conversation(ME))
            record = await app.upload_screen(scope, body(png()))
            assert record.name.startswith("screen-") and record.name.endswith(".png")
            assert record.media_type == "image/png"
            await app.send(scope, "이거 봐", origin=DESK, screen=record.file_id)
            await app.runner.wait(scope)
            (stored,) = await app.files(scope)
            assert stored.attached and stored.workspace_path is None  # §4.2: not copied into uploads/
            with pytest.raises(DataError) as refused:
                await app.send(scope, "또", origin=DESK, screen=record.file_id)
            assert refused.value.code is DataErrorCode.ATTACHMENT_REFUSED and refused.value.reason == ALREADY_SENT
            (sent,) = adapter.prepared_inputs
            turn = sent.conversation[-1]
            assert isinstance(turn, UserTurn)
            assert turn.blocks[-1] == AttachmentRef(record.file_id, 1, screen=True)
            assert SCREEN_NOTE + " " + SCREEN_MOVE_NOTE in "\n".join(sent.stable_prefix)  # §5: it points
        finally:
            await app.close()

    asyncio.run(scenario())


def test_the_move_note_only_for_a_model_that_points_and_neither_with_the_companion_off(tmp_path: Path) -> None:
    async def scenario(companion: bool, **options: bool) -> str:
        adapter = answers(**options)
        place = tmp_path / str(len(list(tmp_path.iterdir())))
        place.mkdir()
        app = await opened(place, adapter, companion=companion)
        try:
            scope = TrustedScope(ME, await app.create_conversation(ME))
            await app.send(scope, "hi")
            await app.runner.wait(scope)
            return "\n".join(adapter.prepared_inputs[0].stable_prefix)
        finally:
            await app.close()

    looking = asyncio.run(scenario(True, image_input=True))
    assert SCREEN_NOTE in looking and SCREEN_MOVE_NOTE not in looking
    blind = asyncio.run(scenario(True, screen_points=True))  # points without images: not told
    assert SCREEN_MOVE_NOTE not in blind
    off = asyncio.run(scenario(False, image_input=True, screen_points=True))
    assert SCREEN_NOTE not in off and SCREEN_MOVE_NOTE not in off


def test_a_screen_is_a_png_or_a_jpeg(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = await opened(tmp_path, answers())
        try:
            scope = TrustedScope(ME, await app.create_conversation(ME))
            before = app.data.blobs.rows.live_total()
            with pytest.raises(ScreenRefused):
                await app.upload_screen(scope, body(b"GIF89a\x01\x00\x01\x00" + b"\x00" * 20))
            with pytest.raises(ScreenRefused):
                await app.upload_screen(scope, body(b"plain text"))
            assert await app.data.count_staged(scope) == 0
            assert app.data.blobs.rows.live_total() == before  # PR #126 review C1: refused before it is stored
            text = await app.upload(scope, "notes.txt", "text/plain", body(b"hello"))
            with pytest.raises(ScreenRefused):  # a staged upload that is not an image is no screen
                await app.send(scope, "x", origin=DESK, screen=FileId(text.file_id))
            log = await app.data.read_events(scope)
            assert not [e for e in log if isinstance(e.payload, UserMessage)]
        finally:
            await app.close()

    asyncio.run(scenario())


def test_the_screen_mark_is_kept_only_when_true() -> None:
    """§4.3: older events have no "screen" and read as not one; no schema change."""
    from himari.data import codec
    from himari.data.api import EventKind

    message = UserMessage((TextBlock("봐"), AttachmentRef("f1", 1, screen=True), AttachmentRef("f2", 1)), origin=DESK)
    text = codec.payload(message)
    assert text.count('"screen":true') == 1
    assert codec.payload_from(EventKind.USER_MESSAGE, text) == message


def test_a_broken_screen_is_refused_and_repeating_it_takes_no_more_room(tmp_path: Path) -> None:
    """PR #126 re-review N1: a PNG's start with a header that cannot be read is stored, then refused;
    the same bytes again are the same blob, and a body over the limit is never kept."""
    async def scenario() -> None:
        app = await opened(tmp_path, answers())
        try:
            scope = TrustedScope(ME, await app.create_conversation(ME))
            broken = b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR" + b"\x00" * 64  # width and height 0
            before = app.data.blobs.rows.live_total()
            for _ in range(3):
                with pytest.raises(ScreenRefused):
                    await app.upload_screen(scope, body(broken))
            assert app.data.blobs.rows.live_total() - before <= len(broken)  # one blob at most, for the sweep
            oversized = png() + b"\x00" * SCREEN_MAX_BYTES
            with pytest.raises(DataError) as refused:
                await app.upload_screen(scope, body(oversized))
            assert refused.value.code is DataErrorCode.TOO_LARGE
            assert app.data.blobs.rows.live_total() - before <= len(broken)
            assert await app.data.count_staged(scope) == 0
        finally:
            await app.close()

    asyncio.run(scenario())


# --- V3-P91: the screen Tool ------------------------------------------------------------------------

def looks_then_answers() -> ScriptedAdapter:
    return ScriptedAdapter([NormalizedToolCalls((ToolCall(CallId("c0"), 0, ToolId("look_at_screen"), {}),)),
                            NormalizedFinal((TextBlock("보여요"),))], image_input=True, screen_tool=True)


async def screen_request(stream: Subscription) -> dict[str, object]:
    while True:
        event = await stream.next(5)
        assert event is not None, "no screen request came"
        if event.name == "screen_request":
            data = thaw(event.data)
            assert isinstance(data, dict)
            return data


def tool_result(adapter: ScriptedAdapter):  # type: ignore[no-untyped-def]
    (group,) = [t for t in adapter.prepared_inputs[-1].conversation if isinstance(t, ToolGroupTurn)]
    return group.results[group.calls[0].call_id]


def test_a_device_message_looks_at_that_devices_screen(tmp_path: Path) -> None:
    """V3-P91 §2–§5: the tool asks the device the message came from, alone; its image is the result, a screen."""
    async def scenario() -> None:
        adapter = looks_then_answers()
        app = await opened(tmp_path, adapter, companion=True)
        try:
            made = await app.devices.create(ME, "데스크톱")
            token = "hd_" + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
            device = (await app.devices.pair(made.code, token)).device.device_id
            stream, web = app.hub.subscribe(ME, device), app.hub.subscribe(ME)
            app.hub.signal(ME, device, 0, "Asia/Seoul", Signals(session=Session.ACTIVE, screen_look=True))
            scope = TrustedScope(ME, await app.create_conversation(ME))
            await app.send(scope, "이 에러 뭐야?", origin=MessageOrigin(OriginKind.DEVICE, device))
            asked = await screen_request(stream)
            assert asked["conversation"] == scope.conversation_id and asked["job_id"] == app.runner.status(scope).job_id
            assert app.screens is not None
            assert app.screens.deliver(ME, device, str(asked["request_id"]), png()).value == "taken"
            await app.runner.wait(scope)
            result = tool_result(adapter)
            (attachment,) = [b for b in result.blocks if isinstance(b, AttachmentRef)]
            assert attachment.screen and not result.is_error
            (stored,) = [f for f in await app.files(scope) if f.file_id == attachment.file_id]
            assert stored.name.startswith("screen-") and stored.workspace_path is None  # V3-P84 §4.4's rules
            seen = []
            while (event := await web.next(0)) is not None:
                seen.append(event.name)
            assert "screen_request" not in seen  # §4.1: that device's stream only
            tools = [d.tool_id for d in adapter.prepared_inputs[0].tool_catalog]
            assert "look_at_screen" in tools
        finally:
            await app.close()

    asyncio.run(scenario())


def test_a_web_message_asks_no_device_and_a_model_without_the_capability_is_not_offered(tmp_path: Path) -> None:
    async def scenario() -> None:
        adapter = looks_then_answers()
        app = await opened(tmp_path, adapter, companion=True)
        try:
            scope = TrustedScope(ME, await app.create_conversation(ME))
            await app.send(scope, "내 화면 봐 줘", origin=MessageOrigin(OriginKind.WEB))
            await app.runner.wait(scope)
            result = tool_result(adapter)
            assert result.is_error and result.blocks == (TextBlock(NO_DEVICE),)
        finally:
            await app.close()
        plain = answers(image_input=True)
        place = tmp_path / "plain"
        place.mkdir()
        app = await opened(place, plain, companion=True)
        try:
            scope = TrustedScope(ME, await app.create_conversation(ME))
            await app.send(scope, "안녕", origin=MessageOrigin(OriginKind.WEB))
            await app.runner.wait(scope)
            assert "look_at_screen" not in [d.tool_id for d in plain.prepared_inputs[0].tool_catalog]
        finally:
            await app.close()

    asyncio.run(scenario())
