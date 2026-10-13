from __future__ import annotations

import dataclasses

import pytest
from testkit import FAKE, RecordingTool

from himari.common import JsonValue
from himari.tool.api import Completion
from himari.tool.registry import ToolRegistry
from himari.tool.schema import SchemaError, check_schema, json_equal, validate


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


@pytest.mark.parametrize(
    ("schema", "reason"),
    [
        # re-review B3: intended "command is required", silently not enforced before
        ({"type": "object", "properties": {"command": {"type": "string"}}, "required": "command"},
         "required must be an array of strings"),
        ({"type": "object", "properties": {"x": {}}, "required": ["x", "x"]}, "duplicates"),
        ({"type": "object", "properties": {"x": {}}, "required": ["y"]}, "not in properties"),
        ({"type": "object", "properties": {"x": {}}, "required": [1]}, "array of strings"),
        ({"type": "object", "additionalProperties": "no"}, "must be a boolean"),
        ({"type": "object", "properties": ["x"]}, "properties must be an object"),
        ({"type": ["string", "null"]}, "type must be one of"),
        ({"type": "str"}, "type must be one of"),
        ({"enum": "ab"}, "non-empty array"),
        ({"enum": []}, "non-empty array"),
        ({"type": "integer", "enum": [1, "2"]}, "enum values must match"),
        ({"type": "string", "properties": {"x": {}}}, "requires type 'object'"),
        ({"required": ["x"]}, "requires type 'object'"),
        ({"type": "object", "items": {}}, "requires type 'array'"),
        ({"type": "array", "items": "string"}, "schema must be an object"),
        ({"description": 5}, "description must be a string"),
        ({"type": "integer", "default": "1"}, "default does not satisfy"),
        ({"type": "object", "properties": {"n": {"type": "integer", "minimum": 0}}}, "unsupported keywords"),
    ],
)
def test_malformed_schemas_fail_closed(schema: JsonValue, reason: str) -> None:
    with pytest.raises(SchemaError, match=reason):
        check_schema(schema)


def test_tool_input_schema_must_be_an_object_schema() -> None:
    tool = RecordingTool("t")
    tool.definition = dataclasses.replace(tool.definition, input_schema={"type": "string"})
    with pytest.raises(SchemaError, match="type 'object'"):
        ToolRegistry([tool])


@pytest.mark.parametrize(
    "schema",
    [
        SCHEMA,
        {"type": "object"},
        {"type": "object", "properties": {"mode": {"type": "string", "enum": ["r", "w"], "default": "r"}}},
        {"type": "array", "items": {"type": "integer"}, "description": "ids"},
    ],
)
def test_well_formed_schemas_are_accepted(schema: JsonValue) -> None:
    check_schema(schema)


@pytest.mark.parametrize(
    ("value", "options", "accepted"),
    [
        (True, [1], False),  # Python True == 1; JSON true != 1
        (1, [True], False),
        (False, [0], False),
        (0.0, [False], False),
        ({"a": True}, [{"a": 1}], False),
        ([1, 0], [[True, False]], False),
        (None, [0], False),
        ("1", [1], False),
        (["a"], ["a"], False),
        ({"a": 1}, [[("a", 1)]], False),
        (1.0, [1], True),  # JSON numbers compare by value
        (True, [True], True),
        ({"a": [1, {"b": None}]}, [{"a": [1.0, {"b": None}]}], True),
        ("r", ["r", "w"], True),
    ],
)
def test_enum_uses_json_equality(value: JsonValue, options: list[JsonValue], accepted: bool) -> None:
    assert (validate(value, {"enum": options}) == []) is accepted


def test_enum_type_check_at_registration_uses_json_semantics() -> None:
    with pytest.raises(SchemaError, match="enum values must match"):
        check_schema({"type": "integer", "enum": [1, True]})
    assert json_equal(1, 1.0) and not json_equal(1, True)
