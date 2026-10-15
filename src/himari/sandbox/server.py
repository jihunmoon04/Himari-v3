"""The server profile: long-lived stdio processes (V3-P59 §4).

A ``ServerSandbox`` shares the host capability with the Workspace backend
but has its own self-test and state (§4.4): stdio servers start only while
it is ENFORCED, and ``run_command`` does not depend on it.

Each ``launch``:

1. checks the request (argv, environment, working directory);
2. opens every ``read_only`` and ``writable`` directory and runs the
   identity checks (``identity.check_trees``) on those descriptors;
3. spawns bwrap with the shared layout, binding those same descriptors at
   their host paths, the operator's environment added to the fixed one, no
   network; Landlock allows writes to ``writable`` and /tmp only;
4. returns the process once the helper reported enforcement — otherwise the
   tree is killed and nothing ran (``SandboxSetupError``, which latches
   SETUP_FAILED like the Workspace backend).

The process is spoken to by lines (stdin/stdout); its stderr is drained
continuously and only the last ``STDERR_TAIL_BYTES`` are kept.

Processes: bwrap (our child, leading its own group) → bwrap as the PID
namespace's init → the server. Every process in the sandbox dies with the
outer bwrap (``--die-with-parent`` and the PID namespace), so SIGKILL to its
group ends the whole server. A SIGTERM there would only end bwrap — the
server would never see it — so shutdown's SIGTERM goes to the server
itself, through a pidfd checked to be the init's child (no pid reuse can
redirect it). Where that cannot be found, the group gets it instead.
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import subprocess
from collections.abc import Awaitable, Callable, Mapping, Sequence
from himari.sandbox import identity
from himari.sandbox.api import (
    MAX_LINE_BYTES,
    MAX_READ_ONLY,
    MAX_WRITABLE,
    STDERR_TAIL_BYTES,
    ProcessGone,
    SandboxAdmissionError,
    SandboxProfileError,
    SandboxSetupError,
    SandboxState,
    SandboxUnavailableError,
    ServerProfile,
    validate_argv,
)
from himari.sandbox.backend import (
    SANDBOX_ENV,
    SETUP_TIMEOUT_SECONDS,
    HostCapability,
    _abandon,
    _kill_tree,
    _reap,
    _settle,
    _wait_sync,
    discover_capability,
    sandbox_arguments,
    system_view,
)
from himari.sandbox import landlock_helper

MAX_ENV_VARS = 64
MAX_ENV_CHARS = 4096
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# asyncio's reader limit: readuntil fails once the newline would sit past
# it, so a line of MAX_LINE_BYTES is read and one byte more is refused.
STREAM_LIMIT = MAX_LINE_BYTES
# Places the layout builds itself (PR #32 review B2): a bound directory may
# not be one of them, lie inside one, or cover one (be an ancestor: bound
# later, it would hide what the layout put there). The system view's own
# destinations are added at check time.
_BUILT = ("/proc", "/dev", "/sys", "/run/himari", "/tmp/home")
# The sandbox's own tmpfs: a directory below /tmp is bound on top of it, but
# /tmp itself or an ancestor would replace it with the host's.
_NOT_COVERED = ("/tmp",)

ServerSelfTest = Callable[["ServerSandbox"], Awaitable[bool]]


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _layout_destinations() -> tuple[str, ...]:
    """What the layout itself puts in place: _BUILT and the system view."""
    view = system_view()
    found = [view[i + 2] for i, arg in enumerate(view) if arg in ("--ro-bind", "--symlink")]
    return (*_BUILT, *found)


def _absolute_roots(paths: Sequence[str]) -> tuple[str, ...]:
    if not all(isinstance(p, str) and os.path.isabs(p) for p in paths):
        raise SandboxProfileError("protected roots are absolute paths")
    return tuple(paths)


def check_profile(profile: ServerProfile, *, protected: Sequence[str], uid: int | None = None) -> ServerProfile:
    """The configuration checks of §4.2. ``protected`` is every root to keep
    out: the sandbox's own and the profile's. The overlaps are compared as
    strings (written and resolved): an early warning for a configuration
    mistake. The guarantee is the identity check at every launch."""
    uid = os.geteuid() if uid is None else uid
    if len(profile.read_only) > MAX_READ_ONLY or len(profile.writable) > MAX_WRITABLE:
        raise SandboxProfileError(f"at most {MAX_READ_ONLY} read_only and {MAX_WRITABLE} writable directories")
    roots = _absolute_roots(protected)
    guarded = {*roots, *(os.path.realpath(p) for p in roots)}
    built = _layout_destinations()
    bound = [*profile.read_only, *profile.writable]
    for path in bound:
        if not isinstance(path, str) or not os.path.isabs(path) or os.path.normpath(path) != path or path == "/":
            raise SandboxProfileError(f"{path!r}: a directory is an absolute, normalized path other than /")
        for written in (path, os.path.realpath(path)):
            for place in _NOT_COVERED:
                if _inside(place, written):
                    raise SandboxProfileError(f"{path}: would cover the sandbox's own {place}")
            for place in built:
                if _inside(written, place) or _inside(place, written):
                    raise SandboxProfileError(f"{path}: the sandbox puts {place} in place itself")
            if any(_inside(written, g) or _inside(g, written) for g in guarded):
                raise SandboxProfileError(f"{path}: overlaps HIMARI's data folder")
        if not os.path.isdir(path):
            raise SandboxProfileError(f"{path}: not an existing directory")
    for i, path in enumerate(bound):
        if any(_inside(path, other) or _inside(other, path) for other in bound[i + 1:]):
            raise SandboxProfileError(f"{path}: overlaps another bound directory")
    for path in profile.writable:
        if os.stat(path).st_uid != uid:
            raise SandboxProfileError(f"{path}: a writable directory must belong to the user HIMARI runs as")
    return profile


def _check_request(profile: ServerProfile, argv: Sequence[str], env: Mapping[str, str], cwd: str) -> list[str]:
    checked = validate_argv(list(argv))
    if len(env) > MAX_ENV_VARS:
        raise SandboxProfileError(f"at most {MAX_ENV_VARS} environment variables")
    for key, value in env.items():
        if not isinstance(key, str) or not _ENV_NAME.fullmatch(key) or key in SANDBOX_ENV or key == "PWD":
            raise SandboxProfileError(f"environment variable {key!r} cannot be set")
        if not isinstance(value, str) or "\0" in value or len(value) > MAX_ENV_CHARS:
            raise SandboxProfileError(f"environment variable {key}: at most {MAX_ENV_CHARS} characters, no NUL")
    # /tmp itself is the sandbox's own; a path below it exists only if bound.
    roots = [*profile.read_only, *profile.writable]
    if not os.path.isabs(cwd) or os.path.normpath(cwd) != cwd or (
            cwd != "/tmp" and not any(_inside(cwd, r) for r in roots)):
        raise SandboxProfileError(f"working directory {cwd!r} is not inside a bound directory or /tmp")
    return checked


def _children(pid: int) -> list[int]:
    with open(f"/proc/{pid}/task/{pid}/children") as handle:
        return [int(child) for child in handle.read().split()]


def _parent(pid: int) -> int:
    with open(f"/proc/{pid}/stat") as handle:
        return int(handle.read().rsplit(")", 1)[1].split()[1])


def _terminate_server(bwrap_pid: int) -> bool:
    """SIGTERM to the server inside the sandbox; False if it was not found."""
    try:
        (init,) = _children(bwrap_pid)
        (server,) = _children(init)
        pidfd = os.pidfd_open(server)
    except (OSError, ValueError):
        return False
    try:
        if _parent(server) != init:  # the pid was reused before the pidfd was opened
            return False
        signal.pidfd_send_signal(pidfd, signal.SIGTERM)
        return True
    except (OSError, ValueError):
        return False
    finally:
        os.close(pidfd)


class ServerProcess:
    """An ``IsolatedProcess``: a running server in its sandbox. Lines are
    read by one reader at a time (asyncio's stream reader allows no other);
    writes may come from several tasks."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process
        self._tail = bytearray()
        self._stderr = asyncio.create_task(self._drain_stderr())
        self._writing = asyncio.Lock()
        self._gone: str | None = None

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    def stderr_tail(self) -> bytes:
        return bytes(self._tail)

    async def write_line(self, data: bytes) -> None:
        if b"\n" in data:
            raise ValueError("a line has no newline in it")
        stdin = self._process.stdin
        if self._gone is not None or stdin is None or self._process.returncode is not None:
            raise ProcessGone(self._gone or "the process has ended")
        async with self._writing:
            try:
                stdin.write(data + b"\n")
                await stdin.drain()
            except (BrokenPipeError, ConnectionResetError, RuntimeError):
                raise ProcessGone("the process stopped reading") from None

    async def read_line(self) -> bytes:
        stdout = self._process.stdout
        if self._gone is not None or stdout is None:
            raise ProcessGone(self._gone or "the process has ended")
        try:
            line = await stdout.readuntil(b"\n")
        except asyncio.IncompleteReadError:
            raise ProcessGone("the process closed its output") from None
        except asyncio.LimitOverrunError:
            self._gone = f"a line longer than {MAX_LINE_BYTES} bytes"
            await self.shutdown(0)
            raise ProcessGone(self._gone) from None
        return line[:-1]

    async def shutdown(self, grace: float) -> None:
        """Close stdin and wait; SIGTERM the group and wait; SIGKILL. Finishes
        killing and reaping even if cancelled meanwhile."""
        self._gone = self._gone or "the process was shut down"
        process = self._process
        try:
            if process.stdin is not None and not process.stdin.is_closing():
                process.stdin.close()
            for sig in (signal.SIGTERM, signal.SIGKILL):
                if process.returncode is not None:
                    break
                try:
                    await asyncio.wait_for(asyncio.shield(process.wait()), grace)
                except TimeoutError:
                    if sig is signal.SIGTERM and _terminate_server(process.pid):
                        continue
                    try:
                        os.killpg(process.pid, sig)
                    except (ProcessLookupError, PermissionError):
                        pass
        except asyncio.CancelledError:
            _kill_tree(process.pid)
            await _reap(process)
            raise
        if process.returncode is None:
            await _reap(process)  # SIGKILL sent: wait however long the exit takes
        try:
            await asyncio.wait_for(self._stderr, 5.0)
        except (TimeoutError, asyncio.CancelledError):
            self._stderr.cancel()

    async def _drain_stderr(self) -> None:
        stream = self._process.stderr
        if stream is None:
            return
        while chunk := await stream.read(65536):
            self._tail += chunk
            if len(self._tail) > STDERR_TAIL_BYTES:
                del self._tail[: len(self._tail) - STDERR_TAIL_BYTES]


class ServerLauncher:
    """A ``ProcessLauncher`` bound to one checked profile."""

    def __init__(self, sandbox: ServerSandbox, profile: ServerProfile) -> None:
        self._sandbox = sandbox
        self.profile = profile

    async def launch(self, argv: Sequence[str], env: Mapping[str, str], cwd: str) -> ServerProcess:
        if self._sandbox.state is not SandboxState.ENFORCED:
            raise SandboxUnavailableError(self._sandbox.state.value)
        try:
            return await self._sandbox.spawn(self.profile, argv, env, cwd)
        except SandboxSetupError:
            self._sandbox.setup_failed()
            raise


class ServerSandbox:
    """``protected`` are the roots no server may reach — HIMARI's data root
    (which holds the Workspaces) at least. They are fixed here and apply to
    every launcher and launch; a profile's own ``protected`` only adds to
    them (PR #32 review B1)."""

    def __init__(self, capability: HostCapability | None, protected: Sequence[str]) -> None:
        if not protected:
            raise ValueError("a server sandbox needs the roots it protects (HIMARI's data root)")
        self.protected = _absolute_roots(tuple(protected))
        self._capability = capability
        self._state = SandboxState.UNAVAILABLE if capability is None else SandboxState.CAPABLE_INACTIVE
        self._started = False

    @classmethod
    def discover(cls, protected: Sequence[str]) -> ServerSandbox:
        return cls(discover_capability(), protected)

    @property
    def state(self) -> SandboxState:
        return self._state

    def setup_failed(self) -> None:
        self._state = SandboxState.SETUP_FAILED  # latched, as for the Workspace backend

    async def start(self, self_test: ServerSelfTest | None = None) -> SandboxState:
        """Once per process: ENFORCED only if the server self-test passes."""
        if self._started:
            raise RuntimeError("the server sandbox starts once per process")
        self._started = True
        if self._state is not SandboxState.CAPABLE_INACTIVE:
            return self._state
        if self_test is None:
            from himari.sandbox.server_self_test import run_server_self_test

            self_test = run_server_self_test
        try:
            passed = await self_test(self)
        except Exception:
            passed = False
        self._state = SandboxState.ENFORCED if passed else SandboxState.SETUP_FAILED
        return self._state

    def launcher(self, profile: ServerProfile) -> ServerLauncher:
        """SandboxProfileError when the profile fails §4.2's configuration checks."""
        return ServerLauncher(self, check_profile(profile, protected=self._roots(profile)))

    def _roots(self, profile: ServerProfile) -> tuple[str, ...]:
        return (*self.protected, *_absolute_roots(profile.protected))

    async def spawn(self, profile: ServerProfile, argv: Sequence[str], env: Mapping[str, str],
                    cwd: str) -> ServerProcess:
        """The one protected spawn of the profile (the launcher's, and the
        self-test's before ENFORCED)."""
        if self._capability is None:
            raise SandboxUnavailableError(SandboxState.UNAVAILABLE.value)
        checked = _check_request(profile, argv, env, cwd)
        fds: list[int] = []
        try:
            binds: list[tuple[str, int, str]] = []
            for option, paths in (("--ro-bind-fd", profile.read_only), ("--bind-fd", profile.writable)):
                for path in paths:
                    fds.append(os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC))
                    binds.append((option, fds[-1], path))
            trees = [identity.BoundTree(fd, option == "--bind-fd") for option, fd, _ in binds]
            check = asyncio.ensure_future(asyncio.to_thread(identity.check_trees, trees, self._roots(profile)))
            if await _settle(check):  # the thread uses the descriptors: they stay open until it ends
                raise asyncio.CancelledError
            check.result()
            return await self._spawn_checked(checked, {**SANDBOX_ENV, **env}, binds, profile.writable, cwd)
        except OSError as error:  # e.g. removed since the profile was checked: this launch only, no latch
            raise SandboxAdmissionError(f"a bound directory could not be opened ({type(error).__name__})") from None
        finally:
            for fd in fds:
                os.close(fd)

    async def _spawn_checked(self, argv: list[str], env: Mapping[str, str], binds: list[tuple[str, int, str]],
                             writable: Sequence[str], cwd: str) -> ServerProcess:
        assert self._capability is not None
        sync_r, sync_w = os.pipe()
        try:
            os.set_blocking(sync_r, False)
            try:
                args = sandbox_arguments(self._capability, env=env, binds=binds, write_roots=[*writable, "/tmp"],
                                         cwd=cwd, sync_fd=sync_w, argv=argv)
            except BaseException:
                os.close(sync_w)
                raise
            spawn = asyncio.ensure_future(asyncio.create_subprocess_exec(
                *args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={},
                pass_fds=(sync_w, *(fd for _, fd, _ in binds)),
                start_new_session=True,
                limit=STREAM_LIMIT,
            ))
            try:
                process = await asyncio.shield(spawn)  # see SandboxBackend.launch: never interrupt a spawn
            except asyncio.CancelledError:
                await _abandon(spawn)
                raise
            except OSError as error:
                raise SandboxSetupError("bwrap could not start") from error
            finally:
                os.close(sync_w)
            try:
                sync = await _wait_sync(sync_r, SETUP_TIMEOUT_SECONDS)
            except BaseException:
                _kill_tree(process.pid)
                await _reap(process)
                raise
            if sync != landlock_helper.SYNC_OK:
                _kill_tree(process.pid)
                await _reap(process)
                raise SandboxSetupError("the sandbox did not report enforcement")
            return ServerProcess(process)
        finally:
            os.close(sync_r)
