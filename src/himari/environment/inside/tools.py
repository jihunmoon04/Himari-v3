"""Inside structured file Tools (FILE-03): list/read/write/edit/delete.

Limits follow V3-051. Results only ever mention ``/workspace/...`` paths.
Refusals that concern the boundary (outside path, link, mount crossing,
permission, shared inode) are DENIED; other problems are FAILED.
"""

from __future__ import annotations

import asyncio
import posixpath
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import shlex

from himari.common import EnvironmentKind, JsonValue, ModelToolResult, TextBlock, ToolId
from himari.data.api import (
    DEFAULT_FILE_MAX_BYTES,
    IMAGE_HEAD_BYTES,
    PRESENT_LIMIT_BYTES,
    PRESENT_WINDOW,
    DataError,
    DataErrorCode,
    PresentedFile,
    SniffedImage,
    clean_file_name,
    clean_media_type,
    sniff_image,
)
from himari.environment.inside import fs
from himari.environment.inside.blocking import run_blocking
from himari.sandbox.api import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_TIMEOUT_SECONDS,
    CommandResult,
    SandboxSetupError,
    SandboxState,
    SandboxUnavailableError,
    validate_argv,
)
from himari.tool.api import (
    EffectProfile,
    ToolContext,
    ToolDefinition,
    ToolInvocation,
    ToolOutcome,
    ResultImage,
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


_CUT_MARK = "\n[cut to the result size limit]"


def _fit(text: str, budget: int) -> str:
    """The longest prefix of ``text`` whose UTF-8 encoding fits ``budget``
    bytes (never splits a character)."""
    data = text.encode("utf-8")
    if len(data) <= budget:
        return text
    return data[: max(budget, 0)].decode("utf-8", errors="ignore")


def _cap(message: str) -> str:
    """Last line of defense: every Inside result is at most
    MAX_OBSERVATION_BYTES of UTF-8, whatever produced it."""
    if len(message.encode("utf-8")) <= MAX_OBSERVATION_BYTES:
        return message
    return _fit(message, MAX_OBSERVATION_BYTES - len(_CUT_MARK.encode())) + _CUT_MARK


def _text(message: str, *, error: bool = False) -> ModelToolResult:
    return ModelToolResult((TextBlock(_cap(message)),), is_error=error)


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
        # directory or slow disk cannot stall other Conversations; once
        # started it always completes before this call (and the lock) ends.
        if self.effect is EffectProfile.READ:
            return await run_blocking(self._run_sync, host_root, cwd, raw_path, args)
        async with self._env.write_lock(ctx.binding.workspace_id):
            return await run_blocking(self._run_sync, host_root, cwd, raw_path, args)

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

    PARTIAL = (
        f"[partial listing: the directory has more entries than shown (limit {MAX_LIST_ENTRIES} entries, "
        f"{MAX_OBSERVATION_BYTES // 1024} KiB); entries are not the first by name]"
    )

    def act(self, root_fd: int, relative: str, args: Mapping[str, Any]) -> ToolOutcome:
        entries, more = fs.list_directory(root_fd, relative, MAX_LIST_ENTRIES)
        shown_path = fs.display(relative)
        # Budget the final text: the header (with the widest possible count)
        # and the partial-listing footer are reserved before any entry.
        widest_header = f"{shown_path} ({MAX_LIST_ENTRIES} entries shown)"
        budget = MAX_OBSERVATION_BYTES - len(widest_header.encode()) - len(self.PARTIAL.encode()) - 2
        lines: list[str] = []
        for e in entries:
            if e.kind == "directory":
                line = f"{e.name}/"
            elif e.kind == "file":
                line = f"{e.name}  ({e.size} bytes)"
            else:
                line = f"{e.name}  [{e.kind}]"
            cost = len(line.encode()) + 1
            if cost > budget:
                more = True
                break
            budget -= cost
            lines.append(line)
        header = f"{shown_path} ({len(lines)} entries shown)"
        body = "\n".join([header, *lines, *([self.PARTIAL] if more else [])])
        return ToolOutcome(
            ToolStatus.SUCCEEDED,
            _text(body),
            raw_result={"path": shown_path, "count": len(lines), "truncated": more},
        )


def _visible(data: bytes, budget: int) -> tuple[str, int]:
    """The longest prefix of ``data`` whose shown text (invalid UTF-8 bytes
    as U+FFFD) fits ``budget`` bytes, and how many bytes of ``data`` it
    covers — so the next ``offset`` continues exactly where it stopped."""
    shown: list[str] = []
    used = covered = 0
    for char in data.decode("utf-8", errors="surrogateescape"):
        if "\udc80" <= char <= "\udcff":
            text, raw = "\ufffd", 1
        else:
            text = char
            raw = len(char.encode("utf-8"))
        cost = len(text.encode("utf-8"))
        if used + cost > budget:
            break
        shown.append(text)
        used += cost
        covered += raw
    return "".join(shown), covered


class ReadFile(_InsideTool):
    name = "read_file"
    description = ("Read a UTF-8 text file from the workspace. Large files are shown in parts: "
                   "the result says which bytes it shows and the offset to read on from.")
    properties = {
        "path": {"type": "string", "description": "File to read."},
        "offset": {"type": "integer", "minimum": 0, "description": "Byte to start at (default 0)."},
    }
    required = ["path"]

    def _image(self, root_fd: int, relative: str, shown: str, size: int, sniffed: SniffedImage) -> ToolOutcome:
        limit = self._env.files.max_bytes if self._env.files is not None else DEFAULT_FILE_MAX_BYTES
        line = f"{shown} ({size} bytes): {sniffed.media_type} {sniffed.width}×{sniffed.height}"
        raw: dict[str, JsonValue] = {"path": shown, "size": size, "image": sniffed.media_type,
                                     "width": sniffed.width, "height": sniffed.height}
        if size > limit:
            return ToolOutcome(ToolStatus.SUCCEEDED, _text(f"{line}; too large to show"), raw_result=raw)
        data, _ = fs.read_file(root_fd, relative, limit)
        return ToolOutcome(ToolStatus.SUCCEEDED, _text(line), raw_result=raw,
                           result_images=(ResultImage(data, sniffed.media_type),))

    @staticmethod
    def _notice(offset: int, end: int, size: int) -> str:
        if offset == 0:
            return f"\n[truncated: only the first {end} bytes of this {size}-byte file are shown; read on with offset={end}]"
        return f"\n[truncated: shown bytes {offset}-{end} of {size}; read on with offset={end}]"

    def act(self, root_fd: int, relative: str, args: Mapping[str, Any]) -> ToolOutcome:
        offset = args.get("offset", 0)
        offset = offset if isinstance(offset, int) and not isinstance(offset, bool) and offset > 0 else 0
        data, size = fs.read_file(root_fd, relative, MAX_READ_BYTES, offset)
        shown = fs.display(relative)
        if offset >= size and size:
            return ToolOutcome(ToolStatus.SUCCEEDED, _text(f"{shown} ({size} bytes): offset {offset} is past the end"),
                               raw_result={"path": shown, "size": size, "offset": offset, "chars_shown": 0})
        sniffed = sniff_image(data[:IMAGE_HEAD_BYTES]) if offset == 0 else None
        if sniffed is not None:  # V3-P67 §5 (Q3): the image itself, for a model that sees images
            return self._image(root_fd, relative, shown, size, sniffed)
        if b"\0" in data:
            return ToolOutcome(
                ToolStatus.SUCCEEDED,
                _text(f"{shown} ({size} bytes): binary file, content not shown"),
                raw_result={"path": shown, "size": size, "binary": True},
            )
        # Invalid UTF-8 policy: show invalid bytes as U+FFFD (which can triple
        # the byte count) and say so; the budget applies to the final encoded
        # text, header and notices included. Judged on the part shown, so a
        # read window ending inside a character is not "invalid" (PR #59 C12).
        def header(invalid: bool) -> str:
            return (f"{shown} ({size} bytes" + (f", from byte {offset}" if offset else "")
                    + (", not valid UTF-8: invalid bytes shown as U+FFFD" if invalid else "") + ")\n")

        budget = MAX_OBSERVATION_BYTES - len(header(True).encode()) - len(self._notice(offset, size, size).encode())
        visible, covered = _visible(data, budget)
        part = data[:covered]
        invalid_utf8 = "\ufffd" in part.decode("utf-8", errors="replace") and "\ufffd".encode() not in part
        end = offset + covered
        truncated = end < size
        notice = self._notice(offset, end, size) if truncated else ""
        return ToolOutcome(
            ToolStatus.SUCCEEDED,
            _text(header(invalid_utf8) + visible + notice),
            raw_result={
                "path": shown,
                "size": size,
                "offset": offset,
                "next_offset": end if truncated else None,
                "chars_shown": len(visible),
                "truncated": truncated,
                "invalid_utf8": invalid_utf8,
            },
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


class RunCommand:
    """``inside/run_command``: one bounded command inside the OS Sandbox
    (TOOL-05/ENV-04). argv only — no shell unless argv asks for one; no
    environment overrides; network isolated; Workspace = the principal root."""

    def __init__(self, env: InsideEnvironment) -> None:
        self._env = env
        self.definition = ToolDefinition(
            tool_id=ToolId("inside/run_command"),
            description=(
                "Run one command in an isolated Linux sandbox with the workspace mounted at /workspace "
                "(no network). argv is a list of strings; use [\"sh\", \"-c\", \"...\"] for shell syntax. "
                f"Runs in the conversation's working directory; timeout {DEFAULT_TIMEOUT_SECONDS}s by default "
                f"(max {MAX_TIMEOUT_SECONDS}s)."
            ),
            input_schema=_schema(
                {
                    "argv": {"type": "array", "items": {"type": "string"}, "description": "Program and arguments."},
                    "timeout_seconds": {"type": "integer", "description": "Optional timeout in seconds."},
                },
                ["argv"],
            ),
            workspace_requirement=WorkspaceRequirement.DEFAULT,
            effect_profile=EffectProfile.UNKNOWN,  # may touch any file under /workspace
            environment_kind=EnvironmentKind("inside"),
        )

    async def run(self, invocation: ToolInvocation, ctx: ToolContext) -> ToolOutcome:
        backend = self._env.sandbox
        if ctx.binding is None or ctx.workspace is None:
            return ToolOutcome(ToolStatus.DENIED, _text("no workspace is bound to this conversation", error=True))
        if backend is None or backend.state is not SandboxState.ENFORCED:
            state = "not configured" if backend is None else backend.state.value
            return ToolOutcome(
                ToolStatus.DENIED,
                _text(f"command execution is unavailable: the sandbox is not enforced on this host ({state})", error=True),
                raw_result={"sandbox_state": state},
            )
        try:
            argv = validate_argv(invocation.arguments.get("argv"))
        except ValueError as error:
            return ToolOutcome(ToolStatus.DENIED, _text(str(error), error=True))
        timeout = invocation.arguments.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
        if not isinstance(timeout, int) or not 1 <= timeout <= MAX_TIMEOUT_SECONDS:
            return ToolOutcome(ToolStatus.DENIED, _text(f"timeout_seconds must be 1..{MAX_TIMEOUT_SECONDS}", error=True))
        try:
            cwd_relative = fs.to_relative(ctx.workspace.cwd_relative, ".")
        except fs.FsError as error:
            return _failure(".", error)
        from himari.environment.inside import admission

        host_root = self._env.host_root(ctx.binding.workspace_id)
        async with self._env.write_lock(ctx.binding.workspace_id):
            try:
                # The same verified root fd is admitted and then bound; it stays
                # open from the check through the spawn.
                with fs.open_root(host_root) as root_fd:
                    fs.ensure_directory(root_fd, cwd_relative)
                    await run_blocking(admission.admit_workspace, root_fd, host_root)  # never outlives root_fd
                    result = await backend.run(argv, root_fd=root_fd, cwd=fs.display(cwd_relative), timeout=timeout)
            except fs.FsError as error:
                return _failure(fs.display(cwd_relative), error)
            except admission.AdmissionError as error:
                return ToolOutcome(
                    ToolStatus.DENIED,
                    _text(f"command not run: {error}", error=True),
                    raw_result={"admission": error.category.name},
                )
            except (SandboxUnavailableError, SandboxSetupError):
                return ToolOutcome(
                    ToolStatus.DENIED,
                    _text("command execution is unavailable: the sandbox could not be set up; nothing was run", error=True),
                    raw_result={"sandbox_state": backend.state.value},
                )
        return _command_outcome(argv, timeout, result)


def _command_outcome(argv: list[str], timeout: int, result: object) -> ToolOutcome:
    """Final model text <= MAX_OBSERVATION_BYTES (header, both streams, notices)."""
    assert isinstance(result, CommandResult)
    command = _fit("$ " + shlex.join(argv), 2048)
    if result.timed_out:
        status_line = f"timed out after {timeout}s; the command was killed and may have changed files"
    else:
        status_line = f"exit code: {result.exit_code}"
    header = f"{command}\n{status_line}\n"
    streams = [("stdout", result.stdout, result.stdout_total), ("stderr", result.stderr, result.stderr_total)]
    labels = {
        name: f"--- {name} ({total} bytes{', truncated' if total > len(kept) else ''}) ---\n"
        for name, kept, total in streams
    }
    notice = "\n[output truncated]"
    budget = MAX_OBSERVATION_BYTES - len(header.encode()) - sum(len(v.encode()) for v in labels.values()) - 2 * len(notice.encode())
    texts = {name: kept.decode("utf-8", errors="replace") for name, kept, _ in streams}
    shares = {"stdout": budget // 2, "stderr": budget - budget // 2}
    for name in ("stdout", "stderr"):  # give unused share of one stream to the other
        other = "stderr" if name == "stdout" else "stdout"
        need = len(texts[name].encode())
        if need < shares[name]:
            shares[other] += shares[name] - need
            shares[name] = need
    parts = [header]
    truncated = {}
    for name, kept, total in streams:
        shown = _fit(texts[name], shares[name])
        truncated[name] = len(shown) < len(texts[name]) or total > len(kept)
        parts.append(labels[name] + shown + (notice if truncated[name] else "") + "\n")
    failed = result.timed_out or result.exit_code != 0
    return ToolOutcome(
        ToolStatus.FAILED if result.timed_out else ToolStatus.SUCCEEDED,
        _text("".join(parts).rstrip("\n"), error=failed),
        raw_result={
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "elapsed_ms": result.elapsed_ms,
            "stdout_bytes": result.stdout_total,
            "stderr_bytes": result.stderr_total,
            "stdout_truncated": truncated["stdout"],
            "stderr_truncated": truncated["stderr"],
        },
    )


def _size(n: int) -> str:
    for unit, scale in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= scale:
            return f"{n / scale:.1f} {unit}"
    return f"{n} bytes"


async def _once(data: bytes) -> AsyncIterator[bytes]:
    yield data


class PresentFile(_InsideTool):
    """``inside/present_file`` (V3-P66 §7.1): give the user a Workspace file.
    Its bytes at this moment go to the file store; the result carries them
    as a presented file, recorded with the result. READ: it changes no
    Workspace file, and running it again only stores the same bytes."""

    name = "present_file"
    description = ("Give a workspace file to the user: they get a download card in the conversation. "
                   "The file's bytes at this moment are kept; later edits need another call.")
    properties = {"path": {"type": "string", "description": "File to give to the user."}}
    required = ["path"]

    async def run(self, invocation: ToolInvocation, ctx: ToolContext) -> ToolOutcome:
        files = self._env.files
        if files is None:
            return ToolOutcome(ToolStatus.DENIED, _text("files cannot be given on this server", error=True))
        if ctx.binding is None or ctx.workspace is None:
            return ToolOutcome(ToolStatus.DENIED, _text("no workspace is bound to this conversation", error=True))
        # Checked before anything is stored (review N1): one Conversation's
        # Tools run one at a time, so the sum cannot change before the commit.
        given = await files.catalog.presented_bytes_since(ctx.scope, datetime.now(UTC) - PRESENT_WINDOW)
        loaded = await run_blocking(self._load, self._env.host_root(ctx.binding.workspace_id),
                                    ctx.workspace.cwd_relative, invocation.arguments.get("path"),
                                    files.max_bytes, PRESENT_LIMIT_BYTES - given)
        if isinstance(loaded, ToolOutcome):
            return loaded
        shown, data = loaded
        try:
            blob = await files.blobs.put(ctx.scope.principal_id, _once(data), files.max_bytes)
        except DataError as error:
            if error.code is DataErrorCode.STORE_FULL:
                return ToolOutcome(ToolStatus.FAILED, _text(f"{shown}: the file store is full; nothing was given",
                                                            error=True), raw_result={"refused": "store_full"})
            if error.code is DataErrorCode.TOO_LARGE:
                return ToolOutcome(ToolStatus.FAILED, _text(f"{shown}: larger than {_size(files.max_bytes)}",
                                                            error=True), raw_result={"refused": "too_large"})
            raise
        name = clean_file_name(posixpath.basename(shown))
        presented = PresentedFile(blob, name, clean_media_type(None, name), shown)
        return ToolOutcome(ToolStatus.SUCCEEDED, _text(f"Presented {name} to the user ({_size(blob.size)})."),
                           raw_result={"path": shown, "size": blob.size, "sha256": blob.sha256},
                           presented_files=(presented,))

    @staticmethod
    def _load(host_root: str, cwd: str, raw_path: object, max_bytes: int,
              allowance: int) -> tuple[str, bytes] | ToolOutcome:
        shown = raw_path if isinstance(raw_path, str) else "?"
        try:
            relative = fs.to_relative(cwd, shown)
            shown = fs.display(relative)
            with fs.open_root(host_root) as root_fd:
                before = fs.identity(root_fd, relative)  # regular, one link
                size = before[2]
                if size > max_bytes:
                    return ToolOutcome(ToolStatus.FAILED, _text(
                        f"{shown}: {_size(size)}, larger than the {_size(max_bytes)} a file may be", error=True),
                        raw_result={"refused": "too_large", "size": size})
                if size > allowance:
                    return ToolOutcome(ToolStatus.FAILED, _text(
                        f"{shown}: too much was given in the last hour (limit {_size(PRESENT_LIMIT_BYTES)}); "
                        "nothing was given", error=True), raw_result={"refused": "limit", "size": size})
                data, total, opened = fs.read_file_with_identity(root_fd, relative, size + 1)
                if opened != before or fs.identity(root_fd, relative) != before or len(data) != total:
                    return ToolOutcome(ToolStatus.FAILED, _text(
                        f"{shown}: the file changed while it was being read; nothing was given", error=True),
                        raw_result={"refused": "changed"})
                return shown, data
        except fs.FsError as error:
            return _failure(shown, error)


def file_tools(env: InsideEnvironment) -> list[_InsideTool]:
    tools: list[_InsideTool] = [ListFiles(env), ReadFile(env), WriteFile(env), EditFile(env), DeleteFile(env)]
    if env.files is not None:
        tools.append(PresentFile(env))
    return tools
