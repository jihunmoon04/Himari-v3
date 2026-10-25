"""An in-process ``himari-runner`` for tests (V3-P74): a real server, real
file operations on a temporary home, and a fake sandbox that records each
command instead of running it. The server runs on its own thread and loop,
so a test may call it from ``asyncio.run`` or from another server's loop."""

from __future__ import annotations

import asyncio
import dataclasses
import os
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

from himari.runner.config import parse
from himari.runner.hidden import HiddenRules
from himari.runner.hostfs import Host
from himari.runner.server import Runner, listener
from himari.sandbox.api import CommandResult, SandboxState
from himari.sandbox.machine import MachineLayout, MachineSandbox


class FakeSandbox(MachineSandbox):
    def __init__(self, enforced: bool = True) -> None:
        super().__init__(None)
        self._state = SandboxState.ENFORCED if enforced else SandboxState.SETUP_FAILED
        self.calls: list[tuple[list[str], MachineLayout, float]] = []
        self.gate: threading.Event | None = None  # set: the command blocks until it is set
        self.cancelled = threading.Event()
        self.reap_seconds = 0.0  # how long a cancelled command takes to be reaped
        self.events: list[str] = []  # "reaped" once a cancelled command is gone
        self.result = CommandResult(0, False, b"out\n", b"", 4, 0, 5)
        self.execute = False  # True: run the command for real, unsandboxed, in its cwd (git push's steps)

    async def run(self, argv: list[str], layout: MachineLayout, timeout: float, *,
                  max_output: int = 0, stdout_to: int | None = None) -> CommandResult:
        self.calls.append((argv, layout, timeout))
        if self.execute:
            done = await asyncio.to_thread(
                subprocess.run, argv, cwd=layout.cwd, stdin=subprocess.DEVNULL, timeout=timeout,
                stdout=subprocess.PIPE if stdout_to is None else stdout_to, stderr=subprocess.PIPE)
            out = done.stdout or b""
            total = len(out) if stdout_to is None else os.fstat(stdout_to).st_size
            return CommandResult(done.returncode, False, out, done.stderr, total, len(done.stderr), 1)
        try:
            while self.gate is not None and not self.gate.is_set():
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            self.cancelled.set()
            await asyncio.sleep(self.reap_seconds)
            self.events.append("reaped")
            raise
        return self.result


@dataclass
class RunnerKit:
    home: Path
    socket: str
    runner: Runner
    sandbox: FakeSandbox
    loop: asyncio.AbstractEventLoop
    thread: threading.Thread
    stop: asyncio.Event
    errors: list[BaseException] = field(default_factory=list)

    @property
    def himari_home(self) -> Path:
        return Path(self.runner.config.himari_home)

    def close(self) -> None:
        if not self.loop.is_closed():  # a test may have stopped it already
            self.loop.call_soon_threadsafe(self.stop.set)
        self.thread.join(10)
        shutil.rmtree(self.runner.config.state_dir, ignore_errors=True)


def make_home(base: Path) -> Path:
    home = base / "home"
    for folder in (".ssh", ".cache/himari-runner", "projects/a", "HIMARI/conversations"):
        (home / folder).mkdir(parents=True, exist_ok=True)
    (home / ".ssh/id").write_text("secret")
    (home / ".gitconfig").write_text("[user]\n")
    (home / "projects/a/main.c").write_text("int main(){}\n")
    return home


def start_runner(base: Path, *, client_uid: int | None = None, enforced: bool = True,
                 sandbox: MachineSandbox | None = None, home: Path | None = None,
                 network: bool = False, push: dict[str, object] | None = None) -> RunnerKit:
    """``sandbox``/``home``: a real machine sandbox (started with its
    self-test) and the user's real home, for the host gate. ``network``:
    the proxy with the default allow list (it starts with a real sandbox
    only). ``push``: the ``[push]`` table. The runner's state folder is a short one of its own (a socket
    path is at most 107 bytes), never the user's real one."""
    real = sandbox is not None
    home = make_home(base) if home is None else home
    himari_home = base / "HIMARI" if real else home / "HIMARI"
    socket_path = str(base / "runner.sock")
    config = parse({"client_uid": os.getuid() if client_uid is None else client_uid,
                    "himari_home": str(himari_home), "socket": socket_path,
                    "cache_dir": str(home / ".cache/himari-runner"), "hidden": [str(base / "secret-root")],
                    "state_dir": tempfile.mkdtemp(prefix="hr-"), "network": {"enabled": network},
                    "push": push or {}})
    config = dataclasses.replace(config, home=str(home))
    rules = HiddenRules(str(home), (".gitconfig", ".local/bin", ".local/lib", ".cache/himari-runner"),
                        config.hidden)
    used = sandbox if sandbox is not None else FakeSandbox(enforced)
    runner = Runner(config, used, Host(rules, config.himari_home))
    ready = threading.Event()
    holder: dict[str, object] = {}

    def serve() -> None:
        loop = asyncio.new_event_loop()
        stop = asyncio.Event()
        holder.update(loop=loop, stop=stop)
        if real:
            loop.run_until_complete(runner.start())
        sock = listener(config)
        ready.set()
        try:
            loop.run_until_complete(runner.serve(sock, stop))
        finally:
            sock.close()
            loop.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    ready.wait(60)
    loop, stop = holder["loop"], holder["stop"]
    assert isinstance(loop, asyncio.AbstractEventLoop) and isinstance(stop, asyncio.Event)
    fake = used if isinstance(used, FakeSandbox) else FakeSandbox()
    return RunnerKit(home, socket_path, runner, fake, loop, thread, stop)
