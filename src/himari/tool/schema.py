"""Minimal JSON Schema subset for model-supplied Tool arguments.

Supported keywords: ``type``, ``properties``, ``required``,
``additionalProperties`` (bool), ``items``, ``enum`` plus annotations
(``description``, ``title``, ``default``). Any other keyword is rejected at
registration (``check_schema``), so validation is never silently partial.
Swap for a full JSON Schema validator later without changing callers.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

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


def check_schema(schema: JsonValue, path: str = "$") -> None:
    if not isinstance(schema, Mapping):
        raise SchemaError(f"{path}: schema must be an object")
    unknown = set(schema) - _KEYWORDS
    if unknown:
        raise SchemaError(f"{path}: unsupported keywords {sorted(unknown)}")
    kind = schema.get("type")
    if kind is not None and kind not in _TYPES:
        raise SchemaError(f"{path}: unsupported type {kind!r}")
    additional = schema.get("additionalProperties", True)
    if not isinstance(additional, bool):
        raise SchemaError(f"{path}: additionalProperties must be a boolean")
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        raise SchemaError(f"{path}: properties must be an object")
    for name, sub in properties.items():
        check_schema(sub, f"{path}.{name}")
    if "items" in schema:
        check_schema(schema["items"], f"{path}[]")


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
    if isinstance(enum, Sequence) and not isinstance(enum, str) and value not in enum:
        errors.append(f"{path}: must be one of {list(enum)}")
    if isinstance(value, Mapping):
        properties = schema.get("properties", {})
        assert isinstance(properties, Mapping)
        required = schema.get("required", ())
        if isinstance(required, Sequence) and not isinstance(required, str):
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
