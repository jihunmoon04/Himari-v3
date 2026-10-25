"""Real-host gate for the machine profile (V3-P74 §13; opt-in: HIMARI_SANDBOX_HOST=1).

Runs the runner with the real machine sandbox and the user's real home (its
real hidden list), and its scratch folders under a folder that is not under
/tmp (the sandbox's /tmp is its own): ``HIMARI_MACHINE_SCRATCH``, by default
``~/himari-machine-test``. Nothing outside the scratch folder is written.
"""

from __future__ import annotations

import asyncio
import base64
import os
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from runnerkit import RunnerKit, start_runner

from himari.environment.machine.client import RunnerClient
from himari.runner import api
from himari.sandbox.api import SandboxState
from himari.sandbox.machine import MachineSandbox

pytestmark = pytest.mark.skipif(os.environ.get("HIMARI_SANDBOX_HOST") != "1", reason="host gate is opt-in")


@pytest.fixture(scope="module")
def kit() -> Iterator[RunnerKit]:
    scratch = Path(os.environ.get("HIMARI_MACHINE_SCRATCH", str(Path.home() / "himari-machine-test")))
    scratch.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="host-", dir=scratch))
    sandbox = MachineSandbox.discover()
    assert sandbox.state is SandboxState.CAPABLE_INACTIVE, "host lacks bwrap/Landlock/userns"
    started = start_runner(base, sandbox=sandbox, home=Path.home())
    state: SandboxState = sandbox.state
    assert state is SandboxState.ENFORCED, f"self-test failed: {started.runner.report_failures}"
    yield started
    started.close()
    import shutil

    shutil.rmtree(base, ignore_errors=True)


def call(kit: RunnerKit, op: str, args: dict[str, Any]) -> dict[str, Any]:
    result, fds = asyncio.run(RunnerClient(kit.socket).call(op, args))
    for fd in fds:
        os.close(fd)
    return result


def run(kit: RunnerKit, script: str, timeout: int = 60) -> tuple[int | None, str, str]:
    folder = call(kit, api.CONVERSATION_FOLDER, {"conversation": "hosttest"})["path"]
    result = call(kit, api.RUN, {"argv": ["sh", "-c", script], "cwd": folder, "write_roots": [folder],
                                 "timeout": timeout})
    return (result["exit_code"], base64.b64decode(result["stdout"]).decode(),
            base64.b64decode(result["stderr"]).decode())


def test_writes_stay_in_the_workspace(kit: RunnerKit) -> None:
    folder = kit.himari_home / "conversations/hosttest"
    code, out, err = run(kit, f"pwd; echo hi > made.txt; echo no > {kit.himari_home}/outside.txt; echo $?")
    assert code == 0 and out.splitlines()[0] == str(folder) and (folder / "made.txt").read_text() == "hi\n"
    assert not (kit.himari_home / "outside.txt").exists() and "Read-only file system" in err


def test_reads_are_broad_but_hidden_paths_are_empty(kit: RunnerKit) -> None:
    home = Path.home()
    code, out, _ = run(kit, f"cat /etc/hostname >/dev/null && echo etc-ok; ls -A {home}/.ssh 2>/dev/null | wc -l; "
                            f"cat {home}/.bashrc | wc -c")
    assert code == 0 and out.split() == ["etc-ok", "0", "0"]


def test_unix_sockets_and_io_uring_are_refused(kit: RunnerKit) -> None:
    script = (
        "python3 -c \"\n"
        "import socket, ctypes, errno\n"
        "def e(f):\n"
        "    try: f(); return 'ok'\n"
        "    except OSError as x: return errno.errorcode[x.errno]\n"
        "print(e(lambda: socket.socket(socket.AF_UNIX)))\n"
        "print(e(lambda: socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)))\n"
        "print(e(lambda: socket.socketpair()))\n"
        "s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen(); c = socket.create_connection(s.getsockname()); print('tcp-ok')\n"
        "import asyncio; asyncio.run(asyncio.sleep(0)); print('asyncio-ok')\n"
        "\"")
    code, out, err = run(kit, script)
    assert code == 0, err
    assert out.split() == ["EACCES", "EACCES", "ok", "tcp-ok", "asyncio-ok"]


def test_cancel_kills_the_command(kit: RunnerKit) -> None:
    folder = call(kit, api.CONVERSATION_FOLDER, {"conversation": "hosttest"})["path"]
    marker = Path(folder) / "late.txt"

    async def cancel() -> None:
        task = asyncio.create_task(RunnerClient(kit.socket).call(
            api.RUN, {"argv": ["sh", "-c", "sleep 3; echo late > late.txt"], "cwd": folder, "write_roots": [folder],
                      "timeout": 60}))
        await asyncio.sleep(1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(cancel())
    time.sleep(3)
    assert not marker.exists()


def test_timeouts(kit: RunnerKit) -> None:
    folder = call(kit, api.CONVERSATION_FOLDER, {"conversation": "hosttest"})["path"]
    result = call(kit, api.RUN, {"argv": ["sleep", "10"], "cwd": folder, "write_roots": [folder], "timeout": 1})
    assert result["timed_out"] is True and result["exit_code"] is None
