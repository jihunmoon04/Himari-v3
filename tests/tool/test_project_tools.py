"""HIMARI's project Tools (character-routine v0.1 §11.2): ``project_start``, ``project_update`` and ``project_end``,
offered in the day session only. Each checks its arguments and hands Data its change (``plan_change``, a PROJECT_*
op for the context's persona, never one the model names); Data's limits are tested in tests/data/test_projects.py."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from testkit import SCOPE

from himari.common import CallId, JsonValue
from himari.data.api import PlanOp, ProjectWrite
from himari.tool.api import ToolContext, ToolInvocation, ToolOutcome, ToolStatus
from himari.tool.project import (
    DAY_SESSION,
    PROJECT_END,
    PROJECT_START,
    PROJECT_UPDATE,
    ProjectEnd,
    ProjectStart,
    ProjectUpdate,
    project_tools,
)

AT = datetime(2026, 11, 26, 5, 0, tzinfo=UTC)


def run(tool: ProjectStart | ProjectUpdate | ProjectEnd, arguments: dict[str, JsonValue],
        persona_id: int | None = 1) -> ToolOutcome:
    invocation = ToolInvocation(tool.definition.tool_id, CallId("c0"), arguments)
    return asyncio.run(tool.run(invocation, ToolContext(SCOPE, persona_id=persona_id)))


def said(outcome: ToolOutcome) -> str:
    return outcome.model_result.blocks[0].text  # type: ignore[union-attr]


def test_the_three_tools_are_the_day_sessions_in_order() -> None:
    tools = project_tools(time_zone="Asia/Seoul", clock=lambda: AT)
    assert [t.definition.tool_id for t in tools] == [PROJECT_START, PROJECT_UPDATE, PROJECT_END]
    assert all(t.definition.requires == DAY_SESSION for t in tools)
    assert all(t.definition.input_schema["additionalProperties"] is False for t in tools)


def test_a_start_hands_data_its_title_goal_and_reason_for_the_contexts_persona() -> None:
    tool = ProjectStart(time_zone="Asia/Seoul", clock=lambda: AT)
    outcome = run(tool, {"title": " 그림 연습 ", "goal": "풍경화 한 장", "reason": "그리고 싶어졌어"}, persona_id=3)
    assert outcome.status is ToolStatus.SUCCEEDED and said(outcome).startswith('Started project "그림 연습".')
    change = outcome.plan_change
    assert change is not None
    assert (change.op, change.persona_id, change.zone, change.at, change.project) == (
        PlanOp.PROJECT_START, 3, "Asia/Seoul", AT, ProjectWrite(title="그림 연습", goal="풍경화 한 장",
                                                               reason="그리고 싶어졌어"))


def test_an_update_and_an_end_name_the_project_by_id() -> None:
    update = run(ProjectUpdate(), {"id": "4", "goal": "한 장만", "reason": "좁혔어"})
    assert update.plan_change is not None and update.plan_change.op is PlanOp.PROJECT_UPDATE
    assert update.plan_change.project == ProjectWrite(4, goal="한 장만", reason="좁혔어")
    end = run(ProjectEnd(), {"id": 4, "ending": "다 그렸다.", "reason": "끝났어"})
    assert end.plan_change is not None and end.plan_change.op is PlanOp.PROJECT_END
    assert end.plan_change.project == ProjectWrite(4, ending="다 그렸다.", reason="끝났어")
    assert said(end) == "Ended project 4."


def test_arguments_out_of_shape_fail_with_what_is_wrong_and_change_nothing() -> None:
    cases: list[tuple[ProjectStart | ProjectUpdate | ProjectEnd, dict[str, JsonValue], str]] = [
        (ProjectStart(), {"title": " ", "goal": "g", "reason": "r"}, "title"),
        (ProjectStart(), {"title": "t", "goal": "g"}, "reason"),
        (ProjectUpdate(), {"id": "x", "title": "t", "reason": "r"}, "id"),
        (ProjectUpdate(), {"id": 4, "reason": "r"}, "new title"),
        (ProjectUpdate(), {"id": True, "title": "t", "reason": "r"}, "id"),
        (ProjectEnd(), {"id": 4, "ending": "", "reason": "r"}, "ending"),
        (ProjectEnd(), {"id": 0, "ending": "e", "reason": "r"}, "id"),
    ]
    for tool, arguments, word in cases:
        outcome = run(tool, arguments)
        assert outcome.status is ToolStatus.FAILED and word in said(outcome) and outcome.plan_change is None
    no_persona = run(ProjectStart(), {"title": "t", "goal": "g", "reason": "r"}, persona_id=None)
    assert (no_persona.status, said(no_persona)) == (ToolStatus.FAILED, "Your projects are not kept here.")
