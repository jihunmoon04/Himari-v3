"""What the sandbox self-tests share (V3-050): the report, the host paths
that must stay invisible, and the check of the mount table a probe read
inside the sandbox. The server profile's own test is
:mod:`himari.sandbox.server_self_test`.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SelfTestReport:
    passed: bool
    observed: dict[str, object] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    error: str = ""


# Host paths that must never be visible inside the sandbox; a profile adds
# the data roots it was started with.
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
