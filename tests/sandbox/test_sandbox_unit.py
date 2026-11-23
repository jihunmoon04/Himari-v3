"""Sandbox mechanics that need no bubblewrap (runs in CI): argv checks, the
mount check of the self-tests, and that no process outlives a protected
spawn of the server profile on any exit path."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from himari.environment.machine.blocking import run_blocking
from himari.sandbox import backend as sb
from himari.sandbox import self_test
from himari.sandbox import server as srv
from himari.sandbox.api import MAX_ARG_CHARS, MAX_ARGV, SandboxSetupError, validate_argv


@pytest.mark.parametrize(
    "argv",
    [[], "ls", ["ls", ""], ["x"] * (MAX_ARGV + 1), ["a" * (MAX_ARG_CHARS + 1)], ["ls", "a\0b"], [1]],
)
def test_invalid_argv_is_rejected(argv: Any) -> None:
    with pytest.raises(ValueError):
        validate_argv(argv)


# --- PR #9 review B1: no process outlives a protected spawn on any exit path -------------

FAKE_SANDBOX = r"""
import os, sys, time, subprocess
sync_fd, pid_file, mode = int(sys.argv[1]), sys.argv[2], sys.argv[3]
child = subprocess.Popen(["sleep", "300"])
with open(pid_file, "w") as handle:
    handle.write(f"{os.getpid()} {child.pid}")
if mode == "no-sync":
    os.close(sync_fd)
time.sleep(300)  # "hold": the report never comes, the sync pipe stays open
"""

CAP = sb.HostCapability(bwrap="/usr/bin/bwrap", landlock_abi=8, helper="/srv/himari/landlock_helper.py")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    with open(f"/proc/{pid}/stat") as handle:  # a zombie is dead for our purposes
        return handle.read().split(") ")[1][0] != "Z"


def _spawn(monkeypatch: pytest.MonkeyPatch, build: Callable[[int], list[str]]) -> Callable[[], Any]:
    """The server profile's own spawn, with ``build(sync_fd)`` in place of the bwrap arguments."""
    monkeypatch.setattr(srv, "sandbox_arguments", lambda capability, **kw: build(kw["sync_fd"]))
    sandbox = srv.ServerSandbox(CAP, ["/nonexistent-data"])
    return lambda: sandbox._spawn_checked(["x"], {}, [], [], "/")


@pytest.mark.parametrize("mode", ["cancel", "no-sync", "setup-timeout"])
def test_no_process_outlives_a_protected_spawn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    pid_file = tmp_path / "pids"
    monkeypatch.setattr(srv, "SETUP_TIMEOUT_SECONDS", 1.0 if mode == "setup-timeout" else 60.0)
    spawn = _spawn(monkeypatch, lambda sync_fd: [sys.executable, "-c", FAKE_SANDBOX, str(sync_fd), str(pid_file),
                                                 "no-sync" if mode == "no-sync" else "hold"])

    async def scenario() -> None:
        task = asyncio.create_task(spawn())
        for _ in range(100):
            if pid_file.exists() and pid_file.read_text():
                break
            await asyncio.sleep(0.05)
        if mode == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(SandboxSetupError):
                await task

    asyncio.run(scenario())
    pids = [int(p) for p in pid_file.read_text().split()]
    deadline = time.monotonic() + 3
    while any(_alive(p) for p in pids) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not any(_alive(p) for p in pids), f"{mode}: survivors {pids}"



# --- the self-tests' mount check ------------------------------------------------------------


def test_mount_allowlist_flags_any_extra_mount() -> None:
    binds = {p: (64, 100 + n) for n, p in enumerate(sorted(["/usr", "/etc/alternatives", "/run/himari/landlock_helper.py", "/home/user"]))}
    devices = {p: 0x100 + n for n, p in enumerate(sorted(self_test.DEVICE_NODES))}
    expected = self_test.ExpectedSources(binds, devices)
    fresh = [[p, t, r, False, [1, 1, 0]] for p, (t, r) in self_test.FRESH_MOUNTS.items()]
    nodes = [[p, "devtmpfs", "/" + p.rsplit("/", 1)[1], True, [5, 9, rdev]] for p, rdev in devices.items()]
    bound = [[p, "ext4", p, False, [dev, ino, 0]] for p, (dev, ino) in binds.items()]
    correct = fresh + nodes + bound + [["/proc/sys", "proc", "/sys", False, [2, 2, 0]]]

    def unexpected(table: object) -> list[str]:
        return self_test._unexpected_mounts(table, expected)

    def swap(point: str, entry: list[object]) -> list[list[object]]:
        return [e if e[0] != point else entry for e in correct]

    assert unexpected(correct) == []
    assert unexpected([*correct, ["/etc", "ext4", "/etc", False, [64, 7, 0]]]) == ["/etc"]
    assert unexpected([*correct, ["/dev/host-data", "ext4", "/etc", False, [64, 7, 0]]]) == ["/dev/host-data"]  # B4
    assert unexpected([*correct, ["/proc/host-data", "ext4", "/srv", False, [64, 8, 0]]]) == ["/proc/host-data"]
    assert unexpected([*correct, ["/usr", "ext4", "/etc", False, [64, 7, 0]]]) == ["/usr (stacked)"]
    # an allowed point with another source: host /etc bound at /etc/alternatives
    assert unexpected(swap("/etc/alternatives", ["/etc/alternatives", "ext4", "/etc", False, [64, 7, 0]])) == ["/etc/alternatives"]
    assert unexpected(swap("/dev/null", ["/dev/null", "ext4", "/etc/passwd", False, [64, 9, 0]])) == ["/dev/null"]
    other_device = devices["/dev/zero"]
    assert unexpected(swap("/dev/null", ["/dev/null", "devtmpfs", "/zero", True, [5, 9, other_device]])) == ["/dev/null"]
    assert unexpected(swap("/tmp", ["/tmp", "tmpfs", "/user/1000", False, [1, 1, 0]])) == ["/tmp"]
    assert unexpected(None) == ["<unreadable>"]
    assert "/etc/passwd" in self_test.DEFAULT_PROTECTED and "/home" in self_test.DEFAULT_PROTECTED



# --- PR #9 re-review N1: cancellation while the process is being created ---------------


def _open_fds() -> int:
    return len(os.listdir("/proc/self/fd"))


def test_cancel_during_spawn_closes_the_sync_pipe(monkeypatch: pytest.MonkeyPatch) -> None:
    async def slow_failing_spawn(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        await asyncio.sleep(0.02)
        raise OSError("spawn did not happen")

    spawn = _spawn(monkeypatch, lambda sync_fd: ["x"])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", slow_failing_spawn)

    async def scenario() -> None:
        for _ in range(20):
            task = asyncio.create_task(spawn())
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    before = _open_fds()
    asyncio.run(scenario())
    assert _open_fds() == before


def test_cancel_during_a_real_spawn_leaves_no_fd_or_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pid_dir = tmp_path / "pids"
    pid_dir.mkdir()
    counter = iter(range(1000))
    spawn = _spawn(monkeypatch, lambda sync_fd: [sys.executable, "-c", FAKE_SANDBOX, str(sync_fd),
                                                 str(pid_dir / str(next(counter))), "hold"])

    async def scenario() -> None:
        for delay in [0.0] * 15 + [0.01, 0.03, 0.1, 0.3]:
            task = asyncio.create_task(spawn())
            await asyncio.sleep(delay)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 15)  # a hang here fails as TimeoutError

    before = _open_fds()
    asyncio.run(scenario())
    assert _open_fds() == before
    pids = [int(p) for f in pid_dir.iterdir() if f.read_text() for p in f.read_text().split()]
    deadline = time.monotonic() + 3
    while any(_alive(p) for p in pids) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not any(_alive(p) for p in pids)


# --- PR #9 re-review B5: a delayed spawn never outlives the cancelled call --------------


def test_cancelled_call_returns_only_after_a_late_spawned_process_is_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    real_spawn = asyncio.create_subprocess_exec
    spawned: list[int] = []

    async def late_spawn(*args: object, **kwargs: Any) -> asyncio.subprocess.Process:
        await asyncio.sleep(1.0)  # longer than the cancellations below take
        process = await real_spawn(sys.executable, "-c", "import time; time.sleep(300)", stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        spawned.append(process.pid)
        return process

    spawn = _spawn(monkeypatch, lambda sync_fd: ["x"])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", late_spawn)

    async def scenario() -> None:
        task = asyncio.create_task(spawn())
        await asyncio.sleep(0.1)
        for _ in range(3):  # repeated cancellation must not cut the wait short
            task.cancel()
            await asyncio.sleep(0.2)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert spawned and not _alive(spawned[0]), "the call returned while its process could still run"

    asyncio.run(scenario())


# --- PR #11 review B1: started blocking work always completes before the call ends -------


def test_a_cancelled_call_waits_for_its_worker_thread() -> None:
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def work() -> None:
        entered.set()
        release.wait(10)
        finished.set()

    async def scenario() -> None:
        task = asyncio.create_task(run_blocking(work))
        while not entered.is_set():
            await asyncio.sleep(0.01)
        for _ in range(3):  # repeated cancellation must not cut the wait short
            task.cancel()
            await asyncio.sleep(0.05)
            assert not task.done(), "the call ended while its thread was still working"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()

    asyncio.run(scenario())
