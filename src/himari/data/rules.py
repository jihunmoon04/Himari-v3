"""Small value rules shared by every Data backend (one behaviour, two stores)."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable, Sequence

from himari.common import ContentBlock, TextBlock, split_marks, thaw, tool_id_problem
from himari.data.api import (
    MAX_APP_PAYLOAD_BYTES,
    MAX_DEVICE_NAME_CHARS,
    MAX_EFFORT_CHARS,
    MAX_EXTERNAL_SETTINGS,
    MAX_INSTRUCTIONS_CHARS,
    MAX_MEMORY_CHARS,
    MAX_SEARCH_WORD_CHARS,
    MAX_SEARCH_WORDS,
    MAX_LIST_LIMIT,
    MAX_MODEL_ID_CHARS,
    MAX_TITLE_CHARS,
    MAX_UI_HTML_BYTES,
    MAX_WORKSPACE_LABEL_CHARS,
    MAX_WORKSPACE_LOCATION_BYTES,
    MAX_WORKSPACE_NOTE_CHARS,
    PREVIEW_CHARS,
    RESERVED_DEVICE_NAMES,
    AppView,
    AssistantFinal,
    DataError,
    DataErrorCode,
    EventText,
    InitialActivation,
    InstructionScope,
    Memory,
    MemoryCategory,
    MemoryChange,
    MemoryChangeKind,
    MessageOrigin,
    ModelSetting,
    OriginKind,
    ToolCompletion,
    ToolExecutionStatus,
    UiResource,
    UserMessage,
    ui_digest,
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


def check_activations(activations: Sequence[InitialActivation]) -> None:
    kinds = [a.environment_kind for a in activations]
    if len(set(kinds)) != len(kinds):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "one activation per Environment kind (P0)")


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
    if scope is not InstructionScope.USER:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "only the user's own instructions are offered")


def check_instructions(scope: object, text: object) -> str:
    """V3-P69 §6: the text as stored, or why not."""
    check_instruction_scope(scope)
    if not isinstance(text, str):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "instructions are text")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Before the blank check: str.strip() would take some control characters as blanks. A lone
    # surrogate (Cs) is not text either, and SQLite could not store it.
    if any(unicodedata.category(c) in ("Cc", "Cs") and c not in "\n\t" for c in text):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "instructions may not contain control characters")
    if not text.strip():
        return ""
    if len(text) > MAX_INSTRUCTIONS_CHARS:
        raise DataError(DataErrorCode.TOO_LARGE, f"instructions are at most {MAX_INSTRUCTIONS_CHARS} characters")
    return text


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


def check_origin(origin: object) -> None:
    """V3-P83 §4.2: a device origin names its device; the others name none."""
    if origin is None:
        return
    if not isinstance(origin, MessageOrigin) or not isinstance(origin.kind, OriginKind):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "not a message origin")
    if (origin.kind is OriginKind.DEVICE) != isinstance(origin.device_id, str) or origin.device_id == "":
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a device origin names its device, and only it")


def check_workspace_location(location: object) -> str:
    """V3-P74 §10.1: an absolute, normalized path (no ``.``/``..``/empty
    component, no trailing ``/`` but for the root itself), as the
    Environment's check gave it."""
    if not isinstance(location, str) or not location.startswith("/"):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a workspace location is an absolute path")
    if (len(location.encode("utf-8", errors="replace")) > MAX_WORKSPACE_LOCATION_BYTES
            or any(unicodedata.category(c) in ("Cc", "Cs") for c in location)):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "not a usable workspace location")
    parts = location.split("/")[1:]
    if location != "/" and any(p in ("", ".", "..") for p in parts):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a workspace location is a normalized path")
    return location


def check_workspace_label(label: object) -> str:
    if not isinstance(label, str):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a workspace label is text")
    label = " ".join(label.split())
    if not label or len(label) > MAX_WORKSPACE_LABEL_CHARS or any(unicodedata.category(c) in ("Cc", "Cs")
                                                                  for c in label):
        raise DataError(DataErrorCode.INVALID_ARGUMENT,
                        f"a workspace label is 1..{MAX_WORKSPACE_LABEL_CHARS} characters of one line")
    return label


def check_workspace_filesystem(filesystem: object) -> str | None:
    """A filesystem kind as mountinfo names it (``ext4``, ``fuse.rclone``)."""
    if filesystem is None:
        return None
    if not isinstance(filesystem, str) or not re.fullmatch(r"[!-~]{1,64}", filesystem):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "not a filesystem kind")
    return filesystem


def check_workspace_note(note: object) -> str | None:
    """The note as stored; only whitespace is none."""
    if note is None:
        return None
    if not isinstance(note, str):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a workspace note is text")
    note = note.replace("\r\n", "\n").replace("\r", "\n")
    if any(unicodedata.category(c) in ("Cc", "Cs") and c not in "\n\t" for c in note):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a workspace note may not contain control characters")
    note = note.strip()
    if len(note) > MAX_WORKSPACE_NOTE_CHARS:
        raise DataError(DataErrorCode.TOO_LARGE, f"a workspace note is at most {MAX_WORKSPACE_NOTE_CHARS} characters")
    return note or None


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
    return MemoryChange(MemoryChangeKind.SAVE, change.memory_id, check_memory_category(change.category),
                        check_memory_text(change.text), change.expected_revision)


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


_VIEW_STATUSES = frozenset({ToolExecutionStatus.SUCCEEDED, ToolExecutionStatus.FAILED})


def check_app_view(completion: ToolCompletion) -> AppView | None:
    """V3-P58: a view goes only with a call that ran, within the size
    limits, and its digest must be its content's."""
    view = completion.app_view
    if view is None:
        return None
    if completion.status not in _VIEW_STATUSES:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, f"a view goes only with a call that ran, not {completion.status.value}")
    check_ui_resource(view.resource)
    if len(json.dumps(thaw(view.payload), ensure_ascii=False).encode("utf-8")) > MAX_APP_PAYLOAD_BYTES:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, f"a view payload is at most {MAX_APP_PAYLOAD_BYTES} bytes")
    return view


def check_ui_resource(resource: UiResource) -> None:
    if "/" in resource.source or tool_id_problem(resource.source) is not None:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a view's source is one Tool id segment")
    if not resource.uri.startswith("ui://"):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a view's uri starts with ui://")
    if len(resource.html.encode("utf-8")) > MAX_UI_HTML_BYTES:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, f"a view's HTML is at most {MAX_UI_HTML_BYTES} bytes")
    expected = ui_digest(resource.source, resource.uri, resource.html, resource.csp, resource.prefers_border)
    if resource.digest != expected:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a view's digest does not match its content")
