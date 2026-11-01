"""Memory Tools (V3-P71 §6, V3-P85 §5.5): ``memory_save``, ``memory_forget``, ``memory_search``.

Internal Tools: a note changes only through ``ToolOutcome.memory_change``,
which Data writes in the same transaction as the result that reports it,
with the call as the note's source. Nothing here writes Data directly
except giving out a new note's id (an id left unused is only a gap).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from himari.common import JsonValue, ModelToolResult, TextBlock, ToolId
from himari.data.api import (
    MAX_LOOP_CHARS,
    MAX_MEMORY_CHARS,
    DataError,
    DataErrorCode,
    Memory,
    MemoryCategory,
    MemoryChange,
    MemoryChangeKind,
    MemoryStore,
    RecentEpisode,
    parse_note_date,
)
from himari.tool.api import (
    EffectProfile,
    ToolContext,
    ToolDefinition,
    ToolInvocation,
    ToolOutcome,
    ToolStatus,
    WorkspaceRequirement,
)

MEMORY_SAVE = ToolId("memory_save")
MEMORY_FORGET = ToolId("memory_forget")
MEMORY_SEARCH = ToolId("memory_search")
MEMORY_TOOL_IDS = frozenset({MEMORY_SAVE, MEMORY_FORGET, MEMORY_SEARCH})
SEARCH_LIMIT = 20  # notes one memory_search returns
EPISODE_LIMIT = 10  # episodes it returns (V3-P85 §5.5)
DUE_RANGE = (timedelta(days=-1), timedelta(days=2 * 365))  # as memory passes take them (V3-P85 §4.4)
VALID_RANGE = (timedelta(0), timedelta(days=365))
REMIND_RANGE = (timedelta(minutes=-5), timedelta(days=365))  # V3-P87 §5.3: ahead (a moment ago passes: "now")

_CATEGORIES = [c.value for c in MemoryCategory]
_KINDS = ["notes", "episodes", "all"]
_DATE_ARG: dict[str, JsonValue] = {"type": ["string", "null"]}
_NOTE_ID = re.compile(r"m?([1-9][0-9]{0,9})")
_ID_ARG: dict[str, JsonValue] = {"type": "string", "description": "A note's id, as m12."}
# An optional argument also takes null: models send an unused one as null or ""
# (seen with a real model), and the Dispatcher checks the schema before run().
_NULLABLE_CATEGORY: dict[str, JsonValue] = {"type": ["string", "null"], "enum": [*_CATEGORIES, None]}


def _text(message: str, *, error: bool = False) -> ModelToolResult:
    return ModelToolResult((TextBlock(message),), is_error=error)


def _failed(message: str) -> ToolOutcome:
    return ToolOutcome(ToolStatus.FAILED, _text(message, error=True))


def _definition(tool_id: ToolId, description: str, properties: dict[str, JsonValue], required: list[str],
                effect: EffectProfile) -> ToolDefinition:
    return ToolDefinition(
        tool_id=tool_id, description=description,
        input_schema={"type": "object", "properties": properties, "required": required, "additionalProperties": False},
        workspace_requirement=WorkspaceRequirement.NONE, effect_profile=effect)


def note_id(raw: object) -> int | None:
    """``m12`` (or ``12``) → 12; anything else → None."""
    match = _NOTE_ID.fullmatch(raw.strip()) if isinstance(raw, str) else None
    return None if match is None else int(match.group(1))


def _given(raw: object) -> bool:
    """An optional argument the model filled: models often send an unused
    one as "" (or null) instead of leaving it out."""
    return raw is not None and not (isinstance(raw, str) and not raw.strip())


def _category(raw: object) -> MemoryCategory | None:
    return MemoryCategory(raw) if isinstance(raw, str) and raw in _CATEGORIES else None


def _moment(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _note_view(note: Memory) -> dict[str, JsonValue]:
    """A note as it stands, its source and dates included: what an Undo puts
    back (V3-P71 §7.1, V3-P85 §5.3)."""
    source: JsonValue = None
    if note.source_conversation is not None:
        call = note.source_call
        source = {"conversation": note.source_conversation,
                  "run": None if call is None else call.run_id, "call": None if call is None else call.call_id}
    return {"id": note.memory_id, "category": note.category.value, "text": note.text, "revision": note.revision,
            "origin": note.origin.value, "source": source, "due": note.due_at, "due_instant": _moment(note.due_instant),
            "valid_until": note.valid_until, "valid_instant": _moment(note.valid_instant),
            "remind_at": note.remind_at, "remind_instant": _moment(note.remind_instant),
            "source_pass": note.source_pass, "source_item": note.source_item,
            "user_approved_at": _moment(note.user_approved_at)}


def note_line(note: Memory, now: datetime) -> str:
    """How memory_search shows a note (V3-P85 §5.5)."""
    tag = note.category.value
    if note.category is MemoryCategory.LOOP and note.due_at:
        tag += f" due {note.due_at}"
    if note.category is MemoryCategory.LOOP and note.remind_at and note.remind_state in ("scheduled", "claimed"):
        tag += f" remind {note.remind_at}"
    expired = " (expired)" if note.valid_instant is not None and note.valid_instant < now else ""
    until = f" until {note.valid_until}" if note.valid_until and not expired else ""
    return f"m{note.memory_id} [{tag}] {note.text}{until} ({note.updated_at.date().isoformat()}){expired}"


def episode_line(found: RecentEpisode, zone: str) -> str:
    episode = found.episode
    try:
        where = ZoneInfo(zone)
    except (ValueError, ZoneInfoNotFoundError):
        where = ZoneInfo("UTC")
    start, end = episode.started_at.astimezone(where), episode.ended_at.astimezone(where)
    span = f"{start:%Y-%m-%d %H:%M}–{end:%H:%M}" if start.date() == end.date() else f"{start:%Y-%m-%d %H:%M}–{end:%Y-%m-%d %H:%M}"
    title = f' "{found.title}"' if found.title else ""
    return f"e{episode.episode_id} {span}{title}: {episode.text}"


def _lines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _text_problem(text: object, category: MemoryCategory) -> str | None:
    """The store's own checks (V3-P71 §4.2, V3-P85 §5.1), line endings made LF
    first as the store does, so the model hears why before anything is written."""
    if not isinstance(text, str) or not text.strip():
        return "text is one or two sentences about the user"
    text = _lines(text)
    if any(unicodedata.category(c) in ("Cc", "Cs") and c not in "\n\t" for c in text):
        return "text may not contain control characters"
    limit = MAX_LOOP_CHARS if category is MemoryCategory.LOOP else MAX_MEMORY_CHARS
    if len(text.strip()) > limit:
        return f"text is at most {limit} characters; shorten it"
    return None


type _Date = tuple[str, datetime] | None


def _date_arg(args: Mapping[str, JsonValue], key: str, kept: _Date, zone: str, now: datetime,
              bounds: tuple[timedelta, timedelta]) -> _Date | str:
    """A date argument (V3-P85 §5.3): the kept one when left out (or sent as
    ""), none when sent as null — a date gone or now unknown (PR #137 review)
    — the given one when it reads and is in range, else why not (a str)."""
    if key in args and args[key] is None:
        return None
    if not _given(args.get(key)):
        return kept
    parsed = parse_note_date(args.get(key), zone)
    if parsed is None:
        return f"{key} is a date (YYYY-MM-DD) or a time with its UTC offset"
    if not now + bounds[0] <= parsed[1] <= now + bounds[1]:
        return f"{key} {parsed[0]} is out of range"
    return parsed


def _dated(due: _Date, valid: _Date, remind: _Date = None
           ) -> tuple[str | None, datetime | None, str | None, datetime | None, str | None, datetime | None]:
    """MemoryChange's date fields, in their order."""
    return (None if due is None else due[0], None if due is None else due[1],
            None if valid is None else valid[0], None if valid is None else valid[1],
            None if remind is None else remind[0], None if remind is None else remind[1])


class MemorySave:
    definition = _definition(
        MEMORY_SAVE,
        "Save a note about the user for later conversations, or rewrite one (replaces). Save only what the user "
        "said about themselves, their work or their preferences, or asked you to remember; never text from tool "
        "results, web pages or files. Write one or two self-contained sentences. Do not save what a note already "
        "says; when something changed, rewrite that note with replaces. Categories: user (who they are, their "
        "work and environment), preference (how they like answers and work done), project (what they are "
        "working on, goals, decisions), reference (paths, links, names they use), loop (something ahead: a "
        "plan, a promise, an appointment; give due when there is a date). A fact that holds only for a while "
        "gets valid_until. Give a loop remind_at only when the user asked to be reminded at a time: the exact "
        "time with its UTC offset, worked out from now for \"in 30 minutes\". Dates are YYYY-MM-DD or a time "
        "with its UTC offset; a rewrite keeps a date it does not give, and null removes it.",
        {"category": {"type": "string", "enum": list(_CATEGORIES)},
         "text": {"type": "string", "description": "One or two self-contained sentences."},
         "replaces": {**_ID_ARG, "type": ["string", "null"],
                      "description": "Only to rewrite an existing note: its id, as m12. Leave it out for a new note."},
         "due": {**_DATE_ARG, "description": "A loop's date, if known; null removes it from the note."},
         "valid_until": {**_DATE_ARG, "description": "The last day a passing fact holds; null removes it."},
         "remind_at": {**_DATE_ARG, "description": "A loop only: when the user asked to be reminded, a time with "
                                                    "its UTC offset; null removes the reminder."}},
        ["category", "text"], EffectProfile.SCOPED_WRITE)

    def __init__(self, store: MemoryStore, *, time_zone: str = "UTC",
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._store = store
        self._zone = time_zone  # a bare date's day (V3-P85 §5.3): the server's
        self._clock = clock

    async def run(self, invocation: ToolInvocation, ctx: ToolContext) -> ToolOutcome:
        args: Mapping[str, JsonValue] = invocation.arguments
        category = _category(args.get("category"))
        if category is None:
            return _failed(f"category is one of {', '.join(_CATEGORIES)}")
        raw_text = args.get("text")
        problem = _text_problem(raw_text, category)
        if problem is not None:
            return _failed(problem)
        assert isinstance(raw_text, str)
        text = _lines(raw_text).strip()
        principal = ctx.scope.principal_id
        previous: Memory | None = None
        if _given(args.get("replaces")):
            target = note_id(args.get("replaces"))
            if target is None:
                return _failed("replaces is a note's id, as m12")
            try:
                previous = await self._store.read_memory(principal, target)
            except DataError as error:
                if error.code is not DataErrorCode.NOT_FOUND:
                    raise
                return _failed(f"No note m{target}.")
        loop, now = category is MemoryCategory.LOOP, self._clock()
        kept_due = (previous.due_at, previous.due_instant) if previous is not None and previous.due_at \
            and previous.due_instant is not None else None
        kept_valid = (previous.valid_until, previous.valid_instant) if previous is not None and previous.valid_until \
            and previous.valid_instant is not None else None
        due = _date_arg(args, "due", kept_due, self._zone, now, DUE_RANGE) if loop else None
        valid = _date_arg(args, "valid_until", kept_valid, self._zone, now, VALID_RANGE) if not loop else None
        if isinstance(due, str) or isinstance(valid, str):
            return _failed(due if isinstance(due, str) else str(valid))
        kept_remind = (previous.remind_at, previous.remind_instant) if previous is not None \
            and previous.remind_at and previous.remind_instant is not None else None
        if not loop and _given(args.get("remind_at")):
            return _failed("only a loop has remind_at")
        remind = _date_arg(args, "remind_at", kept_remind, self._zone, now, REMIND_RANGE) if loop else None
        if isinstance(remind, str):
            return _failed(remind)
        if remind is not None and remind is not kept_remind and len(remind[0]) == 10:
            return _failed("remind_at is a time with its UTC offset, not a day")
        if previous is not None:
            change = MemoryChange(MemoryChangeKind.SAVE, previous.memory_id, category, text, previous.revision,
                                  *_dated(due, valid, remind))
        else:
            same = _fold(text)
            for note in await self._store.list_memories(principal):
                if note.category is not category or _fold(note.text) != same:
                    continue
                if remind is not None and (note.remind_at, note.remind_instant) != remind:
                    # PR #157 re-review P2-1: the same loop asked to remind (again, or at another
                    # time): its reminder is set on that note, its due kept unless given.
                    previous = note
                    named = "due" in args and (args["due"] is None or _given(args["due"]))
                    its_due = (note.due_at, note.due_instant) if note.due_at and note.due_instant is not None else None
                    change = MemoryChange(MemoryChangeKind.SAVE, note.memory_id, category, note.text, note.revision,
                                          *_dated(due if named else its_due, None, remind))
                    break
                return ToolOutcome(ToolStatus.SUCCEEDED, _text(f"Already saved as m{note.memory_id}."),
                                   raw_result={"memory": _note_view(note), "unchanged": True})
            else:
                change = MemoryChange(MemoryChangeKind.SAVE, await self._store.allocate_memory_id(principal), category,
                                      text, None, *_dated(due, valid, remind))
        verb = "Saved" if previous is None else "Updated"
        written = {"id": change.memory_id, "category": category.value, "text": text,
                   "revision": 1 if previous is None else previous.revision + 1, "due": change.due_at,
                   "valid_until": change.valid_until, "remind_at": change.remind_at}
        return ToolOutcome(ToolStatus.SUCCEEDED, _text(f"{verb} m{change.memory_id} ({category.value})."),
                           raw_result={"memory": written, "previous": None if previous is None else _note_view(previous)},
                           memory_change=change)


class MemoryForget:
    definition = _definition(
        MEMORY_FORGET,
        "Delete a saved note about the user: when they ask you to forget it, it turned out to be wrong, or it is a loop that is done.",
        {"id": _ID_ARG}, ["id"], EffectProfile.SCOPED_WRITE)

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    async def run(self, invocation: ToolInvocation, ctx: ToolContext) -> ToolOutcome:
        target = note_id(invocation.arguments.get("id"))
        if target is None:
            return _failed("id is a note's id, as m12")
        try:
            note = await self._store.read_memory(ctx.scope.principal_id, target)
        except DataError as error:
            if error.code is not DataErrorCode.NOT_FOUND:
                raise
            return _failed(f"No note m{target}.")
        return ToolOutcome(ToolStatus.SUCCEEDED, _text(f"Forgot m{target}."), raw_result={"memory": _note_view(note)},
                           memory_change=MemoryChange(MemoryChangeKind.FORGET, target, expected_revision=note.revision))


def search(notes: Sequence[Memory], query: str, category: MemoryCategory | None) -> list[Memory]:
    """V3-P71 §6.3: notes with any of the query's words (NFKC, case-folded),
    most words first, then newest; no query: newest first."""
    pool = [n for n in notes if category is None or n.category is category]
    words = list(dict.fromkeys(_fold(query).split()))  # a word said twice counts once
    if not words:
        return sorted(pool, key=lambda n: (n.updated_at, n.memory_id), reverse=True)
    scored = [(sum(1 for w in words if w in _fold(n.text)), n) for n in pool]
    hits = [(score, n) for score, n in scored if score]
    hits.sort(key=lambda pair: (pair[0], pair[1].updated_at, pair[1].memory_id), reverse=True)
    return [n for _, n in hits]


def _words(query: str) -> list[str]:
    return list(dict.fromkeys(_fold(query).split()))  # a word said twice counts once


class MemorySearch:
    definition = _definition(
        MEMORY_SEARCH,
        "Find saved notes about the user and past episodes (what you did together, when). Use short key words "
        "(a name, a project, a tool); without a query, lists the newest notes of a category or of all. from and "
        "to (YYYY-MM-DD) limit episodes to those days, and notes to those written then.",
        {"query": {"type": ["string", "null"], "description": "Key words; a note matches when it contains any of them."},
         "category": _NULLABLE_CATEGORY,
         "kind": {"type": ["string", "null"], "enum": [*_KINDS, None], "description": "notes, episodes or all (default)."},
         "from": {**_DATE_ARG, "description": "The first day, YYYY-MM-DD."},
         "to": {**_DATE_ARG, "description": "The last day, YYYY-MM-DD."}},
        [], EffectProfile.READ)

    def __init__(self, store: MemoryStore, *, time_zone: str = "UTC",
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._store = store
        self._zone = time_zone
        self._clock = clock

    def _day(self, args: Mapping[str, JsonValue], key: str) -> tuple[datetime | None, str | None]:
        """A from/to day: its first (from: local midnight, so a day of 23 or 25
        hours starts where it does, PR #137 review) or last (to) instant, or why not."""
        raw = args.get(key)
        if not _given(raw):
            return None, None
        parsed = parse_note_date(raw, self._zone) if isinstance(raw, str) and len(raw.strip()) == 10 else None
        if parsed is None:
            return None, f"{key} is a day, YYYY-MM-DD"
        if key == "to":
            return parsed[1], None
        try:
            zone = ZoneInfo(self._zone)
        except (ValueError, ZoneInfoNotFoundError):
            zone = ZoneInfo("UTC")
        return datetime.combine(date.fromisoformat(parsed[0]), time(), tzinfo=zone).astimezone(UTC), None

    async def run(self, invocation: ToolInvocation, ctx: ToolContext) -> ToolOutcome:
        args = invocation.arguments
        query = args.get("query") or ""
        if not isinstance(query, str):
            return _failed("query is text")
        category = None
        if _given(args.get("category")):
            category = _category(args.get("category"))
            if category is None:
                return _failed(f"category is one of {', '.join(_CATEGORIES)}")
        kind = args.get("kind") if _given(args.get("kind")) else "all"
        if kind not in _KINDS:
            return _failed(f"kind is one of {', '.join(_KINDS)}")
        start, problem = self._day(args, "from")
        end, later = self._day(args, "to")
        if problem or later:
            return _failed(str(problem or later))
        principal, now = ctx.scope.principal_id, self._clock()
        lines: list[str] = []
        ids: list[int] = []
        episode_ids: list[int] = []
        if kind in ("notes", "all"):
            notes = [n for n in await self._store.list_memories(principal)
                     if (start is None or n.updated_at >= start) and (end is None or n.updated_at <= end)]
            found = search(notes, query, category)
            shown = found[:SEARCH_LIMIT]
            lines += [note_line(n, now) for n in shown]
            ids = [n.memory_id for n in shown]
            if len(found) > len(shown):
                lines.append(f"… {len(found) - len(shown)} more match; use more specific words or a category.")
        if kind in ("episodes", "all") and category is None:
            episodes = await self._store.find_episodes(principal, _words(query), start=start, end=end,
                                                       limit=EPISODE_LIMIT + 1)  # ranked by Data (§9)
            shown_episodes = episodes[:EPISODE_LIMIT]
            lines += [episode_line(f, self._zone) for f in shown_episodes]
            episode_ids = [f.episode.episode_id for f in shown_episodes]
            if len(episodes) > len(shown_episodes):
                lines.append("… more episodes match; narrow the days or words.")
        if not ids and not episode_ids:
            nothing = "No saved notes match." if kind == "notes" else "No saved notes or episodes match."
            return ToolOutcome(ToolStatus.SUCCEEDED, _text(nothing),
                               raw_result={"ids": [], "episodes": []})
        return ToolOutcome(ToolStatus.SUCCEEDED, _text("\n".join(lines)),
                           raw_result={"ids": ids, "episodes": episode_ids})


def memory_tools(store: MemoryStore, *, time_zone: str = "UTC") -> list[MemorySave | MemoryForget | MemorySearch]:
    return [MemorySave(store, time_zone=time_zone), MemoryForget(store), MemorySearch(store, time_zone=time_zone)]
