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
import hashlib
import os
import posixpath
import secrets
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import BinaryIO

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
    CHANGED = "file changed while it was being edited; nothing written"
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


def ensure_directory(root_fd: int, relative: str, *, durable: bool = False) -> None:
    """``mkdir -p`` beneath the root, one verified component at a time.
    ``durable``: a directory it creates is made durable in its parent too."""
    if relative == ".":
        return
    fd = os.dup(root_fd)
    try:
        for component in relative.split("/"):
            if component in ("", ".", ".."):
                raise FsError(FsCategory.INVALID)
            try:
                os.mkdir(component, 0o755, dir_fd=fd)
                if durable:
                    os.fsync(fd)  # the new entry survives power loss (V3-P66 owner review N1)
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
    """At most ``max_entries`` entries, scanning at most ``max_entries + 1``
    directory items (bounded I/O and memory, whatever the directory size).
    ``truncated`` means more entries exist; the returned ones are then the
    first ones in directory order (sorted for display), not the first by name."""
    entries: list[Entry] = []
    truncated = False
    with _open_dir(root_fd, relative) as dir_fd:
        try:
            with os.scandir(dir_fd) as it:
                for item in it:
                    if len(entries) >= max_entries:
                        truncated = True
                        break
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
    return entries, truncated


def _check_regular(info: os.stat_result) -> None:
    if stat.S_ISDIR(info.st_mode):
        raise FsError(FsCategory.IS_DIRECTORY)
    if not stat.S_ISREG(info.st_mode):
        raise FsError(FsCategory.NOT_REGULAR)
    if info.st_nlink > 1:
        raise FsError(FsCategory.HARDLINK)


Identity = tuple[int, int, int, int]  # (st_dev, st_ino, st_size, st_mtime_ns)


def _identity(info: os.stat_result) -> Identity:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def read_file(root_fd: int, relative: str, max_bytes: int, offset: int = 0) -> tuple[bytes, int]:
    """Up to ``max_bytes`` of a regular file from ``offset`` on, and its total size."""
    data, size, _ = read_file_with_identity(root_fd, relative, max_bytes, offset)
    return data, size


def read_whole(root_fd: int, relative: str, max_bytes: int) -> tuple[bytes | None, int]:
    """A whole regular file and its size, or None and its size when it is
    larger than ``max_bytes`` (judged on the open file before reading, V3-P70 §4.4)."""
    fd = openat2(root_fd, relative, _READ_FLAGS)
    try:
        info = os.fstat(fd)
        _check_regular(info)
        if info.st_size > max_bytes:
            return None, info.st_size
        chunks: list[bytes] = []
        remaining = max_bytes + 1  # a file that grew meanwhile is caught, not cut
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        return (None, len(data)) if len(data) > max_bytes else (data, len(data))
    except OSError as error:
        raise _categorize(error) from None
    finally:
        os.close(fd)


def read_file_with_identity(root_fd: int, relative: str, max_bytes: int,
                            offset: int = 0) -> tuple[bytes, int, Identity]:
    """Like :func:`read_file`, plus the file's identity for an optimistic
    write-back check (see ``write_file(expected=...)``). ``offset``: read
    from that byte on (V3-P66 §5.2)."""
    fd = openat2(root_fd, relative, _READ_FLAGS)
    try:
        info = os.fstat(fd)
        _check_regular(info)
        if offset:
            os.lseek(fd, offset, os.SEEK_SET)
        chunks: list[bytes] = []
        remaining = max_bytes
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks), info.st_size, _identity(info)
    except OSError as error:
        raise _categorize(error) from None
    finally:
        os.close(fd)


def write_file(
    root_fd: int, relative: str, data: bytes, *, create_parents: bool, expected: Identity | None = None
) -> bool:
    """Atomically replace or create a regular file. Returns ``True`` if it was
    created. With ``expected``, the current file must still have that identity
    right before the rename, otherwise CHANGED and nothing is written. This
    narrows (does not close) the read-modify-write window; Workspace write
    serialization is the Scheduler's job (TOOL-03)."""
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
        if expected is not None and (created or _identity(existing) != expected):
            raise FsError(FsCategory.CHANGED)
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
            if expected is not None:
                try:
                    now = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                except OSError:
                    raise FsError(FsCategory.CHANGED) from None
                if _identity(now) != expected:
                    raise FsError(FsCategory.CHANGED)
            os.replace(temp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)  # replaces the entry; never follows
        except (OSError, FsError) as error:
            try:
                os.unlink(temp, dir_fd=dir_fd)
            except OSError:
                pass
            if isinstance(error, FsError):
                raise
            raise _categorize(error) from None
        return created


def identity(root_fd: int, relative: str) -> Identity:
    """The file's identity now, without opening it for reading."""
    fd = openat2(root_fd, relative, _PATH_FLAGS)
    try:
        info = os.fstat(fd)
    finally:
        os.close(fd)
    if stat.S_ISLNK(info.st_mode):
        raise FsError(FsCategory.LINK)
    _check_regular(info)
    return _identity(info)


# --- placing an upload (V3-P66 §5.2) --------------------------------------------

RENAME_NOREPLACE = 1
_renameat2: object = None
MAX_NAME_TRIES = 100


def rename_noreplace(dir_fd: int, old: str, new: str) -> None:
    """``renameat2(dir_fd, old, dir_fd, new, RENAME_NOREPLACE)``: atomic, one
    link at every moment (review N2); EXISTS if ``new`` is taken."""
    global _renameat2
    if _renameat2 is None:
        libc = ctypes.CDLL(None, use_errno=True)
        _renameat2 = libc.renameat2
        _renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        _renameat2.restype = ctypes.c_int
    if _renameat2(dir_fd, os.fsencode(old), dir_fd, os.fsencode(new), RENAME_NOREPLACE) != 0:  # type: ignore[operator]
        err = ctypes.get_errno()
        if err in (errno.EINVAL, errno.ENOSYS):
            raise FsError(FsCategory.UNSUPPORTED)
        raise _categorize(OSError(err, os.strerror(err)))


def _candidates(name: str) -> Iterator[str]:
    yield name
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext = name, ""
    for n in range(2, MAX_NAME_TRIES + 1):
        yield f"{stem} ({n}).{ext}" if ext else f"{stem} ({n})"


def clear_temps(root_fd: int, folder: str, temp_prefix: str) -> None:
    """Remove leftover temporary files (regular files only, links not
    followed). The caller holds the Workspace write lock, so none belongs
    to a live write."""
    with _open_dir(root_fd, folder) as dir_fd:
        for entry in os.listdir(dir_fd):
            if entry.startswith(temp_prefix) and entry.endswith(".tmp"):
                try:
                    if stat.S_ISREG(os.stat(entry, dir_fd=dir_fd, follow_symlinks=False).st_mode):
                        os.unlink(entry, dir_fd=dir_fd)
                except OSError:
                    pass


def write_temp(root_fd: int, folder: str, source: BinaryIO, temp_prefix: str) -> str:
    """Copy ``source`` into a new durable temporary file in ``folder``; its name."""
    with _open_dir(root_fd, folder) as dir_fd:
        temp = f"{temp_prefix}{secrets.token_hex(8)}.tmp"
        try:
            fd = os.open(temp, _CREATE_FLAGS | os.O_CLOEXEC, 0o644, dir_fd=dir_fd)
        except OSError as error:
            raise _categorize(error) from None
        try:
            try:
                while chunk := source.read(1 << 20):
                    view = memoryview(chunk)
                    while view:
                        view = view[os.write(fd, view):]
                os.fsync(fd)
            finally:
                os.close(fd)
        except BaseException as error:
            try:
                os.unlink(temp, dir_fd=dir_fd)
            except OSError:
                pass
            if isinstance(error, OSError):
                raise _categorize(error) from None
            raise
        return temp


def free_name(root_fd: int, folder: str, name: str, preferred: str | None = None) -> str:
    """``preferred`` (a name in ``folder``) if nothing is there, else the
    first of ``name``, ``name (2)``… that is free."""
    with _open_dir(root_fd, folder) as dir_fd:
        def taken(candidate: str) -> bool:
            try:
                os.stat(candidate, dir_fd=dir_fd, follow_symlinks=False)
                return True
            except FileNotFoundError:
                return False
            except OSError as error:
                raise _categorize(error) from None
        for candidate in ([preferred] if preferred else []) + list(_candidates(name)):
            if not taken(candidate):
                return candidate
    raise FsError(FsCategory.EXISTS)


def commit_temp(root_fd: int, folder: str, temp: str, target: str) -> bool:
    """Give the temporary file its name, never replacing anything, then make
    the directory durable; False if ``target`` is taken."""
    with _open_dir(root_fd, folder) as dir_fd:
        try:
            rename_noreplace(dir_fd, temp, target)
        except FsError as error:
            if error.category is FsCategory.EXISTS:
                return False
            raise
        os.fsync(dir_fd)
        return True


def remove_temp(root_fd: int, folder: str, temp: str) -> None:
    try:
        with _open_dir(root_fd, folder) as dir_fd:
            os.unlink(temp, dir_fd=dir_fd)
    except (OSError, FsError):
        pass


def same_content(root_fd: int, relative: str, size: int, sha256: str) -> bool:
    """Whether a regular file is there with exactly these bytes."""
    try:
        fd = openat2(root_fd, relative, _READ_FLAGS)
    except FsError:
        return False
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size != size:
            return False
        digest = hashlib.sha256()
        while chunk := os.read(fd, 1 << 20):
            digest.update(chunk)
        return digest.hexdigest() == sha256
    except OSError:
        return False
    finally:
        os.close(fd)


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


# --- removing a Conversation's folder (V3-P65 §5) ------------------------------

_MAX_DEPTH = 64  # deeper directories are moved up first, so descriptors and recursion stay bounded


def remove_tree(root_fd: int, relative: str) -> bool:
    """Remove ``relative`` and everything beneath it; False if it was not
    there. Never follows a symbolic link (a link is removed, its target
    stays) and never crosses a mount (CROSS_DEVICE, nothing more removed
    beneath it). Directories this user owns but cannot change (``chmod
    500``) are made writable first. A directory deeper than ``_MAX_DEPTH``
    is renamed to the top of the tree and cleared on a later pass."""
    parent, name = _split(relative)
    try:
        parent_fd = openat2(root_fd, parent, _DIRECTORY_FLAGS)
    except FsError as error:
        if error.category is FsCategory.NOT_FOUND:
            return False
        raise
    try:
        try:
            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if not stat.S_ISDIR(info.st_mode):
            os.unlink(name, dir_fd=parent_fd)  # a link or a file in the folder's place
            return True
        top_fd = _open_child(parent_fd, name)
        try:
            while _empty(top_fd, top_fd, 0):  # True: something deep was moved up; another pass
                pass
        finally:
            os.close(top_fd)
        os.rmdir(name, dir_fd=parent_fd)
        return True
    except OSError as error:
        raise _categorize(error) from None
    finally:
        os.close(parent_fd)


def _open_child(dir_fd: int, name: str) -> int:
    try:
        return openat2(dir_fd, name, _DIRECTORY_FLAGS)
    except FsError as error:
        if error.category is not FsCategory.ACCESS:
            raise
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise
        # The caller holds the Workspace lock: nothing in the Workspace swaps
        # this directory for a link meanwhile.
        os.chmod(name, 0o700, dir_fd=dir_fd)
        return openat2(dir_fd, name, _DIRECTORY_FLAGS)


def _empty(dir_fd: int, top_fd: int, depth: int) -> bool:
    """Remove every entry of the open directory; True if a directory was
    moved up to ``top_fd`` instead (it still has to be cleared)."""
    info = os.fstat(dir_fd)
    if info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) & 0o700 != 0o700:
        os.fchmod(dir_fd, stat.S_IMODE(info.st_mode) | 0o700)
    with os.scandir(dir_fd) as it:
        entries = [(entry.name, entry.is_dir(follow_symlinks=False)) for entry in it]
    moved = False
    for name, is_dir in entries:
        if not is_dir:
            os.unlink(name, dir_fd=dir_fd)
        elif depth >= _MAX_DEPTH:
            os.rename(name, f".himari-removing-{secrets.token_hex(8)}", src_dir_fd=dir_fd, dst_dir_fd=top_fd)
            moved = True
        else:
            child = _open_child(dir_fd, name)
            try:
                moved |= _empty(child, top_fd, depth + 1)
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=dir_fd)
    return moved

