"""Images in Data (V3-P67 §3, §4): sniffing, image status and model copies,
Tool images staged and attached by their call, copies counted as blobs —
on every backend."""

from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncIterator, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from PIL import Image
from testkit import FAKE, data_for, new_store, text_result

from himari.common import (
    AttachmentRef,
    ModelToolResult,
    CallId,
    CallRef,
    PrincipalId,
    RawRepresentation,
    ToolCall,
    ToolId,
    TrustedScope,
    WorkspaceId,
)
from himari.data.api import (
    AssistantToolCalls,
    DataError,
    DataErrorCode,
    FileOrigin,
    ImageStatus,
    InitialActivation,
    PreparedView,
    PromptBasis,
    RawReceived,
    RunPreparation,
    ToolCompletion,
    ToolExecutionStatus,
    UserMessage,
    ViewState,
    sniff_image,
    view_state,
)
from himari.data.blobs import BlobRow
from himari.data.memory import InMemoryData
from himari.data.sqlite import SqliteData

ALICE = PrincipalId("alice")
Data = InMemoryData | SqliteData


def picture(fmt: str = "PNG", size: tuple[int, int] = (40, 30), mode: str = "RGB") -> bytes:
    out = io.BytesIO()
    Image.new(mode, size, (200, 20, 20) if mode == "RGB" else 0).save(out, fmt)
    return out.getvalue()


async def chunks(data: bytes) -> AsyncIterator[bytes]:
    yield data


class FakeViews:
    def __init__(self, available: bool = True, fail: str | None = None) -> None:
        self.available, self.fail, self.calls = available, fail, 0

    async def make(self, original: bytes) -> PreparedView | str:
        self.calls += 1
        return self.fail if self.fail else PreparedView(b"copy:" + original[:8], "image/jpeg", 20, 15)


def run(coro: Coroutine[Any, Any, None]) -> None:
    asyncio.run(coro)


async def conversation(data: Data) -> TrustedScope:
    cid = await data.create_conversation(ALICE, title="t", activations=(InitialActivation(FAKE, WorkspaceId("ws")),))
    return TrustedScope(ALICE, cid)


async def call(data: Data, scope: TrustedScope) -> CallRef:
    run_id = await data.prepare_run(scope, RunPreparation(b"{}", "application/json", RawRepresentation.ADAPTER_SERIALIZED,
                                                          (), PromptBasis(0, 0), "p", "m"))
    await data.mark_dispatch(scope, run_id)
    await data.record_raw_outcome(scope, run_id, RawReceived(b"{}", "application/json"))
    await data.project_model_output(scope, run_id, AssistantToolCalls((ToolCall(CallId("c0"), 0, ToolId("x/y"), {}),)))
    return CallRef(run_id, CallId("c0"))


# --- sniffing (§3) -----------------------------------------------------------------------


@pytest.mark.parametrize("fmt, mode, media", [("PNG", "RGB", "image/png"), ("JPEG", "RGB", "image/jpeg"),
                                              ("GIF", "P", "image/gif"), ("WEBP", "RGB", "image/webp")])
def test_the_four_formats_are_sniffed_with_their_size(fmt: str, mode: str, media: str) -> None:
    found = sniff_image(picture(fmt, (321, 123), mode))
    assert found is not None and (found.media_type, found.width, found.height) == (media, 321, 123)


@pytest.mark.parametrize("data", [b"<svg xmlns='http://www.w3.org/2000/svg'/>", b"<html>png</html>",
                                  b"\x89PNG\r\n\x1a\n", b"GIF89a\x00\x00\x00\x00", b"\xff\xd8\xff", b"", b"BM...."])
def test_anything_else_is_not_an_image(data: bytes) -> None:
    assert sniff_image(data) is None


def test_a_jpeg_after_large_exif_is_still_sniffed() -> None:
    out = io.BytesIO()
    image = Image.new("RGB", (64, 48))
    exif = image.getexif()
    exif[0x010E] = "x" * 60_000  # ImageDescription: a large APP1 before the frame header
    image.save(out, "JPEG", exif=exif)
    found = sniff_image(out.getvalue())
    assert found is not None and (found.width, found.height) == (64, 48)


# --- copies and status (§3.1, §4.1) --------------------------------------------------------


@pytest.mark.parametrize("data, views, status", [
    (b"plain text", FakeViews(), ImageStatus.NOT_IMAGE),
    (None, None, ImageStatus.NO_SANDBOX),
    (None, FakeViews(available=False), ImageStatus.NO_SANDBOX),
    (None, FakeViews(fail="DecompressionBombError"), ImageStatus.UNPREPARED),
    (None, FakeViews(), ImageStatus.READY),
])
def test_prepare_image_says_what_became_of_it(data: bytes | None, views: FakeViews | None,
                                              status: ImageStatus) -> None:
    async def scenario() -> None:
        store = data_for(new_store())
        scope = await conversation(store)
        blob = await store.blobs.put(ALICE, chunks(data if data is not None else picture()), 1 << 20)
        setup = await store.blobs.prepare_image(ALICE, blob, views)
        record = await store.stage_upload(scope, blob, "p.png", "image/png", setup)
        assert record.image_status is status
        if status is ImageStatus.READY:
            assert record.view is not None and record.view.media_type == "image/jpeg"
            assert (record.image_type, record.image_width, record.image_height) == ("image/png", 40, 30)
            assert view_state(record) is ViewState.READY
            with store.blobs.open(record.view.blob) as handle:
                assert handle.read().startswith(b"copy:")
        else:
            assert record.view is None and view_state(record) is ViewState(status.value)

    run(scenario())


def test_too_many_pixels_are_never_decoded() -> None:
    async def scenario() -> None:
        store = data_for(new_store())
        scope = await conversation(store)
        huge = bytearray(picture("PNG", (10, 10)))
        huge[16:24] = (10_000).to_bytes(4, "big") + (6_000).to_bytes(4, "big")  # the header claims 60M pixels
        blob = await store.blobs.put(ALICE, chunks(bytes(huge)), 1 << 20)
        views = FakeViews()
        setup = await store.blobs.prepare_image(ALICE, blob, views)
        assert setup.status is ImageStatus.TOO_LARGE and views.calls == 0
        record = await store.stage_upload(scope, blob, "h.png", "image/png", setup)
        assert view_state(record) is ViewState.TOO_LARGE

    run(scenario())


def test_the_copy_and_the_original_expire_apart() -> None:
    """Review B2: a copy expired is VIEW_EXPIRED, the original may still be downloaded."""
    async def scenario() -> None:
        store = data_for(new_store())
        scope = await conversation(store)
        blob = await store.blobs.put(ALICE, chunks(picture()), 1 << 20)
        setup = await store.blobs.prepare_image(ALICE, blob, FakeViews())
        record = await store.stage_upload(scope, blob, "p.png", "image/png", setup)
        await store.append_event(scope, UserMessage((AttachmentRef(record.file_id, 1),)))  # sent: may expire
        assert record.view is not None
        assert store.blobs.rows.expire_blobs([record.view.blob.key], datetime.now(UTC)) == [record.view.blob.key]
        again, = await store.read_file_records(scope, [(record.file_id, 1)])
        assert view_state(again) is ViewState.VIEW_EXPIRED and not again.expired  # the copy only
        assert again.view is not None and again.view.expired

    run(scenario())


def test_a_staged_uploads_copy_never_expires_and_a_purge_releases_it() -> None:
    async def scenario() -> None:
        store = data_for(new_store())
        store.blobs.configure(max_bytes=10_000, keep_days=1)
        scope = await conversation(store)
        blob = await store.blobs.put(ALICE, chunks(picture()), 1 << 20)
        setup = await store.blobs.prepare_image(ALICE, blob, FakeViews())
        record = await store.stage_upload(scope, blob, "p.png", "image/png", setup)
        assert record.view is not None
        view = record.view.blob
        row = store.blobs.rows.blob_row(view.key)
        assert row is not None
        store.blobs.rows.touch_blob(BlobRow(row.key, row.owner, row.sha256, row.size,
                                            datetime.now(UTC) - timedelta(days=3)))
        await store.sweep_blobs(datetime.now(UTC))
        assert store.blobs.rows.blob_row(view.key).expired_at is None  # type: ignore[union-attr]  # staged: kept
        await store.trash_conversation(scope)
        await store.begin_purge(scope)
        await store.purge_conversation(scope)
        assert store.blobs.rows.blob_row(view.key) is None  # released with its file

    run(scenario())


# --- Tool images (§4.2) --------------------------------------------------------------------


def test_a_tool_image_is_attached_by_its_own_calls_result() -> None:
    async def scenario() -> None:
        store = data_for(new_store())
        scope = await conversation(store)
        ref = await call(store, scope)
        blob = await store.blobs.put(ALICE, chunks(picture()), 1 << 20)
        setup = await store.blobs.prepare_image(ALICE, blob, None)
        record = await store.stage_tool_file(scope, ref, blob, "image-1.png", "image/png", setup)
        assert (record.origin, record.call, record.attached) == (FileOrigin.TOOL, ref, False)
        result = ModelToolResult((*text_result("chart").blocks, AttachmentRef(record.file_id, 1)))
        await store.commit_tool_outcome(scope, ref, ToolCompletion(ToolExecutionStatus.SUCCEEDED, result))
        again, = await store.read_file_records(scope, [(record.file_id, 1)])
        assert again.attached

    run(scenario())


@pytest.mark.parametrize("which", ["other call", "upload", "unknown"])
def test_a_result_may_attach_only_its_own_tool_images(which: str) -> None:
    async def scenario() -> None:
        store = data_for(new_store())
        scope = await conversation(store)
        ref = await call(store, scope)
        other = await call(store, scope)
        blob = await store.blobs.put(ALICE, chunks(picture()), 1 << 20)
        setup = await store.blobs.prepare_image(ALICE, blob, None)
        if which == "other call":
            file_id = (await store.stage_tool_file(scope, other, blob, "i.png", "image/png", setup)).file_id
        elif which == "upload":
            file_id = (await store.stage_upload(scope, blob, "i.png", "image/png", setup)).file_id
        else:
            from himari.common import FileId
            file_id = FileId("file_404")
        result = ModelToolResult((AttachmentRef(file_id, 1),))
        with pytest.raises(DataError) as refused:
            await store.commit_tool_outcome(scope, ref, ToolCompletion(ToolExecutionStatus.SUCCEEDED, result))
        assert refused.value.code is DataErrorCode.INVALID_ARGUMENT

    run(scenario())


def test_a_result_may_not_attach_its_image_twice() -> None:
    """Review N8: the same on both backends."""
    async def scenario() -> None:
        store = data_for(new_store())
        scope = await conversation(store)
        ref = await call(store, scope)
        blob = await store.blobs.put(ALICE, chunks(picture()), 1 << 20)
        record = await store.stage_tool_file(scope, ref, blob, "i.png", "image/png",
                                             await store.blobs.prepare_image(ALICE, blob, None))
        twice = ModelToolResult((AttachmentRef(record.file_id, 1), AttachmentRef(record.file_id, 1)))
        with pytest.raises(DataError) as refused:
            await store.commit_tool_outcome(scope, ref, ToolCompletion(ToolExecutionStatus.SUCCEEDED, twice))
        assert refused.value.code is DataErrorCode.INVALID_ARGUMENT

    run(scenario())


def test_an_image_of_another_conversation_cannot_be_attached() -> None:
    async def scenario() -> None:
        store = data_for(new_store())
        scope, elsewhere = await conversation(store), await conversation(store)
        ref, theirs = await call(store, scope), await call(store, elsewhere)
        blob = await store.blobs.put(ALICE, chunks(picture()), 1 << 20)
        record = await store.stage_tool_file(elsewhere, theirs, blob, "i.png", "image/png",
                                             await store.blobs.prepare_image(ALICE, blob, None))
        result = ModelToolResult((AttachmentRef(record.file_id, 1),))
        with pytest.raises(DataError) as refused:
            await store.commit_tool_outcome(scope, ref, ToolCompletion(ToolExecutionStatus.SUCCEEDED, result))
        assert refused.value.code is DataErrorCode.INVALID_ARGUMENT

    run(scenario())


def test_tool_images_are_neither_presented_files_nor_waiting_uploads() -> None:
    """Review C4, N9: a call's images are not files it gave the user, and do not count as staged uploads."""
    async def scenario() -> None:
        store = data_for(new_store())
        scope = await conversation(store)
        ref = await call(store, scope)
        blob = await store.blobs.put(ALICE, chunks(picture()), 1 << 20)
        record = await store.stage_tool_file(scope, ref, blob, "i.png", "image/png",
                                             await store.blobs.prepare_image(ALICE, blob, None))
        assert await store.count_staged(scope) == 0
        result = ModelToolResult((AttachmentRef(record.file_id, 1),))
        await store.commit_tool_outcome(scope, ref, ToolCompletion(ToolExecutionStatus.SUCCEEDED, result))
        assert await store.read_call_files(scope, [ref]) == {}

    run(scenario())


def test_a_copy_gone_from_disk_is_expired_when_read() -> None:
    """Review C2: the next Run sends a line instead of failing again."""
    async def scenario() -> None:
        store = data_for(new_store())
        scope = await conversation(store)
        blob = await store.blobs.put(ALICE, chunks(picture()), 1 << 20)
        record = await store.stage_upload(scope, blob, "p.png", "image/png",
                                          await store.blobs.prepare_image(ALICE, blob, FakeViews()))
        await store.append_event(scope, UserMessage((AttachmentRef(record.file_id, 1),)))
        assert record.view is not None
        store.blobs.files.remove(record.view.blob.key)
        with pytest.raises(DataError) as missing:
            store.blobs.read_bytes(record.view.blob.key)
        assert missing.value.code is DataErrorCode.BLOB_MISSING
        again, = await store.read_file_records(scope, [(record.file_id, 1)])
        assert view_state(again) is ViewState.VIEW_EXPIRED and not again.expired

    run(scenario())


def test_an_original_over_the_source_limit_gets_no_copy() -> None:
    """Review C1: whatever the file limit, a copy is made only of an original up to 32 MiB."""
    from himari.data.api import MAX_IMAGE_SOURCE_BYTES

    async def scenario() -> None:
        store = data_for(new_store())
        blob = await store.blobs.put(ALICE, chunks(picture() + bytes(MAX_IMAGE_SOURCE_BYTES)), 1 << 26)
        views = FakeViews()
        setup = await store.blobs.prepare_image(ALICE, blob, views)
        assert setup.status is ImageStatus.TOO_LARGE and views.calls == 0

    run(scenario())


def test_a_copy_that_cannot_be_opened_just_now_is_not_expired() -> None:
    """Re-review C1: out of descriptors is not "gone"; the copy stays."""
    import errno

    async def scenario() -> None:
        store = data_for(new_store())
        scope = await conversation(store)
        blob = await store.blobs.put(ALICE, chunks(picture()), 1 << 20)
        record = await store.stage_upload(scope, blob, "p.png", "image/png",
                                          await store.blobs.prepare_image(ALICE, blob, FakeViews()))
        assert record.view is not None

        def busy(key: str) -> Any:
            raise OSError(errno.EMFILE, "Too many open files")

        files = store.blobs.files
        opened, files.open = files.open, busy  # type: ignore[method-assign]
        with pytest.raises(DataError):
            store.blobs.read_bytes(record.view.blob.key)
        files.open = opened  # type: ignore[method-assign]
        again, = await store.read_file_records(scope, [(record.file_id, 1)])
        assert view_state(again) is ViewState.READY
        assert store.blobs.read_bytes(record.view.blob.key).startswith(b"copy:")

    run(scenario())
