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
    started = start_runner(base, sandbox=sandbox, home=Path.home(), network=True)
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


def test_an_attached_folder_is_writable_and_others_are_not(kit: RunnerKit) -> None:
    """V3-P74 §10: a second Workspace outside the HIMARI home (part 2)."""
    folder = call(kit, api.CONVERSATION_FOLDER, {"conversation": "hosttest"})["path"]
    base = kit.himari_home.parent
    project, other = base / "project", base / "other"
    project.mkdir(exist_ok=True)
    other.mkdir(exist_ok=True)
    checked = call(kit, api.CHECK_WORKSPACE, {"path": str(project), "others": []})
    assert checked["path"] == str(project)
    result = call(kit, api.RUN, {"argv": ["sh", "-c", f"echo in > {project}/made.txt; echo out > {other}/no.txt; "
                                                      "pwd"],
                                 "cwd": str(project), "write_roots": [folder, str(project)], "timeout": 60})
    assert base64.b64decode(result["stdout"]).decode().strip() == str(project)
    assert (project / "made.txt").read_text() == "in\n" and not (other / "no.txt").exists()


CLOUD = Path("/home/ubuntu/cloud_storage/02_구글_일반")


@pytest.mark.skipif(not os.access(CLOUD, os.R_OK | os.X_OK), reason="no rclone Drive mount here")
def test_a_cloud_mount_is_seen_inside_the_sandbox(kit: RunnerKit) -> None:
    """§14: an allow_other FUSE mount is reached from the sandbox's user namespace (read only here)."""
    assert call(kit, api.CHECK_WORKSPACE, {"path": str(CLOUD), "others": []})["filesystem"] == "fuse.rclone"
    code, out, err = run(kit, f"ls '{CLOUD}' | wc -l")
    assert code == 0 and int(out.strip()) > 0, err


# --- the network (§11) ----------------------------------------------------------------------------------

FETCH = ("import sys, urllib.request\n"
         "try:\n"
         "    print(urllib.request.urlopen(sys.argv[1], timeout=20).status)\n"
         "except Exception as e:\n"
         "    print(type(e).__name__, e)\n")


def fetch(kit: RunnerKit, url: str) -> str:
    code, out, err = run(kit, f"python3 -I -c '{FETCH}' {url}")
    assert code == 0, err
    return out.strip()


def test_the_proxy_lets_allowed_hosts_through_only(kit: RunnerKit) -> None:
    """§11.3 on the real network: an allowed host over HTTPS, nothing else; the model is told which."""
    assert call(kit, api.HELLO, {})["network"] == list(kit.runner.config.network or ())
    assert fetch(kit, "https://pypi.org/simple/pip/") == "200"
    refused = fetch(kit, "https://example.com/")
    assert "403" in refused, refused
    assert "403" in fetch(kit, "http://pypi.org/simple/")  # plain HTTP is not offered
    code, out, _ = run(kit, "env -u HTTPS_PROXY -u https_proxy -u ALL_PROXY -u all_proxy python3 -I -c "
                            "\"import socket; socket.create_connection(('151.101.0.223', 443), timeout=5)\" "
                            "2>&1 | tail -1")
    assert code == 0 and "Error" in out  # around the proxy: no route


SPOOF = ("import socket, ssl\n"
         "s = socket.create_connection((\"127.0.0.1\", 3128), timeout=20)\n"
         "s.sendall(b\"CONNECT pypi.org:443 HTTP/1.1\\r\\n\\r\\n\")\n"
         "print(s.recv(4096).split()[1].decode())\n"
         "try:\n"
         "    ssl.create_default_context().wrap_socket(s, server_hostname=\"example.com\")\n"
         "    print(\"handshake\")\n"
         "except ssl.SSLError as e:\n"
         "    print(e.reason)\n")


def test_the_tunnel_goes_to_the_name_asked_for_only(kit: RunnerKit) -> None:
    """A TLS server name other than the CONNECT name (another site behind the same addresses) is cut."""
    code, out, err = run(kit, f"python3 -I -c '{SPOOF}'")
    assert code == 0 and out.split() == ["200", "TLSV1_UNRECOGNIZED_NAME"], (out, err)


def test_git_over_https_through_the_proxy(kit: RunnerKit) -> None:
    code, out, err = run(kit, "git ls-remote --heads https://github.com/octocat/Hello-World.git", timeout=60)
    assert code == 0 and "refs/heads/master" in out, err


def test_a_command_ends_although_the_bridge_is_running(kit: RunnerKit) -> None:
    """The bridge holds none of the command's output pipes, and goes with the sandbox."""
    started = time.monotonic()
    code, out, _ = run(kit, "echo done")
    assert code == 0 and out == "done\n" and time.monotonic() - started < 10
