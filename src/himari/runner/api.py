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

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 4 * 1024 * 1024
MAX_FDS = 4
_HEADER = struct.Struct(">I")

# Operations
HELLO = "hello"
CONVERSATION_FOLDER = "conversation_folder"
LIST = "list"  # a page of a folder, sorted, after a cursor (V3-P75 §5.1)
STAT = "stat"  # what open checks, nothing handed over (V3-P75 §5.3)
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
WRITES = frozenset({WRITE, DELETE, RUN, COPY_IN, FREE_PATH, COMMIT_COPY, DISCARD_COPY, REMOVE_CONVERSATION_FOLDER,
                    GIT_PUSH})

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
SETUP_FAILED = "setup_failed"  # the sandbox could not be built; nothing ran
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
    SETUP_FAILED: "the sandbox could not be set up; nothing was run",
    OTHER: "file operation failed",
}


class RunnerError(Exception):
    """The runner refused or failed an operation; ``str()`` is safe to show."""

    def __init__(self, category: str, message: str | None = None) -> None:
        super().__init__(message or MESSAGES.get(category, MESSAGES[OTHER]))
        self.category = category


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


def failed(category: str, message: str | None = None) -> dict[str, Any]:
    return {"ok": False, "error": {"category": category, "message": message or MESSAGES.get(category, MESSAGES[OTHER])}}
