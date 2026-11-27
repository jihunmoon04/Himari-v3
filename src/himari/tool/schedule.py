"""HIMARI's schedule Tools (character-day v0.1 §4.3, §18.6): ``schedule_read``, ``schedule_write``, ``schedule_cancel``
and ``go_to_sleep``, offered only in the day session (``requires="day_session"``).

Internal Tools: the schedule changes only through ``ToolOutcome.plan_change``, which Data checks against the
schedule's limits and writes in the same transaction as the result that reports it (origin HIMARI). A change past the
limits comes back as the call's failure with Data's sentence, so the model can fix it and call again ("It overlaps
your sleep slot (01:10–08:40, id 12); move the sleep slot first."). The persona is the one the Driver gives the call's
context, never one the model names; without one the life is not kept here and every call fails.

character-diary §3: the night's ``diary`` slot is only moved (``schedule_write`` with its id), never made or
cancelled; Data refuses the rest with its sentence.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from himari.common import JsonValue, ModelToolResult, TextBlock, ToolId
from himari.data.api import (
    MAX_PLAN_MINUTES,
    MIN_PLAN_MINUTES,
    SCHEDULE_DAYS,
    LifeEntry,
    LifeKind,
    LifeStore,
    PlanChange,
    PlanKind,
    PlanOp,
    PlanWrite,
    plan_kind_of,
    plan_span,
)
from himari.tool.api import (
    EffectProfile,
    ToolContext,
    ToolDefinition,
    ToolInvocation,
    ToolOutcome,
    ToolStatus,
)

SCHEDULE_READ = ToolId("schedule_read")
SCHEDULE_WRITE = ToolId("schedule_write")
SCHEDULE_CANCEL = ToolId("schedule_cancel")
GO_TO_SLEEP = ToolId("go_to_sleep")
SCHEDULE_TOOL_IDS = frozenset({SCHEDULE_READ, SCHEDULE_WRITE, SCHEDULE_CANCEL, GO_TO_SLEEP})
DAY_SESSION = "day_session"  # the capability they require (prompt.api.DAY_SESSION)
NOT_KEPT = "Your schedule is not kept here."
_KINDS = [k.value for k in PlanKind]
_WEEKDAYS = "월화수목금토일"


def _text(message: str, *, error: bool = False) -> ModelToolResult:
    return ModelToolResult((TextBlock(message),), is_error=error)


def _failed(message: str) -> ToolOutcome:
    return ToolOutcome(ToolStatus.FAILED, _text(message, error=True))


def _definition(tool_id: ToolId, description: str, properties: dict[str, JsonValue], required: list[str],
                effect: EffectProfile) -> ToolDefinition:
    return ToolDefinition(
        tool_id=tool_id, description=description,
        input_schema={"type": "object", "properties": properties, "required": required, "additionalProperties": False},
        effect_profile=effect, requires=DAY_SESSION)


def _given(raw: object) -> bool:
    return raw is not None and not (isinstance(raw, str) and not raw.strip())


def _slot_id(raw: object) -> int | None:
    """``12`` or ``"12"`` → 12; anything else → None."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    if isinstance(raw, str) and raw.strip().isascii() and raw.strip().isdigit() and len(raw.strip()) < 12:
        return int(raw.strip()) or None
    return None


def slot_line(entry: LifeEntry, zone: ZoneInfo) -> str:
    """One slot as the Tools show it: its id, date, times, kind and text, and whether it still stands."""
    span = plan_span(entry, zone)
    when = entry.day or "?"
    if span is not None:
        when = (f"{span[0]:%m-%d} ({_WEEKDAYS[span[0].weekday()]}) {span[0]:%H:%M}–{span[1]:%H:%M}"
                + (f" (ends {span[1]:%m-%d})" if span[1].date() != span[0].date() else ""))
    state = "" if entry.plan_state is None else f" [{entry.plan_state.value}]"
    why = f" — {entry.reason}" if entry.reason else ""
    return f"id {entry.entry_id} · {when} · {plan_kind_of(entry).value}{state} · {entry.text}{why}"


class _Schedule:
    def __init__(self, store: LifeStore, *, time_zone: str = "UTC",
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._store = store
        self._zone_name = time_zone
        self._clock = clock

    @property
    def _zone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self._zone_name)
        except (ValueError, ZoneInfoNotFoundError):
            return ZoneInfo("UTC")

    def _change(self, ctx: ToolContext, op: PlanOp, **fields: object) -> PlanChange:
        assert ctx.persona_id is not None
        return PlanChange(op, ctx.persona_id, self._zone_name, self._clock(), **fields)  # type: ignore[arg-type]


class ScheduleRead(_Schedule):
    definition = _definition(
        SCHEDULE_READ,
        "Read your own schedule from today for a number of days (1 to 7): each slot's id, date, start and end, kind "
        "(sleep, work, together, own, diary) and text, with the cancelled and missed ones and why. Today and "
        "tomorrow are "
        "in the runtime state already: read for the days after.",
        {"days": {"type": "integer", "minimum": 1, "maximum": SCHEDULE_DAYS}}, ["days"], EffectProfile.READ)

    async def run(self, invocation: ToolInvocation, ctx: ToolContext) -> ToolOutcome:
        if ctx.persona_id is None:
            return _failed(NOT_KEPT)
        days = invocation.arguments.get("days")
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= SCHEDULE_DAYS:
            return _failed(f"days is 1 to {SCHEDULE_DAYS}")
        local = self._clock().astimezone(self._zone)
        first, last = local.date().isoformat(), (local.date() + timedelta(days=days - 1)).isoformat()
        slots = sorted((e for e in await self._store.list_life(ctx.scope.principal_id, ctx.persona_id, since=first,
                                                                now=self._clock())
                        if e.kind is LifeKind.PLAN and first <= (e.day or "") <= last),
                       key=lambda e: (e.day or "", e.time_of_day or "", e.entry_id))
        if not slots:
            return ToolOutcome(ToolStatus.SUCCEEDED, _text(f"Nothing from {first} to {last}."), raw_result={"ids": []})
        return ToolOutcome(ToolStatus.SUCCEEDED, _text("\n".join(slot_line(e, self._zone) for e in slots)),
                           raw_result={"ids": [e.entry_id for e in slots]})


class ScheduleWrite(_Schedule):
    definition = _definition(
        SCHEDULE_WRITE,
        "Write a slot of your schedule, or change one (give its id; a move too). date is YYYY-MM-DD (today to 7 days "
        "on), start HH:MM, minutes its length. kind: together (a time set with the user), own (something you do "
        "alone), work (time on an open project: give its arc id), sleep (your sleep slot: only moved, from 21:00 to "
        "05:00, 5 to 11 hours, ending by 12:00), diary (your diary time: only moved, 10 to 60 minutes, ending before "
        "your sleep slot). Nothing may lie over your sleep slot: move the sleep first. When you "
        "move or change a slot, give reason, one line in your own words.",
        {"id": {"type": ["integer", "string", "null"], "description": "The slot to change; leave it out for a new one."},
         "date": {"type": "string", "description": "YYYY-MM-DD, of its start."},
         "start": {"type": "string", "description": "HH:MM, 24-hour."},
         "minutes": {"type": "integer", "minimum": MIN_PLAN_MINUTES, "maximum": MAX_PLAN_MINUTES},
         "kind": {"type": "string", "enum": list(_KINDS)},
         "text": {"type": "string", "description": "One short line in your own voice."},
         "arc": {"type": ["integer", "null"], "description": "A work slot's project (an open arc's id)."},
         "reason": {"type": ["string", "null"], "description": "Why you moved or changed it."}},
        ["date", "start", "minutes", "kind", "text"], EffectProfile.SCOPED_WRITE)

    async def run(self, invocation: ToolInvocation, ctx: ToolContext) -> ToolOutcome:
        if ctx.persona_id is None:
            return _failed(NOT_KEPT)
        args: Mapping[str, JsonValue] = invocation.arguments
        entry_id = _slot_id(args.get("id")) if _given(args.get("id")) else None
        if _given(args.get("id")) and entry_id is None:
            return _failed("id is a slot's id, a number")
        day, start, text = args.get("date"), args.get("start"), args.get("text")
        try:
            date.fromisoformat(str(day))
        except ValueError:
            return _failed("date is YYYY-MM-DD")
        if not isinstance(start, str) or len(start.strip()) != 5 or start.strip()[2] != ":":
            return _failed("start is HH:MM")
        minutes, kind = args.get("minutes"), args.get("kind")
        if isinstance(minutes, bool) or not isinstance(minutes, int):
            return _failed(f"minutes is a number, {MIN_PLAN_MINUTES} to {MAX_PLAN_MINUTES}")
        if kind not in _KINDS:
            return _failed(f"kind is one of {', '.join(_KINDS)}")
        if not isinstance(text, str) or not text.strip():
            return _failed("text is one short line")
        arc = args.get("arc")
        reason = args.get("reason")
        write = PlanWrite(str(day), start.strip(), minutes, PlanKind(str(kind)), text.strip(), entry_id,
                          arc if isinstance(arc, int) and not isinstance(arc, bool) else None,
                          reason.strip() if isinstance(reason, str) and reason.strip() else None)
        verb = "Written" if entry_id is None else f"Changed slot {entry_id}"
        end = (datetime.fromisoformat(f"{day}T{start.strip()}") + timedelta(minutes=minutes)).strftime("%H:%M") \
            if start.strip()[:2].isdigit() and start.strip()[3:].isdigit() else "?"
        return ToolOutcome(ToolStatus.SUCCEEDED,
                           _text(f"{verb}: {day} {start.strip()}–{end} {kind} \"{text.strip()}\"."
                                 + (" Its id shows in your schedule." if entry_id is None else "")),
                           raw_result={"write": {"id": entry_id, "date": str(day), "start": start.strip(),
                                                 "minutes": minutes, "kind": str(kind), "text": text.strip()}},
                           plan_change=self._change(ctx, PlanOp.WRITE, write=write))


class ScheduleCancel(_Schedule):
    definition = _definition(
        SCHEDULE_CANCEL,
        "Cancel a slot of your schedule (not your sleep or your diary slot: move those instead), with your reason in "
        "one line. It stays in the schedule as cancelled.",
        {"id": {"type": ["integer", "string"]}, "reason": {"type": "string"}}, ["id", "reason"],
        EffectProfile.SCOPED_WRITE)

    async def run(self, invocation: ToolInvocation, ctx: ToolContext) -> ToolOutcome:
        if ctx.persona_id is None:
            return _failed(NOT_KEPT)
        entry_id = _slot_id(invocation.arguments.get("id"))
        reason = invocation.arguments.get("reason")
        if entry_id is None:
            return _failed("id is a slot's id, a number")
        if not isinstance(reason, str) or not reason.strip():
            return _failed("reason is one line: why you cancel it")
        return ToolOutcome(ToolStatus.SUCCEEDED, _text(f"Cancelled slot {entry_id}."),
                           raw_result={"cancel": {"id": entry_id, "reason": reason.strip()}},
                           plan_change=self._change(ctx, PlanOp.CANCEL, entry_id=entry_id, reason=reason.strip()))


class GoToSleep(_Schedule):
    definition = _definition(
        GO_TO_SLEEP,
        "Go to sleep once this answer is done: tonight's sleep slot starts now. Call it in the same reply as your "
        "good night whenever the user says good night (\"잘 자\", \"나 잘게\") or you part for the night. Only from "
        "21:00.",
        {"reason": {"type": ["string", "null"], "description": "Why you sleep early, one line."}}, [],
        EffectProfile.SCOPED_WRITE)

    async def run(self, invocation: ToolInvocation, ctx: ToolContext) -> ToolOutcome:
        if ctx.persona_id is None:
            return _failed(NOT_KEPT)
        reason = invocation.arguments.get("reason")
        why = reason.strip() if isinstance(reason, str) and reason.strip() else None
        return ToolOutcome(ToolStatus.SUCCEEDED, _text("You will fall asleep once this answer is done."),
                           raw_result={"sleep": {"reason": why}},
                           plan_change=self._change(ctx, PlanOp.SLEEP_NOW, reason=why))


def schedule_tools(store: LifeStore, *, time_zone: str = "UTC",
                   clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> list[_Schedule]:
    """§4.3: the four Tools, in the catalog's order."""
    return [cls(store, time_zone=time_zone, clock=clock) for cls in (ScheduleRead, ScheduleWrite, ScheduleCancel,
                                                                      GoToSleep)]
