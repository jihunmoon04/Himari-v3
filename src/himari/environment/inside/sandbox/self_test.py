"""Startup self-test (V3-050): spawn a real sandbox and try to escape it.

The probe runs through the same ``SandboxBackend.launch`` path as every
command, in a throw-away layout that mimics production: a principal root
(bound at /workspace) with two Conversation folders, a second principal's
root and a host secret next to it (never bound), a TCP listener on the
host's 127.0.0.1 and ::1, and a host Unix socket. ENFORCED requires every
observation to equal ``EXPECTED``.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from himari.environment.inside.sandbox.backend import SandboxBackend

PROBE = r"""
import json, os, socket, stat, sys
p = json.loads(sys.argv[1]); r = {}
def ok(f):
    try:
        f(); return True
    except Exception:
        return False
def write(path):
    with open(path, "w") as h:
        h.write("x")
def connect(family, addr):
    s = socket.socket(family, socket.SOCK_STREAM); s.settimeout(2)
    try: s.connect(addr)
    finally: s.close()
def bind_inside():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); s.close()
r["cwd"] = os.getcwd()
r["write_own_conversation"] = ok(lambda: write("/workspace/conversations/a/probe.txt"))
r["write_sibling_conversation"] = ok(lambda: write("/workspace/conversations/b/probe.txt"))
r["write_tmp"] = ok(lambda: write("/tmp/probe.txt"))
r["write_root"] = ok(lambda: write("/probe.txt"))
r["write_usr"] = ok(lambda: write("/usr/probe.txt"))
r["sees_other_principal"] = any(os.path.exists(x) for x in p["other_paths"])
r["sees_home"] = os.path.exists("/home")
r["sees_protected"] = sorted(x for x in p["protected_paths"] if os.path.lexists(x))
def mount_table():
    table = []  # every entry, duplicates kept: [point, fstype, root within its fs, is a char device, [dev, ino, rdev]]
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
r["udp_external"] = ok(udp_out)  # Landlock does not cover UDP: only the network namespace stops this
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
    "cwd": "/workspace/conversations/a",
    "write_own_conversation": True,
    "write_sibling_conversation": True,  # same principal: not a boundary (V3-033)
    "write_tmp": True,
    "write_root": False,
    "write_usr": False,
    "sees_other_principal": False,
    "sees_home": False,
    "sees_protected": [],  # host system paths and the real Workspace/data roots
    "tcp_external": False,
    "tcp_host_loopback": False,
    "tcp6_host_loopback": False,
    "unix_host_socket": False,
    "abstract_host_socket": False,
    "udp_external": False,
    "interfaces": ["lo"],  # a fresh network namespace: loopback only
    "tcp_bind_inside": False,  # only the Landlock network layer denies this
    "nested_userns": False,
    "no_new_privs": True,
    "cap_eff_zero": True,
}


@dataclass
class SelfTestReport:
    passed: bool
    observed: dict[str, object] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    error: str = ""


# Host paths that must never be visible inside the sandbox; the backend adds
# the real Workspace root and data roots it was started with.
DEFAULT_PROTECTED = (
    "/etc/passwd", "/etc/shadow", "/etc/hostname", "/etc/ssh", "/root", "/home", "/var", "/srv",
    "/opt", "/mnt", "/media", "/boot", "/snap", "/run/docker.sock", "/run/user",
)


# Mounts bwrap creates itself: the filesystem type and the root within that
# filesystem are required (a fresh mount shows "/"; bwrap's own root tmpfs is
# pivoted from "/newroot"), so a host directory bound there instead is caught.
FRESH_MOUNTS = {"/": ("tmpfs", "/newroot"), "/dev": ("tmpfs", "/"), "/dev/pts": ("devpts", "/"),
                "/proc": ("proc", "/"), "/tmp": ("tmpfs", "/")}
# --dev binds exactly these host device nodes; each must be a char device.
DEVICE_NODES = frozenset({"/dev/null", "/dev/zero", "/dev/full", "/dev/random", "/dev/urandom", "/dev/tty"})
# Some bwrap/kernel combinations cover these read-only; they must stay procfs.
PROC_COVERS = frozenset({"/proc/sys", "/proc/sysrq-trigger", "/proc/irq", "/proc/bus"})


@dataclass(frozen=True)
class ExpectedSources:
    """What each intended bind must be, taken from the host side: the
    (st_dev, st_ino) of every bound host path and the st_rdev of every
    device node. A bind point is accepted only with its own source, so an
    alias (e.g. host /etc bound at /etc/alternatives) is caught."""

    binds: dict[str, tuple[int, int]]
    devices: dict[str, int]


def expected_sources(workspace_fd: int) -> ExpectedSources:
    from himari.environment.inside.sandbox import landlock_helper
    from himari.environment.inside.sandbox.backend import HELPER_IN_SANDBOX, WORKSPACE_MOUNT, system_view

    def identity(info: os.stat_result) -> tuple[int, int]:
        return info.st_dev, info.st_ino

    view = system_view()
    binds = {view[i + 2]: identity(os.stat(view[i + 1])) for i, arg in enumerate(view) if arg == "--ro-bind"}
    binds[HELPER_IN_SANDBOX] = identity(os.stat(os.path.realpath(landlock_helper.__file__)))
    binds[WORKSPACE_MOUNT] = identity(os.fstat(workspace_fd))
    devices = {node: os.stat(node).st_rdev for node in DEVICE_NODES if os.path.exists(node)}
    return ExpectedSources(binds, devices)


def _unexpected_mounts(mounts: object, expected: ExpectedSources) -> list[str]:
    """Every mount must be one the view creates, appear once (nothing stacked
    on it), have the expected kind and — for host binds and device nodes —
    the expected source; no prefix is trusted wholesale."""
    if not isinstance(mounts, list) or not mounts:
        return ["<unreadable>"]
    problems: list[str] = []
    seen: set[str] = set()
    for entry in mounts:
        if not (isinstance(entry, list) and len(entry) == 5):
            return ["<unreadable>"]
        point, fstype, root, char_device, ids = entry
        if point in seen:
            problems.append(f"{point} (stacked)")
            continue
        seen.add(point)
        if point in FRESH_MOUNTS:
            ok = (fstype, root) == FRESH_MOUNTS[point]
        elif point in DEVICE_NODES:
            ok = char_device is True and isinstance(ids, list) and ids[2] == expected.devices.get(point)
        elif point in PROC_COVERS:
            ok = fstype == "proc"
        elif point in expected.binds:
            ok = isinstance(ids, list) and (ids[0], ids[1]) == expected.binds[point]
        else:
            ok = False
        if not ok:
            problems.append(str(point))
    return problems


async def probe(backend: SandboxBackend, protected_paths: tuple[str, ...] = ()) -> SelfTestReport:
    with ExitStack() as stack:
        base = stack.enter_context(tempfile.TemporaryDirectory(prefix="himari-sandbox-selftest-"))
        own = os.path.join(base, "principal_a")
        other = os.path.join(base, "principal_b")
        for folder in (f"{own}/conversations/a", f"{own}/conversations/b", other):
            os.makedirs(folder)
        secret = os.path.join(base, "host-secret.txt")
        with open(secret, "w") as handle:
            handle.write("host only")

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
            pass  # host without IPv6 loopback: nothing to escape to
        unix_path = os.path.join(base, "host.sock")
        unix = stack.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
        unix.bind(unix_path)
        unix.listen(1)
        abstract_name = f"himari-selftest-{os.getpid()}-{os.urandom(4).hex()}"
        abstract = stack.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
        abstract.bind("\0" + abstract_name)
        abstract.listen(1)

        params = {
            "protected_paths": sorted({*DEFAULT_PROTECTED, *protected_paths}),
            "other_paths": [other, secret, base, own],
            "port": tcp.getsockname()[1],
            "port6": port6,
            "unix_path": unix_path,
            "abstract_name": abstract_name,
        }
        root_fd = os.open(own, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        stack.callback(os.close, root_fd)
        result = await backend.launch(
            ["/usr/bin/python3", "-I", "-S", "-c", PROBE, json.dumps(params)],
            root_fd=root_fd,
            cwd="/workspace/conversations/a",
            timeout=30,
        )
        if result.timed_out or result.exit_code != 0:
            return SelfTestReport(False, error=f"probe exit={result.exit_code} timed_out={result.timed_out}")
        try:
            observed = json.loads(result.stdout)
        except ValueError:
            return SelfTestReport(False, error="probe output is not JSON")
        mounts = observed.pop("mounts", None)
        failures = [k for k, v in EXPECTED.items() if observed.get(k) != v]
        failures += [k for k in observed if k not in EXPECTED]
        unexpected = _unexpected_mounts(mounts, expected_sources(root_fd))
        if unexpected:
            failures.append(f"mounts:{unexpected}")
        observed["mounts"] = mounts
        return SelfTestReport(not failures, observed, failures)


async def run_self_test(backend: SandboxBackend, protected_paths: tuple[str, ...] = ()) -> bool:
    try:
        return (await probe(backend, protected_paths)).passed
    except Exception:
        return False
