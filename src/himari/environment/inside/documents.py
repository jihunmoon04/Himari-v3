"""``inside/read_document`` (V3-P70 §4): a document as text in numbered
units, read on with ``next``; PDF pages or a document's pictures as images.

The selection grammar, the fingerprint and every line the model reads that
is not document text are made here, from the reader's checked answer. The
result stays within MAX_OBSERVATION_BYTES: units at the end are dropped
(and ``next`` says so) rather than the tail lines cut.
"""

from __future__ import annotations

import bisect
import hashlib
import posixpath
import re
from typing import TYPE_CHECKING

from himari.common import EnvironmentKind, JsonValue, ToolId
from himari.data.api import DEFAULT_FILE_MAX_BYTES, MAX_DOCUMENT_BYTES
from himari.documents.api import (
    VIEW_LIMIT,
    DocumentReading,
    DocumentRefused,
    DocumentsUnavailable,
    Part,
    Piece,
    Reading,
)
from himari.environment.inside import fs
from himari.environment.inside.blocking import run_blocking
from himari.environment.inside.tools import MAX_OBSERVATION_BYTES, _failure, _schema, _size, _text
from himari.tool.api import (
    EffectProfile,
    ResultImage,
    ToolContext,
    ToolDefinition,
    ToolInvocation,
    ToolOutcome,
    ToolStatus,
    WorkspaceRequirement,
)

if TYPE_CHECKING:
    from himari.environment.inside import InsideEnvironment

TAIL_RESERVE = 4096  # the lines after the units: shown, notes, images, view
PLAIN_PIECES, PLAIN_CHARS = 20, 100  # a selection the model wrote (§4)
GIVEN_PIECES, GIVEN_CHARS = 22, 200  # one carrying a fingerprint: what the Tool wrote always fits
FINGERPRINT_CHARS = 12
_PIECE = re.compile(r"(\d{1,10})(?::(\d{1,10})|-(\d{0,10}))?")
_FINGERPRINT = re.compile(r"[0-9a-f]{%d}" % FINGERPRINT_CHARS)
_STRUCTURE = ("===", "[shown", "[view", "[images", "[note", "[not shown")  # every tail line's start (review B3)
_FORMAT_NAMES = {"pdf": "PDF", "docx": "Word", "xlsx": "Excel", "pptx": "PowerPoint", "hwpx": "HWPX"}
_AT_A_LIMIT = ("timed_out", "out_of_memory", "worker_processgone")  # the worker's time, memory or CPU ran out
_LIBRARIES = {".pdf": "pypdf", **dict.fromkeys((".docx", ".docm", ".dotx", ".dotm"), "python-docx"),
              **dict.fromkeys((".xlsx", ".xlsm", ".xltx", ".xltm"), "openpyxl"),
              **dict.fromkeys((".pptx", ".pptm", ".potx", ".potm", ".ppsx", ".ppsm"), "python-pptx"),
              ".hwpx": "zipfile and xml.etree"}
GRAMMAR = ('use unit numbers like "1-5,8" or "12-", or pass the next value of an earlier result as given')

DESCRIPTION = (
    "Read a PDF, Word (.docx), Excel (.xlsx), PowerPoint (.pptx) or HWPX file from the workspace as text, split "
    "into numbered units: pages (PDF), slides (PowerPoint), sheets (Excel) or parts (Word, HWPX). Large documents are shown in parts: the result ends with what to read next, which may hold a "
    "position inside a unit (\"9:61234\") and the document's fingerprint (\"@3f9a1c0b7e2d\"); pass it back as "
    "pages exactly as given. Each PDF page says whether it has pictures or drawings, or no text; these marks are "
    "a guess, so a page without them may still hold a chart. Content hidden in the document is marked [hidden]. "
    "With view=true it returns at most the first 4 selected units with only their text: PDF pages rendered as "
    "images, or up to 4 of the pictures inside other documents' units. To see other pictures of a document that "
    "is not a PDF, pass their numbers (from [picture K]) in pictures. Only models that see images can use the images. Lines the tool "
    "writes start with '===' or '['; a document line that starts with '===', '[' or '\\' is shown with a "
    "leading '\\'."
)


class SelectionError(ValueError):
    pass


def parse_selection(raw: object, *, positions: bool) -> tuple[tuple[Piece, ...], str | None]:
    """The model's ``pages`` or ``pictures`` as normalized pieces and the
    fingerprint it carries (§4): a union, in order, overlaps counted once."""
    if not isinstance(raw, str) or not raw.strip():
        raise SelectionError(GRAMMAR)
    body, at, fingerprint = raw.strip().partition("@")
    body = body.replace(" ", "")
    if at and not _FINGERPRINT.fullmatch(fingerprint):
        raise SelectionError("the part after @ is not a fingerprint this tool wrote; " + GRAMMAR)
    texts = body.split(",")
    pieces_limit, chars_limit = (GIVEN_PIECES, GIVEN_CHARS) if at else (PLAIN_PIECES, PLAIN_CHARS)
    if len(texts) > pieces_limit or len(body) > chars_limit:
        raise SelectionError(f"at most {pieces_limit} pieces and {chars_limit} characters; " + GRAMMAR)
    whole: list[tuple[int, int | None]] = []
    partial: dict[int, int] = {}
    for text in texts:
        match = _PIECE.fullmatch(text)
        if match is None:
            raise SelectionError(f"{text!r}: " + GRAMMAR)
        first = int(match.group(1))
        if first < 1:
            raise SelectionError("numbers start at 1")
        if match.group(2) is not None:
            if not positions:
                raise SelectionError("pictures take picture numbers only, without :positions")
            if not at:
                raise SelectionError("a position inside a unit needs the @fingerprint from the result's next")
            start = int(match.group(2))
            partial[first] = min(start, partial.get(first, start))
            if start == 0:
                whole.append((first, first))
        elif match.group(3) is None:
            whole.append((first, first))
        elif match.group(3) == "":
            whole.append((first, None))
        else:
            last = int(match.group(3))
            if last < first:
                raise SelectionError(f"{text}: a range runs upward")
            whole.append((first, last))
    merged: list[tuple[int, int | None]] = []
    for low, high in sorted(whole, key=lambda r: r[0]):
        if merged:
            top = merged[-1][1]
            if top is None or low <= top + 1:
                merged[-1] = (merged[-1][0], None if top is None or high is None else max(top, high))
                continue
        merged.append((low, high))

    def inside(n: int) -> bool:
        return any(a <= n and (b is None or n <= b) for a, b in merged)

    pieces = [Piece(a, b, 0) for a, b in merged]
    pieces += [Piece(n, n, k) for n, k in partial.items() if k > 0 and not inside(n)]
    pieces.sort(key=lambda p: p.first)
    return tuple(pieces), fingerprint if at else None


def compress(numbers: list[int]) -> str:
    """[1,2,3,5] → "1-3,5"."""
    runs: list[list[int]] = []
    for n in numbers:
        if runs and runs[-1][1] == n - 1:
            runs[-1][1] = n
        else:
            runs.append([n, n])
    return ",".join(f"{a}" if a == b else f"{a}-{b}" for a, b in runs)


def _plural(name: str, count: int) -> str:
    return name if count == 1 else name + "s"


def _ranges_text(name: str, numbers: list[int]) -> str:
    text = compress(numbers).replace(",", ", ")
    return f"{_plural(name, len(numbers))} {text}"


Span = tuple[int, int | None, int]  # first, last (None: open), start inside the first


def clip(pieces: tuple[Piece, ...], after: int, top: int) -> list[Span]:
    """What the pieces hold past unit ``after`` up to ``top``, still as
    ranges: never one entry per unit (review B1)."""
    out: list[Span] = []
    for piece in pieces:
        last = top if piece.last is None else min(piece.last, top)
        first, start = (piece.first, piece.start) if piece.first > after else (after + 1, 0)
        if first <= last:
            out.append((first, last, start))
    return out


def spans_text(spans: list[Span]) -> str:
    """Spans in the selection grammar."""
    return ",".join(f"{a}:{s}" if s else f"{a}" if a == b else f"{a}-" if b is None else f"{a}-{b}"
                    for a, b, s in spans)


def spans_words(name: str, spans: list[Span]) -> str:
    count = sum(2 if b is None else b - a + 1 for a, b, _ in spans)
    return f"{_plural(name, count)} {spans_text(spans).replace(',', ', ')}"


def first_numbers(spans: list[Span], count: int) -> list[int]:
    out: list[int] = []
    for a, b, _ in spans:
        n = a
        while len(out) < count and (b is None or n <= b):
            out.append(n)
            n += 1
    return out


def past_end(pieces: tuple[Piece, ...], units: int) -> list[Span]:
    """The parts of the selection after the last unit, as ranges."""
    return [(max(p.first, units + 1), p.last, 0) for p in pieces if p.last is None and p.first > units
            or p.last is not None and p.last > units]


def next_value(pieces: tuple[Piece, ...], units: int, parts: tuple[Part, ...]) -> str | None:
    """What is left of the selection after ``parts`` (a prefix of it, the
    reader checked), in the grammar, without the fingerprint."""
    head = [(parts[-1].n, parts[-1].n, parts[-1].end)] if parts and parts[-1].cut else []
    return spans_text(head + clip(pieces, parts[-1].n if parts else 0, units)) or None


def fingerprint_of(data: bytes, salt: bytes) -> str:
    digest = hashlib.sha256(data)  # no copy of the document (review C4)
    digest.update(b"\0" + salt)
    return digest.hexdigest()[:FINGERPRINT_CHARS]


def backstop(body: str) -> str:
    """A '\\' before any line of document text shaped like the Tool's own
    structure lines (the worker already escaped them; §4.2)."""
    return "".join("\\" + line if line.startswith(_STRUCTURE) else line for line in body.splitlines(keepends=True))


def _part_block(part: Part, unit_name: str) -> str:
    marks = ""
    if part.label:
        marks += f' (label "{part.label}")'
    if part.pictures and unit_name == "page":
        marks += f" [{part.pictures} {_plural('picture', part.pictures)}]"
    if "drawings" in part.flags:
        marks += " [drawings]"
    if "hidden" in part.flags:
        marks += " [hidden]"
    if "no_text" in part.flags:
        marks += " [no text: a scan or only pictures? view it]" if unit_name == "page" else " [no text]"
    if part.start > 0 or part.cut:
        of = "?" if part.length is None else f"{part.length:,}"
        marks += f" (characters {part.start:,}-{part.end:,} of {of})"
    if part.mid_line:
        marks += " (continues mid-line)"
    body = (part.header or "") + ("…" if part.mid_line else "") + part.text
    if body and not body.endswith("\n"):
        body += "\n"
    return f"=== {unit_name} {part.n}{marks} ===\n" + backstop(body)


def document_definition(namespace: str, *, binding: bool, description: str = DESCRIPTION) -> ToolDefinition:
    """``<namespace>/read_document`` (Inside, or the machine Environment, V3-P74 §8.4)."""
    return ToolDefinition(
            tool_id=ToolId(f"{namespace}/read_document"),
            description=description,
            input_schema=_schema({
                "path": {"type": "string", "description": "Document to read."},
                "pages": {"type": "string", "description": "Units to read, like \"1-5,8\" or \"12-\" (default: "
                                                           "from the first), or the next value of an earlier result."},
                "view": {"type": "boolean", "description": "Return the first 4 selected units as images too "
                                                          "(default false)."},
                "pictures": {"type": "string", "description": "Not for PDFs: picture numbers to return as images, "
                                                              "like \"5-8\" (at most 4 per call); when given, pages "
                                                              "and view are ignored."},
            }, ["path"], binding=binding),
            workspace_requirement=WorkspaceRequirement.DEFAULT,
            effect_profile=EffectProfile.READ,
            environment_kind=EnvironmentKind(namespace),
        )


class ReadDocument:
    """``inside/read_document``; offered only when the reader passed its startup check."""

    def __init__(self, env: InsideEnvironment, reader: DocumentReading) -> None:
        self._env = env
        self._reader = reader
        self.definition = document_definition("inside", binding=True)

    def _limit(self) -> int:
        return min(MAX_DOCUMENT_BYTES, self._env.files.max_bytes if self._env.files is not None
                   else DEFAULT_FILE_MAX_BYTES)

    async def _fetch(self, ctx: ToolContext, raw_path: object, limit: int) -> ToolOutcome | tuple[str, bytes]:
        """The document's shown path and bytes, or the refusal."""
        assert ctx.binding is not None and ctx.workspace is not None
        return await run_blocking(_load, self._env.host_root(ctx.binding.workspace_id), ctx.workspace.cwd_relative,
                                  raw_path, limit)

    async def run(self, invocation: ToolInvocation, ctx: ToolContext) -> ToolOutcome:
        if ctx.binding is None or ctx.workspace is None:
            return ToolOutcome(ToolStatus.DENIED, _text("no workspace is bound to this conversation", error=True))
        args = invocation.arguments
        # An optional argument a model fills with "" means it is not given: models often send every property
        # of the schema (seen live with openai/gpt-6-luna, which then never saw the page). A null never gets
        # here: the Dispatcher checks the arguments against the schema first.
        raw_pictures, raw_pages = (_given(args.get(name)) for name in ("pictures", "pages"))
        try:
            pictures = None if raw_pictures is None else parse_selection(raw_pictures, positions=False)
            pages = (parse_selection("1-" if raw_pages is None else raw_pages, positions=True)
                     if pictures is None else None)
        except SelectionError as error:
            return _refused(f"{args.get('path', '?')}: {error}")
        view = args.get("view") is True
        loaded = await self._fetch(ctx, args.get("path"), self._limit())
        if isinstance(loaded, ToolOutcome):
            return loaded
        shown, data = loaded
        fingerprint = await run_blocking(fingerprint_of, data, self._reader.salt)
        given = (pictures or pages or ((), None))[1]
        if given is not None and given != fingerprint:
            return _refused(f"{shown}: the document changed since that position was given (now @{fingerprint}); "
                            'read it again from the start (pages="1-")')
        try:
            reading = await self._reader.read(data, pages=None if pages is None else pages[0],
                                              pictures=None if pictures is None else
                                              tuple((p.first, p.last) for p in pictures[0]),
                                              view=view, text_budget=_budget(shown))
        except DocumentsUnavailable:
            return ToolOutcome(ToolStatus.DENIED, _text(f"{shown}: documents cannot be read on this server now",
                                                        error=True))
        except DocumentRefused as refused:
            return _refused(f"{shown}: {self._why(refused.reason, shown)}")
        head = (f"{shown} ({_FORMAT_NAMES.get(reading.format, reading.format)}, {reading.units:,} "
                f"{_plural(reading.unit_name, reading.units)}, {_size(len(data))})")
        if pictures is not None:
            return self._pictures(head, shown, reading, pictures[0], fingerprint, len(data))
        assert pages is not None
        return self._pages(head, shown, reading, pages[0], view, fingerprint, len(data))

    def _why(self, reason: str, shown: str) -> str:
        if reason == "encrypted":
            return "password-protected; ask the user for an unprotected copy"
        if reason == "legacy":
            return ("an old binary Office or HWP file (.doc, .xls, .ppt, .hwp), which read_document cannot read; "
                    "ask the user to save it as .docx or .pdf")
        if reason == "not_a_document":
            return "not a PDF, Word, Excel, PowerPoint or HWPX document; if it is text, use read_file"
        if reason in ("position_past_end", "bad_position"):
            return "not a valid position; pass the next value of an earlier result as given"
        why = f"could not read this document ({reason.replace('_', ' ')})"
        if reason in _AT_A_LIMIT and self._reader.scripts:  # §4.2: a huge sheet read on from far into it
            library = _LIBRARIES.get(posixpath.splitext(shown)[1].lower(), "a Python script")
            why += f"; it may be too large to read this way: use run_command with {library}"
        return why

    def _pages(self, head: str, shown: str, reading: Reading, pieces: tuple[Piece, ...], view: bool,
               fingerprint: str, size: int) -> ToolOutcome:
        name, units = reading.unit_name, reading.units
        past = past_end(pieces, units)
        if not any(p.first <= units for p in pieces):
            return _refused(f"{shown}: {spans_words(name, past)}: the document has {units:,} {_plural(name, units)}")
        if not reading.parts:
            return _refused(f"{shown}: could not read this document (a bad answer)")
        # §4.2: the units that fit with their tail lines, chosen in one pass (review B2): units go, the tail stays.
        blocks = [_part_block(p, name).encode("utf-8") for p in reading.parts]
        start = len(head.encode("utf-8")) + 1
        used = [start]
        for block in blocks:
            used.append(used[-1] + len(block))
        # The tail is a few lines (TAIL_RESERVE): start from the most that fit with it, then confirm.
        most = max(1, bisect.bisect_right(used, MAX_OBSERVATION_BYTES - TAIL_RESERVE) - 1)
        for keep in range(min(most, len(blocks)), 0, -1):
            parts = reading.parts[:keep]
            tail, raw = self._tail(reading, pieces, parts, past, view, fingerprint, size, head)
            if used[keep] + len(tail.encode("utf-8")) <= MAX_OBSERVATION_BYTES:
                break
        else:
            return _refused(f"{shown}: could not read this document (a bad answer)")
        text = head + "\n" + b"".join(blocks[:keep]).decode("utf-8") + tail
        kept = {p.n for p in parts}
        images = tuple(ResultImage(i.data, i.media_type) for i in reading.images if i.unit in kept)
        return ToolOutcome(ToolStatus.SUCCEEDED, _text(text.rstrip("\n")), raw_result=raw, result_images=images)

    def _tail(self, reading: Reading, pieces: tuple[Piece, ...], parts: tuple[Part, ...], past: list[Span],
              view: bool, fingerprint: str, size: int, head: str) -> tuple[str, dict[str, JsonValue]]:
        name = reading.unit_name
        rest = next_value(pieces, reading.units, parts)
        whole = [p.n for p in parts if not p.cut]
        shown_bits = [_ranges_text(name, whole)] if whole else []
        if parts and parts[-1].cut:
            last = parts[-1]
            of = "?" if last.length is None else f"{last.length:,}"
            shown_bits.append(f"{name} {last.n} characters {last.start:,}-{last.end:,} of {of}")
        shown_text = ", ".join(shown_bits) or "nothing"
        lines = [f"[shown: {shown_text} of {reading.units:,}]" if rest is None
                 else f'[shown: {shown_text}; read on with pages="{rest}@{fingerprint}"]']
        if past:
            lines.append(f"[note: {spans_words(name, past)} past the end ({reading.units:,} "
                         f"{_plural(name, reading.units)})]")
        kept = {p.n for p in parts}
        images = [i for i in reading.images if i.unit in kept]
        if images:
            labels = [f"{name} {i.unit}" if i.number is None else f"picture {i.number} ({name} {i.unit})"
                      for i in images]
            lines.append(f"[images: {', '.join(labels)}]")
        skipped = [s for s in reading.skipped if s.unit is None or s.unit in kept][:10]
        if skipped:
            lines.append("[not shown: " + "; ".join(
                (f"picture {s.number}" if s.number is not None else f"{name} {s.unit}") + f": {s.reason}"
                for s in skipped) + "]")
        if view and reading.more:
            more = sorted(reading.more)
            lines.append(f"[view: {_ranges_text('picture', more)} in these {_plural(name, 2)} not shown; call "
                         f'again with pictures="{compress(more[:VIEW_LIMIT])}@{fingerprint}"]')
        raw: dict[str, JsonValue] = {
            "path": head.split(" (", 1)[0], "size": size, "format": reading.format, "units": reading.units,
            "fingerprint": fingerprint,
            "shown": [{"n": p.n, "from": p.start, "to": p.end, "length": p.length} for p in parts],
            "next": None if rest is None else f"{rest}@{fingerprint}",
            "pictures": sum(p.pictures for p in parts), "images": len(images),
        }
        return "".join(line + "\n" for line in lines), raw

    def _pictures(self, head: str, shown: str, reading: Reading, pieces: tuple[Piece, ...], fingerprint: str,
                  size: int) -> ToolOutcome:
        if reading.format == "pdf":
            return _refused(f"{shown}: pictures is not for PDFs; for a PDF use pages with view")
        if not clip(pieces, 0, reading.picture_total):
            return _refused(f"{shown}: the document has {reading.picture_total:,} "
                            f"{_plural('picture', reading.picture_total)}")
        blocks = [head + "\n"]
        blocks += [f"=== picture {i.number} ({reading.unit_name} {i.unit}) ===\n" for i in reading.images]
        rest = clip(pieces, max(reading.examined, max((i.number or 0 for i in reading.images), default=0)),
                    reading.picture_total)
        if reading.images:
            blocks.append("[images: " + ", ".join(f"picture {i.number}" for i in reading.images) + "]\n")
        if reading.skipped:
            blocks.append("[not shown: " + "; ".join(f"picture {s.number}: {s.reason}"
                                                     for s in reading.skipped[:10]) + "]\n")
        next_pictures = f"{spans_text(rest)}@{fingerprint}" if rest else None
        if rest:
            blocks.append(f"[view: {spans_words('picture', rest)} not shown; call again with "
                          f'pictures="{compress(first_numbers(rest, VIEW_LIMIT))}@{fingerprint}"]\n')
        raw: dict[str, JsonValue] = {"path": shown, "size": size, "format": reading.format,
                                     "fingerprint": fingerprint, "images": len(reading.images),
                                     "next_pictures": next_pictures}
        images = tuple(ResultImage(i.data, i.media_type) for i in reading.images)
        return ToolOutcome(ToolStatus.SUCCEEDED, _text("".join(blocks).rstrip("\n")), raw_result=raw,
                           result_images=images)


def _given(value: object) -> object:
    """None for an argument left out or empty ("", whitespace); the value otherwise."""
    return None if value is None or (isinstance(value, str) and not value.strip()) else value


def _budget(shown: str) -> int:
    return MAX_OBSERVATION_BYTES - len(shown.encode("utf-8")) - 200 - TAIL_RESERVE


def _refused(message: str) -> ToolOutcome:
    return ToolOutcome(ToolStatus.FAILED, _text(message, error=True))


def _load(host_root: str, cwd: str, raw_path: object, limit: int) -> ToolOutcome | tuple[str, bytes]:
    shown = raw_path if isinstance(raw_path, str) else "?"
    try:
        relative = fs.to_relative(cwd, shown)
        shown = fs.display(relative)
        with fs.open_root(host_root) as root_fd:
            fs.ensure_directory(root_fd, fs.to_relative(cwd, "."))
            data, size = fs.read_whole(root_fd, relative, limit)
    except fs.FsError as error:
        return _failure(shown, error)
    if data is None:
        return _refused(f"{shown}: too large for read_document ({_size(size)}, limit {_size(limit)}); "
                        "use run_command")
    return shown, data
