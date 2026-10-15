"""Identity checks for the trees a server sandbox binds (V3-P59 §4.2).

Paths can reach one file or directory by more than their spelling: hard
links, a symbolic link in an ancestor, a bind mount alias. So every check
here works on what an already opened descriptor *is* — (st_dev, st_ino) —
never on how its path was written, and the sandbox binds that same
descriptor (``--ro-bind-fd`` / ``--bind-fd``).

- **P (protected):** every directory under the protected roots (the roots
  included) and every file there with more than one link. Gathered afresh
  at each launch; it follows mounts below a protected root.
- **M0:** no mount point strictly below a bound tree (mountinfo, and a
  device change while walking). bwrap binds recursively, and a file bind
  mount alias is a mount point too.
- **R0:** no directory of a bound tree, its root included, is in P.
- **R1:** no file of a bound tree with more than one link is in P.
- **R2:** a ``writable`` tree's files have no name outside that tree (the
  names found inside equal the link count).

A tree is walked one directory at a time, each opened by its name under its
parent's descriptor with ``O_NOFOLLOW``: no symbolic link is followed, and
at most two descriptors are open however deep or wide the tree is. Every
entry counts toward one bound (``MAX_IDENTITY_ENTRIES``) for P and all the
trees together; past it nothing is launched. Any error is a refusal.
"""

from __future__ import annotations

import os
import stat
from contextlib import closing
from collections.abc import Generator, Sequence
from dataclasses import dataclass

from himari.sandbox.api import MAX_IDENTITY_ENTRIES, SandboxAdmissionError

Identity = tuple[int, int]
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


@dataclass(frozen=True)
class BoundTree:
    fd: int  # an open directory: what is checked is what is bound
    writable: bool


class _Budget:
    def __init__(self, limit: int) -> None:
        self.left = limit

    def take(self) -> None:
        self.left -= 1
        if self.left < 0:
            raise SandboxAdmissionError("too many entries to verify before launching")


def _unescape(field: str) -> str:
    for code in ("\\040", "\\011", "\\012", "\\134"):
        field = field.replace(code, chr(int(code[1:], 8)))
    return field


def mounts_below(path: str, mountinfo: str) -> list[str]:
    """Mount points strictly below the canonical ``path``."""
    root = path.rstrip("/") + "/"
    found = []
    for line in mountinfo.splitlines():
        fields = line.split(" ")
        if len(fields) > 4 and _unescape(fields[4]).startswith(root):
            found.append(_unescape(fields[4]))
    return found


def _open_relative(root_fd: int, relative: tuple[str, ...]) -> int:
    """The directory ``relative`` below ``root_fd``, one name at a time."""
    fd = os.dup(root_fd)
    try:
        for name in relative:
            next_fd = os.open(name, _DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = next_fd
    except BaseException:
        os.close(fd)
        raise
    return fd


def _walk(root_fd: int, budget: _Budget, *, same_device: bool) -> Generator[os.stat_result]:
    """Every entry below ``root_fd`` (not the root), symbolic links not
    followed. With ``same_device``, a directory on another device is M0."""
    device = os.fstat(root_fd).st_dev
    pending: list[tuple[str, ...]] = [()]
    while pending:
        relative = pending.pop()
        fd = _open_relative(root_fd, relative)
        try:
            if same_device and os.fstat(fd).st_dev != device:
                raise SandboxAdmissionError("a bound tree contains a mount point")
            with os.scandir(fd) as items:
                for item in items:
                    budget.take()
                    info = item.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        if same_device and info.st_dev != device:
                            raise SandboxAdmissionError("a bound tree contains a mount point")
                        pending.append((*relative, item.name))
                    yield info
        finally:
            os.close(fd)


def protected_set(roots: Sequence[str], budget: _Budget) -> set[Identity]:
    """P: the directories (roots included) and multi-link files under ``roots``."""
    found: set[Identity] = set()
    for root in roots:
        try:
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        except FileNotFoundError:
            continue  # nothing there to protect
        try:
            info = os.fstat(fd)
            found.add((info.st_dev, info.st_ino))
            with closing(_walk(fd, budget, same_device=False)) as entries:
                for entry in entries:
                    if stat.S_ISDIR(entry.st_mode) or entry.st_nlink > 1:
                        found.add((entry.st_dev, entry.st_ino))
        finally:
            os.close(fd)
    return found


def check_trees(trees: Sequence[BoundTree], protected: Sequence[str], *, limit: int = MAX_IDENTITY_ENTRIES,
                mountinfo: str | None = None) -> None:
    """Raise SandboxAdmissionError unless every tree passes M0, R0, R1 and
    (when writable) R2. ``mountinfo`` overrides /proc/self/mountinfo (tests)."""
    try:
        if mountinfo is None:
            with open("/proc/self/mountinfo", encoding="utf-8", errors="surrogateescape") as handle:
                mountinfo = handle.read()
        budget = _Budget(limit)
        guarded = protected_set(protected, budget)
        for tree in trees:
            root = os.fstat(tree.fd)
            if (root.st_dev, root.st_ino) in guarded:
                raise SandboxAdmissionError("a bound tree is inside HIMARI's data")  # R0
            if mounts_below(os.readlink(f"/proc/self/fd/{tree.fd}"), mountinfo):
                raise SandboxAdmissionError("a bound tree contains a mount point")  # M0
            links: dict[Identity, list[int]] = {}  # file → [st_nlink, names found in this tree]
            with closing(_walk(tree.fd, budget, same_device=True)) as entries:
                for entry in entries:
                    identity = (entry.st_dev, entry.st_ino)
                    if stat.S_ISDIR(entry.st_mode):
                        if identity in guarded:
                            raise SandboxAdmissionError("a bound tree contains HIMARI's data")  # R0
                    elif entry.st_nlink > 1:
                        if identity in guarded:
                            raise SandboxAdmissionError("a bound tree has a hard link to HIMARI's data")  # R1
                        if tree.writable:
                            links.setdefault(identity, [entry.st_nlink, 0])[1] += 1
            if any(seen < nlink for nlink, seen in links.values()):
                raise SandboxAdmissionError("a writable tree has a file hard-linked from outside it")  # R2
    except SandboxAdmissionError:
        raise
    except (OSError, ValueError):
        raise SandboxAdmissionError("a bound tree could not be verified") from None
