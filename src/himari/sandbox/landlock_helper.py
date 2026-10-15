"""Trusted in-sandbox helper: enforce Landlock + no_new_privs, then exec.

Runs as ``python3 -I -S landlock_helper.py <policy-json> -- <argv...>`` inside
the bubblewrap namespaces, before the requested argv. Standard library only
(no HIMARI import, no site-packages). Ported from HIMARI-v2
``sandbox/landlock_exec.py`` with a v3 addition: Landlock network rules that
deny every TCP bind/connect, as a second layer under the network namespace.

Fail-closed order (exit ``SETUP_FAILURE_EXIT``; the argv never runs and
nothing is written to the sync fd):

1. Landlock ABI >= ``MIN_LANDLOCK_ABI`` (6: scoped abstract sockets/signals);
2. ruleset handling every filesystem right, TCP bind+connect, and scopes;
3. every path rule added (a missing root is a failure); no network rule is
   added, so all TCP bind/connect is denied;
4. ``PR_SET_NO_NEW_PRIVS``, then ``landlock_restrict_self``;
5. write ``SYNC_OK`` to the sync fd, close every fd >= 3, ``execvp``.
"""

from __future__ import annotations

import ctypes
import json
import os
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
    attr = _RulesetAttr(HANDLED_FS, HANDLED_NET, SCOPED)
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
        enforce(policy)
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
