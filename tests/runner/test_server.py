"""The runner's wire contract and server (V3-P74 §5), with a fake sandbox."""

from __future__ import annotations

import asyncio
import base64
import os
import socket
import threading
import time
from collections.abc import Iterator
from typing import Any
from pathlib import Path

import pytest
from runnerkit import RunnerKit, start_runner

from himari.environment.machine.client import RunnerClient
from himari.runner import api
from himari.runner.hostfs import Host


@pytest.fixture
def kit(tmp_path: Path) -> Iterator[RunnerKit]:
    started = start_runner(tmp_path)
    yield started
    started.close()


def call(kit: RunnerKit, op: str, args: dict[str, object], fds: tuple[int, ...] = ()) -> tuple[dict[str, object], list[int]]:
    return asyncio.run(RunnerClient(kit.socket).call(op, args, fds))


def test_frames_carry_descriptors() -> None:
    async def exchange() -> None:
        a, b = socket.socketpair()
        a.setblocking(False)
        b.setblocking(False)
        r, w = os.pipe()
        try:
            await api.send_frame(a, {"op": "x", "args": {"n": "é"}}, [r])
            message, fds = await api.recv_frame(b)
            assert message == {"op": "x", "args": {"n": "é"}} and len(fds) == 1
            os.write(w, b"hi")
            assert os.read(fds[0], 2) == b"hi"
            os.close(fds[0])
            # a frame followed by more bytes is refused
            await api.send_frame(a, {"a": 1})
            a.send(b"junk")
            with pytest.raises(api.ProtocolError):
                await api.recv_frame(b)
        finally:
            for fd in (r, w):
                os.close(fd)
            a.close()
            b.close()

    asyncio.run(exchange())


def test_oversized_frames_are_refused() -> None:
    async def exchange() -> None:
        a, b = socket.socketpair()
        a.setblocking(False)
        b.setblocking(False)
        try:
            with pytest.raises(api.ProtocolError):
                await api.send_frame(a, {"x": "y" * api.MAX_FRAME_BYTES})
            a.send((api.MAX_FRAME_BYTES + 1).to_bytes(4, "big"))
            with pytest.raises(api.ProtocolError):
                await api.recv_frame(b)
        finally:
            a.close()
            b.close()

    asyncio.run(exchange())


def test_hello_and_conversation_folder(kit: RunnerKit) -> None:
    hello, _ = call(kit, api.HELLO, {})
    assert hello["state"] == "enforced" and hello["himari_home"] == str(kit.himari_home)
    assert hello["version"] == api.PROTOCOL_VERSION
    assert hello["network"] is None  # no proxy without a started (self-tested) sandbox
    folder, _ = call(kit, api.CONVERSATION_FOLDER, {"conversation": "c1"})
    assert folder["path"] == f"{kit.himari_home}/conversations/c1"
    assert (kit.himari_home / "conversations/c1").is_dir()


def test_another_user_gets_no_answer(tmp_path: Path) -> None:
    kit = start_runner(tmp_path, client_uid=os.getuid() + 1)
    try:
        with pytest.raises(api.RunnerUnavailable) as caught:
            call(kit, api.HELLO, {})
        assert not isinstance(caught.value, api.NotSent)
    finally:
        kit.close()


def test_no_runner_is_not_sent(tmp_path: Path) -> None:
    with pytest.raises(api.NotSent):
        asyncio.run(RunnerClient(str(tmp_path / "missing.sock")).call(api.HELLO, {}))


def test_refusals_carry_a_category(kit: RunnerKit) -> None:
    folder = str(call(kit, api.CONVERSATION_FOLDER, {"conversation": "c1"})[0]["path"])
    with pytest.raises(api.RunnerError) as caught:
        call(kit, api.OPEN, {"path": "~/.ssh/id", "cwd": folder})
    assert caught.value.category == api.NOT_FOUND
    with pytest.raises(api.RunnerError) as caught:
        call(kit, api.WRITE, {"path": "~/projects/a/x", "cwd": folder, "write_roots": [folder], "content": "x"})
    assert caught.value.category == api.OUTSIDE_WORKSPACES and "attach" in str(caught.value)
    with pytest.raises(api.RunnerError) as caught:
        call(kit, api.WRITE, {"path": "x", "cwd": folder, "write_roots": [str(kit.home / ".ssh")], "content": "x"})
    assert caught.value.category == api.ACCESS
    with pytest.raises(api.RunnerError) as caught:
        call(kit, "nope", {"write_roots": [folder]})
    assert caught.value.category == api.INVALID


def test_open_passes_the_file(kit: RunnerKit) -> None:
    result, fds = call(kit, api.OPEN, {"path": "~/projects/a/main.c", "cwd": "/"})
    try:
        assert result["path"] == f"{kit.home}/projects/a/main.c" and result["size"] == 13
        assert os.read(fds[0], 100) == b"int main(){}\n"
    finally:
        os.close(fds[0])


def test_run_gives_the_sandbox_the_checked_layout(kit: RunnerKit) -> None:
    folder = str(call(kit, api.CONVERSATION_FOLDER, {"conversation": "c1"})[0]["path"])
    result, _ = call(kit, api.RUN, {"argv": ["make"], "cwd": folder, "write_roots": [folder], "timeout": 30})
    assert base64.b64decode(str(result["stdout"])) == b"out\n" and result["exit_code"] == 0 and result["oom"] is False
    argv, layout, timeout = kit.sandbox.calls[-1]
    assert argv == ["make"] and timeout == 30 and layout.cwd == folder and layout.home == str(kit.home)
    assert [path for path, _ in layout.writable] == [folder]
    assert {m.path for m in layout.masks} >= {str(kit.home / ".ssh")}
    assert layout.cache is not None and layout.cache[0] == str(kit.home / ".cache/himari-runner")
    with pytest.raises(api.RunnerError) as caught:
        call(kit, api.RUN, {"argv": ["make"], "cwd": folder, "write_roots": [folder], "timeout": 0})
    assert caught.value.category == api.INVALID
    with pytest.raises(api.RunnerError) as caught:
        call(kit, api.RUN, {"argv": ["ls"], "cwd": str(kit.home / ".ssh"), "write_roots": [folder], "timeout": 5})
    assert caught.value.category == api.NOT_FOUND


def test_closing_the_connection_cancels_the_command(kit: RunnerKit) -> None:
    folder = str(call(kit, api.CONVERSATION_FOLDER, {"conversation": "c1"})[0]["path"])
    kit.sandbox.gate = threading.Event()

    async def cancel() -> None:
        task = asyncio.create_task(RunnerClient(kit.socket).call(
            api.RUN, {"argv": ["sleep", "100"], "cwd": folder, "write_roots": [folder], "timeout": 600}))
        deadline = time.monotonic() + 5
        while not kit.sandbox.calls and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel())
    assert kit.sandbox.cancelled.wait(5)


def _slow_writes(kit: RunnerKit, monkeypatch: pytest.MonkeyPatch) -> tuple[threading.Event, threading.Event]:
    """The runner's next file writes block in their thread until ``gate`` is set."""
    entered, gate = threading.Event(), threading.Event()
    real = Host.write

    def slow(*args: object) -> bool:
        entered.set()
        gate.wait(10)
        return real(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(Host, "write", slow)
    return entered, gate


def test_a_cancelled_write_returns_once_the_runner_is_done(kit: RunnerKit, monkeypatch: pytest.MonkeyPatch) -> None:
    """PR #87 review C1: a file operation's thread cannot be stopped, so the
    cancelled client waits for it — the caller's locks outlive the work."""
    folder = str(call(kit, api.CONVERSATION_FOLDER, {"conversation": "c1"})[0]["path"])
    entered, gate = _slow_writes(kit, monkeypatch)

    async def cancel() -> None:
        task = asyncio.create_task(RunnerClient(kit.socket).call(
            api.WRITE, {"path": "slow.txt", "cwd": folder, "write_roots": [folder], "content": "x"}))
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0.2)
        assert not task.done()
        task.cancel()  # cancelled again: still waits
        await asyncio.sleep(0.1)
        assert not task.done()
        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (Path(folder) / "slow.txt").read_text() == "x"

    asyncio.run(cancel())


def test_a_cancelled_command_returns_once_it_is_reaped(kit: RunnerKit) -> None:
    folder = str(call(kit, api.CONVERSATION_FOLDER, {"conversation": "c1"})[0]["path"])
    kit.sandbox.gate = threading.Event()
    kit.sandbox.reap_seconds = 0.3

    async def cancel() -> None:
        task = asyncio.create_task(RunnerClient(kit.socket).call(
            api.RUN, {"argv": ["sleep", "100"], "cwd": folder, "write_roots": [folder], "timeout": 600}))
        deadline = time.monotonic() + 5
        while not kit.sandbox.calls and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert kit.sandbox.events == ["reaped"]

    asyncio.run(cancel())


def test_a_stopping_runner_closes_only_after_the_work(kit: RunnerKit, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = str(call(kit, api.CONVERSATION_FOLDER, {"conversation": "c1"})[0]["path"])
    entered, gate = _slow_writes(kit, monkeypatch)

    async def stop() -> None:
        task = asyncio.create_task(RunnerClient(kit.socket).call(
            api.WRITE, {"path": "slow.txt", "cwd": folder, "write_roots": [folder], "content": "x"}))
        assert await asyncio.to_thread(entered.wait, 5)
        kit.loop.call_soon_threadsafe(kit.stop.set)
        await asyncio.sleep(0.2)
        assert not task.done()  # the connection is still open while the thread works
        gate.set()
        with pytest.raises(api.RunnerUnavailable):
            await task  # no answer: the runner stopped, but only after the write ended
        assert (Path(folder) / "slow.txt").read_text() == "x"

    asyncio.run(stop())


def test_not_enforced_refuses_everything_but_hello(tmp_path: Path) -> None:
    """§13 (PR #87 review B1): a failed self-test stops files too, not only commands."""
    kit = start_runner(tmp_path, enforced=False)
    try:
        assert call(kit, api.HELLO, {})[0]["state"] == "setup_failed"
        folder = str(kit.himari_home / "conversations")
        roots = {"write_roots": [folder]}
        r, w = os.pipe()
        os.close(w)
        requests: list[tuple[str, dict[str, object], tuple[int, ...]]] = [
            (api.CONVERSATION_FOLDER, {"conversation": "c1"}, ()),
            (api.LIST, {"path": "~", "cwd": "/"}, ()),
            (api.OPEN, {"path": "~/projects/a/main.c", "cwd": "/"}, ()),
            (api.HOLDS, {"path": "~/projects/a/main.c", "size": 1, "sha256": "x"}, ()),
            (api.WRITE, {"path": "x", "cwd": folder, "content": "x", **roots}, ()),
            (api.DELETE, {"path": "x", "cwd": folder, **roots}, ()),
            (api.RUN, {"argv": ["true"], "cwd": folder, "timeout": 5, **roots}, ()),
            (api.COPY_IN, {"folder": f"{folder}/u", "clear": True, **roots}, (r,)),
            (api.FREE_PATH, {"folder": f"{folder}/u", "name": "a", **roots}, ()),
            (api.REMOVE_CONVERSATION_FOLDER, {"conversation": "c1"}, ()),
            (api.CHECK_WORKSPACE, {"path": "~/projects/a", "others": []}, ()),
        ]
        try:
            for op, args, fds in requests:
                with pytest.raises(api.RunnerError) as caught:
                    call(kit, op, args, fds)
                assert caught.value.category == api.UNAVAILABLE, op
        finally:
            os.close(r)
        assert not (kit.home / "HIMARI/conversations/x").exists() and not (kit.home / "HIMARI/conversations/u").exists()
        assert not kit.sandbox.calls
    finally:
        kit.close()


def test_check_workspace(kit: RunnerKit) -> None:
    """V3-P74 §10.1: a read-only check of a folder to register."""
    checked, _ = call(kit, api.CHECK_WORKSPACE, {"path": "~/projects/a", "others": [str(kit.home / "projects/a")]})
    assert checked["path"] == str(kit.home / "projects/a") and checked["same_as"] is None  # its own path is not "another"
    with pytest.raises(api.RunnerError) as caught:
        call(kit, api.CHECK_WORKSPACE, {"path": "~/.ssh", "others": []})
    assert caught.value.category == api.NOT_FOUND
    assert api.CHECK_WORKSPACE not in api.WRITES


def test_upload_steps(kit: RunnerKit) -> None:
    folder = str(call(kit, api.CONVERSATION_FOLDER, {"conversation": "c1"})[0]["path"])
    uploads = f"{folder}/uploads"
    base = {"folder": uploads, "write_roots": [folder]}
    r, w = os.pipe()
    os.write(w, b"data")
    os.close(w)
    try:
        temp = str(call(kit, api.COPY_IN, {**base, "clear": True}, (r,))[0]["temp"])
    finally:
        os.close(r)
    path = str(call(kit, api.FREE_PATH, {**base, "name": "a.txt", "preferred": None})[0]["path"])
    assert path == f"{uploads}/a.txt"
    assert call(kit, api.COMMIT_COPY, {**base, "temp": temp, "path": path})[0]["placed"] is True
    assert Path(path).read_bytes() == b"data"
    import hashlib

    digest = hashlib.sha256(b"data").hexdigest()
    assert call(kit, api.HOLDS, {"path": path, "size": 4, "sha256": digest})[0]["holds"] is True
    assert call(kit, api.FREE_PATH, {**base, "name": "a.txt", "preferred": path})[0]["path"] == f"{uploads}/a (2).txt"
    call(kit, api.DISCARD_COPY, {**base, "temp": temp})
    assert call(kit, api.REMOVE_CONVERSATION_FOLDER, {"conversation": "c1"})[0]["removed"] is True
    assert not Path(folder).exists()


def test_slow_reads_are_given_up_and_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PR #92 review B1: a listing, stat or open that hangs (a stuck mount) answers TIMED_OUT in time; at most
    MAX_READS threads wait on it, a further read is refused at once; an open given up closes its fd."""
    from himari.runner import server as runner_server

    monkeypatch.setattr(runner_server, "READ_SECONDS", 0.3)
    monkeypatch.setattr(runner_server, "MAX_READS", 2)
    gate = threading.Event()
    listing, open_read = Host.listing, Host.open_read

    def stuck_listing(self: Host, *args: Any) -> Any:
        gate.wait(10)
        return listing(self, *args)

    def stuck_open(self: Host, *args: Any) -> Any:
        gate.wait(10)
        return open_read(self, *args)

    monkeypatch.setattr(Host, "listing", stuck_listing)
    monkeypatch.setattr(Host, "open_read", stuck_open)
    kit = start_runner(tmp_path)
    try:
        fds_before = len(os.listdir("/proc/self/fd"))
        reads: list[tuple[str, dict[str, object]]] = [(api.LIST, {"path": "~", "cwd": "/"}),
                                                      (api.OPEN, {"path": "~/projects/a/main.c", "cwd": "/"})]
        for op, args in reads:
            started = time.monotonic()
            with pytest.raises(api.RunnerError) as given_up:
                call(kit, op, args)
            assert given_up.value.category == api.TIMED_OUT and time.monotonic() - started < 3
        with pytest.raises(api.RunnerError, match="earlier slow reads") as busy:
            call(kit, api.STAT, {"path": "~", "cwd": "/"})  # both read threads still wait on the mount
        assert busy.value.category == api.TIMED_OUT
        gate.set()
        deadline = time.monotonic() + 5
        while kit.runner._reads and time.monotonic() < deadline:
            time.sleep(0.05)
        assert kit.runner._reads == 0
        assert len(os.listdir("/proc/self/fd")) <= fds_before  # the given-up open's fd was closed
        result, _ = call(kit, api.LIST, {"path": "~", "cwd": "/"})
        entries = result["entries"]
        assert isinstance(entries, list) and any(e["name"] == "projects" for e in entries)
    finally:
        gate.set()
        kit.close()
