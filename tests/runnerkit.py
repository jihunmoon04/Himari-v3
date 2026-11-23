"""An in-process ``himari-runner`` for tests (V3-P74): a real server and real
file operations on a temporary home. The server runs on its own thread and
loop, so a test may call it from ``asyncio.run`` or from another server's
loop."""

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


@dataclass
class RunnerKit:
    home: Path
    socket: str
    runner: Runner
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


def make_home(base: Path) -> Path:
    home = base / "home"
    for folder in (".ssh", "projects/a", "HIMARI/conversations"):
        (home / folder).mkdir(parents=True, exist_ok=True)
    (home / ".ssh/id").write_text("secret")
    (home / ".gitconfig").write_text("[user]\n")
    (home / "projects/a/main.c").write_text("int main(){}\n")
    return home


def start_runner(base: Path, *, client_uid: int | None = None) -> RunnerKit:
    home = make_home(base)
    socket_path = str(base / "runner.sock")
    config = parse({"client_uid": os.getuid() if client_uid is None else client_uid,
                    "himari_home": str(home / "HIMARI"), "socket": socket_path,
                    "hidden": [str(base / "secret-root")]})
    config = dataclasses.replace(config, home=str(home))
    runner = Runner(config, Host(HiddenRules(str(home), config.visible, config.hidden), config.himari_home))
    ready = threading.Event()
    holder: dict[str, object] = {}

    def serve() -> None:
        loop = asyncio.new_event_loop()
        stop = asyncio.Event()
        holder.update(loop=loop, stop=stop)
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
    return RunnerKit(home, socket_path, runner, loop, thread, stop)
