"""Conversations' own folders, in this process (V3-P109 §3, §4.1).

Every Conversation has one folder, ``<root>/<conversation_id>``, under
HIMARI's data root; nothing else is read or written. HIMARI is the only
writer, so a path is judged by its components: lexically inside the
folder (``..`` and absolute paths that leave it are OUTSIDE), and no
existing component — the folder itself included — may be a link (a
symbolic link or a Windows reparse point: LINKS). Opening uses
``O_NOFOLLOW`` where the platform has it.

Only portable ``os`` calls are used (no ``dir_fd``, ``pread`` or
``/proc``): the same code runs on Linux and on Windows (§7). Every method
blocks; callers run them in a worker thread (``run_blocking``).

Writing is atomic (a temporary file beside the target, synced, then
renamed). An upload's final name is never replaced: POSIX links the
temporary file to it (``link`` fails if the name exists), Windows renames
it (``rename`` fails if the name exists). Callers hold the folder's lock.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import secrets
import shutil
import stat
from collections.abc import Iterator
from typing import BinaryIO

from himari.environment.machine.errors import (
    CHANGED,
    EXISTS,
    INVALID,
    IS_DIRECTORY,
    LINKS,
    NOT_DIRECTORY,
    NOT_FOUND,
    NOT_REGULAR,
    ACCESS,
    OTHER,
    OUTSIDE,
    TOO_LARGE,
    FolderError,
)

MAX_PATH_CHARS = 4096
MAX_ENUMERATED = 100_000  # a folder listing reads at most this many names (V3-P75 §5.1)
MAX_NAME_TRIES = 100
UPLOADS = "uploads"
TEMP_PREFIX = ".himari-"
# Lower case only (Data makes "conv_<n>"): two ids never name one folder on a case-insensitive disk.
_CONVERSATION_ID = re.compile(r"^[a-z0-9_-]{1,128}$")
# Names Windows treats as devices, whatever follows a dot (V3-P109 review).
_PORT_DIGITS = "0123456789¹²³"  # Windows also takes COM¹, COM², COM³ (and LPT) as devices
_RESERVED = frozenset({"con", "prn", "aux", "nul", "conin$", "conout$",
                       *(f"com{d}" for d in _PORT_DIGITS), *(f"lpt{d}" for d in _PORT_DIGITS)})
_REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_BINARY = getattr(os, "O_BINARY", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_POSIX = os.name == "posix"
_ERRNO = {
    errno.ENOENT: NOT_FOUND,
    errno.ENOTDIR: NOT_DIRECTORY,
    errno.EISDIR: IS_DIRECTORY,
    errno.ELOOP: LINKS,
    errno.EEXIST: EXISTS,
    errno.EACCES: ACCESS,
    errno.EPERM: ACCESS,
    errno.EROFS: ACCESS,
    errno.ENAMETOOLONG: INVALID,
    errno.EFBIG: TOO_LARGE,
}


def _error(error: OSError) -> FolderError:
    return FolderError(_ERRNO.get(error.errno or 0, OTHER))


def is_link(info: os.stat_result) -> bool:
    """A symbolic link, or (Windows) any reparse point such as a junction."""
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)


def identity(info: os.stat_result) -> list[int]:
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


def _lstat(path: str) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise _error(error) from None


def _inside(path: str, folder: str) -> bool:
    try:
        return os.path.commonpath([os.path.normcase(path), os.path.normcase(folder)]) == os.path.normcase(folder)
    except ValueError:  # another drive (Windows)
        return False


def _component_ok(name: str) -> bool:
    """One path component that names exactly one thing on this platform. On
    Windows: no drive or stream colon, no trailing dot or space (aliases of
    another name), no device name."""
    if not name or name in (".", "..") or "\0" in name:
        return False
    if _POSIX:
        return True
    return (":" not in name and not name.endswith((".", " ")) and "/" not in name
            and name.split(".", 1)[0].rstrip(" ").lower() not in _RESERVED)


def _sync_folder(path: str) -> None:
    """Make a rename in ``path`` durable (POSIX; Windows has no folder sync)."""
    if not _POSIX:
        return
    fd = os.open(path, os.O_RDONLY | _CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class ConversationFolders:
    def __init__(self, root: str) -> None:
        if not os.path.isabs(root):
            raise ValueError("the folders' root is an absolute path")
        self.root = os.path.normpath(root)

    # --- a Conversation's folder ------------------------------------------------------

    def path_of(self, conversation_id: object) -> str:
        if not isinstance(conversation_id, str) or not _CONVERSATION_ID.match(conversation_id):
            raise FolderError(INVALID)
        return os.path.join(self.root, conversation_id)

    def _private_dir(self, path: str) -> None:
        """``path`` as a folder that is not a link, made (0700) if missing."""
        info = _lstat(path)
        if info is None:
            try:
                os.mkdir(path, 0o700)
            except FileExistsError:
                pass
            except OSError as error:
                raise _error(error) from None
            info = _lstat(path)
            if info is None:
                raise FolderError(NOT_FOUND)
        if is_link(info):
            raise FolderError(LINKS)
        if not stat.S_ISDIR(info.st_mode):
            raise FolderError(NOT_DIRECTORY)

    def folder(self, conversation_id: object) -> str:
        """The Conversation's folder, made if missing."""
        path = self.path_of(conversation_id)
        self._private_dir(self.root)
        self._private_dir(path)
        return path

    def remove(self, conversation_id: object) -> bool:
        """The Conversation's folder and everything in it; links inside are
        removed, never followed. False if there was none."""
        path = self.path_of(conversation_id)
        root = _lstat(self.root)
        if root is None:
            return False
        if is_link(root) or not stat.S_ISDIR(root.st_mode):  # never follow a linked root into another tree
            raise FolderError(LINKS)
        info = _lstat(path)
        if info is None:
            return False
        try:
            if is_link(info) or not stat.S_ISDIR(info.st_mode):
                os.unlink(path)
            else:
                shutil.rmtree(path)
        except OSError as error:
            raise _error(error) from None
        return True

    # --- paths ---------------------------------------------------------------------------

    def resolve(self, folder: str, path: object) -> str:
        """The model's path as an absolute, normal path inside ``folder``:
        relative paths are under it. OUTSIDE otherwise."""
        if not isinstance(path, str) or "\0" in path or len(path) > MAX_PATH_CHARS:
            raise FolderError(INVALID)
        text = path.strip() or "."
        if text.startswith("~"):
            raise FolderError(INVALID, "~ is not used here: paths are inside this conversation's folder, or "
                                       "relative to it")
        if not _POSIX:
            # Judged as written, before normalizing drops what Windows would read differently (V3-P109
            # re-review): a drive-relative path ("D:notes") and every component as given.
            drive, rest = os.path.splitdrive(text)
            if drive and not os.path.isabs(text):
                raise FolderError(INVALID)
            if not all(_component_ok(part) for part in re.split(r"[\\/]", rest) if part not in ("", ".", "..")):
                raise FolderError(INVALID)
        absolute = os.path.normpath(text if os.path.isabs(text) else os.path.join(folder, text))
        if not _inside(absolute, folder):
            raise FolderError(OUTSIDE)
        # Rebuilt on the trusted spelling of ``folder`` (a case-insensitive disk takes "C1" for "c1"), each
        # component checked to name one thing only.
        relative = os.path.relpath(absolute, folder)
        if relative == os.curdir:
            return folder
        parts = relative.split(os.sep)
        if not all(_component_ok(part) for part in parts):
            raise FolderError(INVALID)
        return os.path.join(folder, *parts)

    def _walk(self, folder: str, absolute: str) -> Iterator[tuple[str, os.stat_result | None]]:
        """Each component from ``folder`` down to ``absolute`` with its lstat
        (None once one is missing). LINKS for a link on the way."""
        relative = os.path.relpath(absolute, folder)
        parts = [] if relative == os.curdir else relative.split(os.sep)
        current = folder
        info = _lstat(current)
        yield current, info
        for part in parts:
            if info is None:
                current = os.path.join(current, part)
                yield current, None
                continue
            if is_link(info):
                raise FolderError(LINKS)
            if not stat.S_ISDIR(info.st_mode):
                raise FolderError(NOT_DIRECTORY)
            current = os.path.join(current, part)
            info = _lstat(current)
            yield current, info

    def _existing(self, folder: str, absolute: str) -> os.stat_result:
        """``absolute``'s lstat, with no link on the way or at the end."""
        last: os.stat_result | None = None
        for _, last in self._walk(folder, absolute):
            pass
        if last is None:
            raise FolderError(NOT_FOUND)
        if is_link(last):
            raise FolderError(LINKS)
        return last

    def _parent(self, folder: str, absolute: str, *, create: bool) -> str:
        """The parent folder of a write target: inside, no links, made if
        ``create``. The target itself is not judged here."""
        if os.path.normcase(absolute) == os.path.normcase(folder):
            raise FolderError(IS_DIRECTORY)
        parent = os.path.dirname(absolute)
        for current, info in self._walk(folder, parent):
            if info is None:
                if not create:
                    raise FolderError(NOT_FOUND)
                try:
                    os.mkdir(current, 0o700)
                except FileExistsError:
                    pass
                except OSError as error:
                    raise _error(error) from None
                made = _lstat(current)
                if made is None or is_link(made) or not stat.S_ISDIR(made.st_mode):
                    raise FolderError(NOT_DIRECTORY)
            elif is_link(info):
                raise FolderError(LINKS)
            elif not stat.S_ISDIR(info.st_mode):
                raise FolderError(NOT_DIRECTORY)
        return parent

    # --- reading -------------------------------------------------------------------------

    def open_read(self, folder: str, path: object) -> tuple[BinaryIO, str, os.stat_result]:
        """An open regular file (the caller closes it), its path and stat."""
        absolute = self.resolve(folder, path)
        info = self._existing(folder, absolute)
        if stat.S_ISDIR(info.st_mode):
            raise FolderError(IS_DIRECTORY)
        if not stat.S_ISREG(info.st_mode):
            raise FolderError(NOT_REGULAR)
        try:
            fd = os.open(absolute, os.O_RDONLY | _NOFOLLOW | _BINARY | _CLOEXEC)
        except OSError as error:
            raise _error(error) from None
        handle = os.fdopen(fd, "rb")
        try:
            opened = os.fstat(handle.fileno())
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino) or not stat.S_ISREG(opened.st_mode):
                raise FolderError(CHANGED, "the file changed while it was being opened")
            return handle, absolute, opened
        except BaseException:
            handle.close()
            raise

    def listing(self, folder: str, path: object, limit: int) -> tuple[str, list[dict[str, object]], bool]:
        """The first ``limit`` entries of a folder, folders first, then by
        name as UTF-8 bytes (V3-P75 §5.1), and whether more follow. A folder
        of more than MAX_ENUMERATED entries is refused rather than shown in part."""
        absolute = self.resolve(folder, path)
        info = self._existing(folder, absolute)
        if not stat.S_ISDIR(info.st_mode):
            raise FolderError(NOT_DIRECTORY)
        found: list[tuple[int, bytes, str]] = []
        try:
            with os.scandir(absolute) as items:
                for item in items:
                    if len(found) >= MAX_ENUMERATED:
                        raise FolderError(TOO_LARGE, f"this folder has more than {MAX_ENUMERATED:,} entries; "
                                                     "list a folder inside it")
                    try:
                        directory = item.is_dir(follow_symlinks=False) and not item.is_junction()
                    except OSError:
                        directory = False
                    found.append((0 if directory else 1, item.name.encode("utf-8", "surrogateescape"), item.name))
        except OSError as error:
            raise _error(error) from None
        found.sort()
        entries: list[dict[str, object]] = []
        for _, _, name in found[:limit]:
            entry = _lstat(os.path.join(absolute, name))
            if entry is None:
                continue  # gone since it was listed
            if is_link(entry):
                kind, size = "symlink", None
            elif stat.S_ISREG(entry.st_mode):
                kind, size = "file", entry.st_size
            elif stat.S_ISDIR(entry.st_mode):
                kind, size = "directory", None
            else:
                kind, size = "other", None
            entries.append({"name": name, "kind": kind, "size": size})
        return absolute, entries, len(found) > limit

    # --- writing -------------------------------------------------------------------------

    def write(self, folder: str, path: object, data: bytes, *, create_parents: bool = False,
              expected: list[int] | None = None) -> tuple[str, bool]:
        """Create or replace a file atomically; its path and whether it was
        created. ``expected``: the identity it must still have right before
        the rename (CHANGED)."""
        absolute = self.resolve(folder, path)
        parent = self._parent(folder, absolute, create=create_parents)
        existing = _lstat(absolute)
        if existing is not None:
            if is_link(existing):
                raise FolderError(LINKS)
            if stat.S_ISDIR(existing.st_mode):
                raise FolderError(IS_DIRECTORY)
            if not stat.S_ISREG(existing.st_mode):
                raise FolderError(NOT_REGULAR)
        if expected is not None and (existing is None or identity(existing) != list(expected)):
            raise FolderError(CHANGED)
        mode = stat.S_IMODE(existing.st_mode) if existing is not None else 0o600
        temp = self._temp(parent, TEMP_PREFIX, lambda fd: _write_all(fd, data), mode)
        try:
            if expected is not None:
                now = _lstat(absolute)
                if now is None or identity(now) != list(expected):
                    raise FolderError(CHANGED)
            os.replace(temp, absolute)
        except BaseException as error:
            _discard(temp)
            if isinstance(error, OSError):
                raise _error(error) from None
            raise
        _sync_folder(parent)
        return absolute, existing is None

    def delete(self, folder: str, path: object) -> str:
        """One file, or a link itself (never what it points to, never a folder)."""
        absolute = self.resolve(folder, path)
        self._parent(folder, absolute, create=False)
        info = _lstat(absolute)
        if info is None:
            raise FolderError(NOT_FOUND)
        if stat.S_ISDIR(info.st_mode) and not is_link(info):
            raise FolderError(IS_DIRECTORY)
        try:
            os.unlink(absolute)
        except OSError as error:
            raise _error(error) from None
        return absolute

    @staticmethod
    def _temp(parent: str, prefix: str, fill: object, mode: int) -> str:
        """A new temporary file in ``parent``, filled by ``fill(fd)`` and synced."""
        temp = os.path.join(parent, f"{prefix}{secrets.token_hex(8)}.tmp")
        try:
            fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | _NOFOLLOW | _BINARY | _CLOEXEC, mode)
        except OSError as error:
            raise _error(error) from None
        try:
            try:
                fill(fd)  # type: ignore[operator]
                if _POSIX:
                    os.fchmod(fd, mode)  # after the write, which would clear set-id bits; the umask takes none
                os.fsync(fd)
            finally:
                os.close(fd)
        except BaseException as error:
            _discard(temp)
            if isinstance(error, OSError):
                raise _error(error) from None
            raise
        return temp

    # --- uploads (V3-P66 §5.2) -----------------------------------------------------------

    def uploads(self, folder: str) -> str:
        """The Conversation's ``uploads`` folder, made if missing."""
        path = os.path.join(folder, UPLOADS)
        self._parent(folder, os.path.join(path, "x"), create=True)
        return path

    def copy_in(self, folder: str, source: BinaryIO, prefix: str, *, clear: bool = False) -> str:
        """``source`` copied to a new temporary file in ``uploads``; its name.
        ``clear``: first remove temporary files left by earlier attempts."""
        uploads = self.uploads(folder)
        if clear:
            for name in os.listdir(uploads):
                if name.startswith(prefix) and name.endswith(".tmp"):
                    found = _lstat(os.path.join(uploads, name))
                    if found is not None and stat.S_ISREG(found.st_mode):
                        _discard(os.path.join(uploads, name))
        temp = self._temp(uploads, prefix, lambda fd: _copy(source, fd), 0o600)
        return os.path.basename(temp)

    def free_path(self, folder: str, name: str, preferred: str | None) -> str:
        """A name in ``uploads`` that no file has: ``preferred`` (a path in
        ``uploads``) if free, else ``name``, ``name (2)``, …"""
        uploads = self.uploads(folder)
        if not _plain(name):
            raise FolderError(INVALID)
        wanted = (os.path.basename(preferred) if isinstance(preferred, str)
                  and os.path.normcase(os.path.dirname(preferred)) == os.path.normcase(uploads) else None)
        for candidate in ([wanted] if wanted else []) + list(_candidates(name)):
            if not _plain(candidate):
                continue
            if _lstat(os.path.join(uploads, candidate)) is None:
                return os.path.join(uploads, candidate)
        raise FolderError(EXISTS)

    def commit(self, folder: str, temp: str, path: str) -> bool:
        """The temporary file under ``path``'s name, never replacing a file:
        False if the name is taken (the temporary file stays)."""
        uploads = self.uploads(folder)
        if not _plain(temp) or os.path.normcase(os.path.dirname(path)) != os.path.normcase(uploads) \
                or not _plain(os.path.basename(path)):
            raise FolderError(INVALID)
        source = os.path.join(uploads, temp)
        found = _lstat(source)
        if not temp.endswith(".tmp") or found is None or is_link(found) or not stat.S_ISREG(found.st_mode):
            raise FolderError(INVALID)
        try:
            if _POSIX:
                os.link(source, path)
                os.unlink(source)
            else:
                os.rename(source, path)
        except FileExistsError:
            return False
        except OSError as error:
            raise _error(error) from None
        _sync_folder(uploads)
        return True

    def discard(self, folder: str, temp: str) -> None:
        if _plain(temp) and temp.endswith(".tmp"):
            _discard(os.path.join(self.uploads(folder), temp))

    def holds(self, folder: str, path: object, size: int, sha256: str) -> bool:
        """Whether ``path`` is a file in the folder with these bytes."""
        try:
            handle, _, info = self.open_read(folder, path)
        except FolderError:
            return False
        with handle:
            if info.st_size != size:
                return False
            digest = hashlib.sha256()
            while chunk := handle.read(1 << 20):
                digest.update(chunk)
            return digest.hexdigest() == sha256


def _plain(name: str) -> bool:
    return "/" not in name and os.sep not in name and _component_ok(name)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _copy(source: BinaryIO, fd: int) -> None:
    while chunk := source.read(1 << 20):
        _write_all(fd, chunk)


def _discard(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _candidates(name: str) -> Iterator[str]:
    yield name
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext = name, ""
    for n in range(2, MAX_NAME_TRIES + 1):
        yield f"{stem} ({n}).{ext}" if ext else f"{stem} ({n})"
