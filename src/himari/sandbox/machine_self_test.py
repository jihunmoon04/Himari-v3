"""Startup self-test of the machine profile (V3-P74 §13, V3-050 amended).

The probe runs through the same ``MachineSandbox.launch`` as every command,
in a throw-away layout under ``scratch`` (a folder the user can write that
is not under /tmp: the sandbox's /tmp is its own):

    scratch/home/              attached writable (it holds hidden paths: review N2)
    scratch/home/work/         the cwd
    scratch/home/.secret/      a hidden folder with a token in it
    scratch/home/.tokenfile    a hidden file
    scratch/outside/           not attached; host Unix sockets live here

plus the real masks the caller passes (each must look masked inside), a
TCP listener on the host's 127.0.0.1, the runner's own socket path, and a
proxy (§11) whose allow list holds ``localhost`` only: through the bridge,
a name not on it, ``localhost`` (allowed, but loopback) and an IP literal
are each refused (403), and the bridge cannot be traced or read.
ENFORCED requires every observation to equal ``EXPECTED``.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
from collections.abc import Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field

from himari.sandbox.machine import MachineLayout, MachineSandbox, Mask

PROBE = r"""
import ctypes, errno, json, os, socket, sys
p = json.loads(sys.argv[1]); r = {}
def ok(f):
    try:
        f(); return True
    except Exception:
        return False
def err(f):
    try:
        f(); return "ok"
    except OSError as e:
        return errno.errorcode.get(e.errno, str(e.errno))
def write(path):
    with open(path, "w") as h:
        h.write("x")
def read(path):
    with open(path) as h:
        return h.read()
r["cwd"] = os.getcwd()
r["write_workspace"] = ok(lambda: write(p["work"] + "/probe.txt"))
r["write_outside"] = ok(lambda: write(p["outside"] + "/probe.txt"))
r["write_root"] = ok(lambda: write("/probe.txt"))
r["write_usr"] = ok(lambda: write("/usr/probe.txt"))
r["write_tmp"] = ok(lambda: write("/tmp/probe.txt"))
r["hidden_dir_listing"] = sorted(os.listdir(p["secret_dir"])) if os.path.isdir(p["secret_dir"]) else None
r["hidden_token_readable"] = ok(lambda: read(p["secret_dir"] + "/token"))
r["hidden_dir_writable"] = ok(lambda: write(p["secret_dir"] + "/new"))
r["hidden_file_content"] = read(p["secret_file"]) if os.path.exists(p["secret_file"]) else None
r["hidden_file_writable"] = ok(lambda: write(p["secret_file"]))
def masked(path, kind):
    try:
        return os.listdir(path) == [] if kind == "dir" else read(path) == ""
    except OSError:
        return False
r["real_masks"] = all(masked(m, k) for m, k in p["real_masks"])
r["socket_unix"] = err(lambda: socket.socket(socket.AF_UNIX, socket.SOCK_STREAM).close())
def pair(kind):
    a, b = socket.socketpair(socket.AF_UNIX, kind); a.close(); b.close()
r["socketpair_stream"] = err(lambda: pair(socket.SOCK_STREAM))
r["socketpair_stream_flags"] = err(lambda: pair(socket.SOCK_STREAM | socket.SOCK_NONBLOCK | socket.SOCK_CLOEXEC))
r["socketpair_dgram"] = err(lambda: pair(socket.SOCK_DGRAM))
r["socketpair_seqpacket"] = err(lambda: pair(socket.SOCK_SEQPACKET))
libc = ctypes.CDLL(None, use_errno=True)
buf = ctypes.create_string_buffer(256)
libc.syscall.restype = ctypes.c_long
r["io_uring_setup"] = "ENOSYS" if libc.syscall(425, 1, buf) == -1 and ctypes.get_errno() == errno.ENOSYS else "other"
def connect(addr):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM); s.settimeout(2)
    try: s.connect(addr)
    finally: s.close()
r["tcp_host_loopback"] = ok(lambda: connect(("127.0.0.1", p["port"])))
r["tcp_external"] = ok(lambda: connect(("1.1.1.1", 53)))
def loopback_inside():
    server = socket.socket(); server.bind(("127.0.0.1", 0)); server.listen(1)
    client = socket.socket(); client.settimeout(2); client.connect(server.getsockname())
    client.close(); server.close()
r["tcp_loopback_inside"] = ok(loopback_inside)
# /proc is the sandbox's own (its network namespace); /sys is the host's, read-only, and lists the host's devices
r["interfaces"] = sorted(line.split(":")[0].strip() for line in open("/proc/net/dev").read().splitlines()[2:])
r["sees_run_user"] = os.path.exists("/run/user")
r["sees_runner_socket"] = bool(p["runner_socket"]) and os.path.lexists(p["runner_socket"])
r["nested_userns"] = ok(lambda: os.unshare(os.CLONE_NEWUSER))
status = open("/proc/self/status").read()
r["no_new_privs"] = "NoNewPrivs:\t1" in status
r["cap_eff_zero"] = "CapEff:\t0000000000000000" in status
r["seccomp"] = "Seccomp:\t2" in status
found = []
for name in os.listdir("/proc/self/fd"):
    if int(name) > 2:
        try:
            os.readlink("/proc/self/fd/" + name); found.append(int(name))
        except OSError:
            pass
r["extra_fds"] = found
def via_proxy(where):
    s = socket.create_connection(("127.0.0.1", 3128), timeout=5)
    try:
        s.sendall(("CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n" % (where, where)).encode())
        return s.recv(64).split(b" ")[1].decode()
    finally:
        s.close()
def status(where):
    try:
        return via_proxy(where)
    except Exception as e:
        return type(e).__name__
r["proxy_unlisted"] = status("example.com:443")
r["proxy_loopback_name"] = status("localhost:443")
r["proxy_ip_literal"] = status("127.0.0.1:443")
r["proxy_env"] = os.environ.get("HTTPS_PROXY")
def cmdline(pid):
    try:
        return open("/proc/%d/cmdline" % pid, "rb").read()
    except OSError:
        return b""
pids = [int(n) for n in os.listdir("/proc") if n.isdigit() and int(n) != os.getpid()]
r["bridges"] = sum(1 for pid in pids if cmdline(pid).split(b"\0")[3:4] == [b"/run/himari/landlock_helper.py"])
libc.ptrace.restype = ctypes.c_long
def traceable(pid):
    if libc.ptrace(ctypes.c_long(16), ctypes.c_long(pid), None, None) == 0:  # PTRACE_ATTACH
        libc.ptrace(ctypes.c_long(17), ctypes.c_long(pid), None, None)
        return True
    try:
        open("/proc/%d/mem" % pid, "rb").close()
        return True
    except OSError:
        return False
r["traceable"] = [pid for pid in pids if traceable(pid)]
print(json.dumps(r, sort_keys=True))
"""

EXPECTED: dict[str, object] = {
    "write_workspace": True,
    "write_outside": False,
    "write_root": False,
    "write_usr": False,
    "write_tmp": True,
    "hidden_dir_listing": [],
    "hidden_token_readable": False,
    "hidden_dir_writable": False,
    "hidden_file_content": "",
    "hidden_file_writable": False,
    "real_masks": True,
    "socket_unix": "EACCES",
    "socketpair_stream": "ok",
    "socketpair_stream_flags": "ok",
    "socketpair_dgram": "EACCES",
    "socketpair_seqpacket": "EACCES",
    "io_uring_setup": "ENOSYS",
    "tcp_host_loopback": False,
    "tcp_external": False,
    "tcp_loopback_inside": True,
    "interfaces": ["lo"],
    "sees_run_user": False,
    "sees_runner_socket": False,
    "nested_userns": False,
    "no_new_privs": True,
    "cap_eff_zero": True,
    "seccomp": True,
    "extra_fds": [],
    "proxy_unlisted": "403",
    "proxy_loopback_name": "403",
    "proxy_ip_literal": "403",
    "proxy_env": "http://127.0.0.1:3128",
    "bridges": 1,
    "traceable": [],
}


@dataclass
class MachineReport:
    passed: bool
    observed: dict[str, object] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    error: str = ""


def _directory(path: str) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)


async def probe(sandbox: MachineSandbox, scratch: str, *, home: str, real_masks: Sequence[Mask] = (),
                runner_socket: str = "", cache: str | None = None, proxy: str | None = None,
                cgroup_procs: str | None = None) -> MachineReport:
    """``proxy``: the socket of a proxy that allows ``localhost`` only;
    without one, every proxy check fails (the self-test does not pass)."""
    os.makedirs(scratch, exist_ok=True)
    with ExitStack() as stack:
        base = tempfile.mkdtemp(prefix=".himari-machine-selftest-", dir=scratch)
        stack.callback(shutil.rmtree, base, True)
        sim_home = os.path.join(base, "home")
        work = os.path.join(sim_home, "work")
        secret_dir = os.path.join(sim_home, ".secret")
        secret_file = os.path.join(sim_home, ".tokenfile")
        outside = os.path.join(base, "outside")
        for folder in (work, secret_dir, outside):
            os.makedirs(folder)
        for path in (os.path.join(secret_dir, "token"), secret_file):
            with open(path, "w") as handle:
                handle.write("host only")
        stream = stack.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
        stream.bind(os.path.join(outside, "s.sock"))
        stream.listen(1)
        dgram = stack.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM))
        dgram.bind(os.path.join(outside, "d.sock"))
        tcp = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
        tcp.bind(("127.0.0.1", 0))
        tcp.listen(1)

        home_fd = _directory(sim_home)
        stack.callback(os.close, home_fd)
        cache_fd = None
        if cache is not None:
            cache_fd = _directory(cache)
            stack.callback(os.close, cache_fd)
        masks = (Mask(secret_dir, "dir", os.stat(secret_dir).st_dev),
                 Mask(secret_file, "file", os.stat(secret_file).st_dev), *real_masks)
        layout = MachineLayout(home=home, cwd=work, writable=((sim_home, home_fd),), masks=masks,
                               cache=None if cache is None or cache_fd is None else (cache, cache_fd),
                               cgroup_procs=cgroup_procs, proxy=proxy)
        params = {"work": work, "outside": outside, "secret_dir": secret_dir, "secret_file": secret_file,
                  "port": tcp.getsockname()[1], "runner_socket": runner_socket,
                  "real_masks": [[m.path, m.kind] for m in real_masks]}
        script = os.path.join(base, "probe.py")  # longer than one argument may be; read-only inside
        with open(script, "w") as handle:
            handle.write(PROBE)
        result = await sandbox.launch(["/usr/bin/python3", "-I", "-S", script, json.dumps(params)], layout, 30)
        if result.timed_out or result.exit_code != 0:
            return MachineReport(False, error=f"probe exit={result.exit_code} timed_out={result.timed_out} "
                                              f"stderr={result.stderr[-300:]!r}")
        try:
            observed = json.loads(result.stdout)
        except ValueError:
            return MachineReport(False, error="probe output is not JSON")
        wanted = {**EXPECTED, "cwd": work}
        failures = [k for k, v in wanted.items() if observed.get(k) != v]
        failures += [k for k in observed if k not in wanted]
        return MachineReport(not failures, observed, failures)
