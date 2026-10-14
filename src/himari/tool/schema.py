"""JSON Schema for model-supplied Tool arguments (V3-P52).

A full validator (``jsonschema``): every standard validation keyword is
enforced, so a Tool's schema — ours, or one an external server such as an
MCP server declares — is never silently half-checked. Keywords outside the
standard are annotations, as the standard says; ``format`` is an annotation
too (not asserted). Draft 2020-12 is the dialect unless ``$schema`` names
draft-07 or 2019-09; any other ``$schema`` is refused.

Registration (``check_schema``) fails closed beyond the metaschema:

- references (``$ref``/``$dynamicRef``/``$recursiveRef``) must be local
  (``#...``) and must resolve, so validation never reaches for the network
  and never fails later on a dangling reference; only the root may set ``$id``;
- the schema's JSON is at most ``MAX_SCHEMA_BYTES``.

``validate`` returns readable violations and never raises: a schema that
cannot be evaluated counts as a violation. ``pattern`` uses Python's ``re``
without a time limit; the risk of a pathological pattern is accepted
(decisions V3-P52).
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from typing import Any, cast

from jsonschema import Draft7Validator, Draft201909Validator, Draft202012Validator
from jsonschema.exceptions import SchemaError as _MetaschemaError
from jsonschema.protocols import Validator
from referencing import Registry
from referencing.exceptions import Unresolvable
from referencing.jsonschema import specification_with

from himari.common import JsonValue, thaw

MAX_SCHEMA_BYTES = 64 * 1024
MAX_VIOLATIONS = 10
MAX_MESSAGE_CHARS = 300

_DIALECTS: dict[str, type[Validator]] = {
    cls.META_SCHEMA["$schema"].rstrip("#"): cls
    for cls in (Draft7Validator, Draft201909Validator, Draft202012Validator)
}
_REF_KEYWORDS = ("$ref", "$dynamicRef", "$recursiveRef")
_DATA_KEYWORDS = frozenset({"enum", "const", "default", "examples"})  # values are instances, not schemas


class SchemaError(ValueError):
    pass


def _validator_class(schema: Mapping[str, Any]) -> type[Validator]:
    dialect = schema.get("$schema")
    if dialect is None:
        return Draft202012Validator
    if not isinstance(dialect, str) or dialect.rstrip("#") not in _DIALECTS:
        raise SchemaError(f"$: unsupported $schema {dialect!r} (draft-07, 2019-09 or 2020-12)")
    return _DIALECTS[dialect.rstrip("#")]


def _walk(value: object, path: str) -> Iterator[tuple[str, str, object]]:
    """(path, keyword, value) for every keyword of every (sub)schema object."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield path, key, item
            if key in _DATA_KEYWORDS:
                continue
            if key in ("properties", "patternProperties", "$defs", "definitions", "dependentSchemas"):
                if isinstance(item, Mapping):
                    for name, sub in item.items():
                        yield from _walk(sub, f"{path}.{key}.{name}")
            else:
                yield from _walk(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            yield from _walk(item, f"{path}[{i}]")


def check_schema(schema: JsonValue) -> None:
    """Fail closed: the schema must be valid for its dialect, small enough,
    and every reference must be local and resolvable. Raises SchemaError."""
    plain = thaw(schema)
    if not isinstance(plain, dict):
        raise SchemaError("$: schema must be an object")
    size = len(json.dumps(plain, ensure_ascii=False).encode())
    if size > MAX_SCHEMA_BYTES:
        raise SchemaError(f"$: schema is {size} bytes (at most {MAX_SCHEMA_BYTES})")
    cls = _validator_class(plain)
    try:
        cls.check_schema(plain)
    except _MetaschemaError as exc:
        where = "$" + "".join(f"[{p!r}]" for p in exc.path)
        raise SchemaError(f"{where}: {exc.message}") from None
    resolver = Registry().resolver_with_root(
        specification_with(cls.META_SCHEMA["$schema"]).create_resource(plain)
    )
    for path, key, value in _walk(plain, "$"):
        if key == "$id" and path != "$":
            raise SchemaError(f"{path}: only the root may set $id")
        if key in _REF_KEYWORDS:
            if not isinstance(value, str) or not value.startswith("#"):
                raise SchemaError(f"{path}: {key} must be a local reference ('#...'), got {value!r}")
            try:
                resolver.lookup(value)
            except Unresolvable:
                raise SchemaError(f"{path}: {key} {value!r} does not resolve") from None


def check_tool_input_schema(schema: JsonValue) -> None:
    """Tool arguments are always a JSON object."""
    check_schema(schema)
    assert isinstance(schema, Mapping)
    if schema.get("type") != "object":
        raise SchemaError("$: a Tool input_schema must have type 'object'")


def validate(value: JsonValue, schema: Mapping[str, JsonValue]) -> list[str]:
    """Returns readable violations (at most ``MAX_VIOLATIONS``); empty means
    valid. Frozen values are compared as the JSON they stand for."""
    plain_schema = thaw(schema)
    assert isinstance(plain_schema, dict)
    try:
        validator = _validator_class(plain_schema)(plain_schema)
        errors = sorted(validator.iter_errors(cast(Any, thaw(value))), key=lambda e: [str(p) for p in e.absolute_path])
    except Exception:  # noqa: BLE001 — fail closed: an unevaluable schema rejects the value
        return ["$: the input schema could not be evaluated"]
    out: list[str] = []
    for error in errors[:MAX_VIOLATIONS]:
        message = error.message
        if len(message) > MAX_MESSAGE_CHARS:
            message = message[: MAX_MESSAGE_CHARS - 1] + "…"
        out.append(f"{error.json_path}: {message}")
    if len(errors) > MAX_VIOLATIONS:
        out.append(f"… and {len(errors) - MAX_VIOLATIONS} more")
    return out
