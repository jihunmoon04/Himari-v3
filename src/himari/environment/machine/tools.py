"""The machine Environment's Tools (V3-P74 §8, §9; V3-P109 §4.2): ``machine/<name>``.

The file Tools and documents in the Conversation's own folder, read and
written in this process (:mod:`.folders`); results are formatted by
:mod:`.results`. A refusal about the boundary (outside the folder,
permission) is DENIED; other problems are FAILED.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, BinaryIO

from himari.common import CallId, EnvironmentKind, JsonValue, ToolId
from himari.data.api import DEFAULT_FILE_MAX_BYTES, MAX_DOCUMENT_BYTES, DataError
from himari.documents.api import DocumentReading
from himari.environment.machine.blocking import run_blocking
from himari.environment.machine.documents import ReadDocument, document_definition
from himari.environment.machine.errors import DENIED, TOO_LARGE, FolderError
from himari.environment.machine.results import (
    MAX_LIST_ENTRIES,
    MAX_READ_BYTES,
    MAX_WRITE_BYTES,
    Entry,
    _schema,
    _size,
    _text,
    listing_outcome,
    present,
    present_changed,
    present_refusal,
    read_offset,
    read_outcome,
)
from himari.tool.api import (
    EffectProfile,
    ToolContext,
    ToolDefinition,
    ToolInvocation,
    ToolOutcome,
    ToolHandler,
    ToolStatus,
)

if TYPE_CHECKING:
    from himari.environment.machine import MachineEnvironment

log = logging.getLogger("himari.environment.machine")

_PATHS = ("Paths are inside this conversation's own folder (the working directory in the runtime state): "
          "absolute, or relative to it; nothing outside it can be read or written. ")


def _refused(shown: str, error: FolderError) -> ToolOutcome:
    status = ToolStatus.DENIED if error.category in DENIED else ToolStatus.FAILED
    return ToolOutcome(status, _text(f"{shown}: {error}", error=True), raw_result={"category": error.category})


def _unavailable(why: str) -> ToolOutcome:
    return ToolOutcome(ToolStatus.DENIED, _text(f"{why}; nothing was done", error=True),
                       raw_result={"folder": "unavailable"})


def _no_places(error: DataError) -> ToolOutcome:
    """The Conversation's folders could not be read: nothing runs (V3-P74 §10.5)."""
    return ToolOutcome(ToolStatus.DENIED, _text(f"this conversation's folders could not be read ({error.code.value}); "
                                                "nothing was done", error=True), raw_result={"data": error.code.value})


def _shown(raw: object) -> str:
    return raw if isinstance(raw, str) else "?"


def _read_at(handle: BinaryIO, offset: int, count: int) -> bytes:
    handle.seek(offset)
    return handle.read(count)


def _read_all(handle: BinaryIO, limit: int) -> bytes:
    handle.seek(0)
    return handle.read(limit)


class _MachineTool:
    name = ""
    description = ""
    effect = EffectProfile.READ
    target = "path"  # the argument a refusal names
    properties: dict[str, JsonValue] = {}
    required: list[str] = []

    def __init__(self, env: MachineEnvironment) -> None:
        self._env = env
        self.definition = ToolDefinition(
            tool_id=ToolId(f"machine/{self.name}"),
            description=self.description,
            input_schema=_schema(self.properties, self.required),
            effect_profile=self.effect,
            environment_kind=EnvironmentKind("machine"),
        )

    async def run(self, invocation: ToolInvocation, ctx: ToolContext) -> ToolOutcome:
        from himari.environment.machine import MachineUnavailable

        try:
            places = await self._env.places(ctx.scope)
        except MachineUnavailable as unavailable:
            return _unavailable(str(unavailable))
        except DataError as error:
            return _no_places(error)
        args = invocation.arguments
        try:
            if self.effect is EffectProfile.READ:
                return await self.act(args, places.cwd, ctx, invocation.call_id)
            async with self._env.locks.hold(places.roots):
                return await self.act(args, places.cwd, ctx, invocation.call_id)
        except FolderError as error:
            return _refused(_shown(args.get(self.target, ".")), error)

    async def act(self, args: Mapping[str, Any], folder: str, ctx: ToolContext, call: CallId) -> ToolOutcome:
        raise NotImplementedError

    async def opened[T](self, folder: str, raw: object, use: Callable[[BinaryIO, str, os.stat_result], T]) -> T:
        """``use(file, path, stat)`` on the opened file, in the worker thread."""
        folders = self._env.folders

        def work() -> T:
            handle, shown, info = folders.open_read(folder, raw)
            with handle:
                return use(handle, shown, info)

        return await run_blocking(work)


class ListFiles(_MachineTool):
    name = "list_files"
    description = "List a directory in this conversation's folder. " + _PATHS
    properties = {"path": {"type": "string", "description": "Directory to list (default: the folder itself)."}}

    async def act(self, args: Mapping[str, Any], folder: str, ctx: ToolContext, call: CallId) -> ToolOutcome:
        shown, raw, truncated = await run_blocking(self._env.folders.listing, folder, args.get("path", "."),
                                                   MAX_LIST_ENTRIES)
        entries = [Entry(str(e["name"]), str(e["kind"]), e.get("size")) for e in raw]  # type: ignore[arg-type]
        return listing_outcome(shown, entries, truncated)


class ReadFile(_MachineTool):
    name = "read_file"
    description = ("Read a UTF-8 text file in this conversation's folder. " + _PATHS + "Large files are shown in "
                   "parts: the result says which bytes it shows and the offset to read on from.")
    properties = {
        "path": {"type": "string", "description": "File to read."},
        "offset": {"type": "integer", "minimum": 0, "description": "Byte to start at (default 0)."},
    }
    required = ["path"]

    async def act(self, args: Mapping[str, Any], folder: str, ctx: ToolContext, call: CallId) -> ToolOutcome:
        offset = read_offset(args)
        limit = self._env.files.max_bytes if self._env.files is not None else DEFAULT_FILE_MAX_BYTES
        documents = self._env.documents is not None
        return await self.opened(folder, args.get("path"), lambda handle, shown, info: read_outcome(
            shown, _read_at(handle, offset, MAX_READ_BYTES), info.st_size, offset,
            lambda: _read_all(handle, limit), limit, documents=documents))


class WriteFile(_MachineTool):
    name = "write_file"
    description = "Create or overwrite a text file (written atomically). " + _PATHS
    effect = EffectProfile.SCOPED_WRITE
    properties = {
        "path": {"type": "string", "description": "File to write."},
        "content": {"type": "string", "description": "Full new file content."},
        "create_parents": {"type": "boolean", "description": "Create missing parent directories (default true)."},
    }
    required = ["path", "content"]

    async def act(self, args: Mapping[str, Any], folder: str, ctx: ToolContext, call: CallId) -> ToolOutcome:
        data = str(args["content"]).encode("utf-8")
        if len(data) > MAX_WRITE_BYTES:
            return _refused(_shown(args.get("path")), FolderError(TOO_LARGE))
        path, created = await run_blocking(
            lambda: self._env.folders.write(folder, args.get("path"), data,
                                            create_parents=bool(args.get("create_parents", True))))
        return ToolOutcome(ToolStatus.SUCCEEDED,
                           _text(f"{'created' if created else 'updated'} {path} ({len(data)} bytes)"),
                           raw_result={"path": path, "bytes": len(data), "created": created})


class EditFile(_MachineTool):
    name = "edit_file"
    description = ("Replace exact text in a UTF-8 file. Fails unless old_text occurs exactly expected_replacements "
                   "times (default 1). " + _PATHS)
    effect = EffectProfile.SCOPED_WRITE
    properties = {
        "path": {"type": "string", "description": "File to edit."},
        "old_text": {"type": "string", "description": "Exact text to replace (non-empty)."},
        "new_text": {"type": "string", "description": "Replacement text."},
        "expected_replacements": {"type": "integer", "description": "How many occurrences must exist (default 1)."},
    }
    required = ["path", "old_text", "new_text"]

    async def act(self, args: Mapping[str, Any], folder: str, ctx: ToolContext, call: CallId) -> ToolOutcome:
        shown = _shown(args.get("path"))
        old, new = str(args["old_text"]), str(args["new_text"])
        expected = int(args.get("expected_replacements", 1))
        if not old or expected < 1:
            return ToolOutcome(ToolStatus.FAILED, _text(
                f"{shown}: old_text must be non-empty and expected_replacements >= 1", error=True))
        shown, data, identity = await self.opened(folder, args.get("path"), lambda handle, path, info: (
            path, _read_all(handle, MAX_WRITE_BYTES + 1), _identity(info)))
        if len(data) > MAX_WRITE_BYTES:
            return _refused(shown, FolderError(TOO_LARGE))
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return ToolOutcome(ToolStatus.FAILED, _text(f"{shown}: not a UTF-8 text file", error=True))
        found = text.count(old)
        if found != expected:
            return ToolOutcome(ToolStatus.FAILED, _text(
                f"{shown}: found {found} occurrence(s) of old_text, expected {expected}; nothing changed", error=True),
                raw_result={"path": shown, "found": found, "expected": expected})
        updated = text.replace(old, new).encode("utf-8")
        if len(updated) > MAX_WRITE_BYTES:
            return _refused(shown, FolderError(TOO_LARGE))
        # The path that was opened, with the identity it had: changed since → CHANGED, nothing written.
        await run_blocking(lambda: self._env.folders.write(folder, shown, updated, expected=identity))
        return ToolOutcome(ToolStatus.SUCCEEDED, _text(f"edited {shown}: replaced {found} occurrence(s)"),
                           raw_result={"path": shown, "replacements": found, "bytes": len(updated)})


def _identity(info: os.stat_result) -> list[int]:
    from himari.environment.machine.folders import identity

    return identity(info)


class DeleteFile(_MachineTool):
    name = "delete_file"
    description = "Delete one file (not directories). " + _PATHS
    effect = EffectProfile.SCOPED_WRITE
    properties = {"path": {"type": "string", "description": "File to delete."}}
    required = ["path"]

    async def act(self, args: Mapping[str, Any], folder: str, ctx: ToolContext, call: CallId) -> ToolOutcome:
        path = await run_blocking(self._env.folders.delete, folder, args.get("path"))
        return ToolOutcome(ToolStatus.SUCCEEDED, _text(f"deleted {path}"), raw_result={"path": path})


class PresentFile(_MachineTool):
    """``machine/present_file`` (V3-P66 §7.1): a file in the Conversation's folder."""

    name = "present_file"
    description = ("Prepare a file in the conversation. Whether the user can download it depends on "
                   "runtime_state.interface.capabilities.file_download; this does not confirm local saving. The file's bytes at "
                   "this moment are kept; later edits need another call. " + _PATHS)
    properties = {"path": {"type": "string", "description": "File to prepare in the conversation."}}
    required = ["path"]

    async def act(self, args: Mapping[str, Any], folder: str, ctx: ToolContext, call: CallId) -> ToolOutcome:
        files = self._env.files
        if files is None:
            return ToolOutcome(ToolStatus.DENIED, _text("files cannot be given on this server", error=True))

        async def fetch(raw: object, most: int, allowance: int) -> tuple[str, bytes] | ToolOutcome:
            def load(handle: BinaryIO, shown: str, info: os.stat_result) -> tuple[str, bytes] | ToolOutcome:
                refused = present_refusal(shown, info.st_size, most, allowance)
                if refused is not None:
                    return refused
                data = _read_all(handle, info.st_size + 1)
                after = os.fstat(handle.fileno())
                if len(data) != info.st_size or (info.st_size, info.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    return present_changed(shown)
                return shown, data

            return await self.opened(folder, raw, load)

        return await present(files, ctx, args.get("path"), fetch)


class MachineReadDocument(ReadDocument):
    """``machine/read_document`` (V3-P70) on a file in the Conversation's folder."""

    def __init__(self, env: MachineEnvironment, reader: DocumentReading) -> None:
        self._machine = env
        self._reader = reader
        self.definition = document_definition("machine")

    def _limit(self) -> int:
        files = self._machine.files
        return min(MAX_DOCUMENT_BYTES, files.max_bytes if files is not None else DEFAULT_FILE_MAX_BYTES)

    async def _fetch(self, ctx: ToolContext, raw_path: object, limit: int) -> ToolOutcome | tuple[str, bytes]:
        from himari.environment.machine import MachineUnavailable

        shown = _shown(raw_path)
        folders = self._machine.folders

        def load(folder: str) -> tuple[str, int, bytes]:
            handle, path, info = folders.open_read(folder, raw_path)
            with handle:
                if info.st_size > limit:
                    return path, info.st_size, b""
                return path, info.st_size, _read_all(handle, limit + 1)

        try:
            folder = (await self._machine.places(ctx.scope)).cwd
            shown, size, data = await run_blocking(load, folder)
        except MachineUnavailable as unavailable:
            return _unavailable(str(unavailable))
        except DataError as error:
            return _no_places(error)
        except FolderError as error:
            return _refused(shown, error)
        if size > limit or len(data) > limit:
            return ToolOutcome(ToolStatus.FAILED, _text(
                f"{shown}: too large for read_document ({_size(size)}, limit {_size(limit)})", error=True))
        return shown, data


def machine_tools(env: MachineEnvironment) -> list[ToolHandler]:
    tools: list[ToolHandler] = [ListFiles(env), ReadFile(env), WriteFile(env), EditFile(env), DeleteFile(env)]
    if env.files is not None:
        tools.append(PresentFile(env))
    if env.documents is not None:
        tools.append(MachineReadDocument(env, env.documents))
    return tools
