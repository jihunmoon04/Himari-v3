"""File operations on the user's real paths (V3-P74 §8).

Reading (§8.2): the path is opened (links followed, ``/proc`` magic links
not), then judged by what was opened: the filesystem must be a disk one
(``fstatfs``), the real path (from ``/proc/self/fd``) must not be hidden.
A path naming ``/proc``, ``/sys``, ``/dev`` or ``/run`` is refused first.

Writing (§8.3): the parent folder is opened the same way and its real path
must be inside one of the call's Workspaces; a final component that is a
link is followed and judged again (at most 40 times). The write itself is
relative to the checked folder's descriptor: a temporary file, then a
rename. Hard links are allowed: a rename replaces the name, never the
shared content.

Errors are runner categories (``himari.runner.api``); host error text never
leaves this module. Paths in results are real paths: the model sees the
user's own paths (§8.1).
"""

from __future__ import annotations

import bisect
import ctypes
import errno
import hashlib
import os
import posixpath
import re
import secrets
import shutil
import stat
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass

from himari.runner import api
from himari.runner.hidden import HiddenRules, under
from himari.sandbox.machine import Mask

MAX_PATH_BYTES = 4096
MAX_ENUMERATED = 100_000  # a folder listing reads at most this many names (V3-P75 §5.1)
MAX_SNAPSHOT_BYTES = 1024 * 1024  # a replaced or deleted file's copy, for the web's previous versions (V3-P75 §7.4)
MAX_LINK_HOPS = 40
MAX_NAME_TRIES = 100
SPECIAL_ROOTS = ("/proc", "/sys", "/dev", "/run")
# fstatfs f_type of disk filesystems (§8.2): ext2/3/4, xfs, btrfs, f2fs, zfs, FUSE, overlay, NFS, SMB2, CIFS,
# vfat, exfat, ntfs3. Everything else (proc, sysfs, tmpfs, devtmpfs, cgroup2, debugfs, ...) is refused.
DISK_FILESYSTEMS = frozenset({
    0xEF53, 0x58465342, 0x9123683E, 0xF2F52010, 0x2FC12FC1, 0x65735546, 0x794C7630, 0x6969, 0xFE534D42,
    0xFF534D42, 0x4D44, 0x2011BAB0, 0x7366746E,
})
_CONVERSATION_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_ERRNO = {
    errno.ENOENT: api.NOT_FOUND,
    errno.ENOTDIR: api.NOT_DIRECTORY,
    errno.EISDIR: api.IS_DIRECTORY,
    errno.ELOOP: api.LINKS,
    errno.EEXIST: api.EXISTS,
    errno.EACCES: api.ACCESS,
    errno.EPERM: api.ACCESS,
    errno.EROFS: api.ACCESS,
    errno.ENAMETOOLONG: api.INVALID,
    errno.ENXIO: api.NOT_REGULAR,
}

_RENAME_NOREPLACE = 1
_NO_RENAMEAT2 = frozenset({errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP})  # a file system without RENAME_NOREPLACE
_RESOLVE_NO_MAGICLINKS = 0x02
_SYS_OPENAT2 = 437
_AT_FDCWD = -100


class _OpenHow(ctypes.Structure):
    _fields_ = [("flags", ctypes.c_uint64), ("mode", ctypes.c_uint64), ("resolve", ctypes.c_uint64)]


_libc = ctypes.CDLL(None, use_errno=True)
_libc.syscall.restype = ctypes.c_long
_libc.fstatfs.argtypes = [ctypes.c_int, ctypes.c_void_p]
_libc.renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
_libc.renameat2.restype = ctypes.c_int


def _error(error: OSError) -> api.RunnerError:
    return api.RunnerError(_ERRNO.get(error.errno or 0, api.OTHER))


def _open(path: str, flags: int) -> int:
    """``openat2(AT_FDCWD, path, flags|O_CLOEXEC, RESOLVE_NO_MAGICLINKS)``:
    ordinary links are followed, ``/proc/*/fd`` style links are not."""
    how = _OpenHow(flags | os.O_CLOEXEC, 0, _RESOLVE_NO_MAGICLINKS)
    fd = _libc.syscall(ctypes.c_long(_SYS_OPENAT2), ctypes.c_int(_AT_FDCWD), ctypes.c_char_p(os.fsencode(path)),
                       ctypes.byref(how), ctypes.c_size_t(ctypes.sizeof(how)))
    if fd < 0:
        err = ctypes.get_errno()
        raise _error(OSError(err, os.strerror(err)))
    return int(fd)


def filesystem_type(fd: int) -> int:
    buffer = ctypes.create_string_buffer(256)  # struct statfs: f_type first, a long on 64-bit Linux
    if _libc.fstatfs(fd, buffer) != 0:
        err = ctypes.get_errno()
        raise _error(OSError(err, os.strerror(err)))
    return ctypes.c_long.from_buffer(buffer).value & 0xFFFFFFFF


def _name_bytes(name: str) -> bytes:
    return name.encode("utf-8", "surrogateescape")


def _cursor_key(after: object) -> tuple[int, bytes] | None:
    """``d/<name>`` or ``f/<name>`` (a folder or anything else), as ``listing`` gives it."""
    if after is None:
        return None
    if not isinstance(after, str) or after[:2] not in ("d/", "f/") or len(after) > MAX_PATH_BYTES:
        raise api.RunnerError(api.INVALID, "a bad cursor")
    return (0 if after[0] == "d" else 1, _name_bytes(after[2:]))


def real_path(fd: int) -> str:
    return os.readlink(f"/proc/self/fd/{fd}")


def _unescape(field: str) -> str:
    """A mountinfo path: space, tab, newline and backslash are octal escapes."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def mount_type(path: str, mountinfo: str = "/proc/self/mountinfo") -> str | None:
    """The filesystem type of the mount ``path`` is on (the deepest mount
    point above it), as the mount table names it; None if unknown."""
    best: tuple[int, str] | None = None
    try:
        with open(mountinfo, encoding="utf-8", errors="surrogateescape") as table:
            for line in table:
                fields = line.split()
                if "-" not in fields:
                    continue
                point = _unescape(fields[4])
                kind = fields[fields.index("-") + 1]
                if under(path, point) and (best is None or len(point) >= best[0]):
                    best = (len(point), kind)
    except OSError:
        return None
    return None if best is None else best[1]


@dataclass(frozen=True)
class Host:
    """The runner's view of the user's files: whose home, what is hidden."""

    rules: HiddenRules
    himari_home: str
    scratch: str | None = None  # where copies of replaced files are made (the runner's state folder); None: /tmp

    @property
    def home(self) -> str:
        return self.rules.home

    def absolute(self, path: object, cwd: str) -> str:
        """The model's path as an absolute, lexically normal path: ``~`` is
        the home, a relative path is under ``cwd``."""
        if not isinstance(path, str) or "\0" in path or len(path.encode("utf-8", "surrogateescape")) > MAX_PATH_BYTES:
            raise api.RunnerError(api.INVALID)
        text = path.strip() or "."
        if text == "~" or text.startswith("~/"):
            text = self.home + text[1:]
        elif not text.startswith("/"):
            text = posixpath.join(cwd, text)
        return posixpath.normpath(text)

    def _judge(self, fd: int, masks: tuple[Mask, ...]) -> str:
        """The opened object's real path, if it may be shown at all."""
        real = real_path(fd)
        if any(under(real, root) for root in SPECIAL_ROOTS):
            raise api.RunnerError(api.NOT_REGULAR)
        if self.rules.is_hidden(real, masks):
            raise api.RunnerError(api.NOT_FOUND)
        if filesystem_type(fd) not in DISK_FILESYSTEMS:
            raise api.RunnerError(api.NOT_REGULAR)
        return real

    def _precheck(self, absolute: str) -> None:
        if any(under(absolute, root) for root in SPECIAL_ROOTS):
            raise api.RunnerError(api.NOT_REGULAR)

    # --- reading -------------------------------------------------------------------

    def open_read(self, path: object, cwd: str) -> tuple[int, str, os.stat_result]:
        """An open regular file the model may read, its real path and stat."""
        absolute = self.absolute(path, cwd)
        self._precheck(absolute)
        masks = self.rules.masks()
        if self.rules.is_hidden(absolute, masks):
            raise api.RunnerError(api.NOT_FOUND)
        fd = _open(absolute, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY)
        try:
            real = self._judge(fd, masks)
            info = os.fstat(fd)
            if stat.S_ISDIR(info.st_mode):
                raise api.RunnerError(api.IS_DIRECTORY)
            if not stat.S_ISREG(info.st_mode):
                raise api.RunnerError(api.NOT_REGULAR)
            os.set_blocking(fd, True)
            return fd, real, info
        except BaseException:
            os.close(fd)
            raise

    def listing(self, path: object, cwd: str, limit: int, after: object = None
                ) -> tuple[str, list[dict[str, object]], str | None]:
        """One page of a folder: every entry (but hidden ones) read and
        sorted, folders first, then by name as bytes; the page is the
        ``limit`` entries after the cursor ``after`` (V3-P75 §5.1). The
        cursor of its last entry when more follow, else None. Only the
        page's entries are stat'ed. A folder of more than MAX_ENUMERATED
        entries is refused rather than shown in part."""
        start_key = _cursor_key(after)
        absolute = self.absolute(path, cwd)
        self._precheck(absolute)
        masks = self.rules.masks()
        if self.rules.is_hidden(absolute, masks):
            raise api.RunnerError(api.NOT_FOUND)
        fd = _open(absolute, os.O_RDONLY | os.O_DIRECTORY)
        try:
            real = self._judge(fd, masks)
            found: list[tuple[int, bytes, str]] = []
            try:
                with os.scandir(fd) as items:
                    for item in items:
                        if self.rules.is_hidden(posixpath.join(real, item.name), masks):
                            continue
                        if len(found) >= MAX_ENUMERATED:
                            raise api.RunnerError(api.TOO_LARGE, f"this folder has more than {MAX_ENUMERATED:,} "
                                                                 "entries; open a folder inside it, or use run_command")
                        try:
                            folder = item.is_dir(follow_symlinks=False)
                        except OSError:
                            folder = False
                        found.append((0 if folder else 1, _name_bytes(item.name), item.name))
            except OSError as error:
                raise _error(error) from None
            found.sort()
            first = 0 if start_key is None else bisect.bisect_right(found, start_key, key=lambda e: (e[0], e[1]))
            entries: list[dict[str, object]] = []
            for _, _, name in found[first:first + limit]:
                try:
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except OSError:
                    continue  # gone since it was listed
                if stat.S_ISREG(info.st_mode):
                    kind, size = "file", info.st_size
                elif stat.S_ISDIR(info.st_mode):
                    kind, size = "directory", None
                elif stat.S_ISLNK(info.st_mode):
                    kind, size = "symlink", None
                else:
                    kind, size = "other", None
                entries.append({"name": name, "kind": kind, "size": size, "modified": int(info.st_mtime)})
            last = found[first:first + limit][-1:] if first + limit < len(found) else []
            return real, entries, (f"{'df'[last[0][0]]}/{last[0][2]}" if last else None)
        finally:
            os.close(fd)

    def visible_path(self, path: object, cwd: str, *, follow: bool = False) -> str:
        """A path as the web's previous versions know it (PR #94 review B3):
        its folder resolved and checked, its own name not hidden; the file
        itself need not exist (a deleted one's versions are read too).
        ``follow`` (V3-P76 §8, the path a write lands on): a final link is
        followed as ``target`` follows it, and folders a write would make
        may be missing."""
        if follow:
            return self._landing(path, cwd)
        absolute = self.absolute(path, cwd)
        self._precheck(absolute)
        folder, name = posixpath.split(absolute)
        if name in ("", ".", ".."):
            raise api.RunnerError(api.INVALID)
        masks = self.rules.masks()
        if self.rules.is_hidden(absolute, masks):
            raise api.RunnerError(api.NOT_FOUND)
        fd = _open(folder, os.O_RDONLY | os.O_DIRECTORY)
        try:
            real = posixpath.join(self._judge(fd, masks), name)
        finally:
            os.close(fd)
        if self.rules.is_hidden(real, masks):
            raise api.RunnerError(api.NOT_FOUND)
        return real

    def _landing(self, path: object, cwd: str) -> str:
        absolute = self.absolute(path, cwd)
        self._precheck(absolute)
        masks = self.rules.masks()
        for _ in range(MAX_LINK_HOPS):
            folder, name = posixpath.split(absolute)
            if name in ("", ".", ".."):
                raise api.RunnerError(api.IS_DIRECTORY)
            existing, missing = folder, []
            while not os.path.lexists(existing):
                missing.append(posixpath.basename(existing))
                existing = posixpath.dirname(existing)
            if self.rules.is_hidden(existing, masks):
                raise api.RunnerError(api.NOT_FOUND)
            fd = _open(existing, os.O_RDONLY | os.O_DIRECTORY)
            try:
                real_folder = posixpath.join(self._judge(fd, masks), *reversed(missing))
                link = None
                if not missing:
                    try:
                        if stat.S_ISLNK(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode):
                            link = os.readlink(name, dir_fd=fd)
                    except FileNotFoundError:
                        pass
                    except OSError as error:
                        raise _error(error) from None
            finally:
                os.close(fd)
            if link is not None:
                absolute = posixpath.normpath(posixpath.join(real_folder, link))
                continue
            real = posixpath.join(real_folder, name)
            if self.rules.is_hidden(real, masks):
                raise api.RunnerError(api.NOT_FOUND)
            return real
        raise api.RunnerError(api.LINKS)

    def stat_path(self, path: object, cwd: str) -> dict[str, object]:
        """What ``open`` checks, without handing anything over (V3-P75 §5.3):
        the real path, its kind and its identity; hidden: NOT_FOUND."""
        absolute = self.absolute(path, cwd)
        self._precheck(absolute)
        masks = self.rules.masks()
        if self.rules.is_hidden(absolute, masks):
            raise api.RunnerError(api.NOT_FOUND)
        fd = _open(absolute, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY)
        try:
            real = self._judge(fd, masks)
            info = os.fstat(fd)
        finally:
            os.close(fd)
        kind = "file" if stat.S_ISREG(info.st_mode) else "directory" if stat.S_ISDIR(info.st_mode) else "other"
        return {"path": real, "kind": kind, "identity": self.identity(info),
                "size": info.st_size if kind == "file" else None, "modified": int(info.st_mtime)}

    def directory(self, path: object, cwd: str) -> str:
        """A folder the model may see (a command's working directory): its real path."""
        absolute = self.absolute(path, cwd)
        self._precheck(absolute)
        masks = self.rules.masks()
        if self.rules.is_hidden(absolute, masks):
            raise api.RunnerError(api.NOT_FOUND)
        fd = _open(absolute, os.O_RDONLY | os.O_DIRECTORY)
        try:
            return self._judge(fd, masks)
        finally:
            os.close(fd)

    # --- registering a Workspace (§10.1, §14) ------------------------------------------

    def check_workspace(self, path: object, others: object, writable_under: Sequence[str]) -> dict[str, object]:
        """A folder the user wants to register: an existing folder the user
        may write, not hidden, on a disk filesystem, under ``writable_under``.
        Its real path, its filesystem as the mount table names it (e.g.
        ``fuse.rclone``), and which of ``others`` (registered locations) is
        the same folder seen at another path, if any (§9.1)."""
        if not isinstance(others, list) or len(others) > 1000 or not all(isinstance(o, str) for o in others):
            raise api.RunnerError(api.INVALID)
        absolute = self.absolute(path, self.home)
        self._precheck(absolute)
        masks = self.rules.masks()
        if self.rules.is_hidden(absolute, masks):
            raise api.RunnerError(api.NOT_FOUND)
        fd = _open(absolute, os.O_RDONLY | os.O_DIRECTORY)
        try:
            real = self._judge(fd, masks)
            if not any(under(real, w) for w in writable_under) or not os.access(real, os.W_OK | os.X_OK):
                raise api.RunnerError(api.ACCESS, "this folder cannot be a workspace: the user cannot write it")
            info = os.fstat(fd)
            same = None
            for other in others:
                assert isinstance(other, str)
                if not other.startswith("/") or other == real or self.rules.is_hidden(other, masks):
                    continue
                try:
                    seen = os.stat(other)
                except OSError:
                    continue
                if (seen.st_dev, seen.st_ino) == (info.st_dev, info.st_ino):
                    same = other
                    break
            return {"path": real, "filesystem": mount_type(real), "same_as": same}
        finally:
            os.close(fd)

    # --- the Workspaces of a call ----------------------------------------------------

    def roots(self, given: object, writable_under: Sequence[str]) -> list[str]:
        """The call's Workspaces as the runner accepts them (§5.3): real,
        existing folders, not hidden, on a disk filesystem, under
        ``writable_under``."""
        if not isinstance(given, list) or not given or not all(isinstance(r, str) for r in given):
            raise api.RunnerError(api.INVALID, "no workspace for this call")
        masks = self.rules.masks()
        accepted: list[str] = []
        for root in given:
            assert isinstance(root, str)
            if not root.startswith("/") or posixpath.normpath(root) != root:
                raise api.RunnerError(api.INVALID, "a workspace is an absolute, normal path")
            if self.rules.is_hidden(root, masks) or not any(under(root, w) for w in writable_under):
                raise api.RunnerError(api.ACCESS, "this folder cannot be a workspace")
            try:
                fd = _open(root, os.O_RDONLY | os.O_DIRECTORY)
            except api.RunnerError as error:  # moved, deleted, renamed since it was registered
                raise api.RunnerError(error.category, f"the workspace {root} cannot be used ({error}); ask the user "
                                                      "to detach it") from None
            try:
                if self._judge(fd, masks) != root:
                    raise api.RunnerError(api.INVALID, f"the workspace {root} is not where it was registered (a "
                                                       "link or mount changed); ask the user to detach it")
            finally:
                os.close(fd)
            accepted.append(root)
        return accepted

    @contextmanager
    def open_roots(self, roots: Sequence[str]) -> Iterator[list[tuple[str, int]]]:
        """Each accepted root opened as a directory, checked again by its fd."""
        opened: list[tuple[str, int]] = []
        try:
            for root in roots:
                fd = _open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                opened.append((root, fd))
                if real_path(fd) != root:
                    raise api.RunnerError(api.INVALID, "a workspace changed")
            yield opened
        finally:
            for _, fd in opened:
                os.close(fd)

    # --- writing -----------------------------------------------------------------------

    def _inside(self, real: str, roots: Sequence[str], masks: tuple[Mask, ...]) -> None:
        if self.rules.is_hidden(real, masks):
            raise api.RunnerError(api.NOT_FOUND)
        if not any(under(real, root) for root in roots):
            raise api.RunnerError(api.OUTSIDE_WORKSPACES)

    def _make_parents(self, absolute: str, roots: Sequence[str], masks: tuple[Mask, ...]) -> None:
        missing: list[str] = []
        existing = absolute
        while not os.path.lexists(existing):
            missing.append(posixpath.basename(existing))
            existing = posixpath.dirname(existing)
        if not missing:
            return
        fd = _open(existing, os.O_RDONLY | os.O_DIRECTORY)
        try:
            parent = self._judge(fd, masks)
            self._inside(parent, roots, masks)
            for name in reversed(missing):
                if name in ("", ".", ".."):
                    raise api.RunnerError(api.INVALID)
                # Each folder is judged before it is made (PR #87 review B2): a hidden or outside
                # path is never created, even when the write itself would then be refused.
                self._inside(posixpath.join(parent, name), roots, masks)
                try:
                    os.mkdir(name, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass
                except OSError as error:
                    raise _error(error) from None
                try:
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                except OSError as error:
                    raise _error(error) from None
                os.close(fd)
                fd = child
                parent = self._judge(fd, masks)  # and again by what was opened
                self._inside(parent, roots, masks)
        finally:
            os.close(fd)

    def target(self, path: object, cwd: str, roots: Sequence[str], *, follow: bool,
               create_parents: bool = False) -> tuple[int, str, str]:
        """(parent fd, parent real path, name) of a write target inside the
        call's Workspaces. ``follow``: a final link is followed and judged
        again; without it the link itself is the target (delete)."""
        absolute = self.absolute(path, cwd)
        self._precheck(absolute)
        masks = self.rules.masks()
        for _ in range(MAX_LINK_HOPS):
            parent, name = posixpath.split(absolute)
            if name in ("", ".", ".."):
                raise api.RunnerError(api.IS_DIRECTORY)
            if create_parents:
                self._make_parents(parent, roots, masks)
            fd = _open(parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                parent_real = self._judge(fd, masks)
                self._inside(parent_real, roots, masks)
                self._inside(posixpath.join(parent_real, name), roots, masks)
                try:
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    info = None
                except OSError as error:
                    raise _error(error) from None
                if follow and info is not None and stat.S_ISLNK(info.st_mode):
                    link = os.readlink(name, dir_fd=fd)
                    absolute = posixpath.normpath(posixpath.join(parent_real, link))
                    os.close(fd)
                    continue
                return fd, parent_real, name
            except BaseException:
                os.close(fd)
                raise
        raise api.RunnerError(api.LINKS)

    @staticmethod
    def identity(info: os.stat_result) -> list[int]:
        return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]

    def write(self, parent_fd: int, name: str, data: bytes | int, expected: Sequence[int] | None) -> bool:
        """Create or replace ``name`` atomically; True if it was created.
        ``data`` is bytes or an open file to copy. ``expected``: the file
        must still have that identity right before the rename (CHANGED)."""
        try:
            existing: os.stat_result | None = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        except OSError as error:
            raise _error(error) from None
        if existing is not None:
            if stat.S_ISDIR(existing.st_mode):
                raise api.RunnerError(api.IS_DIRECTORY)
            if not stat.S_ISREG(existing.st_mode):
                raise api.RunnerError(api.NOT_REGULAR)
        if expected is not None and (existing is None or self.identity(existing) != list(expected)):
            raise api.RunnerError(api.CHANGED)
        temp = self._write_temp(parent_fd, data, stat.S_IMODE(existing.st_mode) if existing is not None else 0o644)
        try:
            if expected is not None:
                try:
                    now = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                except OSError:
                    raise api.RunnerError(api.CHANGED) from None
                if self.identity(now) != list(expected):
                    raise api.RunnerError(api.CHANGED)
            os.replace(temp, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        except BaseException as error:
            _discard(parent_fd, temp)
            if isinstance(error, OSError):
                raise _error(error) from None
            raise
        return existing is None

    @staticmethod
    def _write_temp(parent_fd: int, data: bytes | int, mode: int) -> str:
        """A new temporary file beside the target, holding ``data``, synced."""
        temp = f".himari-{secrets.token_hex(8)}.tmp"
        try:
            fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC, mode,
                         dir_fd=parent_fd)
        except OSError as error:
            raise _error(error) from None
        try:
            try:
                if isinstance(data, int):
                    _copy(data, fd)
                else:
                    view = memoryview(data)
                    while view:
                        view = view[os.write(fd, view):]
                os.fsync(fd)
            finally:
                os.close(fd)
        except BaseException as error:
            _discard(parent_fd, temp)
            if isinstance(error, OSError):
                raise _error(error) from None
            raise
        return temp

    # --- the web editor's writes (V3-P75 §7) ---------------------------------------------

    def _hold(self, parent_fd: int, name: str, expected: Sequence[int]) -> tuple[int, os.stat_result]:
        """The file as it is, open to read (a link is not followed), if its
        identity is ``expected``; else CHANGED (gone, or another file now)."""
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent_fd)
        except FileNotFoundError:
            raise api.RunnerError(api.CHANGED) from None
        except OSError as error:
            raise _error(error) from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise api.RunnerError(api.NOT_REGULAR)
            if self.identity(info) != list(expected):
                raise api.RunnerError(api.CHANGED)
        except BaseException:
            os.close(fd)
            raise
        return fd, info

    def _copy_of(self, fd: int, size: int, limit: int = MAX_SNAPSHOT_BYTES) -> int | None:
        """A copy of the open file, in an unnamed file of the runner's own
        (None over ``limit``); taken before any rename or unlink, so later
        writes to the old file cannot change it (review C1)."""
        if size > limit:
            return None
        folder = self.scratch or tempfile.gettempdir()
        try:
            copy = os.open(folder, os.O_TMPFILE | os.O_RDWR | os.O_CLOEXEC, 0o600)
        except OSError:  # a filesystem without O_TMPFILE: made private and unnamed at once
            copy, made = tempfile.mkstemp(dir=folder)
            os.unlink(made)
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            _copy(fd, copy)
            os.lseek(copy, 0, os.SEEK_SET)
        except BaseException:
            os.close(copy)
            raise
        return copy

    def _unchanged(self, parent_fd: int, name: str, held: int, expected: Sequence[int]) -> None:
        """Right before the rename or unlink: the name is still that file, and
        nothing was written to it since it was copied (its mtime and size)."""
        try:
            now = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            raise api.RunnerError(api.CHANGED) from None
        if self.identity(now) != list(expected) or self.identity(os.fstat(held)) != list(expected):
            raise api.RunnerError(api.CHANGED)

    def replace(self, parent_fd: int, name: str, data: bytes | int, expected: Sequence[int],
                copy_max: int = MAX_SNAPSHOT_BYTES) -> tuple[list[int], int | None]:
        """V3-P75 §7.4: replace the file whose identity is ``expected`` with
        ``data``, atomically. Its new identity, and a copy of what it held
        (an fd the caller takes). TOO_LARGE over ``copy_max`` (V3-P77 §6.1:
        1 MiB for the editor, up to 64 MiB for a PDF), where no copy could be
        made; CHANGED, with nothing written, if it changed at any point before
        the rename."""
        held, info = self._hold(parent_fd, name, expected)
        copy: int | None = None
        try:
            if info.st_size > copy_max:  # its previous version could not be kept (PR #94 review B1)
                raise api.RunnerError(api.TOO_LARGE, f"files over {copy_max // (1024 * 1024)} MiB are not replaced "
                                                     "from the web")
            copy = self._copy_of(held, info.st_size, copy_max)
            temp = self._write_temp(parent_fd, data, stat.S_IMODE(info.st_mode))
            try:
                self._unchanged(parent_fd, name, held, expected)
                os.replace(temp, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            except BaseException as error:
                _discard(parent_fd, temp)
                if isinstance(error, OSError):
                    raise _error(error) from None
                raise
            try:
                now = self.identity(os.stat(name, dir_fd=parent_fd, follow_symlinks=False))
            except OSError:
                now = []  # replaced and gone again at once: the next save is told CHANGED
            return now, copy
        except BaseException:
            if copy is not None:
                os.close(copy)
            raise
        finally:
            os.close(held)

    def delete_checked(self, parent_fd: int, name: str, expected: Sequence[int],
                       copy_max: int = MAX_SNAPSHOT_BYTES) -> int | None:
        """§7.4: delete the file whose identity is ``expected``; a copy of what
        it held, taken before the unlink (or None over ``copy_max``)."""
        held, info = self._hold(parent_fd, name, expected)
        copy: int | None = None
        try:
            copy = self._copy_of(held, info.st_size, copy_max)
            self._unchanged(parent_fd, name, held, expected)
            try:
                os.unlink(name, dir_fd=parent_fd)
            except OSError as error:
                raise _error(error) from None
            return copy
        except BaseException:
            if copy is not None:
                os.close(copy)
            raise
        finally:
            os.close(held)

    # --- an agent's writes, kept for review (V3-P76 §4.1) -----------------------------------

    def kept_write(self, parent_fd: int, name: str, data: bytes,
                   expected: Sequence[int] | None) -> tuple[dict[str, object], int | None]:
        """Create or replace ``name`` with ``data``, keeping a copy of what it
        held (an fd, none over MAX_SNAPSHOT_BYTES). ``expected``: it must be
        that file (edit_file's read). A replaced file must not change between
        its copy and the rename; a new name is taken only if still free
        (RENAME_NOREPLACE, or O_EXCL where the file system has no such
        rename). Errors only before the name changes (``_installed``)."""
        try:
            held: int | None = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                                       dir_fd=parent_fd)
        except FileNotFoundError:
            held = None
        except OSError as error:
            raise _error(error) from None
        copy: int | None = None
        try:
            if held is None:
                if expected is not None:
                    raise api.RunnerError(api.CHANGED)
                return self._kept_create(parent_fd, name, data), None
            info = os.fstat(held)
            if stat.S_ISDIR(info.st_mode):
                raise api.RunnerError(api.IS_DIRECTORY)
            if not stat.S_ISREG(info.st_mode):
                raise api.RunnerError(api.NOT_REGULAR)
            prior = self.identity(info)
            if expected is not None and prior != list(expected):
                raise api.RunnerError(api.CHANGED)
            copy = self._copy_of(held, info.st_size)
            temp = self._write_temp(parent_fd, data, stat.S_IMODE(info.st_mode))
            try:
                self._unchanged(parent_fd, name, held, prior)
                os.replace(temp, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            except BaseException as error:
                _discard(parent_fd, temp)
                if isinstance(error, OSError):
                    raise _error(error) from None
                raise
            return self._installed(parent_fd, name, created=False, prior=prior), copy
        except BaseException:
            if copy is not None:
                os.close(copy)
            raise
        finally:
            if held is not None:
                os.close(held)

    def _kept_create(self, parent_fd: int, name: str, data: bytes) -> dict[str, object]:
        temp = self._write_temp(parent_fd, data, 0o644)
        if _libc.renameat2(parent_fd, os.fsencode(temp), parent_fd, os.fsencode(name), _RENAME_NOREPLACE) == 0:
            return self._installed(parent_fd, name, created=True, prior=[])
        err = ctypes.get_errno()
        _discard(parent_fd, temp)
        if err == errno.EEXIST:
            raise api.RunnerError(api.CHANGED)
        if err not in _NO_RENAMEAT2:
            raise _error(OSError(err, os.strerror(err)))
        # No RENAME_NOREPLACE here (some FUSE file systems): the name is taken with O_EXCL, never over a file, and
        # written in place. From the moment it is taken nothing raises: a write that fails leaves a file whose
        # content may be partial, and says so (it cannot be anyone else's: this call made it).
        try:
            fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644,
                         dir_fd=parent_fd)
        except FileExistsError:
            raise api.RunnerError(api.CHANGED) from None
        except OSError as error:
            raise _error(error) from None
        partial = False
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        except OSError:
            partial = True
        finally:
            try:
                os.close(fd)
            except OSError:
                partial = True
        return self._installed(parent_fd, name, created=True, prior=[], partial=partial)

    def _installed(self, parent_fd: int, name: str, *, created: bool, prior: list[int],
                   partial: bool = False) -> dict[str, object]:
        """What a kept write answers once the name has changed: never an error."""
        try:
            after: list[int] | None = self.identity(os.stat(name, dir_fd=parent_fd, follow_symlinks=False))
        except OSError:
            after = None
        return {"created": created, "prior_identity": prior, "after_identity": None if partial else after,
                "complete": after is not None and not partial, "partial": partial}

    def kept_delete(self, parent_fd: int, name: str) -> tuple[dict[str, object], int | None]:
        """Delete one file, keeping a copy of what it held (as ``kept_write``).
        A link (or other non-file) goes without a copy."""
        try:
            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as error:
            raise _error(error) from None
        if stat.S_ISDIR(info.st_mode):
            raise api.RunnerError(api.IS_DIRECTORY)
        prior = self.identity(info)
        if not stat.S_ISREG(info.st_mode):
            self.delete(parent_fd, name)
            return {"prior_identity": prior, "after_identity": [], "complete": True, "partial": False}, None
        copy = self.delete_checked(parent_fd, name, prior)
        return {"prior_identity": prior, "after_identity": [], "complete": True, "partial": False}, copy

    def install(self, parent_fd: int, name: str, data: bytes | int) -> list[int] | None:
        """``data`` given the name ``name`` in one step, only while it is free
        (V3-P76 §4.1): CHANGED if taken, UNSUPPORTED where the file system has
        no RENAME_NOREPLACE; nothing is written then. Its identity (None:
        placed, but not seen after)."""
        temp = self._write_temp(parent_fd, data, 0o644)
        if _libc.renameat2(parent_fd, os.fsencode(temp), parent_fd, os.fsencode(name), _RENAME_NOREPLACE) != 0:
            err = ctypes.get_errno()
            _discard(parent_fd, temp)
            if err == errno.EEXIST:
                raise api.RunnerError(api.CHANGED)
            if err in _NO_RENAMEAT2:
                raise api.RunnerError(api.UNSUPPORTED)
            raise _error(OSError(err, os.strerror(err)))
        try:
            return self.identity(os.stat(name, dir_fd=parent_fd, follow_symlinks=False))
        except OSError:
            return None

    def create_file(self, parent_fd: int, name: str) -> list[int]:
        """§7.7 (review C2): a new, empty file, or EXISTS if the name is taken
        (a link included). Nothing can fail after the name is taken."""
        try:
            fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644,
                         dir_fd=parent_fd)
        except OSError as error:
            raise _error(error) from None
        try:
            return self.identity(os.fstat(fd))
        finally:
            os.close(fd)

    @staticmethod
    def make_folder(parent_fd: int, name: str) -> None:
        """§7.5 (review N1): one new folder, 0755, in a folder that exists; EXISTS if taken."""
        try:
            os.mkdir(name, 0o755, dir_fd=parent_fd)
        except OSError as error:
            raise _error(error) from None

    @staticmethod
    def delete(parent_fd: int, name: str) -> None:
        """One file or link (never a folder)."""
        try:
            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                raise api.RunnerError(api.IS_DIRECTORY)
            os.unlink(name, dir_fd=parent_fd)
        except OSError as error:
            raise _error(error) from None

    # --- the Conversation's folder and uploads (V3-P66 §5.2) ----------------------------

    def conversation_folder(self, conversation_id: object) -> str:
        if not isinstance(conversation_id, str) or not _CONVERSATION_ID.match(conversation_id):
            raise api.RunnerError(api.INVALID)
        folder = posixpath.join(self.himari_home, "conversations", conversation_id)
        os.makedirs(folder, mode=0o755, exist_ok=True)
        return folder

    def remove_conversation_folder(self, conversation_id: object) -> bool:
        if not isinstance(conversation_id, str) or not _CONVERSATION_ID.match(conversation_id):
            raise api.RunnerError(api.INVALID)
        folder = posixpath.join(self.himari_home, "conversations", conversation_id)
        if not os.path.lexists(folder):
            return False
        if os.path.islink(folder) or not os.path.isdir(folder):
            os.unlink(folder)
            return True
        shutil.rmtree(folder)  # fd-based on Linux: links inside are removed, never followed
        return True

    def folder(self, path: object, roots: Sequence[str]) -> int:
        """An uploads folder inside the Workspaces, made if missing; its fd."""
        if not isinstance(path, str) or not path.startswith("/"):
            raise api.RunnerError(api.INVALID)
        masks = self.rules.masks()
        self._make_parents(posixpath.join(path, "x"), roots, masks)
        fd = _open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            self._inside(self._judge(fd, masks), roots, masks)
            return fd
        except BaseException:
            os.close(fd)
            raise

    @staticmethod
    def clear_temps(folder_fd: int, prefix: str) -> None:
        for entry in os.listdir(folder_fd):
            if entry.startswith(prefix) and entry.endswith(".tmp"):
                try:
                    if stat.S_ISREG(os.stat(entry, dir_fd=folder_fd, follow_symlinks=False).st_mode):
                        os.unlink(entry, dir_fd=folder_fd)
                except OSError:
                    pass

    @staticmethod
    def write_temp(folder_fd: int, source: int, prefix: str) -> str:
        temp = f"{prefix}{secrets.token_hex(8)}.tmp"
        try:
            fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644,
                         dir_fd=folder_fd)
        except OSError as error:
            raise _error(error) from None
        try:
            try:
                _copy(source, fd)
                os.fsync(fd)
            finally:
                os.close(fd)
        except BaseException:
            try:
                os.unlink(temp, dir_fd=folder_fd)
            except OSError:
                pass
            raise
        return temp

    @staticmethod
    def free_name(folder_fd: int, name: str, preferred: str | None) -> str:
        if "/" in name or name in ("", ".", ".."):
            raise api.RunnerError(api.INVALID)
        for candidate in ([preferred] if preferred else []) + list(_candidates(name)):
            if "/" in candidate or candidate in ("", ".", ".."):
                continue
            try:
                os.stat(candidate, dir_fd=folder_fd, follow_symlinks=False)
            except FileNotFoundError:
                return candidate
            except OSError as error:
                raise _error(error) from None
        raise api.RunnerError(api.EXISTS)

    @staticmethod
    def commit_temp(folder_fd: int, temp: str, target: str) -> bool:
        if "/" in temp or "/" in target or target in ("", ".", ".."):
            raise api.RunnerError(api.INVALID)
        if _libc.renameat2(folder_fd, os.fsencode(temp), folder_fd, os.fsencode(target), 1) != 0:
            err = ctypes.get_errno()
            if err == errno.EEXIST:
                return False
            raise _error(OSError(err, os.strerror(err)))
        os.fsync(folder_fd)
        return True

    @staticmethod
    def remove_temp(folder_fd: int, temp: str) -> None:
        if "/" in temp:
            return
        try:
            os.unlink(temp, dir_fd=folder_fd)
        except OSError:
            pass

    def holds(self, path: object, size: int, sha256: str) -> bool:
        try:
            fd, _, info = self.open_read(path, "/")
        except api.RunnerError:
            return False
        try:
            if info.st_size != size:
                return False
            digest = hashlib.sha256()
            while chunk := os.read(fd, 1 << 20):
                digest.update(chunk)
            return digest.hexdigest() == sha256
        finally:
            os.close(fd)


def _discard(parent_fd: int, temp: str) -> None:
    try:
        os.unlink(temp, dir_fd=parent_fd)
    except OSError:
        pass


def _copy(source: int, target: int) -> None:
    while chunk := os.read(source, 1 << 20):
        view = memoryview(chunk)
        while view:
            view = view[os.write(target, view):]


def _candidates(name: str) -> Iterator[str]:
    yield name
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext = name, ""
    for n in range(2, MAX_NAME_TRIES + 1):
        yield f"{stem} ({n}).{ext}" if ext else f"{stem} ({n})"
