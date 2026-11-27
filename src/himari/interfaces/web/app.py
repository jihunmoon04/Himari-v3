"""HIMARI Web server: a JSON API over the assembled Application plus the
page built from ``web/`` (V3-P46, V3-P102).

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
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import posixpath
import re
import secrets
import threading
import time
import unicodedata
from urllib.parse import quote
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones
from typing import Any, BinaryIO, Literal

from fastapi import Depends, FastAPI, Path, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from himari.bootstrap.application import (
    Application,
    ConnectionGenerationRequired,
    ConversationStillStopping,
    DayClosed,
    DeleteNotice,
    FileNameRefused,
    LIFE_HISTORY_DAYS,
    LIFE_HISTORY_MAX_DAYS,
    LifeChanged,
    MemoryLine,
    AutoMemoryStatus,
    MemoryPassesOff,
    PassLine,
    FilesUnavailable,
    ModelChoiceView,
    ModelRefused,
    ModelView,
    PromptRefused,
    SCREEN_MAX_BYTES,
    ScreenRefused,
    StaleConnectionGeneration,
    PromptView,
    ToolSourceView,
    UnknownToolSource,
    FolderBusy,
)
from himari.bootstrap.companion import origin_view
from himari.bootstrap.screens import Answer
from himari.bootstrap.proactive import ProactiveRefused, loop_moment
from himari.bootstrap.operator import OPERATOR, derive_cli_bearer, verify_operator_token
from himari.bootstrap.mcp_defaults import McpDefaultsConflict
from himari.life.api import MindModel
from himari.prompt.api import HimariNow
from himari.presence.api import (
    MAX_IDLE_SECONDS,
    ActivitySignal,
    After,
    DeviceState,
    MediaSignal,
    PairingRefused,
    QuietTooLong,
    Session,
    Signals,
    Sleep,
    TaskEnded,
    TaskSignal,
    TooManyStreams,
    is_device_token,
)
from himari.common import (
    LEGACY_ASK_USER,
    ACTIVITIES,
    MOODS,
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
    split_marks,
    drop_thought,
    thought_of,
    thaw,
)
from himari.control.api import ContextStatus, ControlError, ControlErrorCode, JobStatus, TurnUsage
from himari.data.api import (
    AsleepAnswer,
    PlanKind,
    PlanWrite,
    SleepRecord,
    SleepView,
    plan_span,
    IMAGE_HEAD_BYTES,
    sniff_image,
    ALREADY_SENT,
    MAX_ATTACHMENTS,
    MAX_INSTRUCTIONS_CHARS,
    MAX_PERSONA_CHARS,
    MAX_PERSONA_NAME_CHARS,
    MAX_PERSONAS,
    PERSONA_FIELDS,
    MAX_LIST_LIMIT,
    MAX_ARC_CHARS,
    MAX_ARC_TITLE_CHARS,
    MAX_WORK_CHARS,
    MAX_WORK_SUMMARY_CHARS,
    MAX_EVENT_CHARS,
    MAX_DIARY_CHARS,
    MAX_LIFE_CHARS,
    MAX_LIFE_FACTS,
    MAX_BOND_CHARS,
    MAX_BOND_ENTRIES,
    MAX_MEMORIES,
    MAX_MEMORY_CHARS,
    BondStage,
    BondStanding,
    BondTag,
    LifeEntry,
    LifeKind,
    AssistantFinal,
    AssistantToolCalls,
    ConversationEvent,
    ConversationSummary,
    DataError,
    DataErrorCode,
    FileOrigin,
    FileRecord,
    ImageStatus,
    Device,
    InstructionScope,
    Instructions,
    Memory,
    Persona,
    PersonaChoice,
    PersonaList,
    HimariMood,
    MemoryCategory,
    MessageOrigin,
    OriginKind,
    ToolResultRecord,
    UserMessage,
    view_state,
    MAX_EPISODE_CHARS,
    MAX_LOOP_CHARS,
    Episode,
    NoteDates,
    Proposal,
    RecentEpisode,
    parse_note_date,
    Wake,
    WakeRecord,
    WakeKind,
    WakeReason,
)
from himari.interfaces.web import event_stream, live
from himari.interfaces.web.config import WebConfig
from himari.interfaces.web import next_page
from himari.interfaces.web.security import (
    SESSION_COOKIE,
    STYLE_NONCE_STATE,
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

Opener = Callable[[], Awaitable[Application]]


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass(frozen=True)
class Caller:
    """An authenticated caller (V3-P83 §4.3): whose, from where, and the device if it is one."""

    principal: PrincipalId
    origin: MessageOrigin
    device: Device | None = None


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


class DeviceNameBody(_Body):
    name: str = Field(max_length=200)  # Data checks the 40-character limit after trimming


class ActivityBody(_Body):
    """V3-P88 §4: the program in front, as the app classified it."""

    category: str = Field(max_length=64)
    app: str | None = Field(default=None, max_length=1000)
    for_seconds: float = Field(default=0, ge=0, le=30 * 24 * 3600, allow_inf_nan=False)
    fullscreen: StrictBool | None = None
    title: str | None = Field(default=None, max_length=1000)


class MediaBody(_Body):
    """V3-P88 §5: what plays now."""

    title: str = Field(max_length=1000)
    artist: str | None = Field(default=None, max_length=1000)
    app: str | None = Field(default=None, max_length=1000)


class TaskBody(_Body):
    """V3-P90 §6.1: the task that goes on."""

    group: Literal["work", "video", "game"]
    for_seconds: float = Field(ge=0, le=MAX_IDLE_SECONDS, allow_inf_nan=False)


class TaskEndedBody(_Body):
    """V3-P90 §5.3: a task that clearly ended (the app sends games and videos)."""

    group: Literal["work", "video", "game"]
    lasted_seconds: float = Field(ge=0, le=MAX_IDLE_SECONDS, allow_inf_nan=False)
    ended_seconds_ago: float = Field(ge=0, le=MAX_IDLE_SECONDS, allow_inf_nan=False)


class PresenceFeaturesBody(_Body):
    """V3-P90 §9: the app's own settings the server keeps."""

    free_wake_screen: StrictBool = False
    screen_look: StrictBool = False  # V3-P91 §7.1
    file_download: StrictBool = False  # V3-P93 §6.1.2


class PresenceBody(_Body):
    """V3-P83 §5: seconds since the last input anywhere on the device (cut to
    30 days), and its IANA time zone (an unknown one is ignored). V3-P88:
    the rest of the device's snapshot (§2.1) — an absent field is unknown,
    a null ``activity``/``media`` is none. V3-P90 §3.1: ``sleep`` (null:
    awake) from an app that decides when its character sleeps. §9:
    ``features`` (unsent: every one off), only to a server that announces
    ``features.free_wake_screen``."""

    idle_seconds: float = Field(ge=0, allow_inf_nan=False)
    time_zone: str | None = Field(default=None, max_length=64)
    session: Literal["active", "locked"] | None = None
    after: Literal["unlock", "resume"] | None = None
    activity: ActivityBody | None = None
    media: MediaBody | None = None
    mic_or_camera: StrictBool | None = None
    sleep: Literal["idle", "display_off"] | None = None
    task: TaskBody | None = None
    task_ended: TaskEndedBody | None = None
    features: PresenceFeaturesBody | None = None


class ScreenRefuseBody(_Body):
    """V3-P91 §4.2: why the app sends no screen."""

    reason: Literal["capture_failed", "excluded", "turned_off", "asleep"]


SCREEN_TYPES_TAKEN = frozenset({"image/png", "image/jpeg"})  # V3-P84 §4.1: a screen is one of these
SCREEN_ANSWERS = {Answer.UNKNOWN: (404, "not_found", "No such screen request for this device."),
                  Answer.ENDED: (410, "request_ended", "The screen request has ended.")}


class QuietBody(_Body):
    """V3-P88 §6.2: until when (with its UTC offset), or null to end it."""

    until: str | None = Field(max_length=64)


# V3-P88 §4.2, §5: how much of the app's text is kept.
SIGNAL_NAME_CHARS = 64
SIGNAL_TITLE_CHARS = 120


def signal_text(raw: str | None, limit: int) -> str | None:
    """Control characters out, spaces collapsed, cut to ``limit``; None when nothing is left."""
    if raw is None:
        return None
    kept = " ".join("".join(" " if unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") else c for c in raw).split())
    return kept[:limit] or None


def presence_signals(body: PresenceBody) -> Signals:
    """The body as the hub's snapshot (§2.1): a category outside ACTIVITIES is ``other`` (§4.1)."""
    sent = body.model_fields_set
    activity = None
    if body.activity is not None:
        given = body.activity
        activity = ActivitySignal(given.category if given.category in ACTIVITIES else "other",
                                  signal_text(given.app, SIGNAL_NAME_CHARS), float(given.for_seconds),
                                  given.fullscreen, signal_text(given.title, SIGNAL_TITLE_CHARS))
    media = None
    if body.media is not None and (title := signal_text(body.media.title, SIGNAL_TITLE_CHARS)) is not None:
        media = MediaSignal(title, signal_text(body.media.artist, SIGNAL_TITLE_CHARS),
                            signal_text(body.media.app, SIGNAL_NAME_CHARS))
    return Signals(session=None if body.session is None else Session(body.session),
                   activity_known="activity" in sent, activity=activity,
                   media_known="media" in sent, media=media, mic_or_camera=body.mic_or_camera,
                   after=None if body.after is None else After(body.after),
                   sleep_known="sleep" in sent, sleep=None if body.sleep is None else Sleep(body.sleep),
                   task=None if body.task is None else TaskSignal(body.task.group, float(body.task.for_seconds)),
                   task_ended=None if (ended := body.task_ended) is None else TaskEnded(
                       ended.group, float(ended.lasted_seconds), float(ended.ended_seconds_ago)),
                   free_wake_screen=body.features is not None and body.features.free_wake_screen,
                   screen_look=body.features is not None and body.features.screen_look)


def signals_view(state: DeviceState | None) -> dict[str, Any]:
    """V3-P88 §8.1: what the server knows now of a connected device, as the Web shows it."""
    if state is None or not state.connected:
        return {}
    view: dict[str, Any] = {"session": "locked" if state.locked else "active"}
    if (activity := state.activity) is not None:
        view["activity"] = {"category": activity.category, "for_seconds": int(activity.for_seconds),
                            **{k: v for k, v in (("app", activity.app), ("fullscreen", activity.fullscreen),
                                                 ("title", activity.title)) if v is not None}}
    if (media := state.media) is not None:
        view["media"] = {"title": media.title, **{k: v for k, v in (("artist", media.artist), ("app", media.app))
                                                  if v is not None}}
    if state.mic_or_camera is not None:
        view["mic_or_camera"] = state.mic_or_camera
    if state.sleeps:  # V3-P90 §3.1
        view["sleep"] = state.sleep
    if (task := state.task) is not None:
        view["task"] = {"group": task.group, "for_seconds": int(task.for_seconds)}
    if (ended := state.task_ended) is not None:
        view["task_ended"] = {"group": ended.group, "lasted_seconds": int(ended.lasted_seconds),
                              "ended_seconds_ago": int(ended.ended_seconds_ago)}
    if (before := state.before) is not None:
        view["before"] = {"category": before.category, "lasted_seconds": int(before.lasted_seconds),
                          "ended_seconds_ago": int(before.ended_seconds_ago),
                          **({"app": before.app} if before.app is not None else {})}
    if state.free_wake_screen:  # V3-P90 §9: a setting, not told to the model
        view["free_wake_screen"] = True
    if state.screen_look:  # V3-P91 §7.1: likewise
        view["screen_look"] = True
    return view


class CreateBody(_Body):
    title: str | None = Field(default=None, max_length=1000)


class RenameBody(_Body):
    title: str | None = Field(max_length=1000)


class ClientFeaturesBody(_Body):
    """V3-P93 §3.2, §6.1.2: capabilities the sending app declares for this input."""

    file_download: StrictBool = False


class MessageBody(_Body):
    text: str
    attachments: list[str] = Field(default_factory=list)  # staged uploads' file ids (V3-P66)
    screen: str | None = Field(default=None, max_length=200)  # a device's staged screen (V3-P84 §4.2)
    client_features: ClientFeaturesBody | None = None


class ModelBody(_Body):
    model: str | None = Field(max_length=200)  # null: follow the default model
    effort: str | None = Field(default=None, max_length=32)  # null: the model's default effort


class InstructionsBody(_Body):
    text: str  # its limit is Data's (V3-P69 §6), counted after newlines are normalized
    revision: int = Field(ge=0)  # the revision the text was edited from


class PersonaBody(_Body):  # V3-P108 §6.1: the limits are Data's
    name: str
    text: str = ""  # an older client's whole text: saved as the field "free" (character-persona-fields §3)
    fields: dict[str, str] | None = None  # character-persona-fields §2: given, the text is made from them


class PersonaEditBody(PersonaBody):
    revision: int = Field(ge=1)  # the revision the persona was edited from


class PersonaChoiceBody(_Body):
    persona_id: int | None  # null: no persona
    revision: int = Field(ge=0)  # the choice's revision when it was read


class LifeFactBody(_Body):  # character-life §6: the limits are Data's
    text: str
    persona_id: int | None  # the persona whose life the page showed; null: no persona


class LifeEditBody(_Body):
    text: str  # an arc's goal (character-world §4); a work piece's text (character-work §4)
    revision: int = Field(ge=1)  # the revision the entry was edited from
    title: str | None = None  # an arc's or a work piece's title; null or left out: kept
    summary: str | None = None  # a work piece's one line; null or left out: kept


type PlanKindName = Literal["sleep", "work", "together", "own", "diary"]  # PlanKind's values (character-diary §10)


class PlanBody(_Body):  # character-day §18.11: a slot the user writes; the limits are Data's
    day: str
    time: str
    minutes: int
    plan_kind: PlanKindName
    text: str
    arc_id: int | None = None
    persona_id: int | None  # the persona whose schedule the page showed; null: no persona


class PlanEditBody(_Body):  # a slot changed (moved too) from the revision the page read
    day: str
    time: str
    minutes: int
    plan_kind: PlanKindName
    text: str
    arc_id: int | None = None
    revision: int = Field(ge=1)


type BondTagName = Literal["name", "promise", "joke", "first", "date"]  # BondTag's values


class BondBody(_Body):  # character-bond §1, Web: the limits and the day's form are Data's
    tag: BondTagName
    text: str
    month_day: str | None = None  # MM-DD, a date's only
    persona_id: int | None  # the persona whose bond the page showed; null: no persona


class BondEditBody(_Body):
    tag: BondTagName
    text: str
    month_day: str | None = None
    revision: int = Field(ge=1)


class BondStageBody(_Body):
    stage: Literal["new", "familiar", "close"]  # BondStage's values
    persona_id: int | None
    revision: int = Field(ge=0)  # the standing's revision when it was read (0: never stored)


class MemoryBody(_Body):
    category: Literal["user", "preference", "project", "reference", "loop"]  # MemoryCategory's values
    text: str  # its limit is Data's (V3-P71 §4.2)
    # V3-P85 §10.2: a loop's date and a fact's end, YYYY-MM-DD (or a time with its offset); null clears.
    # Both left out on an edit keep the note's. A bare date ends in ``time_zone`` (the browser's), else the server's.
    due: str | None = None
    valid_until: str | None = None
    # V3-P87 §5.3: a loop's reminder — a time with its offset, or the browser's local
    # "YYYY-MM-DDTHH:MM" (read in ``time_zone``); null clears. Left out on an edit: kept.
    remind_at: str | None = None
    time_zone: str | None = Field(default=None, max_length=64)


class EpisodeEditBody(_Body):
    text: str  # its limit is Data's (V3-P85 §6.1)
    revision: int = Field(ge=1)


class ProposalBody(_Body):
    decision: Literal["keep", "discard"]
    # A keep may give the note's dates instead of the proposal's (null: none), e.g. after a stale_date refusal.
    due: str | None = None
    valid_until: str | None = None
    time_zone: str | None = Field(default=None, max_length=64)


class BackfillBody(_Body):
    conversation_id: str


class MemoryEditBody(MemoryBody):
    revision: int = Field(ge=1)  # the revision the note was edited from


class ToolSourceBody(_Body):
    enabled: bool | None  # null: follow the source's default


class McpDefaultBody(_Body):
    enabled: StrictBool
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")


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


def display_view(text: str) -> dict[str, Any]:
    """What the model wrote as it is shown (V3-P83 §7.2, appendix A.6): the
    text without its marks, and each mark's place in it (code points) — moods,
    and moves on the latest screen (V3-P84 §6.2)."""
    plain, moods, moves, _ = split_marks(text, complete=True)
    return {"text": plain, "moods": [{"at": at, "mood": mood} for at, mood in moods],
            "moves": [{"at": at, "x": x, "y": y} for at, x, y in moves]}


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


def presented_file_view(record: FileRecord) -> dict[str, Any]:
    """V3-P93 §6.2: a publicly presented file as shown to a paired device."""
    return {"file_id": record.file_id, "version": record.version, "name": record.name,
            "media_type": record.media_type, "size": record.size, "sha256": record.blob.sha256,
            "created_at": _time(record.created_at), "expired": record.expired}


def _attachment(ref: AttachmentRef, files: Files) -> dict[str, Any]:
    found = files.get((ref.file_id, ref.version))
    view = (file_view(found) if found is not None
            else {"file_id": ref.file_id, "version": ref.version, "name": None, "expired": True, "image": None})
    return {**view, "screen": True} if ref.screen else view  # V3-P84 §4.3


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
        # character-day §18.11: a day session's date (null: an ordinary conversation), and whether it is closed
        "day_session": summary.day_session,
        "closed": summary.closed_at is not None,
    }


def trashed_view(summary: ConversationSummary, trash_days: int) -> dict[str, Any]:
    """A Conversation in the trash, with when it is deleted for good (V3-P65 §9)."""
    trashed_at = summary.trashed_at
    assert trashed_at is not None
    return {**summary_view(summary), "trashed_at": _time(trashed_at),
            "purge_after": _time(trashed_at + timedelta(days=trash_days))}


def delete_notice_view(notice: DeleteNotice) -> dict[str, Any]:
    return {"servers": list(notice.servers), "unconfigured": list(notice.unconfigured), "running": notice.running,
            "trash_days": notice.trash_days, "memory_count": notice.memories, "episode_count": notice.episodes}


def _plain(value: Any) -> Any:
    """Times as the API writes them, in nested dicts and lists (V3-P87 status)."""
    if isinstance(value, datetime):
        return _time(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def wake_reason_view(reason: WakeReason) -> dict[str, Any]:
    out: dict[str, Any] = {"kind": reason.kind.value}
    for name in ("memory_id", "away_minutes", "via", "activity", "group", "minutes", "days", "years", "entry_id"):
        if (value := getattr(reason, name)) is not None:
            out[name] = value
    if reason.late:  # V3-P107 §2: "again" is no more (an old record's is read past)
        out["late"] = True
    return out


def _span(minutes: int) -> str:
    return f"{minutes}m" if minutes < 60 else f"{minutes // 60}h {minutes % 60}m" if minutes % 60 else f"{minutes // 60}h"


def wake_summary(reasons: Sequence[WakeReason]) -> str:
    """V3-P87 §9: why HIMARI reached out, in the page's English, from the reasons only."""
    parts = []
    for r in reasons:
        if r.kind is WakeKind.LOOP_DUE:
            parts.append(f"open loop m{r.memory_id}")
        elif r.kind is WakeKind.REMINDER:
            parts.append(f"reminder m{r.memory_id}" + (" (late)" if r.late else ""))
        elif r.kind is WakeKind.RETURNED:
            parts.append(f"you were away {_span(r.away_minutes or 0)}")
        elif r.kind is WakeKind.FOCUS:
            parts.append(f"{r.activity or 'focus'} for {_span(r.minutes or 0)}")
        elif r.kind is WakeKind.GAME_ENDED:
            parts.append(f"a game ended after {_span(r.minutes or 0)}")
        elif r.kind is WakeKind.TASK_ENDED:
            parts.append(f"{'a game' if r.group == 'game' else 'a video'} ended after {_span(r.minutes or 0)}")
        elif r.kind is WakeKind.WORK_DONE:  # character-work §7.1
            parts.append(f"finished a piece of work (w{r.entry_id})")
        elif r.kind is WakeKind.TOGETHER:  # character-day §9
            parts.append(f"a time set together came (p{r.entry_id})")
        elif r.kind is WakeKind.POKE:  # character-day §5.5
            parts.append("you poked HIMARI in its sleep")
        elif r.kind is WakeKind.ANNIVERSARY:  # character-bond §6
            parts.append(f"{r.days} days together" if r.days is not None else
                         f"{r.years} year{'s' if (r.years or 0) > 1 else ''} together" if r.years is not None else
                         f"a day to mark (b{r.entry_id})")
        else:
            parts.append("a try")
    return "HIMARI reached out · " + " · ".join(parts)


def event_view(event: ConversationEvent,
               models: dict[RunId, str] | None = None, files: Files | None = None,
               presented: dict[CallRef, list[FileRecord]] | None = None,
               memory: dict[CallRef, MemoryLine] | None = None, *, device: bool = False) -> dict[str, Any]:
    """``models``: the model of each Run that wrote an assistant event (V3-P64 §7).
    ``files``: the page's attachments by (id, version); ``presented``: the
    files each call gave the user (V3-P66 §7.3); ``memory``: what each
    memory call changed (V3-P71 §7.1). character-mind §3.2: HIMARI's thought is
    the owner's on the Web only — for a ``device`` the text leaves it out and
    there is no ``thought``."""
    models = {} if models is None else models
    files = {} if files is None else files
    presented = {} if presented is None else presented
    memory = {} if memory is None else memory
    view: dict[str, Any] = {"sequence": event.sequence, "kind": event.kind.value, "created_at": _time(event.created_at)}
    payload = event.payload
    if isinstance(payload, UserMessage):
        view["text"] = _text(payload.blocks)
        view["attachments"] = [_attachment(b, files) for b in payload.blocks if isinstance(b, AttachmentRef)]
        view["references"] = [reference_view(b) for b in payload.blocks if isinstance(b, FileReference)]
        # Where it was sent from (V3-P84 §7.8): an app tells its own message whose answer it lost.
        view["origin"] = origin_view(payload.origin)
        if not device:  # character-day §12, §18.11: received while HIMARI slept, and how (the owner's Web only)
            view["while_asleep"] = None if payload.while_asleep is None else payload.while_asleep.value
    elif isinstance(payload, AssistantFinal):
        view["text"] = _text(drop_thought(payload.blocks) if device else payload.blocks)
        view["display"] = display_view(view["text"])
        if not device:
            view["thought"] = thought_of(payload.blocks)  # character-mind §3.2: shown folded to the owner
        view["model"] = models.get(event.origin_run_id) if event.origin_run_id else None
        view["run_id"] = event.origin_run_id  # the live output it replaces (V3-P73 §7.2)
    elif isinstance(payload, AssistantToolCalls):
        view["text"] = _text(drop_thought(payload.commentary) if device else payload.commentary)
        view["display"] = display_view(view["text"])
        if not device:
            view["thought"] = thought_of(payload.commentary)
        view["model"] = models.get(event.origin_run_id) if event.origin_run_id else None
        view["run_id"] = event.origin_run_id
        view["calls"] = [{"call_id": c.call_id, "tool_id": c.tool_id, "arguments": thaw(c.arguments)}
                         for c in sorted(payload.calls, key=lambda c: c.ordinal)]
    elif isinstance(payload, Wake):  # V3-P87 §9: a dim line, not a bubble
        view["wake"] = {"id": payload.wake_id, "device_id": payload.device_id,
                        "reasons": [wake_reason_view(r) for r in payload.reasons]}
        view["text"] = wake_summary(payload.reasons)
    elif isinstance(payload, ToolResultRecord):
        view["run_id"] = payload.call.run_id
        view["call_id"] = payload.call.call_id
        view["is_error"] = payload.model_result.is_error
        view["text"] = _text(payload.model_result.blocks)
        view["presented_files"] = [file_view(f) for f in presented.get(payload.call, [])]
        line = memory.get(payload.call)
        view["memory"] = None if line is None else {"action": line.action, "id": line.memory_id, "text": line.text,
                                                    "undo": line.undo}
        # V3-P67 §8: the images the call returned (kept as files of the call) — never an older
        # ask_user answer's attachments, which are its result too (PR #63 review C2).
        view["images"] = [file_view(found) for b in payload.model_result.blocks if isinstance(b, AttachmentRef)
                          if (found := files.get((b.file_id, b.version))) is not None
                          and found.origin is FileOrigin.TOOL]
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
        "job_id": status.job_id,  # V3-P84 §6.3: the same as the job events' and live's
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


def sent_view(status: JobStatus) -> dict[str, Any]:
    """A message's 202 (character-day §18.11): the job, and how a sleeping HIMARI met it (``"none"``: no answer and
    no job; null: awake)."""
    return {**job_view(status), "asleep": None if status.asleep is None else status.asleep.value}


def turn_usage_view(usage: TurnUsage) -> dict[str, Any]:
    """What the job's drive used so far (V3-P62 §7)."""
    return {"runs": usage.runs, "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
            "cached_input_tokens": usage.cached_input_tokens, "cache_write_tokens": usage.cache_write_tokens,
            "max_runs": usage.max_runs}


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
            "window_known": view.window_known, "default": view.is_default, "images": view.images,
            "companion": view.companion}


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
    """V3-P71 §7.2, V3-P85 §10.2. ``titles``: each source Conversation's title
    (None: untitled) and preview, which the page names it by when untitled; a
    source missing from it is in the trash."""
    source, now = note.source_conversation, datetime.now(UTC)
    return {"id": note.memory_id, "category": note.category.value, "text": note.text, "origin": note.origin.value,
            "source": None if source is None else {
                "conversation": source, "title": titles.get(source, (None, ""))[0],
                "preview": titles.get(source, (None, ""))[1], "in_trash": source not in titles},
            "revision": note.revision, "created_at": _time(note.created_at), "updated_at": _time(note.updated_at),
            "due": note.due_at, "overdue": note.due_instant is not None and note.due_instant < now,
            "valid_until": note.valid_until, "expired": note.valid_instant is not None and note.valid_instant < now,
            "kept_by_user": note.user_approved_at is not None,
            # V3-P87 §9: when HIMARI checks in on a loop, and its reminder and how it went
            "checks_in_at": _time(loop_moment(note)), "remind_at": note.remind_at,
            "remind_instant": _time(note.remind_instant), "remind_state": note.remind_state,
            "reminded_at": _time(note.reminded_at)}


def auto_memory_view(status: AutoMemoryStatus) -> dict[str, Any]:
    """V3-P85 §10.3: the Memory screen's status line."""
    summary = status.summary
    return {"running": status.running, "told": status.told, "model": status.model_id,
            "daily_tokens": status.daily_tokens, "today_tokens": summary.charged_today,
            "last_applied_at": _time(summary.last_applied_at), "failed": summary.failed,
            "skipped": [{"conversation": p.conversation_id, "pass_id": p.pass_id} for p in summary.skipped]}


def pass_line_view(line: PassLine) -> dict[str, Any]:
    return {"pass_id": line.pass_id, "through_sequence": line.through_sequence,
            "items": [{"item": i.item, "action": i.action, "id": i.memory_id, "text": i.text, "due": i.due,
                       "undo": i.undo, "evidence": list(i.evidence)} for i in line.items]}


def proposal_view(proposal: Proposal, previews: dict[ConversationId, str] | None = None) -> dict[str, Any]:
    record = proposal.record
    try:
        planned = json.loads(record.planned or "{}")
    except ValueError:
        planned = {}
    try:
        cited = json.loads(record.proposal).get("evidence")
    except (ValueError, AttributeError):
        cited = None
    return {"pass_id": record.pass_id, "item": record.item,
            "kind": "sensitive" if record.outcome == "sensitive_proposed" else "past",
            # the user messages it rests on (checked when it was planned, V3-P85 §4.4), to read before keeping it
            "evidence": [n for n in cited if isinstance(n, int)] if isinstance(cited, list) else [],
            "category": planned.get("category"), "text": planned.get("text"), "due": planned.get("due_at"),
            "valid_until": planned.get("valid_until"), "created_at": _time(record.created_at),
            "conversation": {"id": proposal.conversation_id, "title": proposal.title,
                             "preview": (previews or {}).get(proposal.conversation_id, ""),
                             "in_trash": proposal.in_trash}}


def episode_view(found: RecentEpisode, previews: dict[ConversationId, str] | None = None) -> dict[str, Any]:
    episode = found.episode
    return {"id": episode.episode_id, "conversation": {"id": episode.conversation_id, "title": found.title,
                                                       "preview": (previews or {}).get(episode.conversation_id, "")},
            "started_at": _time(episode.started_at), "ended_at": _time(episode.ended_at), "text": episode.text,
            "origin": episode.origin.value, "revision": episode.revision,
            "user_sequences": list(episode.user_sequences), "himari_sequences": list(episode.himari_sequences)}


def episode_cursor(episode: Episode) -> str:
    """The Timeline's opaque page cursor: the last episode shown (§10.3)."""
    return base64.urlsafe_b64encode(f"{episode.ended_at.isoformat()}|{episode.episode_id}".encode()).decode()


def read_episode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        when, number = base64.urlsafe_b64decode(cursor.encode()).decode().rsplit("|", 1)
        moment = datetime.fromisoformat(when)
    except (ValueError, UnicodeDecodeError) as error:
        raise ApiError(400, "invalid_argument", "not a timeline cursor") from error
    if moment.tzinfo is None or not re.fullmatch(r"[0-9]{1,18}", number):  # not one we gave (PR #138 review)
        raise ApiError(400, "invalid_argument", "not a timeline cursor")
    return moment, int(number)


def _reminder(value: str, zone: str) -> tuple[str, datetime] | None:
    """V3-P87 §5.3: a time with its offset, or a local "YYYY-MM-DDTHH:MM" read in ``zone``; never a day."""
    value = value.strip()
    if len(value) == 16 and value[10] == "T":
        try:
            local = datetime.fromisoformat(value).replace(tzinfo=ZoneInfo(zone))
        except (ValueError, ZoneInfoNotFoundError):
            return None
        value = local.isoformat(timespec="minutes")
    found = parse_note_date(value, zone)
    return None if found is None or len(found[0]) == 10 else found


def note_dates(body: MemoryBody, server_zone: str, *, editing: bool, current: Memory | None = None
               ) -> NoteDates | None:
    """V3-P85 §10.2: the dates a note body gives; None (keep the note's) for an
    edit that names none. On an edit a date not named stays as the note has it
    (``current``). A date that does not read is a 422."""
    named = {"due", "valid_until", "remind_at"} & body.model_fields_set
    if editing and not named:
        return None
    zone = body.time_zone if body.time_zone and body.time_zone in available_timezones() else server_zone
    parsed: list[tuple[str, datetime] | None] = []
    for name, value in (("due", body.due), ("valid_until", body.valid_until)):
        found = None if value is None or not value.strip() else parse_note_date(value, zone)
        if value is not None and value.strip() and found is None:
            raise ApiError(422, "invalid_date", "A date is YYYY-MM-DD, or a time with its UTC offset.")
        kept = None if current is None else ((current.due_at, current.due_instant) if name == "due"
                                              else (current.valid_until, current.valid_instant))
        if editing and name not in named and kept is not None and kept[0] is not None and kept[1] is not None:
            found = (kept[0], kept[1])
        parsed.append(found)
    due, valid = parsed
    remind = None if body.remind_at is None or not body.remind_at.strip() else _reminder(body.remind_at, zone)
    if body.remind_at is not None and body.remind_at.strip() and remind is None:
        raise ApiError(422, "invalid_date", "A reminder is a time: YYYY-MM-DDTHH:MM, or with its UTC offset.")
    if editing and "remind_at" not in named and current is not None and current.remind_at is not None \
            and current.remind_instant is not None:
        remind = (current.remind_at, current.remind_instant)
    if body.category == "loop":
        valid = None
    else:
        due = remind = None
    return NoteDates(None if due is None else due[0], None if due is None else due[1],
                     None if valid is None else valid[0], None if valid is None else valid[1],
                     None if remind is None else remind[0], None if remind is None else remind[1])


def pairing_token(raw: bytes) -> str | None:
    """The token in a pairing body, or None for any body but ``{"token": "<string>"}``
    (appendix A.1). Deep nesting fails the parser with RecursionError, not
    ValueError (review C4)."""
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    token = body.get("token") if isinstance(body, dict) and set(body) == {"token"} else None
    return token if isinstance(token, str) else None


def device_view(device: Device, connected: bool = False, current: bool = False) -> dict[str, Any]:
    """A device as the Web's Devices screen shows it (V3-P83 appendix A.1)."""
    return {"device_id": device.device_id, "name": device.name, "paired": device.paired_at is not None,
            "connected": connected, "current": current, "created_at": _time(device.created_at),
            "paired_at": _time(device.paired_at), "last_seen_at": _time(device.last_seen_at),
            "pairing_expires_at": None if device.paired_at is not None else _time(device.pairing_expires_at)}


# V3-P87 §11: why a try opened no wake.
PROACTIVE_REFUSALS = {
    "proactive_off": "Speaking first is off on this server (HIMARI_PROACTIVE).",
    "no_device": "No device is current: open the app and use the computer first.",
    "no_conversation": "There is no conversation to speak in yet.",
    "busy": "A conversation is working; try again when it is done.",
    "closed": "The most recent conversation is being moved to the trash.",
    "not_settled": "The most recent conversation waits for an answer or a recovery first.",
    "model_unchecked": "The most recent conversation's model was not checked for speaking first; choose another.",
}


# How a refused device write answers (V3-P83 appendix A.1): status, code, message.
DEVICE_REFUSALS: dict[DataErrorCode, tuple[int, str, str]] = {
    DataErrorCode.NOT_FOUND: (404, "not_found", "No such device."),
    DataErrorCode.DUPLICATE_IDENTITY: (409, "duplicate_name", "Another device has that name."),
    DataErrorCode.INVALID_ARGUMENT: (422, "invalid_name",
                                     "A device name is 1 to 40 characters on one line, and not “web” or “cli”."),
    DataErrorCode.LIMIT_EXCEEDED: (422, "too_many_devices", "Revoke a device before adding another."),
}

# How a refused pairing answers (appendix A.1).
PAIRING_STATUS = {"pairing_invalid": 401, "pairing_expired": 401, "pairing_used": 409, "invalid_token": 400,
                  "token_in_use": 409, "pairing_busy": 429}
PAIR_PATH = "/api/devices/pair"
PAIR_BODY_BYTES = 1024  # {"token": "hd_…"} is about 60 bytes; the one route without a sign-in reads no more (review C4)
PAIRING_MESSAGES = {
    "pairing_invalid": "That code is not valid. Make a new one in Settings → Devices if it keeps failing.",
    "pairing_expired": "That code has ended. Make a new one in Settings → Devices.",
    "pairing_used": "That code already paired another device.",
    "invalid_token": "The device's token is not usable.",
    "token_in_use": "Another device has that token; make a new token and pair again.",
    "pairing_busy": "Too many pairings are waiting; try again shortly.",
}


def persona_view(persona: Persona) -> dict[str, Any]:
    """V3-P108 §6.1: one of the user's personas."""
    return {"id": persona.persona_id, "name": persona.name, "text": persona.text, "fields": dict(persona.fields),
            "revision": persona.revision, "created_at": _time(persona.created_at),
            "updated_at": _time(persona.updated_at)}


def choice_view(choice: PersonaChoice) -> dict[str, Any]:
    return {"persona_id": choice.persona_id, "revision": choice.revision, "updated_at": _time(choice.updated_at)}


# How a refused persona change answers (V3-P108 §6.1): status, code, message; 422, not 413, as for instructions.
PERSONA_REFUSALS: dict[DataErrorCode, tuple[int, str, str]] = {
    DataErrorCode.NOT_FOUND: (404, "not_found", "No such persona."),
    DataErrorCode.VERSION_CONFLICT: (409, "revision_conflict", "It was changed elsewhere since it was opened."),
    DataErrorCode.DUPLICATE_IDENTITY: (409, "duplicate_name", "Another persona has this name."),
    DataErrorCode.LIMIT_EXCEEDED: (409, "persona_limit", f"At most {MAX_PERSONAS} personas; delete one first."),
    DataErrorCode.TOO_LARGE: (422, "too_large", f"A persona is at most {MAX_PERSONA_CHARS:,} characters."),
    DataErrorCode.INVALID_ARGUMENT: (422, "invalid_persona", f"A persona needs a name of 1–{MAX_PERSONA_NAME_CHARS} "
                                                             "characters, only the known fields, and no control "
                                                             "characters."),
}


def life_entry_view(entry: LifeEntry) -> dict[str, Any]:
    """character-life §6: one entry of HIMARI's life."""
    view = {"id": entry.entry_id, "kind": entry.kind.value, "day": entry.day, "time": entry.time_of_day,
            "text": entry.text, "origin": entry.origin.value, "revision": entry.revision,
            "created_at": _time(entry.created_at), "updated_at": _time(entry.updated_at)}
    if entry.kind is LifeKind.ARC:  # character-world §4: its title, open or closed, and how it ended
        view |= {"title": entry.title, "status": "open" if entry.arc_open else "closed", "end_day": entry.end_day,
                 "ending": entry.ending, "so_far": entry.so_far}  # character-work §3: where the project stands
    elif entry.kind is LifeKind.WORK:  # character-work §4: a piece, its project and its plan
        view |= {"title": entry.title, "summary": entry.summary, "arc_id": entry.arc_id, "plan_id": entry.plan_id,
                 "draft": entry.draft}  # character-mind §4.4: begun at the start step, not finished yet
    elif entry.kind is LifeKind.THOUGHT:  # character-mind §5.2: what it is about and when it stops being told
        view |= {"about": None if entry.about is None else entry.about.value,
                 "expires_at": None if entry.expires_at is None else _time(entry.expires_at)}
    elif entry.arc_id is not None:  # a diary: the arc its day moved; a plan: its project (character-work §4)
        view["arc_id"] = entry.arc_id
    if entry.kind is LifeKind.PLAN:  # character-day §4.1, §18.11: a slot of the schedule
        view |= {"minutes": entry.minutes, "plan_kind": None if entry.plan_kind is None else entry.plan_kind.value,
                 "plan_state": None if entry.plan_state is None else entry.plan_state.value, "reason": entry.reason}
    return view


def sleep_view_json(view: SleepView | None, zone: ZoneInfo) -> dict[str, Any] | None:
    """character-day §12, §18.11: HIMARI's sleep now — its state, whether deep, the minutes to its slot while drowsy
    and tonight's slot (its date, start and length)."""
    if view is None:
        return None
    out: dict[str, Any] = {"state": view.state.value}
    if view.state.value in ("asleep", "half_asleep"):
        out["deep"] = view.deep
    if view.minutes_to_sleep is not None:
        out["minutes_to_sleep"] = view.minutes_to_sleep
    if view.tonight is not None:
        out["tonight"] = {"id": view.tonight.entry_id, "day": view.tonight.day, "time": view.tonight.time_of_day,
                          "minutes": view.tonight.minutes}
    return out


def sleep_record_view(sleep: SleepRecord) -> dict[str, Any]:
    """character-day §12: one sleep of the record."""
    return {"id": sleep.sleep_id, "fell_at": _time(sleep.fell_at), "how_fell": sleep.how_fell.value,
            "woke_at": _time(sleep.woke_at), "how_woke": None if sleep.how_woke is None else sleep.how_woke.value,
            "messages": sleep.messages, "mumbles": sleep.mumbles, "closed_day": sleep.closed_day}


def wake_thought_view(wake: WakeRecord) -> dict[str, Any]:
    """character-mind §3.3, §8 (review P1-5): a free wake that thought and then passed — never shown as a
    wake, so the owner's Web gets its own dim line with the thought folded."""
    return {"sequence": wake.event_sequence, "kind": "wake_thought", "created_at": _time(wake.created_at),
            "text": wake_summary(wake.reasons), "thought": wake.thought,
            # review 2: what really came of it — the model passed, or it was dropped (the user came first,
            # or it was found unshown at a restart): the Web words the dropped one neutrally
            "status": wake.status.value}


def mind_view(mind: MindModel) -> dict[str, Any]:
    """character-mind §8: a self run's model and why it was chosen."""
    return {"model": mind.model_id, "reason": mind.reason}


def mood_view(mood: HimariMood | None) -> dict[str, Any] | None:
    """character-state §2: HIMARI's mood while it is told."""
    return None if mood is None else {"mood": mood.mood, "set_at": _time(mood.set_at), "source": mood.source.value,
                                      "reason": mood.reason}


def now_view(now: HimariNow | None) -> dict[str, Any] | None:
    """character-state §3: what HIMARI is doing now."""
    return None if now is None else {"asleep": now.asleep, "doing": now.doing, "since": now.since}


def device_personas_view(listed: PersonaList) -> dict[str, Any]:
    """character-state §4 (H-45's exception): the personas' ids and names and the choice — never their text."""
    return {"personas": [{"id": p.persona_id, "name": p.name} for p in listed.personas],
            "choice": {"persona_id": listed.choice.persona_id, "revision": listed.choice.revision}}


def bond_entry_view(entry: LifeEntry) -> dict[str, Any]:
    """character-bond §1: one entry of the bond."""
    return {"id": entry.entry_id, "tag": None if entry.tag is None else entry.tag.value, "text": entry.text,
            "month_day": entry.month_day, "origin": entry.origin.value, "revision": entry.revision,
            "created_at": _time(entry.created_at), "updated_at": _time(entry.updated_at)}


def bond_stage_view(standing: BondStanding) -> dict[str, Any]:
    return {"stage": standing.stage.value, "since": None if standing.since is None else _time(standing.since),
            "reason": standing.reason, "revision": standing.revision}


def memories_view(entries: Sequence[LifeEntry], episodes: Sequence[RecentEpisode]) -> list[dict[str, Any]]:
    """character-bond §1: the memories' timeline — episodes and bond entries, newest first."""
    items: list[tuple[datetime, dict[str, Any]]] = [
        (e.created_at, {"kind": "bond", "id": e.entry_id, "at": _time(e.created_at),
                        "tag": None if e.tag is None else e.tag.value, "text": e.text}) for e in entries]
    items += [(f.episode.ended_at, {"kind": "episode", "id": f.episode.episode_id, "at": _time(f.episode.ended_at),
                                    "text": f.episode.text, "title": f.title}) for f in episodes]
    return [item for _, item in sorted(items, key=lambda pair: pair[0], reverse=True)]


# How a refused change of the bond answers (character-bond §1): status, code, message.
BOND_REFUSALS: dict[DataErrorCode, tuple[int, str, str]] = {
    DataErrorCode.NOT_FOUND: (404, "not_found", "No such entry."),
    DataErrorCode.VERSION_CONFLICT: (409, "revision_conflict", "It was changed elsewhere since it was opened."),
    DataErrorCode.LIMIT_EXCEEDED: (409, "bond_full", f"At most {MAX_BOND_ENTRIES} entries, all yours; delete one "
                                                     "first."),
    DataErrorCode.TOO_LARGE: (422, "too_large", f"An entry is at most {MAX_BOND_CHARS:,} characters."),
    DataErrorCode.INVALID_ARGUMENT: (422, "invalid_bond", "An entry needs some text and no control characters; a "
                                                          "date needs its day as MM-DD, and only a date has one."),
}


# How a refused change of HIMARI's life answers (character-life §6): status, code, message.
LIFE_REFUSALS: dict[DataErrorCode, tuple[int, str, str]] = {
    DataErrorCode.NOT_FOUND: (404, "not_found", "No such entry."),
    DataErrorCode.VERSION_CONFLICT: (409, "revision_conflict", "It was changed elsewhere since it was opened."),
    DataErrorCode.LIMIT_EXCEEDED: (409, "life_full", f"At most {MAX_LIFE_FACTS} facts, all yours; delete one first."),
    DataErrorCode.TOO_LARGE: (422, "too_large", f"A fact or a plan is at most {MAX_LIFE_CHARS:,} characters, a diary "
                                                f"{MAX_DIARY_CHARS:,}, an arc's goal {MAX_ARC_CHARS:,}, a title "
                                                f"{MAX_ARC_TITLE_CHARS:,}, a work piece {MAX_WORK_CHARS:,} and its "
                                                f"summary {MAX_WORK_SUMMARY_CHARS:,}, an event {MAX_EVENT_CHARS:,}."),
    DataErrorCode.INVALID_ARGUMENT: (422, "invalid_life", "An entry needs some text and no control characters; only "
                                                          "an arc or a work piece has a title, and only a piece a "
                                                          "summary, each on one line; a kept thought is only "
                                                          "deleted."),
}


# How a refused write of a note answers (V3-P71 §7.2): status, code, message.
MEMORY_REFUSALS: dict[DataErrorCode, tuple[int, str, str]] = {
    DataErrorCode.NOT_FOUND: (404, "not_found", "No such note."),
    DataErrorCode.VERSION_CONFLICT: (409, "revision_conflict", "The note was changed elsewhere since it was opened."),
    DataErrorCode.TOO_LARGE: (422, "too_large", f"A note is at most {MAX_MEMORY_CHARS:,} characters (an open loop "
                                                f"{MAX_LOOP_CHARS:,})."),
    DataErrorCode.LIMIT_EXCEEDED: (422, "memory_full", f"At most {MAX_MEMORIES:,} notes are kept; delete one first."),
    DataErrorCode.INVALID_ARGUMENT: (422, "invalid_memory", "A note needs some text and no control characters."),
    DataErrorCode.STALE_DATE: (422, "stale_date", "Its date has passed: keep it with a new date or without one."),
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
SCREEN_PATH = re.compile(r"^/api/device/screens/[^/]+$")  # V3-P91 §4.2: a screen of at most SCREEN_MAX_BYTES
GENERATION_HEADER = "X-Himari-Connection-Generation"  # V3-P93 §6.1.2: a device SSE stream's generation id

def reference_view(ref: FileReference) -> dict[str, Any]:
    """V3-P76 §3.1: a reference chip in the timeline — where it points, not its excerpt."""
    return {"path": ref.path, "name": posixpath.basename(ref.path), "whole": ref.start is None,
            "start_line": ref.start_line, "end_line": ref.end_line}


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


def create_app(config: WebConfig, opener: Opener) -> FastAPI:
    sessions = Sessions()
    closing = asyncio.Event()  # set as the server begins to shut down: the live streams end
    live_slots = live.Slots()  # open live streams of each principal
    # V3-P83 §8.1: the live streams each device opened, ended when it is revoked.
    device_lives: dict[tuple[PrincipalId, str], set[asyncio.Event]] = {}

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

    async def caller(request: Request) -> Caller:
        """Who calls, and as which origin (V3-P83 §4.2): the Web session, the
        CLI's operator bearer, or a paired device's token (``hd_…``, checked
        only as one). Only the routes a device may use depend on this."""
        found = sessions.principal(request.cookies.get(SESSION_COOKIE))
        if found is not None:
            return Caller(found, MessageOrigin(OriginKind.WEB))
        authorization = request.headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        token = token.strip()
        if scheme.lower() == "bearer" and is_device_token(token):
            application = himari(request)
            if not application.companion_enabled:
                raise ApiError(403, "companion_off", "Devices are turned off on this server.")
            device = await application.devices.authenticate(token)
            if device is None:
                raise ApiError(401, "unauthenticated", "This device is not paired; pair it again.")
            return Caller(device.principal, MessageOrigin(OriginKind.DEVICE, device.device.device_id), device.device)
        if scheme.lower() == "bearer" and verify_operator_token(token, request.app.state.bearer):
            return Caller(OPERATOR, MessageOrigin(OriginKind.CLI))
        raise ApiError(401, "unauthenticated", "Sign in first.")

    async def principal(request: Request) -> PrincipalId:
        """Every other route (V3-P83 §4.3): a device is refused by default."""
        who = await caller(request)
        if who.device is not None:
            raise ApiError(403, "device_scope", "A device may not use this.")
        return who.principal

    def scope_of(conversation_id: str, who: PrincipalId) -> TrustedScope:
        return TrustedScope(who, ConversationId(conversation_id))

    # -- errors: a fixed envelope, never echoing input or internals --------------------

    @app.exception_handler(ApiError)
    async def api_error(request: Request, error: ApiError) -> JSONResponse:
        return JSONResponse({"error": {"code": error.code, "message": error.message}}, status_code=error.status)

    def day_closed_response() -> JSONResponse:
        """character-day §3.2: a closed day session (or one closing) is only read; nothing was recorded."""
        return JSONResponse({"error": {"code": "day_closed", "message": "This day is over: talk in today's day "
                                                                        "session."}}, status_code=409)

    @app.exception_handler(DataError)
    async def data_error(request: Request, error: DataError) -> JSONResponse:
        if error.code is DataErrorCode.DAY_CLOSED:  # refused where it was written (after a wait, or a race)
            return day_closed_response()
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

    @app.exception_handler(DayClosed)
    async def day_closed(request: Request, error: DayClosed) -> JSONResponse:
        return day_closed_response()

    @app.exception_handler(ConversationStillStopping)
    async def still_stopping(request: Request, error: ConversationStillStopping) -> JSONResponse:
        return JSONResponse({"error": {"code": "still_stopping",
                                       "message": "The current work is still stopping; try again shortly."}},
                            status_code=409)

    @app.exception_handler(FilesUnavailable)
    async def files_unavailable(request: Request, error: FilesUnavailable) -> JSONResponse:
        return JSONResponse({"error": {"code": "files_unavailable",
                                       "message": "Files cannot be attached in this conversation."}}, status_code=409)

    @app.exception_handler(ScreenRefused)
    async def not_a_screen(request: Request, error: ScreenRefused) -> JSONResponse:
        return JSONResponse({"error": {"code": "not_a_screen", "message": "A screen is a PNG or JPEG image."}},
                            status_code=400)

    @app.exception_handler(StaleConnectionGeneration)
    async def stale_generation(request: Request, error: StaleConnectionGeneration) -> JSONResponse:
        return JSONResponse({"error": {"code": "stale_connection_generation",
                                       "message": "The device stream generation is no longer active."}},
                            status_code=409)

    @app.exception_handler(ConnectionGenerationRequired)
    async def generation_required(request: Request, error: ConnectionGenerationRequired) -> JSONResponse:
        return JSONResponse({"error": {"code": "connection_generation_required",
                                       "message": "X-Himari-Connection-Generation is required."}},
                            status_code=409)

    @app.exception_handler(FileNameRefused)
    async def bad_name(request: Request, error: FileNameRefused) -> JSONResponse:
        return JSONResponse({"error": {"code": "invalid_name", "message": "A file needs a name of at most 1000 bytes."}},
                            status_code=400)

    @app.exception_handler(FolderBusy)
    async def folder_busy(request: Request, error: FolderBusy) -> JSONResponse:  # the code clients know
        return JSONResponse({"error": {"code": "workspace_busy", "message": "The conversation's folder is busy; "
                                       "send again shortly."}}, status_code=409)

    @app.exception_handler(PromptRefused)
    async def prompt_refused(request: Request, error: PromptRefused) -> JSONResponse:
        return JSONResponse({"error": {"code": error.code, "message": error.message}},
                            status_code=PROMPT_REFUSAL_STATUS.get(error.code, 409))

    @app.exception_handler(ModelRefused)
    async def model_refused(request: Request, error: ModelRefused) -> JSONResponse:
        return JSONResponse({"error": {"code": error.code, "message": error.message}},
                            status_code=MODEL_REFUSAL_STATUS.get(error.code, 400))

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
        return {"authenticated": signed_in, "model": application.model_id if signed_in else None}

    def mcp_settings(application: Application) -> dict[str, Any]:
        return {"revision": application.mcp_defaults_revision,
                "sources": [{"source": s.source, "default": s.enabled_by_default,
                             "available": s.available, "tools": len(s.tool_ids)}
                            for s in application.tool_source_statuses]}

    @app.get("/api/settings/mcp")
    async def read_mcp_settings(request: Request, who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        if who != OPERATOR:
            raise ApiError(403, "not_authorized", "Only the operator can manage MCP defaults.")
        return mcp_settings(himari(request))

    @app.put("/api/settings/mcp/{source}")
    async def write_mcp_setting(source: str, body: McpDefaultBody, request: Request,
                                who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        if who != OPERATOR:
            raise ApiError(403, "not_authorized", "Only the operator can manage MCP defaults.")
        application = himari(request)
        try:
            await application.set_mcp_default(source, body.enabled, body.revision)
        except McpDefaultsConflict:
            raise ApiError(409, "changed", "MCP defaults changed elsewhere. Reload settings and try again.") from None
        except (OSError, ValueError):
            raise ApiError(503, "not_saved", "Could not confirm the save. Reload MCP settings to check.") from None
        return mcp_settings(application)

    @app.get("/api/server")
    async def read_server(request: Request, who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """What this server runs with: the models and the budgets."""
        application = himari(request)
        turn = application.turn
        return {"model": application.model_id,
                "models": [model_view(m) for m in application.models],
                # character-day §10: the model a turn falls back to (an answer's model says so); null: none
                "backup_model": application.backup_model,
                "context_budget": application.context_tokens,  # the default model's
                "trash_days": application.trash_days,  # V3-P65: then a trashed conversation is deleted for good
                "images": application.images_available,  # V3-P67 §3.1: model copies can be made here
                "documents": application.documents_status,  # V3-P70 §9
                "memory": application.memory_enabled,  # V3-P71 §9
                "auto_memory": application.auto_memory,  # V3-P85 §12: "on (…)" or "off (why)"
                "conversation_search": application.conversation_search_enabled,  # V3-P72 §9
                "streaming": application.streaming_enabled,  # V3-P73 §9
                "turn": {"max_runs": turn.max_runs}}

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

    @app.get("/api/conversations")
    async def list_conversations(request: Request, by: Caller = Depends(caller),
                                 limit: int = 50, offset: int = 0, trashed: bool = False) -> dict[str, Any]:
        """``trashed``: the trash instead, most recently trashed first (not for a device)."""
        who = by.principal
        if trashed and by.device is not None:
            raise ApiError(403, "device_scope", "A device may not see the trash.")
        if not 1 <= limit <= MAX_LIST_LIMIT or offset < 0:
            raise ApiError(400, "invalid_argument", f"limit must be 1..{MAX_LIST_LIMIT}, offset >= 0")
        application = himari(request)
        page = await application.data.list_conversations(who, limit=limit, offset=offset, trashed=trashed)
        return {"items": [trashed_view(s, application.trash_days) if trashed else summary_view(s) for s in page],
                "next_offset": offset + limit if len(page) == limit else None}  # may lead to an empty page

    @app.get("/api/conversations/activity")  # before /api/conversations/{conversation_id}, which would take it
    async def conversation_activity(request: Request, by: Caller = Depends(caller)) -> dict[str, Any]:
        """V3-P80 §3.2: which Conversations work now."""
        application, who = himari(request), by.principal
        return {"working": sorted(application.runner.shown_running(who))}  # V3-P90 §7.3: not a hidden one

    @app.post("/api/conversations", status_code=201)
    async def create_conversation(body: CreateBody, request: Request,
                                  by: Caller = Depends(caller)) -> dict[str, Any]:
        application, who = himari(request), by.principal
        initial_model: str | None = None
        if by.origin.kind == OriginKind.DEVICE:
            if application.companion_default_model is None:
                raise ApiError(503, "no_companion_model", "No companion-capable model is offered on this server.")
            initial_model = application.companion_default_model
        cid = await application.create_conversation(who, title=body.title, initial_model=initial_model)
        return summary_view(await application.data.read_conversation(scope_of(cid, who)))

    def _legacy_question(event: ConversationEvent) -> bool:
        """V3-P106: an older server's open ``ask_user`` question waits for the user's message, not Continue."""
        return isinstance(event.payload, AssistantToolCalls) and any(
            c.tool_id == LEGACY_ASK_USER for c in event.payload.calls)

    async def _detail(application: Application, scope: TrustedScope) -> dict[str, Any]:
        summary = await application.data.read_conversation(scope)  # authorizes first
        job = application.runner.shown_status(scope)  # V3-P90 §7.3: a hidden free wake is not working
        # An accepted input (or finished Tool results) with no assistant
        # output after it — e.g. the server stopped before the job ran.
        last = await application.data.read_events(scope, after_sequence=summary.last_sequence - 1, limit=1) \
            if summary.last_sequence else ()
        continuable = (not job.running and bool(last)
                       and last[-1].kind.value in ("user_message", "tool_result", "assistant_tool_calls")
                       and not _legacy_question(last[-1])
                       # character-day §5.5: a message a sleeping HIMARI met with no answer waits for none
                       and not (isinstance(last[-1].payload, UserMessage)
                                and last[-1].payload.while_asleep is AsleepAnswer.NONE)
                       # §3.2: a closed day (or one closing) is only read: nothing to continue there
                       and not (summary.day_session is not None
                                and (summary.closed_at is not None or application.day_closing(scope))))
        return {"conversation": summary_view(summary), "job": job_view(job), "continuable": continuable}

    @app.get("/api/conversations/{conversation_id}")
    async def read_conversation(conversation_id: str, request: Request,
                                by: Caller = Depends(caller)) -> dict[str, Any]:
        return await _detail(himari(request), scope_of(conversation_id, by.principal))

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

    async def diary_items(application: Application, scope: TrustedScope, events: Sequence[ConversationEvent],
                          after: int, cursor: int, full: bool) -> list[dict[str, Any]]:
        """character-day §7, §18.11: the diaries written in this day session's context that this page owns — each
        belongs after the last event written before it (its ``sequence``; 0 before any), and to the one page whose
        range from ``after`` (exclusive) to ``cursor`` holds that event: never on two pages, never on none.
        ``events``: the page's events as read (``full``: as many as asked, more may follow); ``id``: the diary's
        write, stable; character-diary §16.7: ``step`` — ``write`` (the diary, its text as it stands) or ``amend``
        (added to as the session closed)."""
        found = await application.session_diaries(scope)
        if not found or (after > 0 and not events):
            return []
        following = next((e for e in events if e.sequence > cursor), None)  # the first event of a later page
        if following is None and full:
            more = await application.data.read_events(scope, after_sequence=cursor, limit=1)
            following = more[0] if more else None
        items: list[dict[str, Any]] = []
        for write_id, at, text, step in found:
            if after > 0 and events[0].created_at > at:
                continue  # it follows an event of an earlier page
            if following is not None and following.created_at <= at:
                continue  # it follows an event of a later page
            sequence = max((e.sequence for e in events if e.created_at <= at and e.sequence <= cursor),
                           default=after)
            items.append({"id": write_id, "sequence": sequence, "kind": "diary", "created_at": _time(at),
                          "text": text, "step": step})
        return items

    @app.get("/api/conversations/{conversation_id}/events")
    async def read_events(conversation_id: str, request: Request, by: Caller = Depends(caller),
                          after: int = 0, limit: int = 200) -> dict[str, Any]:
        who = by.principal
        if after < 0 or not 1 <= limit <= MAX_EVENTS_PAGE:
            raise ApiError(400, "invalid_argument", f"after >= 0 and limit 1..{MAX_EVENTS_PAGE}")
        application, scope = himari(request), scope_of(conversation_id, who)
        events = read = await application.data.read_events(scope, after_sequence=after, limit=limit)
        # character-mind §3.3 (review 2): the page's sequence range, as read (before anything is left out)
        through = events[-1].sequence if len(events) == limit else None
        if unshown := await application.data.unshown_wakes(scope):  # V3-P90 §7.3: a free wake not shown yet
            events = tuple(e for e in events if e.sequence not in unshown)
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
        # character-mind §3.2: a device never gets a thought
        items = [event_view(e, models, files, presented, memory, device=by.device is not None) for e in events]
        full = len(read) == limit  # as many events as asked: more may follow
        lines_full = False  # as many wake lines as asked: more may follow too
        if by.device is None:  # §3.3 (review P1-5): a free wake that thought and passed: its thought, the owner's
            # review 2: only those within the page's range, merged in order
            lines = await application.data.wake_thoughts(scope, after=after, through=through, limit=limit)
            lines_full = len(lines) == limit
            items = sorted(items + [wake_thought_view(w) for w in lines], key=lambda item: int(item["sequence"]))
        # character-day review 9, second review 6: the page is the range of events it consumed, whatever is shown
        # of it — rows held to ``limit`` (cut: the range ends at the last row given), else to the last event read
        # (one not shown yet too: a page of hidden events still moves on), and on the last page past every wake
        # line too. ``more``: the range stopped at ``limit`` (rows, events or wake lines); read on from
        # ``last_sequence`` — never stop at a page with fewer rows shown.
        cut = len(items) > limit
        if cut:
            items = items[:limit]
            cursor = int(items[-1]["sequence"])
        elif full:
            cursor = read[-1].sequence
        else:
            cursor = max([after] + [e.sequence for e in read] + [int(i["sequence"]) for i in items])
        if by.device is None:
            # character-day §7, §18.11: a diary written in this day session's context, a folded line after the
            # last event before it (shown or not), on the one page whose range holds that event (beside ``limit``:
            # never cut, never given twice)
            diaries = await diary_items(application, scope, read, after, cursor, full)
            items = sorted(items + diaries, key=lambda item: (int(item["sequence"]), item["kind"] == "diary"))
        return {"items": items, "last_sequence": cursor, "more": cut or full or lines_full}

    @app.get("/api/conversations/{conversation_id}/live")
    async def read_live(conversation_id: str, request: Request,
                        by: Caller = Depends(caller)) -> StreamingResponse:
        """What the running job's model is writing, as it writes (V3-P73 §6.1)."""
        who = by.principal
        application, scope = himari(request), scope_of(conversation_id, who)
        await application.data.read_conversation(scope)  # authorizes first: another's is 404
        release = live_slots.take(who)  # checked and taken in one step (PR #83 review B1)
        if release is None:
            raise ApiError(429, "too_many_streams", "Too many live views are open; close one and try again.")
        if by.device is None:
            return _LiveResponse(live.stream(application.runner, scope, closing), release,
                                 media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})
        # V3-P83 §8.1: revoking the device ends its open live stream.
        key, ended = (who, by.device.device_id), asyncio.Event()
        device_lives.setdefault(key, set()).add(ended)

        def done() -> None:
            release()
            kept = device_lives.get(key)
            if kept is not None:
                kept.discard(ended)
                if not kept:
                    del device_lives[key]

        return _LiveResponse(live.stream(application.runner, scope, closing, ended=ended), done,
                             media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

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

    # -- speaking first (V3-P87 §11) -------------------------------------------------------

    @app.get("/api/proactive")
    async def read_proactive(request: Request, who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        status: dict[str, Any] = _plain(await himari(request).proactive_status(who))
        return status

    @app.post("/api/proactive/try", status_code=202)
    async def try_proactive(request: Request, who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """A TRY wake now (the operator's, never a device's: refused by default)."""
        try:
            conversation, status = await himari(request).try_wake(who)
        except ProactiveRefused as refused:
            raise ApiError(409, refused.code, PROACTIVE_REFUSALS.get(refused.code, "HIMARI cannot speak first now.")) \
                from None
        return {"conversation": conversation, "job_id": status.job_id, "wake_id": status.wake_id}

    @app.get("/api/instructions")
    async def read_instructions(request: Request, who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """The instructions the user wrote for every conversation (V3-P69)."""
        return instructions_view(await himari(request).instructions(who))

    @app.put("/api/instructions")
    async def write_instructions(body: InstructionsBody, request: Request,
                                 who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """Saved at once, used from each conversation's next turn."""
        return instructions_view(await save_instructions(himari(request), who, body, InstructionScope.USER))

    # --- companion personas (V3-P108 §6.1): told only while the companion is on (``companion``) ---------

    def persona_refused(error: DataError) -> ApiError:
        if error.code not in PERSONA_REFUSALS:
            raise error
        status, code, message = PERSONA_REFUSALS[error.code]
        return ApiError(status, code, message)

    async def personas_changed(application: Application, who: PrincipalId) -> PersonaList:
        """character-state §4: after any change to the personas or the choice, every device and the
        Web follow the one chosen now (its id and name, never its text); the list as it is now."""
        listed = await application.data.list_personas(who)
        chosen = next((p for p in listed.personas if p.persona_id == listed.choice.persona_id), None)
        application.presence.publish(who, "persona", {"persona_id": listed.choice.persona_id,
                                                      "name": None if chosen is None else chosen.name,
                                                      "revision": listed.choice.revision})
        return listed

    @app.get("/api/device/personas")
    async def read_device_personas(request: Request, by: Caller = Depends(caller)) -> dict[str, Any]:
        """character-state §4 (H-45's narrow exception): the app's menu — the personas' ids and names and
        the one chosen, never their text."""
        if by.device is None:
            raise ApiError(403, "not_a_device", "Only a device reads personas here.")
        return device_personas_view(await himari(request).data.list_personas(by.principal))

    @app.put("/api/device/persona-choice")
    async def choose_device_persona(body: PersonaChoiceBody, request: Request,
                                    by: Caller = Depends(caller)) -> dict[str, Any]:
        """character-state §4 (H-45's narrow exception): the app chooses a persona or none, from the
        choice's revision it read; the menu as it is after."""
        if by.device is None:
            raise ApiError(403, "not_a_device", "Only a device chooses a persona here.")
        application = himari(request)
        try:
            await application.data.choose_persona(by.principal, body.persona_id, body.revision)
        except DataError as error:
            raise persona_refused(error) from None
        return device_personas_view(await personas_changed(application, by.principal))

    @app.get("/api/personas")
    async def read_personas(request: Request, who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        application = himari(request)
        listed = await application.data.list_personas(who)
        return {"personas": [persona_view(p) for p in listed.personas], "choice": choice_view(listed.choice),
                "companion": application.companion_enabled,
                "limits": {"count": MAX_PERSONAS, "name_chars": MAX_PERSONA_NAME_CHARS,
                           "text_chars": MAX_PERSONA_CHARS},
                "fields": list(PERSONA_FIELDS)}  # character-persona-fields §2: in the order the text is made

    @app.post("/api/personas", status_code=201)
    async def add_persona(body: PersonaBody, request: Request, who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        application = himari(request)
        try:
            made = await application.data.create_persona(who, body.name, body.text, body.fields)
        except DataError as error:
            raise persona_refused(error) from None
        await personas_changed(application, who)  # character-state §4: the apps' menus follow
        return persona_view(made)

    @app.put("/api/personas/choice")  # before /{persona_id}, which would take "choice" for an id
    async def choose_persona(body: PersonaChoiceBody, request: Request,
                             who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """Used from each conversation's next turn; ``persona_id`` null: no persona."""
        application = himari(request)
        try:
            chosen = await application.data.choose_persona(who, body.persona_id, body.revision)
        except DataError as error:
            raise persona_refused(error) from None
        await personas_changed(application, who)
        return choice_view(chosen)

    @app.put("/api/personas/{persona_id}")
    async def edit_persona(persona_id: int, body: PersonaEditBody, request: Request,
                           who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        application = himari(request)
        try:
            edited = await application.data.update_persona(who, persona_id, body.name, body.text, body.revision,
                                                           body.fields)
        except DataError as error:
            raise persona_refused(error) from None
        await personas_changed(application, who)  # a new name for the menus
        return persona_view(edited)

    @app.delete("/api/personas/{persona_id}")
    async def delete_persona(persona_id: int, revision: int, request: Request,
                             who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """The choice after: none when the persona deleted was the chosen one."""
        application = himari(request)
        try:
            choice = await application.data.delete_persona(who, persona_id, revision)
        except DataError as error:
            raise persona_refused(error) from None
        await personas_changed(application, who)
        return choice_view(choice)

    # --- HIMARI's life (character-life §6): the chosen persona's, on the Web only (D4) ----------

    def life_refused(error: DataError) -> ApiError:
        if error.code is DataErrorCode.PLAN_REFUSED:  # character-day §18.11: the schedule's own sentence
            return ApiError(409, "plan_refused", error.message or "That change is past the schedule's limits.")
        if error.code not in LIFE_REFUSALS:
            raise error
        status, code, message = LIFE_REFUSALS[error.code]
        return ApiError(status, code, message)

    @app.get("/api/life")
    async def read_life(request: Request, who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """The chosen persona's facts, and its plans, diaries, work pieces and events of the last 30 days on
        (character-routine §9.1; older ones: /api/life/history)."""
        application = himari(request)
        chosen, entries = await application.life(who)
        mood, now = await application.himari_state(who)  # character-state §2, §3: as a turn would see them
        return {"persona": None if chosen is None else {"id": chosen.persona_id, "name": chosen.name},
                "enabled": application.life_enabled,
                "writer": {"running": application.life_writer is not None, "told": application.auto_life},
                # character-work §8: the worker runs with the story writer; the next work plan
                "work": {"running": application.life_writer is not None,
                         "next": None if (planned := application.next_work(entries)) is None
                         else life_entry_view(planned)},
                # character-mind §8: the model HIMARI's self runs use now, and why
                "mind": None if application.life_writer is None
                else mind_view(await application.life_writer.mind_model(who)),
                "mood": mood_view(mood), "now": now_view(now),
                # character-world §2, §3: what the writer and a turn know of the real world, as told at startup
                "world": {"weather": application.world_weather, "today": application.weather_today(),
                          "topics": application.world_topics,
                          # character-mind §8: when the world next decides what happens to HIMARI (character-day
                          # §6: after the day's closing, else noon)
                          "next": _time(await application.next_world(who))},
                "entries": [life_entry_view(e) for e in entries],
                # character-day §12, §18.11: the sleep now; character-routine §9.1: the sleeps that fell in the last
                # 30 days, as the entries (newest first)
                "sleep": sleep_view_json(await application.sleep_view(who), ZoneInfo(application.time_zone)),
                "sleeps": [sleep_record_view(s) for s in await application.sleeps(who)],
                "limits": {"facts": MAX_LIFE_FACTS, "chars": MAX_LIFE_CHARS, "diary_chars": MAX_DIARY_CHARS,
                           "arc_chars": MAX_ARC_CHARS, "arc_title_chars": MAX_ARC_TITLE_CHARS,
                           "work_chars": MAX_WORK_CHARS, "summary_chars": MAX_WORK_SUMMARY_CHARS,
                           "event_chars": MAX_EVENT_CHARS}}

    @app.get("/api/life/history")
    async def read_life_history(request: Request, who: PrincipalId = Depends(principal), before: str | None = None,
                                days: int = LIFE_HISTORY_DAYS) -> dict[str, Any]:
        """character-routine §9.1: the chosen persona's record over the ``days`` dates (the user's) before ``before``
        (left out: the first date /api/life shows) — its diaries, events, plans (cancelled and missed too), work
        pieces and the sleeps that fell then — and whether anything older is kept (``more``: read on from ``from``)."""
        if not 1 <= days <= LIFE_HISTORY_MAX_DAYS:
            raise ApiError(400, "invalid_argument", f"days is 1..{LIFE_HISTORY_MAX_DAYS}")
        end = None
        if before is not None:
            try:
                end = date.fromisoformat(before) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", before) else None
            except ValueError:
                end = None
            if end is None:
                raise ApiError(400, "invalid_argument", "before is a date, YYYY-MM-DD")
        try:
            history = await himari(request).life_history(who, end, days)
        except OverflowError:  # before the first dates there are
            raise ApiError(400, "invalid_argument", "before is a date, YYYY-MM-DD") from None
        return {"entries": [life_entry_view(e) for e in history.entries],
                "sleeps": [sleep_record_view(s) for s in history.sleeps],
                "from": history.since, "before": history.before, "more": history.more}

    def plan_write(body: PlanBody | PlanEditBody, entry_id: int | None) -> PlanWrite:
        return PlanWrite(body.day, body.time, body.minutes, PlanKind(body.plan_kind), body.text, entry_id,
                         body.arc_id if body.plan_kind == "work" else None)

    @app.post("/api/life/plans", status_code=201)
    async def add_plan(body: PlanBody, request: Request, who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """character-day §4.2, §18.11: the user's slot (origin user); 409 ``plan_refused`` past the limits."""
        try:
            return life_entry_view(await himari(request).write_plan(who, body.persona_id, plan_write(body, None)))
        except LifeChanged:
            raise ApiError(409, "persona_changed", "Another persona is in use now; look again.") from None
        except DataError as error:
            raise life_refused(error) from None

    @app.put("/api/life/plans/{entry_id}")
    async def edit_plan(entry_id: int, body: PlanEditBody, request: Request,
                        who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """§18.11: a slot changed or moved by the user (a sleep slot only moved); it becomes the user's."""
        try:
            return life_entry_view(await himari(request).edit_plan(who, plan_write(body, entry_id), body.revision))
        except DataError as error:
            raise life_refused(error) from None

    @app.post("/api/life", status_code=201)
    async def add_life_fact(body: LifeFactBody, request: Request,
                            who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        try:
            return life_entry_view(await himari(request).add_life_fact(who, body.persona_id, body.text))
        except LifeChanged:
            raise ApiError(409, "persona_changed", "Another persona is in use now; look again.") from None
        except DataError as error:
            raise life_refused(error) from None

    @app.put("/api/life/{entry_id}")
    async def edit_life(entry_id: int, body: LifeEditBody, request: Request,
                        who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """It becomes the user's: automatic writes leave it alone."""
        try:
            return life_entry_view(await himari(request).data.edit_life(who, entry_id, body.text,
                                                                        expected_revision=body.revision,
                                                                        title=body.title, summary=body.summary))
        except DataError as error:
            raise life_refused(error) from None

    @app.delete("/api/life/{entry_id}", status_code=204)
    async def retire_life(entry_id: int, revision: int, request: Request,
                          who: PrincipalId = Depends(principal)) -> None:
        """It retires: HIMARI no longer knows it."""
        try:
            await himari(request).data.retire_life(who, entry_id, expected_revision=revision)
        except DataError as error:
            raise life_refused(error) from None

    # --- the bond (character-bond §1): the chosen persona's, on the Web only (D4) --------------

    def bond_refused(error: DataError) -> ApiError:
        if error.code not in BOND_REFUSALS:
            raise error
        status, code, message = BOND_REFUSALS[error.code]
        return ApiError(status, code, message)

    @app.get("/api/bond")
    async def read_bond(request: Request, who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """The chosen persona's bond: its stage, the days together, today's anniversaries, the entries
        and the memories' timeline (the bond's entries with the newest episodes)."""
        application = himari(request)
        chosen, bond, episodes = await application.bond(who)
        return {"persona": None if chosen is None else {"id": chosen.persona_id, "name": chosen.name},
                "enabled": application.life_enabled, "stage": bond_stage_view(bond.standing),
                "together_days": bond.together_days, "since": bond.met_on, "today": [a.name for a in bond.today],
                "entries": [bond_entry_view(e) for e in bond.entries],
                "memories": memories_view(bond.entries, episodes),
                "limits": {"entries": MAX_BOND_ENTRIES, "chars": MAX_BOND_CHARS}}

    @app.post("/api/bond", status_code=201)
    async def add_bond(body: BondBody, request: Request, who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        try:
            return bond_entry_view(await himari(request).add_bond(who, body.persona_id, BondTag(body.tag), body.text,
                                                                  body.month_day))
        except LifeChanged:
            raise ApiError(409, "persona_changed", "Another persona is in use now; look again.") from None
        except DataError as error:
            raise bond_refused(error) from None

    @app.put("/api/bond/stage")  # before /api/bond/{entry_id}, which would take its path
    async def set_bond_stage(body: BondStageBody, request: Request,
                             who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """The owner's choice of stage, up or down; afterwards only what happens later moves it."""
        try:
            return bond_stage_view(await himari(request).set_bond_stage(who, body.persona_id, BondStage(body.stage),
                                                                        body.revision))
        except LifeChanged:
            raise ApiError(409, "persona_changed", "Another persona is in use now; look again.") from None
        except DataError as error:
            raise bond_refused(error) from None

    @app.put("/api/bond/{entry_id}")
    async def edit_bond(entry_id: int, body: BondEditBody, request: Request,
                        who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """It becomes the user's: memory passes leave it alone."""
        try:
            return bond_entry_view(await himari(request).data.edit_bond(
                who, entry_id, BondTag(body.tag), body.text, body.month_day, expected_revision=body.revision))
        except DataError as error:
            raise bond_refused(error) from None

    @app.delete("/api/bond/{entry_id}", status_code=204)
    async def retire_bond(entry_id: int, revision: int, request: Request,
                          who: PrincipalId = Depends(principal)) -> None:
        """It retires: HIMARI no longer knows it."""
        try:
            await himari(request).data.retire_life(who, entry_id, expected_revision=revision)
        except DataError as error:
            raise bond_refused(error) from None

    async def save_instructions(application: Application, who: PrincipalId, body: InstructionsBody,
                                scope: InstructionScope) -> Instructions:
        try:
            return await application.set_instructions(who, body.text, body.revision, scope)
        except DataError as error:
            if error.code not in INSTRUCTIONS_REFUSALS:
                raise
            status, code, message = INSTRUCTIONS_REFUSALS[error.code]
            raise ApiError(status, code, message) from None

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
                "max_chars": MAX_MEMORY_CHARS, "max_loop_chars": MAX_LOOP_CHARS, "enabled": application.memory_enabled,
                "auto": auto_memory_view(await application.memory_status(who))}

    @app.post("/api/memories", status_code=201)
    async def add_memory(body: MemoryBody, request: Request, who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """A note the user writes; used from each conversation's next turn."""
        application = himari(request)
        try:
            note = await application.add_memory(who, MemoryCategory(body.category), body.text,
                                                note_dates(body, application.time_zone, editing=False))
        except DataError as error:
            raise memory_refused(error) from None
        return memory_view(note, {})

    @app.put("/api/memories/{memory_id}")
    async def edit_memory(memory_id: int, body: MemoryEditBody, request: Request,
                          who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        application = himari(request)
        try:
            current = await application.data.read_memory(who, memory_id)
            note = await application.edit_memory(who, memory_id, MemoryCategory(body.category), body.text,
                                                 body.revision, note_dates(body, application.time_zone, editing=True,
                                                                           current=current))
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

    # --- memory passes (V3-P85 §10, §12) ------------------------------------------------

    @app.get("/api/conversations/{conversation_id}/memory-passes")
    async def read_memory_passes(conversation_id: str, request: Request,
                                 who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """What each applied memory pass did here, after the Event it read through (§10.1)."""
        lines = await himari(request).memory_pass_lines(scope_of(conversation_id, who))
        return {"passes": [pass_line_view(line) for line in lines]}

    @app.post("/api/conversations/{conversation_id}/memory-passes/{pass_id}/{item}/undo")
    async def undo_pass_item(conversation_id: str, pass_id: str, item: int, request: Request,
                             who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """Undo one thing a pass did (§10.1), from its own record. ``memory``: the
        note as it is back, or null when an added note was deleted."""
        application = himari(request)
        try:
            note = await application.undo_pass_item(scope_of(conversation_id, who), pass_id, item)
        except DataError as error:
            if error.code is DataErrorCode.NOT_FOUND:
                raise ApiError(404, "not_found", "Nothing to undo: the note is gone, it was forgotten too long ago, "
                                                 "or the conversation it came from was deleted.") from None
            raise memory_refused(error) from None
        return {"memory": None if note is None else memory_view(note, await memory_titles(application, who, (note,)))}

    async def previews(application: Application, who: PrincipalId,
                       ids: Iterable[ConversationId]) -> dict[ConversationId, str]:
        """What the page names an untitled conversation by (as a note's source, V3-P71 §7.2)."""
        found: dict[ConversationId, str] = {}
        for cid in set(ids):
            try:
                found[cid] = (await application.data.read_conversation(scope_of(cid, who))).preview
            except DataError as error:  # in the trash
                if error.code is not DataErrorCode.NOT_AUTHORIZED:
                    raise
        return found

    @app.get("/api/memories/proposals")
    async def read_proposals(request: Request, who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """Notes waiting for the user to keep or discard (§10.2)."""
        application = himari(request)
        found = await application.proposals(who)
        named = await previews(application, who, (p.conversation_id for p in found if not p.title))
        return {"proposals": [proposal_view(p, named) for p in found]}

    @app.post("/api/memories/proposals/{pass_id}/{item}")
    async def decide_proposal(pass_id: str, item: int, body: ProposalBody, request: Request,
                              who: PrincipalId = Depends(principal)) -> Response:
        application = himari(request)
        dates = None
        if {"due", "valid_until"} & body.model_fields_set:
            zone = body.time_zone if body.time_zone and body.time_zone in available_timezones() else \
                application.time_zone
            parsed = []
            for value in (body.due, body.valid_until):
                found = None if value is None or not value.strip() else parse_note_date(value, zone)
                if value is not None and value.strip() and found is None:
                    raise ApiError(422, "invalid_date", "A date is YYYY-MM-DD, or a time with its UTC offset.")
                parsed.append(found)
            due, valid = parsed
            dates = NoteDates(None if due is None else due[0], None if due is None else due[1],
                              None if valid is None else valid[0], None if valid is None else valid[1])
        try:
            note = await application.decide_proposal(who, pass_id, item, body.decision == "keep", dates)
        except DataError as error:
            if error.code is DataErrorCode.NOT_FOUND:
                raise ApiError(404, "not_found", "No such suggestion: its conversation may have been deleted.") from None
            if error.code is DataErrorCode.VERSION_CONFLICT:
                raise ApiError(409, "already_decided", "This suggestion was already kept or discarded.") from None
            raise memory_refused(error) from None
        if note is None:
            return Response(status_code=204)
        return JSONResponse(memory_view(note, await memory_titles(application, who, (note,))), status_code=201)

    @app.get("/api/episodes")
    async def read_episodes(request: Request, who: PrincipalId = Depends(principal), cursor: str | None = None,
                            limit: int = 30) -> dict[str, Any]:
        """The Timeline (§10.3), newest first; ``next`` reads on from the last shown."""
        if not 1 <= limit <= 100:
            raise ApiError(400, "invalid_argument", "limit is 1..100")
        application = himari(request)
        found = await application.episodes(who, None if cursor is None else read_episode_cursor(cursor), limit)
        named = await previews(application, who, (f.episode.conversation_id for f in found if not f.title))
        return {"episodes": [episode_view(f, named) for f in found],
                "next": episode_cursor(found[-1].episode) if len(found) == limit else None}

    @app.put("/api/episodes/{episode_id}")
    async def edit_episode(episode_id: int, body: EpisodeEditBody, request: Request,
                           who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        try:
            episode = await himari(request).edit_episode(who, episode_id, body.text, body.revision)
        except DataError as error:
            if error.code is DataErrorCode.NOT_FOUND:
                raise ApiError(404, "not_found", "This episode was deleted.") from None
            if error.code is DataErrorCode.VERSION_CONFLICT:
                raise ApiError(409, "revision_conflict", "This episode was changed elsewhere.") from None
            if error.code is DataErrorCode.TOO_LARGE:
                raise ApiError(422, "too_large", f"An episode is at most {MAX_EPISODE_CHARS} characters.") from None
            raise ApiError(422, "invalid_episode", "An episode needs some text and no control characters.") from None
        return {"id": episode.episode_id, "text": episode.text, "origin": episode.origin.value,
                "revision": episode.revision}

    @app.delete("/api/episodes/{episode_id}", status_code=204)
    async def delete_episode(episode_id: int, request: Request, revision: int,
                             who: PrincipalId = Depends(principal)) -> None:
        try:
            await himari(request).delete_episode(who, episode_id, revision)
        except DataError as error:
            if error.code is DataErrorCode.NOT_FOUND:
                raise ApiError(404, "not_found", "This episode was deleted.") from None
            if error.code is DataErrorCode.VERSION_CONFLICT:
                raise ApiError(409, "revision_conflict", "This episode was changed elsewhere.") from None
            raise

    @app.post("/api/memory-passes/backfill")
    async def backfill(body: BackfillBody, request: Request, who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """§4.9, §12: read a conversation's part from before the upgrade (notes only proposed)."""
        try:
            outcomes = await himari(request).backfill(scope_of(body.conversation_id, who))
        except MemoryPassesOff as off:
            raise ApiError(409, "memory_passes_off", f"Automatic memory is {off}.") from None
        return {"outcomes": [o.value for o in outcomes]}

    @app.post("/api/conversations/{conversation_id}/memory-passes/{pass_id}/retry")
    async def retry_pass(conversation_id: str, pass_id: str, request: Request,
                         who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """§4.9, §12: read a skipped part once more (notes only proposed)."""
        try:
            outcome = await himari(request).retry_pass(scope_of(conversation_id, who), pass_id)
        except MemoryPassesOff as off:
            raise ApiError(409, "memory_passes_off", f"Automatic memory is {off}.") from None
        return {"outcome": outcome.value}

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

    @app.post("/api/conversations/{conversation_id}/messages", status_code=202)
    async def send_message(conversation_id: str, body: MessageBody, request: Request,
                           by: Caller = Depends(caller)) -> dict[str, Any]:
        who = by.principal
        if by.device is not None and body.attachments:  # V3-P83 §4.3: text and a screen
            raise ApiError(403, "device_scope", "A device sends text and a screen only.")
        if body.screen is not None and by.device is None:
            raise ApiError(400, "invalid_argument", "Only a device sends a screen.")
        application, scope = himari(request), scope_of(conversation_id, who)
        connection_generation: str | None = None
        client_file_download: bool | None = None
        if by.device is not None:
            has_gen = GENERATION_HEADER.lower() in request.headers
            declares_client_features = "client_features" in body.model_fields_set
            if not has_gen and declares_client_features:
                raise ApiError(409, "connection_generation_required",
                               "X-Himari-Connection-Generation is required.")
            if has_gen:
                connection_generation = request.headers.get(GENERATION_HEADER, "")
                if not application.presence.valid_generation(by.principal, by.device.device_id, connection_generation):
                    raise ApiError(409, "stale_connection_generation",
                                   "The device stream generation is no longer active.")
                client_file_download = bool(body.client_features is not None and body.client_features.file_download)
        text = body.text.strip()
        if len(body.attachments) > MAX_ATTACHMENTS:
            raise ApiError(400, "too_many_attachments", f"A message carries at most {MAX_ATTACHMENTS} files.")
        if ((not text and not body.attachments and body.screen is None)
                or len(text) > MAX_MESSAGE_CHARS):
            raise ApiError(400, "invalid_argument", f"A message is 1..{MAX_MESSAGE_CHARS} characters, or files.")
        if body.screen is not None:
            try:
                return sent_view(await application.send(
                    scope, text, origin=by.origin, screen=FileId(body.screen),
                    connection_generation=connection_generation, client_file_download=client_file_download))
            except DataError as error:
                if error.code is DataErrorCode.ATTACHMENT_REFUSED and error.reason == ALREADY_SENT:
                    # §7.8: the earlier send of this screen was recorded; this one is not.
                    raise ApiError(409, "screen_already_sent", "This screen was sent already.") from None
                raise
        return sent_view(await application.send(
            scope, text, [FileId(f) for f in body.attachments], origin=by.origin,
            connection_generation=connection_generation, client_file_download=client_file_download))

    # -- files (V3-P66 §9) ---------------------------------------------------------------------

    @app.post("/api/conversations/{conversation_id}/uploads", status_code=201)
    async def upload_file(conversation_id: str, request: Request, name: str = "",
                          by: Caller = Depends(caller)) -> dict[str, Any]:
        """The body is the file. Received as it streams, held to the file
        limit (not the general body limit); refused at once when its declared
        length is over. A device uploads a screen only (V3-P84 §4.1): a PNG or
        JPEG of at most SCREEN_MAX_BYTES, named by the server."""
        application, scope = himari(request), scope_of(conversation_id, by.principal)
        limit = application.file_max_bytes if by.device is None else min(SCREEN_MAX_BYTES, application.file_max_bytes)
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > limit:
            raise ApiError(413, "file_too_large", "The file is larger than this server takes.")

        async def body() -> AsyncIterator[bytes]:
            async for chunk in request.stream():
                if chunk:
                    yield chunk

        try:
            if by.device is not None:
                record = await application.upload_screen(scope, body())
            else:
                record = await application.upload(scope, name, request.headers.get("content-type"), body())
        except DataError as error:
            if error.code is DataErrorCode.BLOB_MISSING:  # swept while it stalled (§8.2)
                raise ApiError(503, "upload_interrupted", "The upload was interrupted; upload it again.") from None
            raise
        return file_view(record)

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
    async def resume(conversation_id: str, request: Request, by: Caller = Depends(caller)) -> dict[str, Any]:
        application, scope = himari(request), scope_of(conversation_id, by.principal)
        await application.check_day_open(scope)  # character-day §3.2: 409 day_closed for a closed day
        return job_view(await application.runner.submit(scope, None))

    @app.post("/api/conversations/{conversation_id}/cancel", status_code=202)
    async def cancel(conversation_id: str, request: Request, by: Caller = Depends(caller)) -> dict[str, Any]:
        """Stop the running job now (V3-P55); nothing if idle."""
        application, scope = himari(request), scope_of(conversation_id, by.principal)
        await application.data.read_conversation(scope)
        return job_view(application.runner.cancel(scope))

    @app.post("/api/conversations/{conversation_id}/compact", status_code=202)
    async def compact(conversation_id: str, request: Request, who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """Summarize the older history now, whatever its size (no Run); a closed day is not (409 day_closed)."""
        application, scope = himari(request), scope_of(conversation_id, who)
        await application.check_day_open(scope)
        return job_view(await application.runner.submit_compaction(scope))

    # -- devices (V3-P83 §4) -------------------------------------------------------------------

    def device_refused(error: DataError) -> ApiError:
        if error.code not in DEVICE_REFUSALS:
            raise error
        status, code, message = DEVICE_REFUSALS[error.code]
        return ApiError(status, code, message)

    def companion_on(application: Application) -> None:
        if not application.companion_enabled:
            raise ApiError(403, "companion_off", "Devices are turned off on this server.")

    @app.get("/api/devices")
    async def list_devices(request: Request, who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """The Web only (a device is refused): each device with what the server knows of it now (V3-P88 §8.1)."""
        application = himari(request)
        hub = application.presence
        epoch, revision = hub.signals_epoch, hub.signals_revision(who)  # before the states: a later notice rereads
        states, current = hub.states(who), hub.current(who)
        quiet = await application.devices.quiet(who)
        return {"items": [{**device_view(d, d.device_id in states and states[d.device_id].connected,
                                         d.device_id == current), "signals": signals_view(states.get(d.device_id))}
                          for d in await application.devices.list(who)],
                "enabled": application.companion_enabled, "signals_epoch": epoch, "signals_revision": revision,
                "quiet_until": _time(quiet)}

    @app.put("/api/presence/quiet")
    async def set_quiet(body: QuietBody, request: Request, by: Caller = Depends(caller)) -> dict[str, Any]:
        """V3-P88 §6.2: the user's "quiet", for every device and the Web; a device may call it."""
        until = None
        if body.until is not None:
            try:
                until = datetime.fromisoformat(body.until)
            except ValueError:
                until = None
            if until is None or until.utcoffset() is None:
                raise ApiError(400, "invalid_time", "Give a time with its UTC offset, or null.")
        application = himari(request)
        try:
            kept = await application.devices.set_quiet(by.principal, until)
        except QuietTooLong:
            raise ApiError(400, "quiet_too_long", "Quiet lasts at most seven days.") from None
        application.presence.publish(by.principal, "quiet", {"until": _time(kept)})
        return {"until": _time(kept)}

    @app.post("/api/devices", status_code=201)
    async def add_device(body: DeviceNameBody, request: Request, who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """A new device and its pairing code, shown once (§4.1)."""
        application = himari(request)
        companion_on(application)
        try:
            made = await application.devices.create(who, body.name)
        except DataError as error:
            raise device_refused(error) from None
        return {**device_view(made.device), "code": made.code, "expires_at": _time(made.expires_at)}

    @app.patch("/api/devices/{device_id}")
    async def rename_device(device_id: str, body: DeviceNameBody, request: Request,
                            who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        try:
            return device_view(await himari(request).devices.rename(who, device_id, body.name))
        except DataError as error:
            raise device_refused(error) from None

    @app.delete("/api/devices/{device_id}", status_code=204)
    async def revoke_device(device_id: str, request: Request, who: PrincipalId = Depends(principal)) -> None:
        application = himari(request)
        try:
            await application.devices.revoke(who, device_id)
        except DataError as error:
            raise device_refused(error) from None
        application.presence.revoked(who, device_id)  # its event streams end with "revoked" (§8.1)
        for ended in device_lives.get((who, device_id), ()):
            ended.set()  # and its live streams end

    @app.post(PAIR_PATH)
    async def pair_device(request: Request) -> dict[str, Any]:
        """The one route without a sign-in (§4.3): the code in ``Authorization:
        Pairing <code>`` is its permission, and the device's own token is
        registered by its hash. Any body but ``{"token": "<string>"}`` is
        ``invalid_token`` (appendix A.1), not a generic validation error."""
        application = himari(request)
        companion_on(application)
        token = pairing_token(await request.body())
        if token is None:
            raise ApiError(400, "invalid_token", PAIRING_MESSAGES["invalid_token"])
        scheme, _, code = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "pairing" or not code.strip():
            raise ApiError(401, "pairing_invalid", PAIRING_MESSAGES["pairing_invalid"])
        try:
            paired = await application.devices.pair(code.strip(), token)
        except PairingRefused as refused:
            raise ApiError(PAIRING_STATUS[refused.code], refused.code, PAIRING_MESSAGES[refused.code]) from None
        return {"device_id": paired.device.device_id, "name": paired.device.name}

    @app.get("/api/device")
    async def read_device(request: Request, by: Caller = Depends(caller)) -> dict[str, Any]:
        """What a device needs to start (appendix A.2); ``device`` is null for the Web."""
        return {**await device_state(himari(request), by), "limits": {"message_chars": MAX_MESSAGE_CHARS}}

    async def device_state(application: Application, by: Caller) -> dict[str, Any]:
        """What ``GET /api/device`` and ``hello`` both say (appendix A.2, A.3). character-day §11, §18.11: with
        HIMARI's day, the recent conversation is today's day session (opened when none is) and HIMARI's sleep."""
        if application.keeper is not None:
            recent = await application.day_session(by.principal)
        else:
            recent = (await application.data.most_recent_user_conversation(by.principal) if by.device is None
                      else await application.recent_companion_conversation(by.principal))
        quiet = await application.devices.quiet(by.principal)
        features = {"presence_signals": 1, "sleep_signals": 1, "task_signals": 1, "free_wake_screen": 1,
                    "screen_look": 1, "file_download": 1, "connection_generation": 1,
                    "persona_choice": 1}  # character-state §4: the app's persona menu
        state: dict[str, Any] = {}
        if application.keeper is not None:
            features["day_session"] = 1
            sleep = await application.sleep_view(by.principal)
            state["himari"] = {"sleep": None if sleep is None else sleep.state.value}
        return {"device": None if by.device is None else {"device_id": by.device.device_id, "name": by.device.name},
                "moods": list(MOODS), "current_device": application.presence.current(by.principal),
                "recent_conversation": None if recent is None else {"id": recent.conversation_id,
                                                                    "title": recent.title},
                # V3-P88 §14, V3-P90 §3.1: an app sends the new fields only then
                "features": features, **state,
                "quiet_until": _time(quiet)}

    @app.post("/api/device/poke")
    async def device_poke(request: Request, by: Caller = Depends(caller)) -> dict[str, Any]:
        """character-day §5.5, §11: the app's sleeping HIMARI clicked — counted as a message; a mumble or a waking
        opens a turn on this device. ``answer`` null: it was awake (or its day is off)."""
        if by.device is None:
            raise ApiError(403, "not_a_device", "Only a device pokes its character.")
        answer = await himari(request).poke(by.principal, by.device.device_id)
        return {"answer": None if answer is None else answer.value}

    @app.post("/api/device/presence", status_code=204)
    async def device_presence(body: PresenceBody, request: Request, by: Caller = Depends(caller)) -> None:
        """A device's input activity (§5)."""
        if by.device is None:
            raise ApiError(403, "not_a_device", "Only a device sends its activity.")
        has_gen = GENERATION_HEADER.lower() in request.headers
        declares_file_download = body.features is not None and "file_download" in body.features.model_fields_set
        if not has_gen and declares_file_download:
            raise ApiError(409, "connection_generation_required", "X-Himari-Connection-Generation is required.")
        gen = request.headers.get(GENERATION_HEADER, "") if has_gen else None
        file_download = (body.features.file_download if body.features is not None else False) if has_gen else None
        if not himari(request).presence.signal(by.principal, by.device.device_id, body.idle_seconds, body.time_zone,
                                          presence_signals(body), connection_generation=gen,
                                          file_download=file_download):
            raise ApiError(409, "stale_connection_generation", "The device stream generation is no longer active.")

    @app.get("/api/device/conversations/{conversation_id}/presented-files")
    async def list_device_presented_files(conversation_id: str, request: Request, by: Caller = Depends(caller),
                                          limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """V3-P93 §6.2: a Conversation's publicly presented files, for a paired device."""
        if by.device is None:
            raise ApiError(403, "not_a_device", "Only a device reads presented files here.")
        if not 1 <= limit <= MAX_LIST_LIMIT or offset < 0:
            raise ApiError(400, "invalid_argument", f"limit must be 1..{MAX_LIST_LIMIT}, offset >= 0")
        page = await himari(request).presented_files(scope_of(conversation_id, by.principal), limit=limit, offset=offset)
        return {"items": [presented_file_view(f) for f in page],
                "next_offset": offset + limit if len(page) == limit else None}

    @app.get("/api/device/conversations/{conversation_id}/presented-files/{file_id}/{version}")
    async def download_device_presented_file(conversation_id: str, file_id: str, request: Request,
                                             version: int = Path(ge=1, le=2**31),
                                             by: Caller = Depends(caller)) -> Response:
        """V3-P93 §6.2: a publicly presented file's bytes, for a paired device."""
        if by.device is None:
            raise ApiError(403, "not_a_device", "Only a device downloads presented files here.")
        record, handle = await himari(request).download_presented_file(
            scope_of(conversation_id, by.principal), FileId(file_id), version)
        return _FileStream(handle, media_type="application/octet-stream", headers={
            "Content-Disposition": content_disposition(record.name),
            "Content-Length": str(record.size),
            "Content-Security-Policy": "sandbox",
        })

    def screen_answer(answer: Answer) -> None:
        if answer is not Answer.TAKEN:
            status, code, message = SCREEN_ANSWERS[answer]
            raise ApiError(status, code, message)

    @app.post("/api/device/screens/{request_id}", status_code=204)
    async def device_screen(request_id: str, request: Request, by: Caller = Depends(caller)) -> None:
        """V3-P91 §4.2: the screen a request asked for — a PNG or JPEG of at most SCREEN_MAX_BYTES, from
        the device it was asked of. 404 for a request it does not know, 410 for one that just ended."""
        application = himari(request)
        if by.device is None:
            raise ApiError(403, "not_a_device", "Only a device sends its screen.")
        if application.screens is None:
            raise ApiError(404, "not_found", "No such screen request for this device.")
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > SCREEN_MAX_BYTES:
            raise ApiError(413, "file_too_large", "The file is larger than this server takes.")
        image = bytearray()
        async for chunk in request.stream():
            image += chunk
            if len(image) > SCREEN_MAX_BYTES:
                raise ApiError(413, "file_too_large", "The file is larger than this server takes.")
        sniffed = sniff_image(bytes(image[:IMAGE_HEAD_BYTES]))
        if sniffed is None or sniffed.media_type not in SCREEN_TYPES_TAKEN:
            raise ApiError(400, "not_a_screen", "A screen is a PNG or JPEG image.")
        screen_answer(application.screens.deliver(by.principal, by.device.device_id, request_id, bytes(image)))

    @app.post("/api/device/screens/{request_id}/refuse", status_code=204)
    async def device_screen_refuse(request_id: str, body: ScreenRefuseBody, request: Request,
                                   by: Caller = Depends(caller)) -> None:
        """V3-P91 §4.2: the app sends no screen, and why."""
        application = himari(request)
        if by.device is None:
            raise ApiError(403, "not_a_device", "Only a device answers a screen request.")
        if application.screens is None:
            raise ApiError(404, "not_found", "No such screen request for this device.")
        screen_answer(application.screens.refuse(by.principal, by.device.device_id, request_id, body.reason))

    @app.get("/api/events")
    async def read_events_stream(request: Request, by: Caller = Depends(caller)) -> StreamingResponse:
        """The principal's event stream (§8.1, appendix A.3), for the devices and the Web."""
        application = himari(request)
        try:
            subscription = application.presence.subscribe(by.principal, None if by.device is None else by.device.device_id)
        except TooManyStreams:
            raise ApiError(429, "too_many_streams", "Too many event streams are open; close one and try again.") \
                from None
        try:
            assert application.events is not None
            hello = {**await device_state(application, by), "activity": application.events.activity(by.principal)}
            if by.device is None:  # V3-P88 §8.1: the Web compares, and rereads the devices when it differs
                hello["signals"] = {"epoch": application.presence.signals_epoch,
                                    "revision": application.presence.signals_revision(by.principal)}
            elif subscription.connection_generation is not None:  # V3-P93 §6.1.1: this device stream's generation only
                hello["connection_generation"] = subscription.connection_generation
            # character-state §2: a device's character starts with HIMARI's mood now ("neutral": none)
            mood = None if by.device is None else await application.current_mood(by.principal)
        except BaseException:
            application.presence.unsubscribe(subscription)
            raise
        after = () if mood is None else (("mood", {"mood": mood}),)
        return _LiveResponse(event_stream.stream(subscription, hello, closing, after=after),
                             lambda: application.presence.unsubscribe(subscription),
                             media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    # -- the new page beside the old one (V3-P102 §7.2, W1) -------------------------------------

    # -- the page (V3-P102 §7.2; at / since W6) ---------------------------------------------------

    built = next_page.load()

    def page_built() -> next_page.Built:
        if built is None:
            raise ApiError(404, "not_found", "Not available.")
        return built

    @app.get("/next", include_in_schema=False)
    @app.get("/next/{rest:path}", include_in_schema=False)
    async def next_moved(request: Request, rest: str = "") -> Response:
        """The page grew at /next/ (W1–W6c) and is at / now: an old link or bookmark goes to the same place. Only a
        path of this site: a leading slash or backslash, which would make it another site's, is dropped."""
        query = request.url.query
        return Response(status_code=308,
                        headers={"Location": "/" + rest.lstrip("/\\") + (f"?{query}" if query else "")})

    @app.get("/assets/{name:path}", include_in_schema=False)
    async def asset(name: str) -> Response:
        """The build's files (hashed names, and KaTeX as the renderer loads it): only the names that were built, never
        a path into the package."""
        found = page_built().assets.get(name)
        if found is None:
            raise ApiError(404, "not_found", "Not available.")
        item, media = found
        charset = "; charset=utf-8" if media.startswith("text/") else ""
        return Response(item.read_bytes(), media_type=f"{media}{charset}",
                        headers={"Cache-Control": next_page.cache_of(name)})

    @app.get("/", include_in_schema=False)
    @app.get("/{rest:path}", include_in_schema=False)
    async def page(request: Request, rest: str = "") -> Response:
        """The page for any address that is not the API or a file: it reads the address itself (SPA fallback).
        Registered last, so every route of the server comes first."""
        if rest == "api" or rest.startswith(("api/", "assets/")):
            raise ApiError(404, "not_found", "Not available.")
        if built is None:  # installed without `npm run build` in web/: say so instead of a bare 404
            return Response(next_page.NOT_BUILT, status_code=503, media_type="text/html; charset=utf-8",
                            headers={"Cache-Control": "no-store"})
        html, nonce = built.page()
        setattr(request.state, STYLE_NONCE_STATE, nonce)  # the security middleware writes the CSP with it
        return Response(html, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-store"})

    # Outer → inner: headers, Host, Origin, body size, routes.
    app.add_middleware(BodyLimitMiddleware, exempt=lambda method, path: method == "POST" and bool(UPLOAD_PATH.match(path)),
                       larger=lambda method, path: PAIR_BODY_BYTES if method == "POST" and path == PAIR_PATH
                       else SCREEN_MAX_BYTES if method == "POST" and SCREEN_PATH.match(path) else None)
    app.add_middleware(OriginGuardMiddleware, allowed_origins=config.allowed_origins)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=sorted(config.allowed_hosts))
    app.add_middleware(SecurityHeadersMiddleware, csp=page_csp())
    return app
