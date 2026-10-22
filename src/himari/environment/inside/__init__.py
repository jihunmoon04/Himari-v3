"""Inside Environment: the HIMARI-managed default Workspace (V3-030, V3-033, V3-035).

Host layout (never shown to the model)::

    <workspaces_root>/<workspace_id>/          one per principal
        conversations/<conversation_id>/       each Conversation's default cwd

Sandbox/model view: ``/workspace/...``. Conversation folders are an
organizing convention, not a security boundary: a principal's Conversations
may read and write each other's folders. The boundary is the principal's
Workspace root, enforced on every access by :mod:`.fs`.

This package is an EnvironmentPlugin (ENV-06): it provides its own Tools
(V3-041). ``inside/run_command`` is provided only when a Workspace sandbox is
configured (``himari.sandbox``, built by Bootstrap), and refuses to run unless that backend is ENFORCED (V3-032).

Writes and commands on the same Workspace are serialized in-process
(V3-P10): a command's file effects are unknown, so it conflicts with every
write to that principal root, across Conversations too. Reads do not wait.

Files (V3-P66): with ``files`` given, ``inside/present_file`` gives the user
a Workspace file. A message's uploads are copied into the Conversation's
``uploads/`` folder under the write lock (§5.2), in steps — copy, choose a
name, (the caller records it), rename — so a crash never leaves a placed
file its record does not name.
"""

from __future__ import annotations

import asyncio
import os
import posixpath
import re
import secrets
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from himari.common import EnvironmentKind, TrustedScope, WorkspaceId
from himari.data.api import DEFAULT_FILE_MAX_BYTES, TEMP_PREFIX, BindingAuthority, BlobStore, FileCatalog
from himari.environment.api import BindingObservation, ConnectionStatus
from himari.environment.inside import fs
from himari.environment.inside.blocking import run_blocking
from himari.environment.inside.tools import RunCommand, file_tools
from himari.sandbox.api import WorkspaceSandbox
from himari.tool.api import ToolHandler

INSIDE = EnvironmentKind("inside")
_WORKSPACE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
UPLOADS = "uploads"


@dataclass(frozen=True)
class InsideFiles:
    """What ``inside/present_file`` needs from Data (V3-P66 §7.1)."""

    blobs: BlobStore
    catalog: FileCatalog
    max_bytes: int = DEFAULT_FILE_MAX_BYTES


class WorkspaceBusy(Exception):
    """The Workspace write lock was not free in time (§5.1, review N4)."""


class UploadsFolderUnusable(Exception):
    """The Conversation's ``uploads`` is not a folder (a link, a file) (§5.2)."""


class InsideEnvironment:
    def __init__(self, workspaces_root: str | Path, *, sandbox: WorkspaceSandbox | None = None,
                 files: InsideFiles | None = None) -> None:
        root = Path(workspaces_root)
        if not root.is_absolute():
            raise ValueError("workspaces_root must be an absolute, trusted path")
        self._root = root
        self.sandbox = sandbox
        self.files = files
        self._write_locks: dict[str, asyncio.Lock] = {}
        tools: list[ToolHandler] = list(file_tools(self))
        if sandbox is not None:
            tools.append(RunCommand(self))
        self._tools = tuple(tools)

    async def start_sandbox(self, *, protected_paths: tuple[str, ...] = ()) -> None:
        """Start the configured sandbox; the Workspaces root is always among
        the paths the self-test proves invisible (only one principal root is
        ever bound, as /workspace)."""
        if self.sandbox is not None:
            await self.sandbox.start(protected_paths=(str(self._root), *protected_paths))

    def write_lock(self, workspace_id: WorkspaceId) -> asyncio.Lock:
        """One lock per principal Workspace for writes and commands."""
        return self._write_locks.setdefault(workspace_id, asyncio.Lock())

    @property
    def kind(self) -> EnvironmentKind:
        return INSIDE

    def tools(self) -> Sequence[ToolHandler]:
        return self._tools

    def host_root(self, workspace_id: WorkspaceId) -> str:
        """Trusted mapping workspace_id → host directory. The id comes from
        Data (an authorized Binding), never from Tool arguments."""
        if not _WORKSPACE_ID.match(workspace_id):
            raise ValueError("invalid workspace id")
        return str(self._root / workspace_id)

    def provision(self, workspace_id: WorkspaceId) -> None:
        """Administrative setup of a principal's Workspace (not model-reachable)."""
        root = Path(self.host_root(workspace_id))
        (root / "conversations").mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(root, 0o700)

    async def remove_conversation_folder(self, workspace_id: WorkspaceId, scope: TrustedScope,
                                         cwd_relative: str) -> bool:
        """V3-P65 §5: remove the Conversation's own folder, under the
        Workspace's write lock (no Inside Tool or command runs in that root
        meanwhile). Only its default folder ``conversations/<id>``; any other
        cwd is not the Conversation's and is left (False). Idempotent."""
        if cwd_relative != f"conversations/{scope.conversation_id}":
            return False
        host_root = self.host_root(workspace_id)
        async with self.write_lock(workspace_id):
            return await run_blocking(self._remove_folder, host_root, cwd_relative)

    @staticmethod
    def _remove_folder(host_root: str, relative: str) -> bool:
        try:
            with fs.open_root(host_root) as root_fd:
                return fs.remove_tree(root_fd, relative)
        except fs.FsError as error:
            if error.category is fs.FsCategory.NOT_FOUND:  # the principal's root itself is gone
                return False
            raise

    # --- uploads (V3-P66 §5.2) --------------------------------------------------------

    @asynccontextmanager
    async def locked(self, workspace_id: WorkspaceId, timeout: float) -> AsyncIterator[None]:
        """The Workspace write lock, waited for at most ``timeout`` seconds."""
        lock = self.write_lock(workspace_id)
        try:
            await asyncio.wait_for(lock.acquire(), timeout)
        except TimeoutError:
            raise WorkspaceBusy(workspace_id) from None
        try:
            yield
        finally:
            lock.release()

    # Placing one upload, in steps, so the caller can record where it goes
    # before it gets there (V3-P66 owner review B1). The caller holds ``locked``.

    async def copy_in(self, workspace_id: WorkspaceId, cwd_relative: str, source: BinaryIO, *,
                      clear: bool = False) -> str:
        """A durable temporary copy of ``source`` in ``<cwd>/uploads`` (made
        if missing, durably); its name. ``clear``: leftovers go first."""
        return await run_blocking(self._copy_in, self.host_root(workspace_id), cwd_relative, source, clear)

    @staticmethod
    def _copy_in(host_root: str, cwd: str, source: BinaryIO, clear: bool) -> str:
        folder = fs.to_relative(cwd, UPLOADS)
        with fs.open_root(host_root) as root_fd:
            try:
                fs.ensure_directory(root_fd, folder, durable=True)
            except fs.FsError as error:
                if error.category in (fs.FsCategory.LINK, fs.FsCategory.NOT_DIRECTORY, fs.FsCategory.EXISTS):
                    raise UploadsFolderUnusable(fs.display(folder)) from None
                raise
            if clear:
                fs.clear_temps(root_fd, folder, TEMP_PREFIX)
            with source:
                return fs.write_temp(root_fd, folder, source, TEMP_PREFIX)

    async def free_path(self, workspace_id: WorkspaceId, cwd_relative: str, name: str,
                        preferred: str | None) -> str:
        """The ``/workspace/…`` path to give an upload: ``preferred`` if it is
        in ``uploads`` and free, else the first free of ``name``, ``name (2)``…"""
        return await run_blocking(self._free_path, self.host_root(workspace_id), cwd_relative, name, preferred)

    @staticmethod
    def _free_path(host_root: str, cwd: str, name: str, preferred: str | None) -> str:
        folder = fs.to_relative(cwd, UPLOADS)
        wanted = None
        if preferred is not None:
            try:
                relative = fs.to_relative(cwd, preferred)
                if posixpath.dirname(relative) == folder:
                    wanted = posixpath.basename(relative)
            except fs.FsError:
                pass
        with fs.open_root(host_root) as root_fd:
            return fs.display(posixpath.join(folder, fs.free_name(root_fd, folder, name, wanted)))

    async def commit_copy(self, workspace_id: WorkspaceId, cwd_relative: str, temp: str, path: str) -> bool:
        """Give the copy ``path`` (never replacing); False if it is taken."""
        return await run_blocking(self._commit_copy, self.host_root(workspace_id), cwd_relative, temp, path)

    @staticmethod
    def _commit_copy(host_root: str, cwd: str, temp: str, path: str) -> bool:
        folder = fs.to_relative(cwd, UPLOADS)
        relative = fs.to_relative(cwd, path)
        if posixpath.dirname(relative) != folder:
            raise fs.FsError(fs.FsCategory.INVALID)
        with fs.open_root(host_root) as root_fd:
            return fs.commit_temp(root_fd, folder, temp, posixpath.basename(relative))

    async def discard_copy(self, workspace_id: WorkspaceId, cwd_relative: str, temp: str) -> None:
        """Remove the copy if it was not given a name (idempotent)."""
        await run_blocking(self._discard_copy, self.host_root(workspace_id), cwd_relative, temp)

    @staticmethod
    def _discard_copy(host_root: str, cwd: str, temp: str) -> None:
        with fs.open_root(host_root) as root_fd:
            fs.remove_temp(root_fd, fs.to_relative(cwd, UPLOADS), temp)

    async def holds(self, workspace_id: WorkspaceId, cwd_relative: str, path: str, size: int, sha256: str) -> bool:
        """Whether ``path`` is a regular file with exactly these bytes: an
        earlier try placed it there (owner review B1)."""
        return await run_blocking(self._holds, self.host_root(workspace_id), cwd_relative, path, size, sha256)

    @staticmethod
    def _holds(host_root: str, cwd: str, path: str, size: int, sha256: str) -> bool:
        try:
            relative = fs.to_relative(cwd, path)
            with fs.open_root(host_root) as root_fd:
                return fs.same_content(root_fd, relative, size, sha256)
        except fs.FsError:
            return False

    def placement_works(self) -> bool:
        """Whether this filesystem has the atomic no-replace rename placing
        needs (review N2); checked once at startup."""
        os.makedirs(self._root, mode=0o700, exist_ok=True)
        try:
            with fs.open_root(str(self._root)) as root_fd:
                first, second = f"{TEMP_PREFIX}check-{secrets.token_hex(4)}", f"{TEMP_PREFIX}check-{secrets.token_hex(4)}"
                os.close(os.open(first, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=root_fd))
                try:
                    fs.rename_noreplace(root_fd, first, second)
                    os.unlink(second, dir_fd=root_fd)
                finally:
                    try:
                        os.unlink(first, dir_fd=root_fd)
                    except OSError:
                        pass
            return True
        except (fs.FsError, OSError):
            return False

    async def observe(
        self, scope: TrustedScope, bindings: Sequence[BindingAuthority]
    ) -> Sequence[BindingObservation]:
        now = datetime.now(UTC)
        observations = []
        for binding in bindings:
            try:
                status = ConnectionStatus.ONLINE if Path(self.host_root(binding.workspace_id)).is_dir() else ConnectionStatus.OFFLINE
            except ValueError:
                status = ConnectionStatus.OFFLINE
            observations.append(BindingObservation(binding.binding_id, status, now))
        return observations
