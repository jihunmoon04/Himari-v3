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
a Workspace file, and ``place_uploads`` copies a message's uploads into the
Conversation's ``uploads/`` folder under the write lock (§5.2).
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
from collections.abc import AsyncIterator, Callable, Sequence
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


@dataclass(frozen=True)
class Upload:
    """One upload to place: its shown name, how to read its bytes, and where
    it was placed before (a retry keeps that file if it is still there)."""

    name: str
    open: Callable[[], BinaryIO]
    placed_at: str | None = None


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

    async def place_uploads(self, workspace_id: WorkspaceId, cwd_relative: str,
                            uploads: Sequence[Upload]) -> list[str]:
        """Copy uploads into ``<cwd>/uploads`` (never replacing a file);
        the ``/workspace/…`` path of each. The caller holds ``locked``."""
        host_root = self.host_root(workspace_id)
        return await run_blocking(self._place, host_root, cwd_relative, list(uploads))

    @staticmethod
    def _place(host_root: str, cwd: str, uploads: list[Upload]) -> list[str]:
        folder = fs.to_relative(cwd, UPLOADS)
        placed: list[str] = []
        with fs.open_root(host_root) as root_fd:
            try:
                fs.ensure_directory(root_fd, folder)
            except fs.FsError as error:
                if error.category in (fs.FsCategory.LINK, fs.FsCategory.NOT_DIRECTORY, fs.FsCategory.EXISTS):
                    raise UploadsFolderUnusable(fs.display(folder)) from None
                raise
            for upload in uploads:
                if upload.placed_at is not None:
                    try:
                        fs.identity(root_fd, fs.to_relative(cwd, upload.placed_at))
                        placed.append(upload.placed_at)  # placed by an earlier try: kept as it is
                        continue
                    except fs.FsError:
                        pass  # gone since: place it again
                with upload.open() as source:
                    placed.append(fs.display(fs.place_file(root_fd, folder, upload.name, source, TEMP_PREFIX)))
        return placed

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
