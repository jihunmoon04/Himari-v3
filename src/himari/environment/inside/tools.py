"""Inside structured file Tools (FILE-03): list/read/write/edit/delete.

Limits follow V3-051. Results only ever mention ``/workspace/...`` paths.
Refusals that concern the boundary (outside path, link, mount crossing,
permission, shared inode) are DENIED; other problems are FAILED.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from himari.common import EnvironmentKind, JsonValue, ModelToolResult, TextBlock, ToolId
from himari.environment.inside import fs
from himari.tool.api import (
    EffectProfile,
    ToolContext,
    ToolDefinition,
    ToolInvocation,
    ToolOutcome,
    ToolStatus,
    WorkspaceRequirement,
)

if TYPE_CHECKING:
    from himari.environment.inside import InsideEnvironment

MAX_READ_BYTES = 256 * 1024  # acquisition (V3-051)
MAX_OBSERVATION_BYTES = 64 * 1024  # shown to the model (V3-051)
MAX_WRITE_BYTES = 256 * 1024
MAX_LIST_ENTRIES = 1000

_DENIED = {
    fs.FsCategory.OUTSIDE,
    fs.FsCategory.ACCESS,
    fs.FsCategory.LINK,
    fs.FsCategory.CROSS_DEVICE,
    fs.FsCategory.HARDLINK,
}
_BINDING_ARG: dict[str, JsonValue] = {
    "type": "string",
    "description": "Optional workspace binding id; defaults to the conversation's workspace.",
}


def _schema(properties: dict[str, JsonValue], required: list[str]) -> dict[str, JsonValue]:
    return {
        "type": "object",
        "properties": {**properties, "workspace_binding_id": _BINDING_ARG},
        "required": list(required),
        "additionalProperties": False,
    }


def _text(message: str, *, error: bool = False) -> ModelToolResult:
    return ModelToolResult((TextBlock(message),), is_error=error)


def _failure(path: str, error: fs.FsError) -> ToolOutcome:
    status = ToolStatus.DENIED if error.category in _DENIED else ToolStatus.FAILED
    return ToolOutcome(status, _text(f"{path}: {error}", error=True), raw_result={"category": error.category.name})


class _InsideTool:
    name = ""
    description = ""
    effect = EffectProfile.READ
    properties: dict[str, JsonValue] = {}
    required: list[str] = []

    def __init__(self, env: InsideEnvironment) -> None:
        self._env = env
        self.definition = ToolDefinition(
            tool_id=ToolId(f"inside/{self.name}"),
            description=self.description,
            input_schema=_schema(self.properties, self.required),
            workspace_requirement=WorkspaceRequirement.DEFAULT,
            effect_profile=self.effect,
            environment_kind=EnvironmentKind("inside"),
        )

    async def run(self, invocation: ToolInvocation, ctx: ToolContext) -> ToolOutcome:
        if ctx.binding is None or ctx.workspace is None:
            return ToolOutcome(ToolStatus.DENIED, _text("no workspace is bound to this conversation", error=True))
        args = invocation.arguments
        raw_path = args.get("path", ".")
        host_root = self._env.host_root(ctx.binding.workspace_id)
        cwd = ctx.workspace.cwd_relative
        # Blocking filesystem work runs off the event loop so a large
        # directory or slow disk cannot stall other Conversations.
        return await asyncio.to_thread(self._run_sync, host_root, cwd, raw_path, args)

    def _run_sync(self, host_root: str, cwd: str, raw_path: object, args: Mapping[str, Any]) -> ToolOutcome:
        shown = raw_path if isinstance(raw_path, str) else "?"  # the model's own path, until resolved
        try:
            relative = fs.to_relative(cwd, shown)
            shown = fs.display(relative)
            with fs.open_root(host_root) as root_fd:
                # Idempotent setup: the Conversation's own working directory is
                # created on first use, by READ Tools too (no file content changes).
                fs.ensure_directory(root_fd, fs.to_relative(cwd, "."))
                return self.act(root_fd, relative, args)
        except fs.FsError as error:
            return _failure(shown, error)

    def act(self, root_fd: int, relative: str, args: Mapping[str, Any]) -> ToolOutcome:
        raise NotImplementedError


class ListFiles(_InsideTool):
    name = "list_files"
    description = (
        "List a directory in the workspace. Paths are relative to the conversation's "
        "working directory, or absolute under /workspace."
    )
    properties = {"path": {"type": "string", "description": "Directory to list (default: working directory)."}}

    def act(self, root_fd: int, relative: str, args: Mapping[str, Any]) -> ToolOutcome:
        entries, more = fs.list_directory(root_fd, relative, MAX_LIST_ENTRIES)
        lines: list[str] = []
        size = 0
        shown_count = 0
        for e in entries:
            if e.kind == "directory":
                line = f"{e.name}/"
            elif e.kind == "file":
                line = f"{e.name}  ({e.size} bytes)"
            else:
                line = f"{e.name}  [{e.kind}]"
            size += len(line.encode()) + 1
            if size > MAX_OBSERVATION_BYTES - 512:  # leave room for the header/footer
                more = True
                break
            lines.append(line)
            shown_count += 1
        header = f"{fs.display(relative)} ({shown_count} entries shown)"
        footer = (
            [f"[partial listing: the directory has more entries than shown (limit {MAX_LIST_ENTRIES} entries, "
             f"{MAX_OBSERVATION_BYTES // 1024} KiB); entries are not the first by name]"]
            if more
            else []
        )
        return ToolOutcome(
            ToolStatus.SUCCEEDED,
            _text("\n".join([header, *lines, *footer])),
            raw_result={"path": fs.display(relative), "count": shown_count, "truncated": more},
        )


class ReadFile(_InsideTool):
    name = "read_file"
    description = "Read a UTF-8 text file from the workspace (large files are truncated)."
    properties = {"path": {"type": "string", "description": "File to read."}}
    required = ["path"]

    def act(self, root_fd: int, relative: str, args: Mapping[str, Any]) -> ToolOutcome:
        data, size = fs.read_file(root_fd, relative, MAX_READ_BYTES)
        shown = fs.display(relative)
        if b"\0" in data:
            return ToolOutcome(
                ToolStatus.SUCCEEDED,
                _text(f"{shown} ({size} bytes): binary file, content not shown"),
                raw_result={"path": shown, "size": size, "binary": True},
            )
        visible = data[:MAX_OBSERVATION_BYTES]
        truncated = len(visible) < size
        text = visible.decode("utf-8", errors="replace")
        body = f"{shown} ({size} bytes)\n{text}"
        if truncated:
            body += f"\n[truncated: showing the first {len(visible)} of {size} bytes]"
        return ToolOutcome(
            ToolStatus.SUCCEEDED,
            _text(body),
            raw_result={"path": shown, "size": size, "bytes_shown": len(visible), "truncated": truncated},
        )


class WriteFile(_InsideTool):
    name = "write_file"
    description = "Create or overwrite a text file in the workspace (written atomically)."
    effect = EffectProfile.SCOPED_WRITE
    properties = {
        "path": {"type": "string", "description": "File to write."},
        "content": {"type": "string", "description": "Full new file content."},
        "create_parents": {"type": "boolean", "description": "Create missing parent directories (default true)."},
    }
    required = ["path", "content"]

    def act(self, root_fd: int, relative: str, args: Mapping[str, Any]) -> ToolOutcome:
        data = str(args["content"]).encode("utf-8")
        if len(data) > MAX_WRITE_BYTES:
            raise fs.FsError(fs.FsCategory.TOO_LARGE)
        created = fs.write_file(root_fd, relative, data, create_parents=bool(args.get("create_parents", True)))
        shown = fs.display(relative)
        return ToolOutcome(
            ToolStatus.SUCCEEDED,
            _text(f"{'created' if created else 'updated'} {shown} ({len(data)} bytes)"),
            raw_result={"path": shown, "bytes": len(data), "created": created},
        )


class EditFile(_InsideTool):
    name = "edit_file"
    description = (
        "Replace exact text in a UTF-8 file. Fails unless old_text occurs exactly "
        "expected_replacements times (default 1)."
    )
    effect = EffectProfile.SCOPED_WRITE
    properties = {
        "path": {"type": "string", "description": "File to edit."},
        "old_text": {"type": "string", "description": "Exact text to replace (non-empty)."},
        "new_text": {"type": "string", "description": "Replacement text."},
        "expected_replacements": {"type": "integer", "description": "How many occurrences must exist (default 1)."},
    }
    required = ["path", "old_text", "new_text"]

    def act(self, root_fd: int, relative: str, args: Mapping[str, Any]) -> ToolOutcome:
        shown = fs.display(relative)
        old, new = str(args["old_text"]), str(args["new_text"])
        expected = int(args.get("expected_replacements", 1))
        if not old or expected < 1:
            return ToolOutcome(ToolStatus.FAILED, _text(f"{shown}: old_text must be non-empty and expected_replacements >= 1", error=True))
        data, size, identity = fs.read_file_with_identity(root_fd, relative, MAX_WRITE_BYTES + 1)
        if size > MAX_WRITE_BYTES:
            raise fs.FsError(fs.FsCategory.TOO_LARGE)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return ToolOutcome(ToolStatus.FAILED, _text(f"{shown}: not a UTF-8 text file", error=True))
        found = text.count(old)
        if found != expected:
            return ToolOutcome(
                ToolStatus.FAILED,
                _text(f"{shown}: found {found} occurrence(s) of old_text, expected {expected}; nothing changed", error=True),
                raw_result={"path": shown, "found": found, "expected": expected},
            )
        updated = text.replace(old, new).encode("utf-8")
        if len(updated) > MAX_WRITE_BYTES:
            raise fs.FsError(fs.FsCategory.TOO_LARGE)
        fs.write_file(root_fd, relative, updated, create_parents=False, expected=identity)
        return ToolOutcome(
            ToolStatus.SUCCEEDED,
            _text(f"edited {shown}: replaced {found} occurrence(s)"),
            raw_result={"path": shown, "replacements": found, "bytes": len(updated)},
        )


class DeleteFile(_InsideTool):
    name = "delete_file"
    description = "Delete one file from the workspace (not directories)."
    effect = EffectProfile.SCOPED_WRITE
    properties = {"path": {"type": "string", "description": "File to delete."}}
    required = ["path"]

    def act(self, root_fd: int, relative: str, args: Mapping[str, Any]) -> ToolOutcome:
        fs.delete_file(root_fd, relative)
        shown = fs.display(relative)
        return ToolOutcome(ToolStatus.SUCCEEDED, _text(f"deleted {shown}"), raw_result={"path": shown})


def file_tools(env: InsideEnvironment) -> list[_InsideTool]:
    return [ListFiles(env), ReadFile(env), WriteFile(env), EditFile(env), DeleteFile(env)]
