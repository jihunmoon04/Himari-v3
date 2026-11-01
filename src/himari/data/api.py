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

import hashlib
import json
import mimetypes
import re
import unicodedata
from datetime import UTC, date, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import BinaryIO, Protocol

from himari.common import (
    DeepFrozen,
    ArtifactId,
    BindingId,
    CallId,
    CallRef,
    ContentBlock,
    ConversationId,
    EnvironmentInstanceId,
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
    WorkspaceId,
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


# DataError.reason of an upload a message names once more (V3-P84 §4.2: a screen is attached once).
ALREADY_SENT = "already sent"


class DataError(Exception):
    def __init__(self, code: DataErrorCode, message: str = "", *, file_id: FileId | None = None,
                 reason: str | None = None) -> None:
        super().__init__(f"{code.value}: {message}" if message else code.value)
        self.code = code
        self.file_id = file_id  # ATTACHMENT_REFUSED: the attachment refused
        self.reason = reason  # ATTACHMENT_REFUSED: why, e.g. ALREADY_SENT


# --- Conversation Events (Data v0.2 §4) ---------------------------------------


class EventKind(Enum):
    USER_MESSAGE = "user_message"
    ASSISTANT_FINAL = "assistant_final"
    ASSISTANT_TOOL_CALLS = "assistant_tool_calls"
    TOOL_RESULT = "tool_result"


class OriginKind(Enum):
    """Where a user message was sent from (V3-P83 §4.2): how its caller signed in."""

    DEVICE = "device"  # a paired device's token
    WEB = "web"  # the Web session cookie
    CLI = "cli"  # the operator's bearer token


@dataclass(frozen=True)
class MessageOrigin(DeepFrozen):
    kind: OriginKind
    device_id: str | None = None  # DEVICE only


@dataclass(frozen=True)
class UserMessage(DeepFrozen):
    blocks: tuple[ContentBlock, ...]
    answers_call: CallRef | None = None  # set only via answer_pending_call (T7a)
    origin: MessageOrigin | None = None  # V3-P83: set by the entry point; None for older messages


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


type EventPayload = UserMessage | AssistantFinal | AssistantToolCalls | ToolResultRecord
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
    artifact_revisions: Mapping[ArtifactId, int] = field(default_factory=dict)
    # Opaque digest of what the input was composed under besides history and
    # state (model, instructions, Tool catalog); set by Prompt (V3-P48).
    context_key: str | None = None


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
    workspace_binding_id: BindingId | None = None
    # Persisted at claim time. None on an uncertain record means the policy is
    # not known and must be treated as REQUIRE_USER (never silently relaxed).
    unknown_outcome: UnknownOutcomePolicy | None = None
    # The Tool's conversation_record policy, persisted at claim time so a later
    # "outcome unknown" result is recorded the same way whatever the current
    # build registers. None (not claimed / legacy) → record the Event.
    record_event: bool | None = None
    # Set when the call's view (MCP Apps, V3-P58) was stored with its result.
    ui_digest: str | None = None


@dataclass(frozen=True)
class ProjectionResult(DeepFrozen):
    event: ConversationEvent
    pending: tuple[ToolExecutionRecord, ...]  # empty for AssistantFinal


# --- Artifacts (Data v0.2 §8) ----------------------------------------------


@dataclass(frozen=True)
class Artifact(DeepFrozen):
    artifact_id: ArtifactId
    kind: str  # e.g. "plan", "note"; Data does not interpret content
    revision: int
    content: JsonValue


@dataclass(frozen=True)
class ArtifactMutation(DeepFrozen):
    """Requested Plan/Note change carried by a ToolOutcome (V3-042).

    ``artifact_id=None`` creates a new Artifact; otherwise ``expected_revision``
    is checked (CAS) inside the same transaction as the Tool result.
    """

    kind: str
    content: JsonValue
    artifact_id: ArtifactId | None = None
    expected_revision: int | None = None


# --- Tool views (MCP Apps, V3-P58) -------------------------------------------
# A view is HTML a Tool's server gives for showing a call (an MCP Apps
# ``ui://`` resource). It is stored with the call's result, content-addressed,
# and shown only in a sandbox; it never reaches the model.

MAX_UI_HTML_BYTES = 2 * 1024 * 1024  # UTF-8
MAX_APP_PAYLOAD_BYTES = 1024 * 1024  # the payload's JSON, UTF-8


@dataclass(frozen=True)
class UiCsp(DeepFrozen):
    """The origins a view's server declares it needs (MCP Apps ``ui.csp``).
    A declaration, not a grant: the host allows only what its operator also
    allows."""

    connect_domains: tuple[str, ...] = ()
    resource_domains: tuple[str, ...] = ()
    frame_domains: tuple[str, ...] = ()
    base_uri_domains: tuple[str, ...] = ()


def ui_digest(source: str, uri: str, html: str, csp: UiCsp, prefers_border: bool | None) -> str:
    """SHA-256 of the canonical JSON of a view: its identity in storage."""
    canonical = json.dumps(
        {"source": source, "uri": uri, "html": html, "prefers_border": prefers_border,
         "csp": {"connect_domains": list(csp.connect_domains), "resource_domains": list(csp.resource_domains),
                 "frame_domains": list(csp.frame_domains), "base_uri_domains": list(csp.base_uri_domains)}},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class UiResource(DeepFrozen):
    """A Tool's view as its server declared it (V3-P58). Build it with
    ``UiResource.create`` so that ``digest`` matches the content; Data checks."""

    digest: str
    source: str  # the External source (e.g. MCP server name) whose code it is
    uri: str  # "ui://…"
    html: str
    csp: UiCsp = field(default_factory=UiCsp)
    prefers_border: bool | None = None

    @classmethod
    def create(cls, source: str, uri: str, html: str, csp: UiCsp | None = None,
               prefers_border: bool | None = None) -> UiResource:
        csp = UiCsp() if csp is None else csp
        return cls(ui_digest(source, uri, html, csp, prefers_border), source, uri, html, csp, prefers_border)


@dataclass(frozen=True)
class AppView(DeepFrozen):
    """A call's view and what it shows: the view's code plus the call's
    result as the view receives it (MCP ``CallToolResult``)."""

    resource: UiResource
    payload: JsonValue


@dataclass(frozen=True)
class StoredAppView(DeepFrozen):
    """A view stored with a call's result, read back for display."""

    call: CallRef
    tool_id: ToolId
    arguments: Mapping[str, JsonValue]
    status: ToolExecutionStatus
    view: AppView


# --- Files (V3-P66, docs/file-upload-download-v0.1.md) -------------------------
# An uploaded or presented file keeps its bytes in the file store, outside
# every Workspace: written once, addressed per principal, expired when the
# store is full or after the keep days (§8.5) — bytes only, the record stays.

MAX_FILE_NAME_BYTES = 200  # UTF-8, after cleaning (§5.3)
MAX_RAW_NAME_BYTES = 1000  # a name given longer than this is refused
MAX_MEDIA_TYPE_CHARS = 127
MAX_STAGED_UPLOADS = 50  # per Conversation
MAX_ATTACHMENTS = 10  # per message (T1 and T7a)
STAGED_KEEP = timedelta(hours=24)  # staged records older than this are dropped
STAGED_SEND_CUTOFF = timedelta(hours=23)  # sending refuses older staged uploads (§4.2)
BLOB_GRACE = timedelta(hours=1)  # a blob put this recently never expires or goes (§8.2, §8.5)
PRESENT_WINDOW = timedelta(hours=1)
PRESENT_LIMIT_BYTES = 256 * 1024 * 1024  # presented per Conversation per PRESENT_WINDOW (§7.1)
DEFAULT_FILE_MAX_BYTES = 32 * 1024 * 1024
DEFAULT_STORE_MAX_BYTES = 1024 * 1024 * 1024
DEFAULT_KEEP_DAYS = 30
TEMP_PREFIX = ".himari-"  # the Workspace write Tools' temporary names


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
        BLOB_MISSING if the blob (or the image's copy) is gone or expired."""
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
    artifact_mutations: tuple[ArtifactMutation, ...] = ()
    record_event: bool = True  # the Tool's conversation_record policy (FULL → True)
    # V3-P58: the call's view, stored in the same transaction. Only with
    # SUCCEEDED or FAILED (a call that ran); INVALID_ARGUMENT otherwise.
    app_view: AppView | None = None
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


@dataclass(frozen=True)
class AnswerCommit(DeepFrozen):
    user_event: ConversationEvent
    tool_result_event: ConversationEvent | None


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
class BindingAuthority(DeepFrozen):
    """Current, authorized Binding metadata. Not an OS permission proof;
    the Environment re-validates before any effect."""

    binding_id: BindingId
    environment_instance_id: EnvironmentInstanceId
    environment_kind: EnvironmentKind
    workspace_id: WorkspaceId
    revision: int
    # This Conversation's working directory inside *this* Binding's Workspace
    # (DATA-11). Per Binding, so several Environments with different
    # Workspaces can be active at once (V3-008). Never a host path.
    cwd_relative: str | None = None


@dataclass(frozen=True)
class ConversationWorkspace(DeepFrozen):
    workspace_id: WorkspaceId
    cwd_relative: str  # never an absolute host path


@dataclass(frozen=True)
class EnvironmentActivation(DeepFrozen):
    environment_instance_id: EnvironmentInstanceId
    environment_kind: EnvironmentKind  # P0: at most one active per kind (V3-P19)
    default_binding_id: BindingId | None = None


@dataclass(frozen=True)
class PersistedStateBasis(DeepFrozen):
    """One coherent, permission-filtered read (DATA-06)."""

    conversation_sequence: int
    state_revision: int
    environments: tuple[EnvironmentActivation, ...]
    bindings: tuple[BindingAuthority, ...]
    artifacts: tuple[Artifact, ...]
    conversation_workspace: ConversationWorkspace | None = None


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
    scope is added by its packet."""

    USER = "user"  # every Conversation of the principal
    # V3-P89 §4: who HIMARI is as the user's companion; told only with the companion on
    COMPANION_PERSONA = "companion_persona"


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


@dataclass(frozen=True)
class NoteDates(DeepFrozen):
    """The dates the user gives a note (V3-P85 §10.2): all None clears them."""

    due_at: str | None = None
    due_instant: datetime | None = None
    valid_until: str | None = None
    valid_instant: datetime | None = None


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
                      user_approved_at=moment("user_approved_at"))
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
class InitialActivation(DeepFrozen):
    """An Environment activated in the same T0 transaction that creates the
    Conversation (its default Binding's cwd is the Conversation's own folder)."""

    environment_kind: EnvironmentKind
    workspace_id: WorkspaceId


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


@dataclass(frozen=True)
class TrashedConversation(DeepFrozen):
    """V3-P65: what a permanent delete must clean up outside Data."""

    summary: ConversationSummary
    workspace: ConversationWorkspace | None  # the Conversation's own folder (§5)


# --- Sub-ports ---------------------------------------------------------------


class ConversationLog(Protocol):
    """DATA-01/02/03/05/08: events, Run ledger, Tool ledger, snapshot."""

    async def append_event(
        self, scope: TrustedScope, message: UserMessage, *, idempotency_key: str | None = None
    ) -> ConversationEvent:
        """T1: ordinary user input. While an ask_user call is waiting, Control
        must use ``answer_pending_call`` instead (one path per input).
        Same key + same message → the original Event; same key + a different
        message → DUPLICATE_IDENTITY."""
        ...

    async def read_events(
        self, scope: TrustedScope, *, after_sequence: int = 0, limit: int | None = None
    ) -> Sequence[ConversationEvent]: ...

    async def prepare_run(self, scope: TrustedScope, preparation: RunPreparation) -> RunId:
        """T2: persist the request before any send. Raises
        RAW_PERSISTENCE_UNAVAILABLE when it cannot be stored — do not send."""
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
        Artifact mutations (CAS) + configured ``tool_result`` Event in one
        transaction.
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

    async def read_app_view(self, scope: TrustedScope, call: CallRef) -> StoredAppView | None:
        """V3-P58: the view stored with this call's result, or None if it has
        none (still running, ended without one, or a Tool without a view).
        NOT_FOUND for a call this Conversation does not have."""
        ...

    async def find_waiting_call(self, scope: TrustedScope) -> CallRef | None:
        """Derived wait state (V3-P17): the latest PROJECTED Run's group is a
        single ``ask_user`` call still PENDING."""
        ...

    async def answer_pending_call(
        self, scope: TrustedScope, call: CallRef, answer: UserMessage
    ) -> AnswerCommit:
        """T7a: ``user_message`` (with ``answers_call``) + ToolExecution
        ``PENDING → SUCCEEDED`` + ``tool_result`` Event, atomically.
        Raises ALREADY_COMMITTED if the call is no longer waiting."""
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


class ArtifactStore(Protocol):
    """DATA-04: read side. Writes go through ``commit_tool_outcome``."""

    async def read_artifact(self, scope: TrustedScope, artifact_id: ArtifactId) -> Artifact: ...

    async def list_artifacts(
        self, scope: TrustedScope, *, kind: str | None = None
    ) -> Sequence[Artifact]: ...


class WorkspaceState(Protocol):
    """DATA-07/11: Registry, Binding, cwd mapping. (FILE-02 metadata: later.)"""

    async def read_binding_authority(
        self, scope: TrustedScope, binding_id: BindingId
    ) -> BindingAuthority: ...

    async def resolve_default_binding(
        self, scope: TrustedScope, environment_kind: EnvironmentKind
    ) -> BindingAuthority | None:
        """``workspace_requirement = DEFAULT`` resolution."""
        ...

    async def resolve_conversation_workspace(
        self, scope: TrustedScope
    ) -> ConversationWorkspace: ...

    async def read_activation(
        self, scope: TrustedScope, environment_kind: EnvironmentKind
    ) -> EnvironmentActivation | None:
        """Current activation of a kind in this Conversation (P0: ≤1).
        Every Environment Tool call is gated on this, binding or not."""
        ...


# --- Registered Workspaces (V3-P74 §10) ------------------------------------------

MAX_WORKSPACES = 200  # a principal's registered folders, per Environment kind
MAX_ATTACHED_WORKSPACES = 16  # attached to one Conversation, its own folder not counted
MAX_WORKSPACE_LABEL_CHARS = 100
MAX_WORKSPACE_NOTE_CHARS = 1_000
MAX_WORKSPACE_LOCATION_BYTES = 4_096  # UTF-8, as PATH_MAX


@dataclass(frozen=True)
class RegisteredWorkspace(DeepFrozen):
    """A folder a principal registered (V3-P74 §10.1): where a Conversation
    it is attached to may write. ``location`` is the real path the
    Environment checked when it was registered; Data keeps it as given and
    never takes it from a Tool's arguments."""

    workspace_id: WorkspaceId
    environment_kind: EnvironmentKind
    location: str
    label: str
    note: str | None  # the user's, shown to the model in the Current State
    created_at: datetime
    attached: int = 0  # Conversations it is attached to, the trash included
    filesystem: str | None = None  # its kind when registered (e.g. ``fuse.rclone``); not the user's to edit


@dataclass(frozen=True)
class AttachedWorkspace(DeepFrozen):
    binding: BindingAuthority
    workspace: RegisteredWorkspace | None  # None: the Conversation's own folder (never registered)


@dataclass(frozen=True)
class ConversationWorkspaces(DeepFrozen):
    """A Conversation's Workspaces of one Environment kind: its own folder
    first, then the attached ones in the order they were attached.
    ``working`` is the activation's default Binding: the working directory
    (§10.4)."""

    working: BindingId | None
    attached: tuple[AttachedWorkspace, ...]


class WorkspaceRegistry(Protocol):
    """V3-P74 §10: the folders a principal registered, and which of them a
    Conversation has attached. The principal comes from the trusted entry
    point (Web, CLI), never from model output: no Tool writes here (Q4).

    Attaching adds a Binding of the Workspace to the Conversation's
    activation of its kind, and makes it the working one; detaching removes
    it, and the last attached one left (else the Conversation's own folder)
    becomes the working one. Both change ``state_revision``: the model is
    told of its Workspaces in the Current State (§10.5). The Conversation's
    own folder cannot be detached."""

    async def list_workspaces(self, principal: PrincipalId, kind: EnvironmentKind) -> tuple[RegisteredWorkspace, ...]:
        """Oldest first."""
        ...

    async def register_workspace(self, principal: PrincipalId, kind: EnvironmentKind, location: str, label: str,
                                 note: str | None = None, filesystem: str | None = None) -> RegisteredWorkspace:
        """DUPLICATE_IDENTITY: one is registered at ``location`` already.
        LIMIT_EXCEEDED past MAX_WORKSPACES. INVALID_ARGUMENT: ``location`` is
        not an absolute, normalized path of at most
        MAX_WORKSPACE_LOCATION_BYTES, or the label, note or filesystem is not
        one."""
        ...

    async def update_workspace(self, principal: PrincipalId, workspace_id: WorkspaceId, *, label: str,
                               note: str | None) -> RegisteredWorkspace:
        """NOT_FOUND: not one of the principal's."""
        ...

    async def unregister_workspace(self, principal: PrincipalId, workspace_id: WorkspaceId) -> None:
        """NOT_FOUND as above; INVALID_STATE_TRANSITION while it is attached
        to a Conversation (the trash included). The folder is not touched."""
        ...

    async def read_conversation_workspaces(self, scope: TrustedScope,
                                           kind: EnvironmentKind) -> ConversationWorkspaces:
        """NOT_FOUND: the kind is not active in the Conversation."""
        ...

    async def attach_workspace(self, scope: TrustedScope, workspace_id: WorkspaceId) -> BindingAuthority:
        """NOT_FOUND: not one of the principal's Workspaces.
        INVALID_STATE_TRANSITION: its kind is not active in the Conversation.
        DUPLICATE_IDENTITY: attached already. LIMIT_EXCEEDED past
        MAX_ATTACHED_WORKSPACES."""
        ...

    async def detach_workspace(self, scope: TrustedScope, binding_id: BindingId) -> None:
        """NOT_FOUND: no such Binding in the Conversation.
        INVALID_STATE_TRANSITION: the Conversation's own folder."""
        ...

    async def set_working_workspace(self, scope: TrustedScope, binding_id: BindingId) -> None:
        """NOT_FOUND: no such Binding in the Conversation."""
        ...

    async def list_conversations_working_in(self, principal: PrincipalId, workspace_id: WorkspaceId, *,
                                            limit: int = 50) -> Sequence[ConversationSummary]:
        """V3-P75 §6.1: the principal's active Conversations whose working
        folder (an activation's default Binding) is this registered Workspace;
        most recent activity first. None for a Workspace not the principal's.
        ``limit`` is 1..MAX_LIST_LIMIT, else INVALID_ARGUMENT."""
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
        activations: Sequence[InitialActivation] = (),
    ) -> ConversationId:
        """T0: a new Conversation owned by ``principal`` with its initial
        Environment activations, atomically. The id is generated by Data.
        A title longer than MAX_TITLE_CHARS raises INVALID_ARGUMENT; a blank
        title is stored as None."""
        ...

    async def most_recent_user_conversation(self, principal: PrincipalId) -> ConversationSummary | None:
        """V3-P83 §8.2: outside the trash, the Conversation whose latest user
        message (answers included) is the latest; None without any."""
        ...

    async def list_waiting_conversations(self, principal: PrincipalId) -> Sequence[ConversationId]:
        """V3-P80 §3.2: the principal's Conversations outside the trash in
        which ``find_waiting_call`` finds a call, in one read; in no order."""
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
    "AnswerCommit",
    "AppView",
    "Artifact",
    "ArtifactMutation",
    "ArtifactStore",
    "AssistantFinal",
    "AssistantOutput",
    "AssistantToolCalls",
    "AttachedWorkspace",
    "BindingAuthority",
    "CompletedGroupRef",
    "ConversationCatalog",
    "ConversationEvent",
    "ConversationLog",
    "ConversationModelSettings",
    "ConversationRetention",
    "ConversationSummary",
    "ConversationWorkspace",
    "ConversationWorkspaces",
    "DataError",
    "DataErrorCode",
    "EnvironmentActivation",
    "EventKind",
    "EventPayload",
    "EventRef",
    "ExternalSourceSetting",
    "ExternalSourceSettings",
    "InitialActivation",
    "MAX_APP_PAYLOAD_BYTES",
    "MAX_ATTACHED_WORKSPACES",
    "MAX_EFFORT_CHARS",
    "MAX_EXTERNAL_SETTINGS",
    "MAX_LIST_LIMIT",
    "MAX_MODEL_ID_CHARS",
    "MAX_TITLE_CHARS",
    "MAX_UI_HTML_BYTES",
    "MAX_WORKSPACES",
    "MAX_WORKSPACE_LABEL_CHARS",
    "MAX_WORKSPACE_LOCATION_BYTES",
    "MAX_WORKSPACE_NOTE_CHARS",
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
    "RegisteredWorkspace",
    "RunPreparation",
    "RunRecord",
    "RunStatus",
    "StateQuery",
    "StoredAppView",
    "ToolCompletion",
    "ToolExecutionRecord",
    "ToolExecutionStatus",
    "ToolResultRecord",
    "TrashedConversation",
    "TriggerRef",
    "UiCsp",
    "UiResource",
    "UserMessage",
    "WorkspaceRegistry",
    "WorkspaceState",
    "ui_digest",
]
