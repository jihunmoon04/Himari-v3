"""The web's file explorer (V3-P75 §5): the owner's view of this server's
files, through the machine runner.

HIMARI never opens the user's files itself (V3-P74 §4.2): every listing,
read and download is the runner's, with its hidden list and checks. Paths
are absolute or start with ``~`` (the user's home); the runner resolves
them and answers with real paths.

Text is returned as text, to be shown as text; images are shown only as
the copy the image sandbox made (V3-P67), never their own bytes; a PDF is
told as one, for the browser's PDF view to read as a download (V3-P77 §3);
anything else is a download (an attachment, never rendered on this origin).

The editor's writes (§7) are the owner's own: anywhere the runner lets the
user write, hidden paths and ``writable_under`` aside, whatever the
conversations' Workspaces are. Each goes to the runner with the target's
folder as its write root. A save or a delete keeps what the file held as a
previous version (``versions.py``), from the copy the runner took right
before; a save is refused when no version could be kept.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import logging
import os
import posixpath
import re
import tempfile
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

from himari.data.api import (
    IMAGE_HEAD_BYTES,
    MAX_IMAGE_PIXELS,
    MAX_IMAGE_SOURCE_BYTES,
    PreparedView,
    RegisteredWorkspace,
    ViewMaker,
    sniff_image,
)
from himari.bootstrap.versions import Version, VersionStore
from himari.common import FileReference
from himari.environment.machine import MachineEnvironment
from himari.runner import api as runner

log = logging.getLogger("himari.explorer")

BROWSE_PAGE = 500
EDIT_MAX_BYTES = 1024 * 1024  # V3-P75 Q3: what the editor takes whole
PREVIEW_BYTES = 256 * 1024  # the start of a larger text file, shown read-only
MAX_PATH_CHARS = 4096
_CACHE_BYTES = 24 * 1024 * 1024  # image copies kept for the explorer
READ_SECONDS = 15.0  # reading an opened file; the runner bounds its own reads alike (PR #92 review B1)
CALL_SECONDS = 20.0  # a runner answer, its own 15 s included
MAX_READS = 4  # file reads under way here, given-up ones included
DELETED_SHOWN = 50  # files deleted from a folder listed at once
EXCERPT_BYTES = 20 * 1024  # a reference's excerpt, as UTF-8 (V3-P76 Q1)
PDF_HEADER = b"%PDF-"  # what makes a file a PDF here, whatever its name (V3-P77 §3.1)
PDF_MAX_BYTES = runner.MAX_COPY_BYTES  # V3-P77 §3.3, Q2: a PDF written to from the web, after its save too
PDF_APPEND_MAX_BYTES = 8 * 1024 * 1024  # the ink one save adds (§5.3)
PDF_COPY_SECONDS = 120.0  # copying a PDF before ink is added to it (a cloud mount may be slow)
INK_VERSION_EVERY = timedelta(minutes=30)  # §6.2: one previous version per PDF in this time, for ink saves

T = TypeVar("T")


class ExplorerRefused(Exception):
    """``category`` is the runner's (``not_found``, ``access``, …) or
    ``unavailable``/``invalid``; ``str()`` is safe to show the user."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


@dataclass(frozen=True)
class Listing:
    path: str  # real
    parent: str | None  # None at /
    entries: tuple[dict[str, Any], ...]  # name, kind, size, modified
    next: str | None  # the cursor of the next page
    workspace: RegisteredWorkspace | None  # when this folder is a registered one


@dataclass(frozen=True)
class Saved:
    path: str
    identity: tuple[int, ...]
    previous_kept: bool  # False: saved, but its previous version could not be kept (§7.4, a rare failure); an ink
    # save within 30 minutes of the last version kept is True: that version holds the PDF before the ink (V3-P77 §6.2)


@dataclass(frozen=True)
class ShownFile:
    path: str
    size: int
    identity: tuple[int, ...]
    modified: int | None
    kind: str  # "text" | "image" | "pdf" | "binary"
    text: str | None = None
    truncated: bool = False  # the text is the file's start only
    editable: bool = False  # whole, text, at most EDIT_MAX_BYTES (part 3 edits it)


class Explorer:
    def __init__(self, machine: MachineEnvironment, views: ViewMaker | None,
                 versions: VersionStore | None = None, agent_versions: VersionStore | None = None,
                 pdf_versions: VersionStore | None = None) -> None:
        self._machine = machine
        self._views = views
        self.versions = versions  # None: the editor cannot save (no place for previous versions)
        # V3-P76 §4.2: what files held before the agent changed them; shown among the previous versions.
        self.agent_versions = agent_versions
        # V3-P77 §6.2: PDFs' previous versions, ink saves and deletes (a budget of their own); None: not written to.
        self.pdf_versions = pdf_versions
        self._copies: OrderedDict[tuple[str, tuple[int, ...]], PreparedView] = OrderedDict()
        self._pool = concurrent.futures.ThreadPoolExecutor(MAX_READS, thread_name_prefix="himari-explorer")
        self._reads = 0

    async def _call(self, op: str, args: dict[str, Any], fds: Sequence[int] = (),
                    seconds: float | None = CALL_SECONDS) -> tuple[dict[str, Any], list[int]]:
        try:
            async with asyncio.timeout(seconds):
                return await self._machine.call(op, args, fds)
        except runner.RunnerError as error:
            raise ExplorerRefused(error.category, str(error)) from None
        except runner.RunnerUnavailable:
            raise ExplorerRefused("unavailable", "The machine runner does not answer; try again later.") from None
        except TimeoutError:
            raise ExplorerRefused(runner.TIMED_OUT, runner.MESSAGES[runner.TIMED_OUT]) from None

    async def _read(self, fd: int, size: int) -> bytes:
        """Up to ``size`` bytes from the start of ``fd``, which this takes over:
        it is closed when the read has ended, never before (PR #92 review N1),
        even if the caller gave up (a slow mount, a cancelled request)."""
        return await self._reading(fd, lambda: _pread(fd, size), READ_SECONDS)

    async def _reading(self, fd: int, read: Callable[[], T], seconds: float) -> T:
        """``read`` (of ``fd``, which this takes over) on the explorer's threads, as ``_read``."""
        if self._reads >= MAX_READS:
            os.close(fd)
            raise ExplorerRefused(runner.TIMED_OUT, "Earlier slow reads are still under way (an unresponsive "
                                                    "mount?); try again later.")
        self._reads += 1

        def reading() -> T:
            try:
                return read()
            finally:
                os.close(fd)

        work = asyncio.get_running_loop().run_in_executor(self._pool, reading)

        def ended(future: asyncio.Future[T]) -> None:
            self._reads -= 1
            if not future.cancelled():
                future.exception()  # retrieved: a given-up read's failure is not reported

        work.add_done_callback(ended)
        try:
            async with asyncio.timeout(seconds):
                return await asyncio.shield(work)
        except TimeoutError:
            raise ExplorerRefused(runner.TIMED_OUT, runner.MESSAGES[runner.TIMED_OUT]) from None

    @staticmethod
    def _path(path: object) -> str:
        if not isinstance(path, str) or not path.strip() or len(path) > MAX_PATH_CHARS or "\0" in path:
            raise ExplorerRefused("invalid", "Give a folder or file path.")
        given = path.strip()
        if not (given.startswith("/") or given == "~" or given.startswith("~/")):
            raise ExplorerRefused("invalid", "Give an absolute path, or one that starts with ~.")
        return given

    async def browse(self, path: object, after: str | None,
                     registered: Sequence[RegisteredWorkspace]) -> Listing:
        result, _ = await self._call(runner.LIST, {"path": self._path(path), "cwd": "/", "limit": BROWSE_PAGE,
                                                   "after": after})
        real = str(result["path"])
        workspace = next((w for w in registered if w.location == real), None)
        return Listing(real, None if real == "/" else posixpath.dirname(real), tuple(result["entries"]),
                       result.get("next"), workspace)

    async def stat(self, path: object) -> dict[str, Any]:
        result, _ = await self._call(runner.STAT, {"path": self._path(path), "cwd": "/"})
        return result

    async def open(self, path: object) -> tuple[str, int, tuple[int, ...], int]:
        """The file's real path, size, identity (V3-P77 §5.3: what a PDF's ink is saved against) and an open
        descriptor (the caller closes it)."""
        result, fds = await self._call(runner.OPEN, {"path": self._path(path), "cwd": "/"})
        return str(result["path"]), int(result["size"]), tuple(int(v) for v in result["identity"]), fds[0]

    async def show(self, path: object) -> ShownFile:
        result, fds = await self._call(runner.OPEN, {"path": self._path(path), "cwd": "/"})
        fd, real, size = fds[0], str(result["path"]), int(result["size"])
        identity = tuple(int(v) for v in result["identity"])
        head = await self._read(fd, min(size, EDIT_MAX_BYTES) + 1)
        modified = identity[3] // 1_000_000_000 if len(identity) == 4 else None
        if sniff_image(head[:IMAGE_HEAD_BYTES]) is not None:
            return ShownFile(real, size, identity, modified, "image")
        if head.startswith(PDF_HEADER):  # before text: a PDF may be all ASCII
            return ShownFile(real, size, identity, modified, "pdf")
        whole = size <= EDIT_MAX_BYTES and len(head) == size
        text = _text(head if whole else head[:PREVIEW_BYTES], whole)
        if text is None:
            return ShownFile(real, size, identity, modified, "binary")
        return ShownFile(real, size, identity, modified, "text", text, truncated=not whole, editable=whole)

    # --- the editor's writes (§7) ---------------------------------------------------------

    async def _place(self, path: object) -> tuple[str, str]:
        """The target's folder as the runner resolves it, and its name."""
        given = self._path(path)
        folder, name = posixpath.split(given.rstrip("/"))
        if name in ("", ".", "..", "~") or "/" in name:
            raise ExplorerRefused("invalid", "Give a file or folder name.")
        found = await self.stat(folder or "/")
        if found.get("kind") != "directory":
            raise ExplorerRefused("not_directory", f"{found.get('path', folder)} is not a folder.")
        return str(found["path"]), name

    async def _write(self, op: str, path: object, args: dict[str, Any],
                     fds: Sequence[int] = ()) -> tuple[str, dict[str, Any], list[int]]:
        folder, name = await self._place(path)
        target = posixpath.join(folder, name)
        result, received = await self._call(op, {"path": target, "cwd": "/", "write_roots": [folder], **args}, fds,
                                            None)  # a write is never given up on: it waits for its outcome
        log.info("web %s %s", op if op != runner.WRITE else "save", target)
        return target, result, received

    def _stores(self) -> tuple[VersionStore, ...]:
        return tuple(store for store in (self.versions, self.agent_versions, self.pdf_versions) if store is not None)

    def _keep(self, path: str, received: list[int], reason: str, store: VersionStore | None = None) -> bool:
        store = store or self.versions
        if not received or store is None:
            for fd in received:
                os.close(fd)
            return False
        try:
            store.keep(path, received[0], reason)
        except OSError:
            log.exception("a previous version of %s could not be kept", path)
            return False
        return True

    async def save(self, path: object, content: bytes, expected: Sequence[int], reason: str = "save") -> Saved:
        """§7.4: replace the file read with identity ``expected``; its
        previous version kept. CHANGED (nothing written) if it changed since."""
        if self.versions is None:
            raise ExplorerRefused("unavailable", "Files cannot be saved on this server.")
        if len(content) > EDIT_MAX_BYTES:
            raise ExplorerRefused("too_large", "Too large to save from here (at most 1 MiB).")
        if len(expected) == 4 and int(expected[2]) > EDIT_MAX_BYTES:  # no previous version of it (review B1)
            raise ExplorerRefused("too_large", "This file is over 1 MiB: it is read-only here, since its previous "
                                               "version could not be kept.")
        if len(expected) != 4 or not self.versions.ready_for(int(expected[2])):
            raise ExplorerRefused("versions_unavailable", "The previous version could not be kept (the data folder "
                                  "is full or not writable); nothing was saved.")
        with tempfile.TemporaryFile() as body:
            body.write(content)
            body.flush()
            body.seek(0)
            target, result, received = await self._write(
                runner.WRITE, path, {"mode": "replace", "expected": [int(v) for v in expected]}, [body.fileno()])
        kept = await asyncio.to_thread(self._keep, str(result.get("path", target)), received, reason)
        return Saved(str(result.get("path", target)), tuple(int(v) for v in result["identity"]), kept)

    async def install(self, path: object, content: bytes) -> str:
        """V3-P76 §4.1: a file put back where its name is free, in one step:
        CHANGED if the name is taken, ``unsupported`` where the file system
        cannot (nothing is written then)."""
        if len(content) > EDIT_MAX_BYTES:
            raise ExplorerRefused("too_large", "Too large to put back from here (at most 1 MiB).")
        with tempfile.TemporaryFile() as body:
            body.write(content)
            body.flush()
            body.seek(0)
            return await self._install(path, body.fileno())

    async def _install(self, path: object, fd: int) -> str:
        """``install`` of what ``fd`` holds (the caller closes it): a PDF's version is put back as it is, never
        read whole (V3-P77 §6.3)."""
        try:
            keeps = await self._machine.keeps()
        except (runner.RunnerUnavailable, runner.RunnerError):
            raise ExplorerRefused("unavailable", "The machine runner does not answer; try again later.") from None
        if not keeps:  # an older runner would write it plainly, over a file made meanwhile
            raise ExplorerRefused(runner.UNSUPPORTED, "The machine runner is older than HIMARI (restart "
                                                      "himari-runner); nothing was put back.")
        try:
            target, result, _ = await self._write(runner.WRITE, path, {"mode": "install"}, [fd])
        except ExplorerRefused as refused:
            if refused.category == runner.UNSUPPORTED:
                raise ExplorerRefused(runner.UNSUPPORTED, "This folder's file system cannot put a file back in "
                                                          "one step; copy its previous version by hand.") from None
            raise
        return str(result.get("path", target))

    # --- PDFs (V3-P77 §5, §6) --------------------------------------------------------------

    async def _copies_large(self, nothing: str) -> None:
        """The runner copies a PDF up to 64 MiB (protocol 3), or the call is refused before it is made."""
        try:
            copies = await self._machine.copies_large()
        except (runner.RunnerUnavailable, runner.RunnerError):
            raise ExplorerRefused("unavailable", "The machine runner does not answer; try again later.") from None
        if not copies:  # an older one would refuse it, or delete it without a copy
            raise ExplorerRefused(runner.UNSUPPORTED, f"The machine runner is older than HIMARI (restart "
                                                      f"himari-runner); {nothing}.")

    async def _is_pdf(self, path: object, expected: Sequence[int]) -> bool:
        """The file read with identity ``expected`` starts as a PDF (§6.3: decided here, never by its name).
        False also when it is another file now: the write that follows is refused then (CHANGED)."""
        result, fds = await self._call(runner.OPEN, {"path": self._path(path), "cwd": "/"})
        if [int(v) for v in result["identity"]] != [int(v) for v in expected]:
            os.close(fds[0])
            return False
        return (await self._read(fds[0], len(PDF_HEADER))).startswith(PDF_HEADER)

    async def append_pdf(self, path: object, increment: bytes, expected: Sequence[int]) -> Saved:
        """§5.3: ink saved into the PDF read with identity ``expected`` — ``increment`` (an incremental update,
        made by the browser) added after its bytes, which are never changed. The whole is at most 64 MiB. Its
        previous version is kept in the PDFs' store, at most one per 30 minutes (§6.2): refused before any write
        when it would be kept and cannot; after the write, a failure to keep it is told (``previous_kept``)."""
        store = self.pdf_versions
        if store is None:
            raise ExplorerRefused("unavailable", "PDFs cannot be saved on this server.")
        if len(expected) != 4:
            raise ExplorerRefused("invalid", "The file's identity, as it was read, is required.")
        if not increment:
            raise ExplorerRefused("invalid", "There is no ink to save.")
        if len(increment) > PDF_APPEND_MAX_BYTES:
            raise ExplorerRefused("too_large", "Too much ink to save at once (over 8 MiB); save more often.")
        size = int(expected[2])
        if size + len(increment) > PDF_MAX_BYTES:
            raise ExplorerRefused("too_large", "Too large to save here: the PDF would be over 64 MiB.")
        if not increment_ok(size, increment):
            raise ExplorerRefused("invalid", "Not saved: the ink is not an update of this PDF.")
        await self._copies_large("nothing was saved")
        result, fds = await self._call(runner.OPEN, {"path": self._path(path), "cwd": "/"})
        real = str(result["path"])
        if [int(v) for v in result["identity"]] != [int(v) for v in expected]:
            os.close(fds[0])
            raise ExplorerRefused(runner.CHANGED, "This PDF changed since it was opened; nothing was saved.")
        latest = await asyncio.to_thread(store.latest, real)
        keep = latest is None or datetime.now(UTC) - latest.saved_at >= INK_VERSION_EVERY
        if keep and not store.ready_for(size):
            os.close(fds[0])
            raise ExplorerRefused("versions_unavailable", "The previous version could not be kept (the data folder "
                                  "is full or not writable); nothing was saved.")
        with tempfile.TemporaryFile() as body:
            out = body.fileno()
            if not await self._reading(fds[0], lambda: _copy_pdf(fds[0], out, size), PDF_COPY_SECONDS):
                raise ExplorerRefused("not_regular", "This file is not a PDF; nothing was saved.")
            body.seek(0, os.SEEK_END)
            body.write(increment)
            body.flush()
            body.seek(0)
            target, written, received = await self._write(
                runner.WRITE, real, {"mode": "replace", "expected": [int(v) for v in expected],
                                     "copy_max": PDF_MAX_BYTES}, [out])
        saved = str(written.get("path", target))
        if keep:
            kept = await asyncio.to_thread(self._keep, saved, received, "ink", store)
        else:
            for fd in received:
                os.close(fd)
            kept = True  # the version kept within the last 30 minutes holds the PDF before this ink
        return Saved(saved, tuple(int(v) for v in written["identity"]), kept)

    async def put_back(self, path: object, version_id: str, expected: Sequence[int]) -> Saved:
        """§6.3: a PDF's previous version written over the file read with identity ``expected``, which is kept
        as a version first (whatever the 30-minute rule). Text files' versions go back through the editor."""
        store = self.pdf_versions
        real = await self.visible(path)
        if store is None or not await asyncio.to_thread(store.has, real, version_id):
            raise ExplorerRefused("not_found", "No such previous version of this PDF.")
        if len(expected) != 4:
            raise ExplorerRefused("invalid", "The file's identity, as it was read, is required.")
        size = int(expected[2])
        if size > PDF_MAX_BYTES:
            raise ExplorerRefused("too_large", "This file is over 64 MiB: its content could not be kept.")
        args: dict[str, Any] = {"mode": "replace", "expected": [int(v) for v in expected]}
        if size > EDIT_MAX_BYTES:
            await self._copies_large("nothing was put back")
            args["copy_max"] = PDF_MAX_BYTES
        if not store.ready_for(size):
            raise ExplorerRefused("versions_unavailable", "The file's content could not be kept (the data folder is "
                                                          "full or not writable); nothing was put back.")
        fd = await asyncio.to_thread(store.open, real, version_id)
        if fd is None:
            raise ExplorerRefused("not_found", "No such previous version of this PDF.")
        try:
            target, written, received = await self._write(runner.WRITE, real, args, [fd])
        finally:
            os.close(fd)
        saved = str(written.get("path", target))
        kept = await asyncio.to_thread(self._keep, saved, received, "restore", store)
        return Saved(saved, tuple(int(v) for v in written["identity"]), kept)

    async def open_version(self, path: object, version_id: str) -> tuple[str, int, int]:
        """A previous version to read (a PDF's, opened to view): the path, its size and an fd the caller closes."""
        real = await self.visible(path)
        for store in self._stores():
            fd = await asyncio.to_thread(store.open, real, version_id)
            if fd is not None:
                return real, os.fstat(fd).st_size, fd
        raise ExplorerRefused("not_found", "No such previous version.")

    async def create_file(self, path: object) -> tuple[str, tuple[int, ...]]:
        """§7.7: a new, empty file (EXISTS if the name is taken)."""
        target, result, _ = await self._write(runner.CREATE_FILE, path, {})
        return str(result.get("path", target)), tuple(int(v) for v in result["identity"])

    async def make_folder(self, path: object) -> str:
        """§7.5: one new folder (EXISTS if the name is taken)."""
        target, result, _ = await self._write(runner.MKDIR, path, {})
        return str(result.get("path", target))

    async def delete(self, path: object, expected: Sequence[int]) -> tuple[str, bool]:
        """§7.4: delete the file read with identity ``expected``; whether its
        content was kept as a previous version (not over 1 MiB)."""
        if len(expected) != 4:
            raise ExplorerRefused("invalid", "The file's identity, as it was read, is required.")
        size = int(expected[2])
        if self.pdf_versions is not None and size <= PDF_MAX_BYTES and await self._is_pdf(path, expected):
            return await self._delete_pdf(path, expected, self.pdf_versions)
        # Up to 1 MiB its content is kept, so a store that cannot keep it refuses the delete (review B2); a
        # larger file goes without, after the editor's own warning (§7.4).
        if int(expected[2]) <= EDIT_MAX_BYTES and (self.versions is None
                                                   or not self.versions.ready_for(int(expected[2]))):
            raise ExplorerRefused("versions_unavailable", "The file's content could not be kept (the data folder is "
                                                          "full or not writable); nothing was deleted.")
        target, result, received = await self._write(runner.DELETE, path,
                                                      {"expected": [int(v) for v in expected]})
        real = str(result.get("path", target))
        return real, await asyncio.to_thread(self._keep, real, received, "delete")

    async def _delete_pdf(self, path: object, expected: Sequence[int], store: VersionStore) -> tuple[str, bool]:
        """V3-P77 §6.3: a PDF up to 64 MiB is deleted only with its content kept in the PDFs' store (whatever
        the 30-minute rule); refused before the delete when it could not be."""
        size = int(expected[2])
        args: dict[str, Any] = {"expected": [int(v) for v in expected]}
        if size > EDIT_MAX_BYTES:  # an older runner would delete it without a copy
            await self._copies_large("nothing was deleted")
            args["copy_max"] = PDF_MAX_BYTES
        if not store.ready_for(size):
            raise ExplorerRefused("versions_unavailable", "The file's content could not be kept (the data folder is "
                                                          "full or not writable); nothing was deleted.")
        target, result, received = await self._write(runner.DELETE, path, args)
        real = str(result.get("path", target))
        return real, await asyncio.to_thread(self._keep, real, received, "delete", store)

    async def visible(self, path: object) -> str:
        """The path as the versions know it, if the runner shows it now: its
        folder resolved, its name not hidden (review B3: a path hidden since
        it was saved shows no versions)."""
        result, _ = await self._call(runner.VISIBLE, {"path": self._path(path), "cwd": "/"})
        return str(result["path"])

    async def previous(self, path: object) -> tuple[Version, ...]:
        real = await self.visible(path)
        found = [version for store in self._stores() for version in store.of(real)]
        return tuple(sorted(found, key=lambda v: v.saved_at, reverse=True))

    async def deleted(self, folder: object) -> tuple[tuple[str, Version], ...]:
        """Files deleted from this folder whose content is kept (PR #95 review B3): the folder as the runner
        resolves it, and only paths it shows now."""
        found = await self.stat(folder)
        if found.get("kind") != "directory":
            raise ExplorerRefused("not_directory", f"{found.get('path', folder)} is not a folder.")
        # The stores are read on a thread; each path is checked with the runner one at a time (its reads are
        # bounded to a few at once), for at most DELETED_SHOWN of them (PR #95 re-review N1).
        latest: dict[str, Version] = {}
        for store in self._stores():
            for deleted, version in await asyncio.to_thread(store.deleted_in, str(found["path"]), DELETED_SHOWN):
                if deleted not in latest or latest[deleted].saved_at < version.saved_at:
                    latest[deleted] = version
        candidates = sorted(latest.items(), key=lambda item: item[1].saved_at, reverse=True)[:DELETED_SHOWN]
        shown: list[tuple[str, Version]] = []
        for path, version in candidates:
            try:
                if await self.visible(path) == path:
                    shown.append((path, version))
            except ExplorerRefused:
                continue  # hidden now
        return tuple(shown)

    async def restore(self, path: object, version_id: str) -> str:
        """A deleted file put back from one of its versions, in one step
        (V3-P76 §4.1): CHANGED if the name is taken again; ``unsupported``
        where the file system cannot (the editor then restores as before)."""
        real = await self.visible(path)
        if self.pdf_versions is not None:  # a PDF's, up to 64 MiB, put back as it is (V3-P77 §6.3)
            fd = await asyncio.to_thread(self.pdf_versions.open, real, version_id)
            if fd is not None:
                try:
                    return await self._install(real, fd)
                finally:
                    os.close(fd)
        data = next((d for store in (self.versions, self.agent_versions) if store is not None
                     and (d := store.read(real, version_id)) is not None), None)
        if data is None:
            raise ExplorerRefused("not_found", "No such previous version.")
        return await self.install(real, data)

    async def previous_text(self, path: object, version_id: str) -> str:
        real = await self.visible(path)
        if self.pdf_versions is not None and await asyncio.to_thread(self.pdf_versions.has, real, version_id):
            raise ExplorerRefused("not_regular", "This previous version is a PDF: open it instead.")
        data = next((d for store in (self.versions, self.agent_versions) if store is not None
                     and (d := store.read(real, version_id)) is not None), None)
        if data is None:
            raise ExplorerRefused("not_found", "No such previous version.")
        text = text_of(data)
        if text is None:
            raise ExplorerRefused("not_regular", "This previous version is not text.")
        return text

    # --- references (V3-P76 §3) ----------------------------------------------------------

    async def reference(self, path: object, identity: Sequence[int], start: int | None, end: int | None,
                        sha256: str | None) -> FileReference:
        """A reference the server builds from the file itself (§3.2): the file
        must still have the identity it had when the user selected, and the
        text at ``start``–``end`` (UTF-16, the editor's positions, each line
        break one "\\n") must hash to ``sha256``; otherwise ``reference_stale``. The whole file (no
        range) is named only: no excerpt, nothing read beyond its identity."""
        result, fds = await self._call(runner.OPEN, {"path": self._path(path), "cwd": "/"})
        fd, real, size = fds[0], str(result["path"]), int(result["size"])
        now = tuple(int(v) for v in result["identity"])
        name = posixpath.basename(real)
        if tuple(int(v) for v in identity) != now:
            os.close(fd)
            raise ExplorerRefused("reference_stale", f"{name} changed since you selected in it; select again.")
        if start is None and end is None:
            os.close(fd)
            return FileReference(real, now, size=size)
        if start is None or end is None or sha256 is None or not 0 <= start < end:
            os.close(fd)
            raise ExplorerRefused("invalid", "A selection is a start, an end after it, and its text's hash.")
        if size > EDIT_MAX_BYTES:
            os.close(fd)
            raise ExplorerRefused("too_large", f"{name} is over 1 MiB: point at the whole file instead.")
        data = await self._read(fd, size + 1)  # takes the fd
        try:
            # The editor's text: CodeMirror counts every line break (\r\n, \r, \n) as one "\n".
            units = re.sub(r"\r\n?", "\n", data.decode("utf-8")).encode("utf-16-le")
            selected = units[2 * start:2 * end].decode("utf-16-le")
            before = units[:2 * start].decode("utf-16-le")
        except UnicodeError:
            raise ExplorerRefused("reference_stale", f"{name} changed since you selected in it; select again.") \
                from None
        if 2 * end > len(units) or hashlib.sha256(selected.encode("utf-8")).hexdigest() != sha256:
            raise ExplorerRefused("reference_stale", f"{name} changed since you selected in it; select again.")
        start_line = before.count("\n") + 1
        # The line of the last selected character: a selection ending just after a line break does not reach
        # the next line (only its one final break is left out: the empty lines before it are selected).
        end_line = start_line + selected.count("\n") - selected.endswith("\n")
        excerpt = selected.encode("utf-8")
        truncated = len(excerpt) > EXCERPT_BYTES
        text = excerpt[:EXCERPT_BYTES].decode("utf-8", errors="ignore") if truncated else selected
        return FileReference(real, now, start, end, start_line, end_line, text, truncated)

    async def image(self, path: object) -> PreparedView:
        """The image sandbox's copy of an image file (V3-P75 §5.2, review N3);
        refused when it is not one, is too large, or no copy can be made."""
        if self._views is None or not self._views.available:
            raise ExplorerRefused("unavailable", "Images cannot be shown on this server.")
        result, fds = await self._call(runner.OPEN, {"path": self._path(path), "cwd": "/"})
        fd, real, size = fds[0], str(result["path"]), int(result["size"])
        key = (real, tuple(int(v) for v in result["identity"]))
        kept = self._copies.get(key)
        if kept is not None or size > MAX_IMAGE_SOURCE_BYTES:
            os.close(fd)
            if kept is None:
                raise ExplorerRefused("too_large", "This image is too large to show; download it instead.")
            self._copies.move_to_end(key)
            return kept
        data = await self._read(fd, size + 1)
        sniffed = sniff_image(data[:IMAGE_HEAD_BYTES])
        if sniffed is None or len(data) != size:
            raise ExplorerRefused("not_regular", "This file is not an image that can be shown.")
        if sniffed.width * sniffed.height > MAX_IMAGE_PIXELS:
            raise ExplorerRefused("too_large", "This image is too large to show; download it instead.")
        made = await self._views.make(data)
        if not isinstance(made, PreparedView):
            raise ExplorerRefused("other", "This image could not be shown.")
        self._copies[key] = made
        while sum(len(v.data) for v in self._copies.values()) > _CACHE_BYTES:
            self._copies.popitem(last=False)
        return made


def _pread(fd: int, size: int) -> bytes:
    return os.pread(fd, size, 0)


def _copy_pdf(source: int, out: int, size: int) -> bool:
    """The PDF's ``size`` bytes copied into ``out``; False, with nothing copied, if it does not start as one."""
    if os.pread(source, len(PDF_HEADER), 0) != PDF_HEADER:
        return False
    at = 0
    while at < size:
        chunk = os.pread(source, min(1 << 20, size - at), at)
        if not chunk:
            raise OSError("the PDF ended early")
        os.write(out, chunk)
        at += len(chunk)
    return True


_STARTXREF = re.compile(rb"startxref\s+(\d+)\s*%%EOF[\r\n]*$")


def increment_ok(size: int, increment: bytes) -> bool:
    """§5.3 step 4, a light check (the browser makes the same one, ``checkIncrement`` in pdfdoc.js): the update
    ends with ``startxref``, an offset inside it, and ``%%EOF`` (line breaks after it allowed). Its structure is
    not checked: the browser is the owner's, and a broken result goes back to its previous version."""
    found = _STARTXREF.search(increment[-1024:])
    return found is not None and size < int(found.group(1)) < size + len(increment)


def text_of(data: bytes) -> str | None:
    """A whole file's bytes as text, or None (not UTF-8, or binary)."""
    return _text(data, True)


def _text(data: bytes, whole: bool) -> str | None:
    """UTF-8 without NUL, or None. A file's start may end inside a character."""
    if b"\0" in data:
        return None
    for cut in range(1 if whole else 4):
        try:
            return (data[: len(data) - cut] if cut else data).decode("utf-8")
        except UnicodeDecodeError:
            continue
    return None
