from __future__ import annotations

import dataclasses

import pytest
from testkit import FAKE, RecordingTool

from himari.common import JsonValue, deep_freeze
from himari.tool.api import Completion, ToolOrigin, WorkspaceRequirement
from himari.tool.registry import ToolRegistry
from himari.tool.schema import MAX_SCHEMA_BYTES, MAX_VIOLATIONS, SchemaError, check_schema, validate


def test_only_ask_user_may_suspend() -> None:
    hold = RecordingTool("hold")
    hold.definition = dataclasses.replace(hold.definition, completion=Completion.SUSPENDING)
    with pytest.raises(ValueError, match="only ask_user"):
        ToolRegistry([hold])


def test_environment_tool_prefix_must_match_kind() -> None:
    with pytest.raises(ValueError, match="prefix"):
        ToolRegistry([RecordingTool("other/ping", environment_kind=FAKE)])


# --- External Tools (V3-P52) --------------------------------------------------


def test_external_tools_are_namespaced_by_their_source() -> None:
    tool = RecordingTool("calliope/position", external_source="calliope")
    assert tool.definition.origin is ToolOrigin.EXTERNAL
    assert RecordingTool("fake/x", environment_kind=FAKE).definition.origin is ToolOrigin.ENVIRONMENT
    assert RecordingTool("x").definition.origin is ToolOrigin.INTERNAL
    ToolRegistry([tool])
    with pytest.raises(ValueError, match="prefix"):
        ToolRegistry([RecordingTool("other/position", external_source="calliope")])
    with pytest.raises(ValueError, match="iff"):
        ToolRegistry([RecordingTool("position", external_source="calliope")])


def test_a_tool_is_not_both_environment_and_external() -> None:
    with pytest.raises(ValueError, match="not both"):
        ToolRegistry([RecordingTool("fake/x", environment_kind=FAKE, external_source="fake")])


def test_external_tools_need_no_workspace() -> None:
    with pytest.raises(ValueError, match="cannot require a Workspace"):
        ToolRegistry([RecordingTool("remote/x", external_source="remote", workspace=WorkspaceRequirement.DEFAULT)])


@pytest.mark.parametrize("external_first", [True, False])
def test_a_namespace_belongs_to_one_origin(external_first: bool) -> None:
    tools = [RecordingTool("fake/remote", external_source="fake"), RecordingTool("fake/env", environment_kind=FAKE)]
    with pytest.raises(ValueError, match="already belongs"):
        ToolRegistry(tools if external_first else tools[::-1])


def test_external_tools_are_always_in_the_catalog() -> None:
    registry = ToolRegistry([RecordingTool("remote/x", external_source="remote"),
                             RecordingTool("fake/y", environment_kind=FAKE)])
    assert [d.tool_id for d in registry.catalog(frozenset())] == ["remote/x"]


# --- Input schemas ------------------------------------------------------------


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
        ({}, ["$: 'path' is a required property"]),
        ({"path": 1}, ["$.path: 1 is not of type 'string'"]),
        ({"path": "a", "count": True}, ["$.count: True is not of type 'integer'"]),
        ({"path": "a", "count": 2.0}, []),  # JSON: 2.0 is an integer
        ({"path": "a", "mode": "x"}, ["$.mode: 'x' is not one of ['r', 'w']"]),
        ({"path": "a", "tags": "x"}, ["$.tags: 'x' is not of type 'array'"]),
        ({"path": "a", "tags": [1]}, ["$.tags[0]: 1 is not of type 'string'"]),
        ({"path": "a", "extra": 1}, ["$: Additional properties are not allowed ('extra' was unexpected)"]),
    ],
)
def test_validate(value: JsonValue, expected: list[str]) -> None:
    check_schema(SCHEMA)
    assert validate(value, SCHEMA) == expected
    assert validate(deep_freeze(value), deep_freeze(SCHEMA)) == expected  # frozen values validate as their JSON


# What an MCP server built on the Python SDK (pydantic) declares for
# ``def analyse(fen: str, depth: int = 18, side: Side | None = None, line: list[Move] = [])``.
PYDANTIC: dict[str, JsonValue] = {
    "$defs": {
        "Move": {"type": "object", "properties": {"uci": {"type": "string", "pattern": "^[a-h][1-8][a-h][1-8][qrbn]?$"}},
                 "required": ["uci"], "title": "Move"},
        "Side": {"enum": ["white", "black"], "title": "Side", "type": "string"},
    },
    "type": "object",
    "properties": {
        "fen": {"title": "Fen", "type": "string", "minLength": 1},
        "depth": {"default": 18, "title": "Depth", "type": "integer", "minimum": 1, "maximum": 30},
        "side": {"anyOf": [{"$ref": "#/$defs/Side"}, {"type": "null"}], "default": None},
        "line": {"type": "array", "items": {"$ref": "#/$defs/Move"}, "default": []},
    },
    "required": ["fen"],
    "title": "analyseArguments",
    "discriminator": {"propertyName": "kind"},  # outside the standard: an annotation
}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"fen": "x", "depth": 20, "side": None, "line": [{"uci": "e2e4"}]}, []),
        ({"fen": "x", "side": "white"}, []),
        ({"fen": ""}, ["$.fen: '' should be non-empty"]),
        ({"fen": "x", "depth": 99}, ["$.depth: 99 is greater than the maximum of 30"]),
        ({"fen": "x", "side": "red"}, ["$.side: 'red' is not valid under any of the given schemas"]),
        ({"fen": "x", "line": [{"uci": "e9"}]}, ["$.line[0].uci: 'e9' does not match '^[a-h][1-8][a-h][1-8][qrbn]?$'"]),
    ],
)
def test_standard_schemas_from_external_servers_are_fully_enforced(value: JsonValue, expected: list[str]) -> None:
    check_schema(PYDANTIC)
    assert validate(value, PYDANTIC) == expected


def test_draft_07_is_accepted() -> None:
    schema: dict[str, JsonValue] = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "definitions": {"n": {"type": "integer"}},
        "properties": {"n": {"$ref": "#/definitions/n"}},
    }
    check_schema(schema)
    assert validate({"n": "1"}, schema) == ["$.n: '1' is not of type 'integer'"]


@pytest.mark.parametrize(
    ("schema", "reason"),
    [
        # re-review B3: intended "command is required", silently not enforced before
        ({"type": "object", "properties": {"command": {"type": "string"}}, "required": "command"},
         "is not of type 'array'"),
        ({"type": "object", "required": ["x", "x"]}, "non-unique"),
        ({"type": "object", "additionalProperties": "no"}, "is not of type 'object', 'boolean'"),
        ({"type": "object", "properties": ["x"]}, "is not of type 'object'"),
        ({"type": "str"}, "is not valid under any of the given schemas"),
        ({"type": "array", "items": "string"}, "is not of type 'object', 'boolean'"),
        ({"description": 5}, "is not of type 'string'"),
        ({"type": "string", "pattern": "("}, "is not a 'regex'"),
        ({"$schema": "http://json-schema.org/draft-04/schema#"}, "unsupported \\$schema"),
        # references stay inside the schema and must resolve: validation never
        # reaches for the network and never fails later on a dangling one
        ({"properties": {"a": {"$ref": "https://example.com/a.json"}}}, "must be a local reference"),
        ({"properties": {"a": {"$ref": "#/$defs/missing"}}}, "does not resolve"),
        ({"$defs": {"a": {"$id": "https://example.com/a", "type": "string"}}}, "only the root may set \\$id"),
        ({"description": "x" * MAX_SCHEMA_BYTES}, "bytes \\(at most"),
        ("object", "schema must be an object"),
    ],
)
def test_malformed_schemas_fail_closed(schema: JsonValue, reason: str) -> None:
    with pytest.raises(SchemaError, match=reason):
        check_schema(schema)


def test_data_keywords_are_not_mistaken_for_references() -> None:
    check_schema({"type": "object", "default": {"$ref": "https://example.com"},
                  "properties": {"$ref": {"type": "string"}}})


def test_tool_input_schema_must_be_an_object_schema() -> None:
    tool = RecordingTool("t")
    tool.definition = dataclasses.replace(tool.definition, input_schema={"type": "string"})
    with pytest.raises(SchemaError, match="type 'object'"):
        ToolRegistry([tool])


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
        (1.0, [1], True),  # JSON numbers compare by value
        (True, [True], True),
        ({"a": [1, {"b": None}]}, [{"a": [1.0, {"b": None}]}], True),
        ("r", ["r", "w"], True),
    ],
)
def test_enum_uses_json_equality(value: JsonValue, options: list[JsonValue], accepted: bool) -> None:
    assert (validate(value, {"enum": options}) == []) is accepted


def test_validation_output_is_bounded() -> None:
    schema: dict[str, JsonValue] = {"type": "object", "additionalProperties": {"type": "integer"}}
    violations = validate({f"k{i:02}": "x" * 1000 for i in range(MAX_VIOLATIONS + 3)}, schema)
    assert len(violations) == MAX_VIOLATIONS + 1 and violations[-1] == "… and 3 more"
    assert all(len(v) < 400 for v in violations)


def test_an_unevaluable_schema_rejects_rather_than_raises() -> None:
    assert validate({}, {"$schema": "https://example.com/unknown"}) == ["$: the input schema could not be evaluated"]
