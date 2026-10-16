"""Whether a URI is one an RFC 6570 URI template expands to (V3-P60 §5).

Used to *allow* reads, so it errs towards "no": a URI is accepted only if
some expansion of the template's variables gives exactly that URI, checked
by structure, not by character ranges alone (PR #42 review B1):

- ``{?a,b}``, ``{&a}``, ``{;a}``: only the declared names, in their order,
  each once unless exploded (``{?list*}``: ``list=1&list=2``). An exploded
  variable is taken as a list: the arbitrary names of an exploded
  associative array (``{?keys*}`` → ``x=1&y=2``) are not accepted.
- ``{/a,b}``, ``{.a}``: at most one segment per variable unless exploded.
- ``{id:3}``: at most 3 characters (a percent-encoded octet counts as one).
- Values contain only what the operator lets through unencoded: unreserved
  characters and ``%XX`` (and reserved characters for ``{+…}``, ``{#…}``);
  a ``,`` only where the expansion can write one: between variables or the
  items of a list that is not exploded, never in a prefixed or exploded
  (``?``, ``&``, ``;``, ``/``, ``.``) value unless reserved characters pass.

**Bounded time (B2).** Matching walks the template once and never
backtracks: an expression's expansion ends where the next literal first
occurs, or — for an expression followed directly by another — at the first
character the expression cannot contain, which must be the next one's
leading character. A template where that boundary is not certain (``{a}{b}``,
``{a}{.b}``, ``{+a}{?b}``) is not usable for allowing (``parse`` gives None).
So a match costs time linear in the URI's length, and a URI that only an
unusual split of the values would give is refused — the safe side.
"""

from __future__ import annotations

import string
from dataclasses import dataclass

MAX_TEMPLATE_CHARS = 2048
MAX_PREFIX = 9999  # RFC 6570: a prefix length is below 10000

_UNRESERVED = frozenset(string.ascii_letters + string.digits + "-._~")
_RESERVED = frozenset(":/?#[]@!$&'()*+,;=")
_HEX = frozenset(string.hexdigits)
_VARCHARS = frozenset(string.ascii_letters + string.digits + "_.%")


@dataclass(frozen=True)
class _Operator:
    first: str  # what a non-empty expansion starts with
    separator: str
    named: bool  # name=value pairs
    reserved: bool  # reserved characters pass unencoded


_OPERATORS = {
    "": _Operator("", ",", False, False),
    "+": _Operator("", ",", False, True),
    "#": _Operator("#", ",", False, True),
    ".": _Operator(".", ".", False, False),
    "/": _Operator("/", "/", False, False),
    ";": _Operator(";", ";", True, False),
    "?": _Operator("?", "&", True, False),
    "&": _Operator("&", "&", True, False),
}


@dataclass(frozen=True)
class _Variable:
    name: str
    explode: bool
    prefix: int | None


@dataclass(frozen=True)
class _Expression:
    operator: str
    variables: tuple[_Variable, ...]

    @property
    def spec(self) -> _Operator:
        return _OPERATORS[self.operator]

    def characters(self) -> frozenset[str]:
        """Every character an expansion of this expression can contain."""
        spec = self.spec
        allowed = _UNRESERVED | {"%", ",", *spec.first, spec.separator}
        if spec.reserved:
            allowed |= _RESERVED
        if spec.named:
            allowed |= {"="}
        return frozenset(allowed)


def _variable(text: str) -> _Variable | None:
    explode = text.endswith("*")
    if explode:
        text = text[:-1]
    prefix: int | None = None
    if ":" in text:
        text, _, digits = text.partition(":")
        if not digits.isdigit() or digits.startswith("0") or int(digits) > MAX_PREFIX or explode:
            return None
        prefix = int(digits)
    if not text or not set(text) <= _VARCHARS or not _encoded(text):
        return None
    return _Variable(text, explode, prefix)


def _encoded(text: str) -> bool:
    """Every ``%`` begins a ``%XX`` octet."""
    index = text.find("%")
    while index >= 0:
        if len(text) < index + 3 or not set(text[index + 1:index + 3]) <= _HEX:
            return False
        index = text.find("%", index + 3)
    return True


def _length(value: str) -> int:
    """Characters of a value as given, a ``%XX`` octet counting as one."""
    return len(value) - 2 * value.count("%")


@dataclass(frozen=True)
class UriTemplate:
    parts: tuple[str | _Expression, ...]

    @staticmethod
    def parse(template: str) -> UriTemplate | None:
        """The template, or None when it is not usable for allowing: not
        RFC 6570, too long, or with a boundary between two expressions
        that a match could not find without trying several splits."""
        if len(template) > MAX_TEMPLATE_CHARS:
            return None
        parts: list[str | _Expression] = []
        position = 0
        while position < len(template):
            opening = template.find("{", position)
            closing = template.find("}", position)
            if opening < 0:
                if closing >= 0:
                    return None
                parts.append(template[position:])
                break
            if 0 <= closing < opening:
                return None
            if opening > position:
                parts.append(template[position:opening])
            closing = template.find("}", opening)
            if closing < 0:
                return None
            body = template[opening + 1:closing]
            if "{" in body:
                return None
            operator = body[:1] if body[:1] in "+#./;?&" else ""
            names = body[len(operator):]
            if body[:1] in "=,!@|":  # reserved for future operators
                return None
            variables = [_variable(v) for v in names.split(",")] if names else []
            if not variables or any(v is None for v in variables):
                return None
            expression = _Expression(operator, tuple(v for v in variables if v is not None))
            if len(expression.variables) > 1 and any(v.prefix is not None for v in expression.variables):
                return None  # which value a prefix bounds could not be told apart
            if parts and isinstance(parts[-1], _Expression):
                leading = expression.spec.first
                if not leading or leading in parts[-1].characters():
                    return None  # no certain boundary between the two
            parts.append(expression)
            position = closing + 1
        for part in parts:
            if isinstance(part, str) and not _encoded_literal(part):
                return None
        return UriTemplate(tuple(parts))

    def matches(self, uri: str) -> bool:
        position = 0
        for index, part in enumerate(self.parts):
            if isinstance(part, str):
                if not uri.startswith(part, position):
                    return False
                position += len(part)
                continue
            following = self.parts[index + 1] if index + 1 < len(self.parts) else None
            if following is None:
                end = len(uri)
            elif isinstance(following, str):
                end = uri.find(following, position)  # the first occurrence: no other split is tried
                if end < 0:
                    return False
            else:
                allowed = part.characters()
                end = position
                while end < len(uri) and uri[end] in allowed:
                    end += 1
            if not _expansion(part, uri[position:end]):
                return False
            position = end
        return position == len(uri)


def _encoded_literal(text: str) -> bool:
    return "%" not in text or _encoded(text)


def _value(value: str, reserved: bool, comma: bool) -> bool:
    allowed = _UNRESERVED | {"%"} | (_RESERVED if reserved else frozenset()) | ({","} if comma else frozenset())
    return set(value) <= allowed and _encoded(value)


def _comma(variable: _Variable, spec: _Operator) -> bool:
    """Whether this variable's expansion can hold an unencoded ",": a
    string's own "," passes only where reserved characters do; otherwise
    "," joins the items of a list that is not exploded (or, for operators
    that separate with ",", one that is). A prefix applies to strings only
    (PR #42 re-review B1)."""
    if spec.reserved:
        return True
    if variable.prefix is not None:
        return False
    return not variable.explode or spec.separator == ","


def _expansion(expression: _Expression, text: str) -> bool:
    """Whether ``text`` is an expansion of ``expression`` for some values."""
    if text == "":
        return True  # every variable undefined
    spec = expression.spec
    if not text.startswith(spec.first):
        return False
    body = text[len(spec.first):]
    variables = expression.variables
    if not spec.named:
        items = body.split(spec.separator) if spec.separator != "," else [body]
        if spec.separator != "," and len(items) > len(variables) and not any(v.explode for v in variables):
            return False  # more segments than variables
        if len(variables) == 1 and variables[0].prefix is not None and _length(body) > variables[0].prefix:
            return False
        if spec.separator == ",":  # "," also separates the variables
            comma = len(variables) > 1 or _comma(variables[0], spec)
        else:  # which variable a segment is of cannot be told: "," only if every one may hold it
            comma = all(_comma(v, spec) for v in variables)
        return all(_value(item, spec.reserved, comma) for item in items)
    # name=value pairs: declared names only, in order, repeated only when exploded
    current = 0
    previous: str | None = None
    for item in body.split(spec.separator):
        name, equals, value = item.partition("=")
        if not equals and spec.first != ";":  # "?" and "&" always write "name="
            return False
        while current < len(variables) and variables[current].name != name:
            current += 1
        if current == len(variables):
            return False  # not declared, or out of order
        variable = variables[current]
        if previous == name and not variable.explode:
            return False
        if variable.prefix is not None and _length(value) > variable.prefix:
            return False
        if not _value(value, False, _comma(variable, spec)):
            return False
        previous = name
        if not variable.explode:
            current += 1
    return True
