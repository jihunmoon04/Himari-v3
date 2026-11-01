"""The durable P0 application the Web server and CLI are built on.

One data root, private to the server user::

    <data_root>/                 0700
        himari.db (+ .lock)      SQLite Data (V3-P42), 0600
        workspaces/<ws>/         Inside Workspaces, one per principal (0700)
        control/operator.secret  the local operator's credential (0600)
        file-versions/           previous versions of files the web editor saved or deleted (V3-P75)
        file-versions-agent/     files as they were before the agent changed them (V3-P76)
        file-versions-pdf/       deleted PDFs and what a put-back wrote over (V3-P77; ink saves keep none, V3-P82)
        file-changes/            the agent's file changes, per Conversation, for review (V3-P76)
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
import json
import logging
import posixpath
import re
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO
from zoneinfo import available_timezones

import httpx

from himari.bootstrap import DEFAULT_INSTRUCTIONS, Runtime, build_runtime
from himari.bootstrap.operator import check_private_dir, ensure_operator_secret, ensure_private_dir
from himari.bootstrap.mcp_defaults import McpDefaultsConflict, read_defaults, write_defaults
from himari.common import (
    AttachmentRef,
    ContentBlock,
    BindingId,
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
from himari.control.api import (
    ContextStatus,
    ControlError,
    ControlErrorCode,
    JobListener,
    JobStatus,
    ModelCatalog,
    ModelOffer,
    ResolvedModel,
)
from himari.control.driver import NO_PROGRESS_RUNS, Driver
from himari.control.runner import SEEN_SECONDS, BackgroundRunner
from himari.memory.api import MemoryPassSettings, PassOutcome
from himari.memory.worker import MemoryPasses
from himari.data.api import (
    RETIRED_KEEP,
    Episode,
    NoteDates,
    PassItemRecord,
    PassStatus,
    PassSummary,
    Proposal,
    RecentEpisode,
    memory_from_record,
    MessageOrigin,
    MAX_LIST_LIMIT,
    ConversationSummary,
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
    RegisteredWorkspace,
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
    WakeKind,
    WakeReason,
)
from himari.data.sqlite import SqliteData, SqliteStore
from himari.bootstrap.companion import CompanionEvents, CompanionPresence
from himari.bootstrap.proactive import (
    PROACTIVE_SECONDS,
    Inputs,
    ProactiveRefused,
    ProactiveScheduler,
    WakeEvents,
    settle_cut_wakes,
)
from himari.bootstrap.schedule import Ticker
from himari.presence.api import Devices
from himari.presence.devices import DeviceRegistry
from himari.presence.hub import PresenceHub
from himari.proactive import ProactiveSettings
from himari.documents import TMP_BYTES, DocumentReader, Toolkit, ToolkitRefused, open_toolkit, worker_roots
from himari.environment.inside import INSIDE, InsideEnvironment, InsideFiles
from himari.imaging import ImageViews, runtime_roots
from himari.bootstrap.changes import ChangeDiff, ChangeReview, ChangesView, ChangeStore, RevertedAll
from himari.bootstrap.explorer import PDF_MAX_BYTES, Explorer, ExplorerRefused
from himari.bootstrap.versions import VersionStore
# For the interfaces (V3-P66 §9): how placing an upload can be refused.
from himari.environment.inside import UploadsFolderUnusable as UploadsFolderUnusable
from himari.environment.inside import WorkspaceBusy as WorkspaceBusy
# For the interfaces (V3-P74 §14): which registered folders are cloud-synced.
from himari.environment.machine import CLOUD_FILESYSTEMS as CLOUD_FILESYSTEMS
from himari.environment.machine import (
    MACHINE,
    OWN_LABEL,
    MachineEnvironment,
    MachineUnavailable,
    machine_workspace_for,
)
from himari.runner import api as runner_api
from himari.runner.api import RunnerError
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
    DOCUMENTS_READ_NOTE,
    ContextBudget,
    SourceNote,
    SystemSpec,
    documents_script_note,
)
from himari.provider.api import ProviderAdapter
from himari.tool.api import ToolHandler
from himari.tool.memory import MEMORY_FORGET, MEMORY_SAVE
from himari.tool.schema import check_tool_input_schema

DATABASE_NAME = "himari.db"
DEFAULT_TRASH_DAYS = 30  # V3-P65 Q1 (the owner's choice, 2026-10-21)
DEFAULT_TIME_ZONE = "Asia/Seoul"  # V3-P83 §6.4: HIMARI_TIME_ZONE when unset
CLEANUP_SECONDS = 3600.0  # how often trash past its days is purged
TRASH_STOP_SECONDS = 30.0  # how long a running job may take to stop before a trash is refused (§7.1)
BUILT_IN_NAMESPACES = frozenset({"inside", "machine"})  # Tool ids that are HIMARI's own, not an external server's
PDF_VERSIONS_BYTES = 1024 * 1024 * 1024  # PDFs' previous versions, all together (V3-P77 Q3)
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


class ScreenRefused(ValueError):
    """V3-P84 §4.1–§4.2: a screen is a PNG or JPEG image (by its bytes)."""


# V3-P84 §4.1: a device's upload is a screen, of these types, at most this large.
SCREEN_TYPES = {"image/png": "png", "image/jpeg": "jpg"}
SCREEN_MAX_BYTES = 8 * 1024 * 1024
_SCREEN_HEAD = 16  # PNG's signature and IHDR; JPEG's start of image


async def _screen_body(chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """The body again, once its first bytes look like a PNG or a JPEG; refused
    before anything is stored otherwise (PR #126 review C1)."""
    head = b""
    async for chunk in chunks:
        head += chunk
        if len(head) >= _SCREEN_HEAD:
            break
    if not (head.startswith(b"\x89PNG\r\n\x1a\n") and head[12:16] == b"IHDR") and not head.startswith(b"\xff\xd8\xff"):
        raise ScreenRefused("not a PNG or JPEG image")
    return _then(head, chunks)


async def _then(head: bytes, chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    yield head
    async for chunk in chunks:
        yield chunk


class WorkspaceRefused(ValueError):
    """V3-P74 §10: a folder was not registered, attached or detached;
    ``message`` is for the user. ``code``: ``unavailable`` (folders are not
    offered to this principal, or the runner does not answer),
    ``not_usable`` (the runner's check refused the folder), ``duplicate``
    (registered or attached already), ``not_machine`` (this Conversation does
    not use the machine Environment), ``own_folder`` (it cannot be detached)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


NOT_MACHINE = "This conversation does not work on this server's folders (it was started before they were offered)."


@dataclass(frozen=True)
class AttachedFolder:
    """One of a Conversation's folders as a client shows it (V3-P74 §10.3)."""

    binding_id: str
    path: str | None  # the own folder's is None while the runner does not answer
    label: str
    note: str | None
    working: bool  # the working directory (§10.4)
    own: bool  # the Conversation's own folder: cannot be detached
    workspace_id: str | None  # the registration; None for the own folder


@dataclass(frozen=True)
class ReferenceRequest:
    """V3-P76 §3.2: what the browser sends for a reference — never the text
    itself. ``start``/``end`` (UTF-16) and ``sha256`` (of the selection's
    UTF-8) for a selection; all None for the whole file."""

    path: str
    identity: tuple[int, ...]
    start: int | None = None
    end: int | None = None
    sha256: str | None = None


@dataclass(frozen=True)
class FolderConversations:
    """V3-P75 §6.1: the Conversations working in a folder, most recent first.
    ``workspace``: its registration, if any. ``own``: it is one
    Conversation's own folder (that one is the list). ``refused``: why it
    cannot be a workspace (then nothing can be opened in it). ``above``:
    registered folders above it with Conversations of their own (Q2: shown
    as links, not mixed in), nearest first, with how many."""

    path: str
    workspace: RegisteredWorkspace | None
    conversations: tuple[ConversationSummary, ...]
    own: bool = False
    refused: str | None = None
    above: tuple[tuple[RegisteredWorkspace, int], ...] = ()


@dataclass(frozen=True)
class DeleteNotice:
    """What the delete confirmation tells (V3-P65 §6.1)."""

    servers: tuple[str, ...]  # external servers this Conversation called, by name
    unconfigured: tuple[str, ...]  # of them, the ones no longer configured
    running: bool  # a job runs now: it is stopped first
    trash_days: int
    memories: int = 0  # notes its calls wrote: deleted with it for good (V3-P71 D5)
    episodes: int = 0  # its episodes, deleted with it too (V3-P85 §10.3)


@dataclass(frozen=True)
class MemoryLine:
    """A memory call as its Conversation shows it (V3-P71 §7.1)."""

    action: str  # "saved", "updated", "forgot" or "already" (the note was there)
    memory_id: int
    text: str
    undo: bool  # the note is still as the call left it, so an Undo is offered


@dataclass(frozen=True)
class PassLineItem:
    """One thing a memory pass did, as its Conversation shows it (V3-P85 §10.1)."""

    item: int
    action: str  # "remembered", "updated", "forgot", "open_loop", "closed_loop" or "waiting" (a sensitive proposal)
    memory_id: int | None
    text: str
    due: str | None
    undo: bool  # the note is as the pass left it (a retired one within its days): an Undo is offered
    evidence: tuple[int, ...] = ()  # the user messages it rests on, to go to (§10.1)


@dataclass(frozen=True)
class PassLine:
    """An applied memory pass: shown after the Event it read through."""

    pass_id: str
    through_sequence: int
    items: tuple[PassLineItem, ...]


def _planned(text: str | None) -> dict[str, Any]:
    try:
        value = json.loads(text) if text else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _evidence(record: PassItemRecord) -> tuple[int, ...]:
    """The user messages a decision cited (checked when it was planned, §4.4)."""
    cited = _planned(record.proposal).get("evidence")
    return tuple(n for n in cited if isinstance(n, int)) if isinstance(cited, list) else ()


def _pass_action(record: PassItemRecord) -> str | None:
    """ADD, UPDATE or FORGET from what an applied item recorded (§11): no
    previous state is an ADD, no planned note a FORGET."""
    if record.outcome != "applied":
        return None
    if record.previous is None:
        return "add"
    return "forget" if record.planned is None else "update"


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

    def moment(key: str) -> datetime | None:  # V3-P85: older records have no dates (None)
        value = previous.get(key)
        try:
            return datetime.fromisoformat(value) if isinstance(value, str) else None
        except ValueError:
            return None

    def text_of(key: str) -> str | None:
        value = previous.get(key)
        return value if isinstance(value, str) else None

    due_at, due_instant = text_of("due"), moment("due_instant")
    valid_until, valid_instant = text_of("valid_until"), moment("valid_instant")
    remind_at, remind_instant = text_of("remind_at"), moment("remind_instant")  # V3-P87 §5.3
    item = previous.get("source_item")
    return Memory(memory_id, MemoryCategory(category), text, MemoryOrigin(origin),
                  None if conversation is None else ConversationId(conversation), call, 0, epoch, epoch,
                  due_at=due_at if due_instant is not None else None,
                  due_instant=due_instant if due_at is not None else None,
                  valid_until=valid_until if valid_instant is not None else None,
                  valid_instant=valid_instant if valid_until is not None else None,
                  source_pass=text_of("source_pass"), source_item=item if isinstance(item, int) else None,
                  user_approved_at=moment("user_approved_at"),
                  remind_at=remind_at if remind_instant is not None else None,
                  remind_instant=remind_instant if remind_at is not None else None)


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


@dataclass(frozen=True)
class AutoMemoryStatus:
    """V3-P85 §10.3: automatic memory as the Memory screen tells it."""

    running: bool
    told: str  # as at startup: "on (…)" or "off (why)"
    model_id: str | None
    daily_tokens: int | None
    summary: PassSummary


PASS_LINES_LIMIT = 200  # the newest applied passes a conversation shows lines for (PR #138 review)


class MemoryPassesOff(RuntimeError):
    """A backfill or retry asked while memory passes do not run here."""


class _JobListeners:
    """The Runner's one listener, passing each job event on to several (V3-P85:
    the event stream and memory passes). One that fails is logged; the others
    still hear it."""

    def __init__(self, *listeners: JobListener) -> None:
        self.listeners = list(listeners)

    def _each(self, call: Callable[[JobListener], None]) -> None:
        for listener in self.listeners:
            try:
                call(listener)
            except Exception:  # noqa: BLE001 — a listener is not the job
                log.exception("a job listener failed")

    def job_started(self, scope: TrustedScope, origin: MessageOrigin | None, status: JobStatus) -> None:
        self._each(lambda listener: listener.job_started(scope, origin, status))

    def accepted(self, scope: TrustedScope) -> None:
        self._each(lambda listener: listener.accepted(scope))

    def run_ended(self, scope: TrustedScope) -> None:
        self._each(lambda listener: listener.run_ended(scope))

    def job_ended(self, scope: TrustedScope, status: JobStatus) -> None:
        self._each(lambda listener: listener.job_ended(scope, status))


class _RunnerActivity:
    """V3-P85 §4.1: whether this process runs a job of the Conversation."""

    def __init__(self, runner: BackgroundRunner) -> None:
        self._runner = runner

    def running(self, scope: TrustedScope) -> bool:
        return scope.conversation_id in self._runner.running(scope.principal_id)


def memory_pass_settings(provider: ProviderAdapter, *, memory: bool, auto: bool, model_id: str | None,
                         idle_minutes: int, daily_tokens: int,
                         time_zone: str) -> tuple[MemoryPassSettings | None, str]:
    """V3-P85 §4.8, §12: the settings memory passes run with, or None, and
    the status told at startup. Only a model the Provider declares checked
    for them (Q6) is taken."""
    if not memory:
        return None, "off (HIMARI_MEMORY=0)"
    if not auto:
        return None, "off (HIMARI_MEMORY_AUTO=0)"
    if model_id is None:
        return None, "off (no HIMARI_MEMORY_MODEL)"
    capabilities = provider.describe_capabilities(model_id)
    if not capabilities.memory_passes:
        return None, f"off ({model_id} is not checked for memory passes)"
    effort = "low" if "low" in capabilities.reasoning_efforts else None
    return (MemoryPassSettings(model_id, effort, timedelta(minutes=idle_minutes), daily_tokens, time_zone),
            f"on ({model_id}, after {idle_minutes} quiet minutes, {daily_tokens:,} tokens a day each)")


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
    # V3-P85 §12: memory passes (None: they do not run) and why, as told at startup.
    memory_passes: MemoryPasses | None = field(default=None, repr=False)
    auto_memory: str = "off"
    time_zone: str = DEFAULT_TIME_ZONE  # HIMARI_TIME_ZONE: a date the user writes ends in it unless the page says (V3-P85)
    conversation_search_enabled: bool = False  # V3-P72 §9 (HIMARI_CONVERSATION_SEARCH)
    streaming_enabled: bool = False  # V3-P73 §9 (HIMARI_STREAMING)
    companion_enabled: bool = False  # V3-P83 §3 (HIMARI_COMPANION): pairing and device tokens
    # V3-P74: the machine Environment (its runner's socket was configured) and
    # the principals whose new Conversations use it instead of Inside (Q1).
    machine: MachineEnvironment | None = None
    machine_principals: frozenset[PrincipalId] = frozenset()
    changes: ChangeStore | None = None  # V3-P76: the review record of the machine file Tools' changes
    # One registration at a time (§10.1): the runner's same-folder check and
    # Data's insert together, so two paths to one folder cannot both pass.
    _registering: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _explorer: Explorer | None = field(default=None, repr=False)
    # One "open this folder's conversation" at a time per folder (PR #93 review B1): by real path.
    _opening: dict[str, tuple[asyncio.Lock, int]] = field(default_factory=dict, repr=False)
    # V3-P65 §7.3: one lifecycle lock per Conversation (P0: one server process),
    # kept only while some call holds or waits for it (PR #56 review C1).
    _lifecycle: dict[ConversationId, _Lifecycle] = field(default_factory=dict, repr=False)
    _mcp_defaults_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    # V3-P83 §5, §8.1: who is connected, the current device and the event stream (one per
    # server), and the Runner's listener that publishes on it.
    hub: PresenceHub = field(default_factory=PresenceHub, repr=False)
    events: CompanionEvents | None = field(default=None, repr=False)
    _ticking: asyncio.Task[None] | None = field(default=None, repr=False)
    # V3-P87: speaking first (HIMARI_PROACTIVE…), and the listener that ends each wake with its job
    proactive: ProactiveSettings = field(default_factory=ProactiveSettings)
    _wake_events: WakeEvents | None = field(default=None, repr=False)
    # V3-P87 §7: the periodic jobs (purge, proactive), the devices' last inputs and the scheduler
    ticker: Ticker = field(default_factory=Ticker, repr=False)
    inputs: Inputs | None = field(default=None, repr=False)
    scheduler: ProactiveScheduler | None = field(default=None, repr=False)
    devices: Devices = field(init=False, repr=False)  # V3-P83 §4: pairing and device tokens

    def __post_init__(self) -> None:
        self.devices = DeviceRegistry(self.data)

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
        memory_auto: bool = True,  # V3-P85 §12: HIMARI_MEMORY_AUTO=0 turns memory passes off
        memory_model: str | None = None,  # HIMARI_MEMORY_MODEL: their model; None: they do not run
        memory_idle_minutes: int = 10,  # HIMARI_MEMORY_IDLE_MINUTES: quiet before a pass
        memory_daily_tokens: int = 1_000_000,  # HIMARI_MEMORY_DAILY_TOKENS: each principal's daily budget
        conversation_search: bool = True,  # V3-P72 §9: HIMARI_CONVERSATION_SEARCH=0 turns its Tools off
        streaming: bool = True,  # V3-P73 §9: HIMARI_STREAMING=0 asks no model for a stream
        companion: bool = True,  # V3-P83 §3: HIMARI_COMPANION=0 refuses pairing and device tokens
        time_zone: str = DEFAULT_TIME_ZONE,  # V3-P83 §6.4: HIMARI_TIME_ZONE, the zone the model is told the time in
        machine_socket: str | None = None,  # V3-P74: HIMARI_MACHINE_SOCKET; None: no machine Environment
        machine_principals: Iterable[str] = ("operator",),  # whose new Conversations use it (Q1)
        proactive: ProactiveSettings | None = None,  # V3-P87 §11: HIMARI_PROACTIVE…; None: off
    ) -> Application:
        speaking_first = proactive if proactive is not None else ProactiveSettings()
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
            saved_defaults = read_defaults(root / "control/mcp-defaults.json")
            mcp.statuses = tuple(replace(s, enabled_by_default=saved_defaults.get(s.source, s.enabled_by_default))
                                 for s in mcp.statuses)
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
            machine = changes = None
            if machine_socket is not None:
                # V3-P76 §4.2, §5: the agent's file changes, recorded for review, with versions of their own.
                changes = ChangeStore(root / "file-changes", VersionStore(root / "file-versions-agent"))
                if cut_short := await asyncio.to_thread(changes.recover):
                    log.warning("%d file change(s) were cut short by a stop; they are shown as unknown", cut_short)
                machine = MachineEnvironment(machine_socket,
                                             files=InsideFiles(store.blobs, SqliteData(store), file_max_bytes),
                                             documents=reader, registry=SqliteData(store), changes=changes)
                try:
                    hello = await machine.hello()
                    log.info("machine runner: sandbox %s, HIMARI home %s", hello.get("state"), hello.get("himari_home"))
                    if not await machine.keeps():
                        log.warning("the machine runner is older than HIMARI (protocol %s): the agent's file changes "
                                    "are not recorded for review until it is restarted", hello.get("version"))
                except Exception as error:  # noqa: BLE001 — the Tools say so until it answers
                    log.warning("the machine runner does not answer yet (%s)", type(error).__name__)
            views = await _image_views(root, sandbox)
            base = DEFAULT_INSTRUCTIONS if instructions is None else instructions
            zones = frozenset(available_timezones())
            hub = PresenceHub(time_zones=zones.__contains__)  # a device's zone that is not known is ignored (§5)
            presence = CompanionPresence(DeviceRegistry(SqliteData(store)), hub, time_zone, enabled=companion)
            runtime = build_runtime(provider, store=store, environments=[inside, *([machine] if machine else [])],
                                    tools=[*tools, *mcp.handlers, *mcp.resource_tools],
                                    models=catalog,
                                    instructions=base,  # V3-P89: the base; the sections are the Prompt module's
                                    system=SystemSpec(companion=companion, attachments=uploads_work,
                                                      file_references=machine is not None,
                                                      proactive=companion and speaking_first.enabled,
                                                      documents=_documents_notes(reader, toolkit, scripts == "on")),
                                    external_defaults=mcp.defaults,
                                    source_notes=[SourceNote(source, note.placement, note.text)
                                                  for source, note in mcp.notes.items()],
                                    max_runs_per_drive=max_runs, turn_budget=turn_budget,
                                    no_progress_runs=turn.no_progress_runs, image_views=views,
                                    file_max_bytes=file_max_bytes, memory=memory,
                                    conversation_search=conversation_search, streaming=streaming,
                                    time_zone=time_zone,
                                    presence=presence)
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
        events = CompanionEvents(hub, runtime.data)
        wake_events = WakeEvents(runtime.data)
        listeners = _JobListeners(events, wake_events)
        runner = BackgroundRunner(runtime.driver, unattended_runs=unattended_runs, seen_seconds=turn.seen_seconds,
                                  listener=listeners)
        application = cls(root, store, runtime.data, runtime, runner, inside, state, model_id,
                          default_budget.limit_tokens, secret, mcp, turn, tuple(unverified), trash_days,
                          file_max_bytes, uploads_work, views, tuple(image_models), reader, toolkit,
                          memory_enabled=memory, conversation_search_enabled=conversation_search,
                          streaming_enabled=streaming, companion_enabled=companion, machine=machine,
                          changes=changes,
                          machine_principals=frozenset(PrincipalId(p) for p in machine_principals),
                          hub=hub, events=events)
        application.time_zone = time_zone
        application.proactive = speaking_first
        application._wake_events = wake_events
        assert isinstance(runtime.driver, Driver)
        await settle_cut_wakes(runtime.data, runtime.driver)  # V3-P87 §4.4: before any new wake
        settings, application.auto_memory = memory_pass_settings(
            provider, memory=memory, auto=memory_auto, model_id=memory_model, idle_minutes=memory_idle_minutes,
            daily_tokens=memory_daily_tokens, time_zone=time_zone)
        if settings is not None:
            async def zone_for(scope: TrustedScope, origin: MessageOrigin | None) -> str:
                """V3-P85 §4.2: the zone of the device the part's last message came from, else the server's."""
                found = (await presence.read(scope.principal_id, origin)).time_zone
                return found if found in zones else time_zone

            passes = MemoryPasses(runtime.data, provider, settings, _RunnerActivity(runner), zone_for=zone_for)
            listeners.listeners.append(passes)
            passes.on_applied = events.memory_applied
            application.memory_passes = passes
            await passes.start()
        application._ticking = asyncio.create_task(hub.run(), name="presence-tick")
        if cleanup_seconds is not None:
            application.ticker.add("purge", cleanup_seconds, application._purge_round)
        if companion:  # V3-P87 §5.2: the devices' last inputs, kept whether or not HIMARI speaks first
            application.inputs = Inputs(runtime.data)
            hub.on_input = application.inputs.seen
            if speaking_first.enabled:
                ticker = application.ticker
                scheduler = ProactiveScheduler(runtime.data, hub, application.devices, runner, application.inputs,
                                               speaking_first, time_zone, zones=zones,
                                               poke=lambda after: ticker.poke("proactive", after))
                hub.on_returned = scheduler.returned
                application.scheduler = scheduler
                ticker.add("proactive", PROACTIVE_SECONDS, scheduler.run)
        application.ticker.start()
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

    @property
    def mcp_defaults_revision(self) -> str:
        return hashlib.sha256(json.dumps(self.mcp.defaults, sort_keys=True).encode()).hexdigest()

    async def set_mcp_default(self, source: str, enabled: bool, revision: str) -> None:
        """Persist an operator default without reconnecting or stopping any server.

        Conversations with no override follow it on their next model request;
        explicit per-conversation settings remain unchanged.
        """
        async with self._mcp_defaults_lock:
            if source not in self.mcp.defaults:
                raise UnknownToolSource(source)
            if revision != self.mcp_defaults_revision:
                raise McpDefaultsConflict("MCP defaults changed; reload them before saving")
            values = read_defaults(self.data_root / "control/mcp-defaults.json")
            # Removed servers cannot accumulate in the override file.
            values = {key: value for key, value in values.items() if key in self.mcp.defaults}
            values[source] = enabled
            write_defaults(self.data_root / "control/mcp-defaults.json", values)
            self.runtime.access.set_default(source, enabled)
            self.mcp.statuses = tuple(replace(s, enabled_by_default=enabled) if s.source == source else s
                                      for s in self.mcp.statuses)

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

    # --- Folders on this server as Workspaces (V3-P74 §10) ---------------------------------

    def _machine_for(self, principal: PrincipalId) -> MachineEnvironment:
        if self.machine is None or principal not in self.machine_principals:
            raise WorkspaceRefused("unavailable", "Folders on this server are not offered to you.")
        return self.machine

    def explorer(self, principal: PrincipalId) -> Explorer:
        """The web's file explorer (V3-P75 §5), for a principal folders are offered to."""
        try:
            machine = self._machine_for(principal)
        except WorkspaceRefused:
            raise ExplorerRefused("not_found", "Not available.") from None
        if self._explorer is None:
            self._explorer = Explorer(machine, self.views, VersionStore(self.data_root / "file-versions"),
                                      None if self.changes is None else self.changes.versions,
                                      VersionStore(self.data_root / "file-versions-pdf", PDF_VERSIONS_BYTES,
                                                   PDF_MAX_BYTES))
        return self._explorer

    # --- the review of the agent's file changes (V3-P76 §5, §6) -----------------------------

    async def _review(self, scope: TrustedScope) -> ChangeReview:
        """For a Conversation of the caller's (404 otherwise), when folders
        are offered to them."""
        await self.data.read_conversation(scope)
        explorer = self.explorer(scope.principal_id)
        if self.changes is None:
            raise ExplorerRefused("not_found", "Not available.")
        return ChangeReview(self.changes, explorer)

    @asynccontextmanager
    async def _reviewing(self, scope: TrustedScope) -> AsyncIterator[ChangeReview]:
        """Accepting or reverting (§6): under the Conversation's lifecycle
        lock (no trash meanwhile), refused while a turn runs or an input is
        being accepted, and no turn starts until it is done."""
        async with self._exclusive(scope.conversation_id):
            review = await self._review(scope)
            with ExitStack() as held:
                try:
                    held.enter_context(self.runner.hold(scope))
                except ControlError as error:
                    if error.code is not ControlErrorCode.BUSY:
                        raise
                    raise ExplorerRefused("busy", "HIMARI is working in this conversation: wait until it has "
                                                  "finished.") from None
                yield review

    async def file_changes(self, scope: TrustedScope) -> ChangesView:
        return await (await self._review(scope)).view(scope.conversation_id)

    async def file_change_diff(self, scope: TrustedScope, path: str) -> ChangeDiff:
        return await (await self._review(scope)).diff(scope.conversation_id, path)

    async def accept_file_changes(self, scope: TrustedScope, paths: Sequence[str] | None) -> None:
        """``paths``: None accepts all (the hidden ones too, §10)."""
        async with self._reviewing(scope) as review:
            await review.accept(scope.conversation_id, paths)

    async def revert_file_change(self, scope: TrustedScope, path: str, seen: tuple[int, ...] | None) -> None:
        """ChangeConflict: changed by something else since; ``seen`` (what
        the owner saw: ``()`` no file) reverts anyway if it is still so."""
        async with self._reviewing(scope) as review:
            await review.revert(scope.conversation_id, path, seen)

    async def revert_all_file_changes(self, scope: TrustedScope) -> RevertedAll:
        async with self._reviewing(scope) as review:
            return await review.revert_all(scope.conversation_id)

    # --- conversations in a folder (V3-P75 §6) ----------------------------------------

    async def folder_conversations(self, principal: PrincipalId, path: str) -> FolderConversations:
        """The folder's real path, its registration and the Conversations
        working in it (§6.1). ExplorerRefused (not_found): folders are not
        offered (§4). WorkspaceRefused: the runner does not answer, or the
        path is not a folder the user can see."""
        try:
            machine = self._machine_for(principal)
        except WorkspaceRefused:
            raise ExplorerRefused("not_found", "Not available.") from None
        try:
            found, _ = await machine.call(runner_api.STAT, {"path": path, "cwd": "/"})
            himari_home = str((await machine.hello())["himari_home"])
        except RunnerError as error:
            raise WorkspaceRefused("not_usable", f"{path}: {error}") from None
        except runner_api.RunnerUnavailable:
            raise WorkspaceRefused("unavailable", "The machine runner does not answer; try again later.") from None
        real = str(found["path"])
        if found["kind"] != "directory":
            raise WorkspaceRefused("not_usable", f"{real} is not a folder.")
        conversations_root = posixpath.join(himari_home, "conversations")
        if posixpath.dirname(real) == conversations_root:  # one Conversation's own folder: that one
            scope = TrustedScope(principal, ConversationId(posixpath.basename(real)))
            try:
                summary = await self.data.read_conversation(scope)
            except DataError:
                return FolderConversations(real, None, (), own=True, refused="This folder belongs to a conversation "
                                           "that is gone.")
            return FolderConversations(real, None, () if summary.trashed_at else (summary,), own=True)
        known = await self.data.list_workspaces(principal, MACHINE)
        workspace = next((w for w in known if w.location == real), None)
        refused = None
        if workspace is None:
            try:  # the same folder registered at another path (a bind mount) counts as this one
                _, _, workspace = await self._check_folder(machine, real, known)
            except WorkspaceRefused as error:
                refused = error.message
        conversations: tuple[ConversationSummary, ...] = ()
        if workspace is not None:
            conversations = tuple(await self.data.list_conversations_working_in(principal, workspace.workspace_id,
                                                                                 limit=MAX_LIST_LIMIT))
        above: list[tuple[RegisteredWorkspace, int]] = []
        for other in sorted((w for w in known if real.startswith(w.location.rstrip("/") + "/")),
                            key=lambda w: len(w.location), reverse=True):
            count = len(await self.data.list_conversations_working_in(principal, other.workspace_id,
                                                                       limit=MAX_LIST_LIMIT))
            if count:
                above.append((other, count))
        return FolderConversations(real, workspace, conversations, refused=refused, above=tuple(above))

    async def open_folder_conversation(self, principal: PrincipalId, path: str, *,
                                       new: bool = False) -> tuple[ConversationId, bool]:
        """§6.1–6.2: the folder's most recent Conversation, or (none, or
        ``new``) a new one working in it: registered first if it is not
        (reused if it is, at any path), then created, attached and made the
        working folder. A Conversation made here whose folder could not be
        attached is deleted again; the registration stays. Whether one was made."""
        folder = await self.folder_conversations(principal, path)
        if new or folder.own:
            return await self._open_in(principal, folder, new)
        # Two at once for the same folder must not both find none and both make one (PR #93 review B1):
        # one at a time per folder, and the folder read again once it is this one's turn.
        async with self._opening_folder(folder.path):
            return await self._open_in(principal, await self.folder_conversations(principal, folder.path), new)

    @asynccontextmanager
    async def _opening_folder(self, real: str) -> AsyncIterator[None]:
        lock, users = self._opening.get(real, (asyncio.Lock(), 0))
        self._opening[real] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            lock, users = self._opening[real]
            if users == 1:
                del self._opening[real]
            else:
                self._opening[real] = (lock, users - 1)

    async def _open_in(self, principal: PrincipalId, folder: FolderConversations,
                       new: bool) -> tuple[ConversationId, bool]:
        if folder.own:
            if not folder.conversations:
                raise WorkspaceRefused("not_usable", folder.refused or "That conversation is in the trash.")
            if new:
                raise WorkspaceRefused("not_usable", "This is a conversation's own folder; open that conversation, or "
                                                     "start a new one in a folder of your own.")
            return folder.conversations[0].conversation_id, False
        if folder.conversations and not new:
            return folder.conversations[0].conversation_id, False
        if folder.refused is not None:
            raise WorkspaceRefused("not_usable", folder.refused)
        workspace = folder.workspace or await self._register(principal, folder.path, None, None, reuse=True)
        conversation = await self.create_conversation(principal)
        scope = TrustedScope(principal, conversation)
        try:
            await self.attach_workspace(scope, workspace_id=workspace.workspace_id)
        except BaseException:
            try:
                await self.trash(scope)
                await self.purge(scope)
            except Exception:  # noqa: BLE001 — the trash keeps it, and its regular purge
                log.exception("a conversation made for %s could not be removed again", folder.path)
            raise
        return conversation, True

    async def machine_workspaces(self, principal: PrincipalId) -> tuple[RegisteredWorkspace, ...]:
        """The principal's registered folders, oldest first."""
        self._machine_for(principal)
        return await self.data.list_workspaces(principal, MACHINE)

    async def register_workspace(self, principal: PrincipalId, path: str, label: str | None = None,
                                 note: str | None = None) -> RegisteredWorkspace:
        """The runner checks the folder first (§10.1): it exists, the user may
        write it, it is not hidden; Data keeps its real path and its
        filesystem (a cloud-synced one is told to the model, §14)."""
        return await self._register(principal, path, label, note, reuse=False)

    async def _register(self, principal: PrincipalId, path: str, label: str | None, note: str | None, *,
                        reuse: bool) -> RegisteredWorkspace:
        """Registers the folder, or (``reuse``) gives the registration that is
        already this folder at any path."""
        machine = self._machine_for(principal)
        async with self._registering:
            known = await self.data.list_workspaces(principal, MACHINE)
            real, filesystem, same = await self._check_folder(machine, path, known)
            if same is not None:
                if reuse:
                    return same
                raise WorkspaceRefused("duplicate", f"This folder is registered already, as {same.location}.")
            try:
                return await self.data.register_workspace(principal, MACHINE, real,
                                                          label or posixpath.basename(real) or real, note, filesystem)
            except DataError as error:
                if error.code is DataErrorCode.DUPLICATE_IDENTITY:
                    raise WorkspaceRefused("duplicate", f"{real} is registered already.") from None
                raise

    async def _check_folder(self, machine: MachineEnvironment, path: str, known: Sequence[RegisteredWorkspace]
                            ) -> tuple[str, str | None, RegisteredWorkspace | None]:
        """The runner's check: the real path, its filesystem, and the
        registration that is already this folder (at that path, or another)."""
        if not isinstance(path, str) or not path.strip():
            raise WorkspaceRefused("not_usable", "Give the folder's path.")
        try:
            checked = await machine.check_workspace(path.strip(), [w.location for w in known])
        except MachineUnavailable:
            raise WorkspaceRefused("unavailable", "The machine runner does not answer; try again later.") from None
        except RunnerError as error:
            raise WorkspaceRefused("not_usable", f"{path.strip()}: {error}") from None
        real = str(checked["path"])
        same_as = checked.get("same_as")
        same = next((w for w in known if w.location == real or w.location == same_as), None)
        filesystem = checked.get("filesystem")
        return real, filesystem if isinstance(filesystem, str) else None, same

    async def update_workspace(self, principal: PrincipalId, workspace_id: str, label: str,
                               note: str | None) -> RegisteredWorkspace:
        self._machine_for(principal)
        return await self.data.update_workspace(principal, WorkspaceId(workspace_id), label=label, note=note)

    async def unregister_workspace(self, principal: PrincipalId, workspace_id: str) -> None:
        """Only while no Conversation has it attached; the folder stays."""
        self._machine_for(principal)
        await self.data.unregister_workspace(principal, WorkspaceId(workspace_id))

    async def conversation_workspaces(self, scope: TrustedScope) -> tuple[AttachedFolder, ...]:
        """This Conversation's folders (its own first); empty when it does not
        use the machine Environment. Authorizes like any read."""
        try:
            found = await self.data.read_conversation_workspaces(scope, MACHINE)
        except DataError as error:
            if error.code is DataErrorCode.NOT_FOUND:
                await self.data.read_conversation(scope)  # NOT_AUTHORIZED stays that
                return ()
            raise
        folders: list[AttachedFolder] = []
        for item in found.attached:
            working = item.binding.binding_id == found.working
            if item.workspace is not None:
                w = item.workspace
                folders.append(AttachedFolder(item.binding.binding_id, w.location, w.label, w.note, working, False,
                                              w.workspace_id))
                continue
            path = None
            if self.machine is not None:
                try:
                    path = await self.machine.folder(scope.conversation_id, item.binding.cwd_relative or "")
                except MachineUnavailable:
                    path = None
            folders.append(AttachedFolder(item.binding.binding_id, path, OWN_LABEL, None, working, True, None))
        return tuple(folders)

    async def attach_workspace(self, scope: TrustedScope, *, workspace_id: str | None = None, path: str | None = None,
                               label: str | None = None) -> tuple[AttachedFolder, ...]:
        """Attach a registered folder, or a path (registered first unless it
        is already); it becomes the working directory (§10.4). Takes effect
        from the next Tool call: one running keeps its folders."""
        self._machine_for(scope.principal_id)
        if not await self.conversation_workspaces(scope):  # NOT_AUTHORIZED first; nothing registered for nothing
            raise WorkspaceRefused("not_machine", NOT_MACHINE)
        if workspace_id is None:
            if path is None:
                raise WorkspaceRefused("not_usable", "Give a registered workspace or a folder's path.")
            workspace_id = (await self._register(scope.principal_id, path, label, None, reuse=True)).workspace_id
        try:
            await self.data.attach_workspace(scope, WorkspaceId(workspace_id))
        except DataError as error:
            if error.code is DataErrorCode.INVALID_STATE_TRANSITION:
                raise WorkspaceRefused("not_machine", NOT_MACHINE) from None
            if error.code is DataErrorCode.DUPLICATE_IDENTITY:
                raise WorkspaceRefused("duplicate", "That folder is attached to this conversation already.") from None
            raise
        return await self.conversation_workspaces(scope)

    async def detach_workspace(self, scope: TrustedScope, binding_id: str) -> tuple[AttachedFolder, ...]:
        try:
            await self.data.detach_workspace(scope, BindingId(binding_id))
        except DataError as error:
            if error.code is DataErrorCode.INVALID_STATE_TRANSITION:
                raise WorkspaceRefused("own_folder", "The conversation's own folder stays attached.") from None
            raise
        return await self.conversation_workspaces(scope)

    async def set_working_workspace(self, scope: TrustedScope, binding_id: str) -> tuple[AttachedFolder, ...]:
        await self.data.set_working_workspace(scope, BindingId(binding_id))
        return await self.conversation_workspaces(scope)

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

    async def instructions(self, principal: PrincipalId,
                           scope: InstructionScope = InstructionScope.USER) -> Instructions:
        """The instructions the user wrote for every Conversation, or (V3-P89 §4)
        their companion persona, which is told only with the companion on."""
        return await self.data.read_instructions(principal, scope)

    async def set_instructions(self, principal: PrincipalId, text: str, revision: int,
                               scope: InstructionScope = InstructionScope.USER) -> Instructions:
        """Saved at once, used from each Conversation's next turn (§5); a turn
        under way keeps the text it started with. DataError VERSION_CONFLICT
        (written since ``revision`` was read), TOO_LARGE or INVALID_ARGUMENT."""
        return await self.data.write_instructions(principal, scope, text, revision)

    # --- memory (V3-P71) ----------------------------------------------------------------

    async def memories(self, principal: PrincipalId) -> tuple[Memory, ...]:
        """The principal's notes, by category, newest first within each."""
        return await self.data.list_memories(principal)

    async def add_memory(self, principal: PrincipalId, category: MemoryCategory, text: str,
                         dates: NoteDates | None = None) -> Memory:
        """A note the user writes; used from each Conversation's next turn.
        DataError TOO_LARGE, LIMIT_EXCEEDED or INVALID_ARGUMENT."""
        return await self.data.save_memory(principal, category, text, dates=dates)

    async def edit_memory(self, principal: PrincipalId, memory_id: int, category: MemoryCategory, text: str,
                          revision: int, dates: NoteDates | None = None) -> Memory:
        """The user's rewrite: their text from now on, its source kept (§4.3);
        ``dates`` None keeps the note's (V3-P85). DataError NOT_FOUND,
        VERSION_CONFLICT, TOO_LARGE or INVALID_ARGUMENT."""
        return await self.data.save_memory(principal, category, text, memory_id=memory_id, expected_revision=revision,
                                           dates=dates)

    # --- memory passes (V3-P85 §10, §12) ---------------------------------------------

    async def memory_pass_lines(self, scope: TrustedScope) -> tuple[PassLine, ...]:
        """§10.1: this Conversation's applied passes that did something to show,
        each item with whether an Undo is offered now. Authorizes like any read."""
        now = datetime.now(UTC)
        found = await self.data.applied_pass_items(scope, limit=PASS_LINES_LIMIT)  # two reads, however many (review)
        targets = [int(r.target.removeprefix("m")) for _, records in found for r in records
                   if r.target is not None and r.target.removeprefix("m").isdigit()]
        states = await self.data.memory_revisions(scope.principal_id, targets)
        lines: list[PassLine] = []
        for memory_pass, records in found:
            items = tuple(item for record in records if (item := self._pass_line_item(record, states, now)) is not None)
            if items:
                lines.append(PassLine(memory_pass.pass_id, memory_pass.through_sequence, items))
        return tuple(lines)

    @staticmethod
    def _pass_line_item(record: PassItemRecord, states: Mapping[int, tuple[int, bool]],
                        now: datetime) -> PassLineItem | None:
        planned, previous = _planned(record.planned), _planned(record.previous)
        memory_id = None if record.target is None else int(record.target.removeprefix("m"))
        if record.outcome in ("sensitive_proposed", "kept"):
            text = str(planned.get("text") or "")
            return PassLineItem(record.item, "waiting" if record.outcome == "sensitive_proposed" else "remembered",
                                memory_id, text, planned.get("due_at"), False, _evidence(record))
        action = _pass_action(record)
        if action is None or memory_id is None:
            return None
        loop = (previous if action == "forget" else planned).get("category") == MemoryCategory.LOOP.value
        state = states.get(memory_id)
        if action == "forget":
            undo = (state == (record.written_revision, True) and record.created_at > now - RETIRED_KEEP)
            return PassLineItem(record.item, "closed_loop" if loop else "forgot", memory_id,
                                str(previous.get("text") or ""), previous.get("due_at"), undo, _evidence(record))
        undo = state == (record.written_revision, False)
        shown = "open_loop" if loop and action == "add" else "remembered" if action == "add" else "updated"
        return PassLineItem(record.item, shown, memory_id, str(planned.get("text") or ""), planned.get("due_at"), undo,
                            _evidence(record))

    async def undo_pass_item(self, scope: TrustedScope, pass_id: str, item: int) -> Memory | None:
        """§10.1: undo what one item of this Conversation's pass did, from its
        own record: an added note is deleted (None), a rewrite put back, a
        retired note brought back. NOT_FOUND: no such change, the note gone,
        retired too long ago or its source deleted; VERSION_CONFLICT: changed since;
        LIMIT_EXCEEDED: memory is full."""
        if not any(p.pass_id == pass_id and p.status is PassStatus.APPLIED for p in await self.data.list_passes(scope)):
            raise DataError(DataErrorCode.NOT_FOUND, "no such memory pass here")
        record = next((r for r in await self.data.read_pass_items(pass_id) if r.item == item), None)
        action = None if record is None else _pass_action(record)
        if record is None or action is None or record.target is None or record.written_revision is None:
            raise DataError(DataErrorCode.NOT_FOUND, "no memory change to undo")
        memory_id, principal = int(record.target.removeprefix("m")), scope.principal_id
        if action == "add":
            await self.data.delete_memory(principal, memory_id, record.written_revision)
            return None
        if action == "forget":
            return await self.data.unretire_memory(principal, memory_id, expected_revision=record.written_revision,
                                                   now=datetime.now(UTC))
        before = None if record.previous is None else memory_from_record(record.previous)
        if before is None or before.memory_id != memory_id:
            raise DataError(DataErrorCode.NOT_FOUND, "this change cannot be undone")
        return await self.data.restore_memory(principal, before, expected_revision=record.written_revision)

    async def proposals(self, principal: PrincipalId) -> tuple[Proposal, ...]:
        return await self.data.list_proposals(principal)

    async def decide_proposal(self, principal: PrincipalId, pass_id: str, item: int, keep: bool,
                              dates: NoteDates | None = None) -> Memory | None:
        """``dates``: the user's, in place of the proposal's (a passed date dropped or moved)."""
        return await self.data.decide_proposal(principal, pass_id, item, keep=keep, now=datetime.now(UTC),
                                               dates=dates)

    async def episodes(self, principal: PrincipalId, before: tuple[datetime, int] | None,
                       limit: int) -> tuple[RecentEpisode, ...]:
        return await self.data.page_episodes(principal, before=before, limit=limit)

    async def edit_episode(self, principal: PrincipalId, episode_id: int, text: str, revision: int) -> Episode:
        return await self.data.update_episode(principal, episode_id, text, expected_revision=revision)

    async def delete_episode(self, principal: PrincipalId, episode_id: int, revision: int) -> None:
        await self.data.delete_episode(principal, episode_id, expected_revision=revision)

    async def memory_status(self, principal: PrincipalId) -> AutoMemoryStatus:
        """§10.3: whether memory passes run here and why, today's tokens, the
        last applied pass, and the trouble of the last seven days."""
        passes = self.memory_passes
        now = datetime.now(UTC)
        day_start = passes.day_start(now) if passes is not None else now.replace(hour=0, minute=0, second=0,
                                                                                  microsecond=0)
        summary = await self.data.pass_summary(principal, day_start=day_start, since=now - timedelta(days=7))
        return AutoMemoryStatus(passes is not None, self.auto_memory,
                                None if passes is None else passes.model_id,
                                None if passes is None else passes.daily_tokens, summary)

    async def backfill(self, scope: TrustedScope) -> list[PassOutcome]:
        """§4.9, §12: read a Conversation's part before the upgrade. NOT_AUTHORIZED
        for another's; MemoryPassesOff when they do not run here."""
        await self.data.read_conversation(scope)
        if self.memory_passes is None:
            raise MemoryPassesOff(self.auto_memory)
        return await self.memory_passes.backfill(scope)

    async def retry_pass(self, scope: TrustedScope, pass_id: str) -> PassOutcome:
        await self.data.read_conversation(scope)
        if self.memory_passes is None:
            raise MemoryPassesOff(self.auto_memory)
        return await self.memory_passes.retry(scope, pass_id)

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
        """T0 with the principal's Inside Workspace activated (provisioned
        first) — or, for a machine principal, the machine Environment (V3-P74
        Q1: new Conversations only; its folder is made on first use)."""
        if self.machine is not None and principal in self.machine_principals:
            return await self.data.create_conversation(
                principal, title=title, activations=(InitialActivation(MACHINE, machine_workspace_for(principal)),))
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

    async def upload_screen(self, scope: TrustedScope, chunks: AsyncIterator[bytes]) -> FileRecord:
        """V3-P84 §4.1: a device's screen, staged like an upload: a PNG or
        JPEG by its bytes, at most SCREEN_MAX_BYTES, named here by when it
        came. A body that does not start as one is refused before it is
        stored (PR #126 review C1); one whose header cannot be read after is
        left unreferenced, and the cleanup drops it."""
        await self.data.read_conversation(scope)  # 404 for a foreign id, before any byte is read
        if await self.data.count_staged(scope) >= MAX_STAGED_UPLOADS:
            raise DataError(DataErrorCode.LIMIT_EXCEEDED, f"{MAX_STAGED_UPLOADS} uploads wait to be sent")
        body = await _screen_body(chunks)
        blob = await self.data.blobs.put(scope.principal_id, body, min(SCREEN_MAX_BYTES, self.file_max_bytes))
        image = await self.data.blobs.prepare_image(scope.principal_id, blob, self.views)
        extension = SCREEN_TYPES.get(image.image_type or "")
        if extension is None:
            raise ScreenRefused("not a PNG or JPEG image")
        name = f"screen-{datetime.now(UTC):%Y%m%d-%H%M%S}.{extension}"
        async with self._exclusive(scope.conversation_id):
            return await self.data.stage_upload(scope, blob, name, image.image_type or "", image)

    async def send(self, scope: TrustedScope, text: str, attachments: Sequence[FileId] = (),
                   references: Sequence[ReferenceRequest] = (), origin: MessageOrigin | None = None,
                   screen: FileId | None = None) -> JobStatus:
        """A user message. With attachments (§5.1, under the lifecycle lock):
        1 the T1 checks as a read; 2 refused while a job runs; 3 the uploads
        placed in ``uploads/`` under the Workspace write lock (waited for at
        most PLACE_LOCK_SECONDS); 4 the message submitted, attaching them.
        ``references`` (V3-P76 §3): parts of files the user pointed at, each
        read again and checked by the explorer before anything is sent; a
        stale one refuses the message (ExplorerRefused ``reference_stale``).
        ``origin``: where it was sent from (V3-P83 §4.2), kept on the message.
        ``screen`` (V3-P84 §4.2): a staged PNG or JPEG upload, attached as the
        user's screen and never placed in the Workspace (§4.4); attached once,
        in the message's own transaction (T1)."""
        await self.data.read_conversation(scope)  # 404 before a job is started (or a file read) for a foreign id
        if screen is not None:
            async with self._exclusive(scope.conversation_id):
                (record,) = await self.data.check_attachments(scope, [screen])
                if record.image_type not in SCREEN_TYPES:
                    raise ScreenRefused("not a PNG or JPEG image")
                if self.runner.status(scope).running:
                    raise ControlError(ControlErrorCode.BUSY, scope.conversation_id)
                said = ((TextBlock(text),) if text else ()) + (AttachmentRef(record.file_id, 1, screen=True),)
                return await self.runner.submit(scope, UserMessage(said, origin=origin))
        pointed: tuple[ContentBlock, ...] = ()
        if references:
            explorer = self.explorer(scope.principal_id)
            pointed = tuple([await explorer.reference(r.path, r.identity, r.start, r.end, r.sha256)
                             for r in references])
        if not attachments:
            return await self.runner.submit(scope, UserMessage(((TextBlock(text),) if text else ()) + pointed,
                                                               origin=origin))
        async with self._exclusive(scope.conversation_id):
            records = await self.data.check_attachments(scope, attachments)
            if self.runner.status(scope).running:
                raise ControlError(ControlErrorCode.BUSY, scope.conversation_id)
            workspace_id, cwd = await self._workspace(scope)
            machine = self.machine if self.machine is not None and self.machine.owns(workspace_id) else None
            try:
                if machine is not None:
                    locked = machine.locked(workspace_id, PLACE_LOCK_SECONDS, conversation_id=scope.conversation_id,
                                            cwd_relative=cwd)
                    placer = _machine_placer(machine, scope.conversation_id, cwd)
                else:
                    locked = self.inside.locked(workspace_id, PLACE_LOCK_SECONDS)
                    placer = _inside_placer(self.inside, workspace_id, cwd)
                async with locked:
                    # Read again under the lock: another tab may have placed them (review C4).
                    fresh = await self.data.read_file_records(scope, [(r.file_id, r.version) for r in records])
                    for index, record in enumerate(fresh):
                        await self._place(scope, placer, record, first=index == 0)
            except MachineUnavailable:
                raise FilesUnavailable(scope.conversation_id) from None
            blocks = ((TextBlock(text),) if text else ()) + tuple(AttachmentRef(r.file_id, r.version) for r in records) \
                + pointed
            return await self.runner.submit(scope, UserMessage(blocks, origin=origin))

    async def _place(self, scope: TrustedScope, placer: _Placer, record: FileRecord, *, first: bool) -> None:
        """One upload into ``uploads/`` (§5.2), its path recorded before it is
        given (owner review B1): a crash after the rename leaves a file its
        record names, and a retry finds it there by its bytes.

        Already there with its bytes: nothing to do, and its stored bytes are
        not needed (PR #59 review C11). Otherwise: a durable copy, a free name
        (the recorded one if it is free), the name recorded, then the rename."""
        if record.workspace_path is not None and await placer.holds(
                record.workspace_path, record.size, record.blob.sha256):
            return
        source = self.data.blobs.open(record.blob)  # here: the store is not thread-safe
        temp = await placer.copy_in(source, first)
        recorded = record.workspace_path
        try:
            for _ in range(3):  # a name is taken only if something outside the lock wrote it
                path = await placer.free_path(record.name, recorded)
                if path != recorded:
                    await self.data.set_placement(scope, record.file_id, path)
                    recorded = path
                if await placer.commit_copy(temp, path):
                    return
            raise FilesUnavailable(f"no free name for {record.name}")
        finally:
            await placer.discard_copy(temp)

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
                            await self.data.count_source_memories(scope), await self.data.count_source_episodes(scope))

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
            self._activity_changed(scope)
            return moved

    async def restore(self, scope: TrustedScope) -> None:
        async with self._exclusive(scope.conversation_id):
            await self.data.restore_conversation(scope)  # owner and state first
            self.runner.reopen(scope)
            self._activity_changed(scope)

    async def purge(self, scope: TrustedScope) -> None:
        """§7.2: PURGING recorded (no restore from here on), the
        Conversation's own folder removed, then every Data row."""
        async with self._exclusive(scope.conversation_id):
            trashed = await self.data.begin_purge(scope)  # owner and state first
            workspace = trashed.workspace
            if workspace is not None and self.machine is not None and self.machine.owns(workspace.workspace_id):
                await self.machine.remove_conversation_folder(workspace.workspace_id, scope, workspace.cwd_relative)
            elif workspace is not None:
                await self.inside.remove_conversation_folder(workspace.workspace_id, scope, workspace.cwd_relative)
            await self.data.purge_conversation(scope)
            if self.changes is not None:
                await asyncio.to_thread(self.changes.forget, scope.conversation_id)
            self.runner.forget(scope)
            self.runner.reopen(scope)
            self._activity_changed(scope)

    def _activity_changed(self, scope: TrustedScope) -> None:
        """V3-P83 §2.2: a waiting conversation may have left or come back."""
        if self.events is not None:
            self.events.activity_changed(scope.principal_id)

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

    async def _purge_round(self) -> None:
        """The Ticker's ``purge`` (V3-P87 §7: the old cleanup loop, the same period)."""
        try:
            await self.purge_due()
        except Exception:  # noqa: BLE001 — the next round tries again
            log.exception("trash cleanup failed")
        try:
            await self.clean_files()
        except Exception:  # noqa: BLE001 — the next round tries again
            log.exception("file cleanup failed")
        try:
            await self.clean_memories()
        except Exception:  # noqa: BLE001 — the next round tries again
            log.exception("memory cleanup failed")

    async def clean_memories(self, now: datetime | None = None) -> None:
        """V3-P85 §5.1, §5.2, §5.4, §4.9: stale loops, expired facts, retired
        notes and proposals left too long; then memory passes look for work
        they missed (§4.1)."""
        cleaned = await self.data.clean_memories(datetime.now(UTC) if now is None else now)
        if any((cleaned.stale_loops, cleaned.expired_facts, cleaned.retired, cleaned.proposals)):
            log.info("memory cleanup: %d stale loop(s), %d expired fact(s), %d retired note(s), %d proposal(s)",
                     cleaned.stale_loops, cleaned.expired_facts, cleaned.retired, cleaned.proposals)
        if self.memory_passes is not None:
            self.memory_passes.scan_soon()

    async def close(self, grace_seconds: float = 30.0) -> None:
        await self.ticker.close()
        for task in (self._ticking,):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)  # its own cancellation, not ours
        if self.memory_passes is not None:
            await self.memory_passes.close()  # a pass cut short is counted at the next start (V3-P85 §4.5)
        try:
            await self.runner.shutdown(grace_seconds)
            if self.events is not None:
                await self.events.aclose()
            if self._wake_events is not None:
                await self._wake_events.aclose()
            if self.inputs is not None:
                await self.inputs.aclose()
        finally:
            try:
                await self.mcp.aclose()
            finally:
                if self.toolkit is not None:
                    if isinstance(self.inside.sandbox, SandboxBackend):
                        self.inside.sandbox.toolkit_fd = None  # the number may be reused once closed (review N8)
                    self.toolkit.close()
                self.store.close()

    # --- speaking first (V3-P87) ----------------------------------------------------------

    async def try_wake(self, principal: PrincipalId) -> tuple[ConversationId, JobStatus]:
        """§11: open a TRY wake now in the most recent conversation, spoken on the
        current device — past the user's quiet, the quiet hours, gap and daily
        limit (and the ten minutes after a message), never while a job runs or
        in a conversation that is not settled. ProactiveRefused."""
        if not (self.proactive.enabled and self.companion_enabled):
            raise ProactiveRefused("proactive_off")
        device = self.hub.current(principal)
        if device is None:
            raise ProactiveRefused("no_device")
        recent = await self.data.most_recent_user_conversation(principal)
        if recent is None:
            raise ProactiveRefused("no_conversation")
        if self.runner.running(principal):  # §6.1: never while any of the user's conversations works
            raise ProactiveRefused("busy")
        scope = TrustedScope(principal, recent.conversation_id)
        try:
            status = await self.runner.submit_wake(scope, (WakeReason(WakeKind.TRY),), device)
        except ControlError as error:
            raise ProactiveRefused(error.code.value) from None
        return recent.conversation_id, status

    async def proactive_status(self, principal: PrincipalId) -> dict[str, object]:
        """§11: the switch, where HIMARI would speak now, today's count, the next loop and the last look."""
        quiet = self.proactive
        status: dict[str, object] = {"enabled": self.proactive.enabled and self.companion_enabled,
                                     "told": self.proactive.describe(), "current_device": self.hub.current(principal),
                                     "quiet_hours": None if quiet.quiet_start is None or quiet.quiet_end is None
                                     else f"{quiet.quiet_start:%H:%M}-{quiet.quiet_end:%H:%M}"}
        if self.scheduler is not None:
            status.update(await self.scheduler.status(principal))
        return status

    def check_layout(self) -> None:
        """Re-verify the private directories (e.g. before serving)."""
        for path in (self.data_root, self.data_root / "workspaces", self.data_root / "control"):
            check_private_dir(str(path))


@dataclass(frozen=True)
class _Placer:
    """One upload's steps (V3-P66 §5.2) in a Conversation's folder: Inside's or the machine's."""

    holds: Callable[[str, int, str], Awaitable[bool]]
    copy_in: Callable[[BinaryIO, bool], Awaitable[str]]
    free_path: Callable[[str, str | None], Awaitable[str]]
    commit_copy: Callable[[str, str], Awaitable[bool]]
    discard_copy: Callable[[str], Awaitable[None]]


def _inside_placer(inside: InsideEnvironment, workspace_id: WorkspaceId, cwd: str) -> _Placer:
    return _Placer(
        holds=lambda path, size, sha256: inside.holds(workspace_id, cwd, path, size, sha256),
        copy_in=lambda source, clear: inside.copy_in(workspace_id, cwd, source, clear=clear),
        free_path=lambda name, preferred: inside.free_path(workspace_id, cwd, name, preferred),
        commit_copy=lambda temp, path: inside.commit_copy(workspace_id, cwd, temp, path),
        discard_copy=lambda temp: inside.discard_copy(workspace_id, cwd, temp),
    )


def _machine_placer(machine: MachineEnvironment, conversation_id: str, cwd: str) -> _Placer:
    return _Placer(
        holds=lambda path, size, sha256: machine.holds(conversation_id, cwd, path, size, sha256),
        copy_in=lambda source, clear: machine.copy_in(conversation_id, cwd, source, clear=clear),
        free_path=lambda name, preferred: machine.free_path(conversation_id, cwd, name, preferred),
        commit_copy=lambda temp, path: machine.commit_copy(conversation_id, cwd, temp, path),
        discard_copy=lambda temp: machine.discard_copy(conversation_id, cwd, temp),
    )


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
