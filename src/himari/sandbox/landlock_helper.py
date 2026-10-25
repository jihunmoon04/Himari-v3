"""Trusted in-sandbox helper: enforce Landlock + no_new_privs, then exec.

Runs as ``python3 -I -S landlock_helper.py <policy-json> -- <argv...>`` inside
the bubblewrap namespaces, before the requested argv. Standard library only
(no HIMARI import, no site-packages). Ported from HIMARI-v2
``sandbox/landlock_exec.py`` with a v3 addition: Landlock network rules that
deny every TCP bind/connect, as a second layer under the network namespace.

Fail-closed order (exit ``SETUP_FAILURE_EXIT``; the argv never runs and
nothing is written to the sync fd):

0. the machine profile only (V3-P74 §6.1): every hidden path is masked as
   the policy says — an empty read-only folder or an empty read-only file,
   on another device than the host's original;
1. Landlock ABI >= ``MIN_LANDLOCK_ABI`` (6: scoped abstract sockets/signals);
2. ruleset handling every filesystem right, TCP bind+connect (unless the
   policy's ``tcp`` is ``"allow"``: the machine profile's private loopback,
   V3-P74 Q8), and scopes;
3. every path rule added (a missing root is a failure); no network rule is
   added, so all TCP bind/connect is denied when TCP is handled;
4. ``PR_SET_NO_NEW_PRIVS``, then ``landlock_restrict_self``;
5. the machine profile only (V3-P74 §6.6): the seccomp filter that refuses
   ``socket(AF_UNIX)``, ``socketpair(AF_UNIX)`` other than ``SOCK_STREAM``
   and io_uring, and ends a process of another architecture;
6. write ``SYNC_OK`` to the sync fd, close every fd >= 3, ``execvp``.
"""

from __future__ import annotations

import ctypes
import json
import os
import stat
import struct
import sys

SETUP_FAILURE_EXIT = 125
SYNC_OK = b"HIMARI_SANDBOX_ENFORCED\n"
MIN_LANDLOCK_ABI = 6

_SYS_CREATE_RULESET = 444
_SYS_ADD_RULE = 445
_SYS_RESTRICT_SELF = 446
_CREATE_RULESET_VERSION = 1 << 0
_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38

EXECUTE = 1 << 0
WRITE_FILE = 1 << 1
READ_FILE = 1 << 2
READ_DIR = 1 << 3
MAKE_CHAR = 1 << 6
MAKE_BLOCK = 1 << 11
IOCTL_DEV = 1 << 15
HANDLED_FS = (1 << 16) - 1  # every filesystem right through ABI 5
READ_RIGHTS = EXECUTE | READ_FILE | READ_DIR
DEVICE_RIGHTS = READ_FILE | WRITE_FILE
WRITE_ROOT_RIGHTS = HANDLED_FS & ~(MAKE_CHAR | MAKE_BLOCK | IOCTL_DEV)

NET_BIND_TCP = 1 << 0
NET_CONNECT_TCP = 1 << 1
HANDLED_NET = NET_BIND_TCP | NET_CONNECT_TCP  # handled with no allow rule: all TCP denied

SCOPE_ABSTRACT_UNIX_SOCKET = 1 << 0
SCOPE_SIGNAL = 1 << 1
SCOPED = SCOPE_ABSTRACT_UNIX_SOCKET | SCOPE_SIGNAL


# --- seccomp (V3-P74 §6.6) ----------------------------------------------------------
_PR_SET_SECCOMP = 22
_SECCOMP_MODE_FILTER = 2
_RET_KILL_PROCESS = 0x80000000
_RET_ERRNO = 0x00050000
_RET_ALLOW = 0x7FFF0000
_EACCES = 13
_ENOSYS = 38
_AF_UNIX = 1
_SOCK_STREAM = 1
_SOCK_FLAGS = 0o4000 | 0o2000000  # SOCK_NONBLOCK | SOCK_CLOEXEC
_LD_ABS_W, _JEQ_K, _JGE_K, _AND_K, _RET_K = 0x20, 0x15, 0x35, 0x54, 0x06
# struct seccomp_data: nr, arch, instruction_pointer, args[6] (each 64-bit, low half first)
_NR, _ARCH, _ARG0, _ARG1 = 0, 4, 16, 24
# machine -> (AUDIT_ARCH, socket, socketpair, first number of a foreign ABI or None)
ARCHES = {
    "aarch64": (0xC00000B7, 198, 199, None),
    "x86_64": (0xC000003E, 41, 53, 0x40000000),  # x32 numbers start here
}
IO_URING = (425, 426, 427)  # setup, enter, register: the same numbers on every 64-bit architecture


def unix_filter(machine: str) -> bytes:
    """The BPF program as packed ``sock_filter`` entries. ValueError for an
    architecture it does not know (the helper then fails closed)."""
    if machine not in ARCHES:
        raise ValueError(f"no seccomp filter for {machine}")
    arch, socket_nr, pair_nr, foreign = ARCHES[machine]
    program: list[tuple[int, int, int, int]] = []

    def op(code: int, k: int, jt: int = 0, jf: int = 0) -> None:
        program.append((code, jt, jf, k))

    op(_LD_ABS_W, _ARCH)
    op(_JEQ_K, arch, 1, 0)
    op(_RET_K, _RET_KILL_PROCESS)
    op(_LD_ABS_W, _NR)
    if foreign is not None:
        op(_JGE_K, foreign, 0, 1)
        op(_RET_K, _RET_KILL_PROCESS)
    for number in IO_URING:
        op(_JEQ_K, number, 0, 1)
        op(_RET_K, _RET_ERRNO | _ENOSYS)
    op(_JEQ_K, socket_nr, 2, 0)
    op(_JEQ_K, pair_nr, 5, 0)
    op(_RET_K, _RET_ALLOW)
    # socket(domain, ...): AF_UNIX refused
    op(_LD_ABS_W, _ARG0)
    op(_JEQ_K, _AF_UNIX, 0, 1)
    op(_RET_K, _RET_ERRNO | _EACCES)
    op(_RET_K, _RET_ALLOW)
    # socketpair(domain, type, ...): AF_UNIX only as SOCK_STREAM (a SOCK_DGRAM pair reaches host sockets)
    op(_LD_ABS_W, _ARG0)
    op(_JEQ_K, _AF_UNIX, 1, 0)
    op(_RET_K, _RET_ALLOW)
    op(_LD_ABS_W, _ARG1)
    op(_AND_K, 0xFFFFFFFF & ~_SOCK_FLAGS)
    op(_JEQ_K, _SOCK_STREAM, 0, 1)
    op(_RET_K, _RET_ALLOW)
    op(_RET_K, _RET_ERRNO | _EACCES)
    return b"".join(struct.pack("=HBBI", *entry) for entry in program)


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]


def install_unix_filter() -> None:
    """After no_new_privs: the filter for this process and everything it runs."""
    try:
        program = unix_filter(os.uname().machine)
    except ValueError as error:
        raise SetupError(str(error)) from None
    buffer = ctypes.create_string_buffer(program, len(program))
    prog = _SockFprog(len(program) // 8, ctypes.cast(buffer, ctypes.c_void_p))
    if _libc().prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER, ctypes.byref(prog), 0, 0) != 0:
        raise SetupError("seccomp filter refused")


# --- hidden paths (V3-P74 §6.1) --------------------------------------------------------
_ST_RDONLY = 1


def check_hidden(hidden: object) -> None:
    """Every hidden path is masked in the final mount tree: a folder is an
    empty read-only mount, a file an empty read-only file, each on another
    device than the host's original. Anything else: the payload must not run."""
    if not isinstance(hidden, list):
        raise SetupError("malformed hidden list")
    for entry in hidden:
        if not (isinstance(entry, list) and len(entry) == 3):
            raise SetupError("malformed hidden entry")
        path, kind, host_device = entry
        try:
            info = os.lstat(path)
            read_only = os.statvfs(path).f_flag & _ST_RDONLY
        except OSError as error:
            raise SetupError("a hidden path is not masked") from error
        if kind == "dir":
            masked = stat.S_ISDIR(info.st_mode) and not os.listdir(path)
        elif kind == "file":
            masked = stat.S_ISREG(info.st_mode) and info.st_size == 0
        else:
            masked = False
        if not (masked and read_only and info.st_dev != host_device):
            raise SetupError("a hidden path is not masked")


class _RulesetAttr(ctypes.Structure):
    _fields_ = [
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
        ("scoped", ctypes.c_uint64),
    ]


class _PathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


class SetupError(Exception):
    """A policy step failed; the payload must not run."""


def _libc() -> ctypes.CDLL:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    return libc


def landlock_abi() -> int:
    try:
        result = _libc().syscall(
            ctypes.c_long(_SYS_CREATE_RULESET), None, ctypes.c_size_t(0), ctypes.c_uint32(_CREATE_RULESET_VERSION)
        )
    except (OSError, AttributeError):
        return 0
    return int(result) if result > 0 else 0


def rules_for(policy: dict[str, object]) -> list[tuple[str, int]]:
    read_roots = policy["read_roots"]
    write_roots = policy["write_roots"]
    assert isinstance(read_roots, list) and isinstance(write_roots, list)
    rules = [(str(root), READ_RIGHTS) for root in read_roots]
    rules.append((str(policy["device_root"]), DEVICE_RIGHTS))
    rules.extend((str(root), WRITE_ROOT_RIGHTS) for root in write_roots)
    return rules


def enforce(policy: dict[str, object]) -> None:
    if landlock_abi() < MIN_LANDLOCK_ABI:
        raise SetupError("Landlock ABI below minimum")
    libc = _libc()
    tcp = policy.get("tcp", "deny")
    if tcp not in ("deny", "allow"):
        raise SetupError("malformed tcp policy")
    attr = _RulesetAttr(HANDLED_FS, HANDLED_NET if tcp == "deny" else 0, SCOPED)
    ruleset = libc.syscall(
        ctypes.c_long(_SYS_CREATE_RULESET), ctypes.byref(attr), ctypes.c_size_t(ctypes.sizeof(attr)), ctypes.c_uint32(0)
    )
    if ruleset < 0:
        raise SetupError("ruleset creation failed")
    try:
        for path, allowed in rules_for(policy):
            try:
                fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            except OSError as error:
                raise SetupError("policy root unavailable") from error
            try:
                rule = _PathBeneathAttr(allowed, fd)
                if libc.syscall(
                    ctypes.c_long(_SYS_ADD_RULE), ctypes.c_int(ruleset), ctypes.c_int(_RULE_PATH_BENEATH),
                    ctypes.byref(rule), ctypes.c_uint32(0),
                ) != 0:
                    raise SetupError("policy rule refused")
            finally:
                os.close(fd)
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise SetupError("no_new_privs failed")
        if libc.syscall(ctypes.c_long(_SYS_RESTRICT_SELF), ctypes.c_int(ruleset), ctypes.c_uint32(0)) != 0:
            raise SetupError("restrict_self failed")
    finally:
        os.close(ruleset)


def _fail() -> None:
    try:
        os.write(2, b"himari-sandbox: protected setup failed\n")
    except OSError:
        pass
    os._exit(SETUP_FAILURE_EXIT)


def main(argv: list[str]) -> None:
    try:
        separator = argv.index("--", 2)
        policy = json.loads(argv[1])
        payload = argv[separator + 1 :]
        if separator != 2 or not payload or not isinstance(policy, dict):
            raise SetupError("malformed helper invocation")
        sync_fd = int(policy["sync_fd"])
        if "hidden" in policy:
            check_hidden(policy["hidden"])
        seccomp = policy.get("seccomp")
        if seccomp not in (None, "unix"):
            raise SetupError("malformed seccomp policy")
        enforce(policy)
        if seccomp == "unix":
            install_unix_filter()
    except Exception:
        _fail()
        return
    try:
        os.write(sync_fd, SYNC_OK)
        os.close(sync_fd)
        os.closerange(3, 65536)  # the payload inherits exactly fd 0/1/2
    except OSError:
        _fail()
        return
    try:
        os.execvp(payload[0], payload)
    except OSError:
        os._exit(127)


if __name__ == "__main__":  # executed inside the sandbox
    main(sys.argv)
