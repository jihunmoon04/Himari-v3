"""``inside/read_document`` (V3-P70 §4): a document as text in numbered
units, read on with ``next``; PDF pages or a document's pictures as images.

The selection grammar, the fingerprint and every line the model reads that
is not document text are made here, from the reader's checked answer. The
result stays within MAX_OBSERVATION_BYTES: units at the end are dropped
(and ``next`` says so) rather than the tail lines cut.
"""

from __future__ import annotations

import hashlib
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
_STRUCTURE = ("===", "[shown", "[view", "[images")
_FORMAT_NAMES = {"pdf": "PDF", "docx": "Word", "xlsx": "Excel", "pptx": "PowerPoint", "hwpx": "HWPX"}
_LATER = {"xlsx": ("Excel (.xlsx)", "openpyxl"), "pptx": ("PowerPoint (.pptx)", "python-pptx"),
          "hwpx": ("HWPX", "zipfile and xml.etree")}
GRAMMAR = ('use unit numbers like "1-5,8" or "12-", or pass the next value of an earlier result as given')

DESCRIPTION = (
    "Read a PDF or Word (.docx) file from the workspace as text, split into numbered units: pages (PDF) or "
    "parts (Word). Large documents are shown in parts: the result ends with what to read next, which may hold a "
    "position inside a unit (\"9:61234\") and the document's fingerprint (\"@3f9a1c0b7e2d\"); pass it back as "
    "pages exactly as given. Each PDF page says whether it has pictures or drawings, or no text; these marks are "
    "a guess, so a page without them may still hold a chart. Content hidden in the document is marked [hidden]. "
    "With view=true it returns at most the first 4 selected units with only their text: PDF pages rendered as "
    "images, or up to 4 of the pictures inside a Word part. To see other pictures of a Word file, pass their "
    "numbers (from [picture K]) in pictures. Only models that see images can use the images. Lines the tool "
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


def order_of(pieces: tuple[Piece, ...], units: int) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for piece in pieces:
        last = units if piece.last is None else min(piece.last, units)
        out += [(n, piece.start if n == piece.first else 0) for n in range(piece.first, last + 1)]
    return out


def next_value(order: list[tuple[int, int]], parts: tuple[Part, ...]) -> str | None:
    """What is left of the selection after ``parts``, in the grammar (without the fingerprint)."""
    pieces = [f"{parts[-1].n}:{parts[-1].end}"] if parts and parts[-1].cut else []
    run: list[int] = []
    for n, start in order[len(parts):]:
        if start > 0:
            if run:
                pieces.append(compress(run))
                run = []
            pieces.append(f"{n}:{start}")
        else:
            run.append(n)
    if run:
        pieces.append(compress(run))
    return ",".join(pieces) or None


def fingerprint_of(data: bytes, salt: bytes) -> str:
    return hashlib.sha256(data + b"\0" + salt).hexdigest()[:FINGERPRINT_CHARS]


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
    body = ("…" if part.mid_line else "") + part.text
    if body and not body.endswith("\n"):
        body += "\n"
    return f"=== {unit_name} {part.n}{marks} ===\n" + backstop(body)


class ReadDocument:
    """``inside/read_document``; offered only when the reader passed its startup check."""

    def __init__(self, env: InsideEnvironment, reader: DocumentReading) -> None:
        self._env = env
        self._reader = reader
        self.definition = ToolDefinition(
            tool_id=ToolId("inside/read_document"),
            description=DESCRIPTION,
            input_schema=_schema({
                "path": {"type": "string", "description": "Document to read."},
                "pages": {"type": "string", "description": "Units to read, like \"1-5,8\" or \"12-\" (default: "
                                                           "from the first), or the next value of an earlier result."},
                "view": {"type": "boolean", "description": "Return the first 4 selected units as images too "
                                                          "(default false)."},
                "pictures": {"type": "string", "description": "Word files: picture numbers to return as images, "
                                                              "like \"5-8\" (at most 4 per call); when given, pages "
                                                              "and view are ignored."},
            }, ["path"]),
            workspace_requirement=WorkspaceRequirement.DEFAULT,
            effect_profile=EffectProfile.READ,
            environment_kind=EnvironmentKind("inside"),
        )

    async def run(self, invocation: ToolInvocation, ctx: ToolContext) -> ToolOutcome:
        if ctx.binding is None or ctx.workspace is None:
            return ToolOutcome(ToolStatus.DENIED, _text("no workspace is bound to this conversation", error=True))
        args = invocation.arguments
        try:
            pictures = None if args.get("pictures") is None else parse_selection(args["pictures"], positions=False)
            pages = parse_selection(args.get("pages", "1-"), positions=True) if pictures is None else None
        except SelectionError as error:
            return _refused(f"{args.get('path', '?')}: {error}")
        view = args.get("view") is True
        limit = min(MAX_DOCUMENT_BYTES, self._env.files.max_bytes if self._env.files is not None
                    else DEFAULT_FILE_MAX_BYTES)
        host_root = self._env.host_root(ctx.binding.workspace_id)
        loaded = await run_blocking(_load, host_root, ctx.workspace.cwd_relative, args.get("path"), limit)
        if isinstance(loaded, ToolOutcome):
            return loaded
        shown, data = loaded
        fingerprint = fingerprint_of(data, self._reader.salt)
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
            return _refused(f"{shown}: {self._why(refused.reason)}")
        head = (f"{shown} ({_FORMAT_NAMES.get(reading.format, reading.format)}, {reading.units:,} "
                f"{_plural(reading.unit_name, reading.units)}, {_size(len(data))})")
        if pictures is not None:
            return self._pictures(head, shown, reading, pictures[0], fingerprint, len(data))
        assert pages is not None
        return self._pages(head, shown, reading, pages[0], view, fingerprint, len(data))

    def _why(self, reason: str) -> str:
        if reason == "encrypted":
            return "password-protected; ask the user for an unprotected copy"
        if reason == "legacy":
            return ("an old binary Office or HWP file (.doc, .xls, .ppt, .hwp), which read_document cannot read; "
                    "ask the user to save it as .docx or .pdf")
        if reason == "not_a_document":
            return "not a PDF or Word document; if it is text, use read_file"
        if reason.startswith("later:"):
            name, library = _LATER.get(reason[6:], (reason[6:], ""))
            hint = f"; use run_command with {library}" if self._reader.scripts and library else ""
            return f"reading {name} files is not available yet{hint}"
        if reason in ("position_past_end", "bad_position"):
            return "not a valid position; pass the next value of an earlier result as given"
        return f"could not read this document ({reason.replace('_', ' ')})"

    def _pages(self, head: str, shown: str, reading: Reading, pieces: tuple[Piece, ...], view: bool,
               fingerprint: str, size: int) -> ToolOutcome:
        order = order_of(pieces, reading.units)
        past = sorted({n for p in pieces for n in range(max(p.first, reading.units + 1),
                                                         (p.last or p.first) + 1) if n > reading.units})
        if not order:
            return _refused(f"{shown}: {_ranges_text(reading.unit_name, past or [p.first for p in pieces])}: "
                            f"the document has {reading.units:,} {_plural(reading.unit_name, reading.units)}")
        parts = reading.parts
        if not parts:
            return _refused(f"{shown}: could not read this document (a bad answer)")
        while True:
            text, raw = self._compose(head, reading, order, parts, past, view, fingerprint, size)
            if len(text.encode("utf-8")) <= MAX_OBSERVATION_BYTES:
                break
            if len(parts) <= 1:
                return _refused(f"{shown}: could not read this document (a bad answer)")
            parts = parts[:-1]  # §4.2: units go, the tail lines stay
        kept = {p.n for p in parts}
        images = tuple(ResultImage(i.data, i.media_type) for i in reading.images if i.unit in kept)
        return ToolOutcome(ToolStatus.SUCCEEDED, _text(text), raw_result=raw, result_images=images)

    def _compose(self, head: str, reading: Reading, order: list[tuple[int, int]], parts: tuple[Part, ...],
                 past: list[int], view: bool, fingerprint: str, size: int) -> tuple[str, dict[str, JsonValue]]:
        name = reading.unit_name
        blocks = [head + "\n", *(_part_block(p, name) for p in parts)]
        rest = next_value(order, parts)
        whole = [p.n for p in parts if not p.cut]
        shown_bits = [_ranges_text(name, whole)] if whole else []
        if parts and parts[-1].cut:
            last = parts[-1]
            of = "?" if last.length is None else f"{last.length:,}"
            shown_bits.append(f"{name} {last.n} characters {last.start:,}-{last.end:,} of {of}")
        shown_text = ", ".join(shown_bits) or "nothing"
        if rest is None:
            blocks.append(f"[shown: {shown_text} of {reading.units:,}]\n")
        else:
            blocks.append(f'[shown: {shown_text}; read on with pages="{rest}@{fingerprint}"]\n')
        if past:
            blocks.append(f"[note: {_ranges_text(name, past)} past the end ({reading.units:,} "
                          f"{_plural(name, reading.units)})]\n")
        kept = {p.n for p in parts}
        images = [i for i in reading.images if i.unit in kept]
        if images:
            labels = [f"{name} {i.unit}" if i.number is None else f"picture {i.number} ({name} {i.unit})"
                      for i in images]
            blocks.append(f"[images: {', '.join(labels)}]\n")
        skipped = [s for s in reading.skipped if s.unit is None or s.unit in kept][:10]
        if skipped:
            blocks.append("[not shown: " + "; ".join(
                (f"picture {s.number}" if s.number is not None else f"{name} {s.unit}") + f": {s.reason}"
                for s in skipped) + "]\n")
        if view and reading.more:
            more = sorted(reading.more)
            blocks.append(f"[view: {_ranges_text('picture', more)} in these {_plural(name, 2)} not shown; call "
                          f'again with pictures="{compress(more[:VIEW_LIMIT])}@{fingerprint}"]\n')
        raw: dict[str, JsonValue] = {
            "path": head.split(" (", 1)[0], "size": size, "format": reading.format, "units": reading.units,
            "fingerprint": fingerprint,
            "shown": [{"n": p.n, "from": p.start, "to": p.end, "length": p.length} for p in parts],
            "next": None if rest is None else f"{rest}@{fingerprint}",
            "pictures": sum(p.pictures for p in parts), "images": len(images),
        }
        return "".join(blocks).rstrip("\n"), raw

    def _pictures(self, head: str, shown: str, reading: Reading, pieces: tuple[Piece, ...], fingerprint: str,
                  size: int) -> ToolOutcome:
        if reading.format == "pdf":
            return _refused(f"{shown}: pictures is for Word files; for a PDF use pages with view")
        wanted = [k for p in pieces for k in range(p.first, min(p.last or reading.picture_total,
                                                                reading.picture_total) + 1)]
        if not wanted:
            return _refused(f"{shown}: the document has {reading.picture_total:,} "
                            f"{_plural('picture', reading.picture_total)}")
        blocks = [head + "\n"]
        blocks += [f"=== picture {i.number} ({reading.unit_name} {i.unit}) ===\n" for i in reading.images]
        rest = [k for k in wanted if k > reading.examined]
        if reading.images:
            blocks.append("[images: " + ", ".join(f"picture {i.number}" for i in reading.images) + "]\n")
        if reading.skipped:
            blocks.append("[not shown: " + "; ".join(f"picture {s.number}: {s.reason}"
                                                     for s in reading.skipped[:10]) + "]\n")
        next_pictures = f"{compress(rest)}@{fingerprint}" if rest else None
        if rest:
            blocks.append(f"[view: {_ranges_text('picture', rest)} not shown; call again with "
                          f'pictures="{compress(rest[:VIEW_LIMIT])}@{fingerprint}"]\n')
        raw: dict[str, JsonValue] = {"path": shown, "size": size, "format": reading.format,
                                     "fingerprint": fingerprint, "images": len(reading.images),
                                     "next_pictures": next_pictures}
        images = tuple(ResultImage(i.data, i.media_type) for i in reading.images)
        return ToolOutcome(ToolStatus.SUCCEEDED, _text("".join(blocks).rstrip("\n")), raw_result=raw,
                           result_images=images)


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
