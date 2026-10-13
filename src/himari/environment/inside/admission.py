"""Whole-Workspace admission before a sandboxed command (adapted in spirit
from HIMARI-v2 ``workspace/admission.py``).

``inside/run_command`` binds the *entire* principal root into the sandbox
and the command uses ordinary file access, so the root must not contain:

- **nested mounts** — any mount point strictly below the root (from
  ``/proc/self/mountinfo``, which also catches same-device bind mounts) or a
  directory on another device;
- **external hard links** — a regular file whose link count is larger than
  the number of its links found inside the root has a link outside, so the
  sandbox could read or modify a host inode through it. Hard links that
  stay entirely inside the root are fine.

The scan walks from the already verified root fd with the same strict
``openat2`` resolution as the file Tools (no symlinks, no mount crossing)
and is bounded: beyond ``max_entries`` the command is refused rather than
declared safe. The caller keeps the same root fd open through the spawn.
"""

from __future__ import annotations

import os
import stat
from enum import Enum

from himari.environment.inside import fs

MAX_ADMISSION_ENTRIES = 200_000


class AdmissionCategory(Enum):
    NESTED_MOUNT = "the workspace contains a mount point"
    EXTERNAL_HARDLINK = "the workspace contains a file hard-linked from outside it"
    TOO_LARGE = "the workspace has too many entries to verify before running a command"
    UNREADABLE = "the workspace could not be verified"


class AdmissionError(Exception):
    def __init__(self, category: AdmissionCategory) -> None:
        super().__init__(category.value)
        self.category = category


def _unescape(field: str) -> str:
    for code in ("\\040", "\\011", "\\012", "\\134"):
        field = field.replace(code, chr(int(code[1:], 8)))
    return field


def mounts_below(host_root: str, mountinfo: str | None = None) -> list[str]:
    """Mount points strictly below ``host_root`` (by canonical path)."""
    root = os.path.realpath(host_root).rstrip("/") + "/"
    if mountinfo is None:
        with open("/proc/self/mountinfo", encoding="utf-8", errors="surrogateescape") as handle:
            mountinfo = handle.read()
    found = []
    for line in mountinfo.splitlines():
        fields = line.split(" ")
        if len(fields) > 4:
            point = _unescape(fields[4])
            if point.startswith(root):
                found.append(point)
    return found


def admit_workspace(
    root_fd: int, host_root: str, *, max_entries: int = MAX_ADMISSION_ENTRIES, mountinfo: str | None = None
) -> None:
    """Raise AdmissionError unless the whole root is safe to bind.
    ``mountinfo`` overrides /proc/self/mountinfo (tests only)."""
    if mounts_below(host_root, mountinfo):
        raise AdmissionError(AdmissionCategory.NESTED_MOUNT)
    try:
        device = os.fstat(root_fd).st_dev
    except OSError:
        raise AdmissionError(AdmissionCategory.UNREADABLE) from None
    links: dict[int, list[int]] = {}  # inode -> [st_nlink, links seen inside]
    entries = 0
    pending = [os.dup(root_fd)]
    try:
        while pending:
            dir_fd = pending.pop()
            try:
                with os.scandir(dir_fd) as items:
                    for item in items:
                        entries += 1
                        if entries > max_entries:
                            raise AdmissionError(AdmissionCategory.TOO_LARGE)
                        info = item.stat(follow_symlinks=False)
                        if stat.S_ISDIR(info.st_mode):
                            if info.st_dev != device:
                                raise AdmissionError(AdmissionCategory.NESTED_MOUNT)
                            try:
                                pending.append(fs.openat2(dir_fd, item.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
                            except fs.FsError as error:
                                category = (AdmissionCategory.NESTED_MOUNT if error.category is fs.FsCategory.CROSS_DEVICE
                                            else AdmissionCategory.UNREADABLE)
                                raise AdmissionError(category) from None
                        elif stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                            seen = links.setdefault(info.st_ino, [info.st_nlink, 0])
                            seen[1] += 1
            except OSError:
                raise AdmissionError(AdmissionCategory.UNREADABLE) from None
            finally:
                os.close(dir_fd)
    finally:
        for fd in pending:
            os.close(fd)
    if any(seen < nlink for nlink, seen in links.values()):
        raise AdmissionError(AdmissionCategory.EXTERNAL_HARDLINK)
