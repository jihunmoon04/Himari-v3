"""HIMARI's project Tools (character-routine v0.1 §11.2): ``project_start``, ``project_update`` and ``project_end``,
offered only in the day session (``requires="day_session"``), right after the schedule Tools.

Internal Tools on the schedule Tools' path: the change goes out as ``ToolOutcome.plan_change`` (a PROJECT_* op),
which Data checks (``rules.project_changed``: at most MAX_OPEN_ARCS open, never ended on the day it started, the
user's title and goal kept, HIMARI's day count) and keeps in the same transaction as the result that reports it
(origin HIMARI). A change past the limits comes back as the call's failure with Data's sentence. The persona is the
one the Driver gives the call's context, never one the model names. A project's "so far" is the pieces' to write,
never these Tools'.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from himari.common import JsonValue, ModelToolResult, TextBlock, ToolId
from himari.data.api import MAX_ARC_CHARS, MAX_ARC_TITLE_CHARS, PlanChange, PlanOp, ProjectWrite
from himari.tool.api import (
    EffectProfile,
    ToolContext,
    ToolDefinition,
    ToolInvocation,
    ToolOutcome,
    ToolStatus,
)

PROJECT_START = ToolId("project_start")
PROJECT_UPDATE = ToolId("project_update")
PROJECT_END = ToolId("project_end")
PROJECT_TOOL_IDS = frozenset({PROJECT_START, PROJECT_UPDATE, PROJECT_END})
DAY_SESSION = "day_session"  # the capability they require (prompt.api.DAY_SESSION)
NOT_KEPT = "Your projects are not kept here."
_REASON = {"type": "string", "description": "Why, one line in your own words."}


def _text(message: str, *, error: bool = False) -> ModelToolResult:
    return ModelToolResult((TextBlock(message),), is_error=error)


def _failed(message: str) -> ToolOutcome:
    return ToolOutcome(ToolStatus.FAILED, _text(message, error=True))


def _definition(tool_id: ToolId, description: str, properties: dict[str, JsonValue],
                required: list[str]) -> ToolDefinition:
    return ToolDefinition(
        tool_id=tool_id, description=description,
        input_schema={"type": "object", "properties": properties, "required": required, "additionalProperties": False},
        effect_profile=EffectProfile.SCOPED_WRITE, requires=DAY_SESSION)


def _line(raw: object) -> str | None:
    """A given one-line text, trimmed; None when missing or blank."""
    return raw.strip() if isinstance(raw, str) and raw.strip() else None


def _project_id(raw: object) -> int | None:
    """``3`` or ``"3"`` → 3; anything else → None."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    if isinstance(raw, str) and raw.strip().isascii() and raw.strip().isdigit() and len(raw.strip()) < 12:
        return int(raw.strip()) or None
    return None


class _Project:
    definition: ToolDefinition  # each Tool's own

    def __init__(self, *, time_zone: str = "UTC", clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._zone_name = time_zone
        self._clock = clock

    def _change(self, ctx: ToolContext, op: PlanOp, write: ProjectWrite) -> PlanChange:
        assert ctx.persona_id is not None
        return PlanChange(op, ctx.persona_id, self._zone_name, self._clock(), project=write)


class ProjectStart(_Project):
    definition = _definition(
        PROJECT_START,
        "Start a new project of your own: something you make yourself over weeks, out of who you are (never the "
        f"user's own matters). title: a few words (at most {MAX_ARC_TITLE_CHARS} characters); goal: one plain line "
        f"(at most {MAX_ARC_CHARS}). At most 2 open at once. Then give it time with schedule_write (kind work, its "
        "id from himari_life.arcs).",
        {"title": {"type": "string"}, "goal": {"type": "string"}, "reason": _REASON}, ["title", "goal", "reason"])

    async def run(self, invocation: ToolInvocation, ctx: ToolContext) -> ToolOutcome:
        if ctx.persona_id is None:
            return _failed(NOT_KEPT)
        args: Mapping[str, JsonValue] = invocation.arguments
        title, goal, reason = _line(args.get("title")), _line(args.get("goal")), _line(args.get("reason"))
        if title is None:
            return _failed("title is a few words")
        if goal is None:
            return _failed("goal is one plain line")
        if reason is None:
            return _failed("reason is one line: why you start it")
        return ToolOutcome(ToolStatus.SUCCEEDED,
                           _text(f"Started project \"{title}\". Its id shows in himari_life.arcs; give it time with "
                                 "schedule_write (kind work)."),
                           raw_result={"start": {"title": title, "goal": goal, "reason": reason}},
                           plan_change=self._change(ctx, PlanOp.PROJECT_START,
                                                    ProjectWrite(title=title, goal=goal, reason=reason)))


class ProjectUpdate(_Project):
    definition = _definition(
        PROJECT_UPDATE,
        "Change an open project's title or goal (give its id from himari_life.arcs, and one or both). What it has "
        "come to so far is written by your pieces, not here.",
        {"id": {"type": ["integer", "string"]}, "title": {"type": ["string", "null"]},
         "goal": {"type": ["string", "null"]}, "reason": _REASON}, ["id", "reason"])

    async def run(self, invocation: ToolInvocation, ctx: ToolContext) -> ToolOutcome:
        if ctx.persona_id is None:
            return _failed(NOT_KEPT)
        args: Mapping[str, JsonValue] = invocation.arguments
        entry_id = _project_id(args.get("id"))
        title, goal, reason = _line(args.get("title")), _line(args.get("goal")), _line(args.get("reason"))
        if entry_id is None:
            return _failed("id is a project's id, a number")
        if title is None and goal is None:
            return _failed("give a new title, a new goal, or both")
        if reason is None:
            return _failed("reason is one line: why you change it")
        return ToolOutcome(ToolStatus.SUCCEEDED, _text(f"Changed project {entry_id}."),
                           raw_result={"update": {"id": entry_id, "title": title, "goal": goal, "reason": reason}},
                           plan_change=self._change(ctx, PlanOp.PROJECT_UPDATE,
                                                    ProjectWrite(entry_id, title=title, goal=goal, reason=reason)))


class ProjectEnd(_Project):
    definition = _definition(
        PROJECT_END,
        "End an open project when it reaches its end (give its id from himari_life.arcs), with a one-line ending "
        f"(at most {MAX_ARC_CHARS} characters). Not on the day it started.",
        {"id": {"type": ["integer", "string"]}, "ending": {"type": "string"}, "reason": _REASON},
        ["id", "ending", "reason"])

    async def run(self, invocation: ToolInvocation, ctx: ToolContext) -> ToolOutcome:
        if ctx.persona_id is None:
            return _failed(NOT_KEPT)
        args: Mapping[str, JsonValue] = invocation.arguments
        entry_id = _project_id(args.get("id"))
        ending, reason = _line(args.get("ending")), _line(args.get("reason"))
        if entry_id is None:
            return _failed("id is a project's id, a number")
        if ending is None:
            return _failed("ending is one line: how it ended")
        if reason is None:
            return _failed("reason is one line: why you end it now")
        return ToolOutcome(ToolStatus.SUCCEEDED, _text(f"Ended project {entry_id}."),
                           raw_result={"end": {"id": entry_id, "ending": ending, "reason": reason}},
                           plan_change=self._change(ctx, PlanOp.PROJECT_END,
                                                    ProjectWrite(entry_id, ending=ending, reason=reason)))


def project_tools(*, time_zone: str = "UTC",
                  clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> list[_Project]:
    """§11.2: the three Tools, in the catalog's order."""
    return [cls(time_zone=time_zone, clock=clock) for cls in (ProjectStart, ProjectUpdate, ProjectEnd)]
