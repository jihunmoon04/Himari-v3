"""Descriptor-relative filesystem mechanics for the Inside Environment.

Ported in spirit from HIMARI-v2 ``workspace/direct_fs.py`` (no code import):

- A Workspace root is opened once per operation (``O_DIRECTORY|O_NOFOLLOW``)
  and every further path is resolved *beneath that fd* with ``openat2`` and
  ``RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS |
  RESOLVE_NO_XDEV``: no ``..`` escape, no symlink component, no mount crossing.
- There is no fallback to ``open()``/``pathlib``; without ``openat2`` the
  operation fails closed.
- Only regular files are read or written, never FIFOs/devices/sockets, and a
  file with more than one hard link is refused (its inode may be shared
  outside the Workspace).
- Writes go to an ``O_EXCL`` temporary file in the same directory and are
  renamed into place, so readers never see a partial file.
- Errors are reduced to :class:`FsCategory`; host paths and raw errno text
  never leave this module.
"""

from __future__ import annotations

import ctypes
import errno
import os
import posixpath
import secrets
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum

RESOLVE_NO_XDEV = 0x01
RESOLVE_NO_MAGICLINKS = 0x02
RESOLVE_NO_SYMLINKS = 0x04
RESOLVE_BENEATH = 0x08
STRICT_RESOLVE = RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS | RESOLVE_NO_XDEV

_SYS_OPENAT2 = 437
MAX_PATH_BYTES = 4096
VIRTUAL_ROOT = "/workspace"

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY
_PATH_FLAGS = os.O_PATH | os.O_NOFOLLOW
_CREATE_FLAGS = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY


class FsCategory(Enum):
    OUTSIDE = "path is outside the workspace"
    INVALID = "invalid path"
    NOT_FOUND = "no such file or directory"
    NOT_DIRECTORY = "not a directory"
    IS_DIRECTORY = "is a directory"
    LINK = "symbolic links are not followed"
    CROSS_DEVICE = "crosses a mount boundary"
    EXISTS = "already exists"
    ACCESS = "permission denied"
    NOT_REGULAR = "not a regular file"
    HARDLINK = "file has multiple hard links"
    TOO_LARGE = "file is too large"
    UNSUPPORTED = "safe file access is not supported on this host"
    OTHER = "file operation failed"


class FsError(Exception):
    """A categorized refusal. ``str(error)`` is safe to show the model."""

    def __init__(self, category: FsCategory) -> None:
        super().__init__(category.value)
        self.category = category


_ERRNO = {
    errno.ENOENT: FsCategory.NOT_FOUND,
    errno.ENOTDIR: FsCategory.NOT_DIRECTORY,
    errno.EISDIR: FsCategory.IS_DIRECTORY,
    errno.ELOOP: FsCategory.LINK,
    errno.EXDEV: FsCategory.CROSS_DEVICE,
    errno.EEXIST: FsCategory.EXISTS,
    errno.EACCES: FsCategory.ACCESS,
    errno.EPERM: FsCategory.ACCESS,
    errno.ENOSYS: FsCategory.UNSUPPORTED,
    errno.E2BIG: FsCategory.UNSUPPORTED,
    errno.ENXIO: FsCategory.NOT_REGULAR,
    errno.EAGAIN: FsCategory.OTHER,
}


def _categorize(error: OSError) -> FsError:
    return FsError(_ERRNO.get(error.errno or 0, FsCategory.OTHER))


class _OpenHow(ctypes.Structure):
    _fields_ = [("flags", ctypes.c_uint64), ("mode", ctypes.c_uint64), ("resolve", ctypes.c_uint64)]


_syscall: object = None


def openat2(dirfd: int, relative: str, flags: int, *, mode: int = 0) -> int:
    """``openat2(dirfd, relative, {flags|O_CLOEXEC, mode, STRICT_RESOLVE})``."""
    global _syscall
    if not sys.platform.startswith("linux"):
        raise FsError(FsCategory.UNSUPPORTED)
    if _syscall is None:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall.restype = ctypes.c_long
        _syscall = libc.syscall
    if not relative or relative.startswith("/") or "\0" in relative:
        raise FsError(FsCategory.INVALID)
    path = os.fsencode(relative)
    if len(path) > MAX_PATH_BYTES:
        raise FsError(FsCategory.INVALID)
    fd, err = _raw_openat2(dirfd, path, flags, mode)
    if fd < 0:
        if err == errno.EINVAL:
            # EINVAL from openat2 itself means the kernel rejected the request shape.
            raise FsError(FsCategory.UNSUPPORTED)
        if err == errno.ENOTDIR and flags & os.O_DIRECTORY and _is_trailing_symlink(dirfd, path):
            # O_DIRECTORY|O_NOFOLLOW on a trailing symlink reports ENOTDIR;
            # it was not followed, but name the refusal precisely.
            raise FsError(FsCategory.LINK)
        raise _categorize(OSError(err, os.strerror(err)))
    return fd


def _raw_openat2(dirfd: int, path: bytes, flags: int, mode: int) -> tuple[int, int]:
    how = _OpenHow(flags | os.O_CLOEXEC, mode, STRICT_RESOLVE)
    fd = _syscall(  # type: ignore[operator]
        ctypes.c_long(_SYS_OPENAT2), ctypes.c_int(dirfd), ctypes.c_char_p(path),
        ctypes.byref(how), ctypes.c_size_t(ctypes.sizeof(how)),
    )
    return int(fd), (ctypes.get_errno() if fd < 0 else 0)


def _is_trailing_symlink(dirfd: int, path: bytes) -> bool:
    """With O_PATH|O_NOFOLLOW, openat2 returns the symlink itself (never its target)."""
    fd, _ = _raw_openat2(dirfd, path, _PATH_FLAGS, 0)
    if fd < 0:
        return False
    try:
        return stat.S_ISLNK(os.fstat(fd).st_mode)
    finally:
        os.close(fd)


# --- model path → Workspace-relative path --------------------------------------


def to_relative(cwd_relative: str, user_path: str) -> str:
    """Map a model-supplied path to a normalized path relative to the
    Workspace root ("." is the root). ``/workspace/...`` is absolute in the
    sandbox view; other paths are relative to the Conversation's cwd. Pure
    string work — ``openat2`` still enforces the boundary on every access."""
    if not isinstance(user_path, str) or "\0" in user_path or len(user_path.encode()) > MAX_PATH_BYTES:
        raise FsError(FsCategory.INVALID)
    path = user_path.strip() or "."
    if path == VIRTUAL_ROOT or path.startswith(VIRTUAL_ROOT + "/"):
        joined = path[len(VIRTUAL_ROOT):].lstrip("/") or "."
    elif path.startswith("/"):
        raise FsError(FsCategory.OUTSIDE)
    else:
        joined = posixpath.join(cwd_relative, path)
    normalized = posixpath.normpath(joined)
    if normalized == ".." or normalized.startswith("../") or normalized.startswith("/"):
        raise FsError(FsCategory.OUTSIDE)
    return normalized


def display(relative: str) -> str:
    """The sandbox-view path shown to the model; never a host path."""
    return VIRTUAL_ROOT if relative == "." else f"{VIRTUAL_ROOT}/{relative}"


def _split(relative: str) -> tuple[str, str]:
    if relative == ".":
        raise FsError(FsCategory.IS_DIRECTORY)
    parent, name = posixpath.split(relative)
    if name in ("", ".", ".."):
        raise FsError(FsCategory.INVALID)
    return parent or ".", name


# --- root -----------------------------------------------------------------------


@contextmanager
def open_root(host_root: str) -> Iterator[int]:
    """The one absolute open: the trusted Workspace root, never followed
    through a symlink in its last component."""
    try:
        fd = os.open(host_root, _DIRECTORY_FLAGS | os.O_CLOEXEC)
    except OSError as error:
        raise _categorize(error) from None
    try:
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _open_dir(root_fd: int, relative: str) -> Iterator[int]:
    fd = openat2(root_fd, relative, _DIRECTORY_FLAGS)
    try:
        yield fd
    finally:
        os.close(fd)


def ensure_directory(root_fd: int, relative: str) -> None:
    """``mkdir -p`` beneath the root, one verified component at a time."""
    if relative == ".":
        return
    fd = os.dup(root_fd)
    try:
        for component in relative.split("/"):
            if component in ("", ".", ".."):
                raise FsError(FsCategory.INVALID)
            try:
                os.mkdir(component, 0o755, dir_fd=fd)
            except FileExistsError:
                pass
            except OSError as error:
                raise _categorize(error) from None
            next_fd = openat2(fd, component, _DIRECTORY_FLAGS)
            os.close(fd)
            fd = next_fd
    finally:
        os.close(fd)


# --- operations ---------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    name: str
    kind: str  # "file" | "directory" | "symlink" | "other"
    size: int | None


def list_directory(root_fd: int, relative: str, max_entries: int) -> tuple[list[Entry], bool]:
    """Entries sorted by name; ``truncated`` if more than ``max_entries``."""
    entries: list[Entry] = []
    with _open_dir(root_fd, relative) as dir_fd:
        try:
            with os.scandir(dir_fd) as it:
                for item in it:
                    info = item.stat(follow_symlinks=False)
                    if stat.S_ISREG(info.st_mode):
                        kind, size = "file", info.st_size
                    elif stat.S_ISDIR(info.st_mode):
                        kind, size = "directory", None
                    elif stat.S_ISLNK(info.st_mode):
                        kind, size = "symlink", None
                    else:
                        kind, size = "other", None
                    entries.append(Entry(item.name, kind, size))
        except OSError as error:
            raise _categorize(error) from None
    entries.sort(key=lambda e: e.name)
    return entries[:max_entries], len(entries) > max_entries


def _check_regular(info: os.stat_result) -> None:
    if stat.S_ISDIR(info.st_mode):
        raise FsError(FsCategory.IS_DIRECTORY)
    if not stat.S_ISREG(info.st_mode):
        raise FsError(FsCategory.NOT_REGULAR)
    if info.st_nlink > 1:
        raise FsError(FsCategory.HARDLINK)


def read_file(root_fd: int, relative: str, max_bytes: int) -> tuple[bytes, int]:
    """Up to ``max_bytes`` of a regular file and its total size."""
    fd = openat2(root_fd, relative, _READ_FLAGS)
    try:
        info = os.fstat(fd)
        _check_regular(info)
        chunks: list[bytes] = []
        remaining = max_bytes
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks), info.st_size
    except OSError as error:
        raise _categorize(error) from None
    finally:
        os.close(fd)


def write_file(root_fd: int, relative: str, data: bytes, *, create_parents: bool) -> bool:
    """Atomically replace or create a regular file. Returns ``True`` if it was created."""
    parent, name = _split(relative)
    if create_parents:
        ensure_directory(root_fd, parent)
    with _open_dir(root_fd, parent) as dir_fd:
        created = True
        try:
            existing = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        except OSError as error:
            raise _categorize(error) from None
        else:
            if stat.S_ISLNK(existing.st_mode):
                raise FsError(FsCategory.LINK)
            _check_regular(existing)
            created = False
        temp = f".himari-{secrets.token_hex(8)}.tmp"
        try:
            fd = os.open(temp, _CREATE_FLAGS | os.O_CLOEXEC, 0o644, dir_fd=dir_fd)
        except OSError as error:
            raise _categorize(error) from None
        try:
            try:
                view = memoryview(data)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)  # replaces the entry; never follows
        except OSError as error:
            try:
                os.unlink(temp, dir_fd=dir_fd)
            except OSError:
                pass
            raise _categorize(error) from None
        return created


def delete_file(root_fd: int, relative: str) -> None:
    """Remove one regular file (not directories, not links)."""
    parent, name = _split(relative)
    with _open_dir(root_fd, parent) as dir_fd:
        try:
            info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError as error:
            raise _categorize(error) from None
        if stat.S_ISLNK(info.st_mode):
            raise FsError(FsCategory.LINK)
        _check_regular(info)
        try:
            os.unlink(name, dir_fd=dir_fd)
        except OSError as error:
            raise _categorize(error) from None
