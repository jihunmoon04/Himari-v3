"""The file store (V3-P66 §4.5, §8): bytes outside every Workspace.

``FileStore`` implements ``BlobStore`` over two parts a Data backend gives
it: where the bytes are (``BlobFiles``: a private directory, or memory for
the in-memory backend) and the ``blobs`` rows (``BlobRows``). Every step
that installs, expires or removes a blob runs under one lock, so a removal
that checked "unreferenced and old" can never race a new put of the same
content (P0: one server process, V3-045).

Rules:
- A put dedupes only when the row is alive *and* the file is there with the
  right size; anything else is absent and the file is (re)installed (review B1).
- Room is made by expiring the least recently put blobs, but only blobs not
  put within ``BLOB_GRACE`` and not referenced by a staged upload; when those
  cannot cover the shortfall nothing is expired (review B2). Choosing them,
  expiring them and registering the new blob is one transaction
  (``BlobRows.reserve``), so the store never ends up over its size (owner
  review B2). It runs only once the new file is durable at its final name
  (owner re-review B3): a process that dies in between leaves a file without
  a row, which the sweep removes — never an older blob expired for nothing.
- Expiry commits ``expired_at`` before the file goes; removing an
  unreferenced blob deletes its row first. A crash in between leaves a file
  the next sweep removes.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import re
import secrets
import stat
import time
from collections.abc import AsyncIterator, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import BinaryIO, Protocol

from himari.common import PrincipalId
from himari.data.api import (
    BLOB_GRACE,
    IMAGE_HEAD_BYTES,
    MAX_IMAGE_PIXELS,
    NO_IMAGE,
    DEFAULT_KEEP_DAYS,
    DEFAULT_STORE_MAX_BYTES,
    DataError,
    DataErrorCode,
    ImageSetup,
    ImageStatus,
    ImageView,
    PreparedView,
    StoredBlob,
    ViewMaker,
    blob_key,
    sniff_image,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BlobRow:
    key: str
    owner: str
    sha256: str
    size: int
    last_used_at: datetime
    expired_at: datetime | None = None


class BlobRows(Protocol):
    """The ``blobs`` rows of one backend. Each call is its own transaction."""

    def blob_row(self, key: str) -> BlobRow | None: ...

    def live_total(self) -> int: ...

    def expirable(self, used_before: datetime) -> Sequence[BlobRow]:
        """Alive rows put before ``used_before`` that no staged upload refers
        to, least recently put first."""
        ...

    def expire_blobs(self, keys: Sequence[str], now: datetime) -> Sequence[str]:
        """Expire those still alive and not referenced by a staged upload; the keys expired."""
        ...

    def touch_blob(self, row: BlobRow) -> None:
        """Insert or revive: alive, ``last_used_at`` from ``row``."""
        ...

    def reserve(self, row: BlobRow, max_bytes: int, used_before: datetime) -> Sequence[str]:
        """In one transaction: room for ``row`` within ``max_bytes`` — the
        least recently put alive blobs, put before ``used_before`` and not
        referenced by a staged upload, expired just enough — and ``row``
        registered alive. STORE_FULL with nothing changed if they cannot make
        the room. The keys it expired."""
        ...

    def drop_unreferenced(self, key: str, used_before: datetime) -> bool:
        """Delete the row if no file record refers to it and it is expired or
        was put before ``used_before``; whether it was deleted."""
        ...

    def unreferenced_keys(self, used_before: datetime) -> Sequence[str]: ...

    def expired_keys(self) -> Sequence[str]: ...

    def known_keys(self) -> frozenset[str]: ...


class Staging(Protocol):
    size: int

    def write(self, chunk: bytes) -> None: ...

    def finish(self) -> str:
        """Flush to stable storage; the content's SHA-256 (hex)."""
        ...

    def discard(self) -> None: ...


class BlobFiles(Protocol):
    def stage(self) -> Staging: ...

    def install(self, staging: Staging, key: str) -> None:
        """Make the staged bytes the blob ``key``, replacing whatever is there."""
        ...

    def inspect(self, key: str) -> int | None:
        """The size of a sound blob file (regular, private), or None."""
        ...

    def open(self, key: str) -> BinaryIO: ...

    def remove(self, key: str) -> None:
        """Idempotent."""
        ...

    def listing(self) -> Iterable[tuple[str, float]]:
        """Blob keys on disk with their modification times (epoch seconds)."""
        ...

    def remove_stale_staging(self, before: float) -> None:
        """Partial uploads not written to since ``before`` (review C14)."""
        ...


# --- bytes in a private directory -------------------------------------------------

_DIR_MODE = 0o700
_KEY = re.compile(r"^[0-9a-f]{64}$")


def _private_dir(path: str) -> None:
    try:
        os.mkdir(path, _DIR_MODE)
        _fsync_dir(os.path.dirname(path))  # the new entry survives power loss (owner review N1)
    except FileExistsError:
        pass
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        # A broken setup, not a lost upload (PR #60 review C6): an internal error.
        raise RuntimeError(f"{path} is not a private directory")
    if stat.S_IMODE(info.st_mode) != _DIR_MODE:
        os.chmod(path, _DIR_MODE)


def _fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class _FileStaging:
    def __init__(self, directory: str) -> None:
        self.path = os.path.join(directory, f"{secrets.token_hex(16)}.partial")
        self._fd: int | None = os.open(
            self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        self._hash = hashlib.sha256()
        self.size = 0

    def write(self, chunk: bytes) -> None:
        assert self._fd is not None
        view = memoryview(chunk)
        while view:
            view = view[os.write(self._fd, view):]
        self._hash.update(chunk)
        self.size += len(chunk)

    def finish(self) -> str:
        assert self._fd is not None
        os.fsync(self._fd)
        os.close(self._fd)
        self._fd = None
        return self._hash.hexdigest()

    def discard(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass


class DirectoryBlobFiles:
    """``<root>/blobs/<key[:2]>/<key>`` and ``<root>/staging/`` (0700 / 0600).
    ``root`` is inside the data root, so no Sandbox can see it."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = os.path.abspath(os.fspath(root))
        self._blobs = os.path.join(self.root, "blobs")
        self._staging = os.path.join(self.root, "staging")

    def _ready(self) -> None:
        for path in (self.root, self._blobs, self._staging):
            _private_dir(path)

    def _path(self, key: str) -> str:
        if not _KEY.match(key):
            raise ValueError("not a blob key")
        return os.path.join(self._blobs, key[:2], key)

    def stage(self) -> _FileStaging:
        self._ready()
        return _FileStaging(self._staging)

    def install(self, staging: Staging, key: str) -> None:
        assert isinstance(staging, _FileStaging)
        target = self._path(key)
        folder = os.path.dirname(target)
        _private_dir(folder)
        try:
            os.unlink(target)  # a leftover of a crash, or a wrong file (review B1)
        except FileNotFoundError:
            pass
        try:
            os.link(staging.path, target)  # never replaces: the lock keeps others out
        except FileNotFoundError:  # the sweep took it: idle for the whole grace (PR #59 review C6)
            raise DataError(DataErrorCode.BLOB_MISSING, "the upload was interrupted") from None
        os.unlink(staging.path)
        _fsync_dir(folder)

    def inspect(self, key: str) -> int | None:
        try:
            info = os.lstat(self._path(key))
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
            return None
        return info.st_size

    def open(self, key: str) -> BinaryIO:
        fd = os.open(self._path(key), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        return os.fdopen(fd, "rb")

    def remove(self, key: str) -> None:
        try:
            os.unlink(self._path(key))
        except FileNotFoundError:
            pass

    def listing(self) -> Iterable[tuple[str, float]]:
        if not os.path.isdir(self._blobs):
            return []
        found: list[tuple[str, float]] = []
        for folder in os.scandir(self._blobs):
            if not folder.is_dir(follow_symlinks=False):
                continue
            for entry in os.scandir(folder.path):
                if _KEY.match(entry.name):  # anything else is not ours to judge (PR #59 review C2)
                    found.append((entry.name, entry.stat(follow_symlinks=False).st_mtime))
        return found

    def remove_stale_staging(self, before: float) -> None:
        if not os.path.isdir(self._staging):
            return
        for entry in os.scandir(self._staging):
            if entry.is_file(follow_symlinks=False) and entry.stat(follow_symlinks=False).st_mtime < before:
                try:
                    os.unlink(entry.path)
                except FileNotFoundError:
                    pass


# --- bytes in memory (the in-memory backend) ---------------------------------------


class _MemoryStaging:
    def __init__(self, owner: MemoryBlobFiles) -> None:
        self._owner = owner
        self._buffer = bytearray()
        self._hash = hashlib.sha256()
        self.size = 0
        self.written_at = time.time()
        owner.staging.add(self)

    def write(self, chunk: bytes) -> None:
        self._buffer += chunk
        self._hash.update(chunk)
        self.size += len(chunk)
        self.written_at = time.time()

    def finish(self) -> str:
        return self._hash.hexdigest()

    def discard(self) -> None:
        self._owner.staging.discard(self)

    def take(self) -> bytes:
        self._owner.staging.discard(self)
        return bytes(self._buffer)


class MemoryBlobFiles:
    def __init__(self) -> None:
        self.blobs: dict[str, tuple[bytes, float]] = {}
        self.staging: set[_MemoryStaging] = set()

    def stage(self) -> _MemoryStaging:
        return _MemoryStaging(self)

    def install(self, staging: Staging, key: str) -> None:
        assert isinstance(staging, _MemoryStaging)
        self.blobs[key] = (staging.take(), time.time())

    def inspect(self, key: str) -> int | None:
        entry = self.blobs.get(key)
        return None if entry is None else len(entry[0])

    def open(self, key: str) -> BinaryIO:
        entry = self.blobs.get(key)
        if entry is None:
            raise FileNotFoundError(key)
        return io.BytesIO(entry[0])

    def remove(self, key: str) -> None:
        self.blobs.pop(key, None)

    def listing(self) -> Iterable[tuple[str, float]]:
        return [(key, at) for key, (_, at) in self.blobs.items()]

    def remove_stale_staging(self, before: float) -> None:
        for staging in [s for s in self.staging if s.written_at < before]:
            self.staging.discard(staging)


# --- the store ---------------------------------------------------------------------------


def _sizes(found: ImageSetup) -> tuple[str | None, int | None, int | None]:
    return found.image_type, found.width, found.height


async def _once(data: bytes) -> AsyncIterator[bytes]:
    yield data


async def _finish[T](function: Callable[..., T], *args: object) -> T:
    """Run in a worker thread and wait until it is done, even if cancelled
    meanwhile; a cancellation that arrived is re-raised afterwards."""
    work = asyncio.ensure_future(asyncio.to_thread(function, *args))
    interrupted = False
    while not work.done():
        try:
            await asyncio.wait([work])
        except asyncio.CancelledError:
            interrupted = True
    if interrupted:
        if not work.cancelled() and work.exception() is not None:  # retrieved, so not logged as lost (N2)
            log.warning("work finished with %r after its caller was cancelled", work.exception())
        raise asyncio.CancelledError
    return work.result()


class FileStore:
    """``BlobStore`` plus the removals the Data backend and the cleanup ask for."""

    def __init__(self, files: BlobFiles, rows: BlobRows, *, max_bytes: int = DEFAULT_STORE_MAX_BYTES,
                 keep_days: int = DEFAULT_KEEP_DAYS, clock: Callable[[], datetime] | None = None) -> None:
        self.files = files
        self.rows = rows
        self.max_bytes = max_bytes
        self.keep_days = keep_days
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = asyncio.Lock()

    def configure(self, *, max_bytes: int, keep_days: int) -> None:
        if max_bytes < 1 or not 1 <= keep_days <= 365:
            raise ValueError("the file store needs a positive size and 1..365 keep days")
        self.max_bytes, self.keep_days = max_bytes, keep_days

    async def put(self, principal: PrincipalId, chunks: AsyncIterator[bytes], max_bytes: int) -> StoredBlob:
        staging = self.files.stage()
        try:
            async for chunk in chunks:
                if staging.size + len(chunk) > max_bytes:
                    raise DataError(DataErrorCode.TOO_LARGE, f"over {max_bytes} bytes")
                staging.write(chunk)
            sha256 = await _finish(staging.finish)  # discard must not close the fd under it (re-review N1)
            size = staging.size
            key = blob_key(principal, sha256)
            async with self._lock:
                now = self._clock()
                row = self.rows.blob_row(key)
                alive = row is not None and row.expired_at is None
                fresh = BlobRow(key, principal, sha256, size, now)
                if alive and self.files.inspect(key) == size:
                    self.rows.touch_blob(fresh)
                elif alive:  # its file is gone (e.g. a restored database): put back, counted already
                    self.files.install(staging, key)
                    self.rows.touch_blob(fresh)
                else:
                    # The file first, then its row (owner re-review B3). On the loop,
                    # under the lock: a link, an unlink and an fsync of a small
                    # directory; the bytes are durable already.
                    self.files.install(staging, key)
                    try:
                        expired = self.rows.reserve(fresh, self.max_bytes, now - BLOB_GRACE)
                    except BaseException:
                        self.files.remove(key)  # no live row names it: ours alone (STORE_FULL, review C9)
                        raise
                    self._remove(expired)
            return StoredBlob(key, sha256, size)
        finally:
            staging.discard()  # a no-op once installed: the staged name was moved

    def _remove(self, keys: Sequence[str]) -> None:
        for key in keys:
            try:
                self.files.remove(key)
            except OSError:
                log.exception("could not remove expired blob %s; the next sweep tries again", key)

    def _expire(self, rows: Sequence[BlobRow], now: datetime) -> None:
        if not rows:
            return
        self._remove(self.rows.expire_blobs([r.key for r in rows], now))  # committed before the bytes go

    def open(self, blob: StoredBlob) -> BinaryIO:
        row = self.rows.blob_row(blob.key)
        if row is None:
            raise DataError(DataErrorCode.BLOB_MISSING, blob.key)
        if row.expired_at is not None:
            raise DataError(DataErrorCode.BLOB_EXPIRED, blob.key)
        if self.files.inspect(blob.key) != blob.size:  # written once: a sound file keeps its size
            raise DataError(DataErrorCode.BLOB_MISSING, blob.key)
        try:
            return self.files.open(blob.key)
        except OSError:
            raise DataError(DataErrorCode.BLOB_MISSING, blob.key) from None

    def read_bytes(self, key: str) -> bytes:
        row = self.rows.blob_row(key)
        if row is None:
            raise DataError(DataErrorCode.BLOB_MISSING, key)
        with self.open(StoredBlob(key, row.sha256, row.size)) as handle:
            return handle.read()

    async def prepare_image(self, principal: PrincipalId, blob: StoredBlob, views: ViewMaker | None) -> ImageSetup:
        """V3-P67 §3–§3.1. The original is read here only to be handed to the
        image sandbox; nothing in this process decodes it."""
        try:
            with self.open(blob) as handle:
                sniffed = sniff_image(handle.read(IMAGE_HEAD_BYTES))
                if sniffed is None:
                    return NO_IMAGE
                found = ImageSetup(ImageStatus.TOO_LARGE, sniffed.media_type, sniffed.width, sniffed.height)
                if sniffed.width * sniffed.height > MAX_IMAGE_PIXELS:
                    return found
                if views is None or not views.available:
                    return ImageSetup(ImageStatus.NO_SANDBOX, *_sizes(found))
                handle.seek(0)
                original = handle.read()
        except DataError:
            return NO_IMAGE
        made = await views.make(original)
        if not isinstance(made, PreparedView):
            log.info("no model copy of an image: %s", made)
            return ImageSetup(ImageStatus.UNPREPARED, *_sizes(found))
        try:
            stored = await self.put(principal, _once(made.data), len(made.data))
        except DataError as error:  # e.g. STORE_FULL: the image is kept, without a copy
            log.info("no room for a model copy of an image: %s", error.code.value)
            return ImageSetup(ImageStatus.UNPREPARED, *_sizes(found))
        return ImageSetup(ImageStatus.READY, *_sizes(found),
                          view=ImageView(stored, made.media_type, made.width, made.height))

    async def release(self, keys: Iterable[str]) -> None:
        """After a purge commits (§8.1): blobs nothing refers to any more go,
        unless put within the grace. Logs failures, never raises (review C13)."""
        try:
            async with self._lock:
                cutoff = self._clock() - BLOB_GRACE
                for key in set(keys):
                    if self.rows.drop_unreferenced(key, cutoff):
                        self.files.remove(key)
        except Exception:  # noqa: BLE001 — the purge stands; the sweep removes what is left
            log.exception("could not remove the blobs of a purged conversation")

    async def sweep(self, now: datetime | None = None) -> None:
        """The hourly cleanup (§8.2, §8.5). Each part logs its own failure."""
        async with self._lock:
            now = self._clock() if now is None else now
            grace = now - BLOB_GRACE
            steps: list[tuple[str, Callable[[], None]]] = [
                ("expire blobs past the keep days",
                 lambda: self._expire(self.rows.expirable(now - timedelta(days=self.keep_days)), now)),
                ("remove unreferenced blobs", lambda: self._drop_unreferenced(grace)),
                ("remove the files of expired blobs", self._remove_expired_files),
                ("remove files without a row", lambda: self._remove_strays(grace)),
                ("remove stale partial uploads", lambda: self.files.remove_stale_staging(grace.timestamp())),
            ]
            for what, step in steps:
                try:
                    step()
                except Exception:  # noqa: BLE001 — the other steps still run
                    log.exception("file store sweep: could not %s", what)

    def _drop_unreferenced(self, used_before: datetime) -> None:
        for key in self.rows.unreferenced_keys(used_before):
            if self.rows.drop_unreferenced(key, used_before):
                self.files.remove(key)

    def _remove_expired_files(self) -> None:
        """Files left by an expiry cut short; looked up on disk, so an expired
        blob costs nothing once its file is gone (PR #59 review C8)."""
        expired = set(self.rows.expired_keys())
        for key, _ in self.files.listing():
            if key in expired:
                self.files.remove(key)

    def _remove_strays(self, used_before: datetime) -> None:
        known = self.rows.known_keys()
        for key, modified in self.files.listing():
            if key not in known and modified < used_before.timestamp():
                self.files.remove(key)
