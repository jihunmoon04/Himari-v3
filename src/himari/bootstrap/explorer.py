"""The web's file explorer (V3-P75 §5): the owner's view of this server's
files, through the machine runner.

HIMARI never opens the user's files itself (V3-P74 §4.2): every listing,
read and download is the runner's, with its hidden list and checks. Paths
are absolute or start with ``~`` (the user's home); the runner resolves
them and answers with real paths.

Text is returned as text, to be shown as text; images are shown only as
the copy the image sandbox made (V3-P67), never their own bytes; anything
else is a download (an attachment, never rendered on this origin).

The explorer only reads (V3-P99): nothing here writes, makes or deletes a file.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import logging
import os
import posixpath
import re
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
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
from himari.common import FileReference
from himari.environment.machine import MachineEnvironment
from himari.runner import api as runner

log = logging.getLogger("himari.explorer")

BROWSE_PAGE = 500
EDIT_MAX_BYTES = 1024 * 1024  # a text file shown whole, and the largest a selection is pointed at in (V3-P76 Q1)
PREVIEW_BYTES = 256 * 1024  # the start of a larger text file
MAX_PATH_CHARS = 4096
_CACHE_BYTES = 24 * 1024 * 1024  # image copies kept for the explorer
READ_SECONDS = 15.0  # reading an opened file; the runner bounds its own reads alike (PR #92 review B1)
CALL_SECONDS = 20.0  # a runner answer, its own 15 s included
MAX_READS = 4  # file reads under way here, given-up ones included
EXCERPT_BYTES = 20 * 1024  # a reference's excerpt, as UTF-8 (V3-P76 Q1)

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
class ShownFile:
    path: str
    size: int
    identity: tuple[int, ...]
    modified: int | None
    kind: str  # "text" | "image" | "binary"
    text: str | None = None
    truncated: bool = False  # the text is the file's start only


class Explorer:
    def __init__(self, machine: MachineEnvironment, views: ViewMaker | None) -> None:
        self._machine = machine
        self._views = views
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
        """``read`` (of ``fd``, which this takes over) on the explorer's threads, as ``_read``: given up on (a
        slow mount, a cancelled request), it still ends on its own and closes ``fd`` then, never before."""
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
        return ShownFile(real, size, identity, modified, "text", text, truncated=not whole)

    # --- references (V3-P76 §3) ----------------------------------------------------------

    async def reference(self, path: object, identity: Sequence[int], start: int | None, end: int | None,
                        sha256: str | None) -> FileReference:
        """A reference the server builds from the file itself (§3.2): the file
        must still have the identity it had when the user selected, and the
        text at ``start``–``end`` (UTF-16, the page's positions, each line
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
            # The text as the page shows it: every line break (\r\n, \r, \n) is one "\n".
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
