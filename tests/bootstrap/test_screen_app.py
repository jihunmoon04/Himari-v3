"""V3-P84 §4–§5: a device's screen, staged and attached once, never placed in
the Workspace, and what the model is told of it."""

from __future__ import annotations

import asyncio
import struct
import zlib
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from himari.bootstrap.application import Application, ScreenRefused
from himari.common import AttachmentRef, FileId, PrincipalId, TextBlock, TrustedScope
from himari.data.api import ALREADY_SENT, DataError, DataErrorCode, MessageOrigin, OriginKind, UserMessage
from himari.prompt.api import SCREEN_MOVE_NOTE, SCREEN_NOTE, UserTurn
from himari.provider.api import NormalizedFinal
from himari.provider.scripted import ScriptedAdapter

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
            prefix = sent.stable_prefix
            assert prefix.index(SCREEN_MOVE_NOTE) == prefix.index(SCREEN_NOTE) + 1  # §5: it points
        finally:
            await app.close()

    asyncio.run(scenario())


def test_the_move_note_only_for_a_model_that_points_and_neither_with_the_companion_off(tmp_path: Path) -> None:
    async def scenario(companion: bool, **options: bool) -> tuple[str, ...]:
        adapter = answers(**options)
        place = tmp_path / str(len(list(tmp_path.iterdir())))
        place.mkdir()
        app = await opened(place, adapter, companion=companion)
        try:
            scope = TrustedScope(ME, await app.create_conversation(ME))
            await app.send(scope, "hi")
            await app.runner.wait(scope)
            return adapter.prepared_inputs[0].stable_prefix
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
