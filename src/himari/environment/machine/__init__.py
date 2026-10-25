"""The machine Environment (V3-P74): work on this server as the user.

HIMARI runs as its service user; commands and file operations are done by
``himari-runner``, which runs as the user (``coder``), through
:mod:`himari.runner.api` (§4, §5). Paths are the user's real paths.
Reading reaches what the user can read except the runner's hidden list;
writing reaches only the Conversation's Workspaces (§3).

Part 1 (§19): each Conversation's one Workspace is its own folder,
``<HIMARI home>/conversations/<conversation_id>`` — the Binding's
``cwd_relative`` under the runner's HIMARI home. Attaching more folders is
part 2.

Writes and commands take path locks (§9.1): overlapping paths wait for
each other, other Conversations' folders do not.
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
from datetime import UTC, datetime
from typing import Any, BinaryIO, TypeVar

from himari.common import EnvironmentKind, PrincipalId, TrustedScope, WorkspaceId
from himari.data.api import TEMP_PREFIX, BindingAuthority
from himari.documents.api import DocumentReading
from himari.environment.api import BindingObservation, ConnectionStatus
from himari.environment.inside import InsideFiles, WorkspaceBusy
from himari.environment.machine.client import RunnerClient
from himari.environment.machine.locks import PathLocks
from himari.runner import api as runner
from himari.tool.api import ToolHandler

MACHINE = EnvironmentKind("machine")
T = TypeVar("T")
_PREFIX = "mws_"
OBSERVE_SECONDS = 2.0


def machine_workspace_for(principal: PrincipalId) -> WorkspaceId:
    """A principal's machine Workspace id (its HIMARI home): stable, and told
    apart from Inside ids (``ws_``) by its prefix."""
    return WorkspaceId(_PREFIX + hashlib.sha256(principal.encode()).hexdigest()[:24])


class MachineUnavailable(Exception):
    """The runner could not be reached (or is not enforced); nothing was done."""


class MachineEnvironment:
    def __init__(self, socket_path: str, *, files: InsideFiles | None = None,
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

    async def observe(self, scope: TrustedScope, bindings: Sequence[BindingAuthority]) -> Sequence[BindingObservation]:
        now = datetime.now(UTC)
        observations = []
        for binding in bindings:
            try:
                folder = await _within(self.folder(scope.conversation_id, binding.cwd_relative or ""), OBSERVE_SECONDS)
            except (MachineUnavailable, TimeoutError):
                observations.append(BindingObservation(binding.binding_id, ConnectionStatus.OFFLINE, now))
                continue
            observations.append(BindingObservation(binding.binding_id, ConnectionStatus.ONLINE, now,
                                                   working_directory=folder, writable=(folder,)))
        return observations

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
