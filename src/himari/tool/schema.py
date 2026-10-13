"""Minimal JSON Schema subset for model-supplied Tool arguments.

Supported keywords: ``type``, ``properties``, ``required``,
``additionalProperties`` (bool), ``items``, ``enum`` plus annotations
(``description``, ``title``, ``default``). Any other keyword is rejected at
registration (``check_schema``), and every supported keyword's value is
checked too (fail closed), so validation is never silently partial or wrong.
Swap for a full JSON Schema validator later without changing callers.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TypeGuard

from himari.common import JsonValue

_KEYWORDS = frozenset(
    {"type", "properties", "required", "additionalProperties", "items", "enum", "description", "title", "default"}
)
_TYPES: dict[str, tuple[type, ...]] = {
    "object": (Mapping,),
    "array": (Sequence,),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
}


class SchemaError(ValueError):
    pass


_OBJECT_ONLY = ("properties", "required", "additionalProperties")


def check_schema(schema: JsonValue, path: str = "$") -> None:
    """Fail closed: every supported keyword must have a well-formed value, and
    keywords must fit the declared ``type``. Raises SchemaError otherwise."""
    if not isinstance(schema, Mapping):
        raise SchemaError(f"{path}: schema must be an object")
    unknown = set(schema) - _KEYWORDS
    if unknown:
        raise SchemaError(f"{path}: unsupported keywords {sorted(unknown)}")

    kind = schema.get("type")
    if kind is not None and (not isinstance(kind, str) or kind not in _TYPES):
        raise SchemaError(f"{path}: type must be one of {sorted(_TYPES)}")
    for key in ("description", "title"):
        if key in schema and not isinstance(schema[key], str):
            raise SchemaError(f"{path}: {key} must be a string")
    for key in _OBJECT_ONLY:
        if key in schema and kind != "object":
            raise SchemaError(f"{path}: {key} requires type 'object'")
    if "items" in schema and kind != "array":
        raise SchemaError(f"{path}: items requires type 'array'")

    if "enum" in schema:
        enum = schema["enum"]
        if not _is_array(enum) or not enum:
            raise SchemaError(f"{path}: enum must be a non-empty array")
        if kind is not None and any(validate(v, {"type": kind}) for v in enum):
            raise SchemaError(f"{path}: enum values must match type {kind!r}")

    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        raise SchemaError(f"{path}: properties must be an object")
    for name, sub in properties.items():
        check_schema(sub, f"{path}.{name}")

    if "required" in schema:
        required = schema["required"]
        if not _is_array(required) or not all(isinstance(n, str) for n in required):
            raise SchemaError(f"{path}: required must be an array of strings")
        if len(set(required)) != len(required):
            raise SchemaError(f"{path}: required has duplicates")
        undeclared = [n for n in required if n not in properties]
        if undeclared:
            raise SchemaError(f"{path}: required names not in properties: {undeclared}")

    if "additionalProperties" in schema and not isinstance(schema["additionalProperties"], bool):
        raise SchemaError(f"{path}: additionalProperties must be a boolean")

    if "items" in schema:
        check_schema(schema["items"], f"{path}[]")

    if "default" in schema:
        without_default = {k: v for k, v in schema.items() if k != "default"}
        problems = validate(schema["default"], without_default, f"{path}(default)")
        if problems:
            raise SchemaError(f"{path}: default does not satisfy the schema: {problems}")


def check_tool_input_schema(schema: JsonValue) -> None:
    """Tool arguments are always a JSON object."""
    check_schema(schema)
    assert isinstance(schema, Mapping)
    if schema.get("type") != "object":
        raise SchemaError("$: a Tool input_schema must have type 'object'")


def _is_array(value: object) -> TypeGuard[Sequence[JsonValue]]:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes))


def validate(value: JsonValue, schema: Mapping[str, JsonValue], path: str = "$") -> list[str]:
    """Returns human-readable violations; empty means valid."""
    errors: list[str] = []
    kind = schema.get("type")
    if isinstance(kind, str):
        ok = isinstance(value, _TYPES[kind])
        if kind in ("integer", "number") and isinstance(value, bool):
            ok = False  # bool is an int subclass in Python, not in JSON
        if kind == "array" and isinstance(value, str):
            ok = False
        if not ok:
            return [f"{path}: expected {kind}"]
    enum = schema.get("enum")
    if _is_array(enum) and value not in enum:
        errors.append(f"{path}: must be one of {list(enum)}")
    if isinstance(value, Mapping):
        properties = schema.get("properties", {})
        assert isinstance(properties, Mapping)
        required = schema.get("required", ())
        if _is_array(required):
            errors.extend(f"{path}.{name}: required" for name in required if name not in value)
        for name, item in value.items():
            sub = properties.get(name)
            if isinstance(sub, Mapping):
                errors.extend(validate(item, sub, f"{path}.{name}"))
            elif schema.get("additionalProperties", True) is False:
                errors.append(f"{path}.{name}: not allowed")
    elif isinstance(value, Sequence) and not isinstance(value, str):
        items = schema.get("items")
        if isinstance(items, Mapping):
            for i, item in enumerate(value):
                errors.extend(validate(item, items, f"{path}[{i}]"))
    return errors
