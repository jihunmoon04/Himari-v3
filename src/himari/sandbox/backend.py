"""bubblewrap + Landlock protected spawn (adapted from HIMARI-v2
``sandbox/linux_backend.py``; no v2 import, no Task/Work authority).

The server profile's mechanics: the host capability, the bwrap arguments,
and the steps of a spawn that never lets the process outlive its call
(waiting for the helper's report, killing and reaping the tree). There is
no unsandboxed fallback in any state.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from typing import Any
from dataclasses import dataclass

from himari.sandbox import landlock_helper

BWRAP_CANDIDATES = ("/usr/bin/bwrap", "/bin/bwrap")
INTERPRETER = "/usr/bin/python3"  # system interpreter, visible inside the /usr view
HELPER_IN_SANDBOX = "/run/himari/landlock_helper.py"

SETUP_TIMEOUT_SECONDS = 15.0
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
    never completes keeps the call, and so the lock, held: fail closed.

    The spawn itself is never interrupted: asyncio's own cleanup of a
    cancelled spawn SIGKILLs only the outer bwrap, which can orphan its
    child while that still waits for the uid map (before --die-with-parent
    applies) and then hang on the child's copy of the pipes. Instead the
    spawn finishes and the whole process group, which the child still
    shares, is killed."""
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
