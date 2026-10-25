"""An in-process ``himari-runner`` for tests (V3-P74): a real server, real
file operations on a temporary home, and a fake sandbox that records each
command instead of running it. The server runs on its own thread and loop,
so a test may call it from ``asyncio.run`` or from another server's loop."""

from __future__ import annotations

import asyncio
import dataclasses
import os
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
        self.result = CommandResult(0, False, b"out\n", b"", 4, 0, 5)

    async def run(self, argv: list[str], layout: MachineLayout, timeout: float, *,
                  max_output: int = 0) -> CommandResult:
        self.calls.append((argv, layout, timeout))
        try:
            while self.gate is not None and not self.gate.is_set():
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            self.cancelled.set()
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
        self.loop.call_soon_threadsafe(self.stop.set)
        self.thread.join(10)


def make_home(base: Path) -> Path:
    home = base / "home"
    for folder in (".ssh", ".cache/himari-runner", "projects/a", "HIMARI/conversations"):
        (home / folder).mkdir(parents=True, exist_ok=True)
    (home / ".ssh/id").write_text("secret")
    (home / ".gitconfig").write_text("[user]\n")
    (home / "projects/a/main.c").write_text("int main(){}\n")
    return home


def start_runner(base: Path, *, client_uid: int | None = None, enforced: bool = True,
                 sandbox: MachineSandbox | None = None, home: Path | None = None) -> RunnerKit:
    """``sandbox``/``home``: a real machine sandbox (started with its
    self-test) and the user's real home, for the host gate."""
    real = sandbox is not None
    home = make_home(base) if home is None else home
    himari_home = base / "HIMARI" if real else home / "HIMARI"
    socket_path = str(base / "runner.sock")
    config = parse({"client_uid": os.getuid() if client_uid is None else client_uid,
                    "himari_home": str(himari_home), "socket": socket_path,
                    "cache_dir": str(home / ".cache/himari-runner"), "hidden": [str(base / "secret-root")]})
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
