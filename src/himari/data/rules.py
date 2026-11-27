"""Small value rules shared by every Data backend (one behaviour, two stores)."""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence

from himari.common import ContentBlock, ConversationId, FrozenMap, TextBlock, split_marks, tool_id_problem
from himari.data.api import (
    DEFAULT_PLAN_MINUTES,
    DIARY_EARLIEST,
    MAX_DIARY_MINUTES,
    MIN_DIARY_MINUTES,
    diary_night,
    MAX_HIMARI_PLAN_WRITES,
    MAX_PLAN_MINUTES,
    MAX_PLAN_REASON_CHARS,
    MAX_SLEEP_MINUTES,
    MIN_PLAN_MINUTES,
    MIN_SLEEP_MINUTES,
    NIGHT_ENDS,
    NIGHT_STARTS,
    PROJECT_OPS,
    SCHEDULE_DAYS,
    SLEEP_ENDS_BY,
    SLEEP_ENDS_FROM,
    PlanKind,
    PlanOp,
    PlanWrite,
    ProjectWrite,
    SleepRecord,
    night_of,
    plan_span,
    PERSONA_FIELDS,
    PERSONA_HIDDEN_FIELDS,
    MAX_ARC_CHARS,
    MAX_ARC_TITLE_CHARS,
    MAX_OPEN_ARCS,
    MAX_SO_FAR_CHARS,
    MAX_WORK_CHARS,
    MAX_WORK_SUMMARY_CHARS,
    MAX_THOUGHT_CHARS,
    MAX_THOUGHTS,
    MAX_EVENT_CHARS,
    WorldEvent,
    WorkDraft,
    THOUGHT_DAYS,
    INTENT_DAYS,
    NEW_ARC,
    ThoughtAbout,
    LifeJob,
    LifeWriteStart,
    WORK_JOBS,
    ThoughtChange,
    ThoughtOp,
    WorkPiece,
    ArcMove,
    ArcOp,
    BOND_CLOSE_DAYS,
    BOND_FAMILIAR_DAYS,
    MAX_BOND_CHARS,
    MAX_BOND_ENTRIES,
    MAX_DIARY_CHARS,
    MAX_EPISODE_CHARS,
    MAX_LIFE_CHARS,
    MAX_LIFE_FACTS,
    MAX_LOOP_CHARS,
    MAX_MOOD_REASON_CHARS,
    MOOD_NAMES,
    HimariMood,
    MoodSource,
    BondChange,
    BondStage,
    BondStanding,
    BondTag,
    together_days,
    LifeChange,
    LifeChangeOp,
    LifeEntry,
    LifeKind,
    LifeOrigin,
    LifePlan,
    MAX_MEMORIES,
    MAX_DEVICE_NAME_CHARS,
    MAX_EFFORT_CHARS,
    MAX_EXTERNAL_SETTINGS,
    MAX_INSTRUCTIONS_CHARS,
    MAX_PERSONA_CHARS,
    MAX_PERSONA_NAME_CHARS,
    MAX_MEMORY_CHARS,
    MAX_SEARCH_WORD_CHARS,
    MAX_SEARCH_WORDS,
    MAX_LIST_LIMIT,
    MAX_MODEL_ID_CHARS,
    MAX_TITLE_CHARS,
    PREVIEW_CHARS,
    RESERVED_DEVICE_NAMES,
    AssistantFinal,
    DataError,
    DataErrorCode,
    EventText,
    InstructionScope,
    Memory,
    MemoryCategory,
    MemoryChange,
    MemoryChangeKind,
    MessageOrigin,
    InterfaceSnapshot,
    interface_kind,
    ModelSetting,
    OriginKind,
    UserMessage,
    Episode,
    EpisodePlan,
    MemoryOrigin,
    PassAction,
    PassItem,
    PassKind,
    NoteDates,
    STALE_DUE,
    MAX_WAKE_REASONS,
    WakeKind,
    WakeReason,
    PENDING_REMINDERS,
)


def normalize_title(title: str | None) -> str | None:
    if title is None:
        return None
    if not isinstance(title, str):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "title must be a string")
    title = " ".join(title.split())  # one line, no control whitespace
    if len(title) > MAX_TITLE_CHARS:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, f"title longer than {MAX_TITLE_CHARS} characters")
    return title or None


def check_page(limit: int, offset: int) -> None:
    if not 1 <= limit <= MAX_LIST_LIMIT or offset < 0:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, f"limit must be 1..{MAX_LIST_LIMIT} and offset >= 0")


def check_environments(kinds: Sequence[str]) -> None:
    if len(set(kinds)) != len(kinds):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "each Environment kind once")


def check_external_setting(source: object, enabled: object, count_after: int) -> None:
    """A source is one Tool id segment: the namespace of its Tools' ids."""
    if not isinstance(source, str) or "/" in source or tool_id_problem(source) is not None:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "an external source is one Tool id segment")
    if enabled is not None and not isinstance(enabled, bool):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "enabled must be true, false or null")
    if count_after > MAX_EXTERNAL_SETTINGS:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, f"at most {MAX_EXTERNAL_SETTINGS} external source settings")


def _visible_ascii(value: object, limit: int) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= limit and all("!" <= c <= "~" for c in value)


def check_model_setting(setting: object) -> None:
    """Shape only: whether the model is offered is Control's to decide."""
    if setting is None:
        return
    if not isinstance(setting, ModelSetting) or not _visible_ascii(setting.model_id, MAX_MODEL_ID_CHARS):
        raise DataError(DataErrorCode.INVALID_ARGUMENT,
                        f"a model id is 1..{MAX_MODEL_ID_CHARS} visible ASCII characters")
    effort = setting.reasoning_effort
    if effort is not None and not _visible_ascii(effort, MAX_EFFORT_CHARS):
        raise DataError(DataErrorCode.INVALID_ARGUMENT,
                        f"a reasoning effort is 1..{MAX_EFFORT_CHARS} visible ASCII characters")


def check_instruction_scope(scope: object) -> None:
    if not isinstance(scope, InstructionScope):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "only the user's own instructions are offered")


def _long_text(text: object, noun: str, limit: int) -> str:
    """V3-P69 §6: newlines become \n, no control character but newline and tab,
    only whitespace is no text, at most ``limit`` code points."""
    if not isinstance(text, str):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, f"{noun} must be text")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Before the blank check: str.strip() would take some control characters as blanks. A lone
    # surrogate (Cs) is not text either, and SQLite could not store it.
    if any(unicodedata.category(c) in ("Cc", "Cs") and c not in "\n\t" for c in text):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, f"{noun} may not contain control characters")
    if not text.strip():
        return ""
    if len(text) > limit:
        raise DataError(DataErrorCode.TOO_LARGE, f"{noun} may be at most {limit} characters")
    return text


def check_instructions(scope: object, text: object) -> str:
    """V3-P69 §6: the text as stored, or why not."""
    check_instruction_scope(scope)
    return _long_text(text, "instructions", MAX_INSTRUCTIONS_CHARS)


def check_persona_text(text: object) -> str:
    """V3-P108 §4.1: a persona's text, checked as instructions are."""
    return _long_text(text, "a persona's text", MAX_PERSONA_CHARS)


# character-persona-fields §3: how a field is named in the text the model is told (as the old starter named it).
PERSONA_LABELS = {
    "name": "이름", "relation": "나와의 관계", "world": "어디서 왔고 지금 어디에 있는지", "calls_user": "나를 부르는 말",
    "personality": "성격", "speech": "말투", "examples": "대사 예시", "habits": "습관",
    "likes": "좋아하는 것 / 싫어하는 것", "makes": "하는 일과 만드는 것", "never": "하지 않는 것",
}
_HIDDEN_MARK = re.compile(r"^\[(familiar|close)\][ \t]?", re.IGNORECASE)
# A value that starts as a hidden line does (prompt/api.py HIDDEN_LINE): put after "label: " its mark would be told
# from the first stage on, so it goes under its label instead (review).
_MARKED_START = re.compile(r"(?:[-*•][ \t]*)?\[(familiar|close)\]", re.IGNORECASE)


def persona_text(fields: Mapping[str, str]) -> str:
    """§3: the text the model is told, made from checked fields in PERSONA_FIELDS' order: ``label: value`` (a
    value of several lines under its label), each line of a hidden story marked ``[familiar]`` or ``[close]``
    (so persona_for_stage hides it until that stage), and ``free`` last as written. The same fields always
    make the same text; only ``free`` makes the text ``free`` itself."""
    parts: list[str] = []
    for key in PERSONA_FIELDS:
        value = fields.get(key, "")
        if not value:
            continue
        if key == "free":
            parts.append(value)
        elif key in PERSONA_HIDDEN_FIELDS:
            parts.append("\n".join(f"[{key}] " + _HIDDEN_MARK.sub("", line.strip())
                                   for line in value.split("\n") if line.strip()))
        elif "\n" in value or _MARKED_START.match(value):  # a marked line keeps its mark at the line's start
            parts.append(f"{PERSONA_LABELS[key]}:\n{value}")
        else:
            parts.append(f"{PERSONA_LABELS[key]}: {value}")
    return "\n".join(parts)


def check_persona_fields(text: object, fields: object) -> tuple[FrozenMap[str, str], str]:
    """§3: the fields as stored (filled ones only, in PERSONA_FIELDS' order) and the text made from them, or
    why not. ``fields`` None saves ``text`` as the field ``free`` (an older client, and the upgrade)."""
    if fields is None:
        free = check_persona_text(text)
        return FrozenMap({"free": free} if free else {}), free
    if not isinstance(fields, Mapping) or not all(isinstance(k, str) for k in fields):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a persona's fields must be an object of texts")
    unknown = sorted(set(fields) - set(PERSONA_FIELDS))
    if unknown:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, f"a persona has no field {unknown[0]!r}")
    kept: dict[str, str] = {}
    for key in PERSONA_FIELDS:
        value = _long_text(fields.get(key, ""), f"a persona's {key}", MAX_PERSONA_CHARS)
        if key != "free":
            value = value.strip()  # free is told as written (§3: an upgraded persona's text stays the same)
        if value:
            kept[key] = value
    made = persona_text(kept)
    if len(made) > MAX_PERSONA_CHARS:
        raise DataError(DataErrorCode.TOO_LARGE, f"a persona's text may be at most {MAX_PERSONA_CHARS} characters")
    return FrozenMap(kept), made


def persona_fields_json(fields: Mapping[str, str]) -> str:
    """How a store keeps the fields: a JSON object in PERSONA_FIELDS' order."""
    return json.dumps({k: fields[k] for k in PERSONA_FIELDS if k in fields}, ensure_ascii=False)


def persona_fields_from_json(raw: str | None) -> FrozenMap[str, str]:
    """The stored fields back (a row from before v27's column filled: none)."""
    loaded = json.loads(raw) if raw else {}
    return FrozenMap({k: loaded[k] for k in PERSONA_FIELDS if isinstance(loaded.get(k), str) and loaded[k]})


def check_persona_name(name: object) -> tuple[str, str]:
    """V3-P108 §4.1: the name as stored and the key that keeps names apart
    (trimmed, NFC, case-folded)."""
    if not isinstance(name, str):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a persona's name must be text")
    name = unicodedata.normalize("NFC", name.strip())
    if any(unicodedata.category(c) in ("Cc", "Cs") for c in name):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a persona's name may not contain control characters")
    if not 1 <= len(name) <= MAX_PERSONA_NAME_CHARS:
        raise DataError(DataErrorCode.INVALID_ARGUMENT,
                        f"a persona's name is 1..{MAX_PERSONA_NAME_CHARS} characters")
    return name, name.casefold()


def check_persona_id(persona_id: object) -> int:
    """An id is a positive int SQLite can hold; anything else is no persona."""
    if isinstance(persona_id, bool) or not isinstance(persona_id, int) or not 1 <= persona_id < 1 << 63:
        raise DataError(DataErrorCode.NOT_FOUND, f"no persona {persona_id}")
    return persona_id


def check_memory_text(text: object) -> str:
    """V3-P71 §4.2: a note's text as stored, or why not."""
    if not isinstance(text, str):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a note is text")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if any(unicodedata.category(c) in ("Cc", "Cs") and c not in "\n\t" for c in text):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a note may not contain control characters")
    text = text.strip()
    if not text:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a note needs some text")
    if len(text) > MAX_MEMORY_CHARS:
        raise DataError(DataErrorCode.TOO_LARGE, f"a note is at most {MAX_MEMORY_CHARS} characters")
    return text


def check_device_name(name: object) -> str:
    """V3-P83 §4.2: a device's name as stored, or why not. One line, so the
    model and the Web show it as given."""
    if not isinstance(name, str):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a device name is text")
    name = unicodedata.normalize("NFC", name).strip()
    if any(unicodedata.category(c) in ("Cc", "Cs", "Zl", "Zp") for c in name):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a device name may not contain control characters")
    if not name:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a device needs a name")
    if len(name) > MAX_DEVICE_NAME_CHARS:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, f"a device name is at most {MAX_DEVICE_NAME_CHARS} characters")
    if name.casefold() in RESERVED_DEVICE_NAMES:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, f"{name!r} is reserved")
    return name


def check_device_name_free(name: str, others: Iterable[str]) -> None:
    """Names are unique among a principal's devices that are not revoked, case ignored."""
    if any(other.casefold() == name.casefold() for other in others):
        raise DataError(DataErrorCode.DUPLICATE_IDENTITY, "another device has that name")


def check_interface(snapshot: InterfaceSnapshot | None, origin: MessageOrigin | None) -> None:
    if snapshot is not None and (not isinstance(snapshot, InterfaceSnapshot) or snapshot.kind is not interface_kind(origin)):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "interface does not match the input origin")


def check_origin(origin: object) -> None:
    """V3-P83 §4.2: a device origin names its device; the others name none."""
    if origin is None:
        return
    if not isinstance(origin, MessageOrigin) or not isinstance(origin.kind, OriginKind):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "not a message origin")
    if origin.kind is OriginKind.WAKE:  # V3-P87 §4.1: a job's origin, never a message's
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a message does not come from a wake")
    if (origin.kind is OriginKind.DEVICE) != isinstance(origin.device_id, str) or origin.device_id == "":
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a device origin names its device, and only it")


def check_wake(reasons: object, device_id: object, keys: object = (), reminders: object = ()) -> None:
    """V3-P87 §3.2: one to MAX_WAKE_REASONS reasons, and the device it is spoken on;
    distinct non-empty keys; the reminders it claims are exactly the notes of its
    REMINDER reasons, each once (PR #153 review P2-2: both backends refuse alike)."""
    if not isinstance(reasons, (list, tuple)) or not 1 <= len(reasons) <= MAX_WAKE_REASONS \
            or not all(isinstance(r, WakeReason) and isinstance(r.kind, WakeKind) for r in reasons):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, f"a wake has 1..{MAX_WAKE_REASONS} reasons")
    if not isinstance(device_id, str) or not device_id:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a wake names the device it is spoken on")
    if not isinstance(keys, (list, tuple)) or not all(isinstance(k, str) and k for k in keys) \
            or len(set(keys)) != len(keys):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a wake's keys are distinct, non-empty strings")
    noted = [r.memory_id for r in reasons if r.kind is WakeKind.REMINDER]
    if not isinstance(reminders, (list, tuple)) \
            or not all(isinstance(m, int) and not isinstance(m, bool) for m in reminders) \
            or None in noted or len(set(noted)) != len(noted) or sorted(reminders) != sorted(noted):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a wake claims exactly the notes of its reminder reasons")


def check_memory_category(category: object) -> MemoryCategory:
    if not isinstance(category, MemoryCategory):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "not a memory category")
    return category


def check_memory_change(change: object) -> MemoryChange:
    """The shape of a Tool's change (ToolCompletion.memory_change); the text
    as stored. Whether the note exists is the store's to say."""
    if (not isinstance(change, MemoryChange) or not isinstance(change.kind, MemoryChangeKind)
            or not isinstance(change.memory_id, int) or change.memory_id < 1):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "not a memory change")
    if change.kind is MemoryChangeKind.FORGET:
        if change.category is not None or change.text is not None or change.expected_revision is None:
            raise DataError(DataErrorCode.INVALID_ARGUMENT, "forgetting names a note and its revision only")
        return change
    category = check_memory_category(change.category)
    loop = category is MemoryCategory.LOOP
    if (change.due_at is None) != (change.due_instant is None) or \
            (change.valid_until is None) != (change.valid_instant is None) or \
            (change.remind_at is None) != (change.remind_instant is None):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a date comes with its instant")
    if (not loop and (change.due_at is not None or change.remind_at is not None)) or \
            (loop and change.valid_until is not None):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a loop has a due date and a reminder, a fact an end")
    return MemoryChange(MemoryChangeKind.SAVE, change.memory_id, category, check_note_text(category, change.text),
                        change.expected_revision, change.due_at, change.due_instant, change.valid_until,
                        change.valid_instant, change.remind_at, change.remind_instant)


_CATEGORY_ORDER = {category: index for index, category in enumerate(MemoryCategory)}


def order_memories(memories: Iterable[Memory]) -> tuple[Memory, ...]:
    """By category, newest first within each (V3-P71 §4.2)."""
    return tuple(sorted(memories, key=lambda m: (_CATEGORY_ORDER[m.category], -m.updated_at.timestamp(),
                                                 -m.memory_id)))


def fold(text: str) -> str:
    """V3-P72 §5.1 (as V3-P71 §6.3): how searched text and words are compared."""
    return unicodedata.normalize("NFKC", text).casefold()


def event_text(payload: object) -> str | None:
    """V3-P72 §3.3: the searchable text of an Event's payload — a user
    message's or final answer's text blocks, joined by newlines — or None.
    An answer's mood marks are not part of it (V3-P83 §7.2)."""
    if not isinstance(payload, (UserMessage, AssistantFinal)):
        return None
    text = "\n".join(b.text for b in payload.blocks if isinstance(b, TextBlock))
    return split_marks(text, complete=True)[0] if isinstance(payload, AssistantFinal) else text


def search_words(words: object) -> tuple[str, ...]:
    """The folded words of a search, each once (V3-P72 §5.2), or INVALID_ARGUMENT."""
    if not isinstance(words, (list, tuple)) or not all(isinstance(w, str) for w in words):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "words are text")
    folded = tuple(dict.fromkeys(fold(w) for w in words if w.strip()))
    if not 1 <= len(folded) <= MAX_SEARCH_WORDS or any(len(w) > MAX_SEARCH_WORD_CHARS for w in folded):
        raise DataError(DataErrorCode.INVALID_ARGUMENT,
                        f"1 to {MAX_SEARCH_WORDS} words of at most {MAX_SEARCH_WORD_CHARS} characters")
    return folded


def matched(folded: str, words: Sequence[str]) -> int:
    return sum(1 for w in words if w in folded)


def pick_hits(found: Iterable[EventText], per_conversation: int, limit: int) -> tuple[tuple[EventText, ...], int]:
    """V3-P72 §5.2: most words first, then newest; at most ``per_conversation``
    of one Conversation and ``limit`` in all. Also how many matched."""
    ordered = sorted(found, key=lambda e: (e.matched, e.created_at, e.sequence), reverse=True)
    taken: list[EventText] = []
    per: dict[str, int] = {}
    for hit in ordered:
        if len(taken) < limit and per.get(hit.conversation_id, 0) < per_conversation:
            taken.append(hit)
            per[hit.conversation_id] = per.get(hit.conversation_id, 0) + 1
    return tuple(taken), len(ordered)


def check_read(conversation: str, from_sequence: object, current: str, current_through: int | None) -> int:
    """V3-P72 §4.4: the last sequence a read of ``conversation`` may reach (None: no limit → -1)."""
    if not isinstance(from_sequence, int) or from_sequence < 1:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "from_sequence is 1 or more")
    if conversation != current:
        return -1
    if current_through is None or from_sequence > current_through:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "that part of the current conversation is not searchable")
    return current_through


def preview(blocks: Sequence[ContentBlock]) -> str:
    text = " ".join(" ".join(b.text.split()) for b in blocks if isinstance(b, TextBlock)).strip()
    return text if len(text) <= PREVIEW_CHARS else text[: PREVIEW_CHARS - 1] + "…"


# --- Memory passes (V3-P85) -----------------------------------------------------------


def check_note_text(category: MemoryCategory, text: object) -> str:
    """A note's text as stored: an open loop is shorter (§5.1)."""
    text = check_memory_text(text)
    if category is MemoryCategory.LOOP and len(text) > MAX_LOOP_CHARS:
        raise DataError(DataErrorCode.TOO_LARGE, f"an open loop is at most {MAX_LOOP_CHARS} characters")
    return text


def check_episode_text(text: object) -> str:
    if not isinstance(text, str):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "an episode is text")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if any(unicodedata.category(c) in ("Cc", "Cs") and c not in "\n\t" for c in text):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "an episode may not contain control characters")
    text = text.strip()
    if not text:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "an episode needs some text")
    if len(text) > MAX_EPISODE_CHARS:
        raise DataError(DataErrorCode.TOO_LARGE, f"an episode is at most {MAX_EPISODE_CHARS} characters")
    return text


def user_owned(note: Memory) -> bool:
    """Written or kept by the user: automatic passes leave it alone (§4.4)."""
    return note.origin is MemoryOrigin.USER or note.user_approved_at is not None


def _check_item_fields(item: PassItem) -> PassItem:
    """The note an ADD, UPDATE or PROPOSE writes, normalized; DataError if not one."""
    if not isinstance(item.category, MemoryCategory):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "not a memory category")
    text = check_note_text(item.category, item.text)
    loop = item.category is MemoryCategory.LOOP
    if (item.due_at is None) != (item.due_instant is None) or (item.valid_until is None) != (item.valid_instant is None):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a date comes with its instant")
    if (not loop and item.due_at is not None) or (loop and item.valid_until is not None):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a loop has a due date, a fact an end")
    return PassItem(item.item, item.proposal, item.action, item.reason, item.memory_id, item.expected_revision,
                    item.category, text, item.due_at, item.due_instant, item.valid_until, item.valid_instant)


def decide_item(item: PassItem, kind: PassKind, note: Memory | None,
                live_notes: int) -> tuple[str, PassAction | None, PassItem]:
    """§4.4, §4.9: the outcome recorded for one item, the change to make
    (None: only recorded) and the item as normalized. ``note``: the live note
    an UPDATE or FORGET names (None: none); ``live_notes``: the principal's
    live notes now."""
    try:
        if item.action is PassAction.DROP:
            return item.reason or "dropped", None, item
        if item.action is PassAction.PROPOSE or (kind is not PassKind.LIVE and item.action is PassAction.ADD):
            normal = _check_item_fields(item)
            return (item.reason if item.action is PassAction.PROPOSE and item.reason else "proposed"), None, normal
        if kind is not PassKind.LIVE:
            return "past_range", None, item
        if item.action is PassAction.ADD:
            normal = _check_item_fields(item)
            if live_notes >= MAX_MEMORIES:
                return "memory_full", None, normal
            return "applied", PassAction.ADD, normal
        if note is None:
            return "not_found", None, item
        if user_owned(note):
            return "user_owned", None, item
        if item.expected_revision != note.revision:
            return "revision_conflict", None, item
        if note.remind_state in PENDING_REMINDERS and (item.action is PassAction.FORGET
                                                       or item.category is not MemoryCategory.LOOP):
            # V3-P87 §5.3 (PR #157 re-review P2-2): a reminder the user asked for waits to be told; an
            # automatic pass neither forgets its loop nor makes it something else (the user and the
            # model's memory_forget still can)
            return "reminder_pending", None, item
        if item.action is PassAction.FORGET:
            return "applied", PassAction.FORGET, item
        return "applied", PassAction.UPDATE, _check_item_fields(item)
    except DataError as error:
        return ("too_large" if error.code is DataErrorCode.TOO_LARGE else "invalid"), None, item


def planned_json(item: PassItem) -> str:
    """The normalized note a PROPOSE (or an applied change) names, for the record."""
    def moment(value: object) -> str | None:
        return None if value is None else value.isoformat()  # type: ignore[attr-defined]

    return json.dumps({"category": None if item.category is None else item.category.value, "text": item.text,
                       "due_at": item.due_at, "due_instant": moment(item.due_instant),
                       "valid_until": item.valid_until, "valid_instant": moment(item.valid_instant),
                       "memory_id": item.memory_id}, ensure_ascii=False, sort_keys=True)


def memory_json(note: Memory) -> str:
    """A note's state before a pass changed it (§4.4), for Undo."""
    def moment(value: object) -> str | None:
        return None if value is None else value.isoformat()  # type: ignore[attr-defined]

    call = note.source_call
    return json.dumps({"memory_id": note.memory_id, "category": note.category.value, "text": note.text,
                       "origin": note.origin.value, "source_conversation": note.source_conversation,
                       "source_call": None if call is None else {"run_id": call.run_id, "call_id": call.call_id},
                       "source_pass": note.source_pass, "source_item": note.source_item,
                       "due_at": note.due_at, "due_instant": moment(note.due_instant),
                       "valid_until": note.valid_until, "valid_instant": moment(note.valid_instant),
                       "remind_at": note.remind_at, "remind_instant": moment(note.remind_instant),
                       "user_approved_at": moment(note.user_approved_at), "revision": note.revision},
                      ensure_ascii=False, sort_keys=True)


def extends(plan: EpisodePlan, kind: PassKind, previous: Episode | None) -> bool:
    """§6.2: whether the plan rewrites ``previous`` (this Conversation's newest
    episode, read now) rather than adding one."""
    return (kind is PassKind.LIVE and plan.extend is not None and previous is not None
            and previous.episode_id == plan.extend and previous.origin is MemoryOrigin.EXTRACTOR
            and previous.revision == plan.expected_revision
            and plan.started_at - previous.ended_at <= plan.extend_within)


def overlaps(a_from: int, a_through: int, b_from: int, b_through: int) -> bool:
    """Two parts (from exclusive, through inclusive) share an Event."""
    return a_from < b_through and b_from < a_through


def check_fresh(note: Memory, now: datetime) -> Memory:
    """A kept proposal (V3-P85 §10.2) must not be one the hourly cleanup takes
    at once: STALE_DATE for a loop due STALE_DUE ago or a fact past its end."""
    if note.due_instant is not None and note.due_instant < now - STALE_DUE:
        raise DataError(DataErrorCode.STALE_DATE, f"its date {note.due_at} has passed")
    if note.valid_instant is not None and note.valid_instant < now:
        raise DataError(DataErrorCode.STALE_DATE, f"it held until {note.valid_until}")
    return note


def dated(note: Memory, dates: NoteDates | None) -> Memory:
    """The user's write (V3-P85 §10.2): ``dates`` given are the note's, checked;
    None keeps its own, but not one its category does not have."""
    loop = note.category is MemoryCategory.LOOP
    if dates is None:
        return dataclasses.replace(note, due_at=note.due_at if loop else None,
                                   due_instant=note.due_instant if loop else None,
                                   valid_until=None if loop else note.valid_until,
                                   valid_instant=None if loop else note.valid_instant,
                                   remind_at=note.remind_at if loop else None,
                                   remind_instant=note.remind_instant if loop else None)
    if (dates.due_at is None) != (dates.due_instant is None) or \
            (dates.valid_until is None) != (dates.valid_instant is None) or \
            (dates.remind_at is None) != (dates.remind_instant is None):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a date comes with its instant")
    if (not loop and (dates.due_at is not None or dates.remind_at is not None)) or \
            (loop and dates.valid_until is not None):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a loop has a due date and a reminder, a fact an end")
    return dataclasses.replace(note, due_at=dates.due_at, due_instant=dates.due_instant,
                               valid_until=dates.valid_until, valid_instant=dates.valid_instant,
                               remind_at=dates.remind_at, remind_instant=dates.remind_instant)


def unreminded(note: Memory) -> Memory:
    """V3-P87 §5.3: only a loop keeps a reminder (a note that stops being one loses it)."""
    if note.category is MemoryCategory.LOOP or note.remind_at is None:
        return note
    return dataclasses.replace(note, remind_at=None, remind_instant=None, remind_state=None, reminded_at=None)


# --- HIMARI's life (character-life v0.1 §2) ------------------------------------------


def check_life_text(kind: LifeKind, text: object) -> str:
    """An entry's text as stored: newlines normalized, trimmed, a diary longer."""
    if not isinstance(text, str):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a life entry is text")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if any(unicodedata.category(c) in ("Cc", "Cs") and c not in "\n\t" for c in text):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a life entry may not contain control characters")
    text = text.strip()
    if not text:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a life entry needs some text")
    limit = {LifeKind.DIARY: MAX_DIARY_CHARS, LifeKind.BOND: MAX_BOND_CHARS, LifeKind.ARC: MAX_ARC_CHARS,
             LifeKind.WORK: MAX_WORK_CHARS, LifeKind.THOUGHT: MAX_THOUGHT_CHARS,
             LifeKind.EVENT: MAX_EVENT_CHARS}.get(kind, MAX_LIFE_CHARS)
    if len(text) > limit:
        raise DataError(DataErrorCode.TOO_LARGE, f"a {kind.value} is at most {limit} characters")
    return text


def check_life_day(day: object) -> str:
    """A diary's or plan's date: YYYY-MM-DD."""
    if not isinstance(day, str) or len(day) != 10:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a date is YYYY-MM-DD")
    try:
        date.fromisoformat(day)
    except ValueError:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a date is YYYY-MM-DD") from None
    return day


def check_life_persona(persona_id: object) -> int:
    """NO_PERSONA (0) or a persona's id."""
    if isinstance(persona_id, bool) or not isinstance(persona_id, int) or not 0 <= persona_id < 1 << 63:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, f"no persona {persona_id}")
    return persona_id


# character-routine §9.1: the entries a date's record holds (list_life_days)
DAY_KINDS = frozenset({LifeKind.PLAN, LifeKind.DIARY, LifeKind.WORK, LifeKind.EVENT})


def order_life(entries: Iterable[LifeEntry]) -> tuple[LifeEntry, ...]:
    """Facts first (the user's, then newest), then the arcs (character-world §4: the open
    ones, newest start first, then the closed ones, newest end first), then the dated
    entries, newest date first, then the kept thoughts, newest first (character-mind §5.2)."""
    listed = list(entries)
    facts = sorted((e for e in listed if e.kind is LifeKind.FACT),
                   key=lambda e: (e.origin is not LifeOrigin.USER, -e.updated_at.timestamp(), -e.entry_id))
    arcs = sorted((e for e in listed if e.kind is LifeKind.ARC),
                  key=lambda e: (e.end_day is None, e.end_day or e.day or "", e.entry_id), reverse=True)
    dated_ = sorted((e for e in listed if e.kind not in (LifeKind.FACT, LifeKind.ARC, LifeKind.THOUGHT)),
                    key=lambda e: (e.day or "", e.kind is LifeKind.DIARY, e.entry_id), reverse=True)
    thoughts = sorted((e for e in listed if e.kind is LifeKind.THOUGHT),
                      key=lambda e: (e.created_at, e.entry_id), reverse=True)
    return (*facts, *arcs, *dated_, *thoughts)


def write_job(start: LifeWriteStart) -> LifeJob:
    """character-mind §14.2: a life write's job as stored — the one given, else WORK_END with a plan and
    REVIEW without; a work job names its plan and no other job has one, and why its model was chosen is
    text (INVALID_ARGUMENT otherwise)."""
    job = start.job if start.job is not None else LifeJob.WORK_END if start.plan_id is not None else LifeJob.REVIEW
    if not isinstance(job, LifeJob) or (job in WORK_JOBS) != (start.plan_id is not None):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a work job names its plan, and no other job has one")
    if start.model_reason is not None and not isinstance(start.model_reason, str):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a model's reason is text")
    return job


def check_thought_note(thought: object) -> str:
    """§4.1: a self run's thought as kept with its write: text, trimmed."""
    if not isinstance(thought, str):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a thought is text")
    return thought.strip()


def expired_thoughts(entries: Iterable[LifeEntry], now: datetime) -> list[LifeEntry]:
    """§5.2: the kept thoughts among ``entries`` expired by ``now`` (they retire once the review read them)."""
    return [e for e in entries if e.kind is LifeKind.THOUGHT and not live_thought(e, now)]


def check_world_event(event: object) -> WorldEvent:
    """character-mind §7: a world's event as stored — its time HH:MM and one line of at most MAX_EVENT_CHARS."""
    if not isinstance(event, WorldEvent):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "not an event")
    at = check_time_of_day(event.time_of_day)
    if at is None or not isinstance(event.text, str) or any(c in event.text for c in "\r\n"):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "an event has its time and one line")
    return WorldEvent(at, check_life_text(LifeKind.EVENT, event.text))


def world_entry(persona_id: int, day: str, event: WorldEvent, write_id: str, now: datetime) -> LifeEntry:
    """§7: an event as an EVENT entry of the write's date (its id given as it is inserted)."""
    return LifeEntry(0, persona_id, LifeKind.EVENT, day, event.text, LifeOrigin.WRITER, 1, now, now, source=write_id,
                     time_of_day=event.time_of_day)


def check_editable(entry: LifeEntry) -> None:
    """character-mind §5.2: the user deletes a kept thought, never rewrites it (INVALID_ARGUMENT). character-day
    §4.2: a sleep slot is only moved, through the schedule's own write (PLAN_REFUSED to a generic edit)."""
    if entry.kind is LifeKind.THOUGHT:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a kept thought is only deleted")
    if entry.kind is LifeKind.PLAN and entry.plan_kind is PlanKind.SLEEP:
        raise _refused("A sleep slot is only moved (its date, start and length), on the schedule.")
    if entry.kind is LifeKind.PLAN and entry.plan_kind is PlanKind.DIARY:  # character-diary §3
        raise _refused("A diary slot is only moved (its date, start and length), on the schedule.")


def live_thought(entry: LifeEntry, now: datetime) -> bool:
    """character-mind §5.2: a kept thought not expired at ``now`` (any other entry: True)."""
    return entry.kind is not LifeKind.THOUGHT or entry.expires_at is None or entry.expires_at > now


def thought_expiry(about: ThoughtAbout, now: datetime) -> datetime:
    """§5.2: when a thought kept at ``now`` stops being told: THOUGHT_DAYS, an intent INTENT_DAYS."""
    return now + (INTENT_DAYS if about is ThoughtAbout.INTENT else THOUGHT_DAYS)


def thought_change_outcome(change: object, thoughts: Sequence[LifeEntry]) -> str | None:
    """§5.1, §5.2: why a memory pass's thought decision cannot be applied to the persona's live
    kept ``thoughts``, or None when it can (a KEEP's room is made as it is kept)."""
    if not isinstance(change, ThoughtChange) or not isinstance(change.op, ThoughtOp):
        return "invalid"
    if change.op is ThoughtOp.SAID:
        return None if any(t.entry_id == change.entry_id for t in thoughts) else "unknown"
    if not isinstance(change.about, ThoughtAbout):
        return "invalid"
    try:
        text = check_life_text(LifeKind.THOUGHT, change.text)
    except DataError:
        return "invalid"
    folded = fold(text)
    return "duplicate" if any(fold(t.text) == folded for t in thoughts) else None


def thought_room(thoughts: Sequence[LifeEntry]) -> LifeEntry | None:
    """§5.2: the kept thought to retire so one more fits within MAX_THOUGHTS: the oldest, or None."""
    if len(thoughts) < MAX_THOUGHTS:
        return None
    return min(thoughts, key=lambda t: (t.created_at, t.entry_id))


def kept_thought(persona_id: int, change: ThoughtChange, conversation: str, source: str,
                 now: datetime) -> LifeEntry:
    """§5.2: a KEEP as stored (its id given as it is inserted), origin CONVERSATION."""
    assert change.about is not None
    return LifeEntry(0, persona_id, LifeKind.THOUGHT, None, check_life_text(LifeKind.THOUGHT, change.text),
                     LifeOrigin.CONVERSATION, 1, now, now, ConversationId(conversation), tuple(change.sequences),
                     source, about=change.about, expires_at=thought_expiry(change.about, now))


def same_fact(text: str, facts: Iterable[LifeEntry]) -> bool:
    """A fact whose text is already a live fact's, compared folded."""
    folded = fold(text)
    return any(fold(f.text) == folded for f in facts)


def life_change_outcome(change: object, facts: Sequence[LifeEntry]) -> str | None:
    """§4: why a memory pass's ``self`` decision cannot be applied to the
    persona's live ``facts`` as they are now, or None when it can (an ADD's
    room and repeats are checked as it is added)."""
    if not isinstance(change, LifeChange) or not isinstance(change.op, LifeChangeOp):
        return "invalid"
    text: str | None = None
    if change.op is not LifeChangeOp.RETIRE:
        try:
            text = check_life_text(LifeKind.FACT, change.text)
        except DataError:
            return "invalid"
    if change.op is LifeChangeOp.ADD:
        return None
    target = next((f for f in facts if f.entry_id == change.entry_id), None)
    if target is None:
        return "unknown"
    if target.origin is LifeOrigin.USER:
        return "user_owned"
    if change.expected_revision != target.revision:
        return "conflict"
    if text is not None and same_fact(text, (f for f in facts if f.entry_id != target.entry_id)):
        return "duplicate"
    return None


def fact_room(facts: Sequence[LifeEntry]) -> tuple[bool, LifeEntry | None]:
    """§2: whether one more fact fits among the persona's live ``facts``, and the
    one to retire for it: the oldest of the writer's, then of the conversations'
    (the user's stay). (False, None): all MAX_LIFE_FACTS are the user's."""
    if len(facts) < MAX_LIFE_FACTS:
        return True, None
    others = [f for f in facts if f.origin is not LifeOrigin.USER]
    if not others:
        return False, None
    return True, min(others, key=lambda f: (f.origin is not LifeOrigin.WRITER, f.updated_at, f.entry_id))


# --- The bond (character-bond v0.1 §2–§4) -------------------------------------------


def check_zone(zone: object) -> ZoneInfo:
    """An IANA time zone's name."""
    try:
        if isinstance(zone, str) and zone:
            return ZoneInfo(zone)
    except (ValueError, ZoneInfoNotFoundError):
        pass
    raise DataError(DataErrorCode.INVALID_ARGUMENT, f"no time zone {zone!r}")


def check_bond_tag(tag: object) -> BondTag:
    if not isinstance(tag, BondTag):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, f"no bond tag {tag!r}")
    return tag


def check_month_day(tag: BondTag, month_day: object) -> str | None:
    """A ``date`` entry's MM-DD (a day of some year: 02-29 is one); None for any other tag."""
    if tag is not BondTag.DATE:
        if month_day is not None:
            raise DataError(DataErrorCode.INVALID_ARGUMENT, "only a date has a month and day")
        return None
    if (not isinstance(month_day, str) or len(month_day) != 5 or month_day[2] != "-"
            or not month_day[:2].isdigit() or not month_day[3:].isdigit()):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a date's day is MM-DD")
    try:
        date(2000, int(month_day[:2]), int(month_day[3:]))  # a leap year: every day some year has
    except ValueError:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a date's day is MM-DD") from None
    return month_day


_BOND_FIRST = {BondTag.NAME: 0, BondTag.PROMISE: 1}  # §5: names and promises are told first


def order_bond(entries: Iterable[LifeEntry]) -> tuple[LifeEntry, ...]:
    """Names, then promises, then the rest; newest first within each."""
    return tuple(sorted(entries, key=lambda e: (_BOND_FIRST.get(e.tag, 2) if e.tag is not None else 2,
                                                -e.updated_at.timestamp(), -e.entry_id)))


def same_bond(tag: BondTag | None, text: str, entries: Iterable[LifeEntry]) -> bool:
    """An entry whose tag and text (folded) are already a live one's."""
    folded = fold(text)
    return any(e.tag is tag and fold(e.text) == folded for e in entries)


def bond_change_outcome(change: object, entries: Sequence[LifeEntry]) -> str | None:
    """§3: why a memory pass's ``bond`` decision cannot be applied to the
    persona's live bond ``entries`` as they are now, or None when it can (an
    ADD's room and repeats are checked as it is added)."""
    if not isinstance(change, BondChange) or not isinstance(change.op, LifeChangeOp):
        return "invalid"
    text: str | None = None
    tag: BondTag | None = None
    if change.op is not LifeChangeOp.RETIRE:
        try:
            text = check_life_text(LifeKind.BOND, change.text)
            tag = check_bond_tag(change.tag)
            check_month_day(tag, change.month_day)
        except DataError:
            return "invalid"
    if change.op is LifeChangeOp.ADD:
        return None
    target = next((e for e in entries if e.entry_id == change.entry_id), None)
    if target is None:
        return "unknown"
    if target.origin is LifeOrigin.USER:
        return "user_owned"
    if change.expected_revision != target.revision:
        return "conflict"
    if text is not None and same_bond(tag, text, (e for e in entries if e.entry_id != target.entry_id)):
        return "duplicate"
    return None


def bond_room(entries: Sequence[LifeEntry]) -> tuple[bool, LifeEntry | None]:
    """§2: whether one more entry fits among the persona's live bond ``entries``,
    and the one to retire for it: the oldest not the user's. (False, None): all
    MAX_BOND_ENTRIES are the user's."""
    if len(entries) < MAX_BOND_ENTRIES:
        return True, None
    others = [e for e in entries if e.origin is not LifeOrigin.USER]
    if not others:
        return False, None
    return True, min(others, key=lambda e: (e.updated_at, e.entry_id))


def settle_stage(standing: BondStanding, entries: Sequence[LifeEntry], met_on: date | None, today: date,
                 zone: tzinfo, now: datetime) -> BondStanding:
    """§4 (B3): the stage moved up as far as what happened allows, never down.
    ``new`` → ``familiar``: a ``name`` or ``joke`` entry, or BOND_FAMILIAR_DAYS
    days together. ``familiar`` → ``close``: a ``promise`` or ``first`` entry and
    BOND_CLOSE_DAYS days together. After the user chose the stage, only what
    happened after that counts: an entry made later, or a day count reached on
    a later day. The standing as it was when nothing moves (revision unchanged)."""
    floor = standing.since if standing.reason == "user" else None
    days = 0 if met_on is None else together_days(met_on, today)

    def fresh(entry: LifeEntry) -> bool:
        return floor is None or entry.created_at > floor

    def reached(count: int) -> date | None:
        """The day the count was reached, if it was and counts."""
        if met_on is None or days < count:
            return None
        on = met_on + timedelta(days=count - 1)
        return on if floor is None or on > floor.astimezone(zone).date() else None

    stage, since, reason = standing.stage, standing.since, standing.reason
    if stage is BondStage.NEW:
        made = sorted((e for e in entries if e.tag in (BondTag.NAME, BondTag.JOKE) and fresh(e)),
                      key=lambda e: (e.created_at, e.entry_id))
        if made:
            stage, since, reason = BondStage.FAMILIAR, made[0].created_at, f"b{made[0].entry_id}"
        elif reached(BOND_FAMILIAR_DAYS) is not None:
            stage, since, reason = BondStage.FAMILIAR, now, f"{BOND_FAMILIAR_DAYS}d"
    if stage is BondStage.FAMILIAR and days >= BOND_CLOSE_DAYS:
        keys = sorted((e for e in entries if e.tag in (BondTag.PROMISE, BondTag.FIRST)),
                      key=lambda e: (e.created_at, e.entry_id))
        new_keys = [e for e in keys if fresh(e)]
        on = reached(BOND_CLOSE_DAYS)
        if keys and (new_keys or on is not None):
            key = (new_keys or keys)[0]
            if on is None or key.created_at.astimezone(zone).date() > on:
                stage, since, reason = BondStage.CLOSE, max(key.created_at, since or key.created_at), f"b{key.entry_id}"
            else:
                stage, since, reason = BondStage.CLOSE, now, f"{BOND_CLOSE_DAYS}d"
    if stage is standing.stage:
        return standing
    return BondStanding(standing.persona_id, stage, since, reason, standing.revision + 1)


# --- State: the plans' times and the lasting mood (character-state v0.1 §2, §3) -------------


def check_time_of_day(value: object) -> str | None:
    """A plan's start: HH:MM on a 24-hour clock, or None."""
    if value is None:
        return None
    if (not isinstance(value, str) or len(value) != 5 or value[2] != ":" or not value[:2].isdigit()
            or not value[3:].isdigit() or int(value[:2]) > 23 or int(value[3:]) > 59):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a time is HH:MM")
    return value


def check_mood(persona_id: object, mood: object, source: object, at: object, reason: object) -> HimariMood:
    """§2: a mood as stored — a name in MOOD_NAMES, a reason (the writer's only) trimmed,
    at most MAX_MOOD_REASON_CHARS and with no control characters, at an aware time."""
    persona = check_life_persona(persona_id)
    if not isinstance(mood, str) or mood not in MOOD_NAMES:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, f"no lasting mood {mood!r}")
    if not isinstance(source, MoodSource):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, f"no mood source {source!r}")
    if not isinstance(at, datetime) or at.utcoffset() is None:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a mood's time has its offset")
    why: str | None = None
    if reason is not None:
        if source is not MoodSource.WRITER or not isinstance(reason, str):
            raise DataError(DataErrorCode.INVALID_ARGUMENT, "only the story writer gives a reason")
        if any(unicodedata.category(c) in ("Cc", "Cs") for c in reason):
            raise DataError(DataErrorCode.INVALID_ARGUMENT, "a reason may not contain control characters")
        why = reason.strip() or None
        if why is not None and len(why) > MAX_MOOD_REASON_CHARS:
            raise DataError(DataErrorCode.INVALID_ARGUMENT, f"a reason is at most {MAX_MOOD_REASON_CHARS} characters")
    return HimariMood(persona, mood, at, source, why)


def check_life_plans(plan: LifePlan) -> list[tuple[str, str, str | None, int | None, int]]:
    """A writer's plans as stored: each date, text and (by position in ``plan.times``, ``plan.plan_arcs`` and
    ``plan.plan_minutes``) start time, project (character-work §4: an arc id or NEW_ARC) and length
    (character-day §4.1: DEFAULT_PLAN_MINUTES when not given)."""
    if len(plan.times) > len(plan.plans) or len(plan.plan_arcs) > len(plan.plans) \
            or len(plan.plan_minutes) > len(plan.plans):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a time, a project or a length for no plan")
    arcs = [plan.plan_arcs[i] if i < len(plan.plan_arcs) else None for i in range(len(plan.plans))]
    if any(a is not None and (isinstance(a, bool) or not isinstance(a, int) or a < NEW_ARC) for a in arcs):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a plan's project is an arc's id")
    lengths = [plan.plan_minutes[i] if i < len(plan.plan_minutes) else None for i in range(len(plan.plans))]
    if any(m is not None and (isinstance(m, bool) or not isinstance(m, int)
                              or not MIN_PLAN_MINUTES <= m <= MAX_PLAN_MINUTES) for m in lengths):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a plan's length is out of range")
    return [(check_life_day(day), check_life_text(LifeKind.PLAN, text),
             check_time_of_day(plan.times[i] if i < len(plan.times) else None), arcs[i],
             lengths[i] or DEFAULT_PLAN_MINUTES)
            for i, (day, text) in enumerate(plan.plans)]


def covers_sleep(day: str, at: str | None, minutes: int, plans: Iterable[LifeEntry], zone: tzinfo) -> bool:
    """character-day §4.4, §18.7: whether a writer's timed plan overlaps a live sleep slot among ``plans`` (it is
    then left out alone)."""
    if at is None:
        return False
    span = _span(day, at, minutes, zone)
    return any(e.sleep_slot and (s := plan_span(e, zone)) is not None and _overlaps(span, s) for e in plans)


def plan_project(arc: int | None, at: str | None, arcs: Sequence[LifeEntry], started: int | None) -> int | None:
    """character-work §4: the project a writer's plan is kept with — an open arc of the persona
    (``arcs``: its open ones), or the arc the same write started (NEW_ARC) — and only with a time
    (a work plan has one); else None (the plan stays, not a work plan)."""
    if arc is None or at is None:
        return None
    if arc == NEW_ARC:
        return started
    return arc if any(a.entry_id == arc and a.arc_open for a in arcs) else None


# --- World: HIMARI's arcs (character-world v0.1 §4) -------------------------------------------


def check_arc_title(title: object) -> str:
    """An arc's title: one line of text, trimmed, at most MAX_ARC_TITLE_CHARS."""
    if not isinstance(title, str):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "an arc's title is text")
    if any(unicodedata.category(c) in ("Cc", "Cs") for c in title):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "an arc's title is one line, with no control characters")
    title = title.strip()
    if not title:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "an arc needs a title")
    if len(title) > MAX_ARC_TITLE_CHARS:
        raise DataError(DataErrorCode.TOO_LARGE, f"an arc's title is at most {MAX_ARC_TITLE_CHARS} characters")
    return title


def edited_title(entry: LifeEntry, title: object) -> str | None:
    """The title after the user's edit: an arc's or a work piece's given one, checked, else the
    entry's own. INVALID_ARGUMENT for a title given to an entry that is neither."""
    if title is None:
        return entry.title
    if entry.kind not in (LifeKind.ARC, LifeKind.WORK):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "only an arc or a work piece has a title")
    return check_arc_title(title)


def check_work_summary(summary: object) -> str:
    """character-work §4: a work piece's summary — one line, trimmed, at most MAX_WORK_SUMMARY_CHARS."""
    if not isinstance(summary, str):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a summary is text")
    if any(unicodedata.category(c) in ("Cc", "Cs") for c in summary):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a summary is one line, with no control characters")
    summary = summary.strip()
    if not summary:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a summary needs some text")
    if len(summary) > MAX_WORK_SUMMARY_CHARS:
        raise DataError(DataErrorCode.TOO_LARGE, f"a summary is at most {MAX_WORK_SUMMARY_CHARS} characters")
    return summary


def edited_summary(entry: LifeEntry, summary: object) -> str | None:
    """The summary after the user's edit: a work piece's given one, checked, else the entry's own."""
    if summary is None:
        return entry.summary
    if entry.kind is not LifeKind.WORK:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "only a work piece has a summary")
    return check_work_summary(summary)


def check_work_piece(piece: object) -> WorkPiece:
    """character-work §4: a worker's piece as stored — its text as a WORK entry's, its title as an
    arc's, its summary one line, the "so far" within MAX_SO_FAR_CHARS and an ending as an arc's."""
    if not isinstance(piece, WorkPiece):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "not a work piece")
    return WorkPiece(check_life_text(LifeKind.WORK, piece.text), check_arc_title(piece.title),
                     check_work_summary(piece.summary), _so_far(piece.so_far),
                     None if piece.done is None else check_life_text(LifeKind.ARC, piece.done))


def check_plan_ref(plan_id: object) -> bool:
    """A life write's work plan: None (the story writer's) or an entry id."""
    return plan_id is None or (isinstance(plan_id, int) and not isinstance(plan_id, bool) and plan_id > 0)


def check_wake_at(sleep: SleepRecord, at: object) -> None:
    """character-day §5.1: a sleep ends at or after it began (INVALID_ARGUMENT)."""
    if not isinstance(at, datetime) or at < sleep.fell_at:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a sleep ends at or after it began")


def check_slot_ref(slot_id: object) -> int | None:
    """character-day §5.1: the sleep slot a sleep is in — None or an entry id (INVALID_ARGUMENT)."""
    if not check_plan_ref(slot_id):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a sleep's slot is an entry id")
    return slot_id  # type: ignore[return-value]


def work_project(plan: LifeEntry | None, persona_id: int, arcs: Sequence[LifeEntry],
                 pieces: Sequence[LifeEntry]) -> LifeEntry:
    """character-work §4: the open arc a worker call's piece goes to — its plan a live work plan of
    the call's persona, its project one of the persona's open ``arcs``, and no live piece of that
    plan yet (``pieces``); else INVALID_ARGUMENT (the piece is thrown away)."""
    if plan is None or plan.kind is not LifeKind.PLAN or plan.persona_id != persona_id or plan.arc_id is None:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "the call's plan is no live work plan")
    project = next((a for a in arcs if a.entry_id == plan.arc_id and a.arc_open), None)
    if project is None:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "the plan's project is not open")
    if any(not p.draft for p in pieces):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "the plan has a piece already")
    return project


def check_draft_read(draft: LifeEntry | None, read: tuple[int, int] | None) -> None:
    """character-mind §4.4 (review P0-3): the plan's live ``draft`` is the one the end step read — the same
    entry at the same revision, or none when it read none; else VERSION_CONFLICT (a stale piece)."""
    now = None if draft is None else (draft.entry_id, draft.revision)
    if now != (None if read is None else (read[0], read[1])):
        raise DataError(DataErrorCode.VERSION_CONFLICT, "the draft changed since the work's end read it")


def check_work_draft(draft: object) -> WorkDraft:
    """character-mind §4.4: a start step's draft as stored — its title as an arc's, its concept as a
    piece's summary (one line) and its text as a WORK entry's."""
    if not isinstance(draft, WorkDraft):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "not a work draft")
    return WorkDraft(check_arc_title(draft.title), check_work_summary(draft.concept),
                     check_life_text(LifeKind.WORK, draft.text))


def draft_entry(plan: LifeEntry, project: LifeEntry, draft: WorkDraft, write_id: str, now: datetime) -> LifeEntry:
    """§4.4: the draft as a WORK entry of the plan (its id given as it is inserted)."""
    return LifeEntry(0, plan.persona_id, LifeKind.WORK, plan.day, draft.text, LifeOrigin.WRITER, 1, now, now,
                     source=write_id, time_of_day=plan.time_of_day, title=draft.title, arc_id=project.entry_id,
                     summary=draft.concept, plan_id=plan.entry_id, draft=True)


def finished(draft: LifeEntry, piece: WorkPiece, write_id: str, now: datetime) -> LifeEntry:
    """§4.4: the plan's draft filled by its end step: the piece, its title and summary, made ``now``."""
    return dataclasses.replace(draft, text=piece.text, title=piece.title, summary=piece.summary, draft=False,
                               origin=LifeOrigin.WRITER, revision=draft.revision + 1, created_at=now, updated_at=now,
                               source=write_id)


def work_entry(plan: LifeEntry, project: LifeEntry, piece: WorkPiece, write_id: str, now: datetime) -> LifeEntry:
    """§4: the piece as a WORK entry (its id given as it is inserted): the plan's day and time."""
    return LifeEntry(0, plan.persona_id, LifeKind.WORK, plan.day, piece.text, LifeOrigin.WRITER, 1, now, now,
                     source=write_id, time_of_day=plan.time_of_day, title=piece.title, arc_id=project.entry_id,
                     summary=piece.summary, plan_id=plan.entry_id)


def worked_on(project: LifeEntry, plan: LifeEntry, piece: WorkPiece, now: datetime) -> LifeEntry:
    """§3, §4: the project after a piece — its "so far" rewritten and, with ``done``, closed on
    the plan's day with that ending (its title and goal stay, the user's too). character-routine §11.1: never on
    the day it started (``project_end_refusal``): then the piece and the "so far" stay and the project stays open."""
    worked = dataclasses.replace(project, so_far=piece.so_far, revision=project.revision + 1, updated_at=now)
    if piece.done is None or project_end_refusal(project, plan.day or "", LifeOrigin.WRITER) is not None:
        return worked
    return dataclasses.replace(worked, end_day=plan.day, ending=piece.done)


def _so_far(text: object) -> str:
    if not isinstance(text, str):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a project's so far is text")
    checked = check_life_text(LifeKind.WORK, text)
    if len(checked) > MAX_SO_FAR_CHARS:
        raise DataError(DataErrorCode.TOO_LARGE, f"a project's so far is at most {MAX_SO_FAR_CHARS} characters")
    return checked


def check_arc_move(move: object) -> ArcMove | None:
    """A writer's arc move as stored: START with its title and goal, ADVANCE with its arc,
    CLOSE with its arc and ending (the ending as an arc's goal is checked)."""
    if move is None:
        return None
    if not isinstance(move, ArcMove) or not isinstance(move.op, ArcOp):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "not an arc move")
    if move.op is ArcOp.START:
        return ArcMove(ArcOp.START, title=check_arc_title(move.title), goal=check_life_text(LifeKind.ARC, move.goal))
    if isinstance(move.entry_id, bool) or not isinstance(move.entry_id, int):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "an arc move names its arc")
    if move.op is ArcOp.ADVANCE:
        return ArcMove(ArcOp.ADVANCE, entry_id=move.entry_id)
    return ArcMove(ArcOp.CLOSE, entry_id=move.entry_id, ending=check_life_text(LifeKind.ARC, move.ending))


def arc_moved(move: ArcMove, arcs: Sequence[LifeEntry]) -> LifeEntry | None:
    """§4 (W3): the open arc among the persona's live ``arcs`` an ADVANCE or CLOSE moves,
    or None when it names no open arc of the persona (the move is left out)."""
    return next((a for a in arcs if a.entry_id == move.entry_id and a.arc_open), None)


# --- HIMARI's projects (character-routine v0.1 §11.1) --------------------------------------------


def check_project_write(op: object, write: object, origin: LifeOrigin) -> ProjectWrite:
    """§11.1: a project change as kept — a start's title and goal, an update's project and new title and/or goal,
    an end's project and ending, each with its reason (one line; HIMARI's must say it)."""
    if op not in PROJECT_OPS:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "not a project change")
    if not isinstance(write, ProjectWrite):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a project change carries its project")
    reason = check_plan_reason(write.reason)
    if reason is None and origin is LifeOrigin.HIMARI:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a project change says why")
    if op is PlanOp.PROJECT_START:
        return ProjectWrite(title=check_arc_title(write.title), goal=check_life_text(LifeKind.ARC, write.goal),
                            reason=reason)
    if isinstance(write.entry_id, bool) or not isinstance(write.entry_id, int):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a project change names its project")
    if op is PlanOp.PROJECT_UPDATE:
        if write.title is None and write.goal is None:
            raise DataError(DataErrorCode.INVALID_ARGUMENT, "an update changes the title or the goal")
        return ProjectWrite(write.entry_id, title=None if write.title is None else check_arc_title(write.title),
                            goal=None if write.goal is None else check_life_text(LifeKind.ARC, write.goal),
                            reason=reason)
    return ProjectWrite(write.entry_id, ending=check_life_text(LifeKind.ARC, write.ending), reason=reason)


def project_end_refusal(project: LifeEntry, day: str, origin: LifeOrigin) -> str | None:
    """§11.1 (R6): a project is never ended on the day it started (nor before it) — but by the user."""
    if origin is LifeOrigin.USER or day > (project.day or ""):
        return None
    return f"Project {project.entry_id} started today; carry it on and end it on another day."


def project_changed(op: PlanOp, write: ProjectWrite, entries: Sequence[LifeEntry], *, persona_id: int,
                    origin: LifeOrigin, day: str, zone: tzinfo, now: datetime,
                    source: str | None = None) -> LifeEntry:
    """§11.1: the one project change the Tools, the diary and the Web make — ``write`` checked
    (``check_project_write``) against the persona's ``entries`` (its plans and arcs: the open ones and, for HIMARI's
    day count, the ones it closed), by ``origin`` on ``day``. The project as kept: a new one (id 0, given as it is
    inserted) or the open one changed (revision + 1). PLAN_REFUSED with the sentence, or NOT_FOUND."""
    open_ = [a for a in entries if a.arc_open and a.persona_id == persona_id]
    if op is PlanOp.PROJECT_START:
        if len(open_) >= MAX_OPEN_ARCS:
            raise _refused(f"You have {MAX_OPEN_ARCS} open projects already; end one, or start this later.")
        same = next((a for a in open_ if fold(a.title or "") == fold(write.title or "")), None)
        if same is not None:
            raise _refused(f"A project titled \"{same.title}\" is open already (id {same.entry_id}).")
        _counted(entries, None, origin, zone, now)
        assert write.goal is not None
        return LifeEntry(0, persona_id, LifeKind.ARC, day, write.goal, origin, 1, now, now, source=source,
                         title=write.title, reason=write.reason)
    target = next((a for a in open_ if a.entry_id == write.entry_id), None)
    if target is None:
        raise DataError(DataErrorCode.NOT_FOUND, f"There is no open project {write.entry_id}.")
    if op is PlanOp.PROJECT_UPDATE:
        if origin is LifeOrigin.HIMARI and target.origin is LifeOrigin.USER:
            raise _refused("The user wrote this project's title and goal; leave them as they are.")
        _counted(entries, target.entry_id, origin, zone, now)
        return dataclasses.replace(target, title=write.title or target.title, text=write.goal or target.text,
                                   origin=origin, reason=write.reason or target.reason,
                                   revision=target.revision + 1, updated_at=now)
    refusal = project_end_refusal(target, day, origin)
    if refusal is not None:
        raise _refused(refusal)
    _counted(entries, target.entry_id, origin, zone, now)
    return dataclasses.replace(target, end_day=day, ending=write.ending, reason=write.reason or target.reason,
                               origin=target.origin if target.origin is LifeOrigin.USER else origin,
                               revision=target.revision + 1, updated_at=now)


def arc_project_write(move: ArcMove) -> tuple[PlanOp, ProjectWrite]:
    """§11.1: a diary's START or CLOSE as the same change the Tools make (no reason: the diary says it)."""
    if move.op is ArcOp.START:
        return PlanOp.PROJECT_START, ProjectWrite(title=move.title, goal=move.goal)
    return PlanOp.PROJECT_END, ProjectWrite(move.entry_id, ending=move.ending)


def writer_mood(plan: LifePlan, now: datetime) -> HimariMood | None:
    """§2: the writer's mood as stored (its persona set by the write), at ``plan.mood_at`` or ``now``."""
    if plan.mood is None:
        return None
    return check_mood(0, plan.mood, MoodSource.WRITER, plan.mood_at or now, plan.mood_reason)


def amend_mood(mood: str | None, reason: str | None, now: datetime) -> HimariMood | None:
    """character-diary §5: an amend's mood (None: unchanged), as the writer's at ``now`` (its persona set by the
    write)."""
    return None if mood is None else check_mood(0, mood, MoodSource.WRITER, now, reason)


def kept_mood(stored: HimariMood | None, new: HimariMood) -> HimariMood:
    """§2: the newer of the two; one set after ``new`` stays."""
    return stored if stored is not None and stored.set_at > new.set_at else new


# --- HIMARI's schedule (character-day v0.1 §4, §18.2) --------------------------------------------


def _refused(sentence: str) -> DataError:
    """§4.3: a schedule change past its limits, said so the model can fix it and call again."""
    return DataError(DataErrorCode.PLAN_REFUSED, sentence)


def check_plan_reason(reason: object) -> str | None:
    """§4.1: why a slot was moved or cancelled — one line, trimmed, at most MAX_PLAN_REASON_CHARS; None for none."""
    if reason is None:
        return None
    if not isinstance(reason, str) or any(unicodedata.category(c) in ("Cc", "Cs") for c in reason):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a reason is one line of text")
    reason = " ".join(reason.split())
    if len(reason) > MAX_PLAN_REASON_CHARS:
        raise DataError(DataErrorCode.TOO_LARGE, f"a reason is at most {MAX_PLAN_REASON_CHARS} characters")
    return reason or None


def check_plan_write(plan: object) -> PlanWrite:
    """§4.1: a slot's shape as stored — its date, start (HH:MM), length (MIN_PLAN_MINUTES..MAX_PLAN_MINUTES),
    kind, text (a plan's), project (an id, a work slot's only) and reason."""
    if not isinstance(plan, PlanWrite) or not isinstance(plan.kind, PlanKind):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "not a plan")
    at = check_time_of_day(plan.time_of_day)
    if at is None:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a slot starts at a time, HH:MM")
    if isinstance(plan.minutes, bool) or not isinstance(plan.minutes, int) \
            or not MIN_PLAN_MINUTES <= plan.minutes <= MAX_PLAN_MINUTES:
        raise _refused(f"A slot is {MIN_PLAN_MINUTES} to {MAX_PLAN_MINUTES} minutes long.")
    if plan.entry_id is not None and (isinstance(plan.entry_id, bool) or not isinstance(plan.entry_id, int)):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a slot's id is a number")
    arc = plan.arc_id if plan.kind is PlanKind.WORK else None
    if arc is not None and (isinstance(arc, bool) or not isinstance(arc, int) or arc < 1):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a project is an arc's id")
    return dataclasses.replace(plan, day=check_life_day(plan.day), time_of_day=at,
                               text=check_life_text(LifeKind.PLAN, plan.text), arc_id=arc,
                               reason=check_plan_reason(plan.reason))


def _span(day: str, at: str, minutes: int, zone: tzinfo) -> tuple[datetime, datetime]:
    start = datetime.combine(date.fromisoformat(day), time.fromisoformat(at), tzinfo=zone)
    return start, start + timedelta(minutes=minutes)


def _clock(moment: datetime) -> str:
    return moment.strftime("%H:%M")


def _slot_words(entry: LifeEntry, zone: tzinfo) -> str:
    """A slot as a refusal names it: its times and id."""
    span = plan_span(entry, zone)
    when = "" if span is None else f"{_clock(span[0])}–{_clock(span[1])}, "
    return f"({when}id {entry.entry_id})"


def _overlaps(a: tuple[datetime, datetime], b: tuple[datetime, datetime]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def sleep_slot_of(entries: Iterable[LifeEntry], night: date, zone: tzinfo, *,
                  besides: int | None = None) -> LifeEntry | None:
    """§4.4: the live sleep slot of ``night`` among ``entries`` (one besides ``besides``), if any."""
    for entry in entries:
        span = plan_span(entry, zone)
        if entry.sleep_slot and entry.entry_id != besides and span is not None and night_of(span[0]) == night:
            return entry
    return None


def diary_slot_of(entries: Iterable[LifeEntry], night: date, zone: tzinfo, *,
                  besides: int | None = None) -> LifeEntry | None:
    """character-diary §3: the live diary slot of ``night`` among ``entries`` (one besides ``besides``), if any."""
    for entry in entries:
        span = plan_span(entry, zone)
        if entry.diary_slot and entry.entry_id != besides and span is not None and diary_night(span[0]) == night:
            return entry
    return None


def _diary_limits(plan: PlanWrite, before: LifeEntry | None, plans: Sequence[LifeEntry], *, origin: LifeOrigin,
                  zone: tzinfo, start: datetime, end: datetime) -> PlanWrite:
    """character-diary §3, §16.2: a diary slot as ``origin`` writes it — only the keeper (WRITER) makes one; HIMARI
    and the user only move it (its text kept); one a night, starting from DIARY_EARLIEST of its night and ending
    before that night's sleep slot starts, MIN_DIARY_MINUTES to MAX_DIARY_MINUTES long; moved, it stays in its
    night."""
    night = diary_night(start)
    if before is None and origin is not LifeOrigin.WRITER:
        tonight = None if night is None else diary_slot_of(plans, night, zone)
        raise _refused("A diary slot is only moved, never made" + (
            f": change that night's (id {tonight.entry_id}) instead." if tonight is not None else "."))
    if before is not None:
        plan = dataclasses.replace(plan, text=before.text)  # moved: its times only, as a sleep slot's
    if night is None:
        raise _refused(f"A diary slot starts between {DIARY_EARLIEST:%H:%M} and {NIGHT_ENDS:%H:%M}.")
    if not MIN_DIARY_MINUTES <= plan.minutes <= MAX_DIARY_MINUTES:
        raise _refused(f"A diary slot is {MIN_DIARY_MINUTES} to {MAX_DIARY_MINUTES} minutes long.")
    other = diary_slot_of(plans, night, zone, besides=None if before is None else before.entry_id)
    if other is not None:
        raise _refused(f"That night has its diary slot already {_slot_words(other, zone)}.")
    if before is not None:
        span = plan_span(before, zone)
        if span is not None and diary_night(span[0]) != night:
            raise _refused(f"A diary slot stays in its night (from {diary_night(span[0])} "
                           f"{DIARY_EARLIEST:%H:%M} until that night's sleep).")
    sleep = sleep_slot_of(plans, night, zone)
    sleep_span = None if sleep is None else plan_span(sleep, zone)
    if sleep is not None and sleep_span is not None and end > sleep_span[0]:
        raise _refused(f"A diary slot ends before that night's sleep slot {_slot_words(sleep, zone)} starts; move "
                       "the sleep slot first.")
    return plan


def sleep_refusal(start: datetime, minutes: int) -> str | None:
    """§4.4: why a sleep slot from ``start`` (its zone) for ``minutes`` is out of its limits, or None."""
    night = night_of(start)
    if night is None:
        return f"A sleep slot starts between {NIGHT_STARTS:%H:%M} and {NIGHT_ENDS:%H:%M}."
    if not MIN_SLEEP_MINUTES <= minutes <= MAX_SLEEP_MINUTES:
        return f"A sleep slot is {MIN_SLEEP_MINUTES // 60} to {MAX_SLEEP_MINUTES // 60} hours long."
    end = start + timedelta(minutes=minutes)
    clock = end.timetz().replace(tzinfo=None)
    if end.date() != night + timedelta(days=1) or not SLEEP_ENDS_FROM <= clock <= SLEEP_ENDS_BY:
        return f"A sleep slot ends between {SLEEP_ENDS_FROM:%H:%M} and {SLEEP_ENDS_BY:%H:%M} the morning after."
    return None


def himari_writes_today(entries: Iterable[LifeEntry], zone: tzinfo, now: datetime) -> set[int]:
    """§4.4: the slots HIMARI's Tools changed since the user's midnight (each slot once) — character-routine
    §11.1: and its projects (arcs), on the same count."""
    midnight = datetime.combine(now.astimezone(zone).date(), time(), tzinfo=zone)
    return {e.entry_id for e in entries if e.kind in (LifeKind.PLAN, LifeKind.ARC) and e.origin is LifeOrigin.HIMARI
            and e.updated_at >= midnight}


def _counted(plans: Sequence[LifeEntry], entry_id: int | None, origin: LifeOrigin, zone: tzinfo,
             now: datetime) -> None:
    if origin is not LifeOrigin.HIMARI:
        return
    changed = himari_writes_today(plans, zone, now)
    if entry_id not in changed and len(changed) >= MAX_HIMARI_PLAN_WRITES:
        raise _refused(f"You have changed {MAX_HIMARI_PLAN_WRITES} slots and projects today; leave them as they "
                       "are until tomorrow.")


def _uncovered(start: datetime, end: datetime, plans: Sequence[LifeEntry], zone: tzinfo, now: datetime) -> None:
    """§4.4: a sleep slot over [start, end) covers no live slot not over yet (whoever writes it: the keeper too) —
    character-diary §16.2: but the diary slot (an earlier sleep is never held back by it: the diary is then
    written as HIMARI falls asleep)."""
    for other in plans:
        span = plan_span(other, zone)
        if other.live_plan and other.plan_kind not in (PlanKind.SLEEP, PlanKind.DIARY) and span is not None \
                and _overlaps((start, end), span) and span[1] > now:
            raise _refused(f"Your sleep would cover the slot \"{other.text}\" {_slot_words(other, zone)}; move "
                           "or cancel that first.")


def check_plan_limits(plan: PlanWrite, before: LifeEntry | None, plans: Sequence[LifeEntry],
                      arcs: Sequence[LifeEntry], *, origin: LifeOrigin, zone: tzinfo, now: datetime,
                      asleep_in: bool = False) -> PlanWrite:
    """§4.2, §4.4: a checked slot (``check_plan_write``) against the persona's schedule — ``before`` the live slot
    it changes (None: a new one), ``plans`` every plan of the persona (live or not), ``arcs`` its arcs — as
    ``origin`` writes it at ``now``; the write as kept (a sleep slot stays one and keeps its text: only its times
    move), else PLAN_REFUSED. The keeper (WRITER) places and adjusts sleep slots with the night's start and one a
    night only. ``asleep_in``: HIMARI sleeps in ``before`` now — its end never moves before now (a wake never
    comes before the fall)."""
    local = now.astimezone(zone)
    start, end = _span(plan.day, plan.time_of_day, plan.minutes, zone)
    if before is not None and (before.plan_kind is PlanKind.SLEEP) != (plan.kind is PlanKind.SLEEP):
        raise _refused("A sleep slot stays a sleep slot, and no other slot becomes one.")
    if before is not None and (before.plan_kind is PlanKind.DIARY) != (plan.kind is PlanKind.DIARY):
        raise _refused("A diary slot stays a diary slot, and no other slot becomes one.")  # character-diary §3
    if plan.kind is PlanKind.DIARY:
        plan = _diary_limits(plan, before, plans, origin=origin, zone=zone, start=start, end=end)
        if origin is LifeOrigin.WRITER:  # the keeper places it by its own rules (character-diary §3)
            return plan
    if plan.kind is PlanKind.SLEEP and before is not None:
        plan = dataclasses.replace(plan, text=before.text)  # character-day second review 7: moved, times only
        if asleep_in and end < now:
            raise _refused(f"HIMARI is asleep in this slot now; its end can only move to now ({local:%H:%M}) or "
                           "later.")
    if plan.kind is PlanKind.SLEEP:
        night = night_of(start)
        if night is None:
            raise _refused(f"A sleep slot starts between {NIGHT_STARTS:%H:%M} and {NIGHT_ENDS:%H:%M}.")
        if before is None and origin is not LifeOrigin.WRITER:
            tonight = sleep_slot_of(plans, night, zone)
            raise _refused("A sleep slot is only moved, never made" + (
                f": change that night's (id {tonight.entry_id}) instead." if tonight is not None else "."))
        other = sleep_slot_of(plans, night, zone, besides=None if before is None else before.entry_id)
        if other is not None:
            raise _refused(f"That night has its sleep slot already {_slot_words(other, zone)}.")
        if before is not None:
            span = plan_span(before, zone)
            if span is not None and night_of(span[0]) != night:
                raise _refused(f"A sleep slot stays in its night (from {night_of(span[0])} 21:00 to 05:00).")
        if origin is LifeOrigin.WRITER:  # the keeper's own rules place and move it; it never covers a live slot
            _uncovered(start, end, plans, zone, now)
            return plan
    elif origin is LifeOrigin.WRITER and plan.kind is not PlanKind.DIARY:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "the keeper writes sleep and diary slots only")
    _counted([*plans, *arcs], None if before is None else before.entry_id, origin, zone, now)
    if before is not None and (span := plan_span(before, zone)) is not None and span[1] <= now:
        raise _refused(f"That slot is over {_slot_words(before, zone)}; it cannot be changed.")
    if not local.date() <= date.fromisoformat(plan.day) <= local.date() + timedelta(days=SCHEDULE_DAYS):
        raise _refused(f"A slot is dated from today ({local.date()}) to {SCHEDULE_DAYS} days on.")
    if plan.kind is PlanKind.SLEEP:
        refusal = sleep_refusal(start, plan.minutes)
        if refusal is not None:
            raise _refused(refusal)
        moved = before is None or (before.day, before.time_of_day) != (plan.day, plan.time_of_day)
        if moved and start < now - timedelta(minutes=1):
            raise _refused("A sleep slot cannot start in the past; to sleep now, use go_to_sleep.")
        _uncovered(start, end, plans, zone, now)
        return plan
    if end <= now:
        raise _refused(f"That time is already past (it is {local:%H:%M} now).")
    # second review 7: no other slot becomes one either (a together slot stays one when moved)
    if plan.kind is PlanKind.TOGETHER and origin is not LifeOrigin.HIMARI and (
            before is None or before.plan_kind is not PlanKind.TOGETHER):
        raise _refused("Only HIMARI makes a together slot, when it sets a time with you in a conversation: ask it "
                       "there, or add the slot as its own.")
    if plan.kind is PlanKind.WORK and not any(a.entry_id == plan.arc_id and a.arc_open for a in arcs):
        raise _refused("A work slot names an open project by its arc id." if plan.arc_id is None
                       else f"There is no open project {plan.arc_id}.")
    for other in plans:
        span = plan_span(other, zone)
        if other.sleep_slot and span is not None and _overlaps((start, end), span):
            raise _refused(f"It overlaps your sleep slot {_slot_words(other, zone)}; move the sleep slot first.")
    return plan


def planned_entry(persona_id: int, plan: PlanWrite, before: LifeEntry | None, origin: LifeOrigin,
                  now: datetime) -> LifeEntry:
    """§4.1: the slot as stored — new (its id given as it is inserted) or ``before`` changed (revision + 1)."""
    if before is None:
        return LifeEntry(0, persona_id, LifeKind.PLAN, plan.day, plan.text, origin, 1, now, now,
                         time_of_day=plan.time_of_day, arc_id=plan.arc_id, minutes=plan.minutes, plan_kind=plan.kind,
                         reason=plan.reason)
    return dataclasses.replace(before, day=plan.day, time_of_day=plan.time_of_day, minutes=plan.minutes,
                               text=plan.text, arc_id=plan.arc_id, plan_kind=plan.kind,
                               reason=plan.reason if plan.reason is not None else before.reason, origin=origin,
                               revision=before.revision + 1, updated_at=now)


def check_cancel(before: LifeEntry, plans: Sequence[LifeEntry], *, origin: LifeOrigin, zone: tzinfo,
                 now: datetime) -> None:
    """§4.3: a live slot may be cancelled — never a sleep slot (character-diary §3: nor a diary slot), never one
    over; HIMARI's within its day's count (``plans``: the persona's plans and arcs, for it)."""
    if before.plan_kind is PlanKind.SLEEP:
        raise _refused("A sleep slot cannot be cancelled or deleted; move it instead.")
    if before.plan_kind is PlanKind.DIARY:
        raise _refused("The diary is never skipped: a diary slot cannot be cancelled or deleted; move it instead.")
    span = plan_span(before, zone)
    if span is not None and span[1] <= now:
        raise _refused(f"That slot is over {_slot_words(before, zone)}; it cannot be changed.")
    _counted(plans, before.entry_id, origin, zone, now)


def check_himari_count(plans: Sequence[LifeEntry], entry_id: int | None, zone: tzinfo, now: datetime) -> None:
    """§4.4: HIMARI's Tools change at most MAX_HIMARI_PLAN_WRITES slots a day (``entry_id``'s already counted)."""
    _counted(plans, entry_id, LifeOrigin.HIMARI, zone, now)


def check_retire(entry: LifeEntry) -> None:
    """§4.2: a sleep slot is never deleted, only moved (PLAN_REFUSED); character-diary §3: nor a diary slot."""
    if entry.kind is LifeKind.PLAN and entry.plan_kind is PlanKind.SLEEP:
        raise _refused("A sleep slot cannot be cancelled or deleted; move it instead.")
    if entry.kind is LifeKind.PLAN and entry.plan_kind is PlanKind.DIARY:
        raise _refused("The diary is never skipped: a diary slot cannot be cancelled or deleted; move it instead.")


def live_plan_of(entry: LifeEntry, persona_id: int | None = None) -> LifeEntry:
    """The entry when it is a live slot (of ``persona_id`` when given), else NOT_FOUND."""
    if not entry.live_plan or (persona_id is not None and entry.persona_id != persona_id):
        raise DataError(DataErrorCode.NOT_FOUND, f"no slot {entry.entry_id} to change")
    return entry


def _ceil_ten(moment: datetime) -> datetime:
    moment = moment.replace(second=0, microsecond=0) + (timedelta(minutes=1) if moment.second or moment.microsecond
                                                        else timedelta(0))
    return moment + timedelta(minutes=(-moment.minute) % 10)


def sleep_now(plans: Sequence[LifeEntry], *, zone: tzinfo, now: datetime,
              reason: str | None) -> tuple[LifeEntry, PlanWrite | None]:
    """§4.3, §5.2 (go_to_sleep): tonight's sleep slot and the change that makes it start now (rounded up to ten
    minutes, its end kept within the limits) — None when it is under way already. The slot is only moved, never
    made (the keeper places the night's on first need, §4.2); the request is bound to it (the keeper sleeps in no
    other). PLAN_REFUSED outside the night's start (21:00 to 05:00), with no slot tonight or one already over,
    or when a slot of the night would be covered."""
    local = now.astimezone(zone)
    night = night_of(local)
    if night is None:
        raise _refused(f"You can go to sleep early from {NIGHT_STARTS:%H:%M} (it is {local:%H:%M} now).")
    slot = sleep_slot_of(plans, night, zone)
    if slot is None:
        raise _refused("Tonight has no sleep slot yet (no room was left for one): move or cancel a slot of the "
                       "night first.")
    span = plan_span(slot, zone)
    if span is not None and span[1] <= local:  # second review 1: never the next night's for an ended one
        raise _refused(f"Tonight's sleep slot is over {_slot_words(slot, zone)}; you are up for the day.")
    if span is not None and span[0] <= local:
        return slot, None  # its time came already (it was woken): it only sleeps again
    start = _ceil_ten(local)
    if night_of(start) != night:
        start = local.replace(second=0, microsecond=0)
    first, last = (start + timedelta(minutes=MIN_SLEEP_MINUTES), start + timedelta(minutes=MAX_SLEEP_MINUTES))
    morning_by = datetime.combine(night + timedelta(days=1), SLEEP_ENDS_BY, tzinfo=zone)
    wanted = span[1] if span is not None else datetime.combine(night + timedelta(days=1), time(8, 0), tzinfo=zone)
    end = min(max(wanted, first), last, morning_by)
    end = max(end, datetime.combine(night + timedelta(days=1), SLEEP_ENDS_FROM, tzinfo=zone))
    minutes = int((end - start).total_seconds() // 60)
    for other in plans:  # character-diary §16.2: the diary slot never holds the sleep back
        other_span = plan_span(other, zone)
        if other.live_plan and other.plan_kind not in (PlanKind.SLEEP, PlanKind.DIARY) and other_span is not None \
                and _overlaps((start, end), other_span) and other_span[1] > now:
            raise _refused(f"You have the slot \"{other.text}\" {_slot_words(other, zone)} tonight; cancel or move "
                           "it before you sleep.")
    write = PlanWrite(start.date().isoformat(), _clock(start), minutes, PlanKind.SLEEP, slot.text, slot.entry_id,
                      reason=reason)
    return slot, write
