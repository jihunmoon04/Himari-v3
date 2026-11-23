"""``himari-runner``: file operations as the user (V3-P74 §4, §5).

One request per connection. The peer must be HIMARI's service user
(``SO_PEERCRED``, checked again whatever the socket's mode says). Blocking
file work runs in threads. The connection is closed only once the work has
ended — its thread returned — even when the client cancelled or the runner
itself is stopping: a client that cancelled waits for that close before it
lets go of its locks (§9.1, PR #87 review C1).

The runner knows no Conversation or principal: each write names its write
roots (the Conversation's folder), and the runner checks them against its
own configuration — the hidden list, ``writable_under`` — before anything
is done (§5.3).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import posixpath
import socket
import struct
from collections.abc import Callable, Sequence
from typing import Any, TypeVar

from himari.runner import api
from himari.runner.config import RunnerConfig
from himari.runner.hostfs import Host

log = logging.getLogger("himari.runner")
MAX_LIST = 1000
MAX_CONTENT_BYTES = 256 * 1024  # a write Tool's content (V3-051)
READ_SECONDS = 15.0  # a listing or open answers within this, or is given up (V3-P75 §5.1, PR #92 review B1)
MAX_READS = 4  # reads under way, given-up ones included: a stuck mount holds at most these threads
_PEERCRED = struct.Struct("3i")
T = TypeVar("T")


def _peer_uid(conn: socket.socket) -> int:
    _, uid, _ = _PEERCRED.unpack(conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _PEERCRED.size))
    return int(uid)


def _text(args: dict[str, Any], name: str, default: str | None = None) -> str:
    value = args.get(name, default)
    if not isinstance(value, str):
        raise api.RunnerError(api.INVALID, f"{name} is required")
    return value


class Runner:
    def __init__(self, config: RunnerConfig, host: Host) -> None:
        self.config = config
        self.host = host
        # Reads that can hang on a slow mount (PR #92 review B1): threads of their own, a bounded number.
        self._read_pool = concurrent.futures.ThreadPoolExecutor(MAX_READS, thread_name_prefix="himari-read")
        self._reads = 0

    async def serve(self, listener: socket.socket, stop: asyncio.Event) -> None:
        listener.setblocking(False)
        loop = asyncio.get_running_loop()
        tasks: set[asyncio.Task[None]] = set()
        stopping = asyncio.ensure_future(stop.wait())
        try:
            while True:
                accepting = asyncio.ensure_future(loop.sock_accept(listener))
                done, _ = await asyncio.wait({accepting, stopping}, return_when=asyncio.FIRST_COMPLETED)
                if stopping in done:
                    accepting.cancel()
                    break
                conn, _ = accepting.result()
                task = asyncio.create_task(self.handle(conn))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        finally:
            stopping.cancel()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def handle(self, conn: socket.socket) -> None:
        conn.setblocking(False)
        fds: list[int] = []
        out: list[int] = []
        try:
            if _peer_uid(conn) != self.config.client_uid:
                log.warning("refused a connection from uid %s", _peer_uid(conn))
                return
            try:
                request, fds = await api.recv_frame(conn)
            except (api.ProtocolError, api.RunnerUnavailable, ValueError):
                return
            op, args = request.get("op"), request.get("args", {})
            if not isinstance(op, str) or not isinstance(args, dict):
                await api.send_frame(conn, api.failed(api.INVALID, "malformed request"))
                return
            try:
                result, out = await self.dispatch(op, args, fds)
            except api.RunnerError as error:
                await api.send_frame(conn, api.failed(error.category, str(error)))
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("%s failed", op)
                await api.send_frame(conn, api.failed(api.OTHER))
                return
            await api.send_frame(conn, api.ok(result), out)
        except (OSError, api.ProtocolError, api.RunnerUnavailable):
            pass
        finally:
            for fd in (*fds, *out):
                try:
                    os.close(fd)
                except OSError:
                    pass
            conn.close()

    async def _read(self, call: Callable[[], T], abandoned: Callable[[T], None] | None = None) -> T:
        """A read that may hang on a slow mount (PR #92 review B1): answered
        within READ_SECONDS or given up (TIMED_OUT). A thread cannot be
        stopped, so a given-up read keeps its thread until the mount answers;
        at most MAX_READS are under way, and a further one is refused at once.
        ``abandoned`` cleans up what a given-up read returns (an open fd)."""
        if self._reads >= MAX_READS:
            raise api.RunnerError(api.TIMED_OUT, "the server is still waiting on earlier slow reads (an unresponsive "
                                                 "mount?); try again later")
        self._reads += 1
        work = asyncio.get_running_loop().run_in_executor(self._read_pool, call)
        given_up = False

        def ended(future: asyncio.Future[T]) -> None:
            self._reads -= 1
            if future.cancelled() or future.exception() is not None:
                return
            if given_up and abandoned is not None:
                abandoned(future.result())

        work.add_done_callback(ended)
        try:
            async with asyncio.timeout(READ_SECONDS):
                return await asyncio.shield(work)
        except TimeoutError:
            given_up = True
            raise api.RunnerError(api.TIMED_OUT) from None
        except asyncio.CancelledError:
            given_up = True
            raise

    async def _blocking(self, call: Callable[[], T]) -> T:
        work = asyncio.ensure_future(asyncio.to_thread(call))
        await _ended(work)
        return work.result()

    async def dispatch(self, op: str, args: dict[str, Any], fds: Sequence[int]) -> tuple[dict[str, Any], list[int]]:
        host, config = self.host, self.config
        if op == api.HELLO:
            masks = await self._blocking(host.rules.masks)
            return {"version": api.PROTOCOL_VERSION, "home": config.home, "himari_home": config.himari_home,
                    "hidden": len(masks)}, []
        if op == api.CONVERSATION_FOLDER:
            path = await self._blocking(lambda: host.conversation_folder(args.get("conversation")))
            return {"path": path}, []
        if op == api.REMOVE_CONVERSATION_FOLDER:
            removed = await self._blocking(lambda: host.remove_conversation_folder(args.get("conversation")))
            return {"removed": removed}, []
        if op == api.LIST:
            limit = args.get("limit", MAX_LIST)
            limit = limit if isinstance(limit, int) and 1 <= limit <= MAX_LIST else MAX_LIST
            real, entries, truncated = await self._read(
                lambda: host.listing(args.get("path", "."), _text(args, "cwd"), limit))
            return {"path": real, "entries": entries, "truncated": truncated}, []
        if op == api.OPEN:
            fd, real, info = await self._read(lambda: host.open_read(args.get("path"), _text(args, "cwd")),
                                              abandoned=lambda opened: os.close(opened[0]))
            return {"path": real, "size": info.st_size, "identity": host.identity(info)}, [fd]
        if op == api.HOLDS:
            size, digest = args.get("size"), args.get("sha256")
            if not isinstance(size, int) or not isinstance(digest, str):
                raise api.RunnerError(api.INVALID)
            return {"holds": await self._blocking(lambda: host.holds(args.get("path"), size, digest))}, []
        roots = await self._blocking(lambda: host.roots(args.get("write_roots"), config.writable_under))
        if op == api.WRITE:
            return await self._write(args, fds, roots), []
        if op == api.DELETE:
            def delete() -> str:
                parent_fd, parent, name = host.target(args.get("path"), _text(args, "cwd"), roots, follow=False)
                try:
                    host.delete(parent_fd, name)
                finally:
                    os.close(parent_fd)
                return posixpath.join(parent, name)
            return {"path": await self._blocking(delete)}, []
        if op in (api.COPY_IN, api.FREE_PATH, api.COMMIT_COPY, api.DISCARD_COPY):
            return await self._blocking(lambda: self._upload(op, args, fds, roots)), []
        raise api.RunnerError(api.INVALID, f"unknown operation {op}")

    async def _write(self, args: dict[str, Any], fds: Sequence[int], roots: list[str]) -> dict[str, Any]:
        host = self.host
        content = args.get("content")
        if fds:
            data: bytes | int = fds[0]
        elif isinstance(content, str):
            data = content.encode("utf-8")
            if len(data) > MAX_CONTENT_BYTES:
                raise api.RunnerError(api.TOO_LARGE)
        else:
            raise api.RunnerError(api.INVALID, "content is required")
        expected = args.get("expected")
        if expected is not None and not (isinstance(expected, list) and all(isinstance(v, int) for v in expected)):
            raise api.RunnerError(api.INVALID)

        def write() -> dict[str, Any]:
            parent_fd, parent, name = host.target(args.get("path"), _text(args, "cwd"), roots, follow=True,
                                                  create_parents=bool(args.get("create_parents", False)))
            try:
                created = host.write(parent_fd, name, data, expected)
            finally:
                os.close(parent_fd)
            return {"path": posixpath.join(parent, name), "created": created,
                    "bytes": len(data) if isinstance(data, bytes) else None}

        return await self._blocking(write)

    def _upload(self, op: str, args: dict[str, Any], fds: Sequence[int], roots: list[str]) -> dict[str, Any]:
        host = self.host
        folder_fd = host.folder(args.get("folder"), roots)
        try:
            folder = os.readlink(f"/proc/self/fd/{folder_fd}")
            prefix = _text(args, "prefix", ".himari-")
            if op == api.COPY_IN:
                if len(fds) != 1:
                    raise api.RunnerError(api.INVALID, "copy_in takes one file")
                if args.get("clear"):
                    host.clear_temps(folder_fd, prefix)
                return {"temp": host.write_temp(folder_fd, fds[0], prefix)}
            if op == api.FREE_PATH:
                preferred = args.get("preferred")
                wanted = (posixpath.basename(preferred) if isinstance(preferred, str)
                          and posixpath.dirname(preferred) == folder else None)
                return {"path": posixpath.join(folder, host.free_name(folder_fd, _text(args, "name"), wanted))}
            temp = _text(args, "temp")
            if op == api.COMMIT_COPY:
                path = _text(args, "path")
                if posixpath.dirname(path) != folder:
                    raise api.RunnerError(api.INVALID)
                return {"placed": host.commit_temp(folder_fd, temp, posixpath.basename(path))}
            host.remove_temp(folder_fd, temp)
            return {}
        finally:
            os.close(folder_fd)


async def _ended(work: asyncio.Future[Any]) -> None:
    """Return once ``work`` has ended, waiting through cancellations of the
    caller (re-raised then): a thread cannot be stopped."""
    interrupted = False
    while not work.done():
        try:
            await asyncio.wait([work])  # never cancels ``work`` itself
        except asyncio.CancelledError:
            interrupted = True
    if interrupted:
        raise asyncio.CancelledError


def listener(config: RunnerConfig) -> socket.socket:
    """systemd's socket (V3-P74 §4.1, ``LISTEN_FDS``), else the configured path."""
    if os.environ.get("LISTEN_PID") == str(os.getpid()) and os.environ.get("LISTEN_FDS") == "1":
        return socket.socket(fileno=3)
    os.makedirs(os.path.dirname(config.socket), exist_ok=True)
    try:
        os.unlink(config.socket)
    except FileNotFoundError:
        pass
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(config.socket)
    os.chmod(config.socket, 0o660)
    sock.listen(64)
    return sock
