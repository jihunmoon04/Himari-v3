"""What the Inside Environment sees of document reading (V3-P70 §4, §5).

The reader (``himari.documents.DocumentReader``) parses in the documents
sandbox and checks every answer; the Tool (``inside/read_document``) parses
the model's selection, keeps the result within its size and writes the
lines the model reads. Both share these types.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

VIEW_LIMIT = 4  # units (view) or pictures (pictures) per call: Q2
UNIT_FLAGS = frozenset({"drawings", "no_text", "hidden"})
LABEL_CHARS = 20


@dataclass(frozen=True)
class Piece:
    """Units ``first`` to ``last`` (None: to the end), the first read from
    character ``start`` of its laid-out text (only when first == last)."""

    first: int
    last: int | None
    start: int = 0


@dataclass(frozen=True)
class Part:
    n: int
    flags: tuple[str, ...]
    pictures: int
    start: int
    end: int
    length: int | None  # None: the unit goes on past what was laid out
    text: str
    label: str | None = None
    mid_line: bool = False

    @property
    def cut(self) -> bool:
        return self.length is None or self.end < self.length


@dataclass(frozen=True)
class Picture:
    unit: int
    number: int | None  # None: a rendered PDF page
    media_type: str
    data: bytes


@dataclass(frozen=True)
class Skipped:
    unit: int | None
    number: int | None
    reason: str


@dataclass(frozen=True)
class Reading:
    format: str  # "pdf", "docx", ...
    units: int
    unit_name: str  # "page", "part", ...
    picture_total: int
    parts: tuple[Part, ...]
    images: tuple[Picture, ...]
    skipped: tuple[Skipped, ...]
    examined: int  # pictures mode: the last picture number looked at
    more: tuple[int, ...]  # view mode: pictures in the viewed units not returned


class DocumentRefused(Exception):
    """The document cannot be read; ``reason`` is the worker's word
    (encrypted, legacy, not_a_document, broken, later:<format>,
    position_past_end, bad_position, ...) or "a bad answer"."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class DocumentsUnavailable(Exception):
    """The worker could not be started now (sandbox, admission, the toolkit
    is not the one checked at startup): nothing was read."""


class DocumentReading(Protocol):
    @property
    def salt(self) -> bytes:
        """What the fingerprint adds to the file's bytes: the toolkit
        version and the worker's split rule (§4)."""
        ...

    @property
    def scripts(self) -> bool:
        """Whether run_command has the toolkit (hints may name it)."""
        ...

    async def read(self, data: bytes, *, pages: tuple[Piece, ...] | None,
                   pictures: tuple[tuple[int, int | None], ...] | None, view: bool, text_budget: int) -> Reading:
        """DocumentRefused or DocumentsUnavailable; the answer is checked."""
        ...
