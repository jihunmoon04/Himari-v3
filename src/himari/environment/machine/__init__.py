"""The machine Environment (V3-P74, V3-P109): each Conversation's own folder.

Every Conversation that has the Environment has one folder,
``<HIMARI data root>/conversations/<conversation_id>``, its working
directory. The
file Tools read and write only there (V3-P109 Q1); the operations run in
this process (:mod:`.folders`), in a worker thread that a cancelled caller
waits for, so its lock outlives the work (``run_blocking``).

Writes take a path lock (V3-P74 §9.1) on the Conversation's folder: other
Conversations' folders do not wait for it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import BinaryIO

from himari.common import EnvironmentKind, TrustedScope
from himari.data.api import TEMP_PREFIX
from himari.documents.api import DocumentReading
from himari.environment.api import ConnectionStatus, EnvironmentObservation
from himari.environment.machine.blocking import run_blocking
from himari.environment.machine.errors import FolderError
from himari.environment.machine.folders import ConversationFolders
from himari.environment.machine.locks import PathLocks
from himari.environment.machine.results import ToolFiles
from himari.tool.api import ToolHandler

MACHINE = EnvironmentKind("machine")
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


class MachineUnavailable(Exception):
    """The Conversation's folder could not be made or used; nothing was done."""


class FolderBusy(Exception):
    """The Conversation's folder lock was not free in time (V3-P66 §5.1, review N4)."""


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

    async def folder(self, conversation_id: str) -> str:
        """The Conversation's own folder, made if missing. MachineUnavailable
        when it cannot be."""
        try:
            return await run_blocking(self.folders.folder, conversation_id)
        except FolderError as error:
            raise MachineUnavailable(f"this conversation's folder cannot be used ({error})") from None

    async def places(self, scope: TrustedScope) -> Places:
        return Places(await self.folder(scope.conversation_id))

    async def observe(self, scope: TrustedScope) -> EnvironmentObservation:
        """Online with the Conversation's folder (V3-P74 §10.5), offline when it cannot be used."""
        now = datetime.now(UTC)
        try:
            places = await self.places(scope)
        except MachineUnavailable:
            return EnvironmentObservation(ConnectionStatus.OFFLINE, now)
        return EnvironmentObservation(ConnectionStatus.ONLINE, now, working_directory=places.cwd,
                                      writable=places.writable, readable=READ_SCOPE)

    # --- uploads (V3-P66 §5.2) ---------------------------------------------------------------

    @asynccontextmanager
    async def locked(self, conversation_id: str, timeout: float) -> AsyncIterator[None]:
        folder = await self.folder(conversation_id)
        try:
            async with self.locks.hold((folder,), timeout):
                yield
        except TimeoutError:
            raise FolderBusy(conversation_id) from None

    async def _upload(self, conversation_id: str, call: object, *args: object) -> object:
        folder = await self.folder(conversation_id)
        try:
            return await run_blocking(call, folder, *args)  # type: ignore[arg-type]
        except FolderError as error:
            raise MachineUnavailable(str(error)) from None

    async def copy_in(self, conversation_id: str, source: BinaryIO, *, clear: bool = False) -> str:
        with source:
            folders = self.folders
            temp = await self._upload(conversation_id,
                                      lambda folder: folders.copy_in(folder, source, TEMP_PREFIX, clear=clear))
        return str(temp)

    async def free_path(self, conversation_id: str, name: str, preferred: str | None) -> str:
        return str(await self._upload(conversation_id, self.folders.free_path, name, preferred))

    async def commit_copy(self, conversation_id: str, temp: str, path: str) -> bool:
        return bool(await self._upload(conversation_id, self.folders.commit, temp, path))

    async def discard_copy(self, conversation_id: str, temp: str) -> None:
        await self._upload(conversation_id, self.folders.discard, temp)

    async def holds(self, conversation_id: str, path: str, size: int, sha256: str) -> bool:
        try:
            return bool(await self._upload(conversation_id, self.folders.holds, path, size, sha256))
        except MachineUnavailable:
            return False

    async def remove_conversation_folder(self, scope: TrustedScope) -> bool:
        """V3-P65 §5: only the Conversation's own folder, under its lock."""
        folder = self.folders.path_of(scope.conversation_id)
        async with self.locks.hold((folder,)):
            try:
                return await run_blocking(self.folders.remove, scope.conversation_id)
            except FolderError as error:
                raise MachineUnavailable(str(error)) from None
