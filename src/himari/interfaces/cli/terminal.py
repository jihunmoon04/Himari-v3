"""Terminal-safe projection of untrusted text (adapted from HIMARI-v2
``interfaces/terminal_text.py``).

A terminal interprets what it is handed: ESC, C1 CSI, DCS/OSC introducers
issue control sequences, a carriage return rewrites the row already
printed, and bidi overrides reverse what is read without being visible.
Every C0 control (except the newline and tab handled below), DEL, C1
control, Unicode format control and lone surrogate is therefore escaped
to a visible backslash form — by Unicode category, not by matching one
syntax. A character the stream's codec cannot carry becomes the same
visible escape instead of an encoding error.
"""

from __future__ import annotations

import codecs
import unicodedata
from typing import Any

_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Cs"})
_ESCAPES = {"\\": "\\\\", "\r": "\\r"}


def stream_encoding(stream: Any) -> str | None:
    name = getattr(stream, "encoding", None)
    if not isinstance(name, str):
        return None
    try:
        codecs.lookup(name)
    except LookupError:
        return None
    return name


def _escape_code_point(character: str) -> str:
    code_point = ord(character)
    return f"\\u{code_point:04x}" if code_point <= 0xFFFF else f"\\U{code_point:08x}"


def safe_line(value: str, *, encoding: str | None = None) -> str:
    """One line of untrusted text: every control (newline and tab too) escaped."""
    out: list[str] = []
    for character in value:
        if character in _ESCAPES:
            out.append(_ESCAPES[character])
        elif character == "\n":
            out.append("\\n")
        elif character == "\t":
            out.append("\\t")
        elif unicodedata.category(character) in _UNSAFE_CATEGORIES:
            out.append(_escape_code_point(character))
        elif encoding is not None:
            try:
                character.encode(encoding)
            except UnicodeEncodeError:
                out.append(_escape_code_point(character))
            else:
                out.append(character)
        else:
            out.append(character)
    return "".join(out)
