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

import ctypes
import errno
import hashlib
import os
import posixpath
import re
import secrets
import shutil
import stat
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass

from himari.runner import api
from himari.runner.hidden import HiddenRules, under
from himari.sandbox.machine import Mask

MAX_PATH_BYTES = 4096
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

    def listing(self, path: object, cwd: str, limit: int) -> tuple[str, list[dict[str, object]], bool]:
        absolute = self.absolute(path, cwd)
        self._precheck(absolute)
        masks = self.rules.masks()
        if self.rules.is_hidden(absolute, masks):
            raise api.RunnerError(api.NOT_FOUND)
        fd = _open(absolute, os.O_RDONLY | os.O_DIRECTORY)
        try:
            real = self._judge(fd, masks)
            entries: list[dict[str, object]] = []
            truncated = False
            try:
                with os.scandir(fd) as items:
                    for item in items:
                        if self.rules.is_hidden(posixpath.join(real, item.name), masks):
                            continue
                        if len(entries) >= limit:
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
                        entries.append({"name": item.name, "kind": kind, "size": size})
            except OSError as error:
                raise _error(error) from None
            entries.sort(key=lambda e: str(e["name"]))
            return real, entries, truncated
        finally:
            os.close(fd)

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
        temp = f".himari-{secrets.token_hex(8)}.tmp"
        try:
            fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                         stat.S_IMODE(existing.st_mode) if existing is not None else 0o644, dir_fd=parent_fd)
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
            if expected is not None:
                try:
                    now = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                except OSError:
                    raise api.RunnerError(api.CHANGED) from None
                if self.identity(now) != list(expected):
                    raise api.RunnerError(api.CHANGED)
            os.replace(temp, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        except BaseException as error:
            try:
                os.unlink(temp, dir_fd=parent_fd)
            except OSError:
                pass
            if isinstance(error, OSError):
                raise _error(error) from None
            raise
        return existing is None

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
