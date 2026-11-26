"""The durable P0 application the Web server and CLI are built on.

One data root, private to the server user::

    <data_root>/                 0700
        himari.db (+ .lock)      SQLite Data (V3-P42), 0600
        control/operator.secret  the local operator's credential (0600)
        mcp.json                 optional: MCP servers to connect (0600, V3-P53)
        conversations/<id>/      each Conversation's own folder (0700, V3-P109)

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
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any, BinaryIO
from zoneinfo import ZoneInfo, available_timezones

import httpx

from himari.bootstrap import Runtime, build_runtime
from himari.bootstrap.interfaces import (
    ConfiguredInterfaces,
    ConnectionGenerationRequired as ConnectionGenerationRequired,
    StaleConnectionGeneration as StaleConnectionGeneration,
    device_input_negotiation,
)
from himari.bootstrap.operator import check_private_dir, ensure_operator_secret, ensure_private_dir
from himari.bootstrap.mcp_defaults import McpDefaultsConflict, read_defaults, write_defaults
from himari.common import (
    AttachmentRef,
    CallId,
    CallRef,
    ConversationId,
    FileId,
    PrincipalId,
    RunId,
    TextBlock,
    TrustedScope,
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
from himari.control.driver import MAX_RUNS, Driver
from himari.control.runner import BackgroundRunner
from himari.life.api import (
    DEFAULT_DAILY_TOKENS as LIFE_DAILY_TOKENS,
    DEFAULT_WRITE_AT,
    LifeSettings,
    MindModel,
    SessionInput,
)
from himari.life.day import session_title
from himari.life.keeper import DAY_SECONDS, DayKeeper
from himari.life.work import next_plan
from himari.life.worker import LifeWriter
from himari.bootstrap.world import WEATHER_SECONDS, search_tool, topic_search
from himari.world.topics import SearchTopics
from himari.world.weather import OpenMeteoWeather
from himari.memory.api import MemoryPassSettings, PassOutcome
from himari.memory.worker import MemoryPasses
from himari.data.api import (
    NO_PERSONA,
    RETIRED_KEEP,
    AsleepAnswer,
    LifeJob,
    LifeKind,
    LifeOrigin,
    PlanWrite,
    SleepRecord,
    SleepState,
    SleepView,
    BondSnapshot,
    BondStage,
    BondStanding,
    BondTag,
    Episode,
    HimariMood,
    LifeEntry,
    live_mood,
    Persona,
    NoteDates,
    PassItemRecord,
    PassStatus,
    PassSummary,
    Proposal,
    RecentEpisode,
    memory_from_record,
    MessageOrigin,
    OriginKind,
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
    ViewMaker,
    ViewState,
    ModelSetting,
    ToolExecutionRecord,
    ToolExecutionStatus,
    UserMessage,
    clean_file_name,
    clean_media_type,
    view_state,
    WakeKind,
    WakeReason,
)
from himari.data.sqlite import SqliteData, SqliteStore
from himari.bootstrap.companion import CompanionEvents, CompanionPresence
from himari.bootstrap.interfaces import ConfiguredInterfaces
from himari.bootstrap.screens import ScreenRequests
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
from himari.imaging import ImageViews, runtime_roots
# For the interfaces (V3-P66 §9): how placing an upload can be refused.
from himari.environment.machine import FolderBusy as FolderBusy
from himari.environment.machine import (
    MACHINE,
    MachineEnvironment,
    MachineUnavailable,
)
from himari.environment.machine.results import ToolFiles
from himari.sandbox import identity
from himari.sandbox.api import SandboxAdmissionError, SandboxState, ServerProfile
from himari.sandbox.backend import discover_capability, supports_tmp_size
from himari.sandbox.server import ServerSandbox
from himari.mcp.config import CONFIG_NAME, load_config
from himari.mcp.jsonrpc import McpError
from himari.mcp.prompts import PromptContentError, prompt_text
from himari.mcp.tools import McpConnection, PromptArgument, SourceStatus, connect
from himari.prompt.api import (
    DOCUMENTS_READ_NOTE,
    LIFE_READ_DAYS,
    ContextBudget,
    HimariNow,
    SourceNote,
    SystemSpec,
    doing_now,
)
from himari.prompt.composer import Composer
from himari.prompt.system import base_instructions
from himari.provider.api import ProviderAdapter
from himari.tool.api import ToolHandler
from himari.tool.memory import MEMORY_FORGET, MEMORY_SAVE
from himari.tool.schema import check_tool_input_schema

DATABASE_NAME = "himari.db"
CONVERSATIONS_NAME = "conversations"  # V3-P109 §3 D1: <data root>/conversations/<conversation id>
DEFAULT_TRASH_DAYS = 30  # V3-P65 Q1 (the owner's choice, 2026-10-21)
DEFAULT_TIME_ZONE = "Asia/Seoul"  # V3-P83 §6.4: HIMARI_TIME_ZONE when unset
CLEANUP_SECONDS = 3600.0  # how often trash past its days is purged
TRASH_STOP_SECONDS = 30.0  # how long a running job may take to stop before a trash is refused (§7.1)
BUILT_IN_NAMESPACES = frozenset({"inside", "machine"})  # HIMARI's own Tool ids (inside: old Conversations, V3-P101)
PLACE_LOCK_SECONDS = 60.0  # how long sending waits for the folder's lock (V3-P66 review N4)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TurnSettings:
    """How a turn is guarded (V3-P62), as this server runs it: one Run limit."""

    max_runs: int = MAX_RUNS


# Default request budget: the model's window, capped so that a long
# Conversation is compacted before each request costs a whole large window.
DEFAULT_CONTEXT_TOKENS = 200_000
DEFAULT_COMPANION_MODEL = "deepseek/deepseek-v4.1-flash-1017"
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


def check_tool_sources(data_root: str | os.PathLike[str], environ: Mapping[str, str]) -> None:
    """Fails (ValueError) on an ``mcp.json`` that ``Application.open`` would
    refuse, so a server can report it before starting."""
    load_config(os.path.join(os.path.abspath(data_root), CONFIG_NAME), environ)


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
    companion: bool = False  # all companion features verified (V3-P83/84/87/90/91)


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


async def _documents(root: Path, sandbox: bool, documents_dir: str | None,
                     owners: frozenset[int]) -> tuple[Toolkit | None, DocumentReader | None, str]:
    """V3-P70 §6.4 steps 1-2: the toolkit checked on its descriptor, then
    the reader in its own server sandbox, checked with the probe documents.
    The toolkit comes back only with a working reader; the text says why
    there is none."""
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


def _documents_notes(reader: DocumentReader | None, toolkit: Toolkit | None) -> tuple[str, ...]:
    """V3-P70 §6.4 step 5: the sentence only while read_document can do it."""
    if reader is None or toolkit is None:
        return ()
    return (DOCUMENTS_READ_NOTE,)


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


LIFE_SECONDS = 600.0  # character-life §3: how often the story writer looks at each principal's day
LIFE_SHOWN_DAYS = timedelta(days=30)  # §6: the plans and diaries the Web shows, back from today
BOND_EPISODES = 60  # character-bond §1: the newest episodes the memories' timeline shows with the bond


class LifeChanged(RuntimeError):
    """A fact added for a persona that is no longer the one chosen (§6)."""


class DayClosed(RuntimeError):
    """character-day §3.2: a message to a day session that is closed (only read now)."""


def quiet_hours(settings: ProactiveSettings) -> tuple[time, time] | None:
    """character-state §3 (S2): HIMARI's sleeping hours are speaking first's quiet hours
    (HIMARI_PROACTIVE_QUIET, whether or not HIMARI speaks first); None: no quiet hours."""
    if settings.quiet_start is None or settings.quiet_end is None:
        return None
    return settings.quiet_start, settings.quiet_end


def world_weather(location: tuple[float, float] | None, *, life: bool, companion: bool) -> str:
    """character-world §2: whether the weather is read, as told at startup."""
    if location is None:
        return "off (no HIMARI_LOCATION)"
    if not (life and companion):
        return "off (HIMARI's life is off)"
    return f"on (Open-Meteo at {location[0]:.2f},{location[1]:.2f})"


def life_settings(provider: ProviderAdapter, *, life: bool, companion: bool, model_id: str | None, write_at: time,
                  daily_tokens: int, time_zone: str) -> tuple[LifeSettings | None, str]:
    """character-life §3 (L4): the story writer's settings, or None, and the status told
    at startup. Its model is the memory passes' (HIMARI_MEMORY_MODEL, one the Provider
    declares checked for them), whether or not the passes run."""
    if not companion:
        return None, "off (HIMARI_COMPANION=0)"
    if not life:
        return None, "off (HIMARI_LIFE=0)"
    if model_id is None:
        return None, "off (no HIMARI_MEMORY_MODEL)"
    capabilities = provider.describe_capabilities(model_id)
    if not capabilities.memory_passes:
        return None, f"off ({model_id} is not checked for memory passes)"
    effort = "low" if "low" in capabilities.reasoning_efforts else None
    return (LifeSettings(model_id, effort, write_at, daily_tokens, time_zone),
            f"on ({model_id}, at {write_at:%H:%M}, {daily_tokens:,} tokens a day each)")


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
    model_id: str
    context_tokens: int  # the request token budget (compaction starts at 80%)
    operator_secret: str = field(repr=False)  # never in reprs, logs or tracebacks
    mcp: McpConnection = field(default_factory=lambda: McpConnection([], (), []))
    turn: TurnSettings = field(default_factory=TurnSettings)
    # Offered models whose window, efforts and Tool calling the Provider does
    # not declare (V3-P64 §3, review B1): told at startup, used as configured.
    unverified_models: tuple[str, ...] = ()
    trash_days: int = DEFAULT_TRASH_DAYS
    file_max_bytes: int = DEFAULT_FILE_MAX_BYTES  # V3-P66 §8.3
    uploads_work: bool = True  # there is a place for uploads: the machine Environment (V3-P101)
    views: ViewMaker | None = None  # model copies of images (V3-P67 §3.1); None or unavailable: no copies
    image_models: tuple[str, ...] = ()  # offered models that get images (V3-P67 §7.1)
    companion_models: tuple[str, ...] = ()  # offered models that satisfy all companion features
    companion_default_model: str | None = None  # V3-P92: default companion model for companion devices
    companion_models_only: bool = False  # only companion-verified models offered / selectable
    documents: DocumentReader | None = None  # read_document's reader (V3-P70); None: not offered
    toolkit: Toolkit | None = field(default=None, repr=False)  # its checked folder, open until close()
    memory_enabled: bool = False  # V3-P71 §9: the memory Tools and notes (HIMARI_MEMORY)
    # V3-P85 §12: memory passes (None: they do not run) and why, as told at startup.
    memory_passes: MemoryPasses | None = field(default=None, repr=False)
    auto_memory: str = "off"
    # character-life v0.1: HIMARI's life is told (the companion and HIMARI_LIFE on), and its story
    # writer (None: it does not run) with why, as told at startup.
    life_enabled: bool = False
    life_writer: LifeWriter | None = field(default=None, repr=False)
    auto_life: str = "off"
    # character-world v0.1: the weather (None: none is read) and the day's topics, as told at startup.
    weather: OpenMeteoWeather | None = field(default=None, repr=False)
    world_weather: str = "off"
    world_topics: str = "off"
    time_zone: str = DEFAULT_TIME_ZONE  # HIMARI_TIME_ZONE: a date the user writes ends in it unless the page says (V3-P85)
    conversation_search_enabled: bool = False  # V3-P72 §9 (HIMARI_CONVERSATION_SEARCH)
    streaming_enabled: bool = False  # V3-P73 §9 (HIMARI_STREAMING)
    companion_enabled: bool = False  # V3-P83 §3 (HIMARI_COMPANION): pairing and device tokens
    # V3-P74, V3-P109: the machine Environment (each Conversation's folder under the data root) and
    # the principals whose new Conversations use it (Q1); the others only talk (V3-P101).
    machine: MachineEnvironment | None = None
    machine_principals: frozenset[PrincipalId] = frozenset()
    # V3-P109 review: the folders a purge removes, whoever may still use them (also with no machine principal).
    folders: MachineEnvironment | None = None
    # V3-P65 §7.3: one lifecycle lock per Conversation (P0: one server process),
    # kept only while some call holds or waits for it (PR #56 review C1).
    _lifecycle: dict[ConversationId, _Lifecycle] = field(default_factory=dict, repr=False)
    _mcp_defaults_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    # V3-P83 §5, §8.1: who is connected, the current device and the event stream (one per
    # server), and the Runner's listener that publishes on it.
    presence: PresenceHub = field(default_factory=PresenceHub, repr=False)
    events: CompanionEvents | None = field(default=None, repr=False)
    screens: ScreenRequests | None = field(default=None, repr=False)  # V3-P91 §4: None with the companion off
    _ticking: asyncio.Task[None] | None = field(default=None, repr=False)
    # V3-P87: speaking first (HIMARI_PROACTIVE…), and the listener that ends each wake with its job
    proactive: ProactiveSettings = field(default_factory=ProactiveSettings)
    _wake_events: WakeEvents | None = field(default=None, repr=False)
    # V3-P87 §7: the periodic jobs (purge, proactive), the devices' last inputs and the scheduler
    ticker: Ticker = field(default_factory=Ticker, repr=False)
    inputs: Inputs | None = field(default=None, repr=False)
    scheduler: ProactiveScheduler | None = field(default=None, repr=False)
    devices: Devices = field(init=False, repr=False)  # V3-P83 §4: pairing and device tokens
    # character-day: the day's keeper (with HIMARI's life; None: no day session, no sleep) and the backup model
    keeper: DayKeeper | None = field(default=None, repr=False)
    backup_model: str | None = None
    _day_opening: dict[PrincipalId, asyncio.Lock] = field(default_factory=dict, repr=False)
    _day_tasks: set[asyncio.Task[object]] = field(default_factory=set, repr=False)

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
        max_runs: int = MAX_RUNS,
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
        life: bool = True,  # character-life §3, §5: HIMARI_LIFE=0 turns HIMARI's life off (prompt and writer)
        life_write_at: time = DEFAULT_WRITE_AT,  # HIMARI_LIFE_WRITE_AT: the story writer's time (server's zone)
        life_daily_tokens: int = LIFE_DAILY_TOKENS,  # HIMARI_LIFE_DAILY_TOKENS: each principal's daily budget
        location: tuple[float, float] | None = None,  # character-world §2: HIMARI_LOCATION; None: no weather
        life_topics: bool = True,  # character-world §3: HIMARI_LIFE_TOPICS=0 turns the day's topics off
        weather_transport: httpx.AsyncBaseTransport | None = None,  # tests: a fake Open-Meteo
        conversation_search: bool = True,  # V3-P72 §9: HIMARI_CONVERSATION_SEARCH=0 turns its Tools off
        streaming: bool = True,  # V3-P73 §9: HIMARI_STREAMING=0 asks no model for a stream
        companion: bool = True,  # V3-P83 §3: HIMARI_COMPANION=0 refuses pairing and device tokens
        companion_default_model: str | None = None,  # V3-P92: None selects the offered companion default automatically
        companion_models_only: bool = False,  # only models satisfying all companion features (needs screen_tool, screen_points, image_input, speaks_first, free_wake)
        time_zone: str = DEFAULT_TIME_ZONE,  # V3-P83 §6.4: HIMARI_TIME_ZONE, the zone the model is told the time in
        machine_principals: Iterable[str] = ("operator",),  # V3-P74 Q1: whose new Conversations have a folder; (): none
        proactive: ProactiveSettings | None = None,  # V3-P87 §11: HIMARI_PROACTIVE…; None: off
        backup_model: str | None = None,  # character-day §10: HIMARI_BACKUP_MODEL, an offered model; None: none
        day_clock: Callable[[], datetime] | None = None,  # tests: the day keeper's clock
    ) -> Application:
        speaking_first = proactive if proactive is not None else ProactiveSettings()
        if file_max_bytes < 1 or file_max_bytes > file_store_max_bytes:
            raise ValueError("a file's limit must be positive and within the file store's (V3-P66)")
        if max_runs < 1:
            raise ValueError("a turn needs a Run limit (V3-P62)")
        if not 1 <= trash_days <= 365:
            raise ValueError("trash_days is 1..365 (V3-P65)")
        turn = TurnSettings(max_runs)
        offers: list[ModelOffer] = []
        unverified: list[str] = []
        image_models: list[str] = []
        companion_models: list[str] = []
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
            offers.append(ModelOffer(offered, effort, capabilities.reasoning_efforts, limit))
            if capabilities.context_tokens is None:
                unverified.append(offered)
            if capabilities.image_input:
                image_models.append(offered)
            if capabilities.companion and capabilities.companion_capable:
                companion_models.append(offered)
        if companion and companion_models_only:
            if model_id not in companion_models:
                raise ValueError(f"the default model {model_id!r} does not satisfy companion requirements "
                                 "(needs screen_tool, screen_points, image_input, speaks_first, free_wake)")
            unmet = [o.model_id for o in offers if o.model_id not in companion_models]
            if unmet:
                raise ValueError(f"models not verified for companion mode offered under companion_models_only: {unmet}")
        resolved_companion_default: str | None = None
        if companion:
            if companion_default_model is not None:
                if companion_default_model not in [o.model_id for o in offers]:
                    raise ValueError(f"the companion default model {companion_default_model!r} is not offered")
                if companion_default_model not in companion_models:
                    raise ValueError(f"the companion default model {companion_default_model!r} does not satisfy companion requirements "
                                     "(needs screen_tool, screen_points, image_input, speaks_first, free_wake)")
                resolved_companion_default = companion_default_model
            elif companion_models:
                resolved_companion_default = (DEFAULT_COMPANION_MODEL if DEFAULT_COMPANION_MODEL in companion_models
                                              else model_id if model_id in companion_models else companion_models[0])
        catalog = ModelCatalog(tuple(offers), model_id)  # duplicates, the default, efforts (§3)
        if backup_model is not None and catalog.offer(backup_model) is None:  # character-day §18.10
            raise ValueError(f"the backup model {backup_model!r} is not offered (add it to HIMARI_MODELS)")
        default_budget = catalog.default.budget
        assert default_budget is not None
        root = Path(os.path.abspath(data_root))
        ensure_private_dir(str(root))
        secret = ensure_operator_secret(str(root / "control"))
        mcp_config = load_config(str(root / CONFIG_NAME), os.environ if environ is None else environ)

        store = SqliteStore(root / DATABASE_NAME)  # first: a second server on this root fails here
        mcp: McpConnection | None = None
        toolkit: Toolkit | None = None
        weather: OpenMeteoWeather | None = None
        try:
            store.blobs.configure(max_bytes=file_store_max_bytes, keep_days=file_keep_days)
            mcp = await connect(mcp_config, check_schema=check_tool_input_schema, transport=mcp_transport)
            saved_defaults = read_defaults(root / "control/mcp-defaults.json")
            mcp.statuses = tuple(replace(s, enabled_by_default=saved_defaults.get(s.source, s.enabled_by_default))
                                 for s in mcp.statuses)
            toolkit, reader, documents_reason = await _documents(root, sandbox, documents_dir, documents_owners)
            machine_allowed = frozenset(PrincipalId(p) for p in machine_principals)
            machine = None
            if machine_allowed:  # V3-P109 §3 D1: each Conversation's folder under the data root
                machine = MachineEnvironment(str(root / CONVERSATIONS_NAME),
                                             files=ToolFiles(store.blobs, SqliteData(store), file_max_bytes),
                                             documents=reader)
                log.info("conversation folders: %s", machine.root)
            else:
                log.warning("no principal has conversation folders: Conversations only talk (no files or uploads)")
            log.info("documents: read_document %s%s", documents_reason,
                     f"; libraries {', '.join(f'{k} {v}' for k, v in reader.versions.items())}" if reader else "")
            uploads_work = machine is not None
            views = await _image_views(root, sandbox)
            if location is not None and life and companion:  # character-world §2 (W1): read by the job "weather"
                weather = OpenMeteoWeather(*location, time_zone, transport=weather_transport)
            base = base_instructions(companion) if instructions is None else instructions  # V3-P108 §3.1
            zones = frozenset(available_timezones())
            hub = PresenceHub(time_zones=zones.__contains__)  # a device's zone that is not known is ignored (§5)
            presence = CompanionPresence(DeviceRegistry(SqliteData(store)), hub, time_zone, enabled=companion)
            screens = ScreenRequests(hub) if companion else None  # V3-P91: the screen Tool's requests
            system = SystemSpec(companion=companion, attachments=uploads_work,
                                proactive=companion and speaking_first.enabled,
                                documents=_documents_notes(reader, toolkit)
                                if machine is not None else ())  # read_document is the machine's (V3-P101)
            # character-day §18.7: the day's keeper, with HIMARI's life (its busy check is set with the runner)
            keeper = DayKeeper(SqliteData(store), time_zone=time_zone, clock=day_clock) if life and companion else None
            runtime = build_runtime(provider, store=store, environments=[machine] if machine else [],
                                    tools=[*tools, *mcp.handlers, *mcp.resource_tools],
                                    models=catalog,
                                    instructions=base,  # V3-P89: the base; the sections are the Prompt module's
                                    system=system,
                                    external_defaults=mcp.defaults,
                                    source_notes=[SourceNote(source, note.placement, note.text)
                                                  for source, note in mcp.notes.items()],
                                    max_runs_per_drive=max_runs, turn_guidance=True, image_views=views,
                                    file_max_bytes=file_max_bytes, memory=memory, life=life and companion,
                                    # character-day §13: HIMARI sleeps by its schedule, never by the quiet hours
                                    weather=weather,  # character-world §2
                                    day=keeper, backup_model=backup_model,
                                    conversation_search=conversation_search, streaming=streaming,
                                    time_zone=time_zone,
                                    presence=presence, screen=screens,
                                    # Stored-blob downloads do not require upload placement (renameat2).
                                    interfaces=ConfiguredInterfaces(file_download=True,
                                                                    hub=hub,
                                                                    companion_enabled=companion))
        except BaseException:
            try:
                if weather is not None:
                    await weather.aclose()
                if mcp is not None:
                    await mcp.aclose()
            finally:
                if toolkit is not None:
                    toolkit.close()
                store.close()
            raise
        assert isinstance(runtime.data, SqliteData)
        events = CompanionEvents(hub)
        wake_events = WakeEvents(runtime.data)
        listeners = _JobListeners(events, wake_events)
        runner = BackgroundRunner(runtime.driver, listener=listeners)
        application = cls(root, store, runtime.data, runtime, runner, model_id,
                          default_budget.limit_tokens, secret, mcp, turn, tuple(unverified), trash_days,
                          file_max_bytes, uploads_work, views, tuple(image_models),
                          tuple(companion_models), resolved_companion_default, companion_models_only, reader, toolkit,
                          memory_enabled=memory, conversation_search_enabled=conversation_search,
                          streaming_enabled=streaming, companion_enabled=companion, machine=machine,
                          machine_principals=machine_allowed,
                          presence=hub, events=events)
        application.time_zone = time_zone
        application.proactive = speaking_first
        application.folders = machine if machine is not None else MachineEnvironment(str(root / CONVERSATIONS_NAME))
        if screens is not None:
            screens.job_of = lambda scope: runner.status(scope).job_id  # §6: the app's move context
            application.screens = screens
        application._wake_events = wake_events
        application.backup_model = backup_model
        if keeper is not None:  # character-day §18.10: the keeper, told of every job's end
            keeper.busy = lambda scope: scope.conversation_id in runner.running(scope.principal_id)
            keeper.on_sleep = lambda principal, state: hub.publish(principal, "himari_sleep", {"sleep": state.value})
            keeper.on_session = application._tell_session
            keeper.on_closed = application._day_closed
            keeper.on_asleep = application._fell_asleep
            listeners.listeners.append(keeper)
            application.keeper = keeper
        assert isinstance(runtime.driver, Driver)
        await settle_cut_wakes(runtime.data, runtime.driver)  # V3-P87 §4.4: before any new wake
        settings, application.auto_memory = memory_pass_settings(
            provider, memory=memory, auto=memory_auto, model_id=memory_model, idle_minutes=memory_idle_minutes,
            daily_tokens=memory_daily_tokens, time_zone=time_zone)
        if settings is not None and backup_model is not None:  # character-day §10, §18.8: the passes' backup too
            spare = catalog.offer(backup_model)
            if spare is not None:
                settings = replace(settings, backup_model_id=spare.model_id, backup_effort=spare.default_effort)
        if settings is not None:
            async def zone_for(scope: TrustedScope, origin: MessageOrigin | None) -> str:
                """V3-P85 §4.2: the zone of the device the part's last message came from, else the server's."""
                found = (await presence.read(scope.principal_id, origin)).time_zone
                return found if found in zones else time_zone

            # character-life §4: with HIMARI's life told, a pass also keeps what HIMARI said of itself
            # character-bond §3: and what the two settled
            passes = MemoryPasses(runtime.data, provider, settings, _RunnerActivity(runner), zone_for=zone_for,
                                  life=runtime.data if life and companion else None, personas=runtime.data,
                                  bond=runtime.data if life and companion else None)
            listeners.listeners.append(passes)
            passes.on_applied = events.memory_applied
            application.memory_passes = passes
            await passes.start()
        application.life_enabled = life and companion
        writing, application.auto_life = life_settings(
            provider, life=life, companion=companion, model_id=memory_model, write_at=life_write_at,
            daily_tokens=life_daily_tokens, time_zone=time_zone)
        if writing is not None:  # character-day §10: a self run's default model, and the backup every call has
            backup = None if backup_model is None else catalog.offer(backup_model)
            writing = replace(writing, default_model_id=catalog.default.model_id,
                              default_effort=catalog.default.default_effort,
                              backup_model_id=None if backup is None else backup.model_id,
                              backup_effort=None if backup is None else backup.default_effort)
        application.weather = weather
        application.world_weather = world_weather(location, life=life, companion=companion)
        topics: SearchTopics | None = None
        found = search_tool(mcp) if writing is not None and life_topics else None
        if found is not None:  # character-world §3 (W2): the writer's only, once a date
            topics = SearchTopics(topic_search(found))
        application.world_topics = ("off (HIMARI_LIFE_TOPICS=0)" if not life_topics
                                    else "off (no story writer)" if writing is None
                                    else "off (no MCP search Tool)" if found is None
                                    else f"on ({found.definition.tool_id})")
        if writing is not None:  # character-life §3: a periodic job, never a Conversation's Run
            # character-work §4: the worker runs with it; no work plan in HIMARI's sleeping hours
            # character-mind §4.1: a self run has a turn's system text (as build_runtime fills it in) and
            # chooses its model through the application (the conversation a wake would go to)
            # character-day §13: no quiet hours: the day's sleep (its slots) is what the work and the world keep out of
            application.life_writer = LifeWriter(runtime.data, provider, writing, weather=weather, topics=topics,
                                                 composer=Composer(), instructions=base, link=application,
                                                 system=replace(system, memory=memory, turn_guidance=True,
                                                                life=True))
            await application.life_writer.start()
        application._ticking = asyncio.create_task(hub.run(), name="presence-tick")
        if cleanup_seconds is not None:
            application.ticker.add("purge", cleanup_seconds, application._purge_round)
        if application.life_writer is not None:
            application.ticker.add("life", LIFE_SECONDS, application.life_writer.run)
        if application.keeper is not None:
            application.ticker.add("day", DAY_SECONDS, application.keeper.run)
        if weather is not None:
            application.ticker.add("weather", WEATHER_SECONDS, weather.refresh)
        if companion:  # V3-P87 §5.2: the devices' last inputs, kept whether or not HIMARI speaks first
            application.inputs = Inputs(runtime.data)
            hub.on_input = application.inputs.seen
            if speaking_first.enabled:
                ticker = application.ticker
                scheduler = ProactiveScheduler(runtime.data, hub, application.devices, runner, application.inputs,
                                               speaking_first, time_zone, zones=zones,
                                               poke=lambda after: ticker.poke("proactive", after),
                                               recent=application.wake_conversation,  # character-day §18.9
                                               free_ok=runtime.driver.free_wake_ok,
                                               bond=life and companion,  # character-bond §6: anniversaries
                                               work=life and companion,  # character-work §7.1: finished pieces
                                               day=application.keeper)  # character-day §9: its sleep, promises
                hub.on_returned = scheduler.returned
                application.scheduler = scheduler
                ticker.add("proactive", PROACTIVE_SECONDS, scheduler.run)
        application.ticker.start()
        return application

    async def mind_model(self, principal: PrincipalId) -> MindModel:
        """character-day §10, §18.7 (MindLink): a self run's model — the day session's, as its next turn would
        use it; with no day session (or the life off), the default model."""
        session = await self.day_session(principal, open=False)
        if session is None:
            return MindModel(None, reason="the default model (no day session)")
        choice = await self.model_choice(TrustedScope(principal, session.conversation_id))
        return MindModel(choice.model_id, choice.effort, "the day session's model")

    async def session_input(self, principal: PrincipalId) -> SessionInput | None:
        """character-day §7, §18.7 (MindLink): the open day session's next turn as it would be composed now, and
        its model's ceiling; None without one."""
        session = await self.day_session(principal, open=False)
        if session is None:
            return None
        scope = TrustedScope(principal, session.conversation_id)
        inputs = await self.runtime.driver.turn_inputs(scope)
        budget = self.runtime.models.resolve(await self.data.read_model_setting(scope)).offer.budget
        return SessionInput(session.conversation_id, session.day_session or "", inputs,
                            None if budget is None else budget.ceiling_tokens)

    # --- the day session (character-day v0.1 §3, §18.10) ------------------------------------------

    async def day_session(self, principal: PrincipalId, *, open: bool = True) -> ConversationSummary | None:
        """§3.1, §3.2: the principal's open day session — with ``open``, opened now when none is (its title the
        date, "11월 27일 (금)"; on the companion's default model). None with HIMARI's life off, or none open."""
        if self.keeper is None:
            return None
        current = await self.data.current_day_session(principal)
        if current is not None or not open:
            return current
        async with self._day_opening.setdefault(principal, asyncio.Lock()):
            current = await self.data.current_day_session(principal)
            if current is not None:
                return current
            today = datetime.now(ZoneInfo(self.time_zone)).date()
            setting = ModelSetting(self.companion_default_model, None) if self.companion_default_model else None
            environments = (MACHINE,) if self.machine is not None and principal in self.machine_principals else ()
            try:
                opened = await self.data.open_day_session(principal, today.isoformat(), session_title(today),
                                                          environments=environments, model_setting=setting)
            except DataError as error:
                if error.code is not DataErrorCode.DUPLICATE_IDENTITY:
                    raise
                return await self.data.current_day_session(principal)
            summary = await self.data.read_conversation(TrustedScope(principal, opened))
            log.info("opened %s's day session %s (%s)", principal, opened, summary.title)
            self._tell_session(principal, summary, False)
            return summary

    async def wake_conversation(self, principal: PrincipalId) -> ConversationSummary | None:
        """§18.9: where a wake (and a device, a try) goes — the day session, opened when needed; with HIMARI's life
        off, the latest companion conversation as before."""
        if self.keeper is None:
            return await self.recent_companion_conversation(principal)
        return await self.day_session(principal)

    def _tell_session(self, principal: PrincipalId, summary: ConversationSummary, closed: bool) -> None:
        """§18.10: the stream's ``day_session`` (the Web and the devices follow it)."""
        self.presence.publish(principal, "day_session", {"conversation": summary.conversation_id,
                                                         "title": summary.title, "closed": closed})

    def _spawn(self, work: Awaitable[object], name: str) -> None:
        async def guarded() -> object:
            try:
                return await work
            except Exception:  # noqa: BLE001 — logged; the next round does it again
                log.exception("%s failed", name)
                return None

        task: asyncio.Task[object] = asyncio.get_running_loop().create_task(guarded(), name=name)
        self._day_tasks.add(task)
        task.add_done_callback(self._day_tasks.discard)

    async def _day_closed(self, principal: PrincipalId, closed: TrustedScope | None) -> None:
        """§6 steps 3 and 4: the closed session read by the memory passes at once, and the world's new day."""
        if closed is not None and self.memory_passes is not None:
            self._spawn(self.memory_passes.read(closed), f"memory-pass-{closed.conversation_id}")
        if self.life_writer is not None:
            self._spawn(self.life_writer.world(principal), f"world-{principal}")

    def _fell_asleep(self, principal: PrincipalId) -> None:
        """§7: asleep before 22:00, the diary is written as it falls asleep (the writer knows whether it is due)."""
        if self.life_writer is not None:
            self._spawn(self.life_writer.write(principal), f"diary-{principal}")

    async def poke(self, principal: PrincipalId, device_id: str) -> AsleepAnswer | None:
        """§5.5, §11: the app's sleeping character clicked — counted as a message; when it stirs (a mumble) or wakes,
        a turn opens on that device (a ``poke`` wake). None: awake (or HIMARI's life off)."""
        if self.keeper is None:
            return None
        answer = await self.keeper.poke(principal)
        if answer in (AsleepAnswer.MUMBLE, AsleepAnswer.WAKE):
            session = await self.day_session(principal)
            if session is not None:
                try:
                    await self.runner.submit_wake(TrustedScope(principal, session.conversation_id),
                                                  (WakeReason(WakeKind.POKE),), device_id)
                except (ControlError, DataError) as error:  # busy, not settled: the stir is counted all the same
                    log.info("a poke opened no turn for %s: %s", principal, error)
        return answer

    async def sleep_view(self, principal: PrincipalId) -> SleepView | None:
        """§12: HIMARI's sleep now (the Web's status, a device's state); None with HIMARI's life off."""
        return None if self.keeper is None else await self.keeper.sleep_view(principal)

    async def sleeps(self, principal: PrincipalId) -> tuple[SleepRecord, ...]:
        """§12: the latest sleeps (newest first)."""
        return () if self.keeper is None else await self.keeper.recent_sleeps(principal)

    async def write_plan(self, principal: PrincipalId, persona_id: int | None, plan: PlanWrite) -> LifeEntry:
        """§4.2, §18.11: the user's slot (origin USER) for the persona the page showed; LifeChanged as for a fact;
        PLAN_REFUSED past the schedule's limits."""
        shown = await self._persona_shown(principal, persona_id)
        return await self.data.write_plan(principal, shown, plan, origin=LifeOrigin.USER, zone=self.time_zone,
                                          now=datetime.now(UTC))

    async def edit_plan(self, principal: PrincipalId, plan: PlanWrite, revision: int) -> LifeEntry:
        """§18.11: the user's change of a slot (a move too) at the revision the page read."""
        found = next((e for e in (await self.life(principal))[1] if e.entry_id == plan.entry_id), None)
        if found is None or found.kind is not LifeKind.PLAN:
            raise DataError(DataErrorCode.NOT_FOUND, f"no slot {plan.entry_id}")
        if found.revision != revision:
            raise DataError(DataErrorCode.VERSION_CONFLICT, f"the entry is at revision {found.revision}")
        return await self.data.write_plan(principal, found.persona_id, plan, origin=LifeOrigin.USER,
                                          zone=self.time_zone, now=datetime.now(UTC))

    async def session_diaries(self, scope: TrustedScope) -> tuple[tuple[datetime, str], ...]:
        """§7, §18.11: the diaries written in this day session's context — when each was written and its text (as
        the diary stands now; one deleted since is left out), oldest first. Authorizes like any read."""
        summary = await self.data.read_conversation(scope)
        if summary.day_session is None:
            return ()
        found: list[tuple[datetime, str]] = []
        for write in await self.data.session_writes(scope.principal_id, scope.conversation_id):
            if write.job is not LifeJob.REVIEW or write.finished_at is None or write.status.value != "applied":
                continue
            diary = next((e for e in await self.data.list_life(scope.principal_id, write.persona_id, since=write.day)
                          if e.kind is LifeKind.DIARY and e.day == write.day), None)
            if diary is not None:
                found.append((write.finished_at, diary.text))
        return tuple(found)

    async def remarks(self, principal: PrincipalId, since: datetime, until: datetime) -> tuple[str, ...]:
        """character-mind §4.4 (MindLink): the user's messages, as text, in the day session (character-day §18.9;
        with HIMARI's life off, the latest companion conversation) between ``since`` and ``until`` — what they said
        while HIMARI was at its own work."""
        recent = (await self.recent_companion_conversation(principal) if self.keeper is None
                  else await self.day_session(principal, open=False))
        if recent is None:
            return ()
        said: list[str] = []
        for event in await self.data.read_events(TrustedScope(principal, recent.conversation_id)):
            if isinstance(event.payload, UserMessage) and since <= event.created_at <= until:
                text = "\n".join(b.text for b in event.payload.blocks if isinstance(b, TextBlock)).strip()
                if text:
                    said.append(text)
        return tuple(said)

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

    # --- Models (V3-P64) ---------------------------------------------------------------

    @property
    def models(self) -> tuple[ModelView, ...]:
        """The offered models, in the operator's order."""
        catalog = self.runtime.models
        return tuple(ModelView(o.model_id, o.default_effort, ordered_efforts(o.efforts),
                               o.model_id not in self.unverified_models, o.model_id == catalog.default_model,
                               o.model_id in self.image_models,
                               o.model_id in self.companion_models)
                     for o in catalog.offers)

    def _choice_view(self, setting: ModelSetting | None, resolved: ResolvedModel) -> ModelChoiceView:
        return ModelChoiceView(resolved.model_id, resolved.reasoning_effort, setting, resolved.fallback)

    async def model_choice(self, scope: TrustedScope) -> ModelChoiceView:
        """What this Conversation's next turn would use. Authorizes like any
        read (NOT_AUTHORIZED)."""
        setting = await self.data.read_model_setting(scope)
        return self._choice_view(setting, self.runtime.models.resolve(setting))

    async def require_companion_model(self, scope: TrustedScope) -> None:
        """A device may reuse a Conversation only on a currently offered companion model.

        Check the resolved choice, including fallback from a model removed since it was saved.
        The Driver checks its pinned execution model again before any request is sent.
        """
        if (await self.model_choice(scope)).model_id not in self.companion_models:
            raise ModelRefused("companion_unsupported",
                               "This conversation needs a companion model for device messages. Choose one on the Web.")

    async def recent_companion_conversation(self, principal: PrincipalId) -> ConversationSummary | None:
        """The latest user conversation whose next turn resolves to an offered companion model.

        Shared by device reconnects, scheduled wakes/reminders and manual tries.
        Scan past incompatible conversations, including models removed from the catalog.
        """
        if not self.companion_models:
            return None
        offset = 0
        while batch := await self.data.list_user_conversations(principal, offset=offset):
            for conversation in batch:
                choice = await self.model_choice(TrustedScope(principal, conversation.conversation_id))
                if choice.model_id in self.companion_models:
                    return conversation
            offset += len(batch)
        return None

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
            if self.companion_enabled and self.companion_models_only and offer.model_id not in self.companion_models:
                raise ModelRefused("companion_unsupported",
                                   f"Model {offer.model_id} does not satisfy companion requirements.")
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

    # --- the user's instructions (V3-P69) ----------------------------------------------

    async def instructions(self, principal: PrincipalId,
                           scope: InstructionScope = InstructionScope.USER) -> Instructions:
        """The instructions the user wrote for every Conversation (the companion
        persona is the PersonaStore's, V3-P108)."""
        return await self.data.read_instructions(principal, scope)

    async def set_instructions(self, principal: PrincipalId, text: str, revision: int,
                               scope: InstructionScope = InstructionScope.USER) -> Instructions:
        """Saved at once, used from each Conversation's next turn (§5); a turn
        under way keeps the text it started with. DataError VERSION_CONFLICT
        (written since ``revision`` was read), TOO_LARGE or INVALID_ARGUMENT."""
        return await self.data.write_instructions(principal, scope, text, revision)

    # --- HIMARI's life (character-life v0.1 §6) ---------------------------------------------

    async def life(self, principal: PrincipalId) -> tuple[Persona | None, tuple[LifeEntry, ...]]:
        """The persona chosen now (None: no persona, whose life is NO_PERSONA's, L2) and its
        life: every fact, and the plans and diaries of the last LIFE_SHOWN_DAYS and later."""
        chosen = await self.data.active_persona(principal)
        since = (datetime.now(ZoneInfo(self.time_zone)).date() - LIFE_SHOWN_DAYS).isoformat()
        entries = await self.data.list_life(principal, NO_PERSONA if chosen is None else chosen.persona_id,
                                            since=since)
        return chosen, entries

    async def next_world(self, principal: PrincipalId) -> datetime | None:
        """character-mind §8, character-day §6: when the world next decides HIMARI's day (None: no story writer) —
        today's closing brings it on before noon."""
        if self.life_writer is None:
            return None
        closed = None
        if self.keeper is not None:
            chosen = await self.data.active_persona(principal)
            closed = (await self.data.read_day_state(
                principal, NO_PERSONA if chosen is None else chosen.persona_id)).last_closing_at
        return self.life_writer.next_world(datetime.now(UTC), closed)

    def next_work(self, entries: Sequence[LifeEntry]) -> LifeEntry | None:
        """character-work §8: the next work plan among the life as read (the server's clock and zone)."""
        return next_plan(entries, datetime.now(UTC), ZoneInfo(self.time_zone))

    async def add_life_fact(self, principal: PrincipalId, persona_id: int | None, text: str) -> LifeEntry:
        """The user's fact for the persona the page showed (None: no persona). LifeChanged when
        another persona is chosen now, so a fact never lands in a life the user did not see."""
        chosen = await self.data.active_persona(principal)
        if (None if chosen is None else chosen.persona_id) != persona_id:
            raise LifeChanged("another persona is chosen now")
        return await self.data.add_life_fact(principal, NO_PERSONA if persona_id is None else persona_id, text)

    # --- the bond (character-bond v0.1 §1, Web) ------------------------------------------------

    async def bond(self, principal: PrincipalId) -> tuple[Persona | None, BondSnapshot, tuple[RecentEpisode, ...]]:
        """The persona chosen now (None: no persona), its bond as a turn would see it now (its days in
        the server's zone; the stage moves as it is read) and the newest BOND_EPISODES episodes for the
        memories' timeline."""
        chosen = await self.data.active_persona(principal)
        snapshot = await self.data.read_bond(principal, NO_PERSONA if chosen is None else chosen.persona_id,
                                             now=datetime.now(UTC), zone=self.time_zone)
        episodes = await self.data.find_episodes(principal, (), limit=BOND_EPISODES)
        return chosen, snapshot, episodes

    async def _persona_shown(self, principal: PrincipalId, persona_id: int | None) -> int:
        chosen = await self.data.active_persona(principal)
        if (None if chosen is None else chosen.persona_id) != persona_id:
            raise LifeChanged("another persona is chosen now")
        return NO_PERSONA if persona_id is None else persona_id

    async def add_bond(self, principal: PrincipalId, persona_id: int | None, tag: BondTag, text: str,
                       month_day: str | None) -> LifeEntry:
        """The user's bond entry for the persona the page showed; LifeChanged as add_life_fact."""
        return await self.data.add_bond(principal, await self._persona_shown(principal, persona_id), tag, text,
                                        month_day)

    async def set_bond_stage(self, principal: PrincipalId, persona_id: int | None, stage: BondStage,
                             revision: int) -> BondStanding:
        """The user's choice of stage for the persona the page showed; LifeChanged as add_life_fact."""
        return await self.data.set_bond_stage(principal, await self._persona_shown(principal, persona_id), stage,
                                              expected_revision=revision)

    # --- HIMARI's state (character-state v0.1 §2, §3) ------------------------------------------

    def weather_today(self) -> str | None:
        """character-world §2: today's weather line (the server's date) as a turn would be told it now."""
        if self.weather is None:
            return None
        found = self.weather.weather_on(datetime.now(ZoneInfo(self.time_zone)).date())
        return None if found is None else found.line()

    async def _chosen_id(self, principal: PrincipalId) -> int:
        chosen = await self.data.active_persona(principal)
        return NO_PERSONA if chosen is None else chosen.persona_id

    async def himari_state(self, principal: PrincipalId) -> tuple[HimariMood | None, HimariNow | None]:
        """The chosen persona's mood while it is told and what HIMARI is doing now, as a turn
        starting now sees them (the server's clock); (None, None) while the life is off."""
        if not self.life_enabled:
            return None, None
        persona_id = await self._chosen_id(principal)
        now = datetime.now(UTC)
        local = now.astimezone(ZoneInfo(self.time_zone))
        sleep = await self.sleep_view(principal)  # character-day §4.5: asleep as its sleep says
        entries = await self.data.list_life(principal, persona_id,
                                            since=(local.date() - LIFE_READ_DAYS).isoformat())
        return (live_mood(await self.data.read_mood(principal, persona_id), now),
                doing_now(entries, local, asleep=sleep is not None and sleep.state in (SleepState.ASLEEP,
                                                                                    SleepState.HALF_ASLEEP)))

    async def current_mood(self, principal: PrincipalId) -> str | None:
        """§2: the mood a device's character starts with — the chosen persona's while it is
        told, else "neutral"; None while the life is off (no mood is kept)."""
        if not self.life_enabled:
            return None
        mood = live_mood(await self.data.read_mood(principal, await self._chosen_id(principal)), datetime.now(UTC))
        return "neutral" if mood is None else mood.mood

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

    async def create_conversation(self, principal: PrincipalId, *, title: str | None = None,
                                  initial_model: str | None = None) -> ConversationId:
        """T0: for a machine principal, the machine Environment activated
        (V3-P74 Q1; its folder is made on first use); for anyone else, no
        Environment: the Conversation only talks (V3-P101)."""
        setting = ModelSetting(initial_model, None) if initial_model is not None else None
        if self.machine is not None and principal in self.machine_principals:
            return await self.data.create_conversation(
                principal, title=title, environments=(MACHINE,), model_setting=setting)
        return await self.data.create_conversation(principal, title=title, model_setting=setting)

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
        await self._check_folder(scope)  # authorizes; FilesUnavailable without a place for uploads
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
        """V3-P70 §9: read_document offered, and the toolkit's libraries."""
        reader = self.documents
        read = reader is not None and reader.available
        return {"read": read, "libraries": dict(reader.versions) if read and reader is not None else {}}

    async def _check_folder(self, scope: TrustedScope) -> MachineEnvironment:
        """The machine Environment, when this Conversation has a folder there
        (an old Inside one has none, V3-P101); FilesUnavailable otherwise."""
        if not self.uploads_work or self.machine is None:
            raise FilesUnavailable(scope.conversation_id)
        if not await self.data.has_environment(scope, MACHINE):  # authorizes first
            raise FilesUnavailable(scope.conversation_id)
        return self.machine

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
                   origin: MessageOrigin | None = None,
                   screen: FileId | None = None, *, connection_generation: str | None = None,
                   client_file_download: bool | None = None) -> JobStatus:
        """A user message. With attachments (§5.1, under the lifecycle lock):
        1 the T1 checks as a read; 2 refused while a job runs; 3 the uploads
        placed in ``uploads/`` under the Workspace write lock (waited for at
        most PLACE_LOCK_SECONDS); 4 the message submitted, attaching them.
        ``origin``: where it was sent from (V3-P83 §4.2), kept on the message.
        ``screen`` (V3-P84 §4.2): a staged PNG or JPEG upload, attached as the
        user's screen and never placed in the Workspace (§4.4); attached once,
        in the message's own transaction (T1)."""
        if origin is not None and origin.kind is OriginKind.DEVICE:
            if connection_generation is None and client_file_download is not None:
                raise ConnectionGenerationRequired
            if connection_generation is not None and not self.presence.valid_generation(
                    scope.principal_id, origin.device_id or "", connection_generation):
                raise StaleConnectionGeneration
        summary = await self.data.read_conversation(scope)  # 404 before a job is started (or a file read)
        if summary.day_session is not None and summary.closed_at is not None:
            raise DayClosed(scope.conversation_id)  # character-day §3.2: a closed day is only read
        if origin is not None and origin.kind is OriginKind.DEVICE:
            await self.require_companion_model(scope)
        with device_input_negotiation(connection_generation, client_file_download):
            if screen is not None:
                async with self._exclusive(scope.conversation_id):
                    (record,) = await self.data.check_attachments(scope, [screen])
                    if record.image_type not in SCREEN_TYPES:
                        raise ScreenRefused("not a PNG or JPEG image")
                    if self.runner.status(scope).running:
                        raise ControlError(ControlErrorCode.BUSY, scope.conversation_id)
                    said = ((TextBlock(text),) if text else ()) + (AttachmentRef(record.file_id, 1, screen=True),)
                    return await self.runner.submit(scope, UserMessage(said, origin=origin))
            if not attachments:
                return await self.runner.submit(scope, UserMessage((TextBlock(text),) if text else (), origin=origin))
            async with self._exclusive(scope.conversation_id):
                records = await self.data.check_attachments(scope, attachments)
                if self.runner.status(scope).running:
                    raise ControlError(ControlErrorCode.BUSY, scope.conversation_id)
                machine = await self._check_folder(scope)
                try:
                    locked = machine.locked(scope.conversation_id, PLACE_LOCK_SECONDS)
                    placer = _machine_placer(machine, scope.conversation_id)
                    async with locked:
                        # Read again under the lock: another tab may have placed them (review C4).
                        fresh = await self.data.read_file_records(scope, [(r.file_id, r.version) for r in records])
                        for index, record in enumerate(fresh):
                            await self._place(scope, placer, record, first=index == 0)
                except MachineUnavailable:
                    raise FilesUnavailable(scope.conversation_id) from None
                blocks = ((TextBlock(text),) if text else ()) + tuple(AttachmentRef(r.file_id, r.version) for r in records)
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

    async def presented_files(self, scope: TrustedScope, *, limit: int = 50,
                              offset: int = 0) -> Sequence[FileRecord]:
        """V3-P93 §6.2: presented files of succeeded, publicly visible Tool calls
        in this Conversation, filtered before paging."""
        return await self.data.read_presented_files(scope, limit=limit, offset=offset)

    async def download_presented_file(self, scope: TrustedScope, file_id: FileId,
                                      version: int) -> tuple[FileRecord, BinaryIO]:
        """V3-P93 §6.2: a publicly presented file's stored bytes, for a device."""
        record = await self.data.read_presented_file(scope, file_id, version)
        if record is None:
            raise DataError(DataErrorCode.NOT_FOUND, file_id)
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
            folders = self.folders or self.machine
            if MACHINE in trashed.environments and folders is not None:
                await folders.remove_conversation_folder(scope)
            # An old Inside folder (<data>/workspaces) is left: V3-P101 removes it by hand.
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
        if self.keeper is not None:
            await self.keeper.aclose()
        for day_task in list(self._day_tasks):
            day_task.cancel()
        if self._day_tasks:
            await asyncio.gather(*self._day_tasks, return_exceptions=True)
        if self.memory_passes is not None:
            await self.memory_passes.close()  # a pass cut short is counted at the next start (V3-P85 §4.5)
        try:
            await self.runner.shutdown(grace_seconds)
            if self._wake_events is not None:
                await self._wake_events.aclose()
            if self.inputs is not None:
                await self.inputs.aclose()
        finally:
            try:
                await self.mcp.aclose()
            finally:
                if self.weather is not None:
                    await self.weather.aclose()
                if self.toolkit is not None:
                    self.toolkit.close()
                self.store.close()

    # --- speaking first (V3-P87) ----------------------------------------------------------

    async def try_wake(self, principal: PrincipalId) -> tuple[ConversationId, JobStatus]:
        """§11: open a TRY wake now in the most recent companion conversation, spoken on the
        current device — past the user's quiet, the quiet hours, gap and daily
        limit (and the ten minutes after a message), never while a job runs or
        in a conversation that is not settled. ProactiveRefused."""
        if not (self.proactive.enabled and self.companion_enabled):
            raise ProactiveRefused("proactive_off")
        device = self.presence.current(principal)
        if device is None:
            raise ProactiveRefused("no_device")
        recent = await self.wake_conversation(principal)  # character-day §18.9: the day session
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
                                     "told": self.proactive.describe(), "current_device": self.presence.current(principal),
                                     "quiet_hours": None if quiet.quiet_start is None or quiet.quiet_end is None
                                     else f"{quiet.quiet_start:%H:%M}-{quiet.quiet_end:%H:%M}"}
        if self.scheduler is not None:
            status.update(await self.scheduler.status(principal))
        return status

    def check_layout(self) -> None:
        """Re-verify the private directories (e.g. before serving)."""
        for path in (self.data_root, self.data_root / "control"):
            check_private_dir(str(path))


@dataclass(frozen=True)
class _Placer:
    """One upload's steps (V3-P66 §5.2) in a Conversation's own machine folder."""

    holds: Callable[[str, int, str], Awaitable[bool]]
    copy_in: Callable[[BinaryIO, bool], Awaitable[str]]
    free_path: Callable[[str, str | None], Awaitable[str]]
    commit_copy: Callable[[str, str], Awaitable[bool]]
    discard_copy: Callable[[str], Awaitable[None]]


def _machine_placer(machine: MachineEnvironment, conversation_id: str) -> _Placer:
    return _Placer(
        holds=lambda path, size, sha256: machine.holds(conversation_id, path, size, sha256),
        copy_in=lambda source, clear: machine.copy_in(conversation_id, source, clear=clear),
        free_path=lambda name, preferred: machine.free_path(conversation_id, name, preferred),
        commit_copy=lambda temp, path: machine.commit_copy(conversation_id, temp, path),
        discard_copy=lambda temp: machine.discard_copy(conversation_id, temp),
    )
