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
cannot be evaluated counts as a violation. It runs in-process, so it is only
for schemas HIMARI itself wrote. A schema from an external server is
untrusted code for the validator: ``pattern`` runs on Python's ``re``, which
can backtrack exponentially, and nested ``anyOf``/``$ref`` can multiply the
work without any regex. ``validate_isolated`` therefore validates in a child
process under a wall-clock limit (killed when it runs over) and CPU/memory
limits, and an over-limit validation is a violation (V3-P52, PR #19 review B1).
Registration (``check_schema``) stays in-process: it checks the schema
against the trusted metaschema and only compiles patterns, never runs them.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Iterator, Mapping
from typing import Any, cast
from urllib.parse import unquote

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
ISOLATED_SECONDS = 5.0
_SRC_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DIALECTS: dict[str, type[Validator]] = {
    cls.META_SCHEMA["$schema"].rstrip("#"): cls
    for cls in (Draft7Validator, Draft201909Validator, Draft202012Validator)
}
_REF_KEYWORDS = ("$ref", "$dynamicRef", "$recursiveRef")
# Where a schema holds subschemas (Draft 2020-12, 2019-09 and draft-07). Only
# these are followed: anything else — data keywords like ``default``, and
# keywords outside the standard (``x-vendor``) — is an annotation, so a
# ``$ref`` inside it is not a reference.
_ONE = ("additionalProperties", "items", "additionalItems", "contains", "propertyNames", "if", "then", "else",
        "not", "unevaluatedItems", "unevaluatedProperties", "contentSchema")
_MANY = ("allOf", "anyOf", "oneOf", "prefixItems", "items")  # items: draft-07's array form
_NAMED = ("properties", "patternProperties", "$defs", "definitions", "dependentSchemas", "dependencies")


class SchemaError(ValueError):
    pass


def _validator_class(schema: Mapping[str, Any]) -> type[Validator]:
    dialect = schema.get("$schema")
    if dialect is None:
        return Draft202012Validator
    if not isinstance(dialect, str) or dialect.rstrip("#") not in _DIALECTS:
        raise SchemaError(f"$: unsupported $schema {dialect!r} (draft-07, 2019-09 or 2020-12)")
    return _DIALECTS[dialect.rstrip("#")]


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _subschemas(schema: object, pointer: str = "") -> Iterator[tuple[str, object]]:
    """(JSON pointer, schema) for the schema and every subschema in it."""
    yield pointer, schema
    if not isinstance(schema, dict):
        return  # a boolean schema
    for key in _ONE:
        if isinstance(schema.get(key), (dict, bool)):
            yield from _subschemas(schema[key], f"{pointer}/{key}")
    for key in _MANY:
        if isinstance(schema.get(key), list):
            for i, sub in enumerate(schema[key]):
                yield from _subschemas(sub, f"{pointer}/{key}/{i}")
    for key in _NAMED:
        if isinstance(schema.get(key), dict):
            for name, sub in schema[key].items():
                if isinstance(sub, (dict, bool)):  # draft-07 dependencies may also be arrays of names
                    yield from _subschemas(sub, f"{pointer}/{_escape(key)}/{_escape(name)}")


def check_schema(schema: JsonValue) -> None:
    """Fail closed: the schema must be valid for its dialect, small enough,
    and every reference must be local and resolve to a subschema (not to a
    data value such as a ``default``). Raises SchemaError."""
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
    nodes = list(_subschemas(plain))
    pointers = {pointer for pointer, _ in nodes}
    objects = {id(node) for _, node in nodes if isinstance(node, dict)}
    for pointer, node in nodes:
        if not isinstance(node, dict):
            continue
        if "$id" in node and pointer:
            raise SchemaError(f"#{pointer}: only the root may set $id")
        for key in _REF_KEYWORDS:
            if key not in node:
                continue
            ref = node[key]
            if not isinstance(ref, str) or not ref.startswith("#"):
                raise SchemaError(f"#{pointer}: {key} must be a local reference ('#...'), got {ref!r}")
            try:
                target = resolver.lookup(ref).contents
            except Unresolvable:
                raise SchemaError(f"#{pointer}: {key} {ref!r} does not resolve") from None
            fragment = unquote(ref[1:])
            if fragment == "" or fragment.startswith("/"):
                ok = fragment in pointers
            else:  # an anchor
                ok = isinstance(target, dict) and id(target) in objects
            if not ok:
                raise SchemaError(f"#{pointer}: {key} {ref!r} does not point to a schema")


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


async def validate_isolated(
    value: JsonValue, schema: Mapping[str, JsonValue], *, timeout: float | None = None
) -> list[str]:
    """``validate`` in a child process that is killed after ``timeout``
    seconds (default ``ISOLATED_SECONDS``); for schemas HIMARI did not write.
    Never raises. The child gets no environment but the import path (no
    credentials)."""
    request = json.dumps({"schema": thaw(schema), "value": thaw(value)}).encode()
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-s", "-m", "himari.tool._schema_worker",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            env={"PYTHONPATH": _SRC_ROOT},
        )
    except OSError:
        return ["$: the input schema could not be evaluated"]
    try:
        out, _ = await asyncio.wait_for(process.communicate(request), ISOLATED_SECONDS if timeout is None else timeout)
    except TimeoutError:
        return ["$: validating the arguments took too long"]
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    try:
        violations = json.loads(out)
    except ValueError:
        violations = None
    if process.returncode != 0 or not isinstance(violations, list) or not all(isinstance(v, str) for v in violations):
        return ["$: the input schema could not be evaluated"]
    return violations
