"""The machine profile's arguments and seccomp program (V3-P74 §6), without spawning."""

from __future__ import annotations

import json
import struct

import pytest

from himari.sandbox import landlock_helper
from himari.sandbox.api import TOOLKIT_MOUNT
from himari.sandbox.backend import HostCapability
from himari.sandbox.machine import MachineLayout, Mask, machine_arguments, machine_env, machine_policy

CAPABILITY = HostCapability("/usr/bin/bwrap", 8, "/opt/helper.py")
AF_UNIX, AF_INET = 1, 2
SOCK_STREAM, SOCK_DGRAM, SOCK_SEQPACKET = 1, 2, 5
SOCK_NONBLOCK, SOCK_CLOEXEC = 0o4000, 0o2000000


def layout(**changes: object) -> MachineLayout:
    base: dict[str, object] = dict(
        home="/home/u", cwd="/home/u/HIMARI/conversations/c1",
        writable=(("/home/u/HIMARI/conversations/c1", 11), ("/home/u", 10)),
        masks=(Mask("/home/u/.ssh", "dir", 2049), Mask("/home/u/.netrc", "file", 2049)),
        cache=("/home/u/.cache/himari-runner", 12))
    base.update(changes)
    return MachineLayout(**base)  # type: ignore[arg-type]


def test_mounts_follow_the_contract_order() -> None:
    """§6.1 (review N2): root and the sandbox's own trees, then Workspaces
    (shallow first), the cache, and the masks last."""
    args = machine_arguments(CAPABILITY, layout(), 5, ["true"], [20])
    flat = " ".join(args)
    order = [flat.index(part) for part in (
        "--ro-bind / /", "--tmpfs /run", "--bind-fd 10 /home/u ", "--bind-fd 11 /home/u/HIMARI/conversations/c1",
        "--bind-fd 12 /home/u/.cache/himari-runner", "--tmpfs /home/u/.ssh --remount-ro /home/u/.ssh",
        "--ro-bind-data 20 /home/u/.netrc", "--chdir")]
    assert order == sorted(order)
    assert "--unshare-all" in args and "--cap-drop" in args and "--clearenv" in args
    assert args[args.index("--chdir") + 1] == "/home/u/HIMARI/conversations/c1"


def test_each_hidden_file_gets_its_own_empty_fd() -> None:
    masks = (Mask("/h/.a", "file", 1), Mask("/h/.b", "dir", 1), Mask("/h/.c", "file", 1))
    args = machine_arguments(CAPABILITY, layout(masks=masks), 5, ["true"], [20, 21])
    assert args[args.index("/h/.a") - 1] == "20" and args[args.index("/h/.c") - 1] == "21"
    with pytest.raises(ValueError):
        machine_arguments(CAPABILITY, layout(masks=(Mask("/h/.x", "fifo", 1),)), 5, ["true"], [])


def test_policy_allows_loopback_tcp_and_installs_the_filter() -> None:
    policy = machine_policy(layout(), 5)
    assert policy["tcp"] == "allow" and policy["seccomp"] == "unix" and policy["read_roots"] == ["/"]
    assert policy["write_roots"] == ["/home/u/HIMARI/conversations/c1", "/home/u", "/tmp",
                                     "/home/u/.cache/himari-runner"]
    assert policy["hidden"] == [["/home/u/.ssh", "dir", 2049], ["/home/u/.netrc", "file", 2049]]
    args = machine_arguments(CAPABILITY, layout(), 5, ["true"], [20])
    assert json.loads(args[args.index("--") + 5])["sync_fd"] == 5


def test_environment() -> None:
    env = machine_env(layout(toolkit_fd=13))
    assert env["HOME"] == "/home/u" and env["PATH"].startswith("/home/u/.local/bin:")
    assert env["PIP_CACHE_DIR"] == "/home/u/.cache/himari-runner/pip"
    assert env["PYTHONPATH"] == f"{TOOLKIT_MOUNT}/site-packages"
    assert "XDG_CACHE_HOME" not in machine_env(layout(cache=None))


# --- the seccomp program, run by a small BPF interpreter -------------------------------------


def run_filter(program: bytes, arch: int, nr: int, args: tuple[int, ...] = (0, 0)) -> int:
    data = struct.pack("=iIQ", nr, arch, 0) + b"".join(struct.pack("=Q", a) for a in (*args, 0, 0, 0, 0)[:6])
    insns = [struct.unpack_from("=HBBI", program, i) for i in range(0, len(program), 8)]
    acc, pc = 0, 0
    while True:
        code, jt, jf, k = insns[pc]
        if code == 0x20:
            acc = int(struct.unpack_from("=I", data, k)[0])
        elif code == 0x54:
            acc &= k
        elif code == 0x15:
            pc += jt if acc == k else jf
        elif code == 0x35:
            pc += jt if acc >= k else jf
        elif code == 0x06:
            return int(k)
        else:
            raise AssertionError(f"unknown instruction {code:#x}")
        pc += 1


ALLOW, KILL = 0x7FFF0000, 0x80000000
EACCES, ENOSYS = 0x00050000 | 13, 0x00050000 | 38


@pytest.mark.parametrize("machine", sorted(landlock_helper.ARCHES))
def test_filter_decisions(machine: str) -> None:
    arch, socket_nr, pair_nr, foreign = landlock_helper.ARCHES[machine]
    program = landlock_helper.unix_filter(machine)

    def decide(nr: int, *args: int) -> int:
        return run_filter(program, arch, nr, args)

    assert decide(socket_nr, AF_UNIX, SOCK_STREAM) == EACCES
    assert decide(socket_nr, AF_INET, SOCK_STREAM) == ALLOW
    assert decide(pair_nr, AF_UNIX, SOCK_STREAM) == ALLOW
    assert decide(pair_nr, AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC) == ALLOW
    assert decide(pair_nr, AF_UNIX, SOCK_DGRAM) == EACCES  # review N7: a DGRAM pair reaches host sockets
    assert decide(pair_nr, AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC) == EACCES
    assert decide(pair_nr, AF_UNIX, SOCK_SEQPACKET) == EACCES
    assert decide(pair_nr, AF_INET, SOCK_DGRAM) == ALLOW
    for number in landlock_helper.IO_URING:
        assert decide(number) == ENOSYS
    assert decide(63) == ALLOW  # read() and the like
    assert run_filter(program, 0x40000003, socket_nr, (AF_UNIX, SOCK_STREAM)) == KILL  # another architecture
    if foreign is not None:
        assert decide(foreign + socket_nr, AF_UNIX, SOCK_STREAM) == KILL  # x32


def test_unknown_architecture_fails_closed() -> None:
    with pytest.raises(ValueError):
        landlock_helper.unix_filter("riscv64")


def test_hidden_check_refuses_malformed_entries() -> None:
    with pytest.raises(landlock_helper.SetupError):
        landlock_helper.check_hidden("nope")
    with pytest.raises(landlock_helper.SetupError):
        landlock_helper.check_hidden([["/etc", "dir", 1]])  # a real, readable host folder: not masked
