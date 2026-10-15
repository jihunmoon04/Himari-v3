"""Real-host gate for the server profile (V3-P59 §4; opt-in: HIMARI_SANDBOX_HOST=1).

Long-lived processes in a real sandbox: the server self-test, lines both
ways, the operator's directories at their host paths, the identity checks
refusing before anything is spawned, the line bound, and shutdown taking
every process of the sandbox with it.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from himari.sandbox.api import (
    MAX_LINE_BYTES,
    ProcessGone,
    SandboxAdmissionError,
    SandboxState,
    ServerProfile,
)
from himari.sandbox.server import ServerProcess, ServerSandbox
from himari.sandbox.server_self_test import probe

pytestmark = pytest.mark.skipif(os.environ.get("HIMARI_SANDBOX_HOST") != "1", reason="host gate is opt-in")

PYTHON = "/usr/bin/python3"
ECHO = "import sys\nfor line in sys.stdin:\n    sys.stdout.write(line); sys.stdout.flush()\n"


@pytest.fixture
def layout(tmp_path: Path) -> dict[str, Path]:
    paths = {name: tmp_path / name for name in ("data", "ro", "rw")}
    for path in paths.values():
        path.mkdir()
    (paths["data"] / "himari.db").write_text("db")
    (paths["ro"] / "given.txt").write_text("given")
    return paths


def run(layout: dict[str, Path], script: str, scenario: Callable[[ServerProcess], Awaitable[None]], *,
        env: dict[str, str] | None = None, grace: float = 2.0) -> None:
    async def go() -> None:
        sandbox = ServerSandbox.discover()
        assert await sandbox.start(protected_paths=(str(layout["data"]),)) is SandboxState.ENFORCED
        launcher = sandbox.launcher(ServerProfile((str(layout["ro"]),), (str(layout["rw"]),), (str(layout["data"]),)))
        process = await launcher.launch([PYTHON, "-I", "-S", "-c", script], env or {}, str(layout["rw"]))
        try:
            await asyncio.wait_for(scenario(process), 60)
        finally:
            await process.shutdown(grace)

    asyncio.run(go())


def test_the_server_self_test_passes_with_every_observation(layout: dict[str, Path]) -> None:
    report = asyncio.run(probe(ServerSandbox.discover(), (str(layout["data"]),)))
    assert report.passed, (report.failures, report.error)


def test_lines_round_trip_and_the_directories_are_at_their_host_paths(layout: dict[str, Path]) -> None:
    script = ("import json, os, sys\n"
              "for line in sys.stdin:\n"
              "    ask = json.loads(line)\n"
              "    try:\n"
              "        if ask['op'] == 'read': out = open(ask['path']).read()\n"
              "        else: open(ask['path'], 'w').write('server'); out = 'written'\n"
              "    except OSError as e: out = type(e).__name__\n"
              "    print(json.dumps({'out': out, 'cwd': os.getcwd(), 'env': os.environ.get('GIVEN')}), flush=True)\n")

    async def scenario(process: ServerProcess) -> None:
        async def ask(op: str, path: Path) -> dict[str, str]:
            await process.write_line(json.dumps({"op": op, "path": str(path)}).encode())
            answer: dict[str, str] = json.loads(await process.read_line())
            return answer

        assert await ask("read", layout["ro"] / "given.txt") == {"out": "given", "cwd": str(layout["rw"]), "env": "1"}
        assert (await ask("write", layout["ro"] / "new.txt"))["out"] == "OSError"
        assert (await ask("write", layout["rw"] / "new.txt"))["out"] == "written"
        assert (await ask("read", layout["data"] / "himari.db"))["out"] == "FileNotFoundError"

    run(layout, script, scenario, env={"GIVEN": "1"})
    assert (layout["rw"] / "new.txt").read_text() == "server"  # the host sees the server's writes
    assert not (layout["ro"] / "new.txt").exists()


def test_identity_failures_refuse_before_spawning(layout: dict[str, Path]) -> None:
    os.link(layout["data"] / "himari.db", layout["rw"] / "db-link")

    async def go() -> None:
        sandbox = ServerSandbox.discover()
        await sandbox.start(protected_paths=(str(layout["data"]),))
        launcher = sandbox.launcher(ServerProfile((str(layout["ro"]),), (str(layout["rw"]),), (str(layout["data"]),)))
        with pytest.raises(SandboxAdmissionError, match="hard link to HIMARI's data"):
            await launcher.launch([PYTHON, "-c", "open('/tmp/ran', 'w')"], {}, str(layout["rw"]))
        assert sandbox.state is SandboxState.ENFORCED  # a refused launch is not a setup failure

    asyncio.run(go())


def test_a_line_over_the_bound_shuts_the_server_down(layout: dict[str, Path]) -> None:
    script = f"import sys, time\nsys.stdout.write('x' * {MAX_LINE_BYTES + 1} + '\\n'); sys.stdout.flush(); time.sleep(600)"

    async def scenario(process: ServerProcess) -> None:
        with pytest.raises(ProcessGone, match="longer than"):
            await process.read_line()
        assert process.returncode is not None

    run(layout, script, scenario)


def test_shutdown_ends_every_process_of_the_sandbox(layout: dict[str, Path]) -> None:
    marker = f"himari-server-host-{os.getpid()}"
    script = ("import subprocess, sys, time\n"
              f"subprocess.Popen(['sh', '-c', 'exec -a {marker}-own-session sleep 600'], start_new_session=True)\n"
              f"subprocess.Popen(['sh', '-c', 'exec -a {marker}-child sleep 600'])\n"
              "print('ready', flush=True)\n"
              "import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
              "sys.stdin.read(); time.sleep(600)\n")

    def survivors() -> str:
        return subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True).stdout

    async def scenario(process: ServerProcess) -> None:
        assert await process.read_line() == b"ready"
        await asyncio.sleep(0.3)
        assert survivors()  # the grandchildren run
        await process.shutdown(0.5)  # ignores EOF and SIGTERM: killed
        assert process.returncode is not None

    run(layout, script, scenario, grace=0.5)
    for _ in range(50):
        if not survivors():
            break
        asyncio.run(asyncio.sleep(0.1))
    assert not survivors()


def test_stderr_never_blocks_the_server(layout: dict[str, Path]) -> None:
    script = "import sys\nsys.stderr.write('e' * 1000000); sys.stderr.flush()\nprint('alive', flush=True)\n" + ECHO

    async def scenario(process: ServerProcess) -> None:
        assert await process.read_line() == b"alive"
        await process.write_line(b"ping")
        assert await process.read_line() == b"ping"

    run(layout, script, scenario)


def test_shutdown_sigterm_reaches_the_server_itself(layout: dict[str, Path]) -> None:
    """MCP's stdio shutdown: close stdin, then SIGTERM, then SIGKILL. A
    SIGTERM to bwrap's group would end bwrap and SIGKILL the server."""
    script = (f"import signal, sys, time\n"
              f"def term(*_):\n    open({str(layout['rw'] / 'term')!r}, 'w').write('SIGTERM'); sys.exit(3)\n"
              "signal.signal(signal.SIGTERM, term)\n"
              "print('ready', flush=True)\nsys.stdin.read(); time.sleep(600)\n")

    async def scenario(process: ServerProcess) -> None:
        assert await process.read_line() == b"ready"
        await process.shutdown(1.0)
        assert process.returncode == 3  # its own exit, not a signal

    run(layout, script, scenario)
    assert (layout["rw"] / "term").read_text() == "SIGTERM"

