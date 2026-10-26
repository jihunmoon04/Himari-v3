"""``himari-runner``: commands and file operations as the user (V3-P74 §4, §5).

One request per connection. The peer must be HIMARI's service user
(``SO_PEERCRED``, checked again whatever the socket's mode says). Blocking
file work runs in threads; a command runs in the machine sandbox, and the
client closing the connection cancels it (the tree is killed). The
connection is closed only once the work has ended — a thread returned, a
command reaped — even when the runner itself is stopping: a client that
cancelled waits for that close before it lets go of its locks (§9.1, PR #87
review C1).

The runner knows no Conversation or principal: each call names its
Workspaces, and the runner checks them against its own configuration —
the hidden list, ``writable_under`` — before anything is done (§5.3).
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import logging
import os
import posixpath
import secrets
import shutil
import socket
import struct
import tempfile
from collections.abc import Callable, Coroutine, Sequence
from typing import Any, TypeVar

from himari.runner import api
from himari.runner.cgroups import Commands
from himari.runner.config import RunnerConfig
from himari.runner.hidden import under
from himari.runner.hostfs import MAX_SNAPSHOT_BYTES, Host
from himari.runner.proxy import Door, Proxy
from himari.runner.push import MAX_BUNDLE_BYTES, Pusher, check_branch, remote, token_problem
from himari.sandbox.api import (
    MAX_ARGV,
    MAX_OUTPUT_BYTES,
    MAX_TIMEOUT_SECONDS,
    CommandResult,
    SandboxSetupError,
    SandboxState,
    SandboxUnavailableError,
    validate_argv,
)
from himari.sandbox.machine import MachineLayout, MachineSandbox, Mask
from himari.sandbox.machine_self_test import probe

log = logging.getLogger("himari.runner")
MAX_LIST = 1000
MAX_CONTENT_BYTES = 256 * 1024  # a write Tool's content (V3-051)
BUNDLE_SECONDS = 300  # git push's steps in the sandbox (§12)
READ_SECONDS = 15.0  # a listing, stat or open answers within this, or is given up (V3-P75 §5.1, PR #92 review B1)
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
    def __init__(self, config: RunnerConfig, sandbox: MachineSandbox, host: Host,
                 commands: Commands | None = None) -> None:
        self.config = config
        self.sandbox = sandbox
        self.host = host
        self.commands = commands
        self.report_failures: list[str] = []
        self.proxy: Proxy | None = None  # commands' way out (§11); None: no network
        # git push's steps outside the sandbox (§12); None: not set up
        self.pusher = None if config.push is None else Pusher(config.push, config.push_token, config.state_dir)
        # One push at a time: at most one bundle (2 GiB) on disk (PR #90 review B2).
        self._pushing = asyncio.Semaphore(1)
        # Reads that can hang on a slow mount (PR #92 review B1): threads of their own, a bounded number.
        self._read_pool = concurrent.futures.ThreadPoolExecutor(MAX_READS, thread_name_prefix="himari-read")
        self._reads = 0

    async def start(self) -> SandboxState:
        """The self-test (V3-050, §13), once; then the proxy, if configured."""
        config = self.config
        os.makedirs(os.path.join(config.himari_home, "conversations"), mode=0o755, exist_ok=True)
        os.makedirs(config.cache_dir, mode=0o700, exist_ok=True)
        os.makedirs(config.state_dir, mode=0o700, exist_ok=True)  # also where the web editor's copies are made

        async def self_test(sandbox: MachineSandbox) -> bool:
            # The bridge and the rules are checked through a proxy of the self-test's own: "localhost" is
            # allowed by name there, and must still be refused for its address (§13).
            folder = tempfile.mkdtemp(prefix="himari-runner-")
            checked = Proxy(("localhost",))
            try:
                door = await checked.open(os.path.join(folder, "proxy.sock"))
                report = await probe(sandbox, config.himari_home, home=config.home,
                                     real_masks=self.host.rules.masks(), runner_socket=config.socket,
                                     cache=config.cache_dir, proxy=door.path,
                                     cgroup_procs=None if self.commands is None else self.commands.procs)
            finally:
                await checked.close()
                shutil.rmtree(folder, ignore_errors=True)
            self.report_failures = report.failures + ([report.error] if report.error else [])
            return report.passed

        state = await self.sandbox.start(self_test)
        if self.pusher is not None and (problem := await self._token_problem()) is not None:
            log.error("git push is off: %s (%s)", problem, self.pusher.token_file)
        if state is not SandboxState.ENFORCED:
            log.error("the machine sandbox is %s: %s", state.value, self.report_failures)
        elif config.network is not None:
            try:
                os.makedirs(config.state_dir, mode=0o700, exist_ok=True)
            except OSError:
                log.exception("the network proxy has no state folder; commands have no network")
            else:
                self.proxy = Proxy(config.network)
        return state

    async def close(self) -> None:
        if self.proxy is not None:
            await self.proxy.close()
            self.proxy = None

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
            await self.close()

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

    async def dispatch(self, op: str, args: dict[str, Any], fds: Sequence[int],
                       conn: socket.socket) -> tuple[dict[str, Any] | None, list[int]]:
        host, config = self.host, self.config
        if op == api.HELLO:
            masks = await self._blocking(host.rules.masks)
            return {"version": api.PROTOCOL_VERSION, "state": self.sandbox.state.value, "home": config.home,
                    "himari_home": config.himari_home, "hidden": len(masks), "cgroup": config.cgroup,
                    "memory_max": config.memory_max if self.commands else None,
                    "pids_max": config.pids_max if self.commands else None,
                    "toolkit": config.documents_toolkit is not None,
                    "network": None if self.proxy is None else list(self.proxy.allow),
                    "push": None if self.pusher is None or await self._token_problem(masks) is not None
                    else list(self.pusher.repos)}, []
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
            real, entries, following = await self._read(
                lambda: host.listing(args.get("path", "."), _text(args, "cwd"), limit, args.get("after")))
            return {"path": real, "entries": entries, "truncated": following is not None, "next": following}, []
        if op == api.STAT:
            return await self._read(lambda: host.stat_path(args.get("path"), _text(args, "cwd"))), []
        if op == api.VISIBLE:
            return {"path": await self._read(lambda: host.visible_path(args.get("path"), _text(args, "cwd"),
                                                                       follow=args.get("follow") is True))}, []
        if op == api.OPEN:
            fd, real, info = await self._read(lambda: host.open_read(args.get("path"), _text(args, "cwd")),
                                              abandoned=lambda opened: os.close(opened[0]))
            return {"path": real, "size": info.st_size, "identity": host.identity(info)}, [fd]
        if op == api.HOLDS:
            size, digest = args.get("size"), args.get("sha256")
            if not isinstance(size, int) or not isinstance(digest, str):
                raise api.RunnerError(api.INVALID)
            return {"holds": await self._blocking(lambda: host.holds(args.get("path"), size, digest))}, []
        if op == api.CHECK_WORKSPACE:
            return await self._blocking(lambda: host.check_workspace(args.get("path"), args.get("others", []),
                                                                     config.writable_under)), []
        roots =await self._blocking(lambda: host.roots(args.get("write_roots"), config.writable_under))
        if op == api.WRITE and args.get("mode") == "replace":
            return await self._replace(args, fds, roots)
        if op == api.WRITE and args.get("mode") == "install":
            return await self._install(args, fds, roots), []
        if op in (api.WRITE, api.DELETE) and args.get("keep") is True:
            return await self._kept(op, args, roots)
        if op == api.WRITE:
            return await self._write(args, fds, roots), []
        if op == api.DELETE and args.get("expected") is not None:
            return await self._delete_checked(args, roots)
        if op in (api.CREATE_FILE, api.MKDIR):
            def make() -> dict[str, Any]:
                parent_fd, parent, name = host.target(args.get("path"), _text(args, "cwd"), roots, follow=False)
                try:
                    if op == api.MKDIR:
                        host.make_folder(parent_fd, name)
                        return {"path": posixpath.join(parent, name)}
                    return {"path": posixpath.join(parent, name), "identity": host.create_file(parent_fd, name)}
                finally:
                    os.close(parent_fd)
            return await self._blocking(make), []
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
        if op == api.GIT_PUSH:
            return await self._push(args, roots, conn), []
        if op in (api.COPY_IN, api.FREE_PATH, api.COMMIT_COPY, api.DISCARD_COPY):
            return await self._blocking(lambda: self._upload(op, args, fds, roots)), []
        raise api.RunnerError(api.INVALID, f"unknown operation {op}")

    @staticmethod
    def _expected(args: dict[str, Any]) -> list[int]:
        expected = args.get("expected")
        if not (isinstance(expected, list) and len(expected) == 4 and all(isinstance(v, int) for v in expected)):
            raise api.RunnerError(api.INVALID, "the file's identity, as it was read, is required")
        return expected

    @staticmethod
    def _copy_max(args: dict[str, Any]) -> int:
        """V3-P77 §6.1: the largest file a replace or a checked delete copies."""
        copy_max = args.get("copy_max", MAX_SNAPSHOT_BYTES)
        if not isinstance(copy_max, int) or isinstance(copy_max, bool) \
                or not MAX_SNAPSHOT_BYTES <= copy_max <= api.MAX_COPY_BYTES:
            raise api.RunnerError(api.INVALID, "copy_max is 1 MiB to 64 MiB")
        return copy_max

    async def _replace(self, args: dict[str, Any], fds: Sequence[int],
                       roots: list[str]) -> tuple[dict[str, Any], list[int]]:
        """V3-P75 §7.4: the web editor's save; the content comes as an fd."""
        host, expected, copy_max = self.host, self._expected(args), self._copy_max(args)
        if len(fds) != 1:
            raise api.RunnerError(api.INVALID, "the content comes as one file")

        def replace() -> tuple[dict[str, Any], list[int]]:
            parent_fd, parent, name = host.target(args.get("path"), _text(args, "cwd"), roots, follow=False)
            try:
                identity, copy = host.replace(parent_fd, name, fds[0], expected, copy_max)
            finally:
                os.close(parent_fd)
            return ({"path": posixpath.join(parent, name), "identity": identity, "previous": copy is not None},
                    [] if copy is None else [copy])

        return await self._blocking(replace)

    async def _delete_checked(self, args: dict[str, Any], roots: list[str]) -> tuple[dict[str, Any], list[int]]:
        host, expected, copy_max = self.host, self._expected(args), self._copy_max(args)

        def delete() -> tuple[dict[str, Any], list[int]]:
            parent_fd, parent, name = host.target(args.get("path"), _text(args, "cwd"), roots, follow=False)
            try:
                copy = host.delete_checked(parent_fd, name, expected, copy_max)
            finally:
                os.close(parent_fd)
            return {"path": posixpath.join(parent, name), "previous": copy is not None}, [] if copy is None else [copy]

        return await self._blocking(delete)

    async def _kept(self, op: str, args: dict[str, Any], roots: list[str]) -> tuple[dict[str, Any], list[int]]:
        """V3-P76 §4.1: an agent's write or delete, with a copy of what the file held (an fd, when there is one)."""
        host = self.host
        expected = args.get("expected")
        if expected is not None and not (isinstance(expected, list) and len(expected) == 4
                                         and all(isinstance(v, int) for v in expected)):
            raise api.RunnerError(api.INVALID)
        content, data = args.get("content"), b""
        if op == api.WRITE:
            if not isinstance(content, str):
                raise api.RunnerError(api.INVALID, "content is required")
            data = content.encode("utf-8")
            if len(data) > MAX_CONTENT_BYTES:
                raise api.RunnerError(api.TOO_LARGE)

        def kept() -> tuple[dict[str, Any], list[int]]:
            parent_fd, parent, name = host.target(args.get("path"), _text(args, "cwd"), roots, follow=op == api.WRITE,
                                                  create_parents=bool(args.get("create_parents", False)))
            try:
                if op == api.WRITE:
                    result, copy = host.kept_write(parent_fd, name, data, expected)
                else:
                    result, copy = host.kept_delete(parent_fd, name)
            finally:
                os.close(parent_fd)
            return {"path": posixpath.join(parent, name), "previous": copy is not None, **result}, \
                [] if copy is None else [copy]

        return await self._blocking(kept)

    async def _install(self, args: dict[str, Any], fds: Sequence[int], roots: list[str]) -> dict[str, Any]:
        """V3-P76 §4.1: a file put back in one step, where its name is free (its content as an fd)."""
        host = self.host
        if len(fds) != 1:
            raise api.RunnerError(api.INVALID, "the content comes as one file")

        def install() -> dict[str, Any]:
            parent_fd, parent, name = host.target(args.get("path"), _text(args, "cwd"), roots, follow=False)
            try:
                identity = host.install(parent_fd, name, fds[0])
            finally:
                os.close(parent_fd)
            return {"path": posixpath.join(parent, name), "identity": identity}

        return await self._blocking(install)

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
        door: Door | None = None
        try:
            if self.proxy is not None:  # a socket of the command's own: its connections, its limit (§22.5)
                try:
                    door = await self.proxy.open(config.proxy_socket(secrets.token_hex(8)))
                except OSError:
                    log.exception("the network proxy could not open; this command has no network")
            cache_fd = os.open(config.cache_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            if config.documents_toolkit is not None:
                toolkit_fd = os.open(config.documents_toolkit, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            with host.open_roots(roots) as opened:
                layout = MachineLayout(home=config.home, cwd=cwd, writable=tuple(opened), masks=masks,
                                       cache=(config.cache_dir, cache_fd), toolkit_fd=toolkit_fd,
                                       cgroup_procs=None if self.commands is None else self.commands.procs,
                                       proxy=None if door is None else door.path)
                before = 0 if self.commands is None else self.commands.oom_kills()
                command = await self._unless_cancelled(self._sandboxed(argv, layout, timeout), conn)
                if command is None:
                    return None
                result = command.result()
                oom = self.commands is not None and self.commands.oom_kills() > before
        finally:
            for fd in (cache_fd, toolkit_fd):
                if fd is not None:
                    os.close(fd)
            if door is not None:
                await door.close()
        return {"exit_code": result.exit_code, "timed_out": result.timed_out,
                "stdout": base64.b64encode(result.stdout).decode(), "stderr": base64.b64encode(result.stderr).decode(),
                "stdout_total": result.stdout_total, "stderr_total": result.stderr_total,
                "elapsed_ms": result.elapsed_ms, "oom": oom}

    async def _unless_cancelled(self, work: Coroutine[Any, Any, T], conn: socket.socket) -> asyncio.Future[T] | None:
        """``work`` run to its end; or, when the client closes the connection,
        stopped and waited for (a command's tree is killed and reaped): None."""
        task = asyncio.ensure_future(work)
        closed = asyncio.ensure_future(api.wait_closed(conn))
        try:
            done, _ = await asyncio.wait({task, closed}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:  # the runner is stopping: the work goes first
            await _stopped(task)
            raise
        finally:
            closed.cancel()
        if task not in done:
            await _stopped(task)
            return None
        return task

    async def _sandboxed(self, argv: list[str], layout: MachineLayout, timeout: float, *,
                         max_output: int = MAX_OUTPUT_BYTES, stdout_to: int | None = None) -> CommandResult:
        try:
            return await self.sandbox.run(argv, layout, timeout, max_output=max_output, stdout_to=stdout_to)
        except SandboxUnavailableError:
            raise api.RunnerError(api.UNAVAILABLE) from None
        except SandboxSetupError:
            log.error("a command's sandbox could not be set up; the machine sandbox is now %s",
                      self.sandbox.state.value)
            raise api.RunnerError(api.SETUP_FAILED) from None

    async def _push(self, args: dict[str, Any], roots: list[str], conn: socket.socket) -> dict[str, Any] | None:
        """§12: a branch of a repository in the call's Workspaces, to its
        checked https remote; the repository is read only in the sandbox."""
        host, pusher = self.host, self.pusher
        if pusher is None:
            raise api.RunnerError(api.ACCESS, "git push is not set up on this server")
        if (problem := await self._token_problem()) is not None:
            log.error("git push refused: %s (%s)", problem, pusher.token_file)
            raise api.RunnerError(api.ACCESS, f"git push is not set up safely on this server: {problem}")
        branch = check_branch(args.get("branch"))
        repository = await self._blocking(lambda: host.directory(args.get("repository", "."), _text(args, "cwd")))
        if not any(under(repository, root) for root in roots):
            raise api.RunnerError(api.OUTSIDE_WORKSPACES, "git_push pushes repositories inside this conversation's "
                                                          "workspaces only; ask the user to attach the folder")
        work = await self._unless_cancelled(self._one_push(pusher, repository, branch), conn)
        return None if work is None else work.result()

    async def _one_push(self, pusher: Pusher, repository: str, branch: str) -> dict[str, Any]:
        async with self._pushing:
            return await self._push_steps(pusher, repository, branch)

    async def _token_problem(self, masks: tuple[Mask, ...] | None = None) -> str | None:
        """Checked on every use: the file, or the hidden list, may have changed (PR #90 review B1)."""
        pusher, rules = self.pusher, self.host.rules
        if pusher is None:
            return "git push is not set up"
        return await self._blocking(lambda: token_problem(pusher.token_file, rules,
                                                          rules.masks() if masks is None else masks))

    async def _push_steps(self, pusher: Pusher, repository: str, branch: str) -> dict[str, Any]:
        config = self.config
        masks = await self._blocking(self.host.rules.masks)
        # Nothing writable but its own /tmp, no network, no cache: the repository is only read.
        layout = MachineLayout(home=config.home, cwd=repository, writable=(), masks=masks,
                               cgroup_procs=None if self.commands is None else self.commands.procs)
        with pusher.workdir() as folder:
            asked = await self._sandboxed(["git", "remote", "get-url", "--push", "origin"], layout, BUNDLE_SECONDS)
            if asked.timed_out:
                raise api.RunnerError(api.OTHER, "git remote get-url took too long; nothing was pushed")
            if asked.exit_code != 0:
                raise api.RunnerError(api.INVALID, "the repository has no 'origin' remote to push to: "
                                                   + asked.stderr.decode("utf-8", "replace").strip()[-500:])
            url = remote(asked.stdout.decode("utf-8", "replace"), pusher.repos)
            bundle = os.path.join(folder, "bundle")
            fd = os.open(bundle, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
            try:
                made = await self._sandboxed(["git", "bundle", "create", "-", f"refs/heads/{branch}"], layout,
                                             BUNDLE_SECONDS, max_output=MAX_BUNDLE_BYTES, stdout_to=fd)
            finally:
                os.close(fd)
            if made.timed_out:
                raise api.RunnerError(api.OTHER, f"git bundle took over {BUNDLE_SECONDS}s; nothing was pushed")
            if made.stdout_total > MAX_BUNDLE_BYTES:
                raise api.RunnerError(api.TOO_LARGE, "the branch is too large to push from here (bundle over 2 GiB)")
            if made.exit_code != 0:
                raise api.RunnerError(api.INVALID, f"git bundle failed for {branch}: "
                                                   + made.stderr.decode("utf-8", "replace").strip()[-500:])
            pushed = await pusher.push(folder, bundle, branch, url)
        return {**pushed.result(), "repository": repository}

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
    caller (re-raised then): a thread cannot be stopped, and a killed
    command is still being reaped."""
    interrupted = False
    while not work.done():
        try:
            await asyncio.wait([work])  # never cancels ``work`` itself
        except asyncio.CancelledError:
            interrupted = True
    if interrupted:
        raise asyncio.CancelledError


async def _stopped(command: asyncio.Future[Any]) -> None:
    """Cancel a command (its tree is killed) and wait until it is reaped."""
    command.cancel()
    await _ended(command)
    if not command.cancelled():
        command.exception()  # retrieved: a failure while stopping is not reported


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
