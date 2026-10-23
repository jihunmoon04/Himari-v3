"""The document worker (V3-P70 §5): run alone, inside the documents sandbox.

Run as ``/usr/bin/python3 -I -S worker.py <toolkit site-packages>``. It
imports only the standard library and the documents toolkit (put first on
``sys.path`` from the one argument), never HIMARI. Before anything is
parsed it limits itself (memory, CPU, file size), so a hostile document
ends this process, not the server.

One request, then the answer, each a line on stdin/stdout:

    in:  {"op": "probe", "pdf": <base64>, "docx": <base64>}
         {"op": "read", "data": <base64>, "pages": [{"first", "last", "start"}...] | null,
          "pictures": [[first, last]...] | null, "view": bool, "text_budget": <UTF-8 bytes>}
    out: {"ok": true, ...} then one line per image: {"unit", "picture", "media_type", "data"}
         {"ok": false, "reason": "..."}

The text of a unit is laid out once, the same way for the same file, toolkit
and SPLIT_RULE, so a position K in it means the same place on every call
(§4). Every document line that starts with '===', '[' or '\\' gets a
leading '\\' before the worker adds its own marks ([picture K], [hidden: …],
[^n]); positions count the escaped text.
"""

from __future__ import annotations

import base64
import io
import json
import re
import resource
import sys
import zipfile
from collections.abc import Iterator
from typing import Any

SPLIT_RULE = 1  # bump whenever the layout or the part split changes: positions and numbers move with it
PART_CHARS = 8000  # a DOCX/HWPX part ends at the first block boundary past this
VIEW_UNITS = 4  # §4.3 (Q2)
PART_OVERHEAD = 256  # bytes kept per part for the header line the server writes (= documents.api.PART_OVERHEAD)
MID_LINE_BYTES = len("…".encode())
LONG_EDGE = 1568
MAX_PIXELS = 50_000_000
MAX_IMAGE_BYTES = 3 * 1024 * 1024 - 4096  # its base64 and the JSON around it fit one 4 MiB line
PICTURE_FORMATS = ("PNG", "JPEG", "GIF", "WEBP")
MEMORY_BYTES = 1024 * 1024 * 1024
CPU_SECONDS = 30
FILE_BYTES = 16 * 1024 * 1024
MAX_PAGE_OBJECTS = 20_000  # counted per page for the marks, then counting stops
DRAWINGS = 20  # path objects on a page from which it is marked [drawings] (a guess, §4.1)
OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
ENCRYPTED_STREAM = "EncryptedPackage".encode("utf-16-le")

_MAIN_TYPES = {
    "docx": ("wordprocessingml.document.main+xml", "wordprocessingml.template.main+xml",
             "ms-word.document.macroEnabled.main+xml", "ms-word.template.macroEnabledTemplate.main+xml"),
    "xlsx": ("spreadsheetml.sheet.main+xml", "spreadsheetml.template.main+xml",
             "ms-excel.sheet.macroEnabled.main+xml", "ms-excel.template.macroEnabled.main+xml"),
    "pptx": ("presentationml.presentation.main+xml", "presentationml.slideshow.main+xml",
             "presentationml.template.main+xml", "ms-powerpoint.presentation.macroEnabled.main+xml",
             "ms-powerpoint.slideshow.macroEnabled.main+xml", "ms-powerpoint.template.macroEnabled.main+xml"),
}
_DOCX_MAIN = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
# Every line boundary str.splitlines knows becomes "\n"; other control characters but tab become a space.
_BREAKS = re.compile("\r\n|[\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]")
_CONTROLS = re.compile("[\x00-\x08\x0e-\x1f\x7f-\x84\x86-\x9f]")
_SURROGATES = re.compile("[\ud800-\udfff]")
_LABEL = re.compile(r"[^\s\x00-\x1f\x7f-\x9f\u2028\u2029]{1,20}")
_NOTE_ID = re.compile(r"-?\d{1,10}")  # a footnote id goes into a mark: digits only (review B3)
_MARKS = re.compile(r"\[picture \d+\]|\[\^-?\d+\]")


def limit() -> None:
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS))
    resource.setrlimit(resource.RLIMIT_FSIZE, (FILE_BYTES, FILE_BYTES))


def answer(**fields: object) -> None:
    sys.stdout.write(json.dumps(fields, ensure_ascii=True) + "\n")
    sys.stdout.flush()


class Refused(Exception):
    """The document cannot be read; the reason is one short word for the server."""


# --- text --------------------------------------------------------------------------------


def clean(text: str) -> str:
    """Document text with "\\n" as its only line boundary and no control characters but tab."""
    return _SURROGATES.sub("\ufffd", _CONTROLS.sub(" ", _BREAKS.sub("\n", text)))


def escape(text: str) -> str:
    """Every line of document text starting with '===', '[' or '\\' gets a '\\'."""
    return "".join("\\" + line if line.startswith(("===", "[", "\\")) else line
                   for line in text.splitlines(keepends=True))


def one_line(text: str) -> str:
    return " ".join(clean(text).split())


class Lines:
    """Builds laid-out text from document text and the worker's own marks:
    document text is escaped at each line start it opens, marks never are."""

    def __init__(self) -> None:
        self.out: list[str] = []
        self._pending: list[str] = []
        self._at_start = True

    def text(self, value: str) -> None:
        self._pending.append(value)

    def mark(self, value: str) -> None:
        self._flush()
        self.out.append(value)
        self._at_start = value.endswith("\n")

    def _flush(self) -> None:
        if not self._pending:
            return
        value = clean("".join(self._pending))
        self._pending = []
        for line in value.splitlines(keepends=True):
            if self._at_start and line.startswith(("===", "[", "\\")):
                line = "\\" + line
            self.out.append(line)
            self._at_start = line.endswith("\n")

    def end_line(self) -> None:
        self._flush()
        if not self._at_start:
            self.out.append("\n")
            self._at_start = True

    def result(self) -> str:
        self._flush()
        return "".join(self.out)


def cut_at(text: str, start: int, budget: int) -> int:
    """The end of the longest slice text[start:end] within ``budget`` UTF-8
    bytes: after the last line boundary in it if there is one, else at a
    character, never right after an escaping '\\' that opens a line."""
    used = 0
    end = start
    last_break = -1
    while end < len(text):
        size = len(text[end].encode("utf-8", "surrogatepass"))
        if used + size > budget:
            break
        used += size
        end += 1
        if text[end - 1] == "\n":
            last_break = end
    if end >= len(text):
        return len(text)
    if last_break > start:
        return last_break
    if end > start and text[end - 1] == "\\" and (end - 1 == 0 or text[end - 2] == "\n"):
        end -= 1  # the escape and the character it escapes stay together
    return end


def mid_line(text: str, start: int) -> bool:
    return 0 < start <= len(text) and text[start - 1] != "\n"


# --- pictures -----------------------------------------------------------------------------


def shrink(data: bytes) -> tuple[str, int, int, bytes] | str:
    """A picture made ready for the model as the V3-P67 copy is made, or why not."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        with Image.open(io.BytesIO(data), formats=PICTURE_FORMATS) as opened:
            if opened.width * opened.height > MAX_PIXELS:
                return "too many pixels"
            opened.seek(0)
            alpha = opened.mode in ("RGBA", "LA", "PA") or "transparency" in opened.info
            image = opened.convert("RGBA" if alpha else "RGB") if opened.mode in ("1", "P", "PA") else opened
            image.thumbnail((LONG_EDGE, LONG_EDGE), Image.Resampling.LANCZOS)
            image = ImageOps.exif_transpose(image).convert("RGBA" if alpha else "RGB")
    except UnidentifiedImageError:
        return "not a PNG, JPEG, GIF or WebP picture"
    return encode(image, alpha)


def encode(image: Any, alpha: bool) -> tuple[str, int, int, bytes] | str:
    from PIL import Image

    if alpha:
        out = io.BytesIO()
        image.save(out, "PNG", optimize=True)
        if out.tell() <= MAX_IMAGE_BYTES:
            return "image/png", image.width, image.height, out.getvalue()
        flat = Image.new("RGB", image.size, (255, 255, 255))
        flat.paste(image, mask=image.getchannel("A"))
        image = flat
    for quality in (85, 70):
        out = io.BytesIO()
        image.save(out, "JPEG", quality=quality, optimize=True)
        if out.tell() <= MAX_IMAGE_BYTES:
            return "image/jpeg", image.width, image.height, out.getvalue()
    return "too large even as a small JPEG"


# --- formats ------------------------------------------------------------------------------


def detect(data: bytes) -> str:
    if b"%PDF-" in data[:1024]:
        return "pdf"
    if data[:8] == OLE2:
        raise Refused("encrypted" if ENCRYPTED_STREAM in data else "legacy")
    if data[:2] != b"PK":
        raise Refused("not_a_document")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        names = archive.namelist()
        if names and names[0] == "mimetype" and archive.read("mimetype").strip() == b"application/hwp+zip":
            return "hwpx"
        if "[Content_Types].xml" not in names:
            raise Refused("not_a_document")
        types = archive.read("[Content_Types].xml").decode("utf-8", "replace")
    except (zipfile.BadZipFile, KeyError, OSError, ValueError, EOFError):
        raise Refused("not_a_document") from None
    for kind, endings in _MAIN_TYPES.items():
        if any(ending in types for ending in endings):
            return kind
    raise Refused("not_a_document")


class Document:
    """Units of laid-out text, their marks, and the pictures in them."""

    unit_name = "unit"

    def __init__(self) -> None:
        self.pictures: list[tuple[int, Any]] = []  # (unit, loader) by picture number - 1

    def count(self) -> int:
        raise NotImplementedError

    def unit(self, n: int) -> dict[str, Any]:
        """{"text", "flags", "pictures": n, "label"?} of unit ``n``."""
        raise NotImplementedError

    def text(self, n: int) -> str:
        return str(self.unit(n)["text"])

    def view(self, n: int, room: int) -> tuple[list[tuple[int | None, str | tuple[str, int, int, bytes]]], list[int]]:
        """Images for unit ``n``, at most ``room`` made: (picture number or
        None, made image or why not), and the picture numbers left unmade."""
        raise NotImplementedError

    def picture(self, number: int) -> tuple[str, int, int, bytes] | str:
        """A picture made ready, or why not: one broken picture never fails the call (review C2)."""
        try:
            return shrink(self.pictures[number - 1][1]())
        except MemoryError:
            return "out of memory"
        except Exception as error:  # noqa: BLE001 — truncated, a decompression bomb, a codec error
            return f"could not be decoded ({type(error).__name__})"


class Pdf(Document):
    unit_name = "page"

    def __init__(self, data: bytes) -> None:
        super().__init__()
        import pypdfium2 as pdfium
        import pypdfium2.raw as raw

        try:
            self._doc = pdfium.PdfDocument(data)
        except pdfium.PdfiumError as error:
            raise Refused("encrypted" if "password" in str(error).lower() else "broken") from None
        self._raw = raw
        self._units: dict[int, dict[str, Any]] = {}  # laid out only when read (review C3)

    def count(self) -> int:
        return len(self._doc)

    def unit(self, n: int) -> dict[str, Any]:
        if n not in self._units:
            page = self._doc[n - 1]
            text = escape(clean(page.get_textpage().get_text_bounded())).rstrip()
            images = paths = seen = 0
            for obj in page.get_objects():
                seen += 1
                if seen > MAX_PAGE_OBJECTS:
                    break
                images += obj.type == self._raw.FPDF_PAGEOBJ_IMAGE
                paths += obj.type == self._raw.FPDF_PAGEOBJ_PATH
            flags = (["drawings"] if paths >= DRAWINGS else []) + ([] if text.strip() else ["no_text"])
            unit: dict[str, Any] = {"text": text + "\n" if text else "", "flags": flags, "pictures": images}
            try:
                label = self._doc.get_page_label(n - 1)
            except Exception:  # noqa: BLE001 — a page without a readable label just has none
                label = ""
            if label and label != str(n) and _LABEL.fullmatch(label):
                unit["label"] = label
            self._units[n] = unit
        return self._units[n]

    def view(self, n: int, room: int) -> tuple[list[tuple[int | None, str | tuple[str, int, int, bytes]]], list[int]]:
        if room <= 0:
            return [], []
        try:
            page = self._doc[n - 1]
            width, height = page.get_size()
            scale = LONG_EDGE / max(width, height, 1)
            image = page.render(scale=scale).to_pil().convert("RGB")
            if max(image.size) > LONG_EDGE:  # rounding: never past the long edge
                image.thumbnail((LONG_EDGE, LONG_EDGE))
            made: str | tuple[str, int, int, bytes] = encode(image, False)
        except MemoryError:
            made = "out of memory"
        except Exception as error:  # noqa: BLE001 — a page that does not render costs its image only
            made = f"could not be rendered ({type(error).__name__})"
        return [(None, made)], []


W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
V = "{urn:schemas-microsoft-com:vml}"


def _as_document_type(data: bytes) -> bytes:
    """python-docx opens the plain document type only: a template or
    macro-enabled file is read through a copy that says it is one."""
    source = zipfile.ZipFile(io.BytesIO(data))
    types = source.read("[Content_Types].xml").decode("utf-8")
    fixed = re.sub(r'ContentType="application/vnd\.(?:openxmlformats-officedocument\.wordprocessingml\.template'
                   r'|ms-word\.(?:document|template)\.macroEnabled(?:Template)?)\.main\+xml"',
                   f'ContentType="{_DOCX_MAIN}"', types)
    if fixed == types:
        return data
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as copy:
        for info in source.infolist():
            copy.writestr(info, fixed if info.filename == "[Content_Types].xml" else source.read(info))
    return out.getvalue()


class Docx(Document):
    unit_name = "part"

    def __init__(self, data: bytes) -> None:
        super().__init__()
        import docx

        try:
            self._doc = docx.Document(io.BytesIO(_as_document_type(data)))
        except Exception:  # noqa: BLE001 — anything python-docx cannot open
            raise Refused("broken") from None
        self._part = self._doc.part
        self._styles = {s.style_id: str(s.name or "") for s in self._doc.styles}
        self._notes = self._footnotes()
        self._list: list[dict[str, Any]] = []
        blocks: list[tuple[str, list[str], list[int]]] = []  # (text, footnote lines, picture numbers)
        self._blocks(self._doc.element.body, blocks)
        self._split(blocks)

    def count(self) -> int:
        return len(self._list)

    def unit(self, n: int) -> dict[str, Any]:
        return self._list[n - 1]

    def _blocks(self, container: Any, blocks: list[tuple[str, list[str], list[int]]]) -> None:
        """Paragraphs and tables in order, inside content controls and custom XML too (review C5)."""
        for child in container.iterchildren():
            if child.tag == W + "p":
                blocks.append(self._paragraph(child))
            elif child.tag == W + "tbl":
                blocks.extend(self._table(child))
            elif child.tag in (W + "sdt", W + "sdtContent", W + "customXml"):
                self._blocks(child, blocks)

    def _footnotes(self) -> dict[str, str]:
        found: dict[str, str] = {}
        for rel in self._part.rels.values():
            if rel.reltype.endswith("/footnotes") and not rel.is_external:
                from lxml import etree

                root = etree.fromstring(rel.target_part.blob)
                for note in root.iter(W + "footnote"):
                    ident = note.get(W + "id")
                    if ident is not None and _NOTE_ID.fullmatch(ident) and note.get(W + "type") is None:
                        found[ident] = one_line("".join(t.text or "" for t in note.iter(W + "t")))
        return found

    def _picture(self, element: Any) -> int | None:
        ident = element.get(R + "embed") or element.get(R + "id")
        if not ident or ident not in self._part.related_parts:
            return None
        target = self._part.related_parts[ident]
        self.pictures.append((0, lambda: target.blob))  # the unit is set when parts are split
        return len(self.pictures)

    def _runs(self, element: Any, lines: Lines, notes: list[str], pictures: list[int], hidden: list[str]) -> None:
        """Document order inside a paragraph: inserted text counts, deleted text does not."""
        for node in element.iterchildren():
            tag = node.tag
            if tag == W + "r":
                vanish = node.find(f"{W}rPr/{W}vanish")
                gone = vanish is not None and vanish.get(W + "val") not in ("0", "false", "off")
                if hidden and not gone:
                    self._hidden(lines, hidden)  # hidden text so far, in its place before this run
                for part in node.iterchildren():
                    if part.tag == W + "t":
                        (hidden.append if gone else lines.text)(part.text or "")
                    elif part.tag == W + "tab":
                        (hidden.append if gone else lines.text)("\t")
                    elif part.tag in (W + "br", W + "cr"):
                        (hidden.append if gone else lines.text)("\n")
                    elif part.tag == W + "footnoteReference":
                        ident = part.get(W + "id")
                        if ident is not None and _NOTE_ID.fullmatch(ident) and ident in self._notes:
                            lines.mark(f"[^{ident}]")
                            notes.append(f"[^{ident}]: {self._notes[ident]}")
                    elif part.tag in (W + "drawing", W + "pict", W + "object"):
                        for blip in [*part.iter(A + "blip"), *part.iter(V + "imagedata")]:
                            number = self._picture(blip)
                            if number is not None:
                                pictures.append(number)
                                lines.mark(f"[picture {number}]")
            elif tag in (W + "ins", W + "moveTo", W + "hyperlink", W + "smartTag", W + "sdt", W + "sdtContent",
                         W + "fldSimple", W + "customXml"):
                self._runs(node, lines, notes, pictures, hidden)
        if hidden:
            self._hidden(lines, hidden)

    @staticmethod
    def _hidden(lines: Lines, hidden: list[str]) -> None:
        text = one_line("".join(hidden))
        hidden.clear()
        if text:
            lines.mark(f"[hidden: {text.replace(']', ')')}]")

    def _paragraph(self, element: Any) -> tuple[str, list[str], list[int]]:
        lines, notes, pictures = Lines(), list[str](), list[int]()
        props = element.find(W + "pPr")
        style = props.find(W + "pStyle").get(W + "val", "") if props is not None and props.find(
            W + "pStyle") is not None else ""
        level = _heading(self._styles.get(style, style))
        if level:
            lines.mark("#" * level + " ")
        elif (props is not None and props.find(W + "numPr") is not None) or self._styles.get(
                style, style).lower().startswith("list"):
            lines.mark("- ")
        self._runs(element, lines, notes, pictures, [])
        lines.end_line()
        text = lines.result()
        return ("" if re.fullmatch(r"(#+ |- )?\s*", text) else text), notes, pictures

    def _cell(self, cell: Any, notes: list[str], pictures: list[int]) -> str:
        lines = Lines()
        for paragraph in cell.iter(W + "p"):
            self._runs(paragraph, lines, notes, pictures, [])
            lines.mark(" ")
        return one_line(lines.result()).replace("|", "\\|")

    def _table(self, element: Any) -> list[tuple[str, list[str], list[int]]]:
        """A markdown table; one too large for a part is split at rows, its
        header row repeated."""
        rows: list[tuple[str, list[str], list[int]]] = []
        for row in element.iterchildren(W + "tr"):
            notes: list[str] = []
            pictures: list[int] = []
            cells = [self._cell(cell, notes, pictures) for cell in row.iterchildren(W + "tc")]
            rows.append(("| " + " | ".join(cells) + " |\n", notes, pictures))
        if not rows:
            return []
        width = len(list(next(element.iterchildren(W + "tr")).iterchildren(W + "tc"))) or 1
        header = rows[0][0] + "|" + " --- |" * width + "\n"
        repeated = _MARKS.sub("", header)  # a repeated header carries no picture or note marks (review N5)
        blocks: list[tuple[str, list[str], list[int]]] = [(header, list(rows[0][1]), list(rows[0][2]))]
        for text, notes, pictures in rows[1:]:
            current = blocks[-1]
            if len(current[0]) + len(text) > PART_CHARS and current[0] not in (header, repeated):
                blocks.append((repeated, [], []))  # the next piece is its own part, its header repeated
                current = blocks[-1]
            blocks[-1] = (current[0] + text, current[1] + notes, current[2] + pictures)
        return blocks

    def _split(self, blocks: list[tuple[str, list[str], list[int]]]) -> None:
        text, notes, pictures = "", list[str](), list[int]()
        for block, block_notes, block_pictures in blocks:
            if not block:
                continue
            if text and len(text) + len(block) > PART_CHARS:
                self._unit(text, notes, pictures)
                text, notes, pictures = "", [], []
            text += block
            notes += block_notes
            pictures += block_pictures
        if text or not self._list:
            self._unit(text, notes, pictures)

    def _unit(self, text: str, notes: list[str], pictures: list[int]) -> None:
        number = len(self._list) + 1
        for picture in pictures:
            self.pictures[picture - 1] = (number, self.pictures[picture - 1][1])
        if notes:
            text += "".join(note.replace("\n", " ") + "\n" for note in notes)
        self._list.append({"text": text, "flags": [] if text.strip() else ["no_text"], "pictures": len(pictures),
                           "picture_numbers": pictures})

    def view(self, n: int, room: int) -> tuple[list[tuple[int | None, str | tuple[str, int, int, bytes]]], list[int]]:
        made_list: list[tuple[int | None, str | tuple[str, int, int, bytes]]] = []
        more: list[int] = []
        for number in self._list[n - 1]["picture_numbers"]:
            if room <= 0:
                more.append(number)  # not decoded: the model asks for it with pictures (review C2)
                continue
            made = self.picture(number)
            made_list.append((number, made))
            room -= not isinstance(made, str)
        return made_list, more


def _heading(name: str) -> int:
    lowered = name.lower().replace(" ", "")
    if lowered == "title":
        return 1
    match = re.fullmatch(r"heading([1-6])", lowered)
    return int(match.group(1)) if match else 0


def open_document(data: bytes) -> Document:
    kind = detect(data)
    if kind == "pdf":
        return Pdf(data)
    if kind == "docx":
        return Docx(data)
    raise Refused(f"later:{kind}")  # V3-P70 §16: XLSX, PPTX and HWPX come in part 2


# --- requests -----------------------------------------------------------------------------


def selected(pieces: list[dict[str, Any]], units: int) -> Iterator[tuple[int, int]]:
    """(unit, start) in order, past the end left out; pieces come normalized from the server."""
    for piece in pieces:
        first, last, start = int(piece["first"]), piece.get("last"), int(piece.get("start", 0))
        last = units if last is None else min(int(last), units)
        for n in range(first, last + 1):
            yield n, start if n == first else 0


def read(request: dict[str, Any]) -> None:
    data = base64.b64decode(request["data"], validate=True)
    document = open_document(data)
    units = document.count()
    head = {"ok": True, "format": type(document).__name__.lower(), "units": units,
            "unit_name": document.unit_name, "picture_total": len(document.pictures)}
    if request.get("pictures") is not None:
        read_pictures(document, request["pictures"], head)
    else:
        read_pages(document, request, head)


def send_images(images: list[tuple[int, int | None, tuple[str, int, int, bytes]]]) -> None:
    for unit, number, (media_type, _, _, blob) in images:
        answer(unit=unit, picture=number, media_type=media_type, data=base64.b64encode(blob).decode("ascii"))


def read_pictures(document: Document, pieces: list[list[int | None]], head: dict[str, Any]) -> None:
    total = len(document.pictures)
    wanted = [k for first, last in pieces
              for k in range(int(first or 1), min(total if last is None else int(last), total) + 1)]
    images: list[tuple[int, int | None, tuple[str, int, int, bytes]]] = []
    skipped: list[dict[str, Any]] = []
    examined = 0
    for number in wanted:
        if len(images) == VIEW_UNITS:
            break
        examined = number
        unit = document.pictures[number - 1][0]
        made = document.picture(number)
        if isinstance(made, str):
            skipped.append({"unit": unit, "picture": number, "reason": made})
        else:
            images.append((unit, number, made))
    answer(**head, parts=[], images=len(images), skipped=skipped, examined=examined, more=[])
    send_images(images)


def read_pages(document: Document, request: dict[str, Any], head: dict[str, Any]) -> None:
    units = document.count()
    view = bool(request.get("view"))
    budget = int(request["text_budget"])
    parts: list[dict[str, Any]] = []
    images: list[tuple[int, int | None, tuple[str, int, int, bytes]]] = []
    skipped: list[dict[str, Any]] = []
    more: list[int] = []
    for count, (n, start) in enumerate(selected(request["pages"], units)):
        if view and count == VIEW_UNITS:
            break
        text = document.text(n)
        if start > len(text) or (start and start < len(text) and text[start - 1] == "\\"
                                 and (start == 1 or text[start - 2] == "\n")):
            raise Refused("position_past_end" if start > len(text) else "bad_position")
        unit = document.unit(n)
        middle = mid_line(text, start)
        room = budget - PART_OVERHEAD - (MID_LINE_BYTES if middle else 0)
        if room <= 0:
            break  # no room for even a header: empty units stop here too (review B2)
        end = cut_at(text, start, room)
        if end == start and start < len(text):
            break  # nothing of it fits: it is where the next call starts
        budget = room - len(text[start:end].encode("utf-8", "surrogatepass"))
        part = {"n": n, "flags": unit["flags"], "pictures": unit["pictures"], "from": start, "to": end,
                "length": len(text), "text": text[start:end]}
        if "label" in unit:
            part["label"] = unit["label"]
        if middle:
            part["mid_line"] = True
        parts.append(part)
        if view and start == 0:
            made_list, left = document.view(n, VIEW_UNITS - len(images))
            for number, made in made_list:
                if isinstance(made, str):
                    skipped.append({"unit": n, "picture": number, "reason": made})
                else:
                    images.append((n, number, made))
            more += left
        if end < len(text):
            break
    answer(**head, parts=parts, images=len(images), skipped=skipped, examined=0, more=more)
    send_images(images)


def probe(request: dict[str, Any]) -> None:
    versions: dict[str, str] = {}
    from importlib import metadata

    for name in ("pypdfium2", "pypdf", "python-docx", "openpyxl", "python-pptx", "reportlab", "pillow"):
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            pass
    texts = {}
    for kind in ("pdf", "docx"):
        document = open_document(base64.b64decode(request[kind], validate=True))
        texts[kind] = "".join(document.text(n + 1) for n in range(document.count()))
    answer(ok=True, versions=versions, split_rule=SPLIT_RULE, texts=texts)


def main() -> None:
    limit()
    if len(sys.argv) > 1:
        sys.path.insert(0, sys.argv[1])
    line = sys.stdin.buffer.readline()
    try:
        request = json.loads(line)
        op = request["op"]
    except (ValueError, KeyError, TypeError):
        answer(ok=False, reason="bad_request")
        return
    try:
        (probe if op == "probe" else read)(request)
    except Refused as refused:
        answer(ok=False, reason=str(refused))
    except MemoryError:
        answer(ok=False, reason="out_of_memory")
    except Exception as error:  # noqa: BLE001 — any parse problem is "could not read", said in one word
        answer(ok=False, reason=type(error).__name__)


if __name__ == "__main__":
    main()
