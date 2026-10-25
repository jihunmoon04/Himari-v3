"""bubblewrap + Landlock protected spawn (adapted from HIMARI-v2
``sandbox/linux_backend.py``; no v2 import, no Task/Work authority).

States::

    discover: no capability -> UNAVAILABLE ; capability -> CAPABLE_INACTIVE
    start():  real-spawn self-test passes -> ENFORCED ; fails -> SETUP_FAILED
    run():    a setup failure latches SETUP_FAILED

Nothing promotes back to ENFORCED within a process, and there is no
unsandboxed fallback in any state.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any
from dataclasses import dataclass

from himari.sandbox import landlock_helper
# Re-exported: the backend module still answers for the names it held before
# the move to himari.sandbox (V3-P59 §3: tests change only their imports).
from himari.sandbox.api import (
    DEFAULT_TIMEOUT_SECONDS as DEFAULT_TIMEOUT_SECONDS,
    MAX_ARG_CHARS as MAX_ARG_CHARS,
    MAX_ARGV as MAX_ARGV,
    MAX_OUTPUT_BYTES as MAX_OUTPUT_BYTES,
    MAX_TIMEOUT_SECONDS as MAX_TIMEOUT_SECONDS,
    TOOLKIT_MOUNT as TOOLKIT_MOUNT,
    WORKSPACE_MOUNT as WORKSPACE_MOUNT,
    CommandResult as CommandResult,
    SandboxSetupError as SandboxSetupError,
    SandboxState as SandboxState,
    SandboxUnavailableError as SandboxUnavailableError,
    validate_argv as validate_argv,
)

BWRAP_CANDIDATES = ("/usr/bin/bwrap", "/bin/bwrap")
INTERPRETER = "/usr/bin/python3"  # system interpreter, visible inside the /usr view
HELPER_IN_SANDBOX = "/run/himari/landlock_helper.py"

SETUP_TIMEOUT_SECONDS = 15.0
_DRAIN_GRACE_SECONDS = 5.0
SANDBOX_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": "/tmp/home",
    "TMPDIR": "/tmp",
    "LANG": "C.UTF-8",
    "TERM": "dumb",
}


@dataclass(frozen=True)
class HostCapability:
    bwrap: str
    landlock_abi: int
    helper: str


def _bwrap_path() -> str | None:
    for candidate in BWRAP_CANDIDATES:
        try:
            info = os.stat(candidate)
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode) and info.st_mode & 0o111 and not info.st_mode & (stat.S_ISUID | stat.S_ISGID):
            return candidate
    return None


def system_view() -> list[str]:
    """bwrap arguments for the minimal read-only system: /usr plus the
    merged-/usr top-level links, and a few /etc files programs need."""
    args = ["--ro-bind", "/usr", "/usr"]
    for name in ("bin", "sbin", "lib", "lib64", "lib32", "libx32"):
        host = f"/{name}"
        if os.path.islink(host):
            args += ["--symlink", os.readlink(host), host]
        elif os.path.isdir(host):
            args += ["--ro-bind", host, host]
    for etc in ("/etc/ld.so.cache", "/etc/alternatives", "/etc/localtime"):
        if os.path.exists(etc):
            args += ["--ro-bind", etc, etc]
    return args


def sandbox_arguments(capability: HostCapability, *, env: Mapping[str, str], binds: Sequence[tuple[str, int, str]],
                      write_roots: Sequence[str], cwd: str, sync_fd: int, argv: Sequence[str],
                      tmp_bytes: int | None = None) -> list[str]:
    """The one bwrap layout every profile uses: fresh namespaces (network
    included), a read-only root with the system view, the helper, fresh
    /dev, /proc and /tmp; then the profile's ``binds`` — each ``(option,
    fd, destination)`` with ``--bind-fd`` or ``--ro-bind-fd``, so a verified
    descriptor is bound and no path is resolved again. ``argv`` only appears
    after the helper's ``--`` separator."""
    policy = {"read_roots": ["/"], "write_roots": list(write_roots), "device_root": "/dev", "sync_fd": sync_fd}
    args = [
        capability.bwrap,
        "--unshare-all",  # user, ipc, pid, net, uts, cgroup
        "--unshare-user",  # required, not best-effort
        "--die-with-parent",
        "--new-session",
        "--disable-userns",
        "--cap-drop", "ALL",
        "--clearenv",
    ]
    for key, value in env.items():
        args += ["--setenv", key, value]
    args += system_view()
    args += [
        "--dev", "/dev",
        "--proc", "/proc",
        *(("--size", str(tmp_bytes)) if tmp_bytes is not None else ()),
        "--tmpfs", "/tmp",
        "--dir", "/tmp/home",
        "--ro-bind", capability.helper, HELPER_IN_SANDBOX,
    ]
    for option, fd, destination in binds:
        if option not in ("--bind-fd", "--ro-bind-fd"):
            raise ValueError(f"not a descriptor bind: {option}")
        args += [option, str(fd), destination]
    args += [
        "--remount-ro", "/",
        "--chdir", cwd,
        "--",
        INTERPRETER, "-I", "-S", HELPER_IN_SANDBOX, json.dumps(policy, separators=(",", ":")),
        "--",
        *argv,
    ]
    return args


def toolkit_env() -> dict[str, str]:
    """What the Workspace profile adds when the documents toolkit is bound
    (V3-P70 §6.3) — never part of SANDBOX_ENV, which server profiles may
    not override (review N1)."""
    return {"PYTHONPATH": f"{TOOLKIT_MOUNT}/site-packages", "HIMARI_FONTS": f"{TOOLKIT_MOUNT}/fonts"}


def bwrap_arguments(capability: HostCapability, root_fd: int, cwd: str, sync_fd: int, argv: list[str],
                    toolkit_fd: int | None = None) -> list[str]:
    """The Workspace profile: the verified Workspace root fd at /workspace,
    writable with /tmp, a cwd under /workspace, the fixed environment; with
    the documents toolkit, its folder read-only at TOOLKIT_MOUNT and the two
    variables that point there."""
    binds = [("--bind-fd", root_fd, WORKSPACE_MOUNT)]
    env = dict(SANDBOX_ENV)
    if toolkit_fd is not None:
        binds.append(("--ro-bind-fd", toolkit_fd, TOOLKIT_MOUNT))
        env.update(toolkit_env())
    return sandbox_arguments(capability, env=env, binds=binds, write_roots=[WORKSPACE_MOUNT, "/tmp"], cwd=cwd,
                             sync_fd=sync_fd, argv=argv)


def supports_tmp_size(capability: HostCapability) -> bool:
    """Whether this bwrap knows --size (0.9.0 does, 0.6 does not: V3-P70 re-review N4)."""
    try:
        completed = subprocess.run([capability.bwrap, "--help"], stdin=subprocess.DEVNULL, capture_output=True,
                                   env={}, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return b"--size" in completed.stdout + completed.stderr


def discover_capability() -> HostCapability | None:
    """Read-only checks plus one harmless namespace probe. Never activation."""
    if not sys.platform.startswith("linux"):
        return None
    bwrap = _bwrap_path()
    abi = landlock_helper.landlock_abi()
    helper = os.path.realpath(landlock_helper.__file__)
    if bwrap is None or abi < landlock_helper.MIN_LANDLOCK_ABI or not os.path.isfile(helper):
        return None
    if not os.path.realpath(INTERPRETER).startswith("/usr/") or not os.access(INTERPRETER, os.X_OK):
        return None
    probe = [bwrap, "--unshare-all", "--unshare-user", "--die-with-parent", *system_view(),
             "--proc", "/proc", "--dev", "/dev", "--", "/usr/bin/true"]
    try:
        completed = subprocess.run(probe, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, env={}, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return HostCapability(bwrap, abi, helper) if completed.returncode == 0 else None


async def _drain(stream: asyncio.StreamReader | None, limit: int) -> tuple[bytes, int]:
    """Keep the first ``limit`` bytes, count (and discard) the rest so the
    command never blocks on a full pipe."""
    if stream is None:
        return b"", 0
    kept = bytearray()
    total = 0
    while chunk := await stream.read(65536):
        total += len(chunk)
        if len(kept) < limit:
            kept += chunk[: limit - len(kept)]
    return bytes(kept), total


async def _spool(stream: asyncio.StreamReader | None, sink: int, limit: int,
                 overflow: Callable[[], None]) -> tuple[bytes, int]:
    """Write the first ``limit`` bytes to the file ``sink`` (on a thread: a
    slow disk does not hold up the loop), then call ``overflow`` (which
    stops the command) and count what is left until the end of the stream."""
    if stream is None:
        return b"", 0
    total = 0
    while chunk := await stream.read(1 << 20):
        if total <= limit:
            await asyncio.to_thread(_write_all, sink, chunk[: limit - total])
            if total + len(chunk) > limit:
                overflow()
        total += len(chunk)
    return b"", total


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


async def _wait_sync(fd: int, timeout: float) -> bytes:
    """Read the helper's report from a non-blocking fd via the event loop
    (no thread left blocked on an fd that is about to be closed). EOF or
    timeout ends the wait."""
    loop = asyncio.get_running_loop()
    data = bytearray()
    done: asyncio.Future[None] = loop.create_future()

    def readable() -> None:
        try:
            chunk = os.read(fd, 64)
        except BlockingIOError:
            return
        except OSError:
            chunk = b""
        data.extend(chunk)
        if (not chunk or len(data) >= len(landlock_helper.SYNC_OK)) and not done.done():
            done.set_result(None)

    loop.add_reader(fd, readable)
    try:
        await asyncio.wait_for(done, timeout)
    except TimeoutError:
        pass
    finally:
        loop.remove_reader(fd)
    return bytes(data)


async def _settle(future: asyncio.Future[Any]) -> bool:
    """Wait until ``future`` is done, however long, even if the current task
    is cancelled meanwhile; returns whether a cancellation arrived."""
    interrupted = False
    while not future.done():
        try:
            await asyncio.wait([future])  # never cancels ``future`` itself
        except asyncio.CancelledError:
            interrupted = True
    return interrupted


async def _abandon(spawn: asyncio.Future[asyncio.subprocess.Process]) -> None:
    """After a cancelled spawn: wait for the creation to finish (no grace
    period — returning earlier would release the Workspace lock while the
    process may still start), then kill the group and reap it. A spawn that
    never completes keeps the call, and so the lock, held: fail closed."""
    await _settle(spawn)
    if not spawn.cancelled() and spawn.exception() is None:
        process = spawn.result()
        _kill_tree(process.pid)
        await _reap(process)


async def _reap(process: asyncio.subprocess.Process) -> None:
    """Wait, without a time limit and through further cancellations, until
    the killed process has exited and its pipes are closed (every member of
    the tree is gone); a cancellation that arrived is re-raised after."""
    if await _settle(asyncio.ensure_future(process.wait())):
        raise asyncio.CancelledError


def _kill_tree(pid: int) -> None:
    """bwrap leads its own process group; killing it tears down the PID
    namespace (--die-with-parent), so every descendant dies with it."""
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


SelfTest = Callable[["SandboxBackend", tuple[str, ...]], Awaitable[bool]]


class SandboxBackend:
    def __init__(self, capability: HostCapability | None, *, toolkit_fd: int | None = None) -> None:
        self._capability = capability
        # V3-P70 §6.3: the documents toolkit's checked folder, bound read-only
        # in every command and the self-test alike; its owner closes it.
        self.toolkit_fd = toolkit_fd
        self._state = SandboxState.UNAVAILABLE if capability is None else SandboxState.CAPABLE_INACTIVE
        self._started = False

    @classmethod
    def discover(cls, *, toolkit_fd: int | None = None) -> SandboxBackend:
        return cls(discover_capability(), toolkit_fd=toolkit_fd)

    @property
    def state(self) -> SandboxState:
        return self._state

    async def start(self, self_test: SelfTest | None = None, *, protected_paths: Sequence[str] = ()) -> SandboxState:
        """Once per process (V3-050): ENFORCED only if the real-spawn
        self-test passes. ``protected_paths`` are real host roots (Workspace
        root, data root, ...) the self-test proves invisible inside."""
        if self._started:
            raise RuntimeError("the sandbox backend starts once per process")
        self._started = True
        if self._state is not SandboxState.CAPABLE_INACTIVE:
            return self._state
        if self_test is None:
            from himari.sandbox.self_test import run_self_test

            self_test = run_self_test
        try:
            passed = await self_test(self, tuple(protected_paths))
        except Exception:
            passed = False
        self._state = SandboxState.ENFORCED if passed else SandboxState.SETUP_FAILED
        return self._state

    async def run(self, argv: list[str], *, root_fd: int, cwd: str, timeout: float) -> CommandResult:
        """The runtime entry: refuses unless ENFORCED; never unsandboxed."""
        if self._state is not SandboxState.ENFORCED:
            raise SandboxUnavailableError(self._state.value)
        try:
            return await self.launch(argv, root_fd=root_fd, cwd=cwd, timeout=timeout)
        except SandboxSetupError:
            self._state = SandboxState.SETUP_FAILED  # latched
            raise

    async def launch(self, argv: list[str], *, root_fd: int, cwd: str, timeout: float) -> CommandResult:
        """The one protected spawn (used by ``run`` and by the self-test).
        Returns only after the helper reported enforcement; otherwise the
        tree is killed and SandboxSetupError raised (argv never ran)."""
        capability = self._capability
        if capability is None:
            raise SandboxUnavailableError(SandboxState.UNAVAILABLE.value)
        if not cwd.startswith(WORKSPACE_MOUNT):
            raise ValueError("cwd must be under /workspace")
        argv = validate_argv(argv)
        toolkit_fd = self.toolkit_fd
        return await protected_run(
            lambda sync_w: bwrap_arguments(capability, root_fd, cwd, sync_w, argv, toolkit_fd),
            (root_fd, *(() if toolkit_fd is None else (toolkit_fd,))), timeout)


async def protected_run(build: Callable[[int], list[str]], pass_fds: Sequence[int], timeout: float, *,
                        max_output: int = MAX_OUTPUT_BYTES, stdout_to: int | None = None) -> CommandResult:
    """Spawn ``build(sync_w)`` (bwrap arguments around the helper) with only
    ``pass_fds`` and the sync pipe passed, wait for the helper's report and
    supervise the command: the tree never outlives this call (V3-P41).
    Every profile's spawn goes through here. ``stdout_to``: a file the
    stdout goes to (its first ``max_output`` bytes) instead of the result."""
    sync_r, sync_w = os.pipe()
    try:
        os.set_blocking(sync_r, False)
        started = time.monotonic()
        spawn = asyncio.ensure_future(asyncio.create_subprocess_exec(
            *build(sync_w),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={},
            pass_fds=(sync_w, *pass_fds),
            start_new_session=True,
        ))
        try:
            # The spawn itself is never interrupted: asyncio's own cleanup
            # of a cancelled spawn SIGKILLs only the outer bwrap, which can
            # orphan its child while that still waits for the uid map
            # (before --die-with-parent applies) and then hang on the
            # child's copy of the pipes. Instead let the spawn finish and
            # kill the whole process group, which the child still shares.
            process = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            await _abandon(spawn)
            raise
        except OSError as error:
            raise SandboxSetupError("bwrap could not start") from error
        finally:
            # Both ends are closed on every path, cancellation included.
            os.close(sync_w)
        return await _supervise(process, sync_r, timeout, started, max_output, stdout_to)
    finally:
        os.close(sync_r)


async def _supervise(process: asyncio.subprocess.Process, sync_r: int, timeout: float, started: float,
                     max_output: int, stdout_to: int | None = None) -> CommandResult:
    tasks: list[asyncio.Task[tuple[bytes, int]]] = []
    finished = False
    try:
        sync = await _wait_sync(sync_r, SETUP_TIMEOUT_SECONDS)
        if sync != landlock_helper.SYNC_OK:
            raise SandboxSetupError("the sandbox did not report enforcement")

        tasks = [
            asyncio.create_task(_drain(process.stdout, max_output) if stdout_to is None
                                else _spool(process.stdout, stdout_to, max_output, lambda: _kill_tree(process.pid))),
            asyncio.create_task(_drain(process.stderr, MAX_OUTPUT_BYTES)),
        ]
        timed_out = False
        try:
            await asyncio.wait_for(process.wait(), timeout)
        except TimeoutError:
            timed_out = True
            _kill_tree(process.pid)
            await process.wait()
        try:
            (stdout, stdout_total), (stderr, stderr_total) = await asyncio.wait_for(
                asyncio.gather(*tasks), _DRAIN_GRACE_SECONDS
            )
        except TimeoutError:
            stdout = stderr = b""
            stdout_total = stderr_total = 0
        finished = True
    finally:
        if not finished:
            # Cancellation, setup failure or any error after spawn: the
            # command must not outlive this call (it could keep writing
            # after the Workspace lock is released).
            _kill_tree(process.pid)
            await _reap(process)
        for task in tasks:
            if not task.done():
                task.cancel()
    return CommandResult(
        exit_code=None if timed_out else process.returncode,
        timed_out=timed_out,
        stdout=stdout,
        stderr=stderr,
        stdout_total=stdout_total,
        stderr_total=stderr_total,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )
