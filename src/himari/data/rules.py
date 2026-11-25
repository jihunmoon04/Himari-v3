"""Small value rules shared by every Data backend (one behaviour, two stores)."""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence

from himari.common import ContentBlock, ConversationId, FrozenMap, TextBlock, split_marks, tool_id_problem
from himari.data.api import (
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
    THOUGHT_DAYS,
    INTENT_DAYS,
    NEW_ARC,
    ThoughtAbout,
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
             LifeKind.WORK: MAX_WORK_CHARS, LifeKind.THOUGHT: MAX_THOUGHT_CHARS}.get(kind, MAX_LIFE_CHARS)
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


def check_editable(entry: LifeEntry) -> None:
    """character-mind §5.2: the user deletes a kept thought, never rewrites it (INVALID_ARGUMENT)."""
    if entry.kind is LifeKind.THOUGHT:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a kept thought is only deleted")


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


def check_life_plans(plan: LifePlan) -> list[tuple[str, str, str | None, int | None]]:
    """A writer's plans as stored: each date, text and (by position in ``plan.times`` and
    ``plan.plan_arcs``) start time and project (character-work §4: an arc id or NEW_ARC)."""
    if len(plan.times) > len(plan.plans) or len(plan.plan_arcs) > len(plan.plans):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a time or a project for no plan")
    arcs = [plan.plan_arcs[i] if i < len(plan.plan_arcs) else None for i in range(len(plan.plans))]
    if any(a is not None and (isinstance(a, bool) or not isinstance(a, int) or a < NEW_ARC) for a in arcs):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a plan's project is an arc's id")
    return [(check_life_day(day), check_life_text(LifeKind.PLAN, text),
             check_time_of_day(plan.times[i] if i < len(plan.times) else None), arcs[i])
            for i, (day, text) in enumerate(plan.plans)]


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
    if pieces:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "the plan has a piece already")
    return project


def work_entry(plan: LifeEntry, project: LifeEntry, piece: WorkPiece, write_id: str, now: datetime) -> LifeEntry:
    """§4: the piece as a WORK entry (its id given as it is inserted): the plan's day and time."""
    return LifeEntry(0, plan.persona_id, LifeKind.WORK, plan.day, piece.text, LifeOrigin.WRITER, 1, now, now,
                     source=write_id, time_of_day=plan.time_of_day, title=piece.title, arc_id=project.entry_id,
                     summary=piece.summary, plan_id=plan.entry_id)


def worked_on(project: LifeEntry, plan: LifeEntry, piece: WorkPiece, now: datetime) -> LifeEntry:
    """§3, §4: the project after a piece — its "so far" rewritten and, with ``done``, closed on
    the plan's day with that ending (its title and goal stay, the user's too)."""
    worked = dataclasses.replace(project, so_far=piece.so_far, revision=project.revision + 1, updated_at=now)
    return worked if piece.done is None else dataclasses.replace(worked, end_day=plan.day, ending=piece.done)


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


def arc_starts(move: ArcMove, arcs: Sequence[LifeEntry]) -> bool:
    """§4 (W3): a START fits — fewer than MAX_OPEN_ARCS open arcs, none titled the same (folded)."""
    open_ = [a for a in arcs if a.arc_open]
    return len(open_) < MAX_OPEN_ARCS and all(fold(a.title or "") != fold(move.title or "") for a in open_)


def writer_mood(plan: LifePlan, now: datetime) -> HimariMood | None:
    """§2: the writer's mood as stored (its persona set by the write), at ``plan.mood_at`` or ``now``."""
    if plan.mood is None:
        return None
    return check_mood(0, plan.mood, MoodSource.WRITER, plan.mood_at or now, plan.mood_reason)


def kept_mood(stored: HimariMood | None, new: HimariMood) -> HimariMood:
    """§2: the newer of the two; one set after ``new`` stays."""
    return stored if stored is not None and stored.set_at > new.set_at else new
