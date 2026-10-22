"""Child process for tests/data/test_files.py: fills a small file store so
that the next put must expire an older blob, then dies by SIGKILL at a chosen
point of that put. Not a test module.

Usage: python blob_crash_child.py <db path> <point: before_install | before_reserve | none>
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

from testkit import SCOPE

from himari.common import AttachmentRef, TextBlock
from himari.data.api import UserMessage
from himari.data.blobs import BlobRow
from himari.data.sqlite import SqliteData, SqliteStore


async def chunks(data: bytes) -> AsyncIterator[bytes]:
    yield data


def die(*args: object, **kwargs: object) -> None:
    os.kill(os.getpid(), signal.SIGKILL)


async def main(path: str, point: str) -> None:
    store = SqliteStore(path)
    store.blobs.configure(max_bytes=20, keep_days=30)
    data = SqliteData(store)
    store.create_conversation(SCOPE)
    old = await store.blobs.put(SCOPE.principal_id, chunks(b"o" * 15), 100)
    record = await data.stage_upload(SCOPE, old, "old.bin", "application/octet-stream")
    await data.append_event(SCOPE, UserMessage((TextBlock("keep"), AttachmentRef(record.file_id, 1))))
    store.blobs.rows.touch_blob(BlobRow(old.key, SCOPE.principal_id, old.sha256, old.size,
                                        datetime.now(UTC) - timedelta(hours=5)))  # may expire now
    if point == "before_install":
        store.blobs.files.install = die  # type: ignore[method-assign]
    elif point == "before_reserve":
        store.blobs.rows.reserve = die  # type: ignore[method-assign, assignment]
    await store.blobs.put(SCOPE.principal_id, chunks(b"n" * 10), 100)
    print(old.key)
    store.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
