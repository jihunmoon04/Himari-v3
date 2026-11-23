"""The machine Environment (V3-P74): files on this server as the user.

HIMARI runs as its service user; file operations are done by
``himari-runner``, which runs as the user (``coder``), through
:mod:`himari.runner.api` (§4, §5). Paths are the user's real paths.
Reading reaches what the user can read except the runner's hidden list;
writing reaches only the Conversation's own folder,
``<HIMARI home>/conversations/<conversation_id>`` — the T0 Binding's
``cwd_relative`` under the runner's HIMARI home, and the working directory.

Writes take a path lock (§9.1) on the Conversation's folder: other
Conversations' folders do not wait for it.
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
from himari.data.api import TEMP_PREFIX, BindingAuthority
from himari.documents.api import DocumentReading
from himari.environment.api import BindingObservation, ConnectionStatus
from himari.environment.machine.client import RunnerClient
from himari.environment.machine.locks import PathLocks
from himari.environment.machine.results import ToolFiles
from himari.runner import api as runner
from himari.tool.api import ToolHandler

MACHINE = EnvironmentKind("machine")
T = TypeVar("T")
_PREFIX = "mws_"
OBSERVE_SECONDS = 2.0
READ_SCOPE = ("everything the user can read on this server, except hidden locations (credentials, HIMARI's own "
              "data); the user's home is {home}")


@dataclass(frozen=True)
class Places:
    """A Conversation's own folder as a real path (§3.2): its working
    directory and the one folder it may write."""

    cwd: str

    @property
    def roots(self) -> tuple[str, ...]:
        return (self.cwd,)

    @property
    def writable(self) -> tuple[str, ...]:
        return (self.cwd,)


def machine_workspace_for(principal: PrincipalId) -> WorkspaceId:
    """A principal's machine Workspace id (its HIMARI home): stable, and told
    apart from other ids (the old Inside ``ws_``) by its prefix."""
    return WorkspaceId(_PREFIX + hashlib.sha256(principal.encode()).hexdigest()[:24])


class MachineUnavailable(Exception):
    """The runner could not be reached; nothing was done."""


class WorkspaceBusy(Exception):
    """The Conversation's folder lock was not free in time (V3-P66 §5.1, review N4)."""


class MachineEnvironment:
    def __init__(self, socket_path: str, *, files: ToolFiles | None = None,
                 documents: DocumentReading | None = None) -> None:
        from himari.environment.machine.tools import machine_tools

        self.client = RunnerClient(socket_path)
        self.files = files
        self.documents = documents
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

    async def places(self, scope: TrustedScope) -> Places:
        """The Conversation's own folder, made if missing. MachineUnavailable without the runner."""
        return Places(await self.folder(scope.conversation_id, f"conversations/{scope.conversation_id}"))

    async def observe(self, scope: TrustedScope, bindings: Sequence[BindingAuthority]) -> Sequence[BindingObservation]:
        """Every Binding is told online or offline, with the Conversation's folder (§10.5)."""
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
        return [BindingObservation(b.binding_id, ConnectionStatus.ONLINE, now, working_directory=places.cwd,
                                   writable=places.writable, readable=readable)
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
