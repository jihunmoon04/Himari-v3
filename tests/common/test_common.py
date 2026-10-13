from __future__ import annotations

import copy
import dataclasses
import json
import pickle
from collections.abc import Callable
from types import ModuleType
from typing import Any

import pytest

from himari import common
from himari.common import CallId, FrozenDict, JsonValue, ToolCall, ToolId, deep_freeze


def test_dtos_are_deep_frozen(assert_api_dataclasses_frozen: Callable[[ModuleType], None]) -> None:
    assert_api_dataclasses_frozen(common)


def test_caller_mutation_after_construction_does_not_leak() -> None:
    lines: list[JsonValue] = [1, 2]
    arguments: dict[str, JsonValue] = {"path": "a.txt", "opts": {"lines": lines}}
    call = ToolCall(CallId("c1"), 0, ToolId("read_file"), arguments)
    arguments["path"] = "b.txt"
    lines.append(3)

    assert call.arguments == {"path": "a.txt", "opts": {"lines": (1, 2)}}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda a: a.__setitem__("path", "x"),
        lambda a: a.__delitem__("path"),
        lambda a: a.update(path="x"),
        lambda a: a.pop("path"),
        lambda a: a.setdefault("new", 1),
        lambda a: a.clear(),
        lambda a: a["opts"].__setitem__("lines", ()),
    ],
)
def test_holder_cannot_mutate_nested_values(mutate: Callable[[Any], object]) -> None:
    call = ToolCall(CallId("c1"), 0, ToolId("t"), {"path": "a", "opts": {"lines": [1]}})
    arguments: Any = call.arguments
    with pytest.raises(TypeError):
        mutate(arguments)
    assert isinstance(arguments["opts"]["lines"], tuple)


def test_frozen_values_still_serialize_and_copy() -> None:
    call = ToolCall(CallId("c1"), 0, ToolId("t"), {"a": {"b": [1, {"c": 2}]}})
    assert json.loads(json.dumps(call.arguments)) == {"a": {"b": [1, {"c": 2}]}}
    assert dataclasses.asdict(call)["arguments"] == {"a": {"b": (1, {"c": 2})}}
    assert copy.deepcopy(call) == call
    assert pickle.loads(pickle.dumps(call)) == call
    assert hash(call) == hash(ToolCall(CallId("c1"), 0, ToolId("t"), {"a": {"b": (1, {"c": 2})}}))


def test_deep_freeze_is_idempotent() -> None:
    frozen = deep_freeze({"a": [1, {"b": {2, 3}}]})
    assert isinstance(frozen, FrozenDict)
    assert deep_freeze(frozen) is frozen
