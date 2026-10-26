"""The runner's wire contract (V3-P74 §5): what HIMARI and ``himari-runner`` say.

One request per connection over a Unix stream socket: the client sends one
frame, the runner answers with one frame and the connection ends. A frame
is a 4-byte big-endian length and a JSON object; open files travel with it
as ``SCM_RIGHTS`` (V3-P74 §5.2), attached to the frame's first byte.

Request: ``{"op": <name>, "args": {...}}``. Answer: ``{"ok": true,
"result": {...}}`` or ``{"ok": false, "error": {"category": <category>,
"message": <text safe to show the model>}}``.

While ``run`` is under way the client sends nothing more; closing the
connection (or only its sending side) cancels the command (the runner kills
its tree).

The runner closes a connection only once the request's work has ended: a
cancelled command is killed and reaped first, a file operation's thread has
returned. A client that cancels one of the ``WRITES`` waits for that close
before it gives up the paths it holds (PR #87 review C1).

Standard library and ``himari.common`` only: both processes import it.
"""

from __future__ import annotations

import array
import asyncio
import json
import os
import socket
import struct
from collections.abc import Mapping, Sequence
from typing import Any

PROTOCOL_VERSION = 4  # 2: write and delete with ``keep``, write ``mode: "install"``, visible ``follow`` (V3-P76);
# 3: ``copy_max`` on a replace, ``delete_copied`` (V3-P77 §6.1); 4: ``append`` and ``truncate`` (V3-P82 §3.6)
KEEP_PROTOCOL = 2  # a runner older than this ignores ``keep`` and ``install`` (it would write plainly)
COPY_MAX_PROTOCOL = 3  # a runner older than this ignores ``copy_max`` (it copies 1 MiB at most: a replace of a
# larger file is refused) and refuses ``delete_copied`` as unknown: either way nothing goes uncopied
MAX_COPY_BYTES = 64 * 1024 * 1024  # the largest ``copy_max``: a PDF the web writes to (V3-P77 §3.3)
APPEND_PROTOCOL = 4  # a runner older than this refuses ``append`` and ``truncate`` as unknown (``invalid``)
MAX_APPEND_BYTES = 8 * 1024 * 1024  # one ``append``: a PDF's incremental update (V3-P77 §5.3)
MAX_FRAME_BYTES = 4 * 1024 * 1024
MAX_FDS = 4
_HEADER = struct.Struct(">I")

# Operations
HELLO = "hello"
CONVERSATION_FOLDER = "conversation_folder"
LIST = "list"  # a page of a folder, sorted, after a cursor (V3-P75 §5.1)
STAT = "stat"  # what open checks, nothing handed over (V3-P75 §5.3)
VISIBLE = "visible"  # a path whose folder resolves and which is not hidden; it need not exist (V3-P75 §7.4);
# with ``follow: true``, where a write lands (a final link followed, missing folders allowed: V3-P76 §8)
OPEN = "open"
WRITE = "write"
DELETE = "delete"
RUN = "run"
COPY_IN = "copy_in"
FREE_PATH = "free_path"
COMMIT_COPY = "commit_copy"
DISCARD_COPY = "discard_copy"
HOLDS = "holds"
REMOVE_CONVERSATION_FOLDER = "remove_conversation_folder"
CHECK_WORKSPACE = "check_workspace"  # a folder the user registers (§10.1): nothing is written
GIT_PUSH = "git_push"  # a branch of a repository in the call's Workspaces (§12)
CREATE_FILE = "create_file"  # a new empty file, never over one (V3-P75 §7.7)
MKDIR = "mkdir"  # one new folder (V3-P75 §7.5)
DELETE_COPIED = "delete_copied"  # a checked delete whose copy is required (V3-P77 §6.3): an op of its own, so
# that a runner too old for it refuses it instead of deleting without the copy (PR #103 review P1)
APPEND = "append"  # bytes written at the end of a file, in place (V3-P82 §3.6)
TRUNCATE = "truncate"  # a file cut back to just after one of its ``%%EOF`` (V3-P82 §3.6)
WRITES = frozenset({WRITE, DELETE, RUN, COPY_IN, FREE_PATH, COMMIT_COPY, DISCARD_COPY, REMOVE_CONVERSATION_FOLDER,
                    GIT_PUSH, CREATE_FILE, MKDIR, DELETE_COPIED, APPEND, TRUNCATE})
# ``write`` with ``mode: "replace"`` (V3-P75 §7.4): ``expected`` required; the answer carries a copy of the
# replaced file as an fd (none over 1 MiB). ``delete`` with ``expected``: the same for the deleted file.
# ``write`` and ``delete`` with ``keep: true`` (V3-P76 §4.1, an agent's write): the same copy, never over a file
# that changed meanwhile, and a new file never over one made meanwhile; the answer says ``prior_identity`` (``[]``:
# there was none), ``after_identity`` (``[]``: deleted; null: unknown), ``complete`` and ``partial`` (a new file
# whose writing did not finish). Errors come only before the file was changed: after it, the answer is a success
# with ``complete: false``. ``write`` with ``mode: "install"``: the content (an fd) given a name that is free,
# in one step, or nothing (CHANGED: taken; UNSUPPORTED: the file system cannot).
# ``copy_max`` (bytes, 1 MiB to MAX_COPY_BYTES; 1 MiB if left out) on a replace (V3-P77 §6.1): the largest file
# copied; a larger one is TOO_LARGE. ``delete_copied`` (``expected``, ``copy_max`` required): the file deleted
# only with its copy (the answer's fd), TOO_LARGE over ``copy_max``.
# ``append`` (``expected`` required, the bytes as one fd of at most MAX_APPEND_BYTES): written at the end of the
# file whose identity is ``expected`` (a regular file with one name), in place, never through a temporary file,
# one at a time per file; the answer's identity is the name's after. Refused before writing: CHANGED, LINKS,
# NOT_REGULAR, ACCESS (read-only), UNSUPPORTED (cannot be written into in place). After writing: CHANGED with
# ``partial`` (the file is not the size our bytes left it at, or another file took the name: read it again).
# A failed write is cut back when nothing else wrote meanwhile; its error carries the file's ``identity`` then
# and ``partial`` (bytes of it left at the end; never with ACCESS or UNSUPPORTED). ``truncate`` (``expected``,
# ``to``): the file cut to ``to``, within the last MAX_APPEND_BYTES (and 1 KiB) and right after a
# ``startxref`` and its ``%%EOF`` (and at most one line break): only a save cut short taken off its end.

# Error categories: each message is safe to show the model.
NOT_FOUND = "not_found"  # also every hidden path: whether it exists is not told
OUTSIDE_WORKSPACES = "outside_workspaces"
IS_DIRECTORY = "is_directory"
NOT_DIRECTORY = "not_directory"
NOT_REGULAR = "not_regular"
EXISTS = "exists"
ACCESS = "access"
TOO_LARGE = "too_large"
CHANGED = "changed"
INVALID = "invalid"
LINKS = "links"
UNAVAILABLE = "unavailable"  # the sandbox is not enforced
TIMED_OUT = "timed_out"  # a read did not answer in time (a slow mount, V3-P75 §5.1)
SETUP_FAILED = "setup_failed"  # the sandbox could not be built; nothing ran
UNSUPPORTED = "unsupported"  # the file system cannot do this in one step; nothing was done (V3-P76 §4.1)
OTHER = "other"

MESSAGES = {
    NOT_FOUND: "no such file or directory",
    OUTSIDE_WORKSPACES: "outside this conversation's workspaces: you can write only there; "
                        "ask the user to attach the folder as a workspace",
    IS_DIRECTORY: "is a directory",
    NOT_DIRECTORY: "not a directory",
    NOT_REGULAR: "not a regular file",
    EXISTS: "already exists",
    ACCESS: "permission denied",
    TOO_LARGE: "file is too large",
    CHANGED: "file changed while it was being edited; nothing written",
    INVALID: "invalid path",
    LINKS: "too many levels of symbolic links",
    UNAVAILABLE: "the machine sandbox is not enforced on this server; nothing was done",
    TIMED_OUT: "this did not answer in time (a slow or unresponsive mount?); try again or use a folder inside it",
    SETUP_FAILED: "the sandbox could not be set up; nothing was run",
    UNSUPPORTED: "this file system cannot put a file in place in one step; nothing was done",
    OTHER: "file operation failed",
}


class RunnerError(Exception):
    """The runner refused or failed an operation; ``str()`` is safe to show."""

    def __init__(self, category: str, message: str | None = None, *, identity: list[int] | None = None,
                 partial: bool = False) -> None:
        super().__init__(message or MESSAGES.get(category, MESSAGES[OTHER]))
        self.category = category
        self.identity = identity  # ``append``'s failure (V3-P82 §3.6): the file as it is now, when known
        self.partial = partial  # ... and bytes of it left at the end


class RunnerUnavailable(Exception):
    """No answer: the runner is not running, or the connection broke."""


class NotSent(RunnerUnavailable):
    """The request never reached the runner: nothing was done."""


class ProtocolError(Exception):
    """A malformed or oversized frame."""


async def _ready(sock: socket.socket, *, write: bool) -> None:
    loop = asyncio.get_running_loop()
    future: asyncio.Future[None] = loop.create_future()

    def wake() -> None:
        if not future.done():
            future.set_result(None)

    fd = sock.fileno()
    if write:
        loop.add_writer(fd, wake)
    else:
        loop.add_reader(fd, wake)
    try:
        await future
    finally:
        if write:
            loop.remove_writer(fd)
        else:
            loop.remove_reader(fd)


async def send_frame(sock: socket.socket, message: Mapping[str, Any], fds: Sequence[int] = ()) -> None:
    """``sock`` is non-blocking. The fds go with the first bytes sent."""
    if len(fds) > MAX_FDS:
        raise ProtocolError("too many descriptors")
    body = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_FRAME_BYTES:
        raise ProtocolError("frame too large")
    view = memoryview(_HEADER.pack(len(body)) + body)
    pending = list(fds)
    while view:
        try:
            if pending:
                sent = sock.sendmsg([view], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", pending))])
                pending = []
            else:
                sent = sock.send(view)
        except BlockingIOError:
            await _ready(sock, write=True)
            continue
        view = view[sent:]


async def recv_frame(sock: socket.socket, *, max_fds: int = MAX_FDS) -> tuple[dict[str, Any], list[int]]:
    """One frame and the fds that came with it (close-on-exec). A frame that
    is malformed, too large or followed by more bytes is refused, and any
    fds received are closed. EOF before a whole frame: RunnerUnavailable."""
    fds: list[int] = []
    buffer = bytearray()
    try:
        while True:
            try:
                data, ancillary, flags, _ = sock.recvmsg(65536, socket.CMSG_SPACE(max_fds * 4),
                                                         socket.MSG_CMSG_CLOEXEC)
            except BlockingIOError:
                await _ready(sock, write=False)
                continue
            for level, kind, payload in ancillary:
                if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                    received = array.array("i")
                    received.frombytes(payload[: len(payload) - len(payload) % received.itemsize])
                    fds.extend(received)
            if flags & socket.MSG_CTRUNC:
                raise ProtocolError("too many descriptors")
            if not data:
                raise RunnerUnavailable("the connection closed")
            buffer += data
            if len(buffer) >= _HEADER.size:
                (length,) = _HEADER.unpack_from(buffer)
                if length > MAX_FRAME_BYTES:
                    raise ProtocolError("frame too large")
                end = _HEADER.size + length
                if len(buffer) > end:
                    raise ProtocolError("bytes after the frame")
                if len(buffer) == end:
                    message = json.loads(bytes(buffer[_HEADER.size:]))
                    if not isinstance(message, dict):
                        raise ProtocolError("a frame is a JSON object")
                    return message, fds
    except BaseException:
        for fd in fds:
            os.close(fd)
        raise


async def wait_closed(sock: socket.socket) -> None:
    """Return when the peer closes the connection (or sends anything: the
    protocol has nothing more to say during an operation)."""
    while True:
        try:
            sock.recv(1, socket.MSG_PEEK)
            return
        except BlockingIOError:
            await _ready(sock, write=False)
        except OSError:
            return


def ok(result: Mapping[str, Any]) -> dict[str, Any]:
    return {"ok": True, "result": dict(result)}


def failed(category: str, message: str | None = None, identity: list[int] | None = None,
           partial: bool = False) -> dict[str, Any]:
    error: dict[str, Any] = {"category": category, "message": message or MESSAGES.get(category, MESSAGES[OTHER])}
    if identity is not None:
        error["identity"] = identity
    if partial:
        error["partial"] = True
    return {"ok": False, "error": error}
