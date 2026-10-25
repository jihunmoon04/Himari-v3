"""The machine Environment (V3-P74): work on this server as the user.

HIMARI runs as its service user; commands and file operations are done by
``himari-runner``, which runs as the user (``coder``), through
:mod:`himari.runner.api` (§4, §5). Paths are the user's real paths.
Reading reaches what the user can read except the runner's hidden list;
writing reaches only the Conversation's Workspaces (§3).

A Conversation's own folder, ``<HIMARI home>/conversations/<conversation_id>``,
is its first Workspace — the T0 Binding's ``cwd_relative`` under the
runner's HIMARI home. The user attaches more (§10): each is a Binding of a
registered folder, whose real path Data keeps (``WorkspaceRegistry``). A
call may write in all of them; its working directory is the activation's
default Binding (§10.4).

Writes and commands take path locks (§9.1) on all of the Conversation's
folders at once: overlapping paths wait for each other, other
Conversations' folders do not.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import posixpath
import tempfile
from collections.abc import AsyncIterator, Awaitable, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, BinaryIO, TypeVar

from himari.common import EnvironmentKind, PrincipalId, TrustedScope, WorkspaceId
from himari.data.api import TEMP_PREFIX, BindingAuthority, WorkspaceRegistry
from himari.documents.api import DocumentReading
from himari.environment.api import BindingObservation, ConnectionStatus, ObservedWorkspace
from himari.environment.inside import InsideFiles, WorkspaceBusy
from himari.environment.machine.client import RunnerClient
from himari.environment.machine.locks import PathLocks
from himari.runner import api as runner
from himari.tool.api import ToolHandler

MACHINE = EnvironmentKind("machine")
T = TypeVar("T")
_PREFIX = "mws_"
OBSERVE_SECONDS = 2.0
OWN_LABEL = "this conversation"
READ_SCOPE = ("everything the user can read on this server, except hidden locations (credentials, HIMARI's own "
              "data); the user's home is {home}")
NETWORK = "HTTPS only, through a proxy, to these hosts (others are refused with 403): {hosts}"
NO_NETWORK = "none: commands cannot reach the network"
PUSH = "to the 'origin' remote of a repository in the workspaces, if it is one of: {repos} (https remotes only)"
NO_PUSH = "not set up on this server"
# §14: what the model is told of a cloud-synced folder, apart from the user's note.
CLOUD_FILESYSTEMS = frozenset({"fuse.rclone"})
DRIVE_NOTE = ("Cloud-synced folder: files have no execute bit, changes from other devices can take up to 15 minutes "
              "to appear, keep git repositories and build output on local disk.")


@dataclass(frozen=True)
class Places:
    """A Conversation's folders as real paths (§3.2): its working directory,
    where it may write (its own folder first), and each with its label."""

    cwd: str
    roots: tuple[str, ...]
    shown: tuple[ObservedWorkspace, ...]


def machine_workspace_for(principal: PrincipalId) -> WorkspaceId:
    """A principal's machine Workspace id (its HIMARI home): stable, and told
    apart from Inside ids (``ws_``) by its prefix."""
    return WorkspaceId(_PREFIX + hashlib.sha256(principal.encode()).hexdigest()[:24])


class MachineUnavailable(Exception):
    """The runner could not be reached (or is not enforced); nothing was done."""


class MachineEnvironment:
    def __init__(self, socket_path: str, *, files: InsideFiles | None = None,
                 documents: DocumentReading | None = None, registry: WorkspaceRegistry | None = None) -> None:
        from himari.environment.machine.tools import machine_tools

        self.client = RunnerClient(socket_path)
        self.files = files
        self.documents = documents
        # The attached Workspaces (§10); None: each Conversation has its own folder only.
        self.registry = registry
        self.locks = PathLocks()
        self._tools: tuple[ToolHandler, ...] = tuple(machine_tools(self))

    @property
    def kind(self) -> EnvironmentKind:
        return MACHINE

    def tools(self) -> Sequence[ToolHandler]:
        return self._tools

    @staticmethod
    def owns(workspace_id: WorkspaceId) -> bool:
        return workspace_id.startswith(_PREFIX)

    async def call(self, op: str, args: dict[str, Any], fds: Sequence[int] = ()) -> tuple[dict[str, Any], list[int]]:
        return await self.client.call(op, args, fds)

    async def hello(self) -> dict[str, Any]:
        result, _ = await self.call(runner.HELLO, {})
        return result

    async def folder(self, conversation_id: str, cwd_relative: str) -> str:
        """The Binding's folder as a real path; the Conversation's own folder
        is made if missing. MachineUnavailable without the runner."""
        try:
            if cwd_relative == f"conversations/{conversation_id}":
                result, _ = await self.call(runner.CONVERSATION_FOLDER, {"conversation": conversation_id})
                return str(result["path"])
            home = (await self.hello())["himari_home"]
        except (runner.RunnerUnavailable, runner.RunnerError) as error:
            raise MachineUnavailable(str(error)) from None
        return posixpath.normpath(posixpath.join(str(home), cwd_relative))

    async def check_workspace(self, path: str, others: Sequence[str]) -> dict[str, Any]:
        """The runner's check of a folder the user registers (§10.1):
        ``path`` (real), ``filesystem``, ``same_as``. RunnerError: refused;
        MachineUnavailable without the runner."""
        try:
            result, _ = await self.call(runner.CHECK_WORKSPACE, {"path": path, "others": list(others)})
        except runner.RunnerUnavailable as error:
            raise MachineUnavailable(str(error)) from None
        return result

    async def places(self, scope: TrustedScope) -> Places:
        """The Conversation's folders: its own (its T0 Binding's
        ``cwd_relative`` under the HIMARI home) and the Workspaces attached
        to it. A registered Workspace's location comes from Data only; the
        runner checks it again on every call (§5.3). Not from the Tool's
        Binding: that is the working one, which may be an attached folder.
        MachineUnavailable without the runner; Data's refusal (DataError)
        is raised, never taken for the own folder alone."""
        found = None
        if self.registry is not None:
            found = await self.registry.read_conversation_workspaces(scope, MACHINE)
        own_binding = None if found is None else next((a.binding for a in found.attached if a.workspace is None), None)
        own_cwd = (own_binding.cwd_relative if own_binding is not None and own_binding.cwd_relative
                   else f"conversations/{scope.conversation_id}")
        own = await self.folder(scope.conversation_id, own_cwd)
        cwd, roots, shown = own, [own], [ObservedWorkspace(own, OWN_LABEL)]
        attached, working = ((), None) if found is None else (found.attached, found.working)
        for item in attached:
            if item.workspace is None:  # the own folder: first, as above
                continue
            path = item.workspace.location
            if path not in roots:
                roots.append(path)
                notice = DRIVE_NOTE if item.workspace.filesystem in CLOUD_FILESYSTEMS else None
                shown.append(ObservedWorkspace(path, item.workspace.label, item.workspace.note, notice))
            if item.binding.binding_id == working:
                cwd = path
        return Places(cwd, tuple(roots), tuple(shown))

    async def observe(self, scope: TrustedScope, bindings: Sequence[BindingAuthority]) -> Sequence[BindingObservation]:
        """Every Binding is told online or offline; the working one (the
        activation's default, which the Current State shows) carries the
        Conversation's folders (§10.5)."""
        now = datetime.now(UTC)

        async def look() -> tuple[Places, dict[str, Any]]:
            places = await self.places(scope)
            try:
                return places, await self.hello()  # asked each time: the runner's network may have changed
            except (runner.RunnerUnavailable, runner.RunnerError) as error:
                raise MachineUnavailable(str(error)) from None

        try:
            places, about = await _within(look(), OBSERVE_SECONDS)
        except (MachineUnavailable, TimeoutError):
            return [BindingObservation(b.binding_id, ConnectionStatus.OFFLINE, now) for b in bindings]
        readable = READ_SCOPE.format(home=about["home"])
        hosts = about.get("network")
        network = NETWORK.format(hosts=", ".join(map(str, hosts))) if isinstance(hosts, list) and hosts else NO_NETWORK
        repos = about.get("push")
        push = PUSH.format(repos=", ".join(map(str, repos))) if isinstance(repos, list) and repos else NO_PUSH
        return [BindingObservation(b.binding_id, ConnectionStatus.ONLINE, now, working_directory=places.cwd,
                                   writable=places.roots, workspaces=places.shown, readable=readable,
                                   network=network, push=push)
                for b in bindings]

    # --- uploads (V3-P66 §5.2), the same steps as Inside -------------------------------------

    @asynccontextmanager
    async def locked(self, workspace_id: WorkspaceId, timeout: float, *, conversation_id: str,
                     cwd_relative: str) -> AsyncIterator[None]:
        folder = await self.folder(conversation_id, cwd_relative)
        try:
            async with self.locks.hold((folder,), timeout):
                yield
        except TimeoutError:
            raise WorkspaceBusy(workspace_id) from None

    async def _uploads(self, conversation_id: str, cwd_relative: str) -> tuple[str, list[str]]:
        folder = await self.folder(conversation_id, cwd_relative)
        return posixpath.join(folder, "uploads"), [folder]

    async def _upload_call(self, op: str, args: dict[str, Any], fds: Sequence[int] = ()) -> dict[str, Any]:
        try:
            result, received = await self.call(op, {"prefix": TEMP_PREFIX, **args}, fds)
        except runner.NotSent as error:
            raise MachineUnavailable(str(error)) from None
        for fd in received:
            os.close(fd)
        return result

    async def copy_in(self, conversation_id: str, cwd_relative: str, source: BinaryIO, *, clear: bool = False) -> str:
        folder, roots = await self._uploads(conversation_id, cwd_relative)
        with source, _as_fd(source) as fd:
            result = await self._upload_call(runner.COPY_IN, {"folder": folder, "write_roots": roots,
                                                              "clear": clear}, [fd])
        return str(result["temp"])

    async def free_path(self, conversation_id: str, cwd_relative: str, name: str, preferred: str | None) -> str:
        folder, roots = await self._uploads(conversation_id, cwd_relative)
        result = await self._upload_call(runner.FREE_PATH, {"folder": folder, "write_roots": roots, "name": name,
                                                            "preferred": preferred})
        return str(result["path"])

    async def commit_copy(self, conversation_id: str, cwd_relative: str, temp: str, path: str) -> bool:
        folder, roots = await self._uploads(conversation_id, cwd_relative)
        result = await self._upload_call(runner.COMMIT_COPY, {"folder": folder, "write_roots": roots, "temp": temp,
                                                              "path": path})
        return bool(result["placed"])

    async def discard_copy(self, conversation_id: str, cwd_relative: str, temp: str) -> None:
        folder, roots = await self._uploads(conversation_id, cwd_relative)
        await self._upload_call(runner.DISCARD_COPY, {"folder": folder, "write_roots": roots, "temp": temp})

    async def holds(self, conversation_id: str, cwd_relative: str, path: str, size: int, sha256: str) -> bool:
        try:
            result = await self._upload_call(runner.HOLDS, {"path": path, "size": size, "sha256": sha256})
        except runner.RunnerError:
            return False
        return bool(result["holds"])

    async def remove_conversation_folder(self, workspace_id: WorkspaceId, scope: TrustedScope,
                                         cwd_relative: str) -> bool:
        """V3-P65 §5: only the Conversation's own folder, under its lock."""
        if cwd_relative != f"conversations/{scope.conversation_id}":
            return False
        folder = await self.folder(scope.conversation_id, cwd_relative)
        async with self.locks.hold((folder,)):
            try:
                result, _ = await self.call(runner.REMOVE_CONVERSATION_FOLDER,
                                            {"conversation": scope.conversation_id})
            except (runner.RunnerUnavailable, runner.RunnerError) as error:
                raise MachineUnavailable(str(error)) from None
        return bool(result["removed"])


async def _within(awaitable: Awaitable[T], seconds: float) -> T:
    async with asyncio.timeout(seconds):
        return await awaitable


@contextmanager
def _as_fd(source: BinaryIO) -> Iterator[int]:
    """A descriptor for ``source``: its own, or a temporary copy of a stream
    that has none (the in-memory store)."""
    try:
        fd: int | None = source.fileno()
    except (OSError, io.UnsupportedOperation, AttributeError):
        fd = None
    if fd is not None:
        yield fd
        return
    with tempfile.TemporaryFile() as copy:
        while chunk := source.read(1 << 20):
            copy.write(chunk)
        copy.flush()
        copy.seek(0)
        yield copy.fileno()
