from __future__ import annotations

import dataclasses

import pytest
from testkit import FAKE, RecordingTool

from himari.common import JsonValue
from himari.tool.api import Completion
from himari.tool.registry import ToolRegistry
from himari.tool.schema import SchemaError, check_schema, validate


def test_only_ask_user_may_suspend() -> None:
    hold = RecordingTool("hold")
    hold.definition = dataclasses.replace(hold.definition, completion=Completion.SUSPENDING)
    with pytest.raises(ValueError, match="only ask_user"):
        ToolRegistry([hold])


def test_environment_tool_prefix_must_match_kind() -> None:
    with pytest.raises(ValueError, match="prefix"):
        ToolRegistry([RecordingTool("other/ping", environment_kind=FAKE)])


def test_unsupported_schema_keywords_are_rejected_at_registration() -> None:
    tool = RecordingTool("t")
    tool.definition = dataclasses.replace(tool.definition, input_schema={"type": "object", "minLength": 3})
    with pytest.raises(SchemaError, match="unsupported keywords"):
        ToolRegistry([tool])


SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "count": {"type": "integer"},
        "mode": {"enum": ["r", "w"]},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["path"],
    "additionalProperties": False,
}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"path": "a", "count": 2, "mode": "r", "tags": ["x"]}, []),
        ({}, ["$.path: required"]),
        ({"path": 1}, ["$.path: expected string"]),
        ({"path": "a", "count": True}, ["$.count: expected integer"]),
        ({"path": "a", "mode": "x"}, ["$.mode: must be one of ['r', 'w']"]),
        ({"path": "a", "tags": "x"}, ["$.tags: expected array"]),
        ({"path": "a", "tags": [1]}, ["$.tags[0]: expected string"]),
        ({"path": "a", "extra": 1}, ["$.extra: not allowed"]),
    ],
)
def test_validate_subset(value: JsonValue, expected: list[str]) -> None:
    check_schema(SCHEMA)
    assert validate(value, SCHEMA) == expected
