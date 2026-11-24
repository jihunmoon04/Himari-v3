"""The machine Environment (V3-P74, V3-P109): each Conversation's own folder.

Every Conversation that has the Environment has one folder,
``<HIMARI data root>/conversations/<conversation_id>`` — the T0 Binding's
``cwd_relative`` under the folders' root, and the working directory. The
file Tools read and write only there (V3-P109 Q1); the operations run in
this process (:mod:`.folders`), in a worker thread that a cancelled caller
waits for, so its lock outlives the work (``run_blocking``).

Writes take a path lock (V3-P74 §9.1) on the Conversation's folder: other
Conversations' folders do not wait for it.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import BinaryIO

from himari.common import EnvironmentKind, PrincipalId, TrustedScope, WorkspaceId
from himari.data.api import TEMP_PREFIX, BindingAuthority
from himari.documents.api import DocumentReading
from himari.environment.api import BindingObservation, ConnectionStatus
from himari.environment.machine.blocking import run_blocking
from himari.environment.machine.errors import FolderError
from himari.environment.machine.folders import ConversationFolders
from himari.environment.machine.locks import PathLocks
from himari.environment.machine.results import ToolFiles
from himari.tool.api import ToolHandler

MACHINE = EnvironmentKind("machine")
_PREFIX = "mws_"
READ_SCOPE = "only this conversation's folder (the working directory)"


@dataclass(frozen=True)
class Places:
    """A Conversation's own folder (V3-P74 §3.2): its working directory and
    the one folder it may read and write."""

    cwd: str

    @property
    def roots(self) -> tuple[str, ...]:
        return (self.cwd,)

    @property
    def writable(self) -> tuple[str, ...]:
        return (self.cwd,)


def machine_workspace_for(principal: PrincipalId) -> WorkspaceId:
    """A principal's machine Workspace id: stable, and told apart from other
    ids (the old Inside ``ws_``) by its prefix."""
    return WorkspaceId(_PREFIX + hashlib.sha256(principal.encode()).hexdigest()[:24])


class MachineUnavailable(Exception):
    """The Conversation's folder could not be made or used; nothing was done."""


class WorkspaceBusy(Exception):
    """The Conversation's folder lock was not free in time (V3-P66 §5.1, review N4)."""


def _own(conversation_id: str, cwd_relative: str) -> None:
    if cwd_relative != f"conversations/{conversation_id}":
        raise MachineUnavailable("not this conversation's folder")


class MachineEnvironment:
    def __init__(self, root: str, *, files: ToolFiles | None = None,
                 documents: DocumentReading | None = None) -> None:
        from himari.environment.machine.tools import machine_tools

        self.folders = ConversationFolders(root)
        self.files = files
        self.documents = documents
        self.locks = PathLocks()
        self._tools: tuple[ToolHandler, ...] = tuple(machine_tools(self))

    @property
    def root(self) -> str:
        return self.folders.root

    @property
    def kind(self) -> EnvironmentKind:
        return MACHINE

    def tools(self) -> Sequence[ToolHandler]:
        return self._tools

    @staticmethod
    def owns(workspace_id: WorkspaceId) -> bool:
        return workspace_id.startswith(_PREFIX)

    async def folder(self, conversation_id: str, cwd_relative: str) -> str:
        """The Conversation's own folder, made if missing. MachineUnavailable
        when it cannot be (or the Binding names another folder)."""
        _own(conversation_id, cwd_relative)
        try:
            return await run_blocking(self.folders.folder, conversation_id)
        except FolderError as error:
            raise MachineUnavailable(f"this conversation's folder cannot be used ({error})") from None

    async def places(self, scope: TrustedScope) -> Places:
        return Places(await self.folder(scope.conversation_id, f"conversations/{scope.conversation_id}"))

    async def observe(self, scope: TrustedScope, bindings: Sequence[BindingAuthority]) -> Sequence[BindingObservation]:
        """Every Binding is told online, with the Conversation's folder (V3-P74 §10.5)."""
        now = datetime.now(UTC)
        try:
            places = await self.places(scope)
        except MachineUnavailable:
            return [BindingObservation(b.binding_id, ConnectionStatus.OFFLINE, now) for b in bindings]
        return [BindingObservation(b.binding_id, ConnectionStatus.ONLINE, now, working_directory=places.cwd,
                                   writable=places.writable, readable=READ_SCOPE)
                for b in bindings]

    # --- uploads (V3-P66 §5.2) ---------------------------------------------------------------

    @asynccontextmanager
    async def locked(self, workspace_id: WorkspaceId, timeout: float, *, conversation_id: str,
                     cwd_relative: str) -> AsyncIterator[None]:
        folder = await self.folder(conversation_id, cwd_relative)
        try:
            async with self.locks.hold((folder,), timeout):
                yield
        except TimeoutError:
            raise WorkspaceBusy(workspace_id) from None

    async def _upload(self, conversation_id: str, cwd_relative: str, call: object, *args: object) -> object:
        folder = await self.folder(conversation_id, cwd_relative)
        try:
            return await run_blocking(call, folder, *args)  # type: ignore[arg-type]
        except FolderError as error:
            raise MachineUnavailable(str(error)) from None

    async def copy_in(self, conversation_id: str, cwd_relative: str, source: BinaryIO, *, clear: bool = False) -> str:
        with source:
            folders = self.folders
            temp = await self._upload(conversation_id, cwd_relative,
                                      lambda folder: folders.copy_in(folder, source, TEMP_PREFIX, clear=clear))
        return str(temp)

    async def free_path(self, conversation_id: str, cwd_relative: str, name: str, preferred: str | None) -> str:
        return str(await self._upload(conversation_id, cwd_relative, self.folders.free_path, name, preferred))

    async def commit_copy(self, conversation_id: str, cwd_relative: str, temp: str, path: str) -> bool:
        return bool(await self._upload(conversation_id, cwd_relative, self.folders.commit, temp, path))

    async def discard_copy(self, conversation_id: str, cwd_relative: str, temp: str) -> None:
        await self._upload(conversation_id, cwd_relative, self.folders.discard, temp)

    async def holds(self, conversation_id: str, cwd_relative: str, path: str, size: int, sha256: str) -> bool:
        try:
            return bool(await self._upload(conversation_id, cwd_relative, self.folders.holds, path, size, sha256))
        except MachineUnavailable:
            return False

    async def remove_conversation_folder(self, workspace_id: WorkspaceId, scope: TrustedScope,
                                         cwd_relative: str) -> bool:
        """V3-P65 §5: only the Conversation's own folder, under its lock."""
        if cwd_relative != f"conversations/{scope.conversation_id}":
            return False
        folder = self.folders.path_of(scope.conversation_id)
        async with self.locks.hold((folder,)):
            try:
                return await run_blocking(self.folders.remove, scope.conversation_id)
            except FolderError as error:
                raise MachineUnavailable(str(error)) from None
