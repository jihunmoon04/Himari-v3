"""The server profile's startup self-test (V3-P59 §4.4, V3-050's principle).

A real server sandbox is launched through ``ServerSandbox.spawn`` — the
path every stdio server takes, identity checks included — in a throw-away
layout: a ``read_only`` and a ``writable`` directory, a host directory next
to them that is not bound, the protected roots (HIMARI's data root and
Workspaces), a TCP listener on the host's 127.0.0.1 and ::1, and host Unix
sockets. The probe echoes one line from stdin to stdout (the pipe round
trip), writes to stderr, then reports what it could reach. ENFORCED
requires every observation to equal ``EXPECTED``, the environment to be
exactly the fixed one plus the operator's, and the mount table to be
exactly the layout's.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import tempfile
from collections.abc import Sequence
from contextlib import ExitStack
from typing import TYPE_CHECKING

from himari.sandbox.api import ServerProfile
from himari.sandbox.self_test import (
    DEFAULT_PROTECTED,
    DEVICE_NODES,
    ExpectedSources,
    SelfTestReport,
    _unexpected_mounts,
)

if TYPE_CHECKING:
    from himari.sandbox.server import ServerSandbox

PROBE = r"""
import json, os, socket, stat, sys
p = json.loads(sys.argv[1]); r = {}
line = sys.stdin.readline()
sys.stdout.write(line); sys.stdout.flush()
sys.stderr.write("probe-stderr\n"); sys.stderr.flush()
def ok(f):
    try:
        f(); return True
    except Exception:
        return False
def write(path):
    with open(path, "w") as h:
        h.write("x")
def read(path):
    with open(path) as h:
        return h.read()
def connect(family, addr):
    s = socket.socket(family, socket.SOCK_STREAM); s.settimeout(2)
    try: s.connect(addr)
    finally: s.close()
def bind_inside():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); s.close()
r["cwd_is_writable_dir"] = os.getcwd() == p["writable"]
r["read_read_only"] = ok(lambda: read(p["read_only"] + "/given.txt"))
r["write_read_only"] = ok(lambda: write(p["read_only"] + "/probe.txt"))
r["write_writable"] = ok(lambda: write(p["writable"] + "/probe.txt"))
r["write_tmp"] = ok(lambda: write("/tmp/probe.txt"))
r["write_root"] = ok(lambda: write("/probe.txt"))
r["write_usr"] = ok(lambda: write("/usr/probe.txt"))
r["sees_unbound"] = any(os.path.lexists(x) for x in p["unbound_paths"])
r["sees_protected"] = sorted(x for x in p["protected_paths"] if os.path.lexists(x))
r["env"] = dict(os.environ)
def mount_table():
    table = []
    for line in open("/proc/self/mountinfo"):
        fields = line.split(" ")
        point = fields[4]
        for code in ("\\040", "\\011", "\\012", "\\134"):
            point = point.replace(code, chr(int(code[1:], 8)))
        try:
            info = os.stat(point)
            char_device, identity = stat.S_ISCHR(info.st_mode), [info.st_dev, info.st_ino, info.st_rdev]
        except OSError:
            char_device, identity = False, None
        table.append([point, fields[fields.index("-") + 1], fields[3], char_device, identity])
    return table
r["mounts"] = mount_table()
r["tcp_external"] = ok(lambda: connect(socket.AF_INET, ("1.1.1.1", 53)))
r["tcp_host_loopback"] = ok(lambda: connect(socket.AF_INET, ("127.0.0.1", p["port"])))
r["tcp6_host_loopback"] = bool(p["port6"]) and ok(lambda: connect(socket.AF_INET6, ("::1", p["port6"])))
r["unix_host_socket"] = ok(lambda: connect(socket.AF_UNIX, p["unix_path"]))
r["abstract_host_socket"] = ok(lambda: connect(socket.AF_UNIX, "\0" + p["abstract_name"]))
def udp_out():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try: s.sendto(b"x", ("1.1.1.1", 53))
    finally: s.close()
r["udp_external"] = ok(udp_out)
r["interfaces"] = sorted(os.listdir("/sys/class/net")) if os.path.isdir("/sys/class/net") else sorted(
    line.split(":")[0].strip() for line in open("/proc/net/dev").read().splitlines()[2:])
r["tcp_bind_inside"] = ok(bind_inside)
r["nested_userns"] = ok(lambda: os.unshare(os.CLONE_NEWUSER))
status = open("/proc/self/status").read()
r["no_new_privs"] = "NoNewPrivs:\t1" in status
r["cap_eff_zero"] = "CapEff:\t0000000000000000" in status
print(json.dumps(r, sort_keys=True))
"""

EXPECTED: dict[str, object] = {
    "cwd_is_writable_dir": True,
    "read_read_only": True,
    "write_read_only": False,
    "write_writable": True,
    "write_tmp": True,
    "write_root": False,
    "write_usr": False,
    "sees_unbound": False,
    "sees_protected": [],
    "tcp_external": False,
    "tcp_host_loopback": False,
    "tcp6_host_loopback": False,
    "unix_host_socket": False,
    "abstract_host_socket": False,
    "udp_external": False,
    "interfaces": ["lo"],
    "tcp_bind_inside": False,
    "nested_userns": False,
    "no_new_privs": True,
    "cap_eff_zero": True,
}
OPERATOR_ENV = {"HIMARI_SELF_TEST": "server profile"}
PING = b"himari-server-self-test"
PROBE_TIMEOUT_SECONDS = 30.0


def expected_sources(binds: dict[str, int]) -> ExpectedSources:
    """Every system bind and the helper, as for the Workspace, plus each
    operator directory at its host path (by the descriptor that was bound)."""
    from himari.sandbox import landlock_helper
    from himari.sandbox.backend import HELPER_IN_SANDBOX, system_view

    def identity(info: os.stat_result) -> tuple[int, int]:
        return info.st_dev, info.st_ino

    view = system_view()
    found = {view[i + 2]: identity(os.stat(view[i + 1])) for i, arg in enumerate(view) if arg == "--ro-bind"}
    found[HELPER_IN_SANDBOX] = identity(os.stat(os.path.realpath(landlock_helper.__file__)))
    for path, fd in binds.items():
        found[path] = identity(os.fstat(fd))
    devices = {node: os.stat(node).st_rdev for node in DEVICE_NODES if os.path.exists(node)}
    return ExpectedSources(found, devices)


async def probe(sandbox: ServerSandbox, protected_paths: Sequence[str] = ()) -> SelfTestReport:
    from himari.sandbox.backend import SANDBOX_ENV

    with ExitStack() as stack:
        # Under /tmp, whatever TMPDIR says: the layout must not need /home and the like.
        base = stack.enter_context(tempfile.TemporaryDirectory(prefix="himari-server-selftest-", dir="/tmp"))
        read_only, writable, unbound = (os.path.join(base, name) for name in ("read_only", "writable", "unbound"))
        for folder in (read_only, writable, unbound):
            os.makedirs(folder)
        with open(os.path.join(read_only, "given.txt"), "w") as handle:
            handle.write("given")

        tcp = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
        tcp.bind(("127.0.0.1", 0))
        tcp.listen(1)
        port6 = 0
        try:
            tcp6 = stack.enter_context(socket.socket(socket.AF_INET6, socket.SOCK_STREAM))
            tcp6.bind(("::1", 0))
            tcp6.listen(1)
            port6 = tcp6.getsockname()[1]
        except OSError:
            pass
        unix_path = os.path.join(unbound, "host.sock")
        unix = stack.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
        unix.bind(unix_path)
        unix.listen(1)
        abstract_name = f"himari-server-selftest-{os.getpid()}-{os.urandom(4).hex()}"
        abstract = stack.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
        abstract.bind("\0" + abstract_name)
        abstract.listen(1)

        params = {
            "read_only": read_only,
            "writable": writable,
            "unbound_paths": [unbound, unix_path],
            "protected_paths": sorted({*DEFAULT_PROTECTED, *protected_paths}),
            "port": tcp.getsockname()[1],
            "port6": port6,
            "unix_path": unix_path,
            "abstract_name": abstract_name,
        }
        profile = ServerProfile(read_only=(read_only,), writable=(writable,), protected=tuple(protected_paths))
        fds = {path: os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC) for path in (read_only, writable)}
        for fd in fds.values():
            stack.callback(os.close, fd)
        process = await sandbox.spawn(profile, ["/usr/bin/python3", "-I", "-S", "-c", PROBE, json.dumps(params)],
                                      OPERATOR_ENV, writable)
        try:
            async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
                await process.write_line(PING)
                echoed = await process.read_line()
                report = await process.read_line()
        except Exception as error:  # noqa: BLE001 — any failure of the probe is a failed self-test
            return SelfTestReport(False, error=f"probe did not answer ({type(error).__name__})")
        finally:
            await process.shutdown(2.0)
        try:
            observed = json.loads(report)
        except ValueError:
            return SelfTestReport(False, error="probe output is not JSON")
        mounts = observed.pop("mounts", None)
        env = observed.pop("env", None)
        failures = [k for k, v in EXPECTED.items() if observed.get(k) != v]
        failures += [k for k in observed if k not in EXPECTED]
        if echoed != PING:
            failures.append("pipe_round_trip")
        if b"probe-stderr" not in process.stderr_tail():
            failures.append("stderr_tail")
        if env != {**SANDBOX_ENV, **OPERATOR_ENV, "PWD": writable}:
            failures.append("env")
        unexpected = _unexpected_mounts(mounts, expected_sources(fds))
        if unexpected:
            failures.append(f"mounts:{unexpected}")
        observed.update(mounts=mounts, env=env)
        return SelfTestReport(not failures, observed, failures)


async def run_server_self_test(sandbox: ServerSandbox, protected_paths: tuple[str, ...] = ()) -> bool:
    try:
        return (await probe(sandbox, protected_paths)).passed
    except Exception:
        return False
