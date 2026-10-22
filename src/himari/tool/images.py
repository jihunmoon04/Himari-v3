"""Keeping the images Tools return (V3-P67 §4.2) — the Dispatcher's
``ResultImageStore`` over Data's file store and file records.

An image is kept only if its bytes are one of the four formats (never by
its declared type); kept, it gets a model copy (made in the image sandbox,
or a status saying why not) and a staged record of this call, which the
call's result attaches when it is committed (T7).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from himari.common import AttachmentRef, CallRef, TrustedScope
from himari.data.api import (
    BlobStore,
    DataError,
    FileCatalog,
    IMAGE_HEAD_BYTES,
    ViewMaker,
    image_extension,
    sniff_image,
)
from himari.tool.api import ResultImage


async def _once(data: bytes) -> AsyncIterator[bytes]:
    yield data


class StoredResultImages:
    def __init__(self, blobs: BlobStore, files: FileCatalog, views: ViewMaker | None, max_bytes: int) -> None:
        self._blobs, self._files, self._views, self._max_bytes = blobs, files, views, max_bytes

    async def keep(self, scope: TrustedScope, call: CallRef, image: ResultImage, number: int) -> AttachmentRef | str:
        sniffed = sniff_image(image.data[:IMAGE_HEAD_BYTES])
        if sniffed is None:
            return "[an image of an unsupported type was returned]"
        if len(image.data) > self._max_bytes:
            return "[an image too large to keep was returned]"
        try:
            blob = await self._blobs.put(scope.principal_id, _once(image.data), self._max_bytes)
            setup = await self._blobs.prepare_image(scope.principal_id, blob, self._views)
            record = await self._files.stage_tool_file(
                scope, call, blob, f"image-{number}.{image_extension(sniffed.media_type)}", sniffed.media_type, setup)
        except DataError:  # e.g. the file store is full
            return "[an image was returned but could not be kept]"
        return AttachmentRef(record.file_id, record.version)
