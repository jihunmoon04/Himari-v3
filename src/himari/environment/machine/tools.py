"""The machine Environment's Tools (V3-P74 §8, §9): ``machine/<name>``.

The same Tools as Inside, on the user's real paths, through the runner.
Results are formatted by the shared Inside helpers. A refusal about the
boundary (outside the Workspaces, permission, hidden) is DENIED; other
problems are FAILED. A request the runner never received is DENIED (nothing
happened); one whose answer was lost raises, and the Dispatcher records the
call's outcome as unknown (V3-047).

With a review record (V3-P76, ``changes.py``), writes and deletes go to the
runner with ``keep`` and are recorded for the user's review: ``change``.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from himari.common import CallId, EnvironmentKind, JsonValue, TextBlock, ToolId
from himari.data.api import DEFAULT_FILE_MAX_BYTES, MAX_DOCUMENT_BYTES, DataError
from himari.documents.api import DocumentReading
from himari.environment.inside import fs
from himari.environment.inside.blocking import run_blocking
from himari.environment.inside.documents import DESCRIPTION, ReadDocument, document_definition
from himari.environment.inside.tools import (
    MAX_LIST_ENTRIES,
    MAX_READ_BYTES,
    MAX_WRITE_BYTES,
    _command_outcome,
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
from himari.runner import api as runner
from himari.sandbox.api import DEFAULT_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS, CommandResult, validate_argv
from himari.tool.api import (
    EffectProfile,
    ToolContext,
    ToolDefinition,
    ToolInvocation,
    ToolOutcome,
    ToolHandler,
    ToolStatus,
    WorkspaceRequirement,
)

if TYPE_CHECKING:
    from himari.environment.machine import MachineEnvironment

log = logging.getLogger("himari.environment.machine")

_DENIED = {runner.OUTSIDE_WORKSPACES, runner.ACCESS, runner.UNAVAILABLE, runner.SETUP_FAILED}
_PATHS = ("Paths are absolute, relative to the working directory, or start with ~ (the user's home). ")
_WRITE_SCOPE = ("You can write only inside this conversation's workspaces (listed in the runtime state); "
                "writing elsewhere is refused. ")
_UNREACHABLE = "the machine runner is not reachable; nothing was done"


def _refused(shown: str, error: runner.RunnerError) -> ToolOutcome:
    status = ToolStatus.DENIED if error.category in _DENIED else ToolStatus.FAILED
    return ToolOutcome(status, _text(f"{shown}: {error}", error=True), raw_result={"category": error.category})


def _unreachable(why: str = _UNREACHABLE) -> ToolOutcome:
    return ToolOutcome(ToolStatus.DENIED, _text(why, error=True), raw_result={"runner": "unreachable"})


def _no_places(error: DataError) -> ToolOutcome:
    """The Conversation's folders could not be read: nothing runs (§10.5)."""
    return ToolOutcome(ToolStatus.DENIED, _text(f"this conversation's folders could not be read ({error.code.value}); "
                                                "nothing was done", error=True), raw_result={"data": error.code.value})


def _shown(raw: object) -> str:
    return raw if isinstance(raw, str) else "?"


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
            input_schema=_schema(self.properties, self.required, binding=False),
            workspace_requirement=WorkspaceRequirement.DEFAULT,
            effect_profile=self.effect,
            environment_kind=EnvironmentKind("machine"),
        )

    async def run(self, invocation: ToolInvocation, ctx: ToolContext) -> ToolOutcome:
        if ctx.binding is None or ctx.workspace is None:
            return ToolOutcome(ToolStatus.DENIED, _text("no workspace is bound to this conversation", error=True))
        from himari.environment.machine import MachineUnavailable

        try:
            places = await self._env.places(ctx.scope)
        except MachineUnavailable as unavailable:
            return _unreachable(str(unavailable))
        except DataError as error:
            return _no_places(error)
        args = invocation.arguments
        roots = list(places.roots)
        try:
            if self.effect is EffectProfile.READ:
                return await self.act(args, places.cwd, roots, ctx, invocation.call_id)
            # All of the Conversation's folders at once (§9.1): which one a path lands in is
            # known only once the runner has resolved it.
            async with self._env.locks.hold(places.roots):
                return await self.act(args, places.cwd, roots, ctx, invocation.call_id)
        except runner.NotSent:
            return _unreachable()
        except runner.RunnerError as error:
            return _refused(_shown(args.get(self.target, ".")), error)

    async def act(self, args: Mapping[str, Any], cwd: str, roots: list[str], ctx: ToolContext,
                  call: CallId) -> ToolOutcome:
        raise NotImplementedError

    async def landing(self, path: object, cwd: str, *, follow: bool) -> object:
        """Where a write lands (V3-P76 §8): the real path its change is
        recorded under; without a review record, the model's own path."""
        if await self._env.change_log() is None:
            return path
        result, _ = await self._env.call(runner.VISIBLE, {"path": path, "cwd": cwd, "follow": follow})
        return str(result["path"])

    async def change(self, ctx: ToolContext, call: CallId, path: object, op: str,
                     args: dict[str, Any]) -> dict[str, Any] | ToolOutcome:
        """The runner's write or delete of ``path`` (from ``landing``),
        recorded for review when there is a record (V3-P76 §7): the intent
        first, then the runner with ``keep``, then its outcome. A refusal
        raises RunnerError as without a record."""
        changes = await self._env.change_log()
        if changes is None:
            result, fds = await self._env.call(op, {**args, "path": path})
            for fd in fds:
                os.close(fd)
            return result
        assert isinstance(path, str)
        try:
            token = await changes.begin(ctx.scope, call, path)
        except Exception:  # noqa: BLE001 — whatever the record's failure, nothing is written
            log.exception("the change of %s could not be recorded", path)
            return ToolOutcome(ToolStatus.FAILED, _text(f"{path}: this change could not be recorded for the user's "
                                                        "review; nothing was written", error=True))
        try:
            result, fds = await self._env.call(op, {**args, "path": path, "keep": True})
        except (runner.RunnerError, runner.NotSent):
            await changes.dropped(token)
            raise
        except BaseException:
            await asyncio.shield(changes.unknown(token))
            raise
        after = result.get("after_identity")
        await asyncio.shield(changes.kept(token, str(result["path"]), fds[0] if fds else None,
                                          tuple(result.get("prior_identity") or ()),
                                          None if after is None else tuple(after), bool(result.get("complete"))))
        return result


class ListFiles(_MachineTool):
    name = "list_files"
    description = ("List a directory on this server. " + _PATHS
                   + "Hidden locations (credentials, HIMARI's own data) are not shown.")
    properties = {"path": {"type": "string", "description": "Directory to list (default: working directory)."}}

    async def act(self, args: Mapping[str, Any], cwd: str, roots: list[str], ctx: ToolContext,
                  call: CallId) -> ToolOutcome:
        result, _ = await self._env.call(runner.LIST, {"path": args.get("path", "."), "cwd": cwd,
                                                       "limit": MAX_LIST_ENTRIES})
        entries = [fs.Entry(str(e["name"]), str(e["kind"]), e.get("size")) for e in result["entries"]]
        return listing_outcome(str(result["path"]), entries, bool(result["truncated"]))


class ReadFile(_MachineTool):
    name = "read_file"
    description = ("Read a UTF-8 text file you can read on this server (anything the user can read, except "
                   "hidden locations). " + _PATHS + "Large files are shown in parts: the result says which bytes "
                   "it shows and the offset to read on from.")
    properties = {
        "path": {"type": "string", "description": "File to read."},
        "offset": {"type": "integer", "minimum": 0, "description": "Byte to start at (default 0)."},
    }
    required = ["path"]

    async def act(self, args: Mapping[str, Any], cwd: str, roots: list[str], ctx: ToolContext,
                  call: CallId) -> ToolOutcome:
        result, fds = await self._env.call(runner.OPEN, {"path": args.get("path"), "cwd": cwd})
        offset = read_offset(args)
        limit = self._env.files.max_bytes if self._env.files is not None else DEFAULT_FILE_MAX_BYTES
        fd = fds[0]
        try:
            return await run_blocking(
                lambda: read_outcome(str(result["path"]), os.pread(fd, MAX_READ_BYTES, offset), int(result["size"]),
                                     offset, lambda: _read_all(fd, limit), limit,
                                     documents=self._env.documents is not None))
        finally:
            os.close(fd)


def _read_all(fd: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    position = 0
    while position < limit:
        chunk = os.pread(fd, min(1 << 20, limit - position), position)
        if not chunk:
            break
        chunks.append(chunk)
        position += len(chunk)
    return b"".join(chunks)


class WriteFile(_MachineTool):
    name = "write_file"
    description = "Create or overwrite a text file (written atomically). " + _WRITE_SCOPE + _PATHS
    effect = EffectProfile.SCOPED_WRITE
    properties = {
        "path": {"type": "string", "description": "File to write."},
        "content": {"type": "string", "description": "Full new file content."},
        "create_parents": {"type": "boolean", "description": "Create missing parent directories (default true)."},
    }
    required = ["path", "content"]

    async def act(self, args: Mapping[str, Any], cwd: str, roots: list[str], ctx: ToolContext,
                  call: CallId) -> ToolOutcome:
        data = str(args["content"]).encode("utf-8")
        if len(data) > MAX_WRITE_BYTES:
            return _refused(_shown(args.get("path")), runner.RunnerError(runner.TOO_LARGE))
        result = await self.change(ctx, call, await self.landing(args.get("path"), cwd, follow=True), runner.WRITE, {
            "cwd": cwd, "write_roots": roots, "content": str(args["content"]),
            "create_parents": bool(args.get("create_parents", True))})
        if isinstance(result, ToolOutcome):
            return result
        path = str(result["path"])
        if result.get("partial"):  # V3-P76 §4.1: a new file taken in place, whose writing did not finish
            return ToolOutcome(ToolStatus.SUCCEEDED, _text(
                f"created {path}, but writing it did not finish; its content may be partial — read it and write it "
                "again", error=True), raw_result={"path": path, "created": True, "partial": True})
        return ToolOutcome(ToolStatus.SUCCEEDED,
                           _text(f"{'created' if result['created'] else 'updated'} {path} ({len(data)} bytes)"),
                           raw_result={"path": path, "bytes": len(data), "created": bool(result["created"])})


class EditFile(_MachineTool):
    name = "edit_file"
    description = ("Replace exact text in a UTF-8 file. Fails unless old_text occurs exactly expected_replacements "
                   "times (default 1). " + _WRITE_SCOPE)
    effect = EffectProfile.SCOPED_WRITE
    properties = {
        "path": {"type": "string", "description": "File to edit."},
        "old_text": {"type": "string", "description": "Exact text to replace (non-empty)."},
        "new_text": {"type": "string", "description": "Replacement text."},
        "expected_replacements": {"type": "integer", "description": "How many occurrences must exist (default 1)."},
    }
    required = ["path", "old_text", "new_text"]

    async def act(self, args: Mapping[str, Any], cwd: str, roots: list[str], ctx: ToolContext,
                  call: CallId) -> ToolOutcome:
        shown = _shown(args.get("path"))
        old, new = str(args["old_text"]), str(args["new_text"])
        expected = int(args.get("expected_replacements", 1))
        if not old or expected < 1:
            return ToolOutcome(ToolStatus.FAILED, _text(
                f"{shown}: old_text must be non-empty and expected_replacements >= 1", error=True))
        result, fds = await self._env.call(runner.OPEN, {"path": args.get("path"), "cwd": cwd})
        try:
            data = await run_blocking(_read_all, fds[0], MAX_WRITE_BYTES + 1)
        finally:
            os.close(fds[0])
        shown = str(result["path"])
        if len(data) > MAX_WRITE_BYTES:
            return _refused(shown, runner.RunnerError(runner.TOO_LARGE))
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return ToolOutcome(ToolStatus.FAILED, _text(f"{shown}: not a UTF-8 text file", error=True))
        found = text.count(old)
        if found != expected:
            return ToolOutcome(ToolStatus.FAILED, _text(
                f"{shown}: found {found} occurrence(s) of old_text, expected {expected}; nothing changed", error=True),
                raw_result={"path": shown, "found": found, "expected": expected})
        updated = text.replace(old, new)
        if len(updated.encode("utf-8")) > MAX_WRITE_BYTES:
            return _refused(shown, runner.RunnerError(runner.TOO_LARGE))
        # The path the runner opened, with the identity it read: changed since → CHANGED, nothing written.
        written = await self.change(ctx, call, shown, runner.WRITE, {"cwd": cwd, "write_roots": roots,
                                                                     "content": updated,
                                                                     "expected": result["identity"]})
        if isinstance(written, ToolOutcome):
            return written
        return ToolOutcome(ToolStatus.SUCCEEDED, _text(f"edited {shown}: replaced {found} occurrence(s)"),
                           raw_result={"path": shown, "replacements": found, "bytes": len(updated.encode("utf-8"))})


class DeleteFile(_MachineTool):
    name = "delete_file"
    description = "Delete one file (not directories). " + _WRITE_SCOPE
    effect = EffectProfile.SCOPED_WRITE
    properties = {"path": {"type": "string", "description": "File to delete."}}
    required = ["path"]

    async def act(self, args: Mapping[str, Any], cwd: str, roots: list[str], ctx: ToolContext,
                  call: CallId) -> ToolOutcome:
        result = await self.change(ctx, call, await self.landing(args.get("path"), cwd, follow=False), runner.DELETE,
                                   {"cwd": cwd, "write_roots": roots})
        if isinstance(result, ToolOutcome):
            return result
        path = str(result["path"])
        return ToolOutcome(ToolStatus.SUCCEEDED, _text(f"deleted {path}"), raw_result={"path": path})


class PresentFile(_MachineTool):
    """``machine/present_file`` (V3-P66 §7.1): any file the model can read."""

    name = "present_file"
    description = ("Give a file to the user: they get a download card in the conversation. The file's bytes at "
                   "this moment are kept; later edits need another call. " + _PATHS)
    properties = {"path": {"type": "string", "description": "File to give to the user."}}
    required = ["path"]

    async def act(self, args: Mapping[str, Any], cwd: str, roots: list[str], ctx: ToolContext,
                  call: CallId) -> ToolOutcome:
        files = self._env.files
        if files is None:
            return ToolOutcome(ToolStatus.DENIED, _text("files cannot be given on this server", error=True))

        async def fetch(raw: object, most: int, allowance: int) -> tuple[str, bytes] | ToolOutcome:
            result, fds = await self._env.call(runner.OPEN, {"path": raw, "cwd": cwd})
            fd, shown, size = fds[0], str(result["path"]), int(result["size"])
            try:
                refused = present_refusal(shown, size, most, allowance)
                if refused is not None:
                    return refused
                before = os.fstat(fd)
                data = await run_blocking(_read_all, fd, size + 1)
                after = os.fstat(fd)
                if len(data) != size or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    return present_changed(shown)
                return shown, data
            finally:
                os.close(fd)

        return await present(files, ctx, args.get("path"), fetch)


class RunCommand(_MachineTool):
    """``machine/run_command`` (§9): one command as the user, in the machine sandbox."""

    name = "run_command"
    description = (
        "Run one command as the user in a Linux sandbox on this server. It can read what the user can read, "
        "except hidden locations, and write only inside this conversation's workspaces and /tmp (each command "
        "gets its own /tmp). Network: HTTPS only, to the hosts the current state lists (others get 403 from "
        "the proxy); no SSH, so use https:// git remotes; to push, use git_push (commands have no credentials). argv is a list of strings; use [\"sh\", \"-c\", \"...\"] for shell "
        "syntax. Unix domain sockets cannot be created: use TCP on 127.0.0.1, and the 'fork' start method for "
        "Python multiprocessing. Use a virtual environment in the project for Python packages (the home folder "
        f"is read-only). Runs in the working directory; timeout {DEFAULT_TIMEOUT_SECONDS}s by default "
        f"(max {MAX_TIMEOUT_SECONDS}s)."
    )
    effect = EffectProfile.UNKNOWN  # may touch any file in the Workspaces
    properties = {
        "argv": {"type": "array", "items": {"type": "string"}, "description": "Program and arguments."},
        "timeout_seconds": {"type": "integer", "description": "Optional timeout in seconds."},
    }
    required = ["argv"]

    async def act(self, args: Mapping[str, Any], cwd: str, roots: list[str], ctx: ToolContext,
                  call: CallId) -> ToolOutcome:
        try:
            argv = validate_argv(args.get("argv"))
        except ValueError as error:
            return ToolOutcome(ToolStatus.DENIED, _text(str(error), error=True))
        timeout = args.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
        if not isinstance(timeout, int) or not 1 <= timeout <= MAX_TIMEOUT_SECONDS:
            return ToolOutcome(ToolStatus.DENIED, _text(f"timeout_seconds must be 1..{MAX_TIMEOUT_SECONDS}",
                                                        error=True))
        changes = self._env.changes
        try:
            result, _ = await self._env.call(runner.RUN, {"argv": argv, "cwd": cwd, "write_roots": roots,
                                                          "timeout": timeout})
        except runner.RunnerError as error:
            if error.category in (runner.UNAVAILABLE, runner.SETUP_FAILED):
                return ToolOutcome(ToolStatus.DENIED, _text(f"command execution is unavailable: {error}", error=True),
                                   raw_result={"category": error.category})
            raise
        except runner.NotSent:
            raise
        except BaseException:  # sent: it may have run
            if changes is not None:
                await asyncio.shield(changes.commands_ran(ctx.scope))
            raise
        if changes is not None:  # V3-P76 §9: it may have changed files the review does not know of
            await changes.commands_ran(ctx.scope)
        outcome = _command_outcome(argv, timeout, CommandResult(
            exit_code=result["exit_code"], timed_out=bool(result["timed_out"]),
            stdout=base64.b64decode(result["stdout"]), stderr=base64.b64decode(result["stderr"]),
            stdout_total=int(result["stdout_total"]), stderr_total=int(result["stderr_total"]),
            elapsed_ms=int(result["elapsed_ms"])))
        if result.get("oom"):
            note = "\n[the command ran out of memory: a process was killed (memory limit)]"
            text = "".join(b.text for b in outcome.model_result.blocks if isinstance(b, TextBlock))
            raw = dict(outcome.raw_result) if isinstance(outcome.raw_result, Mapping) else {}
            return ToolOutcome(outcome.status, _text(text + note, error=True), raw_result={**raw, "oom": True})
        return outcome


class GitPush(_MachineTool):
    """``machine/git_push`` (§12): the runner pushes; the repository is only read, in the sandbox."""

    name = "git_push"
    description = (
        "Push one local branch of a git repository in this conversation's workspaces to the branch of the same name "
        "on its 'origin' remote. Only https remotes the current state lists under git_push. It pushes the branch's "
        "commits (commit first with run_command), never forced: if the remote has commits the branch lacks, the push "
        "is rejected; fetch and merge or rebase, then push again."
    )
    effect = EffectProfile.UNKNOWN  # changes a remote repository
    target = "repository"
    properties = {
        "repository": {"type": "string", "description": "The repository's folder (default: working directory)."},
        "branch": {"type": "string", "description": "The local branch to push."},
    }
    required = ["branch"]

    async def act(self, args: Mapping[str, Any], cwd: str, roots: list[str], ctx: ToolContext,
                  call: CallId) -> ToolOutcome:
        result, _ = await self._env.call(runner.GIT_PUSH, {"repository": args.get("repository", "."),
                                                           "branch": args.get("branch"), "cwd": cwd,
                                                           "write_roots": roots})
        branch, url, status = str(result["branch"]), str(result["url"]), str(result["status"])
        new, message = str(result["new"])[:12], str(result["message"])
        if status == "rejected":
            return ToolOutcome(ToolStatus.FAILED, _text(f"the remote rejected {branch} at {url}; nothing was pushed: "
                                                        f"{message}", error=True), raw_result=result)
        text = {"created": f"pushed {branch} to {url}: a new branch at {new}",
                "up_to_date": f"{branch} is already up to date at {url} ({new})"}.get(
                    status, f"pushed {branch} to {url}: {message}")
        return ToolOutcome(ToolStatus.SUCCEEDED, _text(text), raw_result=result)


class MachineReadDocument(ReadDocument):
    """``machine/read_document`` (V3-P70 on the user's paths)."""

    def __init__(self, env: MachineEnvironment, reader: DocumentReading) -> None:
        self._machine = env
        self._reader = reader
        self.definition = document_definition("machine", binding=False,
                                              description=DESCRIPTION.replace(" from the workspace", ""))

    def _limit(self) -> int:
        files = self._machine.files
        return min(MAX_DOCUMENT_BYTES, files.max_bytes if files is not None else DEFAULT_FILE_MAX_BYTES)

    async def _fetch(self, ctx: ToolContext, raw_path: object, limit: int) -> ToolOutcome | tuple[str, bytes]:
        from himari.environment.machine import MachineUnavailable

        assert ctx.workspace is not None
        shown = _shown(raw_path)
        try:
            cwd = (await self._machine.places(ctx.scope)).cwd
            result, fds = await self._machine.call(runner.OPEN, {"path": raw_path, "cwd": cwd})
        except MachineUnavailable as unavailable:
            return _unreachable(str(unavailable))
        except DataError as error:
            return _no_places(error)
        except runner.NotSent:
            return _unreachable()
        except runner.RunnerError as error:
            return _refused(shown, error)
        fd, shown, size = fds[0], str(result["path"]), int(result["size"])
        try:
            if size > limit:
                return ToolOutcome(ToolStatus.FAILED, _text(
                    f"{shown}: too large for read_document ({_size(size)}, limit {_size(limit)}); use run_command",
                    error=True))
            data = await run_blocking(_read_all, fd, limit + 1)
        finally:
            os.close(fd)
        if len(data) > limit:
            return ToolOutcome(ToolStatus.FAILED, _text(f"{shown}: too large for read_document", error=True))
        return shown, data


def machine_tools(env: MachineEnvironment) -> list[ToolHandler]:
    tools: list[ToolHandler] = [ListFiles(env), ReadFile(env), WriteFile(env), EditFile(env), DeleteFile(env),
                                RunCommand(env), GitPush(env)]
    if env.files is not None:
        tools.append(PresentFile(env))
    if env.documents is not None:
        tools.append(MachineReadDocument(env, env.documents))
    return tools
