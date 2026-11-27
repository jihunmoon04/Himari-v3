"""Data public contract — consumer-specific sub-ports (V3-039).

Source documents: docs/data-contract-v0.2.md (§1.1, §4–§10), docs/contracts.md §6.
Sub-ports may share one backend and transaction; contract-change classification
(V3-038) is per sub-port. ``ControlState`` (DATA-09/10) is P2 and not declared.

Durability (V3-045): the in-process Conversation lock only prevents concurrent
drivers inside a live process. Crash duplication is prevented by the persisted
marks ``DISPATCH_CLAIMED`` (before Provider send) and ``EXECUTION_CLAIMED``
(before an effectful Tool run), plus restart cleanup (T-R).
"""

from __future__ import annotations

import calendar
import hashlib
import json
import mimetypes
import re
import unicodedata
from datetime import UTC, date, time, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import BinaryIO, Protocol

from himari.common import (
    MOODS,
    DeepFrozen,
    FrozenMap,
    CallId,
    CallRef,
    ContentBlock,
    ConversationId,
    EnvironmentKind,
    EventId,
    FileId,
    JsonValue,
    ModelToolResult,
    PrincipalId,
    ProviderContinuation,
    RawRepresentation,
    RunId,
    TokenUsage,
    ToolCall,
    ToolId,
    TrustedScope,
    UnknownOutcomePolicy,
)

# --- Errors (Data v0.2 §11) --------------------------------------------------


class DataErrorCode(Enum):
    NOT_AUTHORIZED = "not_authorized"
    NOT_FOUND = "not_found"
    STALE_BASIS = "stale_basis"
    VERSION_CONFLICT = "version_conflict"
    ALREADY_COMMITTED = "already_committed"
    DUPLICATE_IDENTITY = "duplicate_identity"
    INVALID_EVENT_GROUP = "invalid_event_group"
    INCOMPLETE_TOOL_GROUP = "incomplete_tool_group"
    RAW_PERSISTENCE_UNAVAILABLE = "raw_persistence_unavailable"
    INVALID_STATE_TRANSITION = "invalid_state_transition"
    INVALID_ARGUMENT = "invalid_argument"
    STALE_DATE = "stale_date"  # V3-P85: a kept proposal would carry a date already past use
    # V3-P66: files
    LIMIT_EXCEEDED = "limit_exceeded"  # too many staged uploads, or too much presented in the hour
    TOO_LARGE = "too_large"  # one file over the operator's limit
    STORE_FULL = "store_full"  # no room in the file store, even after expiring what may expire
    BLOB_MISSING = "blob_missing"  # the stored bytes are gone (or expired, on a recording path)
    BLOB_EXPIRED = "blob_expired"  # the stored bytes expired (§8.5); the record stays
    ATTACHMENT_REFUSED = "attachment_refused"  # an attachment a message may not carry (§4.2)
    # character-day §4.4: a schedule change past its limits; the message is one English sentence the model can act on
    PLAN_REFUSED = "plan_refused"
    # character-day §3.2: a closed day session is only read — no new message, Run, wake or compaction is written
    DAY_CLOSED = "day_closed"


# DataError.reason of an upload a message names once more (V3-P84 §4.2: a screen is attached once).
ALREADY_SENT = "already sent"


class DataError(Exception):
    def __init__(self, code: DataErrorCode, message: str = "", *, file_id: FileId | None = None,
                 reason: str | None = None) -> None:
        super().__init__(f"{code.value}: {message}" if message else code.value)
        self.code = code
        self.message = message  # character-day §4.3: a PLAN_REFUSED's sentence, told to the model as it is
        self.file_id = file_id  # ATTACHMENT_REFUSED: the attachment refused
        self.reason = reason  # ATTACHMENT_REFUSED: why, e.g. ALREADY_SENT


# --- Conversation Events (Data v0.2 §4) ---------------------------------------


class EventKind(Enum):
    USER_MESSAGE = "user_message"
    ASSISTANT_FINAL = "assistant_final"
    ASSISTANT_TOOL_CALLS = "assistant_tool_calls"
    TOOL_RESULT = "tool_result"
    WAKE = "wake"  # V3-P87 §3: the server opened this turn


class OriginKind(Enum):
    """Where a user message was sent from (V3-P83 §4.2): how its caller signed in."""

    DEVICE = "device"  # a paired device's token
    WEB = "web"  # the Web session cookie
    CLI = "cli"  # the operator's bearer token
    WAKE = "wake"  # V3-P87 §4.1: a job the server opened, spoken on ``device_id``; never a message's origin


@dataclass(frozen=True)
class MessageOrigin(DeepFrozen):
    kind: OriginKind
    device_id: str | None = None  # DEVICE only


class InterfaceKind(Enum):
    WEB = "web"
    CLI = "cli"
    WINDOWS_COMPANION = "windows_companion"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class InterfaceCapabilities(DeepFrozen):
    """User-interface support, never model capabilities or execution permissions."""

    file_download: bool = False

    def __post_init__(self) -> None:
        super().__post_init__()
        if type(self.file_download) is not bool:
            raise ValueError("interface capabilities must be booleans")


@dataclass(frozen=True)
class InterfaceSnapshot(DeepFrozen):
    """V3-P93: server-resolved support when this input/wake was accepted."""

    kind: InterfaceKind
    capabilities: InterfaceCapabilities = InterfaceCapabilities()
    version: int = 1

    def __post_init__(self) -> None:
        super().__post_init__()
        if type(self.version) is not int or self.version != 1 or not isinstance(self.kind, InterfaceKind):
            raise ValueError("unsupported interface snapshot")
        if not isinstance(self.capabilities, InterfaceCapabilities):
            raise ValueError("invalid interface capabilities")
        if self.kind is InterfaceKind.UNKNOWN and self.capabilities != InterfaceCapabilities():
            raise ValueError("unknown interfaces cannot declare capabilities")


def interface_kind(origin: MessageOrigin | None) -> InterfaceKind:
    """Only an authenticated origin selects the interface; missing/legacy is unknown."""
    if not isinstance(origin, MessageOrigin):
        return InterfaceKind.UNKNOWN
    return {OriginKind.WEB: InterfaceKind.WEB, OriginKind.CLI: InterfaceKind.CLI,
            OriginKind.DEVICE: InterfaceKind.WINDOWS_COMPANION,
            OriginKind.WAKE: InterfaceKind.WINDOWS_COMPANION}.get(origin.kind, InterfaceKind.UNKNOWN)


class AsleepAnswer(Enum):
    """character-day §5.5: how a sleeping HIMARI meets a message (or a poke) of its day session."""

    NONE = "none"  # no answer: the message stays, no Run (it only stirs)
    MUMBLE = "mumble"  # half asleep: one short mumbled line, no Tools; the sleep goes on
    WAKE = "wake"  # woken: an ordinary turn; the sleep ends (woken by the user)


@dataclass(frozen=True)
class UserMessage(DeepFrozen):
    blocks: tuple[ContentBlock, ...]
    answers_call: CallRef | None = None  # older history only: the ask_user call it answered (T7a, gone)
    origin: MessageOrigin | None = None  # V3-P83: set by the entry point; None for older messages
    interface: InterfaceSnapshot | None = None  # V3-P93: server-owned; None on legacy inputs
    # character-day §5.6: received while HIMARI slept, and how it was met; set by the Driver, never by a
    # client. None: awake (or an older message).
    while_asleep: AsleepAnswer | None = None


@dataclass(frozen=True)
class AssistantFinal(DeepFrozen):
    blocks: tuple[ContentBlock, ...]


@dataclass(frozen=True)
class AssistantToolCalls(DeepFrozen):
    calls: tuple[ToolCall, ...]  # original order; ordinals unique
    commentary: tuple[ContentBlock, ...] = ()  # V3-P16


@dataclass(frozen=True)
class ToolResultRecord(DeepFrozen):
    call: CallRef
    model_result: ModelToolResult


class WakeKind(Enum):
    """V3-P87 §3.2: why the server opened a turn."""

    LOOP_DUE = "loop_due"
    RETURNED = "returned"
    REMINDER = "reminder"
    FOCUS = "focus"  # V3-P87; no longer made (V3-P90), still read
    GAME_ENDED = "game_ended"  # V3-P87; no longer made (V3-P90), still read
    TASK_ENDED = "task_ended"  # V3-P90 §5.3: a game or a video that clearly ended
    FREE = "free"  # V3-P90 §7, character-work §7.2: no news — the user at the computer a while; the model may pass
    TRY = "try"  # the operator's try (§11)
    ANNIVERSARY = "anniversary"  # character-bond §6: a day to mark together; the model may pass
    WORK_DONE = "work_done"  # character-work §7.1: HIMARI just finished a piece of its work; the model may pass
    TOGETHER = "together"  # character-day §9: a time HIMARI and the user set together has come (``entry_id``)
    POKE = "poke"  # character-day §5.5, §11: the user poked the sleeping HIMARI on a device and it stirred (§19)


@dataclass(frozen=True)
class WakeReason(DeepFrozen):
    """One reason a wake carries. A loop's or reminder's text is never copied
    here — only its id; the prompt reads the note when it draws the wake."""

    kind: WakeKind
    memory_id: int | None = None  # LOOP_DUE, REMINDER
    away_minutes: int | None = None  # RETURNED
    via: str | None = None  # RETURNED: "unlock", "resume", "reconnect"
    activity: str | None = None  # FOCUS: the category
    minutes: int | None = None  # FOCUS: how long it has gone on; GAME_ENDED, TASK_ENDED: how long it lasted
    late: bool = False  # REMINDER only: past its 24-hour window (§5.3)
    group: str | None = None  # TASK_ENDED (V3-P90 §5.3): "game" or "video"; FREE: the task's group
    step: int | None = None  # FREE (V3-P90 §6.2): which of the timer's steps, from 1
    # ANNIVERSARY (character-bond §6): days together (100, 200, 300), years together, or a ``date``
    # entry of the bond (its id only: the prompt reads its text when it draws the wake).
    # WORK_DONE (character-work §7.1): ``entry_id`` is the work piece's (read when the wake is drawn).
    # TOGETHER (character-day §9): ``entry_id`` is the ``together`` slot's (read when the wake is drawn).
    days: int | None = None
    years: int | None = None
    entry_id: int | None = None


@dataclass(frozen=True)
class Wake(DeepFrozen):
    """V3-P87 §3.2: the Event a server-opened turn answers (a Run's trigger)."""

    wake_id: str
    reasons: tuple[WakeReason, ...]
    device_id: str  # where it is spoken: the current device when it opened
    interface: InterfaceSnapshot | None = None  # V3-P93: captured with the wake, not live presence


type EventPayload = UserMessage | AssistantFinal | AssistantToolCalls | ToolResultRecord | Wake
type AssistantOutput = AssistantFinal | AssistantToolCalls


@dataclass(frozen=True)
class ConversationEvent(DeepFrozen):
    event_id: EventId
    conversation_id: ConversationId
    sequence: int  # per-Conversation, monotonic; the only chronology authority
    kind: EventKind
    payload: EventPayload
    payload_schema_version: int
    # Observational; the model also sees it as time lines (V3-P83 §6.4).
    # ``sequence`` stays the chronology authority.
    created_at: datetime
    origin_run_id: RunId | None = None


# --- Runs (Data v0.2 §5) -----------------------------------------------------


class RunStatus(Enum):
    PREPARED = "prepared"
    DISPATCH_CLAIMED = "dispatch_claimed"  # P0: persisted right before send
    RECEIVED = "received"
    FAILED_NO_RESPONSE = "failed_no_response"
    OUTCOME_UNKNOWN = "outcome_unknown"
    PROJECTED = "projected"
    PROJECTION_FAILED = "projection_failed"
    ABANDONED_NOT_SENT = "abandoned_not_sent"


class RunPurpose(Enum):
    """Why a Run called the Provider. A TURN Run's output becomes a
    Conversation Event; a COMPACTION Run's output becomes only the summary
    in the latest PromptSnapshot (``commit_compaction``), never an Event."""

    TURN = "turn"
    COMPACTION = "compaction"


@dataclass(frozen=True)
class EventRef(DeepFrozen):
    event_id: EventId


@dataclass(frozen=True)
class CompletedGroupRef(DeepFrozen):
    run_id: RunId


type TriggerRef = EventRef | CompletedGroupRef


@dataclass(frozen=True)
class PromptBasis(DeepFrozen):
    """Versions of the sources a Run's input was composed from."""

    event_sequence: int
    state_revision: int
    snapshot_revision: int | None = None
    # Opaque digest of what the input was composed under besides history and
    # state (model, instructions, Tool catalog); set by Prompt (V3-P48).
    context_key: str | None = None
    # Opaque digest of the runtime state's parts no revision tracks (memory,
    # recent episodes, presence); set by Prompt (V3-P89 post-merge review P1).
    state_key: str | None = None


@dataclass(frozen=True)
class RunPreparation(DeepFrozen):
    """Everything persisted by ``prepare_run`` (T2) before any send."""

    raw_request_body: bytes
    raw_content_type: str
    raw_representation: RawRepresentation
    trigger_refs: tuple[TriggerRef, ...]
    prompt_basis: PromptBasis
    provider_key: str
    model_id: str
    purpose: RunPurpose = RunPurpose.TURN


@dataclass(frozen=True)
class RawReceived(DeepFrozen):
    body: bytes
    content_type: str
    usage: TokenUsage | None = None


@dataclass(frozen=True)
class RawNoResponse(DeepFrozen):
    """Known failure: no usable model output (``FAILED_NO_RESPONSE``). When the
    Provider returned an error response, ``body`` is its exact bytes and is
    stored like any received response (V3-018)."""

    error_code: str
    body: bytes | None = None
    content_type: str = ""


@dataclass(frozen=True)
class RawUncertain(DeepFrozen):
    """Send may or may not have reached the Provider; never resent. ``body``
    is what arrived of a streamed response before it ended (V3-P73 §4):
    stored as received, never normalized."""

    error_code: str
    body: bytes | None = None
    content_type: str = ""


type RawRunOutcome = RawReceived | RawNoResponse | RawUncertain


@dataclass(frozen=True)
class RunRecord(DeepFrozen):
    run_id: RunId
    conversation_id: ConversationId
    status: RunStatus
    trigger_refs: tuple[TriggerRef, ...]
    prompt_basis: PromptBasis
    provider_key: str
    model_id: str
    raw_representation: RawRepresentation
    usage: TokenUsage | None = None
    error_code: str | None = None
    continuation: ProviderContinuation | None = None  # set at projection (V3-049)
    purpose: RunPurpose = RunPurpose.TURN


# --- Tool executions (Data v0.2 §6) ----------------------------------------


class ToolExecutionStatus(Enum):
    PENDING = "pending"
    EXECUTION_CLAIMED = "execution_claimed"  # P0: persisted before effectful run
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    CANCELLED = "cancelled"
    # Effect uncertain. Never turned into a synthetic failure. Blocks the next
    # Run until an "outcome unknown" model_result is attached (V3-047).
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ToolExecutionRecord(DeepFrozen):
    call: CallRef
    ordinal: int
    tool_id: ToolId
    arguments: Mapping[str, JsonValue]
    status: ToolExecutionStatus
    model_result: ModelToolResult | None = None  # what the model sees
    raw_result: JsonValue = None  # the Tool's own execution record; never sent to the model
    # Persisted at claim time. None on an uncertain record means the policy is
    # not known and must be treated as REQUIRE_USER (never silently relaxed).
    unknown_outcome: UnknownOutcomePolicy | None = None
    # The Tool's conversation_record policy, persisted at claim time so a later
    # "outcome unknown" result is recorded the same way whatever the current
    # build registers. None (not claimed / legacy) → record the Event.
    record_event: bool | None = None


@dataclass(frozen=True)
class ProjectionResult(DeepFrozen):
    event: ConversationEvent
    pending: tuple[ToolExecutionRecord, ...]  # empty for AssistantFinal


# --- Files (V3-P66, docs/file-upload-download-v0.1.md) -------------------------
# An uploaded or presented file keeps its bytes in the file store, outside
# every Conversation folder: written once, addressed per principal, expired when the
# store is full or after the keep days (§8.5) — bytes only, the record stays.

MAX_FILE_NAME_BYTES = 200  # UTF-8, after cleaning (§5.3)
MAX_RAW_NAME_BYTES = 1000  # a name given longer than this is refused
MAX_MEDIA_TYPE_CHARS = 127
MAX_STAGED_UPLOADS = 50  # per Conversation
MAX_ATTACHMENTS = 10  # per message (T1)
STAGED_KEEP = timedelta(hours=24)  # staged records older than this are dropped
STAGED_SEND_CUTOFF = timedelta(hours=23)  # sending refuses older staged uploads (§4.2)
BLOB_GRACE = timedelta(hours=1)  # a blob put this recently never expires or goes (§8.2, §8.5)
PRESENT_WINDOW = timedelta(hours=1)
PRESENT_LIMIT_BYTES = 256 * 1024 * 1024  # presented per Conversation per PRESENT_WINDOW (§7.1)
DEFAULT_FILE_MAX_BYTES = 32 * 1024 * 1024
DEFAULT_STORE_MAX_BYTES = 1024 * 1024 * 1024
DEFAULT_KEEP_DAYS = 30
TEMP_PREFIX = ".himari-"  # the folder write Tools' temporary names


class FileOrigin(Enum):
    UPLOAD = "upload"  # the user sent it
    PRESENTED = "presented"  # the model gave it with inside/present_file
    TOOL = "tool"  # an image a Tool returned (V3-P67 §4.2)


@dataclass(frozen=True)
class StoredBlob(DeepFrozen):
    """Bytes in the file store. ``key`` is the address: SHA-256 of the
    principal and the content hash, so equal content is shared only within
    one principal (review C11)."""

    key: str
    sha256: str
    size: int


def blob_key(principal: PrincipalId, sha256: str) -> str:
    return hashlib.sha256(principal.encode("utf-8") + b"\0" + sha256.encode("ascii")).hexdigest()


# --- Images (V3-P67, docs/images-v0.1.md) ----------------------------------------
# A file is an image only by its bytes (PNG, JPEG, GIF, WebP); the model sees
# a copy made once in the image sandbox, never the original (§3.1).

MAX_IMAGE_PIXELS = 50_000_000  # over it nothing is decoded
MAX_IMAGE_SOURCE_BYTES = 32 * 1024 * 1024  # a larger original gets no copy, whatever the file limit (review C1)
MAX_DOCUMENT_BYTES = 32 * 1024 * 1024  # read_document's input, whatever the file limit (V3-P70 §4.4, C4)
IMAGE_HEAD_BYTES = 256 * 1024  # what sniff_image may need (a JPEG's frame header can follow large EXIF)
MAX_IMAGES_PER_REQUEST = 100
MAX_IMAGE_BYTES_PER_REQUEST = 32 * 1024 * 1024  # model copies, before base64 (review B3)
MAX_RESULT_IMAGES = 8  # images one call may return


class ImageStatus(Enum):
    """Set when the record is made; never changes (review N1)."""

    NOT_IMAGE = "not_image"
    TOO_LARGE = "too_large"  # over MAX_IMAGE_PIXELS or MAX_IMAGE_SOURCE_BYTES: never decoded
    UNPREPARED = "unprepared"  # decoding or encoding failed, or a limit was hit
    NO_SANDBOX = "no_sandbox"  # the image sandbox is not available: nothing decoded (review B1)
    READY = "ready"


class ViewState(Enum):
    """Whether the model (and a preview) may get an image now (review B2)."""

    READY = "ready"
    VIEW_EXPIRED = "view_expired"
    NOT_IMAGE = "not_image"
    TOO_LARGE = "too_large"
    UNPREPARED = "unprepared"
    NO_SANDBOX = "no_sandbox"


@dataclass(frozen=True)
class SniffedImage(DeepFrozen):
    media_type: str
    width: int
    height: int


@dataclass(frozen=True)
class PreparedView(DeepFrozen):
    """A model copy as the image sandbox made it (not yet stored)."""

    data: bytes
    media_type: str  # image/jpeg | image/png
    width: int
    height: int


@dataclass(frozen=True)
class ImageView(DeepFrozen):
    blob: StoredBlob
    media_type: str
    width: int
    height: int
    expired: bool = False  # the copy's own expiry, apart from the original's


@dataclass(frozen=True)
class ImageSetup(DeepFrozen):
    """What a new record says of its image (V3-P67 §4.1)."""

    status: ImageStatus
    image_type: str | None = None  # the original's sniffed type
    width: int | None = None
    height: int | None = None
    view: ImageView | None = None  # READY only


NO_IMAGE = ImageSetup(ImageStatus.NOT_IMAGE)


def sniff_image(head: bytes) -> SniffedImage | None:
    """PNG, JPEG, GIF or WebP by their bytes, with the size their header
    says; None for anything else or a header that cannot be read. Pure:
    nothing is decoded (§3)."""
    try:
        if head.startswith(b"\x89PNG\r\n\x1a\n") and head[12:16] == b"IHDR":
            width, height = int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
            return _sized("image/png", width, height)
        if head[:6] in (b"GIF87a", b"GIF89a"):
            return _sized("image/gif", int.from_bytes(head[6:8], "little"), int.from_bytes(head[8:10], "little"))
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            return _webp(head)
        if head[:3] == b"\xff\xd8\xff":
            return _jpeg(head)
    except (IndexError, ValueError):
        return None
    return None


def _sized(media_type: str, width: int, height: int) -> SniffedImage | None:
    return SniffedImage(media_type, width, height) if width > 0 and height > 0 else None


def _webp(head: bytes) -> SniffedImage | None:
    chunk = head[12:16]
    if chunk == b"VP8X":
        return _sized("image/webp", int.from_bytes(head[24:27], "little") + 1, int.from_bytes(head[27:30], "little") + 1)
    if chunk == b"VP8L" and head[20] == 0x2F:
        bits = int.from_bytes(head[21:25], "little")
        return _sized("image/webp", (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
    if chunk == b"VP8 " and head[23:26] == b"\x9d\x01\x2a":
        return _sized("image/webp", int.from_bytes(head[26:28], "little") & 0x3FFF,
                      int.from_bytes(head[28:30], "little") & 0x3FFF)
    return None


_JPEG_FRAMES = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}


def _jpeg(head: bytes) -> SniffedImage | None:
    at = 2
    while at + 4 <= len(head):
        if head[at] != 0xFF:
            return None
        marker = head[at + 1]
        if marker == 0xFF:  # fill byte
            at += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:  # no length
            at += 2
            continue
        length = int.from_bytes(head[at + 2:at + 4], "big")
        if marker in _JPEG_FRAMES:
            return _sized("image/jpeg", int.from_bytes(head[at + 7:at + 9], "big"),
                          int.from_bytes(head[at + 5:at + 7], "big"))
        if marker == 0xDA or length < 2:  # image data before any frame header
            return None
        at += 2 + length
    return None


def image_extension(media_type: str) -> str:
    return {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}.get(media_type, "bin")


@dataclass(frozen=True)
class FileRecord(DeepFrozen):
    file_id: FileId
    version: int  # uploads: always 1; presenting the same path again: +1
    origin: FileOrigin
    name: str  # cleaned (§5.3)
    media_type: str  # display and model note only; never trusted for serving
    blob: StoredBlob
    created_at: datetime
    workspace_path: str | None  # "/workspace/…": where an upload was placed, or what was presented
    attached: bool  # a message or a Tool result refers to it (§4.2)
    expired: bool  # its bytes expired (§8.5)
    call: CallRef | None = None  # a presented file, or a Tool image: its call
    image_status: ImageStatus = ImageStatus.NOT_IMAGE  # V3-P67 §4.1
    image_type: str | None = None  # the original's sniffed type
    image_width: int | None = None
    image_height: int | None = None
    view: ImageView | None = None  # the model copy (READY)

    @property
    def size(self) -> int:
        return self.blob.size


def view_state(record: FileRecord) -> ViewState:
    """The one answer to "may the model see this image now" (review B2): a
    ready copy that has not expired. The original's expiry does not matter."""
    if record.image_status is not ImageStatus.READY or record.view is None:
        return ViewState(record.image_status.value)
    return ViewState.VIEW_EXPIRED if record.view.expired else ViewState.READY


@dataclass(frozen=True)
class PresentedFile(DeepFrozen):
    """A file a Tool gives the user, stored with its result (§7.1)."""

    blob: StoredBlob
    name: str
    media_type: str
    workspace_path: str


_FORMAT = {"Cc", "Cf", "Cs", "Zl", "Zp"}  # control, format (bidi, zero width), lone surrogates, separators


def clean_file_name(raw: str) -> str:
    """A shown name from an untrusted one (§5.3): never a path."""
    name = "".join("_" if c in "/\\" or unicodedata.category(c) in _FORMAT else c
                   for c in unicodedata.normalize("NFC", raw))
    name = name.strip().rstrip(".").strip()
    if name in ("", ".", ".."):
        name = "file"
    if name.startswith(TEMP_PREFIX):
        name = "_" + name
    if len(name.encode("utf-8")) > MAX_FILE_NAME_BYTES:
        stem, dot, ext = name.rpartition(".")
        suffix = f".{ext}" if dot and stem and len(ext.encode("utf-8")) <= 16 else ""
        stem = name[: len(name) - len(suffix)] if suffix else name
        budget = MAX_FILE_NAME_BYTES - len(suffix.encode("utf-8"))
        stem = stem.encode("utf-8")[:budget].decode("utf-8", errors="ignore")
        name = (stem + suffix).strip().rstrip(".").strip() or "file"  # a cut may end in "." or space
    return name


_MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,62}/[a-z0-9][a-z0-9!#$&^_.+-]{0,62}$")
OCTET_STREAM = "application/octet-stream"


def clean_media_type(declared: str | None, name: str) -> str:
    """``type/subtype`` from a declared Content-Type (parameters dropped), else
    from the name's extension, else octet-stream (review C9)."""
    if declared:
        candidate = declared.split(";", 1)[0].strip().lower()
        if len(candidate) <= MAX_MEDIA_TYPE_CHARS and _MEDIA_TYPE.match(candidate):
            return candidate
    guessed, _ = mimetypes.guess_type(name, strict=False)
    return guessed if guessed and _MEDIA_TYPE.match(guessed) else OCTET_STREAM


class FileCatalog(Protocol):
    """FILE-02: a Conversation's file records. Authorizes like every port."""

    async def stage_upload(self, scope: TrustedScope, blob: StoredBlob, name: str, media_type: str,
                           image: ImageSetup = NO_IMAGE) -> FileRecord:
        """A staged upload (§4.2). LIMIT_EXCEEDED at MAX_STAGED_UPLOADS;
        BLOB_MISSING if the blob (or the image's copy) is gone or expired;
        DAY_CLOSED in a closed day session (character-day §3.2: only read)."""
        ...

    async def stage_tool_file(self, scope: TrustedScope, call: CallRef, blob: StoredBlob, name: str,
                              media_type: str, image: ImageSetup) -> FileRecord:
        """A staged image a Tool returned (V3-P67 §4.2); the call's result
        attaches it (T7). BLOB_MISSING as for uploads."""
        ...

    async def count_staged(self, scope: TrustedScope) -> int:
        """Staged uploads, counted before the bytes are received (review C10)."""
        ...

    async def set_placement(self, scope: TrustedScope, file_id: FileId, workspace_path: str) -> None:
        """Where a staged upload was placed (§5.2). INVALID_STATE_TRANSITION once attached."""
        ...

    async def check_attachments(self, scope: TrustedScope, file_ids: Sequence[FileId], *,
                                now: datetime | None = None) -> Sequence[FileRecord]:
        """The T1 checks (§4.2) as a read, plus the STAGED_SEND_CUTOFF;
        ATTACHMENT_REFUSED naming the file, or the records in order."""
        ...

    async def read_files(self, scope: TrustedScope, *, limit: int = 50, offset: int = 0) -> Sequence[FileRecord]:
        """Attached files, one per version, newest first."""
        ...

    async def read_file_records(self, scope: TrustedScope, refs: Sequence[tuple[FileId, int]]
                                ) -> Sequence[FileRecord]:
        """The given versions (staged ones too); unknown ones are left out."""
        ...

    async def read_call_files(self, scope: TrustedScope, calls: Sequence[CallRef]
                              ) -> Mapping[CallRef, Sequence[FileRecord]]:
        """The files each call presented (calls without any are left out);
        never its Tool images (V3-P67, review C4)."""
        ...

    async def read_presented_files(self, scope: TrustedScope, *, limit: int = 50,
                                   offset: int = 0) -> Sequence[FileRecord]:
        """V3-P93 §6.2: presented files of succeeded, publicly visible Tool calls
        in this Conversation, filtered before paging, newest first."""
        ...

    async def read_presented_file(self, scope: TrustedScope, file_id: FileId,
                                  version: int) -> FileRecord | None:
        """V3-P93 §6.2: one presented file of a succeeded, publicly visible Tool
        call in this Conversation, or None if not found or not publicly presented."""
        ...

    async def presented_bytes_since(self, scope: TrustedScope, since: datetime) -> int: ...


class BlobStore(Protocol):
    """The file store's bytes (§4.5), owned by Data."""

    async def put(self, principal: PrincipalId, chunks: AsyncIterator[bytes], max_bytes: int) -> StoredBlob:
        """Store the bytes; TOO_LARGE past ``max_bytes`` (reading stops there),
        STORE_FULL when no room can be made (nothing is expired then)."""
        ...

    def open(self, blob: StoredBlob) -> BinaryIO:
        """Read-only. BLOB_EXPIRED, or BLOB_MISSING if gone or of another size."""
        ...

    async def prepare_image(self, principal: PrincipalId, blob: StoredBlob, views: ViewMaker | None) -> ImageSetup:
        """V3-P67 §3, §3.1: sniff the stored bytes; for an image the model may
        see, have ``views`` make the copy and store it. Never raises for the
        image's sake: the status says what became of it."""
        ...

    def read_bytes(self, key: str) -> bytes:
        """A whole blob by its address (a model copy when a request is sent,
        §7.4). BLOB_EXPIRED or BLOB_MISSING."""
        ...


class ViewMaker(Protocol):
    """Makes model copies in the image sandbox (V3-P67 §3.1)."""

    @property
    def available(self) -> bool: ...

    async def make(self, original: bytes) -> PreparedView | str:
        """The copy, or a short reason why there is none."""
        ...


class FileRetention(Protocol):
    """Bootstrap only, like ConversationRetention: cleanup across principals."""

    async def drop_stale_staged(self, cutoff: datetime) -> int: ...

    async def sweep_blobs(self, now: datetime) -> None:
        """Unreferenced blobs (§8.2), the keep-days expiry (§8.5), files
        without a row, stale staging; failures are logged, not raised."""
        ...


@dataclass(frozen=True)
class ToolCompletion(DeepFrozen):
    """Terminal outcome committed by the Tool Dispatcher (DATA-08, T7/T8)."""

    status: ToolExecutionStatus  # SUCCEEDED | FAILED | DENIED | CANCELLED
    model_result: ModelToolResult
    raw_result: JsonValue = None  # persisted on the ToolExecutionRecord
    record_event: bool = True  # the Tool's conversation_record policy (FULL → True)
    # V3-P67 §4.2: the model result's attachments that are this call's staged
    # Tool images get attached in the same transaction (INVALID_ARGUMENT for
    # any other attachment). Nothing to set: Data finds them in model_result.
    # V3-P66 §7.1: files given to the user, recorded in the same transaction.
    # Only with SUCCEEDED (INVALID_ARGUMENT otherwise); BLOB_MISSING when a
    # blob is gone or expired, LIMIT_EXCEEDED past the hour's limit.
    presented_files: tuple[PresentedFile, ...] = ()
    # V3-P71: a memory Tool's note, written in the same transaction with this
    # call as its source. Only with SUCCEEDED (INVALID_ARGUMENT otherwise);
    # MemoryStore's errors when the note cannot be written.
    memory_change: MemoryChange | None = None
    # character-day §18.6: a schedule Tool's change (origin HIMARI), written in the same transaction. Only with
    # SUCCEEDED (INVALID_ARGUMENT otherwise); LifeStore.write_plan's errors when it cannot be written.
    plan_change: PlanChange | None = None


@dataclass(frozen=True)
class RecoveryReport(DeepFrozen):
    """Result of restart cleanup (T-R). Nothing is re-executed."""

    runs_outcome_unknown: tuple[RunId, ...] = ()
    runs_abandoned_not_sent: tuple[RunId, ...] = ()
    tools_unknown: tuple[CallRef, ...] = ()
    # Reported, not changed: the response is stored but not yet projected.
    # Control must project these (no new Provider call) before any new Run.
    runs_received_unprojected: tuple[RunId, ...] = ()
    # RECEIVED COMPACTION Runs whose summary was never committed: marked
    # PROJECTION_FAILED. Their basis may be stale, so the next Run compacts
    # again if it still has to; the stored response stays on the Run.
    compactions_discarded: tuple[RunId, ...] = ()


# --- Prompt snapshot (Data v0.2 §7) ----------------------------------------


@dataclass(frozen=True)
class PromptSnapshot(DeepFrozen):
    """Latest full provider-neutral prompt; one per Conversation.

    ``body`` is owned and interpreted by Prompt; Data stores it opaquely.
    """

    revision: int
    schema_version: int
    body: JsonValue
    covers_through_sequence: int
    basis: PromptBasis


# --- Operational state (Data v0.2 §8, CurrentState v0.1 §2) -----------------


@dataclass(frozen=True)
class PersistedStateBasis(DeepFrozen):
    """One coherent, permission-filtered read (DATA-06)."""

    conversation_sequence: int
    state_revision: int
    # The Environment kinds this Conversation has, ordered by kind; each is
    # set when the Conversation is created (an old one's kind this server
    # no longer has stays listed: whoever reads it leaves it out).
    environments: tuple[EnvironmentKind, ...]


# --- External Tool sources (V3-P53) -----------------------------------------

MAX_EXTERNAL_SETTINGS = 64


@dataclass(frozen=True)
class ExternalSourceSetting(DeepFrozen):
    """A Conversation's own choice for one External Tool source (e.g. an MCP
    server): on or off regardless of the operator's default for it."""

    source: str
    enabled: bool


# --- A Conversation's model (V3-P64) ----------------------------------------

MAX_MODEL_ID_CHARS = 200
MAX_EFFORT_CHARS = 32


@dataclass(frozen=True)
class ModelSetting(DeepFrozen):
    """A Conversation's own model choice, kept exactly as made: whether the
    model is still offered is decided when it is used (V3-P64 §4.2)."""

    model_id: str
    reasoning_effort: str | None = None  # None: the model's default effort


# --- User instructions (V3-P69) ----------------------------------------------

MAX_INSTRUCTIONS_CHARS = 10_000  # code points, after newlines are normalized (Q3)


class InstructionScope(Enum):
    """Whose conversations a text applies to (V3-P69 §3.1); a project or lab
    scope is added by its packet. The companion persona (V3-P89 §4) moved to
    PersonaStore (V3-P108 §5)."""

    USER = "user"  # every Conversation of the principal


@dataclass(frozen=True)
class Instructions(DeepFrozen):
    text: str  # "" when there are none
    revision: int  # 0: never written
    updated_at: datetime | None = None


class InstructionsStore(Protocol):
    """V3-P69: the instructions a user writes for their conversations.

    The principal comes from the trusted entry point (a signed-in user),
    never from model output: no Tool reaches this port (§3). Writes are
    compare-and-set on ``revision`` so two editors never silently overwrite
    each other."""

    async def read_instructions(self, principal: PrincipalId, scope: InstructionScope) -> Instructions:
        """The current text; none written: ``Instructions("", 0)``."""
        ...

    async def write_instructions(self, principal: PrincipalId, scope: InstructionScope, text: str,
                                 expected_revision: int) -> Instructions:
        """Replaces the text when ``expected_revision`` is the current one and
        returns the new record (revision + 1). Newlines become ``\n``; only
        whitespace is no text. VERSION_CONFLICT for another revision,
        TOO_LARGE past MAX_INSTRUCTIONS_CHARS, INVALID_ARGUMENT for a control
        character other than newline and tab, or a scope not offered."""
        ...


# --- Companion personas (V3-P108 §4, §5) ----------------------------------------

MAX_PERSONAS = 20  # a principal's personas (Q6)
MAX_PERSONA_NAME_CHARS = 40  # code points, after trimming
MAX_PERSONA_CHARS = MAX_INSTRUCTIONS_CHARS  # a persona's text, counted as instructions are
# character-persona-fields §2: a persona's fields, in the order its text is made from them. ``name`` (HIMARI's own
# name, as the old starter's first line) is added to the design's 13; ``familiar`` and ``close`` are the hidden
# stories (their lines are told from that bond stage on); ``free`` is everything else, told last and as written.
PERSONA_FIELDS = ("name", "relation", "world", "calls_user", "personality", "speech", "examples", "habits", "likes",
                  "makes", "never", "familiar", "close", "free")
PERSONA_HIDDEN_FIELDS = ("familiar", "close")


@dataclass(frozen=True)
class Persona(DeepFrozen):
    """One of the user's companion personas. ``name`` is the user's label in
    the list and never reaches the model; ``text`` is who HIMARI is, made from
    ``fields`` when it is saved (character-persona-fields §3)."""

    persona_id: int  # per principal, never given out again
    name: str
    text: str  # "" allowed: a persona not written yet
    revision: int
    created_at: datetime
    updated_at: datetime
    fields: Mapping[str, str] = FrozenMap()  # PERSONA_FIELDS' filled ones, in that order


@dataclass(frozen=True)
class PersonaChoice(DeepFrozen):
    """Which persona is told now: one of the principal's or none."""

    persona_id: int | None
    revision: int  # 0: never chosen
    updated_at: datetime | None = None


@dataclass(frozen=True)
class PersonaList(DeepFrozen):
    personas: tuple[Persona, ...]  # by id
    choice: PersonaChoice


class PersonaStore(Protocol):
    """V3-P108 §5.2: the companion personas a user keeps and the one told now.

    The principal comes from the trusted entry point (a signed-in user), never
    from model output: no Tool reaches this port. Changes are compare-and-set on
    ``revision``. A name is 1..MAX_PERSONA_NAME_CHARS characters after trimming
    and unique per principal, compared trimmed, NFC and case-folded
    (DUPLICATE_IDENTITY); a text is checked as instructions are (newlines become
    ``\n``, only whitespace is no text, TOO_LARGE past MAX_PERSONA_CHARS,
    INVALID_ARGUMENT for a control character other than newline and tab).

    character-persona-fields §3: a persona is saved from its ``fields`` (each
    checked so, an unknown key INVALID_ARGUMENT) and its text is made from them;
    the text alone (``fields`` None) is saved as the field ``free``."""

    async def list_personas(self, principal: PrincipalId) -> PersonaList:
        ...

    async def create_persona(self, principal: PrincipalId, name: str, text: str,
                             fields: Mapping[str, str] | None = None) -> Persona:
        """A new persona at revision 1. LIMIT_EXCEEDED past MAX_PERSONAS."""
        ...

    async def update_persona(self, principal: PrincipalId, persona_id: int, name: str, text: str,
                             expected_revision: int, fields: Mapping[str, str] | None = None) -> Persona:
        """Replaces its name and text (revision + 1). NOT_FOUND, VERSION_CONFLICT."""
        ...

    async def delete_persona(self, principal: PrincipalId, persona_id: int, expected_revision: int) -> PersonaChoice:
        """Deletes it; when it was chosen, the choice becomes none in the same
        transaction (its revision + 1). The choice after. NOT_FOUND, VERSION_CONFLICT."""
        ...

    async def choose_persona(self, principal: PrincipalId, persona_id: int | None,
                             expected_revision: int) -> PersonaChoice:
        """Chooses the persona told from each conversation's next turn, or none.
        NOT_FOUND for an id that is not the principal's, VERSION_CONFLICT."""
        ...

    async def active_persona(self, principal: PrincipalId) -> Persona | None:
        """The chosen persona, read at the start of a turn; None when none is chosen."""
        ...


# --- Memory (V3-P71) -----------------------------------------------------------

MAX_MEMORY_CHARS = 500  # one note, in code points after newlines are normalized (Q3)
MAX_LOOP_CHARS = 300  # an open loop (V3-P85 §5.1)
MAX_MEMORIES = 1_000  # a principal's live notes (Q3); retired ones are not counted (V3-P85 §5.4)


class MemoryCategory(Enum):
    """The fixed kinds of note (V3-P71 Q5; LOOP from V3-P85 §5.1), in the order
    they are shown."""

    USER = "user"  # who the user is: work, environment, languages
    PREFERENCE = "preference"  # how they like answers and work done
    PROJECT = "project"  # what they are working on: projects, goals, decisions
    REFERENCE = "reference"  # paths, links, names they use
    LOOP = "loop"  # something ahead: a plan, a promise, a date (V3-P85 §7)


class MemoryOrigin(Enum):
    """Who wrote a note's current text."""

    USER = "user"  # the Memory screen or the CLI
    MODEL = "model"  # memory_save in a Conversation
    EXTRACTOR = "extractor"  # a memory pass after a Conversation went quiet (V3-P85 §4)


@dataclass(frozen=True)
class Memory(DeepFrozen):
    memory_id: int  # shown as m<id>; never reused for another note of the principal
    category: MemoryCategory
    text: str
    origin: MemoryOrigin
    # The memory_save call that wrote it, in which Conversation; None when the
    # user wrote it outside a Conversation. A permanent delete of that
    # Conversation deletes the note (D5).
    source_conversation: ConversationId | None
    source_call: CallRef | None
    revision: int
    created_at: datetime
    updated_at: datetime
    # V3-P85 §5.3: a loop's date and a fact's end, as written (a date or an
    # offset time) and as the instant fixed when written; every comparison
    # uses the instant.
    due_at: str | None = None
    due_instant: datetime | None = None
    valid_until: str | None = None
    valid_instant: datetime | None = None
    source_pass: str | None = None  # origin EXTRACTOR: the pass and item that wrote it
    source_item: int | None = None
    # A proposal the user kept (§10.2): automatic passes leave it alone.
    user_approved_at: datetime | None = None
    # V3-P87 §5.3: a loop's reminder, as written and as its instant, and how it was
    # delivered (scheduled, claimed, delivered, missed; None without one). A new
    # remind_at schedules it afresh; automatic passes never write one.
    remind_at: str | None = None
    remind_instant: datetime | None = None
    remind_state: str | None = None
    reminded_at: datetime | None = None


class MemoryChangeKind(Enum):
    SAVE = "save"  # a new note, or a rewrite of one (expected_revision set)
    FORGET = "forget"


@dataclass(frozen=True)
class MemoryChange(DeepFrozen):
    """What a memory Tool's succeeded call does, committed with its result
    (ToolCompletion.memory_change) so the note and the result that reports
    it are written together or not at all. The source is the call itself.

    SAVE of a new note: ``memory_id`` from ``allocate_memory_id``, no
    ``expected_revision``. SAVE of an existing note and FORGET: the note's
    id and the revision the Tool read. A SAVE writes the note's dates as
    given (V3-P85 §5.3): a loop's due, a fact's end, each with its instant."""

    kind: MemoryChangeKind
    memory_id: int
    category: MemoryCategory | None = None  # SAVE only
    text: str | None = None  # SAVE only
    expected_revision: int | None = None
    due_at: str | None = None  # SAVE of a loop
    due_instant: datetime | None = None
    valid_until: str | None = None  # SAVE of another note
    valid_instant: datetime | None = None
    remind_at: str | None = None  # SAVE of a loop the user asked to be reminded of (V3-P87 §5.3)
    remind_instant: datetime | None = None


@dataclass(frozen=True)
class NoteDates(DeepFrozen):
    """The dates the user gives a note (V3-P85 §10.2): all None clears them."""

    due_at: str | None = None
    due_instant: datetime | None = None
    valid_until: str | None = None
    valid_instant: datetime | None = None
    remind_at: str | None = None  # a loop's reminder (V3-P87 §5.3)
    remind_instant: datetime | None = None


def parse_note_date(value: object, zone: str) -> tuple[str, datetime] | None:
    """V3-P85 §5.3: a note's date as written and as the instant compared — a
    date (``2026-11-04``: its end of day in ``zone``) or a time with its UTC
    offset; None for anything else (no offset, not a date, the calendar's edge)."""
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    try:
        if len(value) == 10:
            day = date.fromisoformat(value)
            end = datetime.combine(day + timedelta(days=1), time(), tzinfo=ZoneInfo(zone)) - timedelta(microseconds=1)
            return value, end.astimezone(UTC)
        moment = datetime.fromisoformat(value)
        if moment.tzinfo is None:
            return None
        return value, moment.astimezone(UTC)
    except (ValueError, OverflowError, ZoneInfoNotFoundError):
        return None


@dataclass(frozen=True)
class RecentEpisode(DeepFrozen):
    """An episode with its Conversation's title, as the Current State shows it (§8)."""

    episode: Episode
    title: str | None


class MemoryStore(Protocol):
    """V3-P71: the notes HIMARI keeps about a principal. The principal comes
    from the trusted entry point or the Tool's TrustedScope, never from model
    output. Errors: NOT_FOUND (no such note of this principal),
    VERSION_CONFLICT (written since the revision given), TOO_LARGE (past
    MAX_MEMORY_CHARS), LIMIT_EXCEEDED (MAX_MEMORIES reached),
    INVALID_ARGUMENT (empty text, a control character other than newline and
    tab, a lone surrogate)."""

    async def list_memories(self, principal: PrincipalId) -> tuple[Memory, ...]:
        """By category (MemoryCategory order), newest first within each."""
        ...

    async def read_memory(self, principal: PrincipalId, memory_id: int) -> Memory: ...

    async def allocate_memory_id(self, principal: PrincipalId) -> int:
        """A new id for a note memory_save will add; one left unused is a gap."""
        ...

    async def save_memory(self, principal: PrincipalId, category: MemoryCategory, text: str, *,
                          memory_id: int | None = None, expected_revision: int | None = None,
                          dates: NoteDates | None = None) -> Memory:
        """The user's write. No ``memory_id``: a new note. With it: rewrites
        that note when ``expected_revision`` is its revision; it becomes the
        user's text and keeps its source. ``dates`` (V3-P85): None keeps a
        rewritten note's dates (but not one its new category does not have);
        given, they are the note's — checked as a Tool's are."""
        ...

    async def delete_memory(self, principal: PrincipalId, memory_id: int,
                            expected_revision: int | None = None) -> Memory:
        """The user's delete; returns the note as it was. Gone at once."""
        ...

    async def restore_memory(self, principal: PrincipalId, before: Memory, *, expected_revision: int) -> Memory:
        """An Undo (V3-P71 §4.2): puts the note ``before`` names back to
        before's category, text, origin and source, at its next revision.
        NOT_FOUND when the note is gone (a deleted note is never brought back)
        or before's source Conversation was deleted for good (one in the
        trash is fine: its purge still takes the note); VERSION_CONFLICT when
        the note is not at ``expected_revision``."""
        ...

    async def count_source_memories(self, scope: TrustedScope) -> int:
        """The notes this Conversation's calls wrote: deleted with it."""
        ...

    async def unretire_memory(self, principal: PrincipalId, memory_id: int, *, expected_revision: int,
                              now: datetime) -> Memory:
        """V3-P85 §5.4: bring back a note an automatic forget retired, as it
        was, at its next revision. NOT_FOUND when it is not retired, retired
        RETIRED_KEEP ago or more, or its source Conversation was deleted for
        good; VERSION_CONFLICT when not at ``expected_revision``;
        LIMIT_EXCEEDED when MAX_MEMORIES live notes are kept."""
        ...

    async def memory_revision(self, principal: PrincipalId, memory_id: int) -> tuple[int, bool] | None:
        """A note's revision and whether it is retired; None when there is no such note."""
        ...

    async def memory_revisions(self, principal: PrincipalId, memory_ids: Sequence[int]) -> dict[int, tuple[int, bool]]:
        """``memory_revision`` of many notes in one read; a note that is not there is left out."""
        ...

    async def count_source_episodes(self, scope: TrustedScope) -> int:
        """The episodes of this Conversation: deleted with it (V3-P85 §10.3)."""
        ...

    async def page_episodes(self, principal: PrincipalId, *, before: tuple[datetime, int] | None,
                            limit: int) -> tuple[RecentEpisode, ...]:
        """V3-P85 §10.3: the Timeline, newest first by (ended_at, episode_id),
        after ``before`` (the last one shown); not of a Conversation in the trash."""
        ...

    async def update_episode(self, principal: PrincipalId, episode_id: int, text: str, *,
                             expected_revision: int) -> Episode:
        """The user's rewrite: it becomes the user's (passes no longer extend
        it). NOT_FOUND, VERSION_CONFLICT, TOO_LARGE, INVALID_ARGUMENT."""
        ...

    async def delete_episode(self, principal: PrincipalId, episode_id: int, *, expected_revision: int) -> None:
        """NOT_FOUND, VERSION_CONFLICT."""
        ...

    async def recent_episodes(self, principal: PrincipalId, *, since: datetime, besides: ConversationId | None,
                              limit: int) -> tuple[RecentEpisode, ...]:
        """V3-P85 §8: the newest episodes ended since ``since`` (at most
        ``limit``, newest first), with their Conversation's title — not of
        ``besides`` (the Conversation asking) nor of one in the trash."""
        ...

    async def count_episodes(self, principal: PrincipalId) -> int: ...

    async def find_episodes(self, principal: PrincipalId, words: Sequence[str], *, start: datetime | None = None,
                            end: datetime | None = None, limit: int = 200) -> tuple[RecentEpisode, ...]:
        """V3-P85 §9: episodes that ended in [start, end] holding any of the
        folded ``words`` (none: all of them), not of a Conversation in the
        trash, ranked before the ``limit`` is taken (PR #137 review): with
        words, most words held first, then newest (a word given twice counts
        once); without words but with a day, in time order (oldest first);
        with neither, newest first."""
        ...


# --- Memory passes (V3-P85) -----------------------------------------------------------

MAX_EPISODE_CHARS = 600  # one episode (§6.1)
MAX_EPISODES = 10_000  # a principal's episodes; past it the oldest go (§6.2)
SKIP_AFTER_FAILURES = 3  # failed or interrupted passes from one cursor before its part is skipped (§4.5)
STALE_DUE = timedelta(days=14)  # an open loop this far past its date is cleaned up (§5.1)
STALE_UNDATED = timedelta(days=90)  # an open loop without a date, untouched this long
EXPIRED_KEEP = timedelta(days=30)  # a fact this far past valid_until (§5.2)
RETIRED_KEEP = timedelta(days=30)  # a retired note, while Undo can still bring it back (§5.4)
PROPOSAL_KEEP = timedelta(days=30)  # a proposal nobody kept or discarded (§4.9)


class PassKind(Enum):
    LIVE = "live"  # the unread part after the cursor
    BACKFILL = "backfill"  # the part before the upgrade (§4.9)
    RETRY = "retry"  # a skipped part, once more (§4.9)


class PassStatus(Enum):
    RUNNING = "running"
    APPLIED = "applied"
    FAILED = "failed"
    INTERRUPTED = "interrupted"  # the process ended while it ran
    DISCARDED = "discarded"  # the Conversation or its cursor changed before it was applied (§4.4)
    SKIPPED = "skipped"  # SKIP_AFTER_FAILURES counted from one cursor
    RETRIED = "retried"  # a skipped pass whose part a retry read


class PassAction(Enum):
    """What one decision of a pass does, as the memory module planned it."""

    ADD = "add"
    UPDATE = "update"
    FORGET = "forget"  # retires the note (§5.4)
    PROPOSE = "propose"  # kept as a proposal the user keeps or discards (§4.9, §4.4)
    DROP = "drop"  # refused before Data (``reason`` says why); only recorded


@dataclass(frozen=True)
class PassItem(DeepFrozen):
    """One decision of a pass. ``proposal`` is the decision as the model wrote
    it (JSON text), kept for the record. ADD/PROPOSE carry the note's fields;
    UPDATE carries them with ``memory_id`` and ``expected_revision``; FORGET
    only those two; DROP and PROPOSE carry ``reason`` (for PROPOSE:
    ``proposed`` or ``sensitive_proposed``)."""

    item: int
    proposal: str
    action: PassAction
    reason: str | None = None
    memory_id: int | None = None
    expected_revision: int | None = None
    category: MemoryCategory | None = None
    text: str | None = None
    due_at: str | None = None
    due_instant: datetime | None = None
    valid_until: str | None = None
    valid_instant: datetime | None = None


@dataclass(frozen=True)
class EpisodePlan(DeepFrozen):
    """The pass's episode (§6.2). ``extend``: the episode of this Conversation
    to rewrite, at ``expected_revision``; Data turns it into a new one when it
    is gone, the user's, rewritten since, or ended more than ``extend_within``
    before ``started_at``."""

    text: str
    started_at: datetime
    ended_at: datetime
    user_sequences: tuple[int, ...]
    himari_sequences: tuple[int, ...]
    extend: int | None = None
    expected_revision: int | None = None
    extend_within: timedelta = timedelta(hours=3)


@dataclass(frozen=True)
class Episode(DeepFrozen):
    episode_id: int  # e<id>, never reused for another episode of the principal
    conversation_id: ConversationId
    from_sequence: int  # exclusive
    through_sequence: int
    started_at: datetime
    ended_at: datetime
    text: str
    user_sequences: tuple[int, ...]
    himari_sequences: tuple[int, ...]
    origin: MemoryOrigin  # EXTRACTOR, or USER once the user rewrote it
    source_pass: str | None
    revision: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class MemoryCursor(DeepFrozen):
    """A Conversation's reading position (§4.2, §4.9). A Conversation with no
    row reads from 0."""

    through_sequence: int = 0  # read through here; only moves forward
    failures: int = 0  # failed or interrupted passes from this cursor
    retry_after: datetime | None = None
    baseline_sequence: int = 0  # the upgrade's cursor: (backfill_through, baseline] is unread
    backfill_through: int = 0


@dataclass(frozen=True)
class DueConversation(DeepFrozen):
    """A Conversation with work for a live pass (§4.1): not trashed, a user
    message after the cursor, quiet since ``last_event_at``, nothing
    unfinished, past its retry time."""

    scope: TrustedScope
    cursor: MemoryCursor
    last_event_at: datetime


@dataclass(frozen=True)
class PassStart(DeepFrozen):
    kind: PassKind
    from_sequence: int  # exclusive: the cursor it reads from
    through_sequence: int  # the end of the part it reads
    snapshot_sequence: int  # the Conversation's last event when the input was made (§4.4)
    provider_key: str
    model_id: str
    omitted_chars: int = 0  # the middle of long messages left out (§4.2)
    retry_of: str | None = None  # RETRY: the skipped pass


@dataclass(frozen=True)
class MemoryPass(DeepFrozen):
    pass_id: str
    conversation_id: ConversationId
    kind: PassKind
    from_sequence: int
    through_sequence: int
    snapshot_sequence: int
    status: PassStatus
    provider_key: str
    model_id: str
    calls: int
    reserved_tokens: int  # of the call in flight (§4.8)
    input_tokens: int  # as the Provider reported them
    output_tokens: int
    unmetered_tokens: int  # reservations of calls that reported no usage, counted as used
    over_estimate: int  # calls whose reported use passed their reservation
    omitted_chars: int
    error: str | None
    retry_of: str | None
    created_at: datetime
    finished_at: datetime | None

    @property
    def charged_tokens(self) -> int:
        """What it counts against the daily budget."""
        return self.reserved_tokens + self.input_tokens + self.output_tokens + self.unmetered_tokens


@dataclass(frozen=True)
class PassApplied(DeepFrozen):
    """``apply_pass``'s answer: APPLIED with each item's outcome (``applied``,
    ``proposed``, ``sensitive_proposed`` or why it was dropped) and the
    episode written, if any; or DISCARDED with why (nothing was applied)."""

    status: PassStatus
    outcomes: tuple[str, ...] = ()
    episode_id: int | None = None
    reason: str | None = None


@dataclass(frozen=True)
class PassItemRecord(DeepFrozen):
    """One item of a pass as recorded (§11). ``proposal``: the decision as the
    model wrote it; ``planned``: the note it names (JSON, as ``rules.planned_json``)
    for an ADD, UPDATE or proposal; ``previous``: the note before an UPDATE or
    FORGET (JSON, as ``rules.memory_json``)."""

    pass_id: str
    item: int
    proposal: str
    planned: str | None
    outcome: str
    target: str | None
    previous: str | None
    written_revision: int | None
    decided_at: datetime | None
    created_at: datetime


def memory_from_record(previous: str) -> Memory | None:
    """A note as an item's ``previous`` recorded it (``rules.memory_json``), for
    an Undo (V3-P85 §10.1); None when the record does not hold one. Its times
    are not recorded: ``restore_memory`` keeps the note's own."""
    try:
        raw = json.loads(previous)
        call = raw.get("source_call")

        def moment(key: str) -> datetime | None:
            return None if raw.get(key) is None else datetime.fromisoformat(raw[key])

        epoch = datetime.fromtimestamp(0, UTC)
        return Memory(int(raw["memory_id"]), MemoryCategory(raw["category"]), str(raw["text"]),
                      MemoryOrigin(raw["origin"]),
                      None if raw.get("source_conversation") is None else ConversationId(raw["source_conversation"]),
                      None if not isinstance(call, dict) else CallRef(RunId(call["run_id"]), CallId(call["call_id"])),
                      int(raw["revision"]), epoch, epoch, due_at=raw.get("due_at"), due_instant=moment("due_instant"),
                      valid_until=raw.get("valid_until"), valid_instant=moment("valid_instant"),
                      source_pass=raw.get("source_pass"), source_item=raw.get("source_item"),
                      user_approved_at=moment("user_approved_at"),
                      # V3-P87 §5.3 (PR #157 review P1-2); records before it have none
                      remind_at=raw.get("remind_at") if raw.get("remind_instant") else None,
                      remind_instant=moment("remind_instant") if raw.get("remind_at") else None)
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def planned_item(planned: str, item: int, action: PassAction) -> PassItem | None:
    """The note an item's ``planned`` names, as a PassItem to check and write; None when unreadable."""
    try:
        raw = json.loads(planned)

        def moment(key: str) -> datetime | None:
            return None if raw.get(key) is None else datetime.fromisoformat(raw[key])

        return PassItem(item, planned, action, category=MemoryCategory(raw["category"]), text=raw["text"],
                        due_at=raw.get("due_at"), due_instant=moment("due_instant"),
                        valid_until=raw.get("valid_until"), valid_instant=moment("valid_instant"))
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


@dataclass(frozen=True)
class Proposal(DeepFrozen):
    record: PassItemRecord
    conversation_id: ConversationId
    kind: PassKind  # LIVE: a sensitive note without a request; BACKFILL, RETRY: a past part's
    title: str | None
    in_trash: bool


@dataclass(frozen=True)
class PassSummary(DeepFrozen):
    charged_today: int
    last_applied_at: datetime | None
    skipped: tuple[MemoryPass, ...]  # still SKIPPED (not retried) since then
    failed: int  # FAILED or INTERRUPTED since then


@dataclass(frozen=True)
class MemoryCleanup(DeepFrozen):
    stale_loops: int = 0
    expired_facts: int = 0
    retired: int = 0
    proposals: int = 0


MAX_WAKE_REASONS = 4
# §5.3: a loop with one is not cleaned up as stale (V3-P107 §2: ``told`` is no more)
PENDING_REMINDERS = frozenset({"scheduled", "claimed"})
REMIND_ON_TIME = timedelta(hours=24)  # §5.3: a reminder is told on time within this of its moment
REMIND_LATE = timedelta(days=7)  # §5.3 (Q9): after that, at a later return, until this; then missed  # V3-P87 §6.4
REMINDER_ATTEMPTS = 3  # V3-P87 §5.3: a reminder whose wakes ended unanswered this often is missed


class WakeStatus(Enum):
    OPENED = "opened"  # a free wake (V3-P90 §7.3): not shown yet
    SPEAKING = "speaking"  # V3-P90 §7.3: a free wake decided to speak, shown; still in progress
    ANSWERED = "answered"  # a final answer
    CANCELLED = "cancelled"  # no assistant output; the user won (§4.3)
    FAILED = "failed"
    PASSED = "passed"  # V3-P90 §7.3: a free wake the model passed — never shown
    DROPPED = "dropped"  # V3-P90 §7.3: a free wake that ended before it was shown — never shown

    @property
    def in_progress(self) -> bool:
        """V3-P90 §7.3 (PR #162 review P1-7): opened or speaking — not ended."""
        return self in (WakeStatus.OPENED, WakeStatus.SPEAKING)


# V3-P90 §7.3, character-bond §6: what a wake may pass on — HIMARI's own openings with no news;
# character-work §7.1: a piece of work just finished too
PASSABLE_KINDS = frozenset({WakeKind.FREE, WakeKind.ANNIVERSARY, WakeKind.WORK_DONE})


def free_wake(reasons: Sequence[WakeReason]) -> bool:
    """V3-P90 §7.3: a wake whose reasons are all FREE — its own path (hidden until it speaks, may pass).
    character-bond §6: an anniversary takes the same path (it may pass too); character-work §7.1: so does
    a piece of work just finished."""
    return bool(reasons) and all(r.kind in PASSABLE_KINDS for r in reasons)


@dataclass(frozen=True)
class WakeRecord(DeepFrozen):
    wake_id: str
    principal: PrincipalId
    conversation_id: ConversationId
    event_sequence: int
    reasons: tuple[WakeReason, ...]
    device_id: str
    status: WakeStatus
    created_at: datetime
    ended_at: datetime | None
    # V3-P91 §8: the Run whose screen-Tool group a free wake ran unshown; its events are unshown and
    # gone with the wake (``unshown_wakes``, ``read_events``). None: it held none.
    held_run_id: RunId | None = None
    # character-mind §3.3, §8: the thought of a free wake that passed (or was dropped) unshown — the owner's
    # to see on the Web, never spoken or sent to a device. None: none.
    thought: str | None = None


class WakeStore(Protocol):
    """V3-P87 §3.3: the server-opened turns and what keeps them from repeating."""

    async def begin_wake(self, scope: TrustedScope, reasons: Sequence[WakeReason], device_id: str, *,
                         keys: Sequence[str] = (), reminders: Sequence[int] = (),
                         interface: InterfaceSnapshot | None = None) -> ConversationEvent:
        """One transaction (§3.3): each key is taken (DUPLICATE_IDENTITY when one
        was taken already), each reminder goes ``scheduled → claimed``
        (INVALID_STATE_TRANSITION when it is not scheduled), the wake Event is
        appended and its record written. Refused as an Event is (trash; a closed
        day session, DAY_CLOSED). INVALID_ARGUMENT: no reason, or more than
        MAX_WAKE_REASONS."""
        ...

    async def end_wake(self, principal: PrincipalId, wake_id: str, status: WakeStatus) -> WakeRecord:
        """``opened → status`` (V3-P90: ``speaking →`` answered, cancelled or failed), once (a later call
        changes nothing). The
        reminders it claimed (§5.3), only while still claimed by it: ANSWERED →
        delivered (``reminded_at`` the moment); else back to scheduled with one
        more attempt, missed at the third."""
        ...

    async def read_wake(self, principal: PrincipalId, wake_id: str) -> WakeRecord: ...

    async def opened_wakes(self) -> tuple[WakeRecord, ...]:
        """Every principal's wakes still in progress — opened or speaking (a restart settles them, §4.4)."""
        ...

    async def reveal_wake(self, principal: PrincipalId, wake_id: str) -> bool:
        """V3-P90 §7.3: a free wake decided to speak — ``opened → speaking``, before it is shown.
        False (and nothing changes) when it is not opened (ended meanwhile, the user won)."""
        ...

    async def finalize_hidden_run(self, scope: TrustedScope, run_id: RunId, wake_id: str, status: WakeStatus, *,
                                  thought: str | None = None) -> None:
        """V3-P90 §7.3 (PR #162 review P1-10): one transaction — the Run ``RECEIVED → PROJECTED`` with
        no event, the free wake ``opened → status`` (PASSED or DROPPED). Each only from where it should
        be: a Run no longer RECEIVED stays, a wake no longer opened stays. character-mind §3.3: the
        ``thought`` its answer began with is kept with the wake as it ends (the owner's only)."""
        ...

    async def hold_wake_run(self, scope: TrustedScope, wake_id: str, run_id: RunId) -> bool:
        """V3-P91 §8: an opened free wake runs this Run's tool group (it only looks at the screen) without
        being shown — recorded before the Run's output is, so its events are never seen unless the wake
        speaks. True when held (or held already); False — nothing changes — when the wake is no longer
        opened or holds another Run."""
        ...

    async def wake_of_run(self, scope: TrustedScope, run_id: RunId) -> WakeRecord | None:
        """V3-P90 §7.3: the free wake whose Event is among the Run's triggers, if any (a passed or
        dropped one too, whose Event ``read_events`` no longer gives)."""
        ...

    async def unshown_wakes(self, scope: TrustedScope) -> frozenset[int]:
        """V3-P90 §7.3: the event sequences of this Conversation's free wakes not shown yet (opened), and
        of the events of the Run each holds (V3-P91 §8). Passed and dropped ones never come out of
        ``read_events`` at all, their held Run's events neither."""
        ...

    async def wake_thoughts(self, scope: TrustedScope, *, after: int = 0, through: int | None = None,
                            limit: int | None = None) -> tuple[WakeRecord, ...]:
        """character-mind §3.3 (review P1-5): this Conversation's free wakes that ended unshown (passed or
        dropped) with a thought, their event after ``after`` and at most ``through`` (None: any), by
        sequence, at most ``limit`` (None: all) — for the owner's Web only."""
        ...

    async def wakes_since(self, principal: PrincipalId, since: datetime) -> tuple[WakeRecord, ...]:
        """V3-P87 §6.2: the principal's wakes created at or after ``since``, oldest first."""
        ...

    async def taken_keys(self, principal: PrincipalId, keys: Sequence[str]) -> frozenset[str]:
        """§5: those of ``keys`` a wake took, or that were expired."""
        ...

    async def expire_keys(self, principal: PrincipalId, keys: Sequence[str]) -> None:
        """§5.1: take ``keys`` with no wake — past their window, never spoken. A key taken already stays."""
        ...

    async def miss_reminders(self, principal: PrincipalId, before: datetime) -> int:
        """§5.3 (Q9): scheduled reminders whose moment is before
        ``before`` become missed (shown on the memory screen); how many."""
        ...

    async def release_keys(self, principal: PrincipalId, wake_id: str) -> None:
        """§4.4 (part 2): give back the keys of a wake a restart cut before it said
        anything, so a trigger still in its window may be spoken of again."""
        ...


class MemoryPassLog(Protocol):
    """V3-P85: the record and the application of memory passes. A pass reads a
    Conversation's settled part without its lock; ``apply_pass`` checks in
    its own transaction that what it read still stands. The principal comes
    from the scope the memory module was given, never from model output."""

    async def read_memory_cursor(self, scope: TrustedScope) -> MemoryCursor: ...

    async def memory_due(self, *, quiet_before: datetime, now: datetime, after: ConversationId | None = None,
                         limit: int = 50) -> Sequence[DueConversation]:
        """Conversations of every principal with work for a live pass, by
        Conversation id after ``after``: a page of at most ``limit``."""
        ...

    async def last_event(self, scope: TrustedScope) -> tuple[int, datetime] | None:
        """The sequence and time of the Conversation's last Event (none: None).
        NOT_AUTHORIZED for one that is gone or trashed."""
        ...

    async def unfinished_work(self, scope: TrustedScope) -> bool:
        """A Run PREPARED, DISPATCH_CLAIMED or RECEIVED, or a Tool execution
        EXECUTION_CLAIMED (§4.1). Waiting for the user is not unfinished."""
        ...

    async def last_episode(self, scope: TrustedScope) -> Episode | None:
        """The newest episode of this Conversation."""
        ...

    async def list_episodes(self, principal: PrincipalId) -> tuple[Episode, ...]:
        """Newest first: by ``ended_at``, then id."""
        ...

    async def pass_usage(self, principal: PrincipalId, since: datetime) -> int:
        """The tokens charged by the principal's calls prepared since then
        (§4.8; by the call, so one past midnight counts on its own day): a
        call in flight or with an unknown outcome its reservation, one that
        reported usage that use, one never sent nothing."""
        ...

    async def begin_pass(self, scope: TrustedScope, start: PassStart) -> str:
        """A RUNNING pass; its id. NOT_FOUND for a Conversation that is gone
        or trashed; INVALID_STATE_TRANSITION while another pass of the
        principal runs (one at a time per principal, §4.8)."""
        ...

    async def prepare_pass_call(self, pass_id: str, body: bytes, content_type: str, reserved_tokens: int) -> int:
        """Store one call's request before it is sent (no store, no send) and
        reserve its tokens; the call's number. RAW_PERSISTENCE_UNAVAILABLE
        when it cannot be stored. INVALID_STATE_TRANSITION unless RUNNING."""
        ...

    async def mark_pass_call_dispatched(self, pass_id: str, call: int) -> None: ...

    async def record_pass_call(self, pass_id: str, call: int, outcome: RawRunOutcome) -> None:
        """The call's response, if any. Its reservation becomes the reported
        usage, or is counted as used when none is reported."""
        ...

    async def read_pass_response(self, pass_id: str, call: int) -> bytes | None: ...

    async def fail_pass(self, pass_id: str, error: str, retry_after: datetime) -> PassStatus:
        """A RUNNING pass failed: FAILED, counted on its cursor (live) — or
        SKIPPED once SKIP_AFTER_FAILURES are counted from that cursor, which
        then moves past the part (§4.5). A failed retry or backfill is only
        FAILED: the skipped pass stays SKIPPED, ``backfill_through`` stays."""
        ...

    async def interrupt_running_passes(self, retry_after: datetime) -> int:
        """At startup: every RUNNING pass becomes INTERRUPTED and counts as a
        failure, as ``fail_pass``; how many."""
        ...

    async def apply_pass(self, pass_id: str, items: Sequence[PassItem], episode: EpisodePlan | None, *,
                         now: datetime) -> PassApplied:
        """One transaction (§4.4): first the Conversation exists and is not
        trashed, the pass RUNNING, its part overlaps no APPLIED pass, and by
        kind — LIVE: the cursor is ``from_sequence``, the last event is
        ``snapshot_sequence`` and nothing is unfinished; BACKFILL:
        ``backfill_through`` is ``from_sequence`` within the baseline;
        RETRY: the skipped pass is still SKIPPED. Otherwise DISCARDED and
        nothing changes (also when the pass itself is gone: its Conversation
        was deleted for good). Then each item on its own (the note's revision, not
        retired, not the user's or kept by the user, the limits), the
        episode, the cursor (LIVE) or ``backfill_through`` (BACKFILL), and
        the pass APPLIED."""
        ...

    async def read_pass(self, pass_id: str) -> MemoryPass: ...

    async def list_passes(self, scope: TrustedScope) -> tuple[MemoryPass, ...]:
        """This Conversation's passes, oldest first."""
        ...

    async def read_pass_outcomes(self, pass_id: str) -> tuple[str, ...]:
        """Each item's outcome, by item."""
        ...

    async def read_pass_items(self, pass_id: str) -> tuple[PassItemRecord, ...]:
        """Each item as recorded, by item. NOT_FOUND for no such pass."""
        ...

    async def applied_pass_items(self, scope: TrustedScope, *,
                                 limit: int) -> tuple[tuple[MemoryPass, tuple[PassItemRecord, ...]], ...]:
        """§10.1 in one read (PR #138 review): this Conversation's newest ``limit``
        APPLIED passes that did something to show (an item applied, kept, or a
        sensitive proposal), oldest first, each with those items by item."""
        ...

    async def list_proposals(self, principal: PrincipalId) -> tuple[Proposal, ...]:
        """V3-P85 §10.2: the principal's proposals nobody kept or discarded, oldest first."""
        ...

    async def decide_proposal(self, principal: PrincipalId, pass_id: str, item: int, *, keep: bool,
                              now: datetime, dates: NoteDates | None = None) -> Memory | None:
        """Keep (the note it names, added now: EXTRACTOR, its pass as source,
        ``user_approved_at`` set; ``dates`` given replace the proposal's) or
        discard a proposal; the note kept, or None. NOT_FOUND when the pass is
        not the principal's (or is gone); VERSION_CONFLICT when the item is
        not a proposal still waiting; STALE_DATE when the note would keep a
        date the cleanup takes at once (a loop due STALE_DUE ago, a fact past
        its end — PR #138 review); for a keep, the note's own checks
        (TOO_LARGE, INVALID_ARGUMENT, LIMIT_EXCEEDED). After a refusal it still waits."""
        ...

    async def pass_summary(self, principal: PrincipalId, *, day_start: datetime, since: datetime) -> PassSummary:
        """V3-P85 §10.3's status: today's charged tokens, the last applied pass,
        and the parts skipped and passes failed since ``since``."""
        ...

    async def clean_memories(self, now: datetime) -> MemoryCleanup:
        """The hourly cleanup (§5.1, §5.2, §5.4, §4.9): loops due more than 14
        days ago or without a date and untouched for 90 days, facts 30 days
        past their end, notes retired 30 days ago, proposals left 30 days."""
        ...


# --- HIMARI's life (character-life v0.1) ------------------------------------------------

MAX_LIFE_CHARS = 500  # a fact or a plan, in code points after newlines are normalized
MAX_DIARY_CHARS = 1_500  # one day's diary
MAX_LIFE_FACTS = 200  # a persona's live facts; past it the oldest not the user's retire (§2)
NO_PERSONA = 0  # L2: the life HIMARI has while no persona is chosen
MAX_ARC_TITLE_CHARS = 60  # character-world §4: an arc's title, one line
MAX_ARC_CHARS = 200  # an arc's goal and its ending, one line each
MAX_OPEN_ARCS = 2  # W3: a persona's open arcs; a start past it is left out
MAX_WORK_CHARS = 3_000  # character-work §4: a work piece's text (the worker is asked for 400 to 2,000)
MAX_WORK_SUMMARY_CHARS = 200  # a work piece's one-line summary, in HIMARI's voice
MAX_SO_FAR_CHARS = 600  # §3: an arc's (a project's) "so far", rewritten with each piece
MAX_THOUGHT_CHARS = 200  # character-mind §5.2: a kept thought, a line or two in HIMARI's voice
MAX_THOUGHTS = 30  # §5.2: a persona's live kept thoughts; past it the oldest retires
THOUGHT_DAYS = timedelta(days=3)  # §5.2: a kept thought lasts this long from when it was kept
INTENT_DAYS = timedelta(days=2)  # an ``intent`` one (what HIMARI wants to say or do) this long
MAX_EVENT_CHARS = 200  # character-mind §7: what happened to HIMARI, one sentence
MAX_EVENTS_PER_DAY = 2  # §7: a persona's live events of one date
# character-day §4.1, §4.4: HIMARI's schedule — a plan is a slot with its start (``day``, ``time_of_day``) and length
MIN_PLAN_MINUTES = 5
MAX_PLAN_MINUTES = 720
DEFAULT_PLAN_MINUTES = 60  # a slot written without a length (and every plan from before v29)
SCHEDULE_DAYS = 7  # §4.4: a slot is dated today or within the seven days after (as PLAN_DAYS)
MIN_SLEEP_MINUTES = 5 * 60  # §4.4: a sleep slot is 5 to 11 hours
MAX_SLEEP_MINUTES = 11 * 60
NIGHT_STARTS = time(21, 0)  # §4.4: the night of date D: a slot starting from D 21:00 ...
NIGHT_ENDS = time(5, 0)  # ... to D+1 05:00 (inclusive: the latest a sleep may start)
SLEEP_ENDS_FROM = time(5, 0)  # §4.4: a sleep slot ends from 05:00 ...
SLEEP_ENDS_BY = time(12, 0)  # ... to 12:00 the morning after
MAX_HIMARI_PLAN_WRITES = 20  # §4.4: slots HIMARI's Tools may change a day (from the user's midnight)
MAX_PLAN_REASON_CHARS = 200  # §4.1: why a slot was moved or cancelled, one line in HIMARI's voice
# character-diary §3, §16.2: the night's diary slot — placed by the keeper, only moved by HIMARI and the user
DIARY_TEXT = "일기 쓰기"
DIARY_MINUTES = 30
MIN_DIARY_MINUTES = 10
MAX_DIARY_MINUTES = 60
DIARY_EARLIEST = time(18, 0)  # the diary slot of date D starts from D 18:00, and ends before D's night's sleep slot


class LifeKind(Enum):
    FACT = "fact"  # settled about HIMARI itself (canon)
    DIARY = "diary"  # one day's diary: one per persona and date
    PLAN = "plan"  # something ahead, on a date
    BOND = "bond"  # character-bond §2: settled between the user and HIMARI (BondStore, never list_life)
    ARC = "arc"  # character-world §4: a thread of HIMARI's life over weeks (the story writer starts and ends it)
    WORK = "work"  # character-work §4: a piece of an arc's (a project's) work, made by the worker at a work plan
    THOUGHT = "thought"  # character-mind §5.2: what HIMARI thought and left unsaid, kept a few days by a memory pass
    EVENT = "event"  # §7: what happened to HIMARI on a date (its ``day``, ``time_of_day``), decided by the world


class ThoughtAbout(Enum):
    """character-mind §5.2: what a kept thought is about."""

    USER = "user"  # an impression of the user, a worry
    SELF = "self"  # HIMARI's own feeling, left unshown
    WORK = "work"  # its own work
    INTENT = "intent"  # something it wants to say or do (kept INTENT_DAYS)


class LifeOrigin(Enum):
    """Who wrote an entry's current text."""

    WRITER = "writer"  # the story writer (never a bond entry, character-bond §2); character-day: the day's keeper too
    CONVERSATION = "conversation"  # a memory pass, from what HIMARI said of itself (§4) or what the two settled
    USER = "user"  # the Web's Settings → Life: automatic writes leave it alone
    HIMARI = "himari"  # character-day §4.2: HIMARI itself, in a conversation, with its schedule Tools


class PlanKind(Enum):
    """character-day §4.1: what a slot of HIMARI's schedule is."""

    SLEEP = "sleep"  # the night's sleep: one a night, placed by the keeper, moved by HIMARI or the user
    WORK = "work"  # work time for a project (``arc_id``)
    TOGETHER = "together"  # a time set with the user (only HIMARI's Tools write one)
    OWN = "own"  # something HIMARI does alone
    DIARY = "diary"  # character-diary §3: the night's diary slot — one a night, placed by the keeper, only moved


class PlanState(Enum):
    """character-day §4.1: a slot that no longer stands (kept a while: the diary and the next day see it)."""

    CANCELLED = "cancelled"  # HIMARI cancelled it (with why)
    MISSED = "missed"  # a ``together`` slot the user never came to (§5.4)


class BondTag(Enum):
    """character-bond §1: what a bond entry is."""

    NAME = "name"  # what one calls the other ("앞으로 대장이라고 부를게")
    PROMISE = "promise"  # a promise between the two
    JOKE = "joke"  # a joke only the two share
    FIRST = "first"  # something done together for the first time
    DATE = "date"  # a day to mark every year (``month_day``)


@dataclass(frozen=True)
class LifeEntry(DeepFrozen):
    """One entry of a persona's life (§2). ``day`` (YYYY-MM-DD, the user's date)
    only for a diary, a plan or an arc (the day it started); ``tag`` only for a bond
    entry and ``month_day`` (MM-DD) only for a ``date`` one (character-bond §2).
    character-world §4: an arc's ``text`` is its goal, with its ``title`` and, once
    closed, its ``end_day`` and ``ending``; a diary's ``arc_id`` is the arc its day moved.
    character-work §3, §4: an arc (a project) also has its ``so_far``; a plan's ``arc_id`` is the project
    it is work time for (a work plan); a work piece has its plan's ``day`` and ``time_of_day``, its
    ``title``, the piece as ``text``, its ``summary``, its project (``arc_id``) and plan (``plan_id``)."""

    entry_id: int  # per principal, never given out again
    persona_id: int  # NO_PERSONA: the life of no chosen persona
    kind: LifeKind
    day: str | None
    text: str
    origin: LifeOrigin
    revision: int
    created_at: datetime
    updated_at: datetime
    # §2: where a conversation's fact came from (its Conversation and HIMARI's answers), and the
    # write or memory pass that wrote it last.
    source_conversation: ConversationId | None = None
    source_sequences: tuple[int, ...] = ()
    source: str | None = None
    tag: BondTag | None = None
    month_day: str | None = None
    time_of_day: str | None = None  # character-state §3 (S3): a plan's start, HH:MM in the user's zone
    title: str | None = None  # character-world §4: an arc's title
    end_day: str | None = None  # an arc's last day (YYYY-MM-DD) once closed; None: open
    ending: str | None = None  # a closed arc's one-line ending
    arc_id: int | None = None  # a diary's: the arc its day moved; a plan's, a piece's: the project (character-work)
    summary: str | None = None  # character-work §4: a work piece's one line
    plan_id: int | None = None  # a work piece's plan
    so_far: str | None = None  # §3: an arc's "so far" (None: no piece yet)
    # character-mind §5.2: a kept thought's ``about`` and when it stops being told (``expires_at``); its
    # conversation and answers are ``source_conversation`` and ``source_sequences``, the pass ``source``.
    about: ThoughtAbout | None = None
    expires_at: datetime | None = None
    draft: bool = False  # character-mind §4.4: a work piece begun at the start step, not finished yet
    # character-day §4.1: a plan is a slot — its length, what it is, whether it still stands and why it was last
    # moved or cancelled (HIMARI's line). Every plan has a length and a kind from v29 on.
    minutes: int | None = None
    plan_kind: PlanKind | None = None
    plan_state: PlanState | None = None
    reason: str | None = None

    @property
    def arc_open(self) -> bool:
        """character-world §4: an arc not closed yet."""
        return self.kind is LifeKind.ARC and self.end_day is None

    @property
    def live_plan(self) -> bool:
        """character-day §4.1: a plan that still stands (not cancelled, not missed)."""
        return self.kind is LifeKind.PLAN and self.plan_state is None

    @property
    def sleep_slot(self) -> bool:
        """character-day §4.4: a live sleep slot."""
        return self.live_plan and self.plan_kind is PlanKind.SLEEP

    @property
    def diary_slot(self) -> bool:
        """character-diary §3: a live diary slot."""
        return self.live_plan and self.plan_kind is PlanKind.DIARY


def plan_kind_of(entry: LifeEntry) -> PlanKind:
    """character-day §4.1: a plan's kind — as stored, else (a plan from before v29) a work plan with a project,
    something of its own without."""
    if entry.plan_kind is not None:
        return entry.plan_kind
    return PlanKind.WORK if entry.arc_id is not None else PlanKind.OWN


def plan_span(entry: LifeEntry, zone: tzinfo) -> tuple[datetime, datetime] | None:
    """character-day §4.1: a timed plan's [start, end) in ``zone`` (its length, else DEFAULT_PLAN_MINUTES);
    None without a date and a time."""
    if entry.kind is not LifeKind.PLAN or not entry.day or not entry.time_of_day:
        return None
    try:
        start = datetime.combine(date.fromisoformat(entry.day), time.fromisoformat(entry.time_of_day), tzinfo=zone)
    except ValueError:
        return None
    return start, start + timedelta(minutes=entry.minutes or DEFAULT_PLAN_MINUTES)


def night_of(start: datetime) -> date | None:
    """character-day §4.4: the night a moment (its own zone) begins in — D for D 21:00 to D+1 05:00 (inclusive),
    None in the day between."""
    clock = start.timetz().replace(tzinfo=None)
    if clock >= NIGHT_STARTS:
        return start.date()
    if clock <= NIGHT_ENDS:
        return start.date() - timedelta(days=1)
    return None


def diary_night(start: datetime) -> date | None:
    """character-diary §3: the night a diary slot starting at ``start`` (its own zone) belongs to — D from D 18:00
    (DIARY_EARLIEST) to D+1 05:00 (inclusive: before a sleep slot starting as late as it may), None in the day
    between."""
    clock = start.timetz().replace(tzinfo=None)
    if clock >= DIARY_EARLIEST:
        return start.date()
    if clock <= NIGHT_ENDS:
        return start.date() - timedelta(days=1)
    return None


@dataclass(frozen=True)
class PlanWrite(DeepFrozen):
    """character-day §4.3, §18.2: a slot to write — new (``entry_id`` None) or a change of a live one (a move
    too). ``time_of_day`` HH:MM, ``minutes`` MIN_PLAN_MINUTES..MAX_PLAN_MINUTES, ``reason`` why it was moved or
    changed (HIMARI's line; None keeps the slot's). A ``sleep`` slot is only ever changed by HIMARI or the user;
    character-diary §3: so is a ``diary`` slot (moved only, its kind and text kept)."""

    day: str
    time_of_day: str
    minutes: int
    kind: PlanKind
    text: str
    entry_id: int | None = None
    arc_id: int | None = None
    reason: str | None = None


class PlanOp(Enum):
    """character-day §4.3: what a schedule Tool does."""

    WRITE = "write"  # schedule_write: a slot written or changed
    CANCEL = "cancel"  # schedule_cancel: a slot cancelled with why
    SLEEP_NOW = "sleep_now"  # go_to_sleep: tonight's sleep slot starts now; HIMARI sleeps once the turn ends


@dataclass(frozen=True)
class PlanChange(DeepFrozen):
    """character-day §18.6: one schedule Tool's change, checked and written by Data with the call's result
    (origin HIMARI) — for the persona the Driver gave the call (never one the model names), read in ``zone`` at
    ``at``: WRITE carries ``write``; CANCEL ``entry_id`` and ``reason``; SLEEP_NOW ``reason`` (go_to_sleep also
    marks the keeper's ``sleep_requested_at``)."""

    op: PlanOp
    persona_id: int
    zone: str
    at: datetime
    write: PlanWrite | None = None
    entry_id: int | None = None
    reason: str | None = None


class LifeChangeOp(Enum):
    ADD = "add"
    UPDATE = "update"
    RETIRE = "retire"


@dataclass(frozen=True)
class LifeChange(DeepFrozen):
    """One ``self`` decision of a memory pass (§4), checked by the memory module:
    ADD carries the text; UPDATE the fact, the revision it was read at and the
    text; RETIRE the fact and its revision. ``sequences``: HIMARI's answers it rests on."""

    op: LifeChangeOp
    text: str | None = None
    entry_id: int | None = None
    expected_revision: int | None = None
    sequences: tuple[int, ...] = ()


class ThoughtOp(Enum):
    KEEP = "keep"  # character-mind §5.2: a thought of this part left unsaid, kept
    SAID = "said"  # §5.1: a kept thought HIMARI has now said: it retires


@dataclass(frozen=True)
class ThoughtChange(DeepFrozen):
    """One ``thoughts`` or ``said`` decision of a memory pass (character-mind §5), checked by the
    memory module: KEEP carries the text, what it is about and the answers whose thoughts it rests
    on (``sequences``); SAID the kept thought (``entry_id``)."""

    op: ThoughtOp
    text: str | None = None
    about: ThoughtAbout | None = None
    entry_id: int | None = None
    sequences: tuple[int, ...] = ()


class ArcOp(Enum):
    """character-world §4 (W3): the one move a day's write may make on an arc."""

    START = "start"  # a new arc: its title and goal
    ADVANCE = "advance"  # the day's diary carries an open arc on
    CLOSE = "close"  # the day's diary ends an open arc: its one-line ending


@dataclass(frozen=True)
class ArcMove(DeepFrozen):
    """One arc move of a story writer call, checked by the life module: START carries the
    title and the goal; ADVANCE the arc (``entry_id``); CLOSE the arc and the ending."""

    op: ArcOp
    title: str | None = None
    goal: str | None = None
    entry_id: int | None = None
    ending: str | None = None


@dataclass(frozen=True)
class LifePlan(DeepFrozen):
    """What one story writer call writes for its date (§3), checked by the life module.
    character-state §2, §3: the plans' start times by position (HH:MM; missing or None: no
    time), and HIMARI's mood at the day's end with why, as of ``mood_at`` (None: when applied).
    character-world §4: the day's one arc move, if any. character-work §4: the plans' projects by
    position (an open arc's id, NEW_ARC for the arc this write starts; missing or None: not a work plan)."""

    diary: str
    plans: tuple[tuple[str, str], ...] = ()  # (YYYY-MM-DD, text)
    facts: tuple[str, ...] = ()
    times: tuple[str | None, ...] = ()
    mood: str | None = None  # a name in MOODS other than "neutral"
    mood_reason: str | None = None
    mood_at: datetime | None = None
    arc: ArcMove | None = None
    plan_arcs: tuple[int | None, ...] = ()
    # character-day §4.2, §7: the plans' lengths by position (missing or None: DEFAULT_PLAN_MINUTES); a plan that
    # overlaps a sleep slot is left out alone as it is written
    plan_minutes: tuple[int | None, ...] = ()


NEW_ARC = 0  # character-work §4: a plan's project is the arc the same write starts


@dataclass(frozen=True)
class WorkDraft(DeepFrozen):
    """character-mind §4.4: what a work plan's start step began, checked by the life module: the piece's
    title, its concept in one line (kept as the piece's summary until it is finished) and the draft."""

    title: str
    concept: str
    text: str


@dataclass(frozen=True)
class WorldEvent(DeepFrozen):
    """character-mind §7: one thing the world made happen to HIMARI, checked by the life module."""

    time_of_day: str  # HH:MM, at any hour (character-diary §7: in HIMARI's sleep, only what happens without it)
    text: str  # one sentence, at most MAX_EVENT_CHARS


@dataclass(frozen=True)
class WorkPiece(DeepFrozen):
    """character-work §4: what one worker call made for a work plan, checked by the life module:
    the piece, its title and one-line summary, the project's new "so far", and an ending when
    the piece finishes the project (it closes)."""

    text: str
    title: str
    summary: str
    so_far: str
    done: str | None = None


class LifeStore(Protocol):
    """§2, §6: a persona's life. The principal comes from the trusted entry point,
    never from model output: no Tool reaches this port. The user's changes are
    compare-and-set on the entry's ``revision``. Errors: NOT_FOUND (no live
    entry of this principal), VERSION_CONFLICT, TOO_LARGE (past
    MAX_LIFE_CHARS, a diary MAX_DIARY_CHARS, an arc's goal MAX_ARC_CHARS and its
    title MAX_ARC_TITLE_CHARS), INVALID_ARGUMENT (no text, a control character
    other than newline and tab — none at all in an arc's title — or a title for an
    entry that is not an arc or a work piece, or a summary for one that is not a piece),
    LIMIT_EXCEEDED (MAX_LIFE_FACTS live facts, all the user's). character-work §4: a work piece's
    text is at most MAX_WORK_CHARS, its title as an arc's, its summary MAX_WORK_SUMMARY_CHARS (one line).
    character-mind §5.2: a kept thought is only deleted by the user, never edited (INVALID_ARGUMENT)."""

    async def list_life(self, principal: PrincipalId, persona_id: int, *,
                        since: str | None = None, now: datetime | None = None) -> tuple[LifeEntry, ...]:
        """The persona's live entries: every fact (the user's first, then newest
        first), then its arcs — the open ones (newest first), then those started or
        closed ``since`` or later (character-world §4) — then the plans, diaries and
        work pieces dated ``since`` or later (None: all), newest date first, with every
        piece of an open arc whatever its date (character-work §4), then the kept thoughts
        not expired at ``now`` (None: the clock's), newest first (character-mind §5.2).
        Never the bond (character-bond: BondStore.list_bond)."""
        ...

    async def add_life_fact(self, principal: PrincipalId, persona_id: int, text: str) -> LifeEntry:
        """The user's fact (origin USER), at revision 1. At MAX_LIFE_FACTS the
        oldest fact not the user's retires first (§2)."""
        ...

    async def edit_life(self, principal: PrincipalId, entry_id: int, text: str, *,
                        expected_revision: int, title: str | None = None,
                        summary: str | None = None) -> LifeEntry:
        """The user's rewrite: it becomes the user's (revision + 1). An arc's ``text`` is
        its goal and ``title`` (None: kept) its title (character-world §4); a work piece's
        ``text`` is the piece, with its ``title`` and ``summary`` (None: kept, character-work §4)."""
        ...

    async def retire_life(self, principal: PrincipalId, entry_id: int, *, expected_revision: int) -> None:
        """The user's delete: the entry retires and is no longer listed."""
        ...

    async def apply_life_changes(self, principal: PrincipalId, persona_id: int, changes: Sequence[LifeChange], *,
                                 conversation: ConversationId, source: str, now: datetime) -> tuple[str, ...]:
        """§4: a memory pass's ``self`` decisions on the persona's facts, in one
        transaction, each on its own: ``applied``, or why not (``unknown``: not
        a live fact of this persona; ``user_owned``; ``conflict``: another
        revision; ``duplicate``: the same text is a live fact; ``full``; ``invalid``)."""
        ...

    async def apply_thought_changes(self, principal: PrincipalId, persona_id: int,
                                    changes: Sequence[ThoughtChange], *, conversation: ConversationId, source: str,
                                    now: datetime) -> tuple[str, ...]:
        """character-mind §5.1, §5.2: a memory pass's ``thoughts`` and ``said`` on the persona's kept
        thoughts, in one transaction, each on its own, as ``apply_life_changes`` answers: a KEEP is
        a new thought of ``conversation`` (origin CONVERSATION, expiring THOUGHT_DAYS from ``now``,
        an ``intent`` INTENT_DAYS; past MAX_THOUGHTS the oldest retires; ``duplicate``: the same text
        is kept), a SAID retires a live one of the persona (``unknown`` otherwise); ``invalid``."""
        ...

    async def write_plan(self, principal: PrincipalId, persona_id: int, plan: PlanWrite, *, origin: LifeOrigin,
                         zone: str, now: datetime) -> LifeEntry:
        """character-day §4.3, §4.4, §18.2, one transaction: a slot of the persona's schedule written — new
        (``plan.entry_id`` None: origin's, revision 1) or a live one changed (moved too: it becomes origin's,
        revision + 1; its kind stays a sleep slot's) — within the schedule's limits, read in ``zone`` (the
        user's) at ``now``: PLAN_REFUSED with one English sentence the model can act on (a sleep slot only
        changed, one a night, from 21:00 to 05:00 and ending 05:00 to 12:00 after 5 to 11 hours; no other slot
        over a live sleep slot — character-diary §16.2: but a sleep slot may cover the diary slot; a diary slot
        only moved, its kind never changed, one a night, from 18:00 and ending before that night's sleep slot,
        MIN_DIARY_MINUTES to MAX_DIARY_MINUTES; dated today to SCHEDULE_DAYS on; never a slot already past;
        MAX_HIMARI_PLAN_WRITES a day for HIMARI). The keeper (origin WRITER) places and adjusts sleep slots and
        places diary slots with the shape's checks only.
        NOT_FOUND: no live plan of the persona; INVALID_ARGUMENT, TOO_LARGE: the shape (as a plan's text,
        MAX_PLAN_REASON_CHARS for the reason)."""
        ...

    async def cancel_plan(self, principal: PrincipalId, entry_id: int, reason: str, *, origin: LifeOrigin,
                          zone: str, now: datetime) -> LifeEntry:
        """character-day §4.3: a live slot cancelled with why (kept, ``cancelled``): PLAN_REFUSED for a sleep
        or a diary slot (character-diary §3) or one past, and past MAX_HIMARI_PLAN_WRITES for HIMARI; NOT_FOUND: no
        live plan."""
        ...

    async def mark_plan_missed(self, principal: PrincipalId, entry_id: int, *, now: datetime) -> LifeEntry:
        """character-day §5.4: a live ``together`` slot the user never came to (``missed``, by the keeper).
        NOT_FOUND: no live plan; INVALID_ARGUMENT: not a ``together`` slot."""
        ...


class LifeJob(Enum):
    """character-mind §14.2: what a life write is for."""

    REVIEW = "review"  # §4.3: the day's review, a self run (before it: the story writer's call)
    SETTLE = "settle"  # §5.3: the facts the day's diary settles (the memory model)
    WORLD = "world"  # §7: what happens to HIMARI today (the memory model)
    WORK_START = "work_start"  # §4.4: a work plan's start: the concept and a draft (a self run)
    WORK_END = "work_end"  # §4.4: its end, the piece (character-work §4: the worker's call)
    AMEND = "amend"  # character-diary §5: the diary amended as the day session closes (a self run in that session)


WORK_JOBS = frozenset({LifeJob.WORK_START, LifeJob.WORK_END})  # the jobs of a work plan (``plan_id``)


@dataclass(frozen=True)
class LifeWriteStart(DeepFrozen):
    persona_id: int
    day: str  # the date the diary is for
    provider_key: str
    model_id: str
    body: bytes  # the request, stored before it is sent
    content_type: str
    reserved_tokens: int
    plan_id: int | None = None  # character-work §4: a worker call's work plan; None: a story writer call
    # character-mind §14.2: the job (None: WORK_END with a plan, else REVIEW; a work job needs its plan,
    # no other has one) and why its model was chosen (§4.1)
    job: LifeJob | None = None
    model_reason: str | None = None
    # character-day §7: the day session a diary was written in the context of (None: a self run with no history)
    conversation_id: ConversationId | None = None
    # character-diary §5, §16.4: what a diary's request was made of — the day session's last event sequence it held
    # and the moment its life data was read up to (what comes after is the amend's)
    basis_sequence: int | None = None
    basis_at: datetime | None = None


class LifeWriteStatus(Enum):
    PREPARED = "prepared"  # stored, not sent
    DISPATCHED = "dispatched"  # may have been sent
    RECEIVED = "received"  # the response is stored
    APPLIED = "applied"
    FAILED = "failed"  # no usable answer (``error`` says why)
    INTERRUPTED = "interrupted"  # the process ended before it was done
    # character-diary §5, §6: decided without a call, for good (``error`` says why: an amend with nothing new, of the
    # user's diary, of a session in the Trash, given up…) — as final as APPLIED
    SKIPPED = "skipped"


@dataclass(frozen=True)
class LifeWrite(DeepFrozen):
    """One story writer call (§2: recorded as a memory pass's call is)."""

    write_id: str
    persona_id: int
    day: str
    status: LifeWriteStatus
    provider_key: str
    model_id: str
    reserved_tokens: int
    charged_tokens: int  # what counts against the daily budget
    input_tokens: int | None
    output_tokens: int | None
    error: str | None
    created_at: datetime
    finished_at: datetime | None
    plan_id: int | None = None  # character-work §4: a worker call's work plan
    job: LifeJob = LifeJob.REVIEW  # character-mind §14.2
    model_reason: str | None = None  # §4.1: why its model was chosen
    thought: str | None = None  # §4.1: a self run's thought, kept with it (never shown, never told again)
    conversation_id: ConversationId | None = None  # character-day §7: the day session it was written in
    basis_sequence: int | None = None  # character-diary §5: the session's last event its request held
    basis_at: datetime | None = None  # and the moment its life data was read up to


class LifeWriteLog(Protocol):
    """§3: the story writer's record. Every call's request is stored before it
    is sent and charged at its reservation until the Provider reports usage."""

    async def life_principals(self, active_since: datetime) -> tuple[PrincipalId, ...]:
        """The principals who sent a message since then (in a Conversation not in the trash)."""
        ...

    async def life_attempts(self, principal: PrincipalId, persona_id: int, day: str, *,
                            job: LifeJob = LifeJob.REVIEW, since: datetime | None = None) -> int:
        """The writes of ``job`` made for this persona and date, whatever came of them (character-mind §14.2);
        character-diary §5: with ``since``, only those whose request was made of data read from then on
        (``basis_at``)."""
        ...

    async def work_attempts(self, principal: PrincipalId, plan_id: int, *, job: LifeJob = LifeJob.WORK_END) -> int:
        """character-work §4: the calls of ``job`` made for this work plan, whatever came of them."""
        ...

    async def life_done(self, principal: PrincipalId, persona_id: int, day: str, job: LifeJob) -> bool:
        """character-mind §14.2: whether a write of ``job`` for this persona and date was APPLIED (character-diary
        §5: or SKIPPED — decided for good without a call)."""
        ...

    async def last_life_write(self, principal: PrincipalId, persona_id: int, day: str, *, job: LifeJob,
                              plan_id: int | None = None) -> LifeWrite | None:
        """character-day §10, §18.7: the latest write of ``job`` for this persona and date (with ``plan_id``: for that
        work plan) — whether it ended with no answer decides the backup model; None: none yet."""
        ...

    async def note_life_thought(self, write_id: str, thought: str) -> None:
        """§4.1: the thought a self run's answer began with, kept with its write (its raw response
        holds it too). NOT_FOUND for no such write."""
        ...

    async def life_usage(self, principal: PrincipalId, since: datetime) -> int:
        """The tokens charged by the principal's writes (the worker's calls too) prepared since then."""
        ...

    async def begin_life_write(self, principal: PrincipalId, start: LifeWriteStart) -> str:
        """A PREPARED write; its id. RAW_PERSISTENCE_UNAVAILABLE when the request cannot be stored."""
        ...

    async def mark_life_write_dispatched(self, write_id: str) -> None:
        """INVALID_STATE_TRANSITION unless PREPARED."""
        ...

    async def record_life_write(self, write_id: str, outcome: RawRunOutcome) -> None:
        """The response, if any: RECEIVED (charged at the reported usage, else at
        its reservation) or FAILED with the error. INVALID_STATE_TRANSITION
        unless PREPARED or DISPATCHED."""
        ...

    async def apply_life_write(self, write_id: str, plan: LifePlan, *, now: datetime,
                               zone: str | None = None) -> tuple[LifeEntry, ...]:
        """One transaction (§2, §3): the day's diary (a writer's of that date is
        rewritten, the user's stays), the plans (with their times, character-state
        §3) and the facts (one the same as a live fact is skipped; at
        MAX_LIFE_FACTS the oldest not the user's retires; all the user's:
        skipped), the mood (character-state §2: as MoodStore.set_mood, source
        WRITER, at ``plan.mood_at`` or ``now``), the arc move (character-world §4:
        a start below MAX_OPEN_ARCS open arcs and not titled as an open one, or an
        advance or close of an open arc of the persona — else it is left out alone;
        the diary the writer wrote that day names the arc moved); the write APPLIED.
        character-work §4: a plan's project is kept only when it names an open arc of the persona
        (or NEW_ARC with a start made), else the plan stays without one. character-mind §4.3, §5.2:
        the write is a REVIEW's (else INVALID_ARGUMENT), and the persona's kept thoughts expired by
        ``now`` retire with it (the review has read them).
        The entries written. INVALID_STATE_TRANSITION unless RECEIVED."""
        ...

    async def apply_life_amend(self, write_id: str, day: str, text: str | None, *, mood: str | None = None,
                               mood_reason: str | None = None, now: datetime) -> LifeEntry | None:
        """character-diary §5, §16.2, one transaction: an AMEND write's answer on the persona's diary of ``day`` (the
        write's date) read again now — the user's diary (origin USER), or none, is left alone, the mood too; else
        ``text`` (None: nothing to add) rewrites it (origin WRITER, revision + 1, its source the write) and the mood
        (a name in MOOD_NAMES other than "neutral", with why) is kept at ``now`` as the review's is. The write
        APPLIED either way. The diary as rewritten, else None. INVALID_ARGUMENT when the write is not an AMEND's
        of ``day``, the text out of shape (TOO_LARGE past MAX_DIARY_CHARS); INVALID_STATE_TRANSITION unless
        RECEIVED."""
        ...

    async def add_events(self, write_id: str, events: Sequence[WorldEvent], *, now: datetime) -> tuple[LifeEntry, ...]:
        """character-mind §7, one transaction: a WORLD write's events as entries of kind EVENT on its date
        (origin WRITER, its source the write), each one line of at most MAX_EVENT_CHARS with its time;
        past MAX_EVENTS_PER_DAY live events of the persona that date the rest is left out; the write
        APPLIED. The events kept. INVALID_ARGUMENT when the write is not a WORLD's or an event out of shape;
        INVALID_STATE_TRANSITION unless RECEIVED."""
        ...

    async def apply_settle_write(self, write_id: str, facts: Sequence[str], *,
                                 now: datetime) -> tuple[LifeEntry, ...]:
        """character-mind §5.3, one transaction: the facts a SETTLE write found in the date's diary
        (origin WRITER, its source the write; one the same as a live fact is skipped; at MAX_LIFE_FACTS
        the oldest not the user's retires; all the user's: skipped); the write APPLIED. The facts kept.
        INVALID_ARGUMENT when the write is not a SETTLE's; INVALID_STATE_TRANSITION unless RECEIVED."""
        ...

    async def apply_work_write(self, write_id: str, piece: WorkPiece, *, now: datetime,
                               draft_read: tuple[int, int] | None = None) -> tuple[LifeEntry, ...]:
        """character-work §4, one transaction: the worker call's piece (a WORK entry with its plan's
        day and time, its project and plan), the project's "so far" rewritten and, with ``done``, the
        project closed on the plan's day with that ending; the write APPLIED. The piece first, then
        the project. character-mind §4.4: the write is a WORK_END's, and when the plan's start step left
        a draft, the piece fills that entry (no longer a draft, made ``now``). ``draft_read``: the plan's
        draft (id, revision) as read when the request was built, None for none — VERSION_CONFLICT when
        the plan's live draft is not that one at that revision now (the user edited or deleted it, so
        the piece rests on stale content: nothing is written). INVALID_ARGUMENT when the call is not a
        work end's, its plan is no live work plan of an open arc, or the plan has a finished piece
        already; INVALID_STATE_TRANSITION unless RECEIVED."""
        ...

    async def apply_work_start(self, write_id: str, draft: WorkDraft, *, now: datetime) -> LifeEntry:
        """character-mind §4.4, one transaction: a WORK_START write's draft as the plan's WORK entry, a
        draft (its title, the concept as its summary, the draft as its text, its plan's day and time); the
        write APPLIED. INVALID_ARGUMENT when the write is not a work start's, its plan is no live work plan
        of an open arc, or the plan has a piece (a draft too) already; INVALID_STATE_TRANSITION unless RECEIVED."""
        ...

    async def fail_life_write(self, write_id: str, error: str) -> None:
        """A write not yet APPLIED, FAILED or INTERRUPTED becomes FAILED with why."""
        ...

    async def skip_life_write(self, write_id: str, reason: str) -> None:
        """character-diary §5, §6: a PREPARED write (never sent) becomes SKIPPED with why, charged nothing — its job is
        decided for that date for good. INVALID_STATE_TRANSITION unless PREPARED."""
        ...

    async def interrupt_life_writes(self) -> int:
        """At startup: every write left PREPARED, DISPATCHED or RECEIVED becomes
        INTERRUPTED (one never sent is charged nothing); how many."""
        ...

    async def list_life_writes(self, principal: PrincipalId, *, limit: int = 20) -> tuple[LifeWrite, ...]:
        """The newest first."""
        ...

    async def session_writes(self, principal: PrincipalId, conversation_id: ConversationId) -> tuple[LifeWrite, ...]:
        """character-day §7, §18.11: the writes made in a day session's context (its diaries and, character-diary §5,
        their amends), oldest first."""
        ...


# --- The bond (character-bond v0.1) -----------------------------------------------------

MAX_BOND_CHARS = 300  # §2: a bond entry's text
MAX_BOND_ENTRIES = 100  # §2: a persona's live bond entries; past it the oldest from a conversation retires
BOND_FAMILIAR_DAYS = 30  # §4 (B3): days together that make it familiar
BOND_CLOSE_DAYS = 90  # §4 (B3): days together that, with a promise or a first, make it close
BOND_MILESTONES = (100, 200, 300)  # §1: the days together marked (then each year from the first day)


class BondStage(Enum):
    """§4: how close the two are — moved by what happens, never down by itself."""

    NEW = "new"
    FAMILIAR = "familiar"
    CLOSE = "close"


BOND_STAGES = (BondStage.NEW, BondStage.FAMILIAR, BondStage.CLOSE)  # in order


@dataclass(frozen=True)
class BondStanding(DeepFrozen):
    """§2, §4: a persona's stage with the user. ``since``: when it moved there (None: never
    moved); ``reason``: why — a bond entry (``b<id>``), days together (``30d``, ``90d``) or
    the user's choice (``user``). ``revision`` 0: never stored."""

    persona_id: int
    stage: BondStage = BondStage.NEW
    since: datetime | None = None
    reason: str | None = None
    revision: int = 0


@dataclass(frozen=True)
class BondChange(DeepFrozen):
    """One ``bond`` decision of a memory pass (§3), checked by the memory module:
    ADD carries the tag, the text and (a date) the month and day; UPDATE the
    entry, the revision it was read at and its new tag, text and month and day;
    RETIRE the entry and its revision. ``sequences``: the messages it rests on,
    at least one of them the user's (B2)."""

    op: LifeChangeOp
    tag: BondTag | None = None
    text: str | None = None
    month_day: str | None = None
    entry_id: int | None = None
    expected_revision: int | None = None
    sequences: tuple[int, ...] = ()


@dataclass(frozen=True)
class Anniversary(DeepFrozen):
    """§1, §6: a day to mark today — days together (``days``), years together
    (``years``) or a ``date`` entry (``entry_id``, its text the ``name``)."""

    name: str
    days: int | None = None
    years: int | None = None
    entry_id: int | None = None

    @property
    def key(self) -> str:
        """Which of the day's anniversaries (a wake key's end)."""
        if self.years is not None:
            return f"y{self.years}"
        return f"d{self.days}" if self.days is not None else f"b{self.entry_id}"


@dataclass(frozen=True)
class BondSnapshot(DeepFrozen):
    """§5: the bond with a persona as a turn sees it."""

    standing: BondStanding
    met_on: str | None  # YYYY-MM-DD: the principal's first conversation, in the zone read in (None: none)
    together_days: int  # the first day is day 1 (0: no conversation yet)
    entries: tuple[LifeEntry, ...] = ()  # the live bond entries, in list_bond's order
    today: tuple[Anniversary, ...] = ()


def together_days(met_on: date, today: date) -> int:
    """B4: days together from the first conversation's day, which is day 1 (so day 100 is 99 days on)."""
    return max(0, (today - met_on).days + 1)


def _on_day(today: date, month: int, day: int) -> bool:
    """A yearly day; the 29th of February is kept on the 28th in other years."""
    if month == 2 and day == 29 and not calendar.isleap(today.year):
        return (today.month, today.day) == (2, 28)
    return (today.month, today.day) == (month, day)


def anniversaries_on(today: date, met_on: date | None, entries: Sequence[LifeEntry]) -> tuple[Anniversary, ...]:
    """§1: today's days to mark — 100, 200 or 300 days together, each year from the
    first day, and the days of the ``date`` entries among ``entries``."""
    found: list[Anniversary] = []
    if met_on is not None:
        days = together_days(met_on, today)
        if days in BOND_MILESTONES:
            found.append(Anniversary(f"{days} days together", days=days))
        years = today.year - met_on.year
        if years >= 1 and _on_day(today, met_on.month, met_on.day):
            found.append(Anniversary(f"{years} year{'s' if years > 1 else ''} together", years=years))
    for entry in entries:
        if entry.tag is BondTag.DATE and entry.month_day is not None:
            month, _, day = entry.month_day.partition("-")
            if month.isdigit() and day.isdigit() and _on_day(today, int(month), int(day)):
                found.append(Anniversary(entry.text, entry_id=entry.entry_id))
    return tuple(found)


class BondStore(Protocol):
    """character-bond §2–§4: what is really settled between the user and HIMARI as a
    persona (entries of kind BOND in the life's table, so ids, revisions and
    LifeStore.retire_life are shared), and how close the two are. The principal
    comes from the trusted entry point, never from model output. Errors as
    LifeStore's, with TOO_LARGE past MAX_BOND_CHARS, INVALID_ARGUMENT also for a
    month and day that is not MM-DD (a ``date`` needs one, other tags have none),
    LIMIT_EXCEEDED at MAX_BOND_ENTRIES live entries all the user's."""

    async def list_bond(self, principal: PrincipalId, persona_id: int) -> tuple[LifeEntry, ...]:
        """The persona's live bond entries: names, then promises, then the rest; newest first within each."""
        ...

    async def add_bond(self, principal: PrincipalId, persona_id: int, tag: BondTag, text: str,
                       month_day: str | None = None) -> LifeEntry:
        """The user's entry (origin USER, revision 1). At MAX_BOND_ENTRIES the oldest
        not the user's retires first."""
        ...

    async def edit_bond(self, principal: PrincipalId, entry_id: int, tag: BondTag, text: str,
                        month_day: str | None = None, *, expected_revision: int) -> LifeEntry:
        """The user's rewrite of a bond entry (its tag too): it becomes the user's.
        NOT_FOUND for an entry that is not a live bond entry."""
        ...

    async def apply_bond_changes(self, principal: PrincipalId, persona_id: int, changes: Sequence[BondChange], *,
                                 conversation: ConversationId, source: str, now: datetime) -> tuple[str, ...]:
        """§3: a memory pass's ``bond`` decisions, in one transaction, each on its own:
        ``applied``, or why not (``unknown``: not a live bond entry of this persona;
        ``user_owned``; ``conflict``; ``duplicate``: the same tag and text are live;
        ``full``; ``invalid``)."""
        ...

    async def bond_stage(self, principal: PrincipalId, persona_id: int) -> BondStanding:
        """The persona's stage as stored, not moved (roadmap stage 5 reads it). NEW
        at revision 0 when none is stored."""
        ...

    async def read_bond(self, principal: PrincipalId, persona_id: int, *, now: datetime,
                        zone: str) -> BondSnapshot:
        """§4, §5: the bond as a turn starting at ``now`` sees it, its dates in ``zone``:
        the stage first moved up as far as what happened since it last moved
        allows (never down), then the days together, the entries and today's
        anniversaries."""
        ...

    async def set_bond_stage(self, principal: PrincipalId, persona_id: int, stage: BondStage, *,
                             expected_revision: int) -> BondStanding:
        """§4: the user's choice of stage, up or down (reason ``user``), compare-and-set
        on the standing's revision (0: none stored). What moves it by itself
        afterwards is only what happens after this."""
        ...


# --- HIMARI's lasting mood (character-state v0.1 §2) -------------------------------------

MOOD_HOURS = 6  # S1: a mood is told this long after it was set, then fades (it stays stored)
MAX_MOOD_REASON_CHARS = 200  # the story writer's line of why
MOOD_NAMES = frozenset(MOODS) - {"neutral"}  # what a lasting mood may be


class MoodSource(Enum):
    REPLY = "reply"  # the last mood other than neutral of one of HIMARI's final answers
    WRITER = "writer"  # the story writer's mood at the end of the day it wrote, with why


@dataclass(frozen=True)
class HimariMood(DeepFrozen):
    """§2: a persona's mood now — a name in MOOD_NAMES, when it was set, by what,
    and (the writer's only) why."""

    persona_id: int
    mood: str
    set_at: datetime
    source: MoodSource
    reason: str | None = None


def live_mood(mood: HimariMood | None, now: datetime) -> HimariMood | None:
    """S1: the mood while it is told (set less than MOOD_HOURS before ``now``), else None."""
    if mood is None or not timedelta(0) <= now - mood.set_at < timedelta(hours=MOOD_HOURS):
        return None
    return mood


class MoodStore(Protocol):
    """§2: one mood per principal and persona (NO_PERSONA: no persona chosen). The
    principal comes from the trusted entry point, never from model output: no Tool
    reaches this port."""

    async def read_mood(self, principal: PrincipalId, persona_id: int) -> HimariMood | None:
        """The mood last set, faded or not (``live_mood`` says whether it is told); None: never set."""
        ...

    async def set_mood(self, principal: PrincipalId, persona_id: int, mood: str, *, source: MoodSource,
                       at: datetime, reason: str | None = None) -> HimariMood:
        """Sets it unless the stored one was set after ``at`` (then that one stays and is
        returned). INVALID_ARGUMENT for a name not in MOOD_NAMES, a reason on a reply's
        mood, or one past MAX_MOOD_REASON_CHARS or with control characters."""
        ...


# --- HIMARI's day: sleep (character-day v0.1 §5, §18.2) ----------------------------------------

LAST_WORDS_CHARS = 1_500  # §3.3: the closed session's last words handed to the next one, newest kept


class SleepState(Enum):
    """§5.1: HIMARI's sleep as the server keeps it (per principal, the persona chosen now)."""

    AWAKE = "awake"
    DROWSY = "drowsy"  # from DROWSY_MINUTES before the sleep slot's start until it falls asleep
    ASLEEP = "asleep"  # deep for DEEP_HOURS after it fell asleep, light after
    HALF_ASLEEP = "half_asleep"  # only the turn of a mumble (or a reminder heard asleep, §9): the sleep goes on


class FellBy(Enum):
    """§5.1: how HIMARI fell asleep."""

    SCHEDULE = "schedule"  # its sleep slot came (or it slept again after being woken)
    TOOL = "tool"  # it went to sleep early (go_to_sleep)
    MISSED = "missed"  # it fell asleep waiting for a promise the user missed (§5.4)


class WokeBy(Enum):
    """§5.1: how HIMARI woke."""

    SCHEDULE = "schedule"  # its sleep slot ended
    USER = "user"  # the user woke it (§5.5)


@dataclass(frozen=True)
class SleepRecord(DeepFrozen):
    """§5.1: one sleep — when and how it began and ended, the messages (and mumbles) it got meanwhile, the
    wake count now (``count`` as of ``count_at``), whether the day was closed in it (§6) and the sleep slot it
    sleeps in (its night: waking, the early wake and the handoff read it, never the night of ``fell_at``)."""

    sleep_id: int
    persona_id: int
    fell_at: datetime
    how_fell: FellBy
    woke_at: datetime | None = None
    how_woke: WokeBy | None = None
    messages: int = 0
    mumbles: int = 0
    count: int = 0
    count_at: datetime | None = None
    closed_day: bool = False
    first_message_at: datetime | None = None  # §5.6: the messages received while asleep, from …
    last_message_at: datetime | None = None  # … to
    slot_id: int | None = None  # the sleep slot's entry id; None: it fell asleep with no slot
    # character-diary §5 (review 3, 1): the day session the day's closing in this sleep closed (None: none, or a
    # closing from before v30) — the amend's cutoff is this sleep's fall, whenever the closing was looked at
    closed_session: ConversationId | None = None

    @property
    def asleep(self) -> bool:
        return self.woke_at is None


@dataclass(frozen=True)
class DayLine(DeepFrozen):
    """§3.3: one of the closed session's last words: when (HH:MM), whose (``user``, ``himari``) and the text."""

    at: str
    who: str
    text: str


@dataclass(frozen=True)
class DayBefore(DeepFrozen):
    """§3.3: what a new day session is handed of the day before — its last words (newest kept within
    LAST_WORDS_CHARS, oldest first, no thoughts), the promises kept and missed, how HIMARI fell asleep (and why
    its sleep slot was moved later, when it was), how often the user woke it and when it woke."""

    day: str  # the closed session's date
    last_words: tuple[DayLine, ...] = ()
    kept: tuple[str, ...] = ()  # "HH:MM text" of the ``together`` slots the user came to
    missed: tuple[str, ...] = ()  # and of those missed (§5.4)
    fell_at: datetime | None = None
    how_fell: FellBy | None = None
    delayed: str | None = None  # the sleep slot moved later: HIMARI's reason
    woken: int = 0  # times the user woke it that night (read when told)
    woke_at: datetime | None = None  # when it woke in the morning (read when told)
    sleep_id: int | None = None  # the sleep the day was closed in
    conversation_id: ConversationId | None = None  # the closed session: the next is any other (never by date)


@dataclass(frozen=True)
class DayState(DeepFrozen):
    """§18.2: the keeper's small state per principal and persona: go_to_sleep asked (applied when the turn's
    job ends) for the sleep slot it moved (``sleep_requested_slot``: only that slot is slept in for it), when the
    user last woke it (it sleeps again after DOZE_MINUTES of quiet in its slot), the last closing (§6) and what the
    next day session is handed (§3.3)."""

    persona_id: int
    sleep_requested_at: datetime | None = None
    woken_at: datetime | None = None
    last_closing_at: datetime | None = None
    before: DayBefore | None = None
    sleep_requested_slot: int | None = None


@dataclass(frozen=True)
class SleepView(DeepFrozen):
    """§5.1, §8: HIMARI's sleep as a turn (and the Web) sees it now: its state, whether the sleep is deep, the
    minutes to its slot while drowsy, tonight's sleep slot (or the one under way), the current or last sleep,
    and when the user just woke it (in this slot)."""

    state: SleepState
    deep: bool = False
    minutes_to_sleep: int | None = None
    tonight: LifeEntry | None = None
    last: SleepRecord | None = None
    woken_at: datetime | None = None


class SleepStore(Protocol):
    """§5.1, §18.2: HIMARI's sleeps and the day keeper's state, per principal and persona. The principal comes
    from the trusted entry point (the keeper), never from model output."""

    async def open_sleep(self, principal: PrincipalId, persona_id: int, *, at: datetime, how: FellBy,
                         slot_id: int | None = None) -> SleepRecord:
        """It falls asleep, in the sleep slot ``slot_id`` (its night). INVALID_STATE_TRANSITION while a sleep of the
        persona is open."""
        ...

    async def current_sleep(self, principal: PrincipalId, persona_id: int) -> SleepRecord | None:
        """The persona's latest sleep, open or ended; None: it never slept."""
        ...

    async def end_sleep(self, principal: PrincipalId, sleep_id: int, *, at: datetime, how: WokeBy) -> SleepRecord:
        """It wakes. NOT_FOUND: no such sleep; INVALID_STATE_TRANSITION: it ended already; INVALID_ARGUMENT: ``at``
        before it fell asleep (a wake never precedes its fall)."""
        ...

    async def count_wake(self, principal: PrincipalId, sleep_id: int, *, count: int, at: datetime,
                         answer: AsleepAnswer, message: bool) -> SleepRecord:
        """§5.5: a message (or a poke, ``message`` False) to the open sleep — the wake count set, a message and
        a mumble counted. NOT_FOUND; INVALID_STATE_TRANSITION: it ended already."""
        ...

    async def recent_sleeps(self, principal: PrincipalId, persona_id: int, *, limit: int) -> tuple[SleepRecord, ...]:
        """The persona's latest sleeps, newest first."""
        ...

    async def note_closing(self, principal: PrincipalId, persona_id: int, sleep_id: int, *, at: datetime,
                           before: DayBefore | None, conversation_id: ConversationId | None = None) -> bool:
        """§6: the day was closed in this sleep, all in one transaction — the open day session ``conversation_id``
        (when given) closed at ``at``, the sleep says so (and names the session it closed: character-diary §5), the
        keeper's last closing is ``at`` and the next session is handed ``before``. Whether the session was closed now. NOT_FOUND: no such sleep;
        INVALID_STATE_TRANSITION: the day was closed in it already (nothing written)."""
        ...

    async def read_day_state(self, principal: PrincipalId, persona_id: int) -> DayState:
        """The keeper's state (all None when none was written)."""
        ...

    async def write_day_state(self, principal: PrincipalId, state: DayState) -> None:
        """Replaces the keeper's state of ``state.persona_id``."""
        ...


# --- Devices (V3-P83) -----------------------------------------------------------

MAX_DEVICE_NAME_CHARS = 40
MAX_DEVICES = 32  # a principal's devices that are not revoked
PAIRING_FAILURE_LIMIT = 5  # failed pairings that end a live code (§4.1)
RESERVED_DEVICE_NAMES = frozenset({"web", "cli"})  # the other origins' names (§4.2)


@dataclass(frozen=True)
class Device(DeepFrozen):
    """A device of a principal that is not revoked. Its token and pairing code
    are kept as hashes only and never leave Data."""

    device_id: str  # "d<n>", never reused for another device of the principal
    name: str
    created_at: datetime
    pairing_expires_at: datetime | None  # the code's end; None once it ended
    paired_at: datetime | None  # None: still waiting for its code
    last_seen_at: datetime | None


class PairingResult(Enum):
    PAIRED = "paired"  # the code's device now has this token
    RESENT = "resent"  # the same code and token again: the answer was lost
    USED = "used"  # the code's device was paired with another token
    EXPIRED = "expired"  # the code is known but past its end
    INVALID = "invalid"  # no live device has this code
    TOKEN_IN_USE = "token_in_use"  # another paired device has this token: the app must make a new one


@dataclass(frozen=True)
class DevicePairing(DeepFrozen):
    result: PairingResult
    principal: PrincipalId | None = None  # PAIRED and RESENT
    device: Device | None = None  # PAIRED and RESENT


class DeviceStore(Protocol):
    """V3-P83 §4: a principal's devices. The principal comes from the trusted
    entry point (a signed-in user) or from the device's own token, never from
    model output. Revoked devices are invisible to every method; their ids
    are not reused. Errors: NOT_FOUND, INVALID_ARGUMENT (a name that is
    empty, too long, reserved or has control characters),
    DUPLICATE_IDENTITY (a name another device has, case ignored),
    LIMIT_EXCEEDED (MAX_DEVICES)."""

    async def add_device(self, principal: PrincipalId, name: str, pairing_hash: bytes,
                         expires_at: datetime) -> Device:
        """A new unpaired device waiting for the code whose hash is given.
        The principal's other unpaired devices are deleted first: one live
        code at a time."""
        ...

    async def pair_device(self, pairing_hash: bytes, token_hash: bytes, now: datetime) -> DevicePairing:
        """§4.1, one transaction: an unpaired device with this code takes the
        token (PAIRED); a paired one whose code has not ended and that has
        this very token answers again (RESENT); another token: USED; a token
        another unrevoked device has: TOKEN_IN_USE (nothing changes). Never a
        revoked device."""
        ...

    async def record_pairing_failure(self, now: datetime) -> None:
        """A pairing with an unknown code: counted against every live
        unpaired code; one reaching PAIRING_FAILURE_LIMIT is deleted."""
        ...

    async def device_for_token(self, token_hash: bytes) -> tuple[PrincipalId, Device] | None:
        """The paired, unrevoked device with this token."""
        ...

    async def list_devices(self, principal: PrincipalId) -> tuple[Device, ...]:
        """In creation order."""
        ...

    async def rename_device(self, principal: PrincipalId, device_id: str, name: str) -> Device: ...

    async def revoke_device(self, principal: PrincipalId, device_id: str) -> None:
        """Clears its token and code: neither works again (§4.1)."""
        ...

    async def touch_device(self, principal: PrincipalId, device_id: str, at: datetime) -> None:
        """Records when it was last seen; nothing for an unknown device."""
        ...

    async def record_input(self, principal: PrincipalId, device_id: str, at: datetime) -> None:
        """V3-P87 §5.2: the device's last input (``devices.last_input_at``), never
        moved back; nothing for an unknown or revoked device."""
        ...

    async def read_inputs(self, principal: PrincipalId) -> Mapping[str, datetime]:
        """Each unrevoked device's last input, as recorded."""
        ...

    async def read_quiet(self, principal: PrincipalId) -> datetime | None:
        """V3-P88 §6.2: until when the user asked HIMARI to stay quiet, as
        written (a past moment may come back: the caller compares)."""
        ...

    async def write_quiet(self, principal: PrincipalId, until: datetime | None) -> None:
        """Sets it, or clears it with None."""
        ...


# --- Conversation search (V3-P72) -------------------------------------------

SEARCH_KINDS = frozenset({EventKind.USER_MESSAGE, EventKind.ASSISTANT_FINAL})  # what is searched (§3.3)
MAX_SEARCH_WORDS = 8
MAX_SEARCH_WORD_CHARS = 200


@dataclass(frozen=True)
class EventText(DeepFrozen):
    """A user message or final answer as searched and read (V3-P72 §4.4):
    its text blocks joined by newlines."""

    conversation_id: ConversationId
    sequence: int
    kind: EventKind
    created_at: datetime
    text: str
    matched: int = 0  # how many of the search's words it holds (search only)


@dataclass(frozen=True)
class EventSearch(DeepFrozen):
    hits: tuple[EventText, ...]  # most words first, then newest; at most per_conversation of each
    total: int  # every match, of which ``hits`` are the first
    conversations: tuple[ConversationSummary, ...]  # those the hits are in


@dataclass(frozen=True)
class EventPage(DeepFrozen):
    conversation: ConversationSummary
    items: tuple[EventText, ...]  # in order, from the sequence asked for
    more: bool  # searchable Events follow the last one


class ConversationSearch(Protocol):
    """V3-P72: the text of a principal's past Conversations — their user
    messages and final answers (SEARCH_KINDS) — outside the trash. Of the
    ``current`` Conversation only Events up to ``current_through`` count (the
    part a compaction summary covers; None: none of it). A word matches when
    the folded text (NFKC, case-folded) contains it."""

    async def search_events(self, principal: PrincipalId, words: Sequence[str], *, current: ConversationId,
                            current_through: int | None, conversation: ConversationId | None = None,
                            per_conversation: int, limit: int) -> EventSearch:
        """Events holding any of ``words`` (folded here; repeats count once;
        1..MAX_SEARCH_WORDS of 1..MAX_SEARCH_WORD_CHARS characters, else
        INVALID_ARGUMENT). ``conversation``: only that one (NOT_AUTHORIZED when
        it is not the principal's or is in the trash, as for any read)."""
        ...

    async def read_event_texts(self, principal: PrincipalId, conversation: ConversationId, *, from_sequence: int,
                               current: ConversationId, current_through: int | None, limit: int) -> EventPage:
        """Up to ``limit`` searchable Events from ``from_sequence`` on.
        NOT_AUTHORIZED: not the principal's, or in the trash. INVALID_ARGUMENT:
        the part of the current Conversation past ``current_through``."""
        ...


# --- Conversation catalog (DATA-12, T0) -------------------------------------

MAX_TITLE_CHARS = 200
MAX_LIST_LIMIT = 200
PREVIEW_CHARS = 120


@dataclass(frozen=True)
class ConversationSummary(DeepFrozen):
    """One row of a principal's Conversation list. ``preview`` is the start
    of the first user message (display only, never a model input)."""

    conversation_id: ConversationId
    title: str | None
    created_at: datetime
    last_activity_at: datetime  # the latest Event's time, else created_at
    last_sequence: int
    preview: str
    trashed_at: datetime | None = None  # V3-P65: when it was moved to the trash
    # character-day §3: a day session's date (YYYY-MM-DD, the day it was opened) and when it was closed (then it
    # is only read); None, None: an ordinary conversation
    day_session: str | None = None
    closed_at: datetime | None = None


@dataclass(frozen=True)
class TrashedConversation(DeepFrozen):
    """V3-P65: what a permanent delete must clean up outside Data."""

    summary: ConversationSummary
    environments: tuple[EnvironmentKind, ...]  # whose places (its folder, §5) go with it


# --- Sub-ports ---------------------------------------------------------------


class ConversationLog(Protocol):
    """DATA-01/02/03/05/08: events, Run ledger, Tool ledger, snapshot."""

    async def append_event(
        self, scope: TrustedScope, message: UserMessage, *, idempotency_key: str | None = None
    ) -> ConversationEvent:
        """T1: user input; one with ``answers_call`` (older history only) is
        INVALID_STATE_TRANSITION.
        Same key + same message → the original Event; same key + a different
        message → DUPLICATE_IDENTITY. character-day §3.2: DAY_CLOSED in a closed
        day session (checked in the write's own transaction)."""
        ...

    async def read_events(
        self, scope: TrustedScope, *, after_sequence: int = 0, limit: int | None = None
    ) -> Sequence[ConversationEvent]: ...

    async def prepare_run(self, scope: TrustedScope, preparation: RunPreparation) -> RunId:
        """T2: persist the request before any send. Raises
        RAW_PERSISTENCE_UNAVAILABLE when it cannot be stored — do not send;
        DAY_CLOSED in a closed day session (character-day §3.2: no new Run)."""
        ...

    async def mark_dispatch(self, scope: TrustedScope, run_id: RunId) -> None:
        """T3: ``PREPARED → DISPATCH_CLAIMED`` immediately before the one send.
        A Run already past PREPARED raises INVALID_STATE_TRANSITION."""
        ...

    async def record_raw_outcome(
        self, scope: TrustedScope, run_id: RunId, outcome: RawRunOutcome
    ) -> None:
        """T4: store the received body (``RECEIVED``) or failure class."""
        ...

    async def project_model_output(
        self,
        scope: TrustedScope,
        run_id: RunId,
        output: AssistantOutput,
        *,
        continuation: JsonValue = None,
    ) -> ProjectionResult:
        """T5: assistant Event + every pending ToolExecution (+ the turn's
        opaque reasoning continuation, stamped with this Run's provider_key
        and model_id), atomically. Only a TURN Run can be projected."""
        ...

    async def record_projection_failure(
        self, scope: TrustedScope, run_id: RunId, error_code: str
    ) -> None: ...

    async def read_run(self, scope: TrustedScope, run_id: RunId) -> RunRecord: ...

    async def read_raw_response(self, scope: TrustedScope, run_id: RunId) -> RawReceived:
        """The stored response bytes of a Run (a model response, or the error
        response of a ``FAILED_NO_RESPONSE`` Run — the RunStatus tells which).
        Used to re-normalize ``RECEIVED`` Runs after a crash. NOT_FOUND if the
        Provider returned nothing."""
        ...

    async def read_tool_executions(
        self, scope: TrustedScope, run_id: RunId
    ) -> Sequence[ToolExecutionRecord]:
        """Ordered by original ordinal, not completion time."""
        ...

    async def claim_tool_execution(
        self,
        scope: TrustedScope,
        call: CallRef,
        *,
        unknown_outcome: UnknownOutcomePolicy,
        record_event: bool,
    ) -> None:
        """T6: ``PENDING → EXECUTION_CLAIMED`` before an effectful handler runs,
        persisting the policies that apply to this effect: its UNKNOWN policy
        (V3-047) and whether its result is recorded as a Conversation Event."""
        ...

    async def commit_tool_outcome(
        self, scope: TrustedScope, call: CallRef, completion: ToolCompletion
    ) -> ConversationEvent | None:
        """T7/T8 (DATA-08): terminal state + model result + raw result +
        configured ``tool_result`` Event in one transaction.
        Called by the Tool Dispatcher only. Returns the Event if recorded."""
        ...

    async def commit_unknown_outcome(
        self, scope: TrustedScope, call: CallRef, model_result: ModelToolResult
    ) -> ConversationEvent | None:
        """V3-047: ``EXECUTION_CLAIMED`` or ``UNKNOWN`` → ``UNKNOWN`` with a
        model-facing "outcome unknown" result (+ ``tool_result`` Event when the
        claim-time ``record_event`` says so), atomically. The status stays UNKNOWN: the effect is still not known.
        Only for records whose persisted policy is REPORT_TO_MODEL; otherwise
        INVALID_STATE_TRANSITION (a user must resolve it).
        ALREADY_COMMITTED if the call already has a model result."""
        ...

    async def recover_interrupted(self, scope: TrustedScope) -> RecoveryReport:
        """T-R: DISPATCH_CLAIMED without response → OUTCOME_UNKNOWN,
        EXECUTION_CLAIMED without result → UNKNOWN, unclaimed PREPARED →
        ABANDONED_NOT_SENT; RECEIVED Runs are reported for projection.
        Never re-executes anything."""
        ...

    async def get_latest_snapshot(self, scope: TrustedScope) -> PromptSnapshot | None: ...

    async def replace_latest_snapshot(
        self,
        scope: TrustedScope,
        *,
        expected_revision: int | None,
        schema_version: int,
        body: JsonValue,
        covers_through_sequence: int,
        basis: PromptBasis,
    ) -> int:
        """T9: latest-only CAS replace; returns the new revision."""
        ...

    async def record_compaction_step(self, scope: TrustedScope, run_id: RunId) -> None:
        """A RECEIVED COMPACTION Run whose summary was handed on to the next
        call of the same chain, not committed: ``PROJECTED``, snapshot
        unchanged (its summary stays in the stored response and in the next
        Run's request). INVALID_STATE_TRANSITION for any other Run."""
        ...

    async def commit_compaction(
        self,
        scope: TrustedScope,
        run_id: RunId,
        *,
        expected_revision: int | None,
        schema_version: int,
        body: JsonValue,
        covers_through_sequence: int,
        basis: PromptBasis,
    ) -> int:
        """T9c: replace the latest snapshot with a COMPACTION Run's summary and
        mark that Run PROJECTED, atomically. The Run must be a RECEIVED
        COMPACTION Run (INVALID_STATE_TRANSITION otherwise); the snapshot
        revision must still be ``expected_revision`` (VERSION_CONFLICT); the
        new coverage may not move backwards or past the last Event
        (STALE_BASIS). Events and raw Run records are not touched."""
        ...


class EnvironmentState(Protocol):
    """DATA-07: which Environments a Conversation has (set at T0)."""

    async def has_environment(self, scope: TrustedScope, environment_kind: EnvironmentKind) -> bool:
        """Whether this Conversation has the kind. Every Environment Tool
        call is gated on this; NOT_AUTHORIZED for another's Conversation."""
        ...


class ExternalSourceSettings(Protocol):
    """V3-P53: per-Conversation on/off choices for External Tool sources.

    Data keeps exactly what it is told and knows no defaults: whoever reads
    the choices applies them over the operator's configuration (a source
    with no choice follows its default). Changing a choice does not touch
    ``state_revision``; the Tool catalog it changes is part of the request's
    ``context_key``."""

    async def read_external_settings(self, scope: TrustedScope) -> Sequence[ExternalSourceSetting]:
        """This Conversation's choices, ordered by source."""
        ...

    async def set_external_setting(self, scope: TrustedScope, source: str, enabled: bool | None) -> None:
        """Records a choice; ``None`` removes it (back to the default).
        INVALID_ARGUMENT for a source that is not one Tool id segment, or
        past MAX_EXTERNAL_SETTINGS choices."""
        ...


class ConversationModelSettings(Protocol):
    """V3-P64: a Conversation's model choice.

    Like ``ExternalSourceSettings``, Data keeps exactly what it is told and
    knows no defaults or offered models; Control resolves the choice when a
    turn starts. Changing it does not touch ``state_revision``: the model is
    not part of the Prompt (V3-049)."""

    async def read_model_setting(self, scope: TrustedScope) -> ModelSetting | None:
        """This Conversation's choice; None: none made (the default model)."""
        ...

    async def set_model_setting(self, scope: TrustedScope, setting: ModelSetting | None) -> None:
        """Records the choice, replacing any earlier one; ``None`` removes it.
        INVALID_ARGUMENT for a model id of 1..MAX_MODEL_ID_CHARS visible ASCII
        characters, or an effort of 1..MAX_EFFORT_CHARS, it is not."""
        ...


class ConversationCatalog(Protocol):
    """DATA-12: a principal's Conversations (T0 create, list, rename, and the
    trash of V3-P65).

    The principal comes from the trusted entry point, never from model
    output. Listing only ever returns the caller's own Conversations.

    Trash (V3-P65): ACTIVE → TRASHED → PURGING → gone; TRASHED → ACTIVE is
    restore. Every other port treats a trashed Conversation as a missing
    one (NOT_AUTHORIZED). The trash methods check only the owner; a wrong
    state is INVALID_STATE_TRANSITION."""

    async def create_conversation(
        self,
        principal: PrincipalId,
        *,
        title: str | None = None,
        environments: Sequence[EnvironmentKind] = (),
        model_setting: ModelSetting | None = None,
    ) -> ConversationId:
        """T0: a new Conversation owned by ``principal`` with its Environment
        kinds (each once), atomically. The id is generated by Data.
        A title longer than MAX_TITLE_CHARS raises INVALID_ARGUMENT; a blank
        title is stored as None."""
        ...

    async def open_day_session(self, principal: PrincipalId, day: str, title: str, *,
                               environments: Sequence[EnvironmentKind] = (),
                               model_setting: ModelSetting | None = None) -> ConversationId:
        """character-day §3.2: T0 of the principal's day session for ``day`` (YYYY-MM-DD). One is open at a
        time: DUPLICATE_IDENTITY while another is (closed or trashed ones do not count)."""
        ...

    async def current_day_session(self, principal: PrincipalId) -> ConversationSummary | None:
        """§3.1: the principal's open day session; None: none is open (a trashed one is closed)."""
        ...

    async def close_day_session(self, principal: PrincipalId, conversation_id: ConversationId,
                                now: datetime) -> bool:
        """§3.2: the open day session closed (only read from now on); False when it was not open."""
        ...

    async def most_recent_user_conversation(self, principal: PrincipalId) -> ConversationSummary | None:
        """V3-P83 §8.2: outside the trash, the Conversation whose latest user
        message (answers included) is the latest; None without any."""
        ...

    async def list_user_conversations(
        self, principal: PrincipalId, *, limit: int = 50, offset: int = 0
    ) -> Sequence[ConversationSummary]:
        """Outside the trash, Conversations with user messages (answers included),
        ordered by latest user message, newest id first on ties. Same paging
        bounds as ``list_conversations``; assistant activity does not reorder them."""
        ...

    async def last_user_message_at(self, principal: PrincipalId) -> datetime | None:
        """V3-P87 §6.1: when the principal's latest user message (answers included)
        came, in any Conversation outside the trash; None without any."""
        ...

    async def list_conversations(
        self, principal: PrincipalId, *, limit: int = 50, offset: int = 0, trashed: bool = False
    ) -> Sequence[ConversationSummary]:
        """Active ones: most recent activity first (ties: newest id first).
        ``trashed``: the trash instead (PURGING included), most recently
        trashed first. ``limit`` is 1..MAX_LIST_LIMIT, else INVALID_ARGUMENT."""
        ...

    async def read_conversation(self, scope: TrustedScope) -> ConversationSummary:
        """NOT_AUTHORIZED for a foreign or missing Conversation."""
        ...

    async def rename_conversation(self, scope: TrustedScope, title: str | None) -> None: ...

    async def trash_conversation(self, scope: TrustedScope) -> datetime:
        """ACTIVE → TRASHED; returns the time recorded."""
        ...

    async def restore_conversation(self, scope: TrustedScope) -> None:
        """TRASHED → ACTIVE. Refused once a permanent delete has begun."""
        ...

    async def begin_purge(self, scope: TrustedScope) -> TrashedConversation:
        """TRASHED → PURGING (or already PURGING: unchanged); what is left to
        clean up. Recorded before anything outside Data is deleted, so a
        Conversation whose files may be gone is never restored."""
        ...

    async def purge_conversation(self, scope: TrustedScope) -> None:
        """PURGING → gone: every row of the Conversation, and each Tool view
        no other call refers to, in one transaction."""
        ...

    async def used_tool_ids(self, scope: TrustedScope) -> frozenset[ToolId]:
        """Every Tool this (active) Conversation called."""
        ...


class ConversationRetention(Protocol):
    """V3-P65: the one read across principals, for the composition root's
    clean-up only (never given to Web, Control or Tools)."""

    async def due_purges(self, cutoff: datetime) -> Sequence[TrustedScope]:
        """Conversations trashed before ``cutoff``, and every PURGING one,
        oldest first."""
        ...


class StateQuery(Protocol):
    """DATA-06: coherent read for CurrentStateProjection."""

    async def read_current_state_basis(self, scope: TrustedScope) -> PersistedStateBasis: ...


__all__ = [
    "AssistantFinal",
    "AssistantOutput",
    "AssistantToolCalls",
    "CompletedGroupRef",
    "ConversationCatalog",
    "ConversationEvent",
    "ConversationLog",
    "ConversationModelSettings",
    "ConversationRetention",
    "ConversationSummary",
    "DataError",
    "DataErrorCode",
    "EnvironmentState",
    "EventKind",
    "EventPayload",
    "EventRef",
    "ExternalSourceSetting",
    "ExternalSourceSettings",
    "MAX_EFFORT_CHARS",
    "MAX_EXTERNAL_SETTINGS",
    "MAX_LIST_LIMIT",
    "MAX_MODEL_ID_CHARS",
    "MAX_TITLE_CHARS",
    "ModelSetting",
    "PREVIEW_CHARS",
    "PersistedStateBasis",
    "ProjectionResult",
    "PromptBasis",
    "PromptSnapshot",
    "RawNoResponse",
    "RawReceived",
    "RawRunOutcome",
    "RawUncertain",
    "RecoveryReport",
    "RunPreparation",
    "RunRecord",
    "RunStatus",
    "StateQuery",
    "ToolCompletion",
    "ToolExecutionRecord",
    "ToolExecutionStatus",
    "ToolResultRecord",
    "TrashedConversation",
    "TriggerRef",
    "UserMessage",
]
