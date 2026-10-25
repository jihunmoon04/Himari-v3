"""Commands in their own cgroup (V3-P74 §6.5).

systemd delegates the service's cgroup (``Delegate=yes``). The runner moves
itself into a ``runner`` leaf and puts every command into a ``commands``
leaf with ``memory.max`` and ``pids.max``: a command that runs out of
memory is killed inside ``commands``, away from the runner. The service's
own limits stay above both; a service-wide OOM can still end the runner,
and then systemd starts it again (review N3).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

CGROUP_ROOT = "/sys/fs/cgroup"


class CgroupError(RuntimeError):
    pass


@dataclass(frozen=True)
class Commands:
    procs: str  # <commands>/cgroup.procs: a process writes 0 here to enter
    events: str  # <commands>/memory.events

    def oom_kills(self) -> int:
        try:
            with open(self.events) as handle:
                for line in handle:
                    key, _, value = line.partition(" ")
                    if key == "oom_kill":
                        return int(value)
        except (OSError, ValueError):
            pass
        return 0


def _write(path: str, value: str) -> None:
    with open(path, "w") as handle:
        handle.write(value)


def own_cgroup() -> str:
    with open("/proc/self/cgroup") as handle:
        for line in handle:
            if line.startswith("0::"):
                return CGROUP_ROOT + line[3:].strip().rstrip("/")
    raise CgroupError("no cgroup v2 entry")


def delegate(memory_max: str, pids_max: int, runner_memory_low: str) -> Commands:
    """Set up ``runner`` and ``commands`` under the delegated cgroup."""
    base = own_cgroup()
    runner, commands = os.path.join(base, "runner"), os.path.join(base, "commands")
    try:
        for leaf in (runner, commands):
            os.makedirs(leaf, exist_ok=True)
        _write(os.path.join(runner, "cgroup.procs"), "0")  # no process may stay in base (cgroup v2)
        _write(os.path.join(base, "cgroup.subtree_control"), "+memory +pids +cpu")
        _write(os.path.join(commands, "memory.max"), memory_max)
        _write(os.path.join(commands, "memory.swap.max"), "0")
        _write(os.path.join(commands, "pids.max"), str(pids_max))
        _write(os.path.join(runner, "memory.low"), runner_memory_low)
    except OSError as error:
        raise CgroupError(f"cannot set up the delegated cgroup: {error.strerror}") from None
    return Commands(os.path.join(commands, "cgroup.procs"), os.path.join(commands, "memory.events"))
