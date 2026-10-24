"""The durable P0 application the Web server and CLI are built on.

One data root, private to the server user::

    <data_root>/                 0700
        himari.db (+ .lock)      SQLite Data (V3-P42), 0600
        workspaces/<ws>/         Inside Workspaces, one per principal (0700)
        control/operator.secret  the local operator's credential (0600)
        mcp.json                 optional: MCP servers to connect (0600, V3-P53)

``Application.open`` checks the layout, opens the store (single writer),
starts the Sandbox when asked and the host supports it, and composes the
runtime with a background runner. Interfaces talk only to this object.

MCP servers in ``mcp.json`` are connected at startup; one that cannot be
reached is left out until a restart (``tool_sources`` says so). Which
servers a Conversation uses follows each server's default unless the
Conversation chose otherwise (``set_tool_source``).

The operator offers one or more models (V3-P64 §3); each Conversation uses
the default one unless it chose another (``set_model``), from its next turn.

The trash (V3-P65 §7): ``trash`` closes the Conversation to new work and
stops a running job before moving it; ``restore`` brings it back;
``purge`` deletes it for good — its own folder first, then every Data row.
These three run one at a time per Conversation (§7.3). Conversations
trashed more than ``trash_days`` ago are purged at startup and every hour.

Files (V3-P66): ``upload`` stores a file and stages it; ``send`` with
attachments places them in the Conversation's ``uploads/`` folder and
records the message (both under the lifecycle lock, §5.1); ``download``
opens a file's stored bytes. The hourly cleanup also drops stale staged
uploads and sweeps the file store (§8.2, §8.5).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import os
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO

import httpx

from himari.bootstrap import DEFAULT_INSTRUCTIONS, Runtime, build_runtime
from himari.bootstrap.operator import check_private_dir, ensure_operator_secret, ensure_private_dir
from himari.common import (
    AttachmentRef,
    CallId,
    CallRef,
    ConversationId,
    FileId,
    JsonValue,
    PrincipalId,
    RunId,
    TextBlock,
    TrustedScope,
    WorkspaceId,
)
from himari.control.api import ContextStatus, ControlError, ControlErrorCode, JobStatus, ModelCatalog, ModelOffer, ResolvedModel
from himari.control.driver import NO_PROGRESS_RUNS
from himari.control.runner import SEEN_SECONDS, BackgroundRunner
from himari.data.api import (
    DEFAULT_FILE_MAX_BYTES,
    DEFAULT_KEEP_DAYS,
    DEFAULT_STORE_MAX_BYTES,
    MAX_RAW_NAME_BYTES,
    MAX_STAGED_UPLOADS,
    STAGED_KEEP,
    DataError,
    DataErrorCode,
    FileRecord,
    ImageView,
    Instructions,
    InstructionScope,
    Memory,
    MemoryCategory,
    MemoryOrigin,
    ViewMaker,
    ViewState,
    InitialActivation,
    ModelSetting,
    ToolExecutionRecord,
    ToolExecutionStatus,
    UiCsp,
    UiResource,
    UserMessage,
    clean_file_name,
    clean_media_type,
    view_state,
)
from himari.data.sqlite import SqliteData, SqliteStore
from himari.documents import TMP_BYTES, DocumentReader, Toolkit, ToolkitRefused, open_toolkit, worker_roots
from himari.environment.inside import INSIDE, InsideEnvironment, InsideFiles
from himari.imaging import ImageViews, runtime_roots
# For the interfaces (V3-P66 §9): how placing an upload can be refused.
from himari.environment.inside import UploadsFolderUnusable as UploadsFolderUnusable
from himari.environment.inside import WorkspaceBusy as WorkspaceBusy
from himari.sandbox import identity
from himari.sandbox.api import SandboxAdmissionError, SandboxState, ServerProfile
from himari.sandbox.backend import SandboxBackend, discover_capability, supports_tmp_size
from himari.sandbox.server import ServerSandbox
from himari.mcp.config import CONFIG_NAME, McpConfig, McpStdioConfig, McpUiPolicy, load_config
from himari.mcp.jsonrpc import McpError
from himari.mcp.prompts import PromptContentError, prompt_text
from himari.mcp.stdio import ProcessLauncher
from himari.mcp.tools import LauncherFor, McpConnection, PromptArgument, SourceStatus, connect
from himari.prompt.api import (
    ATTACHMENT_NOTE,
    DOCUMENTS_READ_NOTE,
    PROGRESS_NOTE,
    TOOL_IMAGES_NOTE,
    ContextBudget,
    SourceNote,
    documents_script_note,
)
from himari.provider.api import ProviderAdapter
from himari.tool.api import ToolHandler
from himari.tool.memory import MEMORY_FORGET, MEMORY_SAVE
from himari.tool.schema import check_tool_input_schema

DATABASE_NAME = "himari.db"
DEFAULT_TRASH_DAYS = 30  # V3-P65 Q1 (the owner's choice, 2026-10-21)
CLEANUP_SECONDS = 3600.0  # how often trash past its days is purged
TRASH_STOP_SECONDS = 30.0  # how long a running job may take to stop before a trash is refused (§7.1)
BUILT_IN_NAMESPACES = frozenset({"inside"})  # Tool ids that are HIMARI's own, not an external server's
PLACE_LOCK_SECONDS = 60.0  # how long sending waits for the Workspace write lock (V3-P66 review N4)

log = logging.getLogger(__name__)
# One turn's guards (V3-P62): no Run limit by default; a token budget, a
# no-progress stop, and a pause when no client reads the Conversation.
DEFAULT_TURN_BUDGET = 3_000_000  # tokens per drive (O1: the owner's choice, 2026-10-16)
DEFAULT_UNATTENDED_RUNS = 20  # Runs without a client read in SEEN_SECONDS (O2)


@dataclass(frozen=True)
class TurnSettings:
    """How a turn is guarded (V3-P62), as this server runs it."""

    turn_budget: int | None  # None: no token budget (then max_runs is set)
    unattended_runs: int | None  # None: never paused for want of a client
    max_runs: int | None  # None: no Run limit
    no_progress_runs: int = NO_PROGRESS_RUNS
    seen_seconds: float = SEEN_SECONDS


# Default request budget: the model's window, capped so that a long
# Conversation is compacted before each request costs a whole large window.
DEFAULT_CONTEXT_TOKENS = 200_000
UNKNOWN_WINDOW_TOKENS = 128_000  # a model whose window the Provider does not declare
DEFAULT_OUTPUT_TOKENS = 32_000  # room kept for an answer when the model declares no maximum


def input_ceiling(window: int | None, max_output: int | None) -> int | None:
    """The largest input a Run may send: the model's window less a reserve
    for its answer — the model's declared maximum output, or
    ``DEFAULT_OUTPUT_TOKENS`` — but never less than half the window. None
    when the window is unknown (the budget is then the limit).

    A conservative input limit, not a promise of the whole maximum output:
    a reserve capped at half the window leaves less than that, and the
    input is an estimate. No output limit is sent, so HIMARI never cuts an
    answer short; the model may still stop at its own limit."""
    if (window is not None and window <= 0) or (max_output is not None and max_output <= 0):
        raise ValueError(f"the Provider declares an invalid window ({window}) or maximum output ({max_output})")
    if window is None:
        return None
    reserve = DEFAULT_OUTPUT_TOKENS if max_output is None else max_output
    return window - min(reserve, window // 2)


def context_limit(ceiling: int | None, configured: int | None) -> int:
    """The token budget of one request: ``configured`` if given (it may not
    exceed a known input ceiling), else the ceiling capped at the default."""
    if configured is not None:
        if configured <= 0:
            raise ValueError("the context budget must be positive")
        if ceiling is not None and configured > ceiling:
            raise ValueError(f"the context budget {configured} exceeds the model's input limit of {ceiling} tokens"
                             " (its window less the room kept for an answer)")
        return configured
    return min(ceiling, DEFAULT_CONTEXT_TOKENS) if ceiling is not None else UNKNOWN_WINDOW_TOKENS


def workspace_for(principal: PrincipalId) -> WorkspaceId:
    """A principal's Inside Workspace id: stable, and safe as a folder name
    whatever characters the principal id has."""
    return WorkspaceId("ws_" + hashlib.sha256(principal.encode()).hexdigest()[:24])


def check_tool_sources(data_root: str | os.PathLike[str], environ: Mapping[str, str]) -> None:
    """Fails (ValueError) on an ``mcp.json`` that ``Application.open`` would
    refuse, so a server can report it before starting."""
    load_config(os.path.join(os.path.abspath(data_root), CONFIG_NAME), environ)


def tool_views_configured(data_root: str | os.PathLike[str], environ: Mapping[str, str]) -> bool:
    """Whether ``mcp.json`` shows some server's views (V3-P58): the Web
    server starts its sandbox origin only then. Fails like check_tool_sources."""
    config = load_config(os.path.join(os.path.abspath(data_root), CONFIG_NAME), environ)
    return any(server.ui is not None for server in config.servers)


class UnknownToolSource(LookupError):
    """No configured External Tool source has this name."""


class PromptRefused(LookupError):
    """A server prompt was not expanded (V3-P60 §4); ``message`` is for the
    user. ``code``: ``unknown_prompt``, ``source_disabled`` (off in this
    Conversation), ``invalid_arguments``, ``unsupported_content`` (it cannot
    be given as text) or ``server_error``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ConversationStillStopping(RuntimeError):
    """V3-P65 §7.1: the running job did not stop in time; nothing was moved."""


class FilesUnavailable(RuntimeError):
    """V3-P66 §5.2: this Conversation (or server) has no place for uploads."""


class FileNameRefused(ValueError):
    """V3-P66 §5.3: no name, or a name longer than MAX_RAW_NAME_BYTES."""


@dataclass(frozen=True)
class DeleteNotice:
    """What the delete confirmation tells (V3-P65 §6.1)."""

    servers: tuple[str, ...]  # external servers this Conversation called, by name
    unconfigured: tuple[str, ...]  # of them, the ones no longer configured
    running: bool  # a job runs now: it is stopped first
    trash_days: int
    memories: int = 0  # notes its calls wrote: deleted with it for good (V3-P71 D5)


@dataclass(frozen=True)
class MemoryLine:
    """A memory call as its Conversation shows it (V3-P71 §7.1)."""

    action: str  # "saved", "updated", "forgot" or "already" (the note was there)
    memory_id: int
    text: str
    undo: bool  # the note is still as the call left it, so an Undo is offered


def _memory_line(record: ToolExecutionRecord, notes: Mapping[int, Memory]) -> MemoryLine | None:
    """From the call's own record (raw_result, §15); None for a call that
    changed nothing the user should see: failed, or not a memory call."""
    raw = record.raw_result
    if (record.tool_id not in (MEMORY_SAVE, MEMORY_FORGET) or record.status is not ToolExecutionStatus.SUCCEEDED
            or not isinstance(raw, Mapping)):
        return None
    written = raw.get("memory")
    memory_id, text = (written.get("id"), written.get("text")) if isinstance(written, Mapping) else (None, None)
    if not isinstance(memory_id, int) or not isinstance(text, str):
        return None
    assert isinstance(written, Mapping)
    if record.tool_id == MEMORY_FORGET:
        return MemoryLine("forgot", memory_id, text, False)
    if raw.get("unchanged"):
        return MemoryLine("already", memory_id, text, False)
    current = notes.get(memory_id)
    undo = current is not None and current.revision == written.get("revision")
    return MemoryLine("saved" if raw.get("previous") is None else "updated", memory_id, text, undo)


def _memory_before(previous: object) -> Memory | None:
    """The note as it was before a rewrite, from raw_result.previous; None
    when the record does not say all of it (§7.1: origin and source too)."""
    if not isinstance(previous, Mapping):
        return None
    memory_id, category, text, origin = (previous.get(k) for k in ("id", "category", "text", "origin"))
    source = previous.get("source")
    if not (isinstance(memory_id, int) and isinstance(text, str) and category in {c.value for c in MemoryCategory}
            and origin in {o.value for o in MemoryOrigin} and (source is None or isinstance(source, Mapping))):
        return None
    conversation = call = None
    if isinstance(source, Mapping):
        conversation, run, call_id = source.get("conversation"), source.get("run"), source.get("call")
        if not isinstance(conversation, str):
            return None
        call = CallRef(RunId(run), CallId(call_id)) if isinstance(run, str) and isinstance(call_id, str) else None
    epoch = datetime.fromtimestamp(0, UTC)  # restore_memory keeps the note's own times
    return Memory(memory_id, MemoryCategory(category), text, MemoryOrigin(origin),
                  None if conversation is None else ConversationId(conversation), call, 0, epoch, epoch)


class ModelRefused(LookupError):
    """A model choice was not recorded (V3-P64 §4.3); ``message`` is for the
    user. ``code``: ``unknown_model`` (not offered), ``invalid_effort`` (not
    one the model declares) or ``invalid_argument``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# The order efforts are listed in; any other a Provider declares follows, sorted.
EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


def ordered_efforts(efforts: Iterable[str]) -> tuple[str, ...]:
    known = [e for e in EFFORT_ORDER if e in efforts]
    return (*known, *sorted(set(efforts) - set(EFFORT_ORDER)))


@dataclass(frozen=True)
class ModelView:
    """One offered model, as a client lists it."""

    model_id: str
    default_effort: str | None
    efforts: tuple[str, ...]
    window_known: bool  # the Provider declares its window (an unknown model's is not checked)
    is_default: bool
    images: bool = False  # the Provider sends it images (V3-P67 §7.1)


@dataclass(frozen=True)
class ModelChoiceView:
    """What a Conversation's next turn uses, and what it chose (V3-P64 §7)."""

    model_id: str
    effort: str | None
    chosen: ModelSetting | None  # as stored; None: the default model
    fallback: str | None  # why the choice is not used as made


MAX_PROMPT_ARGUMENT_CHARS = 2_000
INVALID_PARAMS = -32602


@dataclass(frozen=True)
class PromptView:
    """One prompt a server on in this Conversation offers, as the user picks it."""

    source: str
    name: str
    title: str
    description: str
    arguments: tuple[PromptArgument, ...]


class AppViewRefused(LookupError):
    """A call has no view to show here (V3-P58). ``code``: ``no_app_view``
    (none stored and none to show), ``source_disabled`` (this Conversation
    turned the source off), ``app_view_disabled`` (the operator does not show
    this server's views, or the server is no longer configured)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# How a view whose call ended without one is closed (V3-P58 §3.5): only a
# call that did not run is "cancelled"; one that ran, or may have, is not.
_ENDED_OUTCOME = {
    ToolExecutionStatus.CANCELLED: "not_run",
    ToolExecutionStatus.DENIED: "not_run",
    ToolExecutionStatus.SUCCEEDED: "view_unavailable",
    ToolExecutionStatus.FAILED: "view_unavailable",
    ToolExecutionStatus.UNKNOWN: "unknown",
}
_RUNNING = frozenset({ToolExecutionStatus.PENDING, ToolExecutionStatus.EXECUTION_CLAIMED})
MAX_REASON_CHARS = 300


@dataclass(frozen=True)
class AppViewState:
    """What a call's view shows now (V3-P58 §6.1).

    ``state``: ``running`` (the call has not ended; the view read at connect),
    ``done`` (the view stored with the result) or ``ended`` (it ended without
    one: ``outcome`` says how the view is closed, ``reason`` why; no resource).
    ``csp`` is what the view may reach now: what its server declared that the
    operator also allows."""

    state: str
    call: CallRef
    source: str
    tool_id: str
    tool: Mapping[str, object] | None  # as the server listed it, while it is registered
    arguments: Mapping[str, JsonValue]
    status: ToolExecutionStatus
    resource: UiResource | None = None
    result: JsonValue = None
    csp: UiCsp = field(default_factory=UiCsp)
    outcome: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class ToolSourceView:
    """One configured External Tool source as a Conversation sees it."""

    source: str
    enabled: bool  # in this Conversation, now
    enabled_by_default: bool
    choice: bool | None  # this Conversation's own choice, if any
    available: bool  # connected at startup
    tool_ids: tuple[str, ...]
    problems: tuple[str, ...]


async def _image_views(root: Path, sandbox: bool) -> ImageViews:
    """V3-P67 §3.1 (review B1): model copies only inside the image profile —
    the server sandbox with the Python runtime read-only and nothing
    writable. Not ENFORCED, or the check fails: no copies at all."""
    if not sandbox:
        return ImageViews(None)
    server = ServerSandbox.discover([str(root)])
    state = await server.start()
    if state is not SandboxState.ENFORCED:
        log.warning("images cannot be prepared on this server: the image sandbox is %s", state.value)
        return ImageViews(None)
    home = Path.home().resolve()
    broad = [r for r in runtime_roots() if home.is_relative_to(r)]
    if broad:  # Python installed with the home directory (or more) as its prefix (re-review N5)
        log.warning("images cannot be prepared on this server: the Python runtime %s would show the home "
                    "directory to the image sandbox", broad[0])
        return ImageViews(None)
    try:
        launcher = server.launcher(ServerProfile(read_only=runtime_roots()))
    except Exception as error:  # noqa: BLE001 — a profile the sandbox refuses: no copies
        log.warning("images cannot be prepared on this server: %s", error)
        return ImageViews(None)
    views = ImageViews(launcher)
    await views.check()
    return views


# The toolkit's dist names and what a script imports (V3-P70 §7).
_MODULES = {"pypdf": "pypdf", "pypdfium2": "pypdfium2", "python-docx": "docx", "openpyxl": "openpyxl",
            "python-pptx": "pptx", "reportlab": "reportlab", "pillow": "PIL"}


async def _documents(root: Path, sandbox: bool, documents_dir: str | None,
                     owners: frozenset[int]) -> tuple[Toolkit | None, DocumentReader | None, str]:
    """V3-P70 §6.4 steps 1-2: the toolkit checked on its descriptor, then
    the reader in its own server sandbox, checked with the probe documents.
    The toolkit comes back only with a working reader (run_command binds it
    only then); the text says why there is none."""
    if not documents_dir:
        return None, None, "HIMARI_DOCUMENTS_DIR is not set"
    if not sandbox:
        return None, None, "the sandbox is off"
    try:
        toolkit = open_toolkit(documents_dir, owners=owners)
    except ToolkitRefused as refused:
        return None, None, str(refused)
    try:
        try:  # apart from HIMARI's data, by what the descriptor is (§6.2, review C3)
            await asyncio.to_thread(identity.check_trees, [identity.BoundTree(toolkit.fd, False)], [str(root)])
        except SandboxAdmissionError:
            raise ToolkitRefused("the toolkit overlaps HIMARI's data") from None
        capability = discover_capability()
        if capability is None:
            raise ToolkitRefused("this host cannot run sandboxes")
        if not await asyncio.to_thread(supports_tmp_size, capability):
            raise ToolkitRefused("this bwrap cannot size /tmp (--size, bwrap 0.9 or later)")
        server = ServerSandbox(capability, [str(root)])  # its own: an image sandbox failure does not turn this off
        state = await server.start()
        if state is not SandboxState.ENFORCED:
            raise ToolkitRefused(f"the documents sandbox is {state.value}")
        try:
            launcher = server.launcher(ServerProfile(read_only=worker_roots(toolkit), tmp_bytes=TMP_BYTES))
        except Exception as error:  # noqa: BLE001 — a profile the sandbox refuses
            raise ToolkitRefused(str(error)) from None
        reader = DocumentReader(launcher, toolkit)
        why = await reader.check()
        if why is not None:
            raise ToolkitRefused(f"the startup check failed ({why})")
    except ToolkitRefused as refused:
        toolkit.close()
        return None, None, str(refused)
    except BaseException:  # nothing else may cost the startup, nor leak the descriptor (review N3)
        toolkit.close()
        raise
    return toolkit, reader, "on"


def _documents_notes(reader: DocumentReader | None, toolkit: Toolkit | None, scripts: bool) -> tuple[str, ...]:
    """V3-P70 §6.4 step 5: each sentence only while its tool can do it."""
    if reader is None or toolkit is None:
        return ()
    notes = [DOCUMENTS_READ_NOTE]
    if scripts:
        modules = [_MODULES[name] for name in _MODULES if name in reader.versions]
        fonts = toolkit.manifest.get("fonts")
        fonts = [f for f in fonts if isinstance(f, str) and re.fullmatch(r"[\w.-]{1,60}\.ttf", f)] if isinstance(
            fonts, list) else []
        notes.append(documents_script_note(modules, fonts))
    return tuple(notes)


async def _server_launchers(config: McpConfig, root: Path) -> LauncherFor | None:
    """The server sandbox for stdio servers (V3-P59 §4), made only when one is
    configured. HIMARI's data root — the database, the operator secret,
    mcp.json and every Workspace — is the root it protects, fixed for every
    launch. Not ENFORCED: no stdio server starts (the reason is shown)."""
    if not any(server.stdio is not None for server in config.servers):
        return None
    sandbox = ServerSandbox.discover([str(root)])
    state = await sandbox.start()

    def launcher_for(source: str, stdio: McpStdioConfig) -> ProcessLauncher:
        if state is not SandboxState.ENFORCED:
            raise RuntimeError(f"stdio servers run only in the server sandbox, which is {state.value} on this host")
        return sandbox.launcher(ServerProfile(stdio.read_only, stdio.writable))

    return launcher_for


@dataclass
class _Lifecycle:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0  # holding or waiting


@dataclass
class Application:
    data_root: Path
    store: SqliteStore
    data: SqliteData
    runtime: Runtime
    runner: BackgroundRunner
    inside: InsideEnvironment
    sandbox_state: SandboxState  # at startup; ``current_sandbox_state`` follows later changes
    model_id: str
    context_tokens: int  # the request token budget (compaction starts at 80%)
    operator_secret: str = field(repr=False)  # never in reprs, logs or tracebacks
    mcp: McpConnection = field(default_factory=lambda: McpConnection([], (), []))
    turn: TurnSettings = field(default_factory=lambda: TurnSettings(DEFAULT_TURN_BUDGET, DEFAULT_UNATTENDED_RUNS,
                                                                    None))
    # Offered models whose window, efforts and Tool calling the Provider does
    # not declare (V3-P64 §3, review B1): told at startup, used as configured.
    unverified_models: tuple[str, ...] = ()
    trash_days: int = DEFAULT_TRASH_DAYS
    file_max_bytes: int = DEFAULT_FILE_MAX_BYTES  # V3-P66 §8.3
    uploads_work: bool = True  # the no-replace rename placing needs works here (review N2)
    views: ViewMaker | None = None  # model copies of images (V3-P67 §3.1); None or unavailable: no copies
    image_models: tuple[str, ...] = ()  # offered models that get images (V3-P67 §7.1)
    documents: DocumentReader | None = None  # read_document's reader (V3-P70); None: not offered
    toolkit: Toolkit | None = field(default=None, repr=False)  # its checked folder, open until close()
    memory_enabled: bool = False  # V3-P71 §9: the memory Tools and notes (HIMARI_MEMORY)
    conversation_search_enabled: bool = False  # V3-P72 §9 (HIMARI_CONVERSATION_SEARCH)
    # V3-P65 §7.3: one lifecycle lock per Conversation (P0: one server process),
    # kept only while some call holds or waits for it (PR #56 review C1).
    _lifecycle: dict[ConversationId, _Lifecycle] = field(default_factory=dict, repr=False)
    _cleanup: asyncio.Task[None] | None = field(default=None, repr=False)

    @classmethod
    async def open(
        cls,
        data_root: str | os.PathLike[str],
        provider: ProviderAdapter,
        *,
        model_id: str,
        reasoning_effort: str | None = None,
        sandbox: bool = True,
        context_tokens: int | None = None,
        tools: Iterable[ToolHandler] = (),
        instructions: Sequence[str] | None = None,
        environ: Mapping[str, str] | None = None,  # where mcp.json's token variables are read
        mcp_transport: httpx.AsyncBaseTransport | None = None,  # tests: a fake MCP server
        mcp_launcher_for: LauncherFor | None = None,  # tests: stdio servers without the server sandbox
        turn_budget: int | None = DEFAULT_TURN_BUDGET,
        unattended_runs: int | None = DEFAULT_UNATTENDED_RUNS,
        max_runs: int | None = None,
        # The offered models with their default efforts (V3-P64 §3), in the
        # operator's order; empty: ``model_id`` alone. The default model's
        # effort, when its entry has none, is ``reasoning_effort``.
        models: Sequence[tuple[str, str | None]] = (),
        trash_days: int = DEFAULT_TRASH_DAYS,  # V3-P65: 1..365
        cleanup_seconds: float | None = CLEANUP_SECONDS,  # None: no periodic purge (tests call purge_due)
        file_max_bytes: int = DEFAULT_FILE_MAX_BYTES,  # V3-P66 §8.3: one file
        file_store_max_bytes: int = DEFAULT_STORE_MAX_BYTES,  # the file store, before expiring the oldest
        file_keep_days: int = DEFAULT_KEEP_DAYS,  # unused this long, a stored file expires (§8.5)
        documents_dir: str | None = None,  # V3-P70 §6.2: HIMARI_DOCUMENTS_DIR; None: no documents
        documents_owners: frozenset[int] = frozenset({0}),  # who may own the toolkit: root (tests add their user)
        memory: bool = True,  # V3-P71 §9: HIMARI_MEMORY=0 turns it off (the notes stay)
        conversation_search: bool = True,  # V3-P72 §9: HIMARI_CONVERSATION_SEARCH=0 turns its Tools off
    ) -> Application:
        if file_max_bytes < 1 or file_max_bytes > file_store_max_bytes:
            raise ValueError("a file's limit must be positive and within the file store's (V3-P66)")
        if turn_budget is None and max_runs is None:
            raise ValueError("a turn needs a token budget or a Run limit (V3-P62)")
        if not 1 <= trash_days <= 365:
            raise ValueError("trash_days is 1..365 (V3-P65)")
        turn = TurnSettings(turn_budget, unattended_runs, max_runs)
        offers: list[ModelOffer] = []
        unverified: list[str] = []
        image_models: list[str] = []
        for offered, effort in models or ((model_id, None),):
            if offered == model_id and effort is None:
                effort = reasoning_effort
            capabilities = provider.describe_capabilities(offered)
            # Compaction works toward the budget; only the model's input ceiling
            # (its window less the room kept for an answer) refuses a Run.
            ceiling = input_ceiling(capabilities.context_tokens, capabilities.max_output_tokens)
            try:
                limit = ContextBudget(context_limit(ceiling, context_tokens), hard_limit_tokens=ceiling)
            except ValueError as error:
                raise ValueError(f"{offered}: {error}") from None
            offers.append(ModelOffer(offered, effort, capabilities.reasoning_efforts, limit,
                                     capabilities.max_output_tokens))
            if capabilities.context_tokens is None:
                unverified.append(offered)
            if capabilities.image_input:
                image_models.append(offered)
        catalog = ModelCatalog(tuple(offers), model_id)  # duplicates, the default, efforts (§3)
        default_budget = catalog.default.budget
        assert default_budget is not None
        root = Path(os.path.abspath(data_root))
        ensure_private_dir(str(root))
        workspaces = root / "workspaces"
        ensure_private_dir(str(workspaces))
        secret = ensure_operator_secret(str(root / "control"))
        mcp_config = load_config(str(root / CONFIG_NAME), os.environ if environ is None else environ)

        store = SqliteStore(root / DATABASE_NAME)  # first: a second server on this root fails here
        mcp: McpConnection | None = None
        toolkit: Toolkit | None = None
        try:
            store.blobs.configure(max_bytes=file_store_max_bytes, keep_days=file_keep_days)
            launcher_for = mcp_launcher_for
            if launcher_for is None and sandbox:
                launcher_for = await _server_launchers(mcp_config, root)
            mcp = await connect(mcp_config, check_schema=check_tool_input_schema, transport=mcp_transport,
                                launcher_for=launcher_for)
            toolkit, reader, documents_reason = await _documents(root, sandbox, documents_dir, documents_owners)
            backend: SandboxBackend | None = None
            state = SandboxState.UNAVAILABLE
            scripts = "off"
            if sandbox:
                protected = (str(root), str(workspaces))
                candidate = SandboxBackend.discover(toolkit_fd=None if toolkit is None else toolkit.fd)
                state = await candidate.start(protected_paths=protected)
                if state is SandboxState.SETUP_FAILED and toolkit is not None:
                    # A toolkit the self-test does not accept costs the scripts, never run_command (§6.4).
                    scripts = f"the self-test refused the toolkit bind ({state.value})"
                    candidate = SandboxBackend.discover()
                    state = await candidate.start(protected_paths=protected)
                if state is SandboxState.ENFORCED:
                    backend = candidate  # run_command is offered only when enforcement is proven (V3-050)
                    if backend.toolkit_fd is not None:
                        scripts = "on"
            if reader is not None:
                reader.scripts = scripts == "on"
            log.info("documents: read_document %s; run_command toolkit %s%s", documents_reason,
                     scripts if reader is not None else "off",
                     f"; libraries {', '.join(f'{k} {v}' for k, v in reader.versions.items())}" if reader else "")
            inside = InsideEnvironment(workspaces, sandbox=backend,
                                       files=InsideFiles(store.blobs, SqliteData(store), file_max_bytes),
                                       documents=reader)
            uploads_work = inside.placement_works()
            if not uploads_work:
                log.warning("this filesystem cannot place uploads without replacing files (renameat2); "
                            "attaching files is off")
            elif state is not SandboxState.ENFORCED:  # V3-P66 §5.2 (review C2), V3-P70 review N8
                log.warning("commands cannot run (sandbox %s): the model can read attached text files%s, "
                            "but cannot run scripts on other files", state.value,
                            " and PDF, Office and HWPX documents" if reader is not None else "")
            views = await _image_views(root, sandbox)
            base = DEFAULT_INSTRUCTIONS if instructions is None else instructions
            runtime = build_runtime(provider, store=store, environments=[inside], tools=[*tools, *mcp.handlers, *mcp.resource_tools],
                                    models=catalog,
                                    instructions=(*base, *((ATTACHMENT_NOTE,) if uploads_work else ()),
                                                  TOOL_IMAGES_NOTE, PROGRESS_NOTE,
                                                  *_documents_notes(reader, toolkit, scripts == "on")),
                                    external_defaults=mcp.defaults,
                                    source_notes=[SourceNote(source, note.placement, note.text)
                                                  for source, note in mcp.notes.items()],
                                    max_runs_per_drive=max_runs, turn_budget=turn_budget,
                                    no_progress_runs=turn.no_progress_runs, image_views=views,
                                    file_max_bytes=file_max_bytes, memory=memory,
                                    conversation_search=conversation_search)
        except BaseException:
            try:
                if mcp is not None:
                    await mcp.aclose()
            finally:
                if toolkit is not None:
                    toolkit.close()
                store.close()
            raise
        assert isinstance(runtime.data, SqliteData)
        runner = BackgroundRunner(runtime.driver, unattended_runs=unattended_runs, seen_seconds=turn.seen_seconds)
        application = cls(root, store, runtime.data, runtime, runner, inside, state, model_id,
                          default_budget.limit_tokens, secret, mcp, turn, tuple(unverified), trash_days,
                          file_max_bytes, uploads_work, views, tuple(image_models), reader, toolkit,
                          memory_enabled=memory, conversation_search_enabled=conversation_search)
        if cleanup_seconds is not None:
            application._cleanup = asyncio.create_task(application._purge_periodically(cleanup_seconds),
                                                       name="trash-cleanup")
        return application

    def seen(self, scope: TrustedScope) -> None:
        """A client read this Conversation (V3-P62 §5): call only after the
        read was authorized."""
        self.runner.seen(scope)

    @property
    def current_sandbox_state(self) -> SandboxState:
        """The sandbox as it is now: a setup failure while running a command
        latches SETUP_FAILED after startup, and commands are refused from then on."""
        return self.inside.sandbox.state if self.inside.sandbox is not None else self.sandbox_state

    async def context_status(self, scope: TrustedScope) -> ContextStatus | None:
        """For display (Web/CLI): the next request's estimated size and the
        latest compaction. Authorizes like any read (NOT_AUTHORIZED)."""
        return await self.runtime.driver.context_status(scope)

    @property
    def tool_source_statuses(self) -> tuple[SourceStatus, ...]:
        """The configured MCP servers as connected at startup."""
        return self.mcp.statuses

    async def tool_sources(self, scope: TrustedScope) -> tuple[ToolSourceView, ...]:
        """Every configured External Tool source and whether this
        Conversation uses it. Authorizes like any read (NOT_AUTHORIZED)."""
        chosen = {s.source: s.enabled for s in await self.data.read_external_settings(scope)}
        return tuple(
            ToolSourceView(s.source, chosen.get(s.source, s.enabled_by_default), s.enabled_by_default,
                           chosen.get(s.source), s.available, s.tool_ids, s.problems)
            for s in self.mcp.statuses
        )

    async def set_tool_source(self, scope: TrustedScope, source: str, enabled: bool | None) -> None:
        """This Conversation's choice for a configured source; None: follow
        its default. A choice equal to the default is not kept (V3-P53: only
        differences are), so the Conversation keeps following the default if
        the operator changes it. Takes effect from the next model request,
        and calls the model already made are checked again before they run."""
        default = self.mcp.defaults.get(source)
        if default is None:
            raise UnknownToolSource(source)
        await self.data.set_external_setting(scope, source, None if enabled == default else enabled)

    # --- Models (V3-P64) ---------------------------------------------------------------

    @property
    def models(self) -> tuple[ModelView, ...]:
        """The offered models, in the operator's order."""
        catalog = self.runtime.models
        return tuple(ModelView(o.model_id, o.default_effort, ordered_efforts(o.efforts),
                               o.model_id not in self.unverified_models, o.model_id == catalog.default_model,
                               o.model_id in self.image_models)
                     for o in catalog.offers)

    def _choice_view(self, setting: ModelSetting | None, resolved: ResolvedModel) -> ModelChoiceView:
        return ModelChoiceView(resolved.model_id, resolved.reasoning_effort, setting, resolved.fallback)

    async def model_choice(self, scope: TrustedScope) -> ModelChoiceView:
        """What this Conversation's next turn would use. Authorizes like any
        read (NOT_AUTHORIZED)."""
        setting = await self.data.read_model_setting(scope)
        return self._choice_view(setting, self.runtime.models.resolve(setting))

    async def set_model(self, scope: TrustedScope, model_id: str | None, effort: str | None) -> ModelChoiceView:
        """This Conversation's choice, kept as made even if it is the default
        model (V3-P64 Q2); ``model_id`` None: follow the default. No effort:
        the model's default effort. Saved at once, used from the next turn
        (the one under way keeps its model)."""
        await self.data.read_conversation(scope)  # NOT_AUTHORIZED before anything else
        if model_id is None:
            if effort is not None:
                raise ModelRefused("invalid_argument", "Choose a model to set its reasoning effort.")
            setting = None
        else:
            offer = self.runtime.models.offer(model_id)
            if offer is None:
                raise ModelRefused("unknown_model", "This server does not offer that model.")
            if effort is not None and effort not in offer.efforts:
                offered = ", ".join(ordered_efforts(offer.efforts)) or "none"
                raise ModelRefused("invalid_effort", f"Reasoning efforts for this model: {offered}.")
            setting = ModelSetting(model_id, effort)
        await self.data.set_model_setting(scope, setting)
        return await self.model_choice(scope)

    async def run_models(self, scope: TrustedScope, run_ids: Iterable[RunId]) -> dict[RunId, str]:
        """The model each of these Runs was sent to (its record)."""
        return {run_id: (await self.data.read_run(scope, run_id)).model_id for run_id in dict.fromkeys(run_ids)}

    # --- Server prompts (V3-P60 §4) ------------------------------------------------------

    async def prompts(self, scope: TrustedScope) -> tuple[PromptView, ...]:
        """The prompts of the servers on in this Conversation, by server.
        Authorizes like any read (NOT_AUTHORIZED)."""
        await self.data.read_conversation(scope)
        enabled = sorted(v.source for v in await self.tool_sources(scope) if v.enabled)
        return tuple(PromptView(source, p.name, p.title, p.description, p.arguments)
                     for source in enabled for p in self.mcp.prompts.get(source, ()))

    async def expand_prompt(self, scope: TrustedScope, source: str, name: str,
                            arguments: Mapping[str, str]) -> str:
        """The prompt as text for the user to review and send; nothing is
        sent or recorded here. Checks the caller may read this Conversation
        and that the server is on in it (PR #40 review C2), then asks the
        server in this Conversation's session (``prompts/get``)."""
        await self.data.read_conversation(scope)
        view = next((v for v in await self.tool_sources(scope) if v.source == source), None)
        if view is None:
            raise UnknownToolSource(source)
        if not view.enabled:
            raise PromptRefused("source_disabled", "This server is off in this conversation.")
        info = next((p for p in self.mcp.prompts.get(source, ()) if p.name == name), None)
        if info is None:
            raise PromptRefused("unknown_prompt", "This server offers no such prompt.")
        declared = [a.name for a in info.arguments]
        if not set(arguments) <= set(declared):
            takes = ", ".join(declared)[:300] if declared else "no arguments"
            raise PromptRefused("invalid_arguments", f"This prompt takes: {takes}.")
        missing = [a.name for a in info.arguments if a.required and not arguments.get(a.name)]
        if missing:
            raise PromptRefused("invalid_arguments", f"Required: {', '.join(missing)[:300]}.")
        if any(len(value) > MAX_PROMPT_ARGUMENT_CHARS for value in arguments.values()):
            raise PromptRefused("invalid_arguments", f"An argument is at most {MAX_PROMPT_ARGUMENT_CHARS} characters.")
        given = [(n, arguments[n]) for n in declared if n in arguments]  # in the prompt's own order
        try:
            result = await self.mcp.get_prompt(source, name, dict(given), str(scope.conversation_id))
        except McpError as error:
            if error.code == INVALID_PARAMS:
                raise PromptRefused("invalid_arguments", "The server refused these arguments.") from None
            raise PromptRefused("server_error", "The server could not give this prompt.") from None
        try:
            return prompt_text(source, name, given, result)
        except PromptContentError as error:
            raise PromptRefused("unsupported_content", f"This prompt cannot be used: {error}.") from None

    # --- Tool views (MCP Apps, V3-P58) ---------------------------------------------------

    @property
    def views_configured(self) -> bool:
        """Some configured server's views are shown (its ``ui`` is set)."""
        return any(policy is not None for policy in self.mcp.ui_policies.values())

    def _view_source(self, tool_id: str) -> str | None:
        """The configured MCP server whose Tool this is, if any."""
        source, slash, _ = tool_id.partition("/")
        return source if slash and source in self.mcp.ui_policies else None

    async def view_flags(self, scope: TrustedScope, run_ids: Iterable[RunId]) -> dict[CallRef, tuple[bool, bool]]:
        """For each call of these Runs with a view that may be shown here:
        (it has a view to show while it runs, its result was stored with a
        view). A call is left out when its source is off in this Conversation
        or the operator does not show its server's views (§3.7)."""
        enabled = await self.runtime.access.enabled_sources(scope)
        flags: dict[CallRef, tuple[bool, bool]] = {}
        for run_id in dict.fromkeys(run_ids):
            for record in await self.data.read_tool_executions(scope, run_id):
                source = self._view_source(record.tool_id)
                if source is None or source not in enabled or self.mcp.ui_policy(source) is None:
                    continue
                current = self.mcp.ui_for(record.tool_id) is not None
                if current or record.ui_digest is not None:
                    flags[record.call] = (current, record.ui_digest is not None)
        return flags

    async def app_view(self, scope: TrustedScope, call: CallRef) -> AppViewState:
        """A call's view as it should be shown now; AppViewRefused when there
        is none to show here. Checks, in this order: the source is on in this
        Conversation, the operator shows its views, there is a view."""
        record = next((r for r in await self.data.read_tool_executions(scope, call.run_id) if r.call == call), None)
        source = None if record is None else self._view_source(record.tool_id)
        if record is None or source is None:
            raise AppViewRefused("no_app_view")
        if source not in await self.runtime.access.enabled_sources(scope):
            raise AppViewRefused("source_disabled")
        policy = self.mcp.ui_policy(source)
        if policy is None:
            raise AppViewRefused("app_view_disabled")
        ended = AppViewState("ended", call, source, record.tool_id, self.mcp.tool_info(record.tool_id),
                             record.arguments, record.status)
        stored = await self.data.read_app_view(scope, call) if record.ui_digest is not None else None
        if stored is not None:
            resource = stored.view.resource
            return replace(ended, state="done", resource=resource, result=stored.view.payload,
                           csp=_allowed(resource.csp, policy))
        current = self.mcp.ui_for(record.tool_id)
        if current is None:
            raise AppViewRefused("no_app_view")
        if record.status in _RUNNING:
            return replace(ended, state="running", resource=current, csp=_allowed(current.csp, policy))
        return replace(ended, outcome=_ENDED_OUTCOME[record.status], reason=_ended_reason(record))

    # --- the user's instructions (V3-P69) ----------------------------------------------

    async def instructions(self, principal: PrincipalId) -> Instructions:
        """The instructions the user wrote for every Conversation."""
        return await self.data.read_instructions(principal, InstructionScope.USER)

    async def set_instructions(self, principal: PrincipalId, text: str, revision: int) -> Instructions:
        """Saved at once, used from each Conversation's next turn (§5); a turn
        under way keeps the text it started with. DataError VERSION_CONFLICT
        (written since ``revision`` was read), TOO_LARGE or INVALID_ARGUMENT."""
        return await self.data.write_instructions(principal, InstructionScope.USER, text, revision)

    # --- memory (V3-P71) ----------------------------------------------------------------

    async def memories(self, principal: PrincipalId) -> tuple[Memory, ...]:
        """The principal's notes, by category, newest first within each."""
        return await self.data.list_memories(principal)

    async def add_memory(self, principal: PrincipalId, category: MemoryCategory, text: str) -> Memory:
        """A note the user writes; used from each Conversation's next turn.
        DataError TOO_LARGE, LIMIT_EXCEEDED or INVALID_ARGUMENT."""
        return await self.data.save_memory(principal, category, text)

    async def edit_memory(self, principal: PrincipalId, memory_id: int, category: MemoryCategory, text: str,
                          revision: int) -> Memory:
        """The user's rewrite: their text from now on, its source kept (§4.3).
        DataError NOT_FOUND, VERSION_CONFLICT, TOO_LARGE or INVALID_ARGUMENT."""
        return await self.data.save_memory(principal, category, text, memory_id=memory_id, expected_revision=revision)

    async def delete_memory(self, principal: PrincipalId, memory_id: int, revision: int | None = None) -> Memory:
        """Gone at once; out of each Conversation's next turn. Results already
        in a Conversation still show its text (§4.3). NOT_FOUND, VERSION_CONFLICT."""
        return await self.data.delete_memory(principal, memory_id, revision)

    async def memory_lines(self, scope: TrustedScope, run_ids: Iterable[RunId]) -> dict[CallRef, MemoryLine]:
        """The memory calls of these Runs with what each changed, and whether
        an Undo is offered now — from the notes as they are (§7.1)."""
        lines: dict[CallRef, MemoryLine] = {}
        notes: dict[int, Memory] | None = None
        for run_id in dict.fromkeys(run_ids):
            for record in await self.data.read_tool_executions(scope, run_id):
                if record.tool_id not in (MEMORY_SAVE, MEMORY_FORGET):
                    continue
                if notes is None:
                    notes = {n.memory_id: n for n in await self.data.list_memories(scope.principal_id)}
                line = _memory_line(record, notes)
                if line is not None:
                    lines[record.call] = line
        return lines

    async def undo_memory(self, scope: TrustedScope, call: CallRef) -> Memory | None:
        """Undo what this memory_save call did (V3-P71 §7.1, rev. 5): a new
        note is deleted (None), a rewrite puts the note back as it was,
        origin and source included. What to put back comes from the call's
        stored record, never from the request. NOT_FOUND: no such change, the
        note is gone, or the conversation it came from was deleted for good;
        VERSION_CONFLICT: the note changed since."""
        record = next((r for r in await self.data.read_tool_executions(scope, call.run_id) if r.call == call), None)
        raw = None if record is None else record.raw_result
        line = None if record is None else _memory_line(record, {})
        if record is None or line is None or line.action not in ("saved", "updated") or not isinstance(raw, Mapping):
            raise DataError(DataErrorCode.NOT_FOUND, "no memory change to undo")
        written = raw["memory"]
        assert isinstance(written, Mapping)
        revision = written.get("revision")
        if not isinstance(revision, int):
            raise DataError(DataErrorCode.NOT_FOUND, "no memory change to undo")
        principal = scope.principal_id
        if line.action == "saved":
            await self.data.delete_memory(principal, line.memory_id, revision)
            return None
        before = _memory_before(raw.get("previous"))
        if before is None or before.memory_id != line.memory_id:
            raise DataError(DataErrorCode.NOT_FOUND, "this change cannot be undone")
        return await self.data.restore_memory(principal, before, expected_revision=revision)

    async def create_conversation(self, principal: PrincipalId, *, title: str | None = None) -> ConversationId:
        """T0 with the principal's Inside Workspace activated (provisioned first)."""
        workspace = workspace_for(principal)
        self.inside.provision(workspace)
        return await self.data.create_conversation(
            principal, title=title, activations=(InitialActivation(INSIDE, workspace),)
        )

    # --- files (V3-P66) ----------------------------------------------------------------

    async def upload(self, scope: TrustedScope, name: str, media_type: str | None,
                     chunks: AsyncIterator[bytes]) -> FileRecord:
        """§5.1: store the bytes, then stage them for a message of this
        Conversation. The staged count is checked before the bytes are read
        (review C10); the lifecycle lock is held only while recording, never
        while the body streams (review N4)."""
        if not name or len(name.encode("utf-8", "surrogatepass")) > MAX_RAW_NAME_BYTES:
            raise FileNameRefused(name[:40])
        cleaned = clean_file_name(name)
        await self._workspace(scope)  # authorizes; FilesUnavailable without a place for uploads
        if await self.data.count_staged(scope) >= MAX_STAGED_UPLOADS:
            raise DataError(DataErrorCode.LIMIT_EXCEEDED, f"{MAX_STAGED_UPLOADS} uploads wait to be sent")
        blob = await self.data.blobs.put(scope.principal_id, chunks, self.file_max_bytes)
        image = await self.data.blobs.prepare_image(scope.principal_id, blob, self.views)  # V3-P67 §3.1
        async with self._exclusive(scope.conversation_id):
            return await self.data.stage_upload(scope, blob, cleaned, clean_media_type(media_type, cleaned), image)

    @property
    def images_available(self) -> bool:
        """Whether model copies of images can be made here (V3-P67 §3.1)."""
        return self.views is not None and self.views.available

    @property
    def documents_status(self) -> dict[str, Any]:
        """V3-P70 §9: read_document offered, run_command with the toolkit, and the toolkit's libraries."""
        reader = self.documents
        read = reader is not None and reader.available
        return {"read": read, "scripts": read and reader is not None and reader.scripts
                and self.current_sandbox_state is SandboxState.ENFORCED,
                "libraries": dict(reader.versions) if read and reader is not None else {}}

    async def _workspace(self, scope: TrustedScope) -> tuple[WorkspaceId, str]:
        if not self.uploads_work:
            raise FilesUnavailable(scope.conversation_id)
        try:
            workspace = await self.data.resolve_conversation_workspace(scope)
        except DataError as error:
            if error.code is DataErrorCode.NOT_FOUND:
                raise FilesUnavailable(scope.conversation_id) from None
            raise
        return workspace.workspace_id, workspace.cwd_relative

    async def send(self, scope: TrustedScope, text: str, attachments: Sequence[FileId] = ()) -> JobStatus:
        """A user message. With attachments (§5.1, under the lifecycle lock):
        1 the T1 checks as a read; 2 refused while a job runs; 3 the uploads
        placed in ``uploads/`` under the Workspace write lock (waited for at
        most PLACE_LOCK_SECONDS); 4 the message submitted, attaching them."""
        if not attachments:
            await self.data.read_conversation(scope)  # 404 before a job is started for a foreign id
            return await self.runner.submit(scope, UserMessage((TextBlock(text),)))
        async with self._exclusive(scope.conversation_id):
            records = await self.data.check_attachments(scope, attachments)
            if self.runner.status(scope).running:
                raise ControlError(ControlErrorCode.BUSY, scope.conversation_id)
            workspace_id, cwd = await self._workspace(scope)
            async with self.inside.locked(workspace_id, PLACE_LOCK_SECONDS):
                # Read again under the lock: another tab may have placed them (review C4).
                fresh = await self.data.read_file_records(scope, [(r.file_id, r.version) for r in records])
                for index, record in enumerate(fresh):
                    await self._place(scope, workspace_id, cwd, record, first=index == 0)
            blocks = ((TextBlock(text),) if text else ()) + tuple(AttachmentRef(r.file_id, r.version) for r in records)
            return await self.runner.submit(scope, UserMessage(blocks))

    async def _place(self, scope: TrustedScope, workspace_id: WorkspaceId, cwd: str, record: FileRecord, *,
                     first: bool) -> None:
        """One upload into ``uploads/`` (§5.2), its path recorded before it is
        given (owner review B1): a crash after the rename leaves a file its
        record names, and a retry finds it there by its bytes.

        Already there with its bytes: nothing to do, and its stored bytes are
        not needed (PR #59 review C11). Otherwise: a durable copy, a free name
        (the recorded one if it is free), the name recorded, then the rename."""
        if record.workspace_path is not None and await self.inside.holds(
                workspace_id, cwd, record.workspace_path, record.size, record.blob.sha256):
            return
        source = self.data.blobs.open(record.blob)  # here: the store is not thread-safe
        temp = await self.inside.copy_in(workspace_id, cwd, source, clear=first)
        recorded = record.workspace_path
        try:
            for _ in range(3):  # a name is taken only if something outside the lock wrote it
                path = await self.inside.free_path(workspace_id, cwd, record.name, recorded)
                if path != recorded:
                    await self.data.set_placement(scope, record.file_id, path)
                    recorded = path
                if await self.inside.commit_copy(workspace_id, cwd, temp, path):
                    return
            raise FilesUnavailable(f"no free name for {record.name}")
        finally:
            await self.inside.discard_copy(workspace_id, cwd, temp)

    async def files(self, scope: TrustedScope, *, limit: int = 50, offset: int = 0) -> Sequence[FileRecord]:
        return await self.data.read_files(scope, limit=limit, offset=offset)

    async def call_files(self, scope: TrustedScope, calls: Sequence[CallRef]) -> Mapping[CallRef, Sequence[FileRecord]]:
        return await self.data.read_call_files(scope, calls)

    async def attachment_records(self, scope: TrustedScope, refs: Sequence[tuple[FileId, int]]) -> Sequence[FileRecord]:
        return await self.data.read_file_records(scope, refs)

    async def preview(self, scope: TrustedScope, file_id: FileId, version: int) -> tuple[ImageView, BinaryIO]:
        """V3-P67 §8: an image's model copy — what the model saw, small and
        without metadata. NOT_FOUND unless ``view_state`` is READY or the
        copy expired (BLOB_EXPIRED); BLOB_MISSING when its bytes are gone."""
        found = await self.data.read_file_records(scope, [(file_id, version)])
        state = view_state(found[0]) if found else None
        if state is ViewState.VIEW_EXPIRED:
            raise DataError(DataErrorCode.BLOB_EXPIRED, file_id)
        if state is not ViewState.READY or found[0].view is None:
            raise DataError(DataErrorCode.NOT_FOUND, file_id)
        view = found[0].view
        return view, self.data.blobs.open(view.blob)

    async def download(self, scope: TrustedScope, file_id: FileId, version: int) -> tuple[FileRecord, BinaryIO]:
        """§7.2: a file's stored bytes. NOT_FOUND for a file this Conversation
        does not have; BLOB_EXPIRED or BLOB_MISSING when its bytes are gone."""
        found = await self.data.read_file_records(scope, [(file_id, version)])
        if not found:
            raise DataError(DataErrorCode.NOT_FOUND, file_id)
        record = found[0]
        return record, self.data.blobs.open(record.blob)

    # --- the trash (V3-P65 §7) --------------------------------------------------------

    @asynccontextmanager
    async def _exclusive(self, conversation_id: ConversationId) -> AsyncIterator[None]:
        """The Conversation's lifecycle lock; dropped when its last user is
        done, so ids that were never (or are no longer) there leave nothing."""
        entry = self._lifecycle.get(conversation_id)
        if entry is None:
            entry = self._lifecycle[conversation_id] = _Lifecycle()
        entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.users -= 1
            if not entry.users:
                del self._lifecycle[conversation_id]

    async def delete_notice(self, scope: TrustedScope) -> DeleteNotice:
        """For the delete confirmation: the external servers this Conversation
        called, named by the recorded Tool ids (``<server>/<tool>``), not by
        what is configured now (review C1). Authorizes like any read."""
        used = await self.data.used_tool_ids(scope)
        configured = {status.source for status in self.mcp.statuses}
        servers = sorted({tool_id.split("/", 1)[0] for tool_id in used if "/" in tool_id}
                         - (BUILT_IN_NAMESPACES - configured))
        return DeleteNotice(tuple(servers), tuple(s for s in servers if s not in configured),
                            self.runner.status(scope).running, self.trash_days,
                            await self.data.count_source_memories(scope))

    async def trash(self, scope: TrustedScope) -> datetime:
        """§7.1: the owner checked first (review B4: before the Runner is
        touched), then the Conversation closed to new work and a running job
        stopped (ConversationStillStopping after TRASH_STOP_SECONDS; nothing
        moved), then moved. Its own MCP processes are ended."""
        async with self._exclusive(scope.conversation_id):
            await self.data.read_conversation(scope)  # NOT_AUTHORIZED: the Runner stays as it is
            try:
                await self.runner.close(scope, TRASH_STOP_SECONDS)
            except BaseException as error:  # a timeout, or this call cancelled (PR #56 review B1): not moved
                self.runner.reopen(scope)
                if isinstance(error, TimeoutError):
                    raise ConversationStillStopping(scope.conversation_id) from None
                raise
            try:
                moved = await self.data.trash_conversation(scope)
            except BaseException:
                self.runner.reopen(scope)
                raise
            self.runner.forget(scope)  # stays closed while in the trash
            try:
                await self.mcp.release(scope.conversation_id)
            except Exception:  # noqa: BLE001 — the move stands; an idle process ends by itself
                log.exception("could not end the MCP processes of %s", scope.conversation_id)
            return moved

    async def restore(self, scope: TrustedScope) -> None:
        async with self._exclusive(scope.conversation_id):
            await self.data.restore_conversation(scope)  # owner and state first
            self.runner.reopen(scope)

    async def purge(self, scope: TrustedScope) -> None:
        """§7.2: PURGING recorded (no restore from here on), the
        Conversation's own folder removed, then every Data row."""
        async with self._exclusive(scope.conversation_id):
            trashed = await self.data.begin_purge(scope)  # owner and state first
            workspace = trashed.workspace
            if workspace is not None:
                await self.inside.remove_conversation_folder(workspace.workspace_id, scope, workspace.cwd_relative)
            await self.data.purge_conversation(scope)
            self.runner.forget(scope)
            self.runner.reopen(scope)

    async def purge_due(self, now: datetime | None = None) -> int:
        """Purge what was trashed more than ``trash_days`` before ``now``,
        and every purge cut short; how many were purged. One that fails is
        logged and tried again next time."""
        cutoff = (datetime.now(UTC) if now is None else now) - timedelta(days=self.trash_days)
        purged = 0
        for scope in await self.data.due_purges(cutoff):
            try:
                await self.purge(scope)
                purged += 1
            except Exception:  # noqa: BLE001 — the others still go
                log.exception("could not purge %s; it is tried again later", scope.conversation_id)
        return purged

    async def clean_files(self, now: datetime | None = None) -> None:
        """V3-P66 §8.2: staged uploads never sent go after STAGED_KEEP; then
        the file store's sweep (unreferenced blobs, the keep days, leftovers)."""
        now = datetime.now(UTC) if now is None else now
        try:
            await self.data.drop_stale_staged(now - STAGED_KEEP)
        except Exception:  # noqa: BLE001 — the sweep still runs
            log.exception("could not drop stale staged uploads")
        await self.data.sweep_blobs(now)

    async def _purge_periodically(self, seconds: float) -> None:
        while True:
            try:
                await self.purge_due()
            except Exception:  # noqa: BLE001 — the next round tries again
                log.exception("trash cleanup failed")
            try:
                await self.clean_files()
            except Exception:  # noqa: BLE001 — the next round tries again
                log.exception("file cleanup failed")
            await asyncio.sleep(seconds)

    async def close(self, grace_seconds: float = 30.0) -> None:
        if self._cleanup is not None:
            self._cleanup.cancel()
            await asyncio.gather(self._cleanup, return_exceptions=True)  # its own cancellation, not ours
        try:
            await self.runner.shutdown(grace_seconds)
        finally:
            try:
                await self.mcp.aclose()
            finally:
                if self.toolkit is not None:
                    if isinstance(self.inside.sandbox, SandboxBackend):
                        self.inside.sandbox.toolkit_fd = None  # the number may be reused once closed (review N8)
                    self.toolkit.close()
                self.store.close()

    def check_layout(self) -> None:
        """Re-verify the private directories (e.g. before serving)."""
        for path in (self.data_root, self.data_root / "workspaces", self.data_root / "control"):
            check_private_dir(str(path))


def _allowed(declared: UiCsp, policy: McpUiPolicy) -> UiCsp:
    """V3-P58 §3.3: what the server declares that the operator also allows,
    as the operator wrote it. Nested frames and other base URIs: never."""
    def both(values: tuple[str, ...], allowed: frozenset[str]) -> tuple[str, ...]:
        return tuple(sorted({v.lower() for v in values} & allowed))
    return UiCsp(both(declared.connect_domains, policy.connect_domains),
                 both(declared.resource_domains, policy.resource_domains))


def _ended_reason(record: ToolExecutionRecord) -> str:
    """Why a call's view closes, from what the call already told the model
    (and the connector's own note when the result was too large to show)."""
    raw = record.raw_result
    problem = raw.get("ui_problem") if isinstance(raw, Mapping) else None
    if isinstance(problem, str):
        return problem[:MAX_REASON_CHARS]
    blocks = () if record.model_result is None else record.model_result.blocks
    text = " ".join(b.text for b in blocks if isinstance(b, TextBlock)).strip()
    if not text:
        text = record.status.value
    return text if len(text) <= MAX_REASON_CHARS else text[: MAX_REASON_CHARS - 1] + "…"
