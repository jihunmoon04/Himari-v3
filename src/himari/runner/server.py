"""``himari-runner``: commands and file operations as the user (V3-P74 §4, §5).

One request per connection. The peer must be HIMARI's service user
(``SO_PEERCRED``, checked again whatever the socket's mode says). Blocking
file work runs in threads; a command runs in the machine sandbox, and the
client closing the connection cancels it (the tree is killed).

The runner knows no Conversation or principal: each call names its
Workspaces, and the runner checks them against its own configuration —
the hidden list, ``writable_under`` — before anything is done (§5.3).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import posixpath
import socket
import struct
from collections.abc import Callable, Sequence
from typing import Any, TypeVar

from himari.runner import api
from himari.runner.cgroups import Commands
from himari.runner.config import RunnerConfig
from himari.runner.hostfs import Host
from himari.sandbox.api import (
    MAX_ARGV,
    MAX_TIMEOUT_SECONDS,
    SandboxSetupError,
    SandboxState,
    SandboxUnavailableError,
    validate_argv,
)
from himari.sandbox.machine import MachineLayout, MachineSandbox
from himari.sandbox.machine_self_test import probe

log = logging.getLogger("himari.runner")
MAX_LIST = 1000
MAX_CONTENT_BYTES = 256 * 1024  # a write Tool's content (V3-051)
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
    def __init__(self, config: RunnerConfig, sandbox: MachineSandbox, host: Host,
                 commands: Commands | None = None) -> None:
        self.config = config
        self.sandbox = sandbox
        self.host = host
        self.commands = commands
        self.report_failures: list[str] = []

    async def start(self) -> SandboxState:
        """The self-test (V3-050, §13), once."""
        os.makedirs(os.path.join(self.config.himari_home, "conversations"), mode=0o755, exist_ok=True)
        os.makedirs(self.config.cache_dir, mode=0o700, exist_ok=True)

        async def self_test(sandbox: MachineSandbox) -> bool:
            report = await probe(sandbox, self.config.himari_home, home=self.config.home,
                                 real_masks=self.host.rules.masks(), runner_socket=self.config.socket,
                                 cache=self.config.cache_dir,
                                 cgroup_procs=None if self.commands is None else self.commands.procs)
            self.report_failures = report.failures + ([report.error] if report.error else [])
            return report.passed

        state = await self.sandbox.start(self_test)
        if state is not SandboxState.ENFORCED:
            log.error("the machine sandbox is %s: %s", state.value, self.report_failures)
        return state

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
                result, out = await self.dispatch(op, args, fds, conn)
            except api.RunnerError as error:
                await api.send_frame(conn, api.failed(error.category, str(error)))
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("%s failed", op)
                await api.send_frame(conn, api.failed(api.OTHER))
                return
            if result is not None:  # None: the client went away during a command
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

    async def _blocking(self, call: Callable[[], T]) -> T:
        return await asyncio.to_thread(call)

    async def dispatch(self, op: str, args: dict[str, Any], fds: Sequence[int],
                       conn: socket.socket) -> tuple[dict[str, Any] | None, list[int]]:
        host, config = self.host, self.config
        if op == api.HELLO:
            masks = await self._blocking(host.rules.masks)
            return {"version": api.PROTOCOL_VERSION, "state": self.sandbox.state.value, "home": config.home,
                    "himari_home": config.himari_home, "hidden": len(masks), "cgroup": config.cgroup,
                    "memory_max": config.memory_max if self.commands else None,
                    "pids_max": config.pids_max if self.commands else None,
                    "toolkit": config.documents_toolkit is not None}, []
        if self.sandbox.state is not SandboxState.ENFORCED:
            # §13 (PR #87 review B1): without a passed self-test nothing but hello is done — files included.
            raise api.RunnerError(api.UNAVAILABLE)
        if op == api.CONVERSATION_FOLDER:
            path = await self._blocking(lambda: host.conversation_folder(args.get("conversation")))
            return {"path": path}, []
        if op == api.REMOVE_CONVERSATION_FOLDER:
            removed = await self._blocking(lambda: host.remove_conversation_folder(args.get("conversation")))
            return {"removed": removed}, []
        if op == api.LIST:
            limit = args.get("limit", MAX_LIST)
            limit = limit if isinstance(limit, int) and 1 <= limit <= MAX_LIST else MAX_LIST
            real, entries, truncated = await self._blocking(
                lambda: host.listing(args.get("path", "."), _text(args, "cwd"), limit))
            return {"path": real, "entries": entries, "truncated": truncated}, []
        if op == api.OPEN:
            fd, real, info = await self._blocking(lambda: host.open_read(args.get("path"), _text(args, "cwd")))
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
        if op == api.RUN:
            return await self._run(args, roots, conn), []
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

    async def _run(self, args: dict[str, Any], roots: list[str], conn: socket.socket) -> dict[str, Any] | None:
        try:
            argv = validate_argv(args.get("argv"))
        except ValueError:
            raise api.RunnerError(api.INVALID, f"argv is 1..{MAX_ARGV} non-empty strings") from None
        timeout = args.get("timeout")
        if not isinstance(timeout, int) or not 1 <= timeout <= MAX_TIMEOUT_SECONDS:
            raise api.RunnerError(api.INVALID, f"timeout is 1..{MAX_TIMEOUT_SECONDS} seconds")
        host, config = self.host, self.config
        cwd = await self._blocking(lambda: host.directory(_text(args, "cwd"), "/"))
        masks = await self._blocking(host.rules.masks)
        toolkit_fd = cache_fd = None
        try:
            cache_fd = os.open(config.cache_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            if config.documents_toolkit is not None:
                toolkit_fd = os.open(config.documents_toolkit, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            with host.open_roots(roots) as opened:
                layout = MachineLayout(home=config.home, cwd=cwd, writable=tuple(opened), masks=masks,
                                       cache=(config.cache_dir, cache_fd), toolkit_fd=toolkit_fd,
                                       cgroup_procs=None if self.commands is None else self.commands.procs)
                before = 0 if self.commands is None else self.commands.oom_kills()
                command = asyncio.ensure_future(self.sandbox.run(argv, layout, timeout))
                closed = asyncio.ensure_future(api.wait_closed(conn))
                try:
                    done, _ = await asyncio.wait({command, closed}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    closed.cancel()
                if command not in done:  # the client cancelled: the tree is killed with the task
                    command.cancel()
                    await asyncio.gather(command, return_exceptions=True)
                    return None
                try:
                    result = command.result()
                except SandboxUnavailableError:
                    raise api.RunnerError(api.UNAVAILABLE) from None
                except SandboxSetupError:
                    log.error("a command's sandbox could not be set up; the machine sandbox is now %s",
                              self.sandbox.state.value)
                    raise api.RunnerError(api.SETUP_FAILED) from None
                oom = self.commands is not None and self.commands.oom_kills() > before
        finally:
            for fd in (cache_fd, toolkit_fd):
                if fd is not None:
                    os.close(fd)
        return {"exit_code": result.exit_code, "timed_out": result.timed_out,
                "stdout": base64.b64encode(result.stdout).decode(), "stderr": base64.b64encode(result.stderr).decode(),
                "stdout_total": result.stdout_total, "stderr_total": result.stderr_total,
                "elapsed_ms": result.elapsed_ms, "oom": oom}

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
