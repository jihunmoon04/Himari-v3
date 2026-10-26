"""HIMARI Web server: a JSON API over the assembled Application plus the
static UI (V3-P46).

Authentication: the local operator signs in with the operator secret
(``POST /api/session``) and gets an in-memory session in an ``HttpOnly``
``SameSite=Strict`` cookie; a CLI sends ``Authorization: Bearer`` with the
token derived from the secret. Every other API route requires one of the
two. Foreign or missing Conversations are indistinguishable (404).

The browser reads progress by polling: the job status of a Conversation
and its Events after the last sequence it has. Messages start background
jobs (202); a second message while one runs is refused (409). While a job
runs, ``GET …/live`` streams what its model is writing (V3-P73 §6.1,
``live.py``): display only, never a record; at most ``live.PER_PRINCIPAL``
at once per principal (429), and every stream ends when the server begins to
shut down (``begin_shutdown``).

The trash (V3-P65 §9): ``POST …/trash`` moves a Conversation there (a
running job is stopped first), ``POST …/restore`` brings it back, and
``DELETE`` deletes one in the trash for good. A trashed Conversation is
missing (404) to every other route; ``GET /api/conversations?trashed=1``
lists the trash.

Files (V3-P66 §9): ``POST …/uploads?name=`` takes one file's bytes as the
body (outside the general body limit, held to the file limit) and stages
it; a message names staged uploads in ``attachments``. ``GET …/files`` lists
a Conversation's files and ``GET …/files/{id}/{version}`` downloads one —
always as an attachment of type octet-stream, sandboxed, so nothing a user
or the model made opens on this origin. Events carry their attachments and
the files a call presented. An image's model copy (V3-P67 §8) is shown
inline from ``GET …/files/{id}/{version}/view``: a JPEG or PNG HIMARI made,
never the uploaded bytes; file views say whether there is one (``image``)
and tool results list the images the call returned (``images``).

Tool views (MCP Apps, V3-P58): events say which calls have one
(``app_view``); ``GET …/calls/{run}/{call}/app`` gives a view as JSON with a
signed URL of the sandbox origin's proxy (``sandbox.py``). The view's HTML
is never served from this origin.
"""

from __future__ import annotations

import asyncio
import logging
import os
import posixpath
import re
import secrets
import threading
import time
from urllib.parse import quote
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from importlib import resources
from typing import Annotated, Any, BinaryIO, Literal

from fastapi import Depends, FastAPI, Path, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from himari.bootstrap.application import (
    CLOUD_FILESYSTEMS,
    Application,
    AppViewRefused,
    AppViewState,
    ConversationStillStopping,
    DeleteNotice,
    FileNameRefused,
    MemoryLine,
    FilesUnavailable,
    ModelChoiceView,
    ModelRefused,
    ModelView,
    PromptRefused,
    PromptView,
    ToolSourceView,
    UnknownToolSource,
    UploadsFolderUnusable,
    WorkspaceBusy,
    WorkspaceRefused,
    AttachedFolder,
    FolderConversations,
    ReferenceRequest,
)
from himari.bootstrap.changes import ChangeConflict, ChangeDiff, ChangesView
from himari.bootstrap.explorer import PDF_APPEND_MAX_BYTES, ExplorerRefused, Listing, ShownFile
from himari.bootstrap.operator import OPERATOR, derive_cli_bearer, verify_operator_token
from himari.common import (
    ASK_USER_TOOL_ID,
    AttachmentRef,
    FileReference,
    CallId,
    CallRef,
    ContentBlock,
    ConversationId,
    FileId,
    PrincipalId,
    RunId,
    TextBlock,
    TrustedScope,
    thaw,
)
from himari.control.api import ContextStatus, ControlError, ControlErrorCode, JobStatus, TurnUsage
from himari.data.api import (
    MAX_ATTACHED_WORKSPACES,
    MAX_ATTACHMENTS,
    MAX_INSTRUCTIONS_CHARS,
    MAX_LIST_LIMIT,
    MAX_MEMORIES,
    MAX_MEMORY_CHARS,
    MAX_WORKSPACE_LABEL_CHARS,
    MAX_WORKSPACE_NOTE_CHARS,
    MAX_WORKSPACES,
    AssistantFinal,
    AssistantToolCalls,
    ConversationEvent,
    ConversationSummary,
    DataError,
    DataErrorCode,
    FileOrigin,
    FileRecord,
    ImageStatus,
    Instructions,
    Memory,
    MemoryCategory,
    RegisteredWorkspace,
    ToolResultRecord,
    UserMessage,
    view_state,
)
from himari.interfaces.web import live
from himari.interfaces.web.config import WebConfig
from himari.sandbox.api import SandboxState
from himari.interfaces.web.sandbox import SandboxGrant, SandboxSigner
from himari.interfaces.web.security import (
    SESSION_COOKIE,
    BodyLimitMiddleware,
    OriginGuardMiddleware,
    SecurityHeadersMiddleware,
    error_body,
    page_csp,
)

log = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 32_000
MAX_EVENTS_PAGE = 500
SESSION_SECONDS = 7 * 24 * 3600
FAILED_LOGIN_DELAY_SECONDS = 1.0
STATIC_FILES = {"app.css": "text/css", "app.js": "text/javascript", "app_view.js": "text/javascript",
                "rich_text.js": "text/javascript", "appearance.js": "text/javascript", "live.js": "text/javascript",
                "files.js": "text/javascript",  # the file explorer's parts (V3-P75 §3)
                "local_store.js": "text/javascript", "drafts.js": "text/javascript",  # kept in the browser (V3-P78 §5)
                "codemirror/codemirror.js": "text/javascript",  # the web editor (V3-P75 §7.6), loaded on first use
                # The PDF view (V3-P77 §3): its part, and PDF.js with its worker, the image decoders it may
                # import and the CMaps it may fetch; each loaded on first use.
                "pdfpane.js": "text/javascript", "pdfjs/pdf.min.mjs": "text/javascript",
                # Its ink (§4, §5): the strokes, perfect-freehand's outlines, the incremental update it writes.
                "pdfink.js": "text/javascript", "pdfdoc.js": "text/javascript",
                "inklab.js": "text/javascript",  # the ink lab (§4.6), asked for with ?ink-lab
                "freehand/perfect-freehand.mjs": "text/javascript",
                "pdfjs/pdf.worker.min.mjs": "text/javascript",
                "pdfjs/wasm/openjpeg_nowasm_fallback.js": "text/javascript",
                "pdfjs/wasm/jbig2_nowasm_fallback.js": "text/javascript",
                # KaTeX for the formulas in assistant text, loaded by rich_text.js on the first one.
                "katex/katex.min.js": "text/javascript", "katex/katex.min.css": "text/css"}
KATEX_FONTS = ("AMS-Regular", "Caligraphic-Bold", "Caligraphic-Regular", "Fraktur-Bold", "Fraktur-Regular",
               "Main-Bold", "Main-BoldItalic", "Main-Italic", "Main-Regular", "Math-BoldItalic", "Math-Italic",
               "SansSerif-Bold", "SansSerif-Italic", "SansSerif-Regular", "Script-Regular", "Size1-Regular",
               "Size2-Regular", "Size3-Regular", "Size4-Regular", "Typewriter-Regular")
STATIC_FILES.update({f"katex/fonts/KaTeX_{name}.woff2": "font/woff2" for name in KATEX_FONTS})
STATIC_FILES.update({f"pdfjs/cmaps/{cmap.name}": "application/octet-stream"
                     for cmap in (resources.files("himari.interfaces.web") / "static/pdfjs/cmaps").iterdir()
                     if cmap.name.endswith(".bcmap")})

Opener = Callable[[], Awaitable[Application]]


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# --- sessions ---------------------------------------------------------------------------


class Sessions:
    """In-memory browser sessions: a restart signs everyone out."""

    def __init__(self, lifetime: float = SESSION_SECONDS) -> None:
        self._lifetime = lifetime
        self._by_id: dict[str, tuple[PrincipalId, float]] = {}

    def create(self, principal: PrincipalId) -> str:
        session_id = secrets.token_urlsafe(32)
        self._by_id[session_id] = (principal, time.monotonic() + self._lifetime)
        return session_id

    def principal(self, session_id: str | None) -> PrincipalId | None:
        if not session_id:
            return None
        entry = self._by_id.get(session_id)
        if entry is None:
            return None
        if entry[1] < time.monotonic():
            del self._by_id[session_id]
            return None
        return entry[0]

    def revoke(self, session_id: str | None) -> None:
        if session_id:
            self._by_id.pop(session_id, None)


# --- request bodies ------------------------------------------------------------------------


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class LoginBody(_Body):
    token: str = Field(max_length=256)


class CreateBody(_Body):
    title: str | None = Field(default=None, max_length=1000)


class RenameBody(_Body):
    title: str | None = Field(max_length=1000)


MAX_REFERENCES = 10  # a message's file references (V3-P76 §3.1)


class ReferenceBody(_Body):
    """V3-P76 §3.2: a reference as the browser sends it — the range and its text's hash, never the text."""

    path: str = Field(min_length=1, max_length=4096)
    identity: IdentityIn
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, ge=1)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class MessageBody(_Body):
    text: str
    attachments: list[str] = Field(default_factory=list)  # staged uploads' file ids (V3-P66)
    references: list[ReferenceBody] = Field(default_factory=list, max_length=MAX_REFERENCES)  # V3-P76


class ModelBody(_Body):
    model: str | None = Field(max_length=200)  # null: follow the default model
    effort: str | None = Field(default=None, max_length=32)  # null: the model's default effort


class InstructionsBody(_Body):
    text: str  # its limit is Data's (V3-P69 §6), counted after newlines are normalized
    revision: int = Field(ge=0)  # the revision the text was edited from


class MemoryBody(_Body):
    category: Literal["user", "preference", "project", "reference"]  # MemoryCategory's values
    text: str  # its limit is Data's (V3-P71 §4.2)


class MemoryEditBody(MemoryBody):
    revision: int = Field(ge=1)  # the revision the note was edited from


class ToolSourceBody(_Body):
    enabled: bool | None  # null: follow the source's default


# A file's identity as the browser holds it: [dev, ino, size, mtime_ns] as decimal strings, since an mtime in
# nanoseconds is past what a JavaScript number keeps exactly (V3-P75 §7.2).
IdentityIn = Annotated[list[Annotated[str, Field(pattern=r"^[0-9]{1,20}$")] | int], Field(min_length=4, max_length=4)]


def identity_out(identity: Iterable[object]) -> list[str]:
    return [str(v) for v in identity]


def identity_in(header: str) -> list[int]:
    """An identity sent in a header (V3-P77 §5.3): four decimal numbers, comma-separated."""
    parts = [part.strip() for part in header.split(",")]
    if len(parts) != 4 or not all(re.fullmatch(r"[0-9]{1,20}", part) for part in parts):
        raise ApiError(400, "invalid_argument", "X-Himari-Expected is the PDF's identity as it was read.")
    return [int(part) for part in parts]


class SaveFileBody(_Body):
    """A file saved from the web editor (V3-P75 §7.7): its whole text, and its identity as it was read."""

    path: str = Field(min_length=1, max_length=4096)
    content: str  # at most 1 MiB as UTF-8 (the explorer checks)
    expected: IdentityIn


class RecreateFileBody(_Body):
    """V3-P78 §4.2: the editor's text as a new file where the file it was editing is gone."""

    path: str = Field(min_length=1, max_length=4096)
    content: str  # at most 1 MiB as UTF-8 (the explorer checks)


class NewPathBody(_Body):
    """A new empty file or folder (V3-P75 §7.5, §7.7)."""

    path: str = Field(min_length=1, max_length=4096)


class DeleteFileBody(_Body):
    path: str = Field(min_length=1, max_length=4096)
    expected: IdentityIn


class PutBackBody(_Body):
    """A PDF's previous version written over the file as it was read (V3-P77 §6.3)."""

    path: str = Field(min_length=1, max_length=4096)
    expected: IdentityIn


class FileStateIn(_Body):
    """A file as the owner saw it (V3-P76 §6): there, with this identity, or not there."""

    exists: bool
    identity: IdentityIn | None = None


class AcceptChangesBody(_Body):
    """Accept the agent's changes of ``paths``, or ``all`` of them (the hidden ones too)."""

    paths: list[Annotated[str, Field(min_length=1, max_length=4096)]] = Field(default_factory=list, max_length=1000)
    all: bool = False


class RevertChangeBody(_Body):
    """Revert one file; ``seen``: after a conflict, the state shown, to revert anyway if it is still so."""

    path: str = Field(min_length=1, max_length=4096)
    seen: FileStateIn | None = None


class FolderConversationBody(_Body):
    """Open a conversation in a folder (V3-P75 §6): its latest, or ``new`` one."""

    path: str = Field(min_length=1, max_length=4096)
    new: bool = False


class WorkspaceBody(_Body):
    """A folder to register (V3-P74 §10.3): absolute, or under the user's home."""

    path: str = Field(min_length=1, max_length=4096)
    label: str | None = Field(default=None, max_length=MAX_WORKSPACE_LABEL_CHARS)  # null: the folder's name
    note: str | None = Field(default=None, max_length=MAX_WORKSPACE_NOTE_CHARS)


class WorkspaceEditBody(_Body):
    label: str = Field(max_length=MAX_WORKSPACE_LABEL_CHARS)
    note: str | None = Field(max_length=MAX_WORKSPACE_NOTE_CHARS)


class AttachBody(_Body):
    """One of the two: a registered folder, or a path (registered first)."""

    workspace_id: str | None = Field(default=None, max_length=64)
    path: str | None = Field(default=None, min_length=1, max_length=4096)
    label: str | None = Field(default=None, max_length=MAX_WORKSPACE_LABEL_CHARS)


class WorkingBody(_Body):
    binding_id: str = Field(max_length=64)


class PromptExpandBody(_Body):
    """In the body, not the path: a prompt's name may hold "/" (V3-P60 C4)."""

    source: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    arguments: dict[str, str] = Field(default_factory=dict, max_length=64)


# --- JSON views ------------------------------------------------------------------------------


def _time(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _text(blocks: tuple[ContentBlock, ...]) -> str:
    """The text blocks; attachments are listed apart (``attachments``)."""
    return "\n\n".join(b.text for b in blocks if isinstance(b, TextBlock))


def image_view(record: FileRecord) -> dict[str, Any] | None:
    """V3-P67 §8: the original's judged type and size, and ``state`` (the
    ``view_state``): a preview exists only when it is ``ready``."""
    if record.image_status is ImageStatus.NOT_IMAGE:
        return None
    view = record.view
    return {"state": view_state(record).value, "media_type": record.image_type, "width": record.image_width,
            "height": record.image_height,
            # The copy's size (orientation applied): the page keeps its box before it loads (PR #63 review C1).
            "preview": None if view is None else {"width": view.width, "height": view.height}}


def file_view(record: FileRecord) -> dict[str, Any]:
    return {"file_id": record.file_id, "version": record.version, "origin": record.origin.value,
            "name": record.name, "media_type": record.media_type, "size": record.size,
            "created_at": _time(record.created_at), "workspace_path": record.workspace_path,
            "expired": record.expired, "image": image_view(record)}


def _attachment(ref: AttachmentRef, files: Files) -> dict[str, Any]:
    found = files.get((ref.file_id, ref.version))
    return (file_view(found) if found is not None
            else {"file_id": ref.file_id, "version": ref.version, "name": None, "expired": True, "image": None})


_DOWNLOAD_SAFE = "!#$&+-.^_`|~"  # RFC 5987 attr-char besides letters and digits


def content_disposition(name: str) -> str:
    """V3-P66 §7.2 (review C9): an ASCII stand-in name and the exact one."""
    fallback = "".join(c if " " <= c <= "~" and c not in '"\\;' else "_" for c in name) or "file"
    return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{quote(name, safe=_DOWNLOAD_SAFE)}'


type Files = dict[tuple[str, int], FileRecord]


def summary_view(summary: ConversationSummary) -> dict[str, Any]:
    return {
        "id": summary.conversation_id,
        "title": summary.title,
        "preview": summary.preview,
        "created_at": _time(summary.created_at),
        "last_activity_at": _time(summary.last_activity_at),
        "last_sequence": summary.last_sequence,
    }


def trashed_view(summary: ConversationSummary, trash_days: int) -> dict[str, Any]:
    """A Conversation in the trash, with when it is deleted for good (V3-P65 §9)."""
    trashed_at = summary.trashed_at
    assert trashed_at is not None
    return {**summary_view(summary), "trashed_at": _time(trashed_at),
            "purge_after": _time(trashed_at + timedelta(days=trash_days))}


def delete_notice_view(notice: DeleteNotice) -> dict[str, Any]:
    return {"servers": list(notice.servers), "unconfigured": list(notice.unconfigured), "running": notice.running,
            "trash_days": notice.trash_days, "memory_count": notice.memories}


def event_view(event: ConversationEvent, views: dict[CallRef, tuple[bool, bool]] | None = None,
               models: dict[RunId, str] | None = None, files: Files | None = None,
               presented: dict[CallRef, list[FileRecord]] | None = None,
               memory: dict[CallRef, MemoryLine] | None = None) -> dict[str, Any]:
    """``views``: Application.view_flags for the page — (shown while it
    runs, stored with its result) per call with a view that may be shown.
    ``models``: the model of each Run that wrote an assistant event (V3-P64 §7).
    ``files``: the page's attachments by (id, version); ``presented``: the
    files each call gave the user (V3-P66 §7.3); ``memory``: what each
    memory call changed (V3-P71 §7.1)."""
    views = {} if views is None else views
    models = {} if models is None else models
    files = {} if files is None else files
    presented = {} if presented is None else presented
    memory = {} if memory is None else memory
    view: dict[str, Any] = {"sequence": event.sequence, "kind": event.kind.value, "created_at": _time(event.created_at)}
    payload = event.payload
    if isinstance(payload, UserMessage):
        view["text"] = _text(payload.blocks)
        # A call is identified by (run_id, call_id): call ids repeat across Runs.
        view["answers_call"] = None if payload.answers_call is None else payload.answers_call.call_id
        view["answers_run"] = None if payload.answers_call is None else payload.answers_call.run_id
        view["attachments"] = [_attachment(b, files) for b in payload.blocks if isinstance(b, AttachmentRef)]
        view["references"] = [reference_view(b) for b in payload.blocks if isinstance(b, FileReference)]
    elif isinstance(payload, AssistantFinal):
        view["text"] = _text(payload.blocks)
        view["model"] = models.get(event.origin_run_id) if event.origin_run_id else None
        view["run_id"] = event.origin_run_id  # the live output it replaces (V3-P73 §7.2)
    elif isinstance(payload, AssistantToolCalls):
        view["text"] = _text(payload.commentary)
        view["model"] = models.get(event.origin_run_id) if event.origin_run_id else None
        view["run_id"] = event.origin_run_id
        run = event.origin_run_id
        view["calls"] = [{"call_id": c.call_id, "tool_id": c.tool_id, "arguments": thaw(c.arguments),
                          "app_view": run is not None and views.get(CallRef(run, c.call_id), (False, False))[0]}
                         for c in sorted(payload.calls, key=lambda c: c.ordinal)]
    elif isinstance(payload, ToolResultRecord):
        view["run_id"] = payload.call.run_id
        view["call_id"] = payload.call.call_id
        view["is_error"] = payload.model_result.is_error
        view["text"] = _text(payload.model_result.blocks)
        view["app_view"] = views.get(payload.call, (False, False))[1]
        view["presented_files"] = [file_view(f) for f in presented.get(payload.call, [])]
        line = memory.get(payload.call)
        view["memory"] = None if line is None else {"action": line.action, "id": line.memory_id, "text": line.text,
                                                    "undo": line.undo}
        # V3-P67 §8: the images the call returned (kept as files of the call) — never an
        # ask_user answer's attachments, which are its result too (PR #63 review C2).
        view["images"] = [file_view(found) for b in payload.model_result.blocks if isinstance(b, AttachmentRef)
                          if (found := files.get((b.file_id, b.version))) is not None
                          and found.origin is FileOrigin.TOOL]
    return view


def app_view_view(state: AppViewState, sandbox_url: str | None) -> dict[str, Any]:
    view: dict[str, Any] = {
        "state": state.state, "source": state.source, "tool_id": state.tool_id,
        "tool": None if state.tool is None else thaw(state.tool),
        "arguments": thaw(state.arguments), "status": state.status.value,
    }
    if state.resource is None:  # ended without a view: how to close it, and why
        view["outcome"], view["reason"] = state.outcome, state.reason
        return view
    declared = state.resource.csp
    view.update({
        "digest": state.resource.digest, "html": state.resource.html, "prefers_border": state.resource.prefers_border,
        "csp": {"connect_domains": list(state.csp.connect_domains),
                "resource_domains": list(state.csp.resource_domains)},
        "declared_csp": {"connect_domains": list(declared.connect_domains),
                         "resource_domains": list(declared.resource_domains),
                         "frame_domains": list(declared.frame_domains),
                         "base_uri_domains": list(declared.base_uri_domains)},
        "sandbox_url": sandbox_url,
        "result": None if state.state != "done" else thaw(state.result),
    })
    return view


MAX_DETAIL_CHARS = 500  # a job's result detail, as shown to its owner


def job_view(status: JobStatus) -> dict[str, Any]:
    result = status.result
    detail = None if result is None or not result.detail else result.detail
    if detail is not None and len(detail) > MAX_DETAIL_CHARS:
        detail = detail[: MAX_DETAIL_CHARS - 1] + "…"
    return {
        "running": status.running,
        "generation": status.generation,
        "state": None if result is None else result.state.value,
        "detail": detail,  # why it ended so (e.g. the size that was refused); plain text
        "unknown_calls": 0 if result is None else len(result.unknown_calls),
        "compactions": 0 if result is None else len(result.compaction_run_ids),
        "error": status.error,
        "started_at": _time(status.started_at),
        "finished_at": _time(status.finished_at),
        "stop_requested": None if status.stop_requested is None else status.stop_requested.value,
        "turn_usage": None if status.turn_usage is None else turn_usage_view(status.turn_usage),
    }


def turn_usage_view(usage: TurnUsage) -> dict[str, Any]:
    """What the job's drive used so far (V3-P62 §7)."""
    return {"runs": usage.runs, "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
            "cached_input_tokens": usage.cached_input_tokens, "cache_write_tokens": usage.cache_write_tokens,
            "budget": usage.budget, "max_runs": usage.max_runs}


def context_view(status: ContextStatus | None) -> dict[str, Any]:
    """The model's view of a Conversation's size, for display. Events up to
    ``covered_through_sequence`` reach the model only through ``summary``."""
    if status is None:
        return {"enabled": False}
    return {
        "enabled": True,
        "estimated_tokens": status.estimated_tokens,
        "measured": status.measured,
        "budget_tokens": status.budget_tokens,
        "compact_at_tokens": status.compact_at_tokens,
        "hard_limit_tokens": status.hard_limit_tokens,
        "compactions": status.compactions,
        "covered_through_sequence": status.covered_through_sequence,
        "summary": status.summary,
    }


def model_view(view: ModelView) -> dict[str, Any]:
    return {"id": view.model_id, "default_effort": view.default_effort, "efforts": list(view.efforts),
            "window_known": view.window_known, "default": view.is_default, "images": view.images}


def instructions_view(record: Instructions) -> dict[str, Any]:
    """V3-P69 §7: the user's instructions and what a save needs."""
    return {"text": record.text, "revision": record.revision, "updated_at": _time(record.updated_at),
            "max_chars": MAX_INSTRUCTIONS_CHARS}


# How a refused save of the instructions answers (V3-P69 §7): status, code, message.
INSTRUCTIONS_REFUSALS: dict[DataErrorCode, tuple[int, str, str]] = {
    DataErrorCode.VERSION_CONFLICT: (409, "revision_conflict", "The instructions were changed elsewhere since "
                                     "they were opened."),
    # 422, not 413: the body limit middleware turns every 413 into "request_too_large".
    DataErrorCode.TOO_LARGE: (422, "too_large", f"Instructions are at most {MAX_INSTRUCTIONS_CHARS:,} characters."),
    DataErrorCode.INVALID_ARGUMENT: (422, "invalid_instructions", "Instructions may not contain control characters."),
}


def memory_view(note: Memory, titles: dict[ConversationId, tuple[str | None, str]]) -> dict[str, Any]:
    """V3-P71 §7.2. ``titles``: each source Conversation's title (None:
    untitled) and preview, which the page names it by when untitled; a
    source missing from it is in the trash."""
    source = note.source_conversation
    return {"id": note.memory_id, "category": note.category.value, "text": note.text, "origin": note.origin.value,
            "source": None if source is None else {
                "conversation": source, "title": titles.get(source, (None, ""))[0],
                "preview": titles.get(source, (None, ""))[1], "in_trash": source not in titles},
            "revision": note.revision, "created_at": _time(note.created_at), "updated_at": _time(note.updated_at)}


# How a refused write of a note answers (V3-P71 §7.2): status, code, message.
MEMORY_REFUSALS: dict[DataErrorCode, tuple[int, str, str]] = {
    DataErrorCode.NOT_FOUND: (404, "not_found", "No such note."),
    DataErrorCode.VERSION_CONFLICT: (409, "revision_conflict", "The note was changed elsewhere since it was opened."),
    DataErrorCode.TOO_LARGE: (422, "too_large", f"A note is at most {MAX_MEMORY_CHARS:,} characters."),
    DataErrorCode.LIMIT_EXCEEDED: (422, "memory_full", f"At most {MAX_MEMORIES:,} notes are kept; delete one first."),
    DataErrorCode.INVALID_ARGUMENT: (422, "invalid_memory", "A note needs some text and no control characters."),
}


def model_choice_view(view: ModelChoiceView) -> dict[str, Any]:
    chosen = view.chosen
    return {"model": view.model_id, "effort": view.effort,
            "chosen": None if chosen is None else {"model": chosen.model_id, "effort": chosen.reasoning_effort},
            "fallback": view.fallback}


# How a refused model choice answers (V3-P64 §4.3).
MODEL_REFUSAL_STATUS = {"unknown_model": 404, "invalid_effort": 422, "invalid_argument": 400}


def tool_source_view(view: ToolSourceView) -> dict[str, Any]:
    return {
        "source": view.source,
        "enabled": view.enabled,
        "default": view.enabled_by_default,
        "choice": view.choice,
        "available": view.available,
        "tools": list(view.tool_ids),
        "problems": list(view.problems),
    }


def prompt_view(view: PromptView) -> dict[str, Any]:
    return {
        "source": view.source,
        "name": view.name,
        "title": view.title,
        "description": view.description,
        "arguments": [{"name": a.name, "description": a.description, "required": a.required}
                      for a in view.arguments],
    }


# How a file refusal from Data answers (V3-P66 §9): status, code, message.
FILE_REFUSALS: dict[DataErrorCode, tuple[int, str, str]] = {
    DataErrorCode.TOO_LARGE: (413, "file_too_large", "The file is larger than this server takes."),
    DataErrorCode.LIMIT_EXCEEDED: (409, "too_many_pending",
                                   "Too many uploads wait to be sent here; send or drop some first."),
    DataErrorCode.STORE_FULL: (507, "storage_full", "The file store is full. Uploads not sent yet may be "
                               "taking the room; try again later or send them."),
    DataErrorCode.BLOB_MISSING: (410, "file_missing", "The file's stored copy is gone."),
    DataErrorCode.BLOB_EXPIRED: (410, "file_expired", "The file has expired: it was not used for a long time."),
}
UPLOAD_PATH = re.compile(r"^/api/conversations/[^/]+/uploads$")
SAVE_BODY_BYTES = 3 * 1024 * 1024  # a saved file of 1 MiB as JSON, escapes included (V3-P75 §7.7)
SURFACE_PATHS = ("/files", "/trash", "/settings/instructions", "/settings/memory", "/settings/folders")  # V3-P79 §3.1
PAGE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")  # a conversation's id in /c/<id>
SAVE_ROUTES = {("PUT", "/api/machine/file"), ("POST", "/api/machine/file/install")}  # V3-P78 §4.2: the same text
PDF_APPEND_PATH = "/api/machine/pdf/append"
PDF_TRUNCATE_PATH = "/api/machine/pdf/truncate"
EXPECTED_HEADER = "X-Himari-Expected"  # a PDF's identity as the browser read it: four decimal numbers (V3-P77 §5.3)
IDENTITY_HEADER = "X-Himari-Identity"  # a download's identity, as the bytes sent (§5.3)

def workspace_view(workspace: RegisteredWorkspace) -> dict[str, Any]:
    """V3-P74 §10.3: a registered folder."""
    return {"id": workspace.workspace_id, "path": workspace.location, "label": workspace.label,
            "note": workspace.note, "created_at": _time(workspace.created_at), "attached": workspace.attached,
            "filesystem": workspace.filesystem, "cloud_synced": workspace.filesystem in CLOUD_FILESYSTEMS}


def reference_view(ref: FileReference) -> dict[str, Any]:
    """V3-P76 §3.1: a reference chip in the timeline — where it points, not its excerpt."""
    return {"path": ref.path, "name": posixpath.basename(ref.path), "whole": ref.start is None,
            "start_line": ref.start_line, "end_line": ref.end_line}


def listing_view(listing: Listing) -> dict[str, Any]:
    """V3-P75 §5.1: a page of a folder; ``next`` is the cursor of the following page."""
    return {"path": listing.path, "parent": listing.parent, "entries": list(listing.entries), "next": listing.next,
            "workspace": None if listing.workspace is None else workspace_view(listing.workspace)}


def folder_conversations_view(folder: FolderConversations) -> dict[str, Any]:
    """V3-P75 §6.1: the conversations working in a folder, and the folders above it that have some."""
    return {"path": folder.path, "own": folder.own, "refused": folder.refused,
            "workspace": None if folder.workspace is None else workspace_view(folder.workspace),
            "conversations": [summary_view(c) for c in folder.conversations],
            "above": [{"workspace": workspace_view(w), "count": n} for w, n in folder.above]}


def shown_file_view(shown: ShownFile) -> dict[str, Any]:
    """V3-P75 §5.2: text as text; an image or other file by its kind only."""
    return {"path": shown.path, "size": shown.size, "identity": identity_out(shown.identity), "modified": shown.modified,
            "kind": shown.kind, "text": shown.text, "truncated": shown.truncated, "editable": shown.editable,
            "cloud_synced": shown.cloud_synced}


def changes_view(changes: ChangesView, running: bool) -> dict[str, Any]:
    """V3-P76 §5: the agent's changes not yet reviewed; the hidden ones counted only (§10)."""
    return {"items": [{"path": c.path, "name": posixpath.basename(c.path), "kind": c.kind, "state": c.state,
                       "interleaved": c.interleaved, "count": c.count, "revertible": c.revertible,
                       "changed_at": _time(c.changed_at)} for c in changes.items],
            "hidden": changes.hidden, "commands_ran": _time(changes.commands_ran), "running": running}


def file_state_out(identity: tuple[int, ...] | None) -> dict[str, Any]:
    return {"exists": bool(identity), "identity": identity_out(identity) if identity else None}


def change_diff_view(diff: ChangeDiff) -> dict[str, Any]:
    return {"path": diff.path, "name": posixpath.basename(diff.path), "kind": diff.kind, "state": diff.state,
            "revertible": diff.state == "ok", "changed": diff.changed,
            "interleaved": diff.interleaved, "before": {"state": diff.before.state, "text": diff.before.text},
            "now": {**file_state_out(diff.now.identity), "text": diff.now.text}}


# How a refused explorer request answers (V3-P75 §5): the runner's category → HTTP status. too_large is
# 422, not 413: the body limit middleware turns every 413 into "request_too_large".
EXPLORER_STATUS = {"not_found": 404, "access": 403, "outside_workspaces": 403, "too_large": 422,
                   "changed": 409, "exists": 409, "versions_unavailable": 507, "reference_stale": 409,
                   "unavailable": 503, "setup_failed": 503, "timed_out": 504, "invalid": 400, "is_directory": 400,
                   "not_directory": 400, "not_regular": 415, "links": 400, "other": 500,
                   "unsupported": 422, "busy": 409, "not_revertible": 409, "outdated": 409}


def attached_view(folder: AttachedFolder) -> dict[str, Any]:
    """One of a Conversation's folders (§10.3): the own one first."""
    return {"binding_id": folder.binding_id, "path": folder.path, "label": folder.label, "note": folder.note,
            "working": folder.working, "own": folder.own, "workspace_id": folder.workspace_id}


# How a refused change of a registered folder answers (V3-P74 §10.3): status, code, message.
WORKSPACE_REFUSALS: dict[DataErrorCode, tuple[int, str, str]] = {
    DataErrorCode.INVALID_STATE_TRANSITION: (409, "workspace_attached", "It is attached to a conversation; detach "
                                             "it there first."),
    DataErrorCode.DUPLICATE_IDENTITY: (409, "duplicate", "That folder is registered already."),
    DataErrorCode.LIMIT_EXCEEDED: (422, "too_many_workspaces", f"At most {MAX_WORKSPACES} folders can be registered, "
                                   f"and {MAX_ATTACHED_WORKSPACES} attached to one conversation."),
    DataErrorCode.TOO_LARGE: (422, "too_large", f"A note is at most {MAX_WORKSPACE_NOTE_CHARS:,} characters."),
    DataErrorCode.INVALID_ARGUMENT: (422, "invalid_workspace", "A label is one line of at most "
                                     f"{MAX_WORKSPACE_LABEL_CHARS} characters; notes have no control characters."),
}


# How a refused prompt answers (V3-P60 §4).
PROMPT_REFUSAL_STATUS = {"unknown_prompt": 404, "source_disabled": 409, "invalid_arguments": 400,
                         "unsupported_content": 422, "server_error": 502}


# --- the application ---------------------------------------------------------------------------


class _LiveResponse(StreamingResponse):
    """A live stream that gives its slot back however it ends — finished,
    cancelled, the client gone — even when its body never started."""

    def __init__(self, content: AsyncIterator[str], release: Callable[[], None], **options: Any) -> None:
        super().__init__(content, **options)
        self._release = release

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._release()


class _FileStream(StreamingResponse):
    """A file sent in chunks, closed however the response ends — finished,
    cancelled, the client gone — even when its body never started (PR #92
    re-review C1); and never under a read still running on its thread: that
    read closes it as it ends (PR #92 review N1). The lock is held only to
    hand the close over, never across a read."""

    def __init__(self, handle: BinaryIO, **options: Any) -> None:
        self._handle = handle
        self._lock = threading.Lock()
        self._reading = self._ending = self._closed = False
        super().__init__(self._chunks(), **options)

    async def _chunks(self) -> AsyncIterator[bytes]:
        while data := await asyncio.to_thread(self._read):
            yield data

    def _read(self) -> bytes:
        with self._lock:
            if self._ending:
                return b""
            self._reading = True
        try:
            return self._handle.read(1 << 16)
        finally:
            with self._lock:
                self._reading = False
                if self._ending:
                    self._shut()

    def _shut(self) -> None:  # with the lock held
        if not self._closed:
            self._closed = True
            self._handle.close()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with self._lock:
                self._ending = True
                if not self._reading:
                    self._shut()


def begin_shutdown(app: FastAPI) -> None:
    """The server got its exit signal: end the live streams now, so that
    uvicorn, which waits for open responses before it stops the application,
    is not held by them (V3-P73 §6.1). Safe from a signal handler."""
    loop = getattr(app.state, "loop", None)
    if loop is not None:
        loop.call_soon_threadsafe(app.state.closing.set)


def create_app(config: WebConfig, opener: Opener, signer: SandboxSigner | None = None) -> FastAPI:
    """``signer``: shared with the sandbox server (``sandbox.py``); made here
    when not given (one per process, so a restart voids old view URLs)."""
    sessions = Sessions()
    signer = SandboxSigner() if signer is None else signer
    closing = asyncio.Event()  # set as the server begins to shut down: the live streams end
    live_slots = live.Slots()  # open live streams of each principal

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.loop = asyncio.get_running_loop()
        application = await opener()
        app.state.himari = application
        app.state.bearer = derive_cli_bearer(application.operator_secret)
        try:
            yield
        finally:
            closing.set()
            await application.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.closing = closing

    def himari(request: Request) -> Application:
        application: Application = request.app.state.himari
        return application

    def principal(request: Request) -> PrincipalId:
        found = sessions.principal(request.cookies.get(SESSION_COOKIE))
        if found is not None:
            return found
        authorization = request.headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and verify_operator_token(token.strip(), request.app.state.bearer):
            return OPERATOR
        raise ApiError(401, "unauthenticated", "Sign in first.")

    def scope_of(conversation_id: str, who: PrincipalId) -> TrustedScope:
        return TrustedScope(who, ConversationId(conversation_id))

    # -- errors: a fixed envelope, never echoing input or internals --------------------

    @app.exception_handler(ApiError)
    async def api_error(request: Request, error: ApiError) -> JSONResponse:
        return JSONResponse({"error": {"code": error.code, "message": error.message}}, status_code=error.status)

    @app.exception_handler(DataError)
    async def data_error(request: Request, error: DataError) -> JSONResponse:
        if error.code in (DataErrorCode.NOT_AUTHORIZED, DataErrorCode.NOT_FOUND):
            return JSONResponse({"error": {"code": "not_found", "message": "No such conversation."}}, status_code=404)
        if error.code is DataErrorCode.INVALID_ARGUMENT:
            return JSONResponse({"error": {"code": "invalid_argument", "message": str(error)}}, status_code=400)
        if error.code is DataErrorCode.ATTACHMENT_REFUSED:  # V3-P66 §9: which chip to mark
            return JSONResponse({"error": {"code": "attachment_refused", "message": str(error).split(": ", 1)[-1],
                                           "file_id": error.file_id}}, status_code=400)
        if error.code in FILE_REFUSALS:
            status, code, message = FILE_REFUSALS[error.code]
            if error.code is DataErrorCode.BLOB_MISSING:  # §7.2: bytes gone or of another size
                log.warning("file store: %s on %s", error, request.url.path)
            return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)
        log.error("unexpected data error: %s", error.code.value)
        return JSONResponse({"error": {"code": "conflict", "message": "The request conflicts with the current state."}},
                            status_code=409)

    @app.exception_handler(ControlError)
    async def control_error(request: Request, error: ControlError) -> JSONResponse:
        if error.code is ControlErrorCode.BUSY:
            return JSONResponse({"error": {"code": "busy", "message": "This conversation is still working."}},
                                status_code=409)
        if error.code is ControlErrorCode.CLOSED:  # V3-P65 §7.1: it is being moved to the trash
            return JSONResponse({"error": {"code": "conversation_closed",
                                           "message": "This conversation is being deleted."}}, status_code=409)
        return JSONResponse({"error": {"code": "conflict", "message": "Not possible right now."}}, status_code=409)

    @app.exception_handler(ConversationStillStopping)
    async def still_stopping(request: Request, error: ConversationStillStopping) -> JSONResponse:
        return JSONResponse({"error": {"code": "still_stopping",
                                       "message": "The current work is still stopping; try again shortly."}},
                            status_code=409)

    @app.exception_handler(FilesUnavailable)
    async def files_unavailable(request: Request, error: FilesUnavailable) -> JSONResponse:
        return JSONResponse({"error": {"code": "files_unavailable",
                                       "message": "Files cannot be attached in this conversation."}}, status_code=409)

    @app.exception_handler(FileNameRefused)
    async def bad_name(request: Request, error: FileNameRefused) -> JSONResponse:
        return JSONResponse({"error": {"code": "invalid_name", "message": "A file needs a name of at most 1000 bytes."}},
                            status_code=400)

    @app.exception_handler(WorkspaceBusy)
    async def workspace_busy(request: Request, error: WorkspaceBusy) -> JSONResponse:
        return JSONResponse({"error": {"code": "workspace_busy", "message": "A command is running in the workspace; "
                                       "send again shortly."}}, status_code=409)

    @app.exception_handler(UploadsFolderUnusable)
    async def uploads_unusable(request: Request, error: UploadsFolderUnusable) -> JSONResponse:
        return JSONResponse({"error": {"code": "uploads_folder_unusable",
                                       "message": "The conversation's uploads is not a folder. Ask the model to tidy "
                                                  "it up, or remove it yourself."}}, status_code=409)

    @app.exception_handler(AppViewRefused)
    async def no_view(request: Request, error: AppViewRefused) -> JSONResponse:
        return JSONResponse({"error": {"code": error.code, "message": "No view to show for this call."}},
                            status_code=404)

    @app.exception_handler(PromptRefused)
    async def prompt_refused(request: Request, error: PromptRefused) -> JSONResponse:
        return JSONResponse({"error": {"code": error.code, "message": error.message}},
                            status_code=PROMPT_REFUSAL_STATUS.get(error.code, 409))

    @app.exception_handler(ModelRefused)
    async def model_refused(request: Request, error: ModelRefused) -> JSONResponse:
        return JSONResponse({"error": {"code": error.code, "message": error.message}},
                            status_code=MODEL_REFUSAL_STATUS.get(error.code, 400))

    @app.exception_handler(WorkspaceRefused)
    async def workspace_refused(request: Request, error: WorkspaceRefused) -> JSONResponse:
        return JSONResponse({"error": {"code": error.code, "message": error.message}},
                            status_code=422 if error.code == "not_usable" else 409)

    @app.exception_handler(ExplorerRefused)
    async def explorer_refused(request: Request, error: ExplorerRefused) -> JSONResponse:
        body: dict[str, Any] = {"code": error.category, "message": str(error)}
        if error.identity is not None:  # a PDF's failed save: the file as it is now (V3-P82 §3.6)
            body["identity"] = identity_out(error.identity)
        if error.partial:
            body["partial"] = True
        return JSONResponse({"error": body}, status_code=EXPLORER_STATUS.get(error.category, 400))

    @app.exception_handler(ChangeConflict)
    async def change_conflict(request: Request, error: ChangeConflict) -> JSONResponse:
        """V3-P76 §6: not reverted; ``now`` is what the file is, to revert anyway with as ``seen``."""
        return JSONResponse({"error": {"code": "changed", "message": str(error), "now": file_state_out(error.now),
                                       "interleaved": error.interleaved}}, status_code=409)

    @app.exception_handler(UnknownToolSource)
    async def unknown_source(request: Request, error: UnknownToolSource) -> JSONResponse:
        return JSONResponse({"error": {"code": "not_found", "message": "No such tool source."}}, status_code=404)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError) -> JSONResponse:
        return JSONResponse({"error": {"code": "invalid_request", "message": "The request is not valid."}},
                            status_code=400)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, error: StarletteHTTPException) -> Response:
        code = {404: "not_found", 405: "method_not_allowed"}.get(error.status_code, "http_error")
        return Response(error_body(code, "Not available."), status_code=error.status_code,
                        media_type="application/json")

    @app.exception_handler(Exception)
    async def unexpected(request: Request, error: Exception) -> JSONResponse:
        log.exception("unexpected error on %s", request.url.path)
        return JSONResponse({"error": {"code": "internal_error", "message": "Something went wrong."}}, status_code=500)

    # -- session -----------------------------------------------------------------------------

    @app.get("/api/session")
    async def read_session(request: Request) -> dict[str, Any]:
        application = himari(request)
        signed_in = sessions.principal(request.cookies.get(SESSION_COOKIE)) is not None
        return {"authenticated": signed_in, "model": application.model_id if signed_in else None,
                "sandbox": application.current_sandbox_state.value if signed_in else None}

    @app.get("/api/server")
    async def read_server(request: Request, who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """What this server runs with: the model, whether commands can run, the budget."""
        application = himari(request)
        turn = application.turn
        return {"model": application.model_id, "sandbox": application.current_sandbox_state.value,
                "models": [model_view(m) for m in application.models],
                "context_budget": application.context_tokens,  # the default model's
                "trash_days": application.trash_days,  # V3-P65: then a trashed conversation is deleted for good
                "images": application.images_available,  # V3-P67 §3.1: model copies can be made here
                "documents": application.documents_status,  # V3-P70 §9
                "memory": application.memory_enabled,  # V3-P71 §9
                "conversation_search": application.conversation_search_enabled,  # V3-P72 §9
                "streaming": application.streaming_enabled,  # V3-P73 §9
                "turn": {"token_budget": turn.turn_budget, "unattended_runs": turn.unattended_runs,
                         "max_runs": turn.max_runs, "no_progress_runs": turn.no_progress_runs,
                         "seen_seconds": turn.seen_seconds}}

    @app.post("/api/session", status_code=204)
    async def sign_in(body: LoginBody, request: Request, response: Response) -> None:
        if not verify_operator_token(body.token.strip(), himari(request).operator_secret):
            await asyncio.sleep(FAILED_LOGIN_DELAY_SECONDS)
            raise ApiError(401, "invalid_token", "That token is not valid.")
        sessions.revoke(request.cookies.get(SESSION_COOKIE))
        response.set_cookie(SESSION_COOKIE, sessions.create(OPERATOR), max_age=SESSION_SECONDS, path="/",
                            httponly=True, samesite="strict", secure=config.secure_cookie)

    @app.delete("/api/session", status_code=204)
    async def sign_out(request: Request, response: Response) -> None:
        sessions.revoke(request.cookies.get(SESSION_COOKIE))
        response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="strict", secure=config.secure_cookie)

    # -- folders on this server as Workspaces (V3-P74 §10.3) -----------------------------------

    def _workspace_refusal(error: DataError, missing: str) -> ApiError | None:
        if error.code is DataErrorCode.NOT_FOUND:
            return ApiError(404, "not_found", missing)
        found = WORKSPACE_REFUSALS.get(error.code)
        return None if found is None else ApiError(*found)

    @app.get("/api/workspaces")
    async def list_workspaces(request: Request, who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """``available``: folders on this server are offered to this user."""
        application = himari(request)
        try:
            items = await application.machine_workspaces(who)
        except WorkspaceRefused:
            return {"available": False, "items": []}
        return {"available": True, "items": [workspace_view(w) for w in items]}

    @app.post("/api/workspaces", status_code=201)
    async def register_workspace(body: WorkspaceBody, request: Request,
                                 who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        try:
            return workspace_view(await himari(request).register_workspace(who, body.path, body.label, body.note))
        except DataError as error:
            refusal = _workspace_refusal(error, "No such workspace.")
            if refusal is None:
                raise
            raise refusal from None

    @app.patch("/api/workspaces/{workspace_id}")
    async def edit_workspace(workspace_id: str, body: WorkspaceEditBody, request: Request,
                             who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        try:
            return workspace_view(await himari(request).update_workspace(who, workspace_id, body.label, body.note))
        except DataError as error:
            refusal = _workspace_refusal(error, "No such workspace.")
            if refusal is None:
                raise
            raise refusal from None

    @app.delete("/api/workspaces/{workspace_id}", status_code=204)
    async def unregister_workspace(workspace_id: str, request: Request, who: PrincipalId = Depends(principal)) -> None:
        try:
            await himari(request).unregister_workspace(who, workspace_id)
        except DataError as error:
            refusal = _workspace_refusal(error, "No such workspace.")
            if refusal is None:
                raise
            raise refusal from None

    @app.get("/api/conversations/{conversation_id}/workspaces")
    async def conversation_workspaces(conversation_id: str, request: Request,
                                      who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """Empty for a Conversation that does not work on this server's folders."""
        folders = await himari(request).conversation_workspaces(scope_of(conversation_id, who))
        return {"items": [attached_view(f) for f in folders]}

    @app.post("/api/conversations/{conversation_id}/workspaces")
    async def attach_workspace(conversation_id: str, body: AttachBody, request: Request,
                               who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        if (body.workspace_id is None) == (body.path is None):
            raise ApiError(400, "invalid_request", "Give a registered workspace or a folder's path.")
        try:
            folders = await himari(request).attach_workspace(scope_of(conversation_id, who),
                                                             workspace_id=body.workspace_id, path=body.path,
                                                             label=body.label)
        except DataError as error:
            refusal = _workspace_refusal(error, "No such workspace.")  # a missing Conversation: NOT_AUTHORIZED, 404
            if refusal is None:
                raise
            raise refusal from None
        return {"items": [attached_view(f) for f in folders]}

    @app.delete("/api/conversations/{conversation_id}/workspaces/{binding_id}")
    async def detach_workspace(conversation_id: str, binding_id: str, request: Request,
                               who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        try:
            folders = await himari(request).detach_workspace(scope_of(conversation_id, who), binding_id)
        except DataError as error:
            if error.code is not DataErrorCode.NOT_FOUND:
                raise
            raise ApiError(404, "not_found", "No such folder in this conversation.") from None
        return {"items": [attached_view(f) for f in folders]}

    @app.put("/api/conversations/{conversation_id}/workspaces/working")
    async def set_working_workspace(conversation_id: str, body: WorkingBody, request: Request,
                                    who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        try:
            folders = await himari(request).set_working_workspace(scope_of(conversation_id, who), body.binding_id)
        except DataError as error:
            if error.code is not DataErrorCode.NOT_FOUND:
                raise
            raise ApiError(404, "not_found", "No such folder in this conversation.") from None
        return {"items": [attached_view(f) for f in folders]}

    # -- conversations -----------------------------------------------------------------------

    # -- the file explorer (V3-P75 §5): the owner's view of this server's files ----------

    @app.get("/api/machine/list")
    async def browse_folder(request: Request, path: str = "~", after: str | None = None,
                            who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        application = himari(request)
        explorer = application.explorer(who)
        return listing_view(await explorer.browse(path, after, await application.machine_workspaces(who)))

    @app.get("/api/machine/conversations")
    async def folder_conversations(request: Request, path: str,
                                   who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        return folder_conversations_view(await himari(request).folder_conversations(who, path))

    @app.post("/api/machine/conversations")
    async def open_folder_conversation(body: FolderConversationBody, request: Request, response: Response,
                                       who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """The folder's latest conversation, or a new one working in it (201)."""
        application = himari(request)
        conversation, created = await application.open_folder_conversation(who, body.path, new=body.new)
        if created:
            response.status_code = 201
        return {"conversation": summary_view(await application.data.read_conversation(scope_of(conversation, who))),
                "created": created}

    # -- the editor (V3-P75 §7): the owner's own writes, anywhere but hidden paths ------------

    @app.put("/api/machine/file")
    async def save_file(body: SaveFileBody, request: Request, who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        saved = await himari(request).explorer(who).save(body.path, body.content.encode("utf-8", "surrogatepass"),
                                                          [int(v) for v in body.expected])
        return {"path": saved.path, "identity": identity_out(saved.identity), "previous_kept": saved.previous_kept}

    @app.post("/api/machine/file", status_code=201)
    async def create_file(body: NewPathBody, request: Request, who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        path, identity = await himari(request).explorer(who).create_file(body.path)
        return {"path": path, "identity": identity_out(identity)}

    @app.post("/api/machine/file/install", status_code=201)
    async def recreate_file(body: RecreateFileBody, request: Request,
                            who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        path, identity = await himari(request).explorer(who).recreate(
            body.path, body.content.encode("utf-8", "surrogatepass"))
        return {"path": path, "identity": identity_out(identity) if identity is not None else None}

    @app.post("/api/machine/folder", status_code=201)
    async def make_folder(body: NewPathBody, request: Request, who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        return {"path": await himari(request).explorer(who).make_folder(body.path)}

    @app.post("/api/machine/file/delete")
    async def delete_machine_file(body: DeleteFileBody, request: Request,
                                  who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        path, kept = await himari(request).explorer(who).delete(body.path, [int(v) for v in body.expected])
        return {"path": path, "previous_kept": kept}

    @app.get("/api/machine/versions")
    async def list_versions(request: Request, path: str, who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """A file's previous versions (§7.4), newest first. ``path``: its real path, as saved."""
        versions = await himari(request).explorer(who).previous(path)
        return {"items": [{"id": v.version_id, "saved_at": _time(v.saved_at), "size": v.size, "reason": v.reason}
                          for v in versions]}

    @app.get("/api/machine/versions/deleted")
    async def deleted_files(request: Request, path: str, who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """Files deleted from the folder ``path`` whose content is kept (PR #95 review B3), latest first."""
        found = await himari(request).explorer(who).deleted(path)
        return {"items": [{"path": p, "name": posixpath.basename(p), "id": v.version_id, "saved_at": _time(v.saved_at),
                           "size": v.size} for p, v in found]}

    @app.post("/api/machine/versions/{version_id}/put_back")
    async def put_back_version(version_id: str, body: PutBackBody, request: Request,
                               who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """A PDF's previous version over the file (V3-P77 §6.3); the file as it was is kept first."""
        saved = await himari(request).explorer(who).put_back(body.path, version_id, [int(v) for v in body.expected])
        return {"path": saved.path, "identity": identity_out(saved.identity), "previous_kept": saved.previous_kept}

    @app.get("/api/machine/versions/{version_id}/download")
    async def download_version(version_id: str, request: Request, path: str,
                               who: PrincipalId = Depends(principal)) -> Response:
        """A previous version's bytes (a PDF's, to view: V3-P77 §6.2), as a download is: never rendered here."""
        real, size, fd = await himari(request).explorer(who).open_version(path, version_id)
        return _FileStream(os.fdopen(fd, "rb"), media_type="application/octet-stream", headers={
            "Content-Disposition": content_disposition(posixpath.basename(real) or "file"),
            "Content-Length": str(size),
            "Content-Security-Policy": "sandbox",
        })

    @app.get("/api/machine/versions/{version_id}")
    async def read_version(version_id: str, request: Request, path: str,
                           who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        return {"text": await himari(request).explorer(who).previous_text(path, version_id)}

    @app.post("/api/machine/versions/{version_id}/restore", status_code=201)
    async def restore_version(version_id: str, body: NewPathBody, request: Request,
                              who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """A deleted file put back in one step (V3-P76 §4.1): 409 changed if the name is taken, 422 unsupported
        where the file system cannot."""
        return {"path": await himari(request).explorer(who).restore(body.path, version_id)}

    @app.get("/api/machine/stat")
    async def stat_path(request: Request, path: str, who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        found = await himari(request).explorer(who).stat(path)
        return {**found, "identity": identity_out(found["identity"])}

    @app.get("/api/machine/file")
    async def show_file(request: Request, path: str, who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        return shown_file_view(await himari(request).explorer(who).show(path))

    @app.get("/api/machine/download")
    async def download_machine_file(request: Request, path: str,
                                    who: PrincipalId = Depends(principal)) -> Response:
        """An attachment of type octet-stream, sandboxed: never rendered on this origin (V3-P75 §8.2)."""
        real, size, identity, fd = await himari(request).explorer(who).open(path)
        return _FileStream(os.fdopen(fd, "rb"), media_type="application/octet-stream", headers={
            "Content-Disposition": content_disposition(posixpath.basename(real) or "file"),
            "Content-Length": str(size),
            "Content-Security-Policy": "sandbox",
            IDENTITY_HEADER: ",".join(identity_out(identity)),
        })

    @app.post(PDF_APPEND_PATH)
    async def append_pdf(request: Request, path: str, who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """V3-P77 §5.3, V3-P82 §3.6: ink saved into a PDF — the body is the bytes to add after it (an incremental
        update, at most 8 MiB), written at its end in place; ``X-Himari-Expected`` its identity as read. 409
        changed; 422 too_large over 64 MiB in all; a failed write's error tells the file's ``identity`` after it
        and ``partial`` when bytes of it were left."""
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/octet-stream":
            raise ApiError(415, "invalid_argument", "The ink comes as application/octet-stream.")
        expected = identity_in(request.headers.get(EXPECTED_HEADER, ""))
        saved = await himari(request).explorer(who).append_pdf(path, await request.body(), expected)
        return {"path": saved.path, "identity": identity_out(saved.identity)}

    @app.post(PDF_TRUNCATE_PATH)
    async def truncate_pdf(request: Request, path: str, to: int,
                           who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """V3-P82 §3.6: a PDF cut back to ``to``, just after one of its ``%%EOF`` — a save whose writing was cut
        short taken off its end. ``X-Himari-Expected``: its identity as read."""
        expected = identity_in(request.headers.get(EXPECTED_HEADER, ""))
        saved = await himari(request).explorer(who).truncate_pdf(path, expected, to)
        return {"path": saved.path, "identity": identity_out(saved.identity)}

    @app.get("/api/machine/image")
    async def show_image(request: Request, path: str, who: PrincipalId = Depends(principal)) -> Response:
        """The image sandbox's copy (V3-P67), inline: HIMARI's JPEG or PNG, never the file's own bytes."""
        made = await himari(request).explorer(who).image(path)
        return Response(made.data, media_type=made.media_type, headers={
            "Content-Disposition": "inline",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        })

    @app.get("/api/conversations")
    async def list_conversations(request: Request, who: PrincipalId = Depends(principal),
                                 limit: int = 50, offset: int = 0, trashed: bool = False) -> dict[str, Any]:
        """``trashed``: the trash instead, most recently trashed first."""
        if not 1 <= limit <= MAX_LIST_LIMIT or offset < 0:
            raise ApiError(400, "invalid_argument", f"limit must be 1..{MAX_LIST_LIMIT}, offset >= 0")
        application = himari(request)
        page = await application.data.list_conversations(who, limit=limit, offset=offset, trashed=trashed)
        return {"items": [trashed_view(s, application.trash_days) if trashed else summary_view(s) for s in page],
                "next_offset": offset + limit if len(page) == limit else None}  # may lead to an empty page

    @app.get("/api/conversations/activity")  # before /api/conversations/{conversation_id}, which would take it
    async def conversation_activity(request: Request, who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """V3-P80 §3.2: which Conversations work now and which wait for an answer. Not a read of any one of
        them: it never counts as the user being there (``seen``, V3-P62 §5)."""
        application = himari(request)
        return {"working": sorted(application.runner.running(who)),
                "waiting": sorted(await application.data.list_waiting_conversations(who))}

    @app.post("/api/conversations", status_code=201)
    async def create_conversation(body: CreateBody, request: Request,
                                  who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        application = himari(request)
        cid = await application.create_conversation(who, title=body.title)
        return summary_view(await application.data.read_conversation(scope_of(cid, who)))

    async def _detail(application: Application, scope: TrustedScope) -> dict[str, Any]:
        summary = await application.data.read_conversation(scope)  # authorizes first
        application.seen(scope)  # a client is reading (V3-P62 §5)
        waiting = await application.data.find_waiting_call(scope)
        question = None
        if waiting is not None:
            for record in await application.data.read_tool_executions(scope, waiting.run_id):
                if record.call == waiting and record.tool_id == ASK_USER_TOOL_ID:
                    asked = record.arguments.get("question")
                    question = {"call_id": waiting.call_id, "question": asked if isinstance(asked, str) else ""}
        job = application.runner.status(scope)
        # An accepted input (or finished Tool results) with no assistant
        # output after it — e.g. the server stopped before the job ran.
        last = await application.data.read_events(scope, after_sequence=summary.last_sequence - 1, limit=1) \
            if summary.last_sequence else ()
        continuable = (not job.running and question is None and bool(last)
                       and last[-1].kind.value in ("user_message", "tool_result", "assistant_tool_calls"))
        return {"conversation": summary_view(summary), "job": job_view(job), "waiting": question,
                "continuable": continuable}

    @app.get("/api/conversations/{conversation_id}")
    async def read_conversation(conversation_id: str, request: Request,
                                who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        return await _detail(himari(request), scope_of(conversation_id, who))

    @app.patch("/api/conversations/{conversation_id}")
    async def rename_conversation(conversation_id: str, body: RenameBody, request: Request,
                                  who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        application, scope = himari(request), scope_of(conversation_id, who)
        await application.data.rename_conversation(scope, body.title)
        return summary_view(await application.data.read_conversation(scope))

    @app.get("/api/conversations/{conversation_id}/delete-notice")
    async def read_delete_notice(conversation_id: str, request: Request,
                                 who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """What the delete confirmation tells (V3-P65 §6.1): the external
        servers this conversation called, whose own data stays."""
        return delete_notice_view(await himari(request).delete_notice(scope_of(conversation_id, who)))

    @app.post("/api/conversations/{conversation_id}/trash")
    async def trash_conversation(conversation_id: str, request: Request,
                                 who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """A running job is stopped first; 409 ``still_stopping`` when it does
        not stop in time (nothing was moved)."""
        application = himari(request)
        trashed_at = await application.trash(scope_of(conversation_id, who))
        return {"trashed_at": _time(trashed_at),
                "purge_after": _time(trashed_at + timedelta(days=application.trash_days))}

    @app.post("/api/conversations/{conversation_id}/restore", status_code=204)
    async def restore_conversation(conversation_id: str, request: Request,
                                   who: PrincipalId = Depends(principal)) -> None:
        try:
            await himari(request).restore(scope_of(conversation_id, who))
        except DataError as error:
            if error.code is not DataErrorCode.INVALID_STATE_TRANSITION:
                raise
            raise ApiError(409, "not_restorable", "Only a conversation in the trash can be restored.") from None

    @app.delete("/api/conversations/{conversation_id}", status_code=204)
    async def purge_conversation(conversation_id: str, request: Request,
                                 who: PrincipalId = Depends(principal)) -> None:
        """For good: only a conversation in the trash."""
        try:
            await himari(request).purge(scope_of(conversation_id, who))
        except DataError as error:
            if error.code is not DataErrorCode.INVALID_STATE_TRANSITION:
                raise
            raise ApiError(409, "not_in_trash", "Move the conversation to the trash first.") from None

    @app.get("/api/conversations/{conversation_id}/events")
    async def read_events(conversation_id: str, request: Request, who: PrincipalId = Depends(principal),
                          after: int = 0, limit: int = 200) -> dict[str, Any]:
        if after < 0 or not 1 <= limit <= MAX_EVENTS_PAGE:
            raise ApiError(400, "invalid_argument", f"after >= 0 and limit 1..{MAX_EVENTS_PAGE}")
        application, scope = himari(request), scope_of(conversation_id, who)
        events = await application.data.read_events(scope, after_sequence=after, limit=limit)
        application.seen(scope)  # a client is reading (V3-P62 §5): only after the read was authorized
        runs = [e.origin_run_id for e in events if isinstance(e.payload, AssistantToolCalls) and e.origin_run_id]
        runs += [e.payload.call.run_id for e in events if isinstance(e.payload, ToolResultRecord)]
        views = await application.view_flags(scope, runs) if runs and application.views_configured else {}
        writers = [e.origin_run_id for e in events
                   if isinstance(e.payload, (AssistantFinal, AssistantToolCalls)) and e.origin_run_id]
        models = await application.run_models(scope, writers) if writers else {}
        refs = [(FileId(b.file_id), b.version) for e in events
                for b in (e.payload.blocks if isinstance(e.payload, UserMessage)
                          else e.payload.model_result.blocks if isinstance(e.payload, ToolResultRecord) else ())
                if isinstance(b, AttachmentRef)]
        files: Files = ({(r.file_id, r.version): r for r in await application.attachment_records(scope, refs)}
                        if refs else {})
        calls = [e.payload.call for e in events if isinstance(e.payload, ToolResultRecord)]
        given = await application.call_files(scope, calls) if calls else {}
        presented = {call: list(records) for call, records in given.items()}
        results = [e.payload.call.run_id for e in events if isinstance(e.payload, ToolResultRecord)]
        memory = await application.memory_lines(scope, results) if results else {}
        return {"items": [event_view(e, views, models, files, presented, memory) for e in events],
                "last_sequence": events[-1].sequence if events else after}

    @app.get("/api/conversations/{conversation_id}/live")
    async def read_live(conversation_id: str, request: Request,
                        who: PrincipalId = Depends(principal)) -> StreamingResponse:
        """What the running job's model is writing, as it writes (V3-P73 §6.1)."""
        application, scope = himari(request), scope_of(conversation_id, who)
        await application.data.read_conversation(scope)  # authorizes first: another's is 404
        release = live_slots.take(who)  # checked and taken in one step (PR #83 review B1)
        if release is None:
            raise ApiError(429, "too_many_streams", "Too many live views are open; close one and try again.")
        return _LiveResponse(live.stream(application.runner, scope, closing), release,
                             media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    @app.get("/api/conversations/{conversation_id}/calls/{run_id}/{call_id}/app")
    async def read_app_view(conversation_id: str, run_id: str, call_id: str, request: Request, origin: str,
                            who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """A call's view (V3-P58 §6.1). ``origin``: the page's own origin,
        one of the allowed ones; the view is framed only by it."""
        if origin not in config.allowed_origins:
            raise ApiError(400, "invalid_argument", "origin must be this page's origin.")
        state = await himari(request).app_view(scope_of(conversation_id, who), CallRef(RunId(run_id), CallId(call_id)))
        url = None
        if state.resource is not None:
            grant = SandboxGrant(origin, state.csp.connect_domains, state.csp.resource_domains)
            url = f"{config.sandbox_origin(origin)}/sandbox?{signer.query(grant)}"
        return app_view_view(state, url)

    @app.get("/api/conversations/{conversation_id}/context")
    async def read_context(conversation_id: str, request: Request,
                           who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        # Composes the whole history like a Run would: fetched on demand, not polled.
        return context_view(await himari(request).context_status(scope_of(conversation_id, who)))

    @app.get("/api/conversations/{conversation_id}/model")
    async def read_model(conversation_id: str, request: Request,
                         who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """The model and effort the next turn uses, and the stored choice (V3-P64 §7)."""
        return model_choice_view(await himari(request).model_choice(scope_of(conversation_id, who)))

    @app.put("/api/conversations/{conversation_id}/model")
    async def set_model(conversation_id: str, body: ModelBody, request: Request,
                        who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """Saved at once, used from the next turn (one under way keeps its model)."""
        view = await himari(request).set_model(scope_of(conversation_id, who), body.model, body.effort)
        return model_choice_view(view)

    @app.get("/api/instructions")
    async def read_instructions(request: Request, who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """The instructions the user wrote for every conversation (V3-P69)."""
        return instructions_view(await himari(request).instructions(who))

    @app.put("/api/instructions")
    async def write_instructions(body: InstructionsBody, request: Request,
                                 who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """Saved at once, used from each conversation's next turn."""
        try:
            saved = await himari(request).set_instructions(who, body.text, body.revision)
        except DataError as error:
            if error.code not in INSTRUCTIONS_REFUSALS:
                raise
            status, code, message = INSTRUCTIONS_REFUSALS[error.code]
            raise ApiError(status, code, message) from None
        return instructions_view(saved)

    async def memory_titles(application: Application, who: PrincipalId,
                            notes: Iterable[Memory]) -> dict[ConversationId, tuple[str | None, str]]:
        titles: dict[ConversationId, tuple[str | None, str]] = {}
        for source in {n.source_conversation for n in notes if n.source_conversation is not None}:
            try:
                summary = await application.data.read_conversation(scope_of(source, who))
                titles[source] = (summary.title, summary.preview)
            except DataError as error:  # in the trash (a purged one took its notes with it)
                if error.code is not DataErrorCode.NOT_AUTHORIZED:
                    raise
        return titles

    def memory_refused(error: DataError) -> ApiError:
        if error.code not in MEMORY_REFUSALS:
            raise error
        status, code, message = MEMORY_REFUSALS[error.code]
        return ApiError(status, code, message)

    @app.get("/api/memories")
    async def read_memories(request: Request, who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """The notes HIMARI keeps about the user (V3-P71 §7.2)."""
        application = himari(request)
        notes = await application.memories(who)
        titles = await memory_titles(application, who, notes)
        return {"memories": [memory_view(n, titles) for n in notes], "max_count": MAX_MEMORIES,
                "max_chars": MAX_MEMORY_CHARS, "enabled": application.memory_enabled}

    @app.post("/api/memories", status_code=201)
    async def add_memory(body: MemoryBody, request: Request, who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """A note the user writes; used from each conversation's next turn."""
        try:
            note = await himari(request).add_memory(who, MemoryCategory(body.category), body.text)
        except DataError as error:
            raise memory_refused(error) from None
        return memory_view(note, {})

    @app.put("/api/memories/{memory_id}")
    async def edit_memory(memory_id: int, body: MemoryEditBody, request: Request,
                          who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        application = himari(request)
        try:
            note = await application.edit_memory(who, memory_id, MemoryCategory(body.category), body.text,
                                                 body.revision)
        except DataError as error:
            raise memory_refused(error) from None
        return memory_view(note, await memory_titles(application, who, (note,)))

    @app.delete("/api/memories/{memory_id}", status_code=204)
    async def delete_memory(memory_id: int, request: Request, who: PrincipalId = Depends(principal),
                            revision: int | None = None) -> None:
        """Gone at once. Conversations that already used it still show it."""
        try:
            await himari(request).delete_memory(who, memory_id, revision)
        except DataError as error:
            raise memory_refused(error) from None

    @app.post("/api/conversations/{conversation_id}/calls/{run_id}/{call_id}/memory-undo")
    async def undo_memory(conversation_id: str, run_id: str, call_id: str, request: Request,
                          who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """Undo what one memory_save call did (V3-P71 §7.1): the server reads
        what to put back from the call's own record. ``memory``: the note as
        restored, or null when the call's new note was deleted."""
        application = himari(request)
        try:
            note = await application.undo_memory(scope_of(conversation_id, who), CallRef(RunId(run_id), CallId(call_id)))
        except DataError as error:
            if error.code is DataErrorCode.NOT_FOUND:
                raise ApiError(404, "not_found", "Nothing to undo: the note or the conversation it came from is "
                                                 "gone.") from None
            raise memory_refused(error) from None
        return {"memory": None if note is None else memory_view(note, await memory_titles(application, who, (note,)))}

    @app.get("/api/conversations/{conversation_id}/tools")
    async def read_tool_sources(conversation_id: str, request: Request,
                                who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """The configured MCP servers and whether this conversation uses them."""
        views = await himari(request).tool_sources(scope_of(conversation_id, who))
        return {"sources": [tool_source_view(v) for v in views]}

    @app.put("/api/conversations/{conversation_id}/tools/{source}")
    async def set_tool_source(conversation_id: str, source: str, body: ToolSourceBody, request: Request,
                              who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        application, scope = himari(request), scope_of(conversation_id, who)
        await application.data.read_conversation(scope)  # 404 for a foreign id before anything else
        await application.set_tool_source(scope, source, body.enabled)
        return {"sources": [tool_source_view(v) for v in await application.tool_sources(scope)]}

    @app.get("/api/conversations/{conversation_id}/prompts")
    async def read_prompts(conversation_id: str, request: Request,
                           who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """The prompts of the servers on in this conversation (V3-P60 §4)."""
        return {"prompts": [prompt_view(v) for v in await himari(request).prompts(scope_of(conversation_id, who))]}

    @app.post("/api/conversations/{conversation_id}/prompts/expand")
    async def expand_prompt(conversation_id: str, body: PromptExpandBody, request: Request,
                            who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """The prompt as text for the composer; nothing is sent: the user
        reviews it and sends it as an ordinary message."""
        text = await himari(request).expand_prompt(scope_of(conversation_id, who), body.source, body.name,
                                                   body.arguments)
        return {"text": text}

    # -- the review of the agent's file changes (V3-P76 §5, §6) ------------------------------

    @app.get("/api/conversations/{conversation_id}/changes")
    async def file_changes(conversation_id: str, request: Request,
                           who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        application, scope = himari(request), scope_of(conversation_id, who)
        changes = await application.file_changes(scope)
        return changes_view(changes, application.runner.status(scope).running)

    @app.get("/api/conversations/{conversation_id}/changes/diff")
    async def file_change_diff(conversation_id: str, path: str, request: Request,
                               who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        return change_diff_view(await himari(request).file_change_diff(scope_of(conversation_id, who), path))

    @app.post("/api/conversations/{conversation_id}/changes/accept")
    async def accept_file_changes(conversation_id: str, body: AcceptChangesBody, request: Request,
                                  who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        if body.all == bool(body.paths):
            raise ApiError(400, "invalid_argument", "Give the paths to accept, or all.")
        await himari(request).accept_file_changes(scope_of(conversation_id, who), None if body.all else body.paths)
        return {"accepted": True}

    @app.post("/api/conversations/{conversation_id}/changes/revert")
    async def revert_file_change(conversation_id: str, body: RevertChangeBody, request: Request,
                                 who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        seen = None
        if body.seen is not None:
            if body.seen.exists != (body.seen.identity is not None):
                raise ApiError(400, "invalid_argument", "A file seen is there with its identity, or not there.")
            seen = tuple(int(v) for v in body.seen.identity) if body.seen.identity is not None else ()
        await himari(request).revert_file_change(scope_of(conversation_id, who), body.path, seen)
        return {"reverted": True}

    @app.post("/api/conversations/{conversation_id}/changes/revert-all")
    async def revert_all_file_changes(conversation_id: str, request: Request,
                                      who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """Each shown file in turn; the ones left (changed since, refused, or
        not revertible) and why, and how many hidden ones were left."""
        done = await himari(request).revert_all_file_changes(scope_of(conversation_id, who))
        return {"left": [{"path": path, "code": why.category if isinstance(why, ExplorerRefused) else "changed",
                          "message": str(why)} for path, why in done.left],
                "hidden": done.hidden}

    @app.post("/api/conversations/{conversation_id}/messages", status_code=202)
    async def send_message(conversation_id: str, body: MessageBody, request: Request,
                           who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        text = body.text.strip()
        if len(body.attachments) > MAX_ATTACHMENTS:
            raise ApiError(400, "too_many_attachments", f"A message carries at most {MAX_ATTACHMENTS} files.")
        if (not text and not body.attachments and not body.references) or len(text) > MAX_MESSAGE_CHARS:
            raise ApiError(400, "invalid_argument", f"A message is 1..{MAX_MESSAGE_CHARS} characters, or files.")
        application, scope = himari(request), scope_of(conversation_id, who)
        references = [ReferenceRequest(r.path, tuple(int(v) for v in r.identity), r.start, r.end, r.sha256)
                      for r in body.references]
        return job_view(await application.send(scope, text, [FileId(f) for f in body.attachments], references))

    # -- files (V3-P66 §9) ---------------------------------------------------------------------

    @app.post("/api/conversations/{conversation_id}/uploads", status_code=201)
    async def upload_file(conversation_id: str, request: Request, name: str = "",
                          who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """The body is the file. Received as it streams, held to the file
        limit (not the general body limit); refused at once when its declared
        length is over."""
        application, scope = himari(request), scope_of(conversation_id, who)
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > application.file_max_bytes:
            raise ApiError(413, "file_too_large", "The file is larger than this server takes.")

        async def body() -> AsyncIterator[bytes]:
            async for chunk in request.stream():
                if chunk:
                    yield chunk

        try:
            record = await application.upload(scope, name, request.headers.get("content-type"), body())
        except DataError as error:
            if error.code is DataErrorCode.BLOB_MISSING:  # swept while it stalled (§8.2)
                raise ApiError(503, "upload_interrupted", "The upload was interrupted; upload it again.") from None
            raise
        # §5.2 (review C2): without commands the model reads text files only.
        return {**file_view(record), "commands": application.current_sandbox_state is SandboxState.ENFORCED}

    @app.get("/api/conversations/{conversation_id}/files")
    async def list_files(conversation_id: str, request: Request, who: PrincipalId = Depends(principal),
                         limit: int = 50, offset: int = 0) -> dict[str, Any]:
        if not 1 <= limit <= MAX_LIST_LIMIT or offset < 0:
            raise ApiError(400, "invalid_argument", f"limit must be 1..{MAX_LIST_LIMIT}, offset >= 0")
        page = await himari(request).files(scope_of(conversation_id, who), limit=limit, offset=offset)
        return {"items": [file_view(f) for f in page], "next_offset": offset + limit if len(page) == limit else None}

    @app.get("/api/conversations/{conversation_id}/files/{file_id}/{version}")
    async def download_file(conversation_id: str, file_id: str, request: Request,
                            version: int = Path(ge=1, le=2**31),  # SQLite's integers (PR #60 review C2)
                            who: PrincipalId = Depends(principal)) -> Response:
        """Always an attachment of type octet-stream, sandboxed (§7.2): an
        HTML or SVG file never opens on this origin."""
        record, handle = await himari(request).download(scope_of(conversation_id, who), FileId(file_id), version)
        return _FileStream(handle, media_type="application/octet-stream", headers={
            "Content-Disposition": content_disposition(record.name),
            "Content-Length": str(record.size),
            "Content-Security-Policy": "sandbox",
        })

    @app.get("/api/conversations/{conversation_id}/files/{file_id}/{version}/view")
    async def preview_file(conversation_id: str, file_id: str, request: Request,
                           version: int = Path(ge=1, le=2**31),
                           who: PrincipalId = Depends(principal)) -> Response:
        """V3-P67 §8: an image's model copy, shown inline in an ``<img>``. Its
        type is the copy's, made by HIMARI from bytes judged an image; the
        browser may not guess another (nosniff) and nothing in it can run
        (CSP). 404 unless the file is an image with a copy; 410 once it expired."""
        view, handle = await himari(request).preview(scope_of(conversation_id, who), FileId(file_id), version)
        return _FileStream(handle, media_type=view.media_type, headers={
            "Content-Disposition": "inline",
            "Content-Length": str(view.blob.size),
            "Content-Security-Policy": "default-src 'none'; sandbox",  # nosniff and no-store: the middleware
        })

    @app.post("/api/conversations/{conversation_id}/resume", status_code=202)
    async def resume(conversation_id: str, request: Request, who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        application, scope = himari(request), scope_of(conversation_id, who)
        await application.data.read_conversation(scope)
        return job_view(await application.runner.submit(scope, None))

    @app.post("/api/conversations/{conversation_id}/pause", status_code=202)
    async def pause(conversation_id: str, request: Request, who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """Stop the running job at the next safe boundary (V3-P55); nothing if idle."""
        application, scope = himari(request), scope_of(conversation_id, who)
        await application.data.read_conversation(scope)
        return job_view(application.runner.request_pause(scope))

    @app.post("/api/conversations/{conversation_id}/cancel", status_code=202)
    async def cancel(conversation_id: str, request: Request, who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """Stop the running job now (V3-P55); nothing if idle."""
        application, scope = himari(request), scope_of(conversation_id, who)
        await application.data.read_conversation(scope)
        return job_view(application.runner.cancel(scope))

    @app.post("/api/conversations/{conversation_id}/dismiss", status_code=202)
    async def dismiss(conversation_id: str, request: Request, who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """Close what was left waiting — a question, or never-started steps
        of a paused or interrupted turn (V3-P55). Busy while a job runs."""
        application, scope = himari(request), scope_of(conversation_id, who)
        await application.data.read_conversation(scope)
        return job_view(await application.runner.submit_cancel(scope))

    @app.post("/api/conversations/{conversation_id}/compact", status_code=202)
    async def compact(conversation_id: str, request: Request, who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """Summarize the older history now, whatever its size (no Run)."""
        application, scope = himari(request), scope_of(conversation_id, who)
        await application.data.read_conversation(scope)
        return job_view(await application.runner.submit_compaction(scope))

    # -- static UI ----------------------------------------------------------------------------

    static = resources.files("himari.interfaces.web") / "static"

    @app.get("/")
    async def index() -> Response:
        return Response((static / "index.html").read_bytes(), media_type="text/html; charset=utf-8")

    # V3-P79 §3.1: each surface's address is the same page, which reads it; the data stays behind the API.
    for surface in SURFACE_PATHS:
        app.add_api_route(surface, index, methods=["GET"], include_in_schema=False)

    @app.get("/c/{conversation_id}", include_in_schema=False)
    async def conversation_page(conversation_id: str) -> Response:
        if not PAGE_ID.fullmatch(conversation_id):
            raise ApiError(404, "not_found", "Not available.")
        return await index()

    @app.get("/assets/{name:path}")
    async def asset(name: str) -> Response:
        media = STATIC_FILES.get(name)  # only the listed names: never a path into the package
        if media is None:
            raise ApiError(404, "not_found", "Not available.")
        charset = "; charset=utf-8" if media.startswith("text/") else ""
        return Response((static / name).read_bytes(), media_type=f"{media}{charset}")

    # Outer → inner: headers, Host, Origin, body size, routes.
    app.add_middleware(BodyLimitMiddleware, exempt=lambda method, path: method == "POST" and bool(UPLOAD_PATH.match(path)),
                       larger=lambda method, path: SAVE_BODY_BYTES if (method, path) in SAVE_ROUTES
                       else PDF_APPEND_MAX_BYTES if method == "POST" and path == PDF_APPEND_PATH else None)
    app.add_middleware(OriginGuardMiddleware, allowed_origins=config.allowed_origins)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=sorted(config.allowed_hosts))
    app.add_middleware(SecurityHeadersMiddleware, csp=page_csp(config.sandbox_origins))
    return app
