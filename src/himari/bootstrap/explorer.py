"""The web's file explorer (V3-P75 §5): the owner's view of this server's
files, through the machine runner.

HIMARI never opens the user's files itself (V3-P74 §4.2): every listing,
read and download is the runner's, with its hidden list and checks. Paths
are absolute or start with ``~`` (the user's home); the runner resolves
them and answers with real paths.

Nothing here writes (part 1). Text is returned as text, to be shown as
text; images are shown only as the copy the image sandbox made (V3-P67),
never their own bytes; anything else is a download (an attachment, never
rendered on this origin).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import posixpath
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from himari.data.api import (
    IMAGE_HEAD_BYTES,
    MAX_IMAGE_PIXELS,
    MAX_IMAGE_SOURCE_BYTES,
    PreparedView,
    RegisteredWorkspace,
    ViewMaker,
    sniff_image,
)
from himari.environment.machine import MachineEnvironment
from himari.runner import api as runner

BROWSE_PAGE = 500
EDIT_MAX_BYTES = 1024 * 1024  # V3-P75 Q3: what the editor takes whole
PREVIEW_BYTES = 256 * 1024  # the start of a larger text file, shown read-only
MAX_PATH_CHARS = 4096
_CACHE_BYTES = 24 * 1024 * 1024  # image copies kept for the explorer
READ_SECONDS = 15.0  # reading an opened file; the runner bounds its own reads alike (PR #92 review B1)
CALL_SECONDS = 20.0  # a runner answer, its own 15 s included
MAX_READS = 4  # file reads under way here, given-up ones included


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
class ShownFile:
    path: str
    size: int
    identity: tuple[int, ...]
    modified: int | None
    kind: str  # "text" | "image" | "binary"
    text: str | None = None
    truncated: bool = False  # the text is the file's start only
    editable: bool = False  # whole, text, at most EDIT_MAX_BYTES (part 3 edits it)


class Explorer:
    def __init__(self, machine: MachineEnvironment, views: ViewMaker | None) -> None:
        self._machine = machine
        self._views = views
        self._copies: OrderedDict[tuple[str, tuple[int, ...]], PreparedView] = OrderedDict()
        self._pool = concurrent.futures.ThreadPoolExecutor(MAX_READS, thread_name_prefix="himari-explorer")
        self._reads = 0

    async def _call(self, op: str, args: dict[str, Any]) -> tuple[dict[str, Any], list[int]]:
        try:
            async with asyncio.timeout(CALL_SECONDS):
                return await self._machine.call(op, args)
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
        if self._reads >= MAX_READS:
            os.close(fd)
            raise ExplorerRefused(runner.TIMED_OUT, "Earlier slow reads are still under way (an unresponsive "
                                                    "mount?); try again later.")
        self._reads += 1

        def read() -> bytes:
            try:
                return _pread(fd, size)
            finally:
                os.close(fd)

        work = asyncio.get_running_loop().run_in_executor(self._pool, read)

        def ended(future: asyncio.Future[bytes]) -> None:
            self._reads -= 1
            if not future.cancelled():
                future.exception()  # retrieved: a given-up read's failure is not reported

        work.add_done_callback(ended)
        try:
            async with asyncio.timeout(READ_SECONDS):
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

    async def open(self, path: object) -> tuple[str, int, int]:
        """The file's real path, size and an open descriptor (the caller closes it)."""
        result, fds = await self._call(runner.OPEN, {"path": self._path(path), "cwd": "/"})
        return str(result["path"]), int(result["size"]), fds[0]

    async def show(self, path: object) -> ShownFile:
        result, fds = await self._call(runner.OPEN, {"path": self._path(path), "cwd": "/"})
        fd, real, size = fds[0], str(result["path"]), int(result["size"])
        identity = tuple(int(v) for v in result["identity"])
        head = await self._read(fd, min(size, EDIT_MAX_BYTES) + 1)
        modified = identity[3] // 1_000_000_000 if len(identity) == 4 else None
        if sniff_image(head[:IMAGE_HEAD_BYTES]) is not None:
            return ShownFile(real, size, identity, modified, "image")
        whole = size <= EDIT_MAX_BYTES and len(head) == size
        text = _text(head if whole else head[:PREVIEW_BYTES], whole)
        if text is None:
            return ShownFile(real, size, identity, modified, "binary")
        return ShownFile(real, size, identity, modified, "text", text, truncated=not whole, editable=whole)

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
