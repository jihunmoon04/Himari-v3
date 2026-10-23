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
import itertools
import json
import re
import resource
import sys
import zipfile
from collections.abc import Iterator
from typing import Any

SPLIT_RULE = 2  # bump whenever the layout or the part split changes: positions and numbers move with it
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

    def gap(self) -> None:
        """A space between pieces of a line, none at its start or after another."""
        self._flush()
        if not self._at_start and not self.out[-1][-1:].isspace():
            self.out.append(" ")

    def end_line(self) -> None:
        self._flush()
        if self.out and self.out[-1] == " ":  # a gap that ended the line
            self.out.pop()
            self._at_start = not self.out or self.out[-1].endswith("\n")
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

    def slice(self, n: int, start: int, room: int) -> dict[str, Any] | None:
        """Unit ``n`` from ``start``, within ``room`` UTF-8 bytes (its
        "…" and repeated header included): {"from", "to", "length",
        "text", "mid_line", "header"}; None when nothing of it fits."""
        text = self.text(n)
        if start > len(text):
            raise Refused("position_past_end")
        if 0 < start < len(text) and text[start - 1] == "\\" and (start == 1 or text[start - 2] == "\n"):
            raise Refused("bad_position")
        middle = mid_line(text, start)
        room -= MID_LINE_BYTES if middle else 0
        end = cut_at(text, start, room) if room > 0 else start
        if end == start and start < len(text):
            return None
        return {"from": start, "to": end, "length": len(text), "text": text[start:end], "mid_line": middle,
                "header": None}

    def view(self, n: int, room: int) -> tuple[list[tuple[int | None, str | tuple[str, int, int, bytes]]], list[int]]:
        """Images for unit ``n``, at most ``room`` made: (picture number or
        None, made image or why not), and the picture numbers left unmade.
        This one: the unit's pictures, in order."""
        made_list: list[tuple[int | None, str | tuple[str, int, int, bytes]]] = []
        more: list[int] = []
        for number in self.unit(n)["picture_numbers"]:
            if room <= 0:
                more.append(number)  # not decoded: the model asks for it with pictures (review C2)
                continue
            made = self.picture(number)
            made_list.append((number, made))
            room -= not isinstance(made, str)
        return made_list, more

    def picture(self, number: int) -> tuple[str, int, int, bytes] | str:
        """A picture made ready, or why not: one broken picture never fails the call (review C2)."""
        try:
            return shrink(self.pictures[number - 1][1]())
        except MemoryError:
            return "out of memory"
        except Exception as error:  # noqa: BLE001 — truncated, a decompression bomb, a codec error
            return f"could not be decoded ({type(error).__name__})"


def table_blocks(rows: list[tuple[list[str], list[str], list[int]]]) -> list[tuple[str, list[str], list[int]]]:
    """A markdown table from cells already cleaned; one too large for a part
    is split at rows, its header row repeated without marks (review N5)."""
    if not rows:
        return []

    def line(cells: list[str]) -> str:
        return "| " + " | ".join(cells) + " |\n"

    header = line(rows[0][0]) + "|" + " --- |" * max(1, len(rows[0][0])) + "\n"
    repeated = _MARKS.sub("", header)
    blocks: list[tuple[str, list[str], list[int]]] = [(header, list(rows[0][1]), list(rows[0][2]))]
    for cells, notes, pictures in rows[1:]:
        text, current = line(cells), blocks[-1]
        if len(current[0]) + len(text) > PART_CHARS and current[0] not in (header, repeated):
            blocks.append((repeated, [], []))  # the next piece is its own part, its header repeated
            current = blocks[-1]
        blocks[-1] = (current[0] + text, current[1] + notes, current[2] + pictures)
    return blocks


def cell_text(value: str) -> str:
    return one_line(value).replace("|", "\\|")


class Flowing(Document):
    """A document without pages (DOCX, HWPX): its blocks cut into parts of
    about PART_CHARS at block boundaries, footnotes at the end of the part."""

    unit_name = "part"

    def __init__(self) -> None:
        super().__init__()
        self._list: list[dict[str, Any]] = []

    def count(self) -> int:
        return len(self._list)

    def unit(self, n: int) -> dict[str, Any]:
        return self._list[n - 1]

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
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"


def _as_plain_type(data: bytes, kind: str) -> bytes:
    """python-docx and python-pptx open the plain document type only: a
    template, slideshow or macro-enabled file is read through a copy that
    says it is one."""
    plain, *others = _MAIN_TYPES[kind]
    source = zipfile.ZipFile(io.BytesIO(data))
    types = source.read("[Content_Types].xml").decode("utf-8")
    fixed = re.sub(r'ContentType="application/vnd\.(?:openxmlformats-officedocument\.)?(?:'
                   + "|".join(re.escape(other) for other in others) + ')"',
                   f'ContentType="application/vnd.openxmlformats-officedocument.{plain}"', types)
    if fixed == types:
        return data
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as copy:
        for info in source.infolist():
            copy.writestr(info, fixed if info.filename == "[Content_Types].xml" else source.read(info))
    return out.getvalue()


class Docx(Flowing):
    def __init__(self, data: bytes) -> None:
        super().__init__()
        import docx

        try:
            self._doc = docx.Document(io.BytesIO(_as_plain_type(data, "docx")))
        except Exception:  # noqa: BLE001 — anything python-docx cannot open
            raise Refused("broken") from None
        self._part = self._doc.part
        self._styles = {s.style_id: str(s.name or "") for s in self._doc.styles}
        self._notes = self._footnotes()
        blocks: list[tuple[str, list[str], list[int]]] = []  # (text, footnote lines, picture numbers)
        self._blocks(self._doc.element.body, blocks)
        self._split(blocks)

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
        return cell_text(lines.result())

    def _table(self, element: Any) -> list[tuple[str, list[str], list[int]]]:
        rows: list[tuple[list[str], list[str], list[int]]] = []
        for row in element.iterchildren(W + "tr"):
            notes: list[str] = []
            pictures: list[int] = []
            rows.append(([self._cell(cell, notes, pictures) for cell in row.iterchildren(W + "tc")], notes, pictures))
        return table_blocks(rows)


def _heading(name: str) -> int:
    lowered = name.lower().replace(" ", "")
    if lowered == "title":
        return 1
    match = re.fullmatch(r"heading([1-6])", lowered)
    return int(match.group(1)) if match else 0


# --- XLSX --------------------------------------------------------------------------------

SML = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
XDR = "{http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing}"
PKG = "{http://schemas.openxmlformats.org/package/2006/relationships}"
MAX_COLUMNS = 100  # columns shown per sheet; the rest is said, for run_command
_CELL = re.compile(r"([A-Z]{1,3})(\d{1,7})")


def _relationships(archive: zipfile.ZipFile, part: str) -> dict[str, tuple[str, str]]:
    """{Id: (Type, target part)} of an OPC part; external targets left out."""
    import posixpath
    import xml.etree.ElementTree as ET

    folder, name = posixpath.split(part)
    try:
        root = ET.fromstring(archive.read(posixpath.join(folder, "_rels", name + ".rels")))
    except (KeyError, ET.ParseError):
        return {}
    found = {}
    for rel in root.iter(PKG + "Relationship"):
        target, kind = rel.get("Target") or "", rel.get("Type") or ""
        if rel.get("TargetMode") == "External" or not target:
            continue
        path = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(folder, target))
        found[rel.get("Id") or ""] = (kind, path)
    return found


def _column(letters: str) -> int:
    number = 0
    for letter in letters:
        number = number * 26 + ord(letter) - 64
    return number


def _letters(number: int) -> str:
    out = ""
    while number:
        number, rest = divmod(number - 1, 26)
        out = chr(65 + rest) + out
    return out


def _value(value: object) -> str:
    import datetime

    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, datetime.datetime):
        return value.date().isoformat() if value.time() == datetime.time() else value.isoformat(sep=" ")
    if isinstance(value, (datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, float):
        return repr(value)
    text = cell_text(str(value))
    # A text that starts like the mark of a formula with no stored value ("=B2*2") is escaped (§4.1).
    return "\\" + text if text.startswith(("=", "\\")) else text


class Xlsx(Document):
    """Sheets, each a table of its stored values laid out row by row and
    only as far as asked (§4.2: the rest is not looked at)."""

    unit_name = "sheet"

    def __init__(self, data: bytes) -> None:
        super().__init__()
        import openpyxl

        try:
            self._book = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            self._zip = zipfile.ZipFile(io.BytesIO(data))
        except Exception:  # noqa: BLE001 — anything openpyxl cannot open
            raise Refused("broken") from None
        self._sheets = list(self._book._sheets)  # worksheets and chart sheets, in the workbook's order
        self._placed: list[list[tuple[str, int]]] = []  # per sheet: (cell, picture number)
        for index, sheet in enumerate(self._sheets, start=1):
            placed = []
            for cell, target in self._drawn(sheet):
                self.pictures.append((index, lambda target=target: self._zip.read(target)))
                placed.append((cell, len(self.pictures)))
            self._placed.append(placed)

    def _path(self, sheet: Any) -> str | None:
        path = getattr(sheet, "_worksheet_path", None)
        return str(path).lstrip("/") if path else None

    def _drawn(self, sheet: Any) -> list[tuple[str, str]]:
        """The pictures anchored on a sheet: (top-left cell, media part), in drawing order."""
        import xml.etree.ElementTree as ET

        path = self._path(sheet)
        if path is None:
            return []
        found = []
        for kind, drawing in _relationships(self._zip, path).values():
            if not kind.endswith("/drawing"):
                continue
            media = _relationships(self._zip, drawing)
            try:
                root = ET.fromstring(self._zip.read(drawing))
            except (KeyError, ET.ParseError):
                continue
            for anchor in root:
                start = anchor.find(XDR + "from")
                cell = "?"
                if start is not None:
                    column, row = start.findtext(XDR + "col"), start.findtext(XDR + "row")
                    if column and row and column.isdigit() and row.isdigit():
                        cell = f"{_letters(int(column) + 1)}{int(row) + 1}"
                for blip in anchor.iter(A + "blip"):  # a group anchor holds several
                    if (blip.get(R + "embed") or "") in media:
                        found.append((cell, media[blip.get(R + "embed") or ""][1]))
        return found

    def count(self) -> int:
        return len(self._sheets)

    def unit(self, n: int) -> dict[str, Any]:
        sheet = self._sheets[n - 1]
        flags = ["hidden"] if getattr(sheet, "sheet_state", "visible") != "visible" else []
        if not hasattr(sheet, "iter_rows"):
            flags.append("no_text")
        return {"flags": flags, "pictures": len(self._placed[n - 1]),
                "picture_numbers": [number for _, number in self._placed[n - 1]]}

    def text(self, n: int) -> str:
        return "".join(line for line, _ in self._lines(n))

    def _parser(self, sheet: Any, source: Any) -> Any:
        """openpyxl's own sheet parser, as its read-only sheets use it, that
        also keeps the formula of a cell with no stored value."""
        from openpyxl.worksheet._reader import FORMULA_TAG, WorkSheetParser

        book = self._book
        parser = WorkSheetParser(source, sheet._shared_strings, data_only=True, epoch=book.epoch,
                                 date_formats=book._date_formats, timedelta_formats=book._timedelta_formats)
        read_cell = parser.parse_cell

        def parse_cell(element: Any) -> dict[str, Any]:
            cell: dict[str, Any] = read_cell(element)
            if element.find(FORMULA_TAG) is not None:
                formula = parser.parse_formula(element)  # every formula, so that shared ones translate
                text = formula if isinstance(formula, str) else getattr(formula, "text", None)
                if cell["value"] is None and isinstance(text, str) and text != "=":
                    cell["formula"] = one_line(text)
            return cell

        parser.parse_cell = parse_cell
        return parser

    @staticmethod
    def _row(cells: list[dict[str, Any]]) -> tuple[list[str], bool]:
        """A row's shown values up to its last one within MAX_COLUMNS, and whether it has values past them."""
        shown: dict[int, str] = {}
        wider = False
        for cell in cells:
            column = cell["column"]
            text = cell["formula"] if "formula" in cell else _value(cell["value"])
            if not text:
                continue
            if column > MAX_COLUMNS:
                wider = True
            elif column >= 1:
                shown[column] = text
        return [shown.get(c, "") for c in range(1, max(shown, default=0) + 1)], wider

    def _lines(self, n: int) -> Iterator[tuple[str, str]]:
        """(line, kind) of sheet ``n``: "head", "header" (the table's
        column row and rule), "row", "end". The sheet's XML is read only as
        far as the lines are taken, and nothing in a line depends on a
        later row (§4.2's invariant). Rows are the ones the XML holds with
        a value, under their own numbers; the declared size bounds nothing."""
        sheet = self._sheets[n - 1]
        path = self._path(sheet)
        if not hasattr(sheet, "iter_rows") or path is None:
            yield "[chart sheet: no cells]\n", "end"
            return
        try:
            size = str(sheet.calculate_dimension())
        except Exception:  # noqa: BLE001 — a sheet that does not say its size
            size = ""
        known = bool(size) and size not in ("A1", "A1:A1")
        yield f'Sheet "{one_line(sheet.title)}" ({size if known else "size not given"})\n', "head"
        hidden = ("1", "true")
        wider = False
        with self._zip.open(path) as source:
            parser = self._parser(sheet, source)
            # A row's attributes are popped as it is read: a long sheet keeps none of them.
            rows = ((number, *self._row(cells), parser.row_dimensions.pop(str(number), None) or {})
                    for number, cells in parser.parse())
            peeked = next((row for row in rows if row[1] or row[2]), None)
            # <cols> comes before the rows: the hidden columns are known once the first row is read.
            hidden_columns: set[int] = set()
            for attrs in parser.column_dimensions.values():
                low, high = attrs.get("min") or "", attrs.get("max") or ""
                if attrs.get("hidden") in hidden and low.isdigit() and high.isdigit():
                    hidden_columns.update(range(int(low), min(int(high), MAX_COLUMNS) + 1))
            # The heading row's width: the declared size, else the first row's (never a later row: §4.2).
            width = sheet.max_column if known and sheet.max_column else len(peeked[1]) if peeked else 1
            columns = max(1, min(width, MAX_COLUMNS))
            names = [f"{_letters(c)} [hidden]" if c in hidden_columns else _letters(c) for c in range(1, columns + 1)]
            yield "| row | " + " | ".join(names) + " |\n", "header"
            yield "| --- |" + " --- |" * columns + "\n", "header"
            for number, shown, more, attrs in itertools.chain([peeked] if peeked else [], rows):
                wider = wider or more
                if not shown:
                    continue
                mark = f"{number} [hidden]" if attrs.get("hidden") in hidden else str(number)
                yield "| " + " | ".join([mark, *shown]) + " |\n", "row"
        if wider:
            yield f"[columns after {_letters(MAX_COLUMNS)} not shown]\n", "end"
        for cell, number in self._placed[n - 1]:
            yield f"[picture {number} at {cell}]\n", "end"

    def slice(self, n: int, start: int, room: int) -> dict[str, Any] | None:
        """Laid out only as far as the room goes: length is known only when
        the sheet ended within it (§4.2, re-check N3)."""
        header: list[str] = []
        taken: list[str] = []
        pos = used = 0
        middle = begun = False
        repeat = ""
        ended = True
        for line, kind in self._lines(n):
            if kind == "header":
                header.append(line)
            if not begun:
                if pos + len(line) <= start and not (pos == start and start == 0):
                    pos += len(line)
                    continue
                begun = True
                if start > pos:
                    middle, line, pos = True, line[start - pos:], start
                elif start > 0 and kind == "row":
                    repeat = "".join(header)  # resumed at a row: the column row and rule again
                room -= (MID_LINE_BYTES if middle else 0) + len(repeat.encode("utf-8"))
                if room <= 0:
                    return None
            size = len(line.encode("utf-8", "surrogatepass"))
            if used + size <= room:
                taken.append(line)
                used += size
                pos += len(line)
                continue
            if not taken:  # one line larger than the room: cut at a character, the rest read on mid-line
                end = cut_at(line, 0, room)
                if end == 0:
                    return None
                taken.append(line[:end])
                pos += end
            ended = False
            break
        if not begun:
            if start > pos:
                raise Refused("position_past_end")
            middle = False  # at the very end: an empty slice
        text = "".join(taken)
        return {"from": start, "to": start + len(text), "length": pos if ended else None, "text": text,
                "mid_line": middle, "header": repeat or None}


# --- PPTX --------------------------------------------------------------------------------


class Pptx(Document):
    """Slides, each its shapes' text in reading order (top to bottom, left
    to right), tables and charts as tables, pictures marked, notes last.
    A shape hidden in the selection pane or lying off the slide is marked
    [hidden: …] whatever it holds (§4.1)."""

    unit_name = "slide"

    def __init__(self, data: bytes) -> None:
        super().__init__()
        import pptx
        from pptx.shapes.group import GroupShape
        from pptx.shapes.picture import Picture

        try:
            self._deck = pptx.Presentation(io.BytesIO(_as_plain_type(data, "pptx")))
        except Exception:  # noqa: BLE001 — anything python-pptx cannot open
            raise Refused("broken") from None
        self._group_type = GroupShape
        self._picture_type = Picture
        self._slides = list(self._deck.slides)
        self._size = (self._deck.slide_width or 0, self._deck.slide_height or 0)
        self._numbers: list[list[int]] = []
        for index, slide in enumerate(self._slides, start=1):
            numbers = []
            for shape, _, _ in self._ordered(slide.shapes):
                if isinstance(shape, Picture):
                    self.pictures.append((index, lambda shape=shape: shape.image.blob))
                    numbers.append(len(self.pictures))
            self._numbers.append(numbers)
        self._units: dict[int, dict[str, Any]] = {}

    def _ordered(self, shapes: Any, frame: tuple[float, float, float, float] = (0, 0, 1, 1),
                 hidden: bool = False) -> list[tuple[Any, tuple[float, float, float, float], bool]]:
        """(shape, frame, hidden) with groups flattened: the frame (x, y, sx, sy)
        maps the shape's coordinates onto the slide, which differ from them
        inside a group; hidden is the selection pane's, the group's included."""
        found: list[tuple[Any, tuple[float, float, float, float], bool]] = []
        for shape in sorted(shapes, key=lambda s: (s.top or 0, s.left or 0)):
            name = shape._element[0].find(P + "cNvPr") if len(shape._element) else None
            gone = hidden or (name is not None and name.get("hidden") in ("1", "true"))
            if isinstance(shape, self._group_type):
                found += self._ordered(shape.shapes, self._inner(shape, frame), gone)
            else:
                found.append((shape, frame, gone))
        return found

    @staticmethod
    def _inner(group: Any, frame: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
        x, y, sx, sy = frame
        xfrm = group._element.find(f"{P}grpSpPr/{A}xfrm")
        if xfrm is None:
            return frame

        def pair(tag: str, first: str, second: str) -> tuple[float, float] | None:
            node = xfrm.find(A + tag)
            try:
                return (float(node.get(first)), float(node.get(second))) if node is not None else None
            except (TypeError, ValueError):
                return None

        off, ext, child_off, child_ext = (pair("off", "x", "y"), pair("ext", "cx", "cy"),
                                          pair("chOff", "x", "y"), pair("chExt", "cx", "cy"))
        if off is None or ext is None or child_off is None or child_ext is None:
            return frame
        kx = ext[0] / child_ext[0] if child_ext[0] else 1.0
        ky = ext[1] / child_ext[1] if child_ext[1] else 1.0
        return x + sx * (off[0] - child_off[0] * kx), y + sy * (off[1] - child_off[1] * ky), sx * kx, sy * ky

    def _off(self, shape: Any, frame: tuple[float, float, float, float]) -> bool:
        width, height = self._size
        if None in (shape.left, shape.top, shape.width, shape.height) or not width or not height:
            return False
        x, y, sx, sy = frame
        left, top = x + sx * shape.left, y + sy * shape.top
        right, bottom = left + sx * shape.width, top + sy * shape.height
        return bool(left >= width or top >= height or right <= 0 or bottom <= 0)

    def count(self) -> int:
        return len(self._slides)

    def unit(self, n: int) -> dict[str, Any]:
        if n not in self._units:
            slide = self._slides[n - 1]
            lines = Lines()
            numbers = iter(self._numbers[n - 1])
            for shape, frame, gone in self._ordered(slide.shapes):
                why = "a hidden shape" if gone else "off the slide" if self._off(shape, frame) else ""
                mark = f"[hidden: {why}]" if why else ""
                if isinstance(shape, self._picture_type):
                    lines.mark(f"{mark} [picture {next(numbers)}]\n" if mark else f"[picture {next(numbers)}]\n")
                    continue
                if getattr(shape, "has_chart", False):
                    lines.mark((mark + "\n" if mark else "") + self._chart(shape.chart))
                if getattr(shape, "has_table", False):
                    rows: list[tuple[list[str], list[str], list[int]]] = [
                        ([cell_text(cell.text) for cell in row.cells], [], []) for row in shape.table.rows]
                    lines.mark((mark + "\n" if mark else "") + "".join(block for block, _, _ in table_blocks(rows)))
                if getattr(shape, "has_text_frame", False):
                    for paragraph in shape.text_frame.paragraphs:
                        text = paragraph.text  # its line breaks are "\v", made "\n" by clean()
                        if not text.strip():
                            continue
                        if mark:  # hidden text stays on its marked line
                            lines.mark(mark + " ")
                            text = one_line(text)
                        lines.text(text)
                        lines.end_line()
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
                notes = slide.notes_slide.notes_text_frame.text
                if notes.strip():
                    lines.mark("Notes: ")
                    lines.text(notes)
                    lines.end_line()
            text = lines.result()
            flags = ["hidden"] if slide._element.get("show") in ("0", "false") else []
            if not text.strip():
                flags.append("no_text")
            self._units[n] = {"text": text, "flags": flags, "pictures": len(self._numbers[n - 1]),
                              "picture_numbers": self._numbers[n - 1]}
        return self._units[n]

    @staticmethod
    def _chart(chart: Any) -> str:
        try:
            title = one_line(chart.chart_title.text_frame.text) if chart.has_title else ""
            plot = chart.plots[0]
            categories = [cell_text(str(c)) for c in plot.categories]
            rows: list[tuple[list[str], list[str], list[int]]] = [(["series", *categories], [], [])]
            for series in plot.series:
                rows.append(([cell_text(series.name or ""), *(_value(v) for v in series.values)], [], []))
            table = "".join(block for block, _, _ in table_blocks(rows))
        except Exception:  # noqa: BLE001 — a chart whose data cannot be read is still a chart
            return "[chart]\n"
        return f"[chart: {title.replace(']', ')')}]\n" + table if title else "[chart]\n" + table


# --- HWPX --------------------------------------------------------------------------------


def _local(tag: object) -> str:
    return str(tag).rsplit("}", 1)[-1]


class Hwpx(Flowing):
    """Hancom's open format: the sections the package's spine lists, their
    paragraphs and tables laid out like a Word file's."""

    def __init__(self, data: bytes) -> None:
        super().__init__()
        import xml.etree.ElementTree as ET

        try:
            self._zip = zipfile.ZipFile(io.BytesIO(data))
            package = ET.fromstring(self._zip.read("Contents/content.hpf"))
        except (KeyError, zipfile.BadZipFile, ET.ParseError):
            raise Refused("broken") from None
        items = {item.get("id") or "": item.get("href") or "" for item in package.iter()
                 if _local(item.tag) == "item"}
        self._items = items
        spine = [items.get(ref.get("idref") or "", "") for ref in package.iter() if _local(ref.tag) == "itemref"]
        sections = [href for href in spine if re.fullmatch(r"Contents/section\d+\.xml", href)]
        if not sections:  # no spine: the section parts by their number, never by name (re-review N6)
            sections = sorted((n for n in self._zip.namelist() if re.fullmatch(r"Contents/section\d+\.xml", n)),
                              key=lambda n: int(re.sub(r"\D", "", n)))
        self._note = 0
        blocks: list[tuple[str, list[str], list[int]]] = []
        try:
            for section in sections:
                for child in ET.fromstring(self._zip.read(section)):
                    if _local(child.tag) == "p":
                        self._paragraph(child, blocks)
        except (KeyError, ET.ParseError):
            raise Refused("broken") from None
        self._split(blocks)

    def _picture(self, element: Any) -> int | None:
        for node in element.iter():
            ref = node.get("binaryItemIDRef")
            if ref and self._items.get(ref) in self._zip.namelist():
                href = self._items[ref]
                self.pictures.append((0, lambda href=href: self._zip.read(href)))
                return len(self.pictures)
        return None

    def _inline(self, run: Any, state: list[Any], table: Any) -> None:
        """One run's text, pictures and notes, in order, into ``state``
        ([lines, notes, pictures]); a table it holds goes to ``table`` at
        its place, which may start ``state`` anew."""
        for item in run:
            lines, notes, pictures = state
            name = _local(item.tag)
            if name == "t":
                lines.text(item.text or "")
                for part in item:
                    lines.text({"tab": "\t", "lineBreak": "\n"}.get(_local(part.tag), "") + (part.tail or ""))
            elif name == "tbl":
                table(self._table(item))
            elif name == "ctrl":
                for control in item:
                    if _local(control.tag) in ("footNote", "endNote"):
                        self._note += 1
                        texts = [t.text or "" for t in control.iter() if _local(t.tag) == "t"]
                        lines.mark(f"[^{self._note}]")
                        notes.append(f"[^{self._note}]: {one_line(' '.join(texts))}")
            else:
                self._drawn(item, state, table)

    def _drawn(self, item: Any, state: list[Any], table: Any) -> None:
        """A picture, or a drawing object (a text box, a group of them) and the text inside it."""
        name = _local(item.tag)
        if name == "pic":
            number = self._picture(item)
            if number is not None:
                state[2].append(number)
                state[0].mark(f"[picture {number}]")
        elif name == "drawText":
            for paragraph in self._paragraphs(item):
                state[0].gap()
                for run in self._runs(paragraph):
                    self._inline(run, state, table)
            state[0].gap()
        else:
            for child in item:
                self._drawn(child, state, table)

    @staticmethod
    def _paragraphs(element: Any) -> Iterator[Any]:
        """The paragraphs of a cell or a text box: its own, not those of the tables or boxes inside them."""
        for sub in element:
            if _local(sub.tag) == "subList":
                yield from (p for p in sub if _local(p.tag) == "p")

    @staticmethod
    def _runs(paragraph: Any) -> Iterator[Any]:
        return (run for run in paragraph if _local(run.tag) == "run")

    def _paragraph(self, element: Any, blocks: list[tuple[str, list[str], list[int]]]) -> None:
        state: list[Any] = [Lines(), [], []]

        def ended() -> None:
            lines, notes, pictures = state
            lines.end_line()
            text = lines.result()
            if text.strip():
                blocks.append((text, notes, pictures))
            state[:] = [Lines(), [], []]

        def table(found: list[tuple[str, list[str], list[int]]]) -> None:
            ended()  # a table sits in the text: the paragraph so far, the table, then the rest
            blocks.extend(found)

        for run in self._runs(element):
            self._inline(run, state, table)
        ended()

    def _table(self, element: Any) -> list[tuple[str, list[str], list[int]]]:
        rows: list[tuple[list[str], list[str], list[int]]] = []
        for row in element:
            if _local(row.tag) != "tr":
                continue
            cells: list[str] = []
            notes: list[str] = []
            pictures: list[int] = []
            for cell in row:
                if _local(cell.tag) != "tc":
                    continue
                state: list[Any] = [Lines(), notes, pictures]

                def table(found: list[tuple[str, list[str], list[int]]], state: list[Any] = state) -> None:
                    for block, block_notes, block_pictures in found:  # a table in a cell: flattened into it
                        state[0].mark(" " + block.replace("\n", " ") + " ")
                        state[1] += block_notes
                        state[2] += block_pictures

                for paragraph in self._paragraphs(cell):
                    for run in self._runs(paragraph):
                        self._inline(run, state, table)
                    state[0].mark(" ")
                cells.append(cell_text(state[0].result()))
            rows.append((cells, notes, pictures))
        return table_blocks(rows)


def open_document(data: bytes) -> Document:
    kind = detect(data)
    if kind == "pdf":
        return Pdf(data)
    if kind == "docx":
        return Docx(data)
    return {"xlsx": Xlsx, "pptx": Pptx, "hwpx": Hwpx}[kind](data)


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
        room = budget - PART_OVERHEAD
        if room <= 0:
            break  # no room for even a header: empty units stop here too (review B2)
        piece = document.slice(n, start, room)
        if piece is None:
            break  # nothing of it fits: it is where the next call starts
        budget = room - len(piece["text"].encode("utf-8", "surrogatepass")) - len(
            (piece["header"] or "").encode("utf-8")) - (MID_LINE_BYTES if piece["mid_line"] else 0)
        unit = document.unit(n)
        part = {"n": n, "flags": unit["flags"], "pictures": unit["pictures"], "from": piece["from"],
                "to": piece["to"], "length": piece["length"], "text": piece["text"]}
        if "label" in unit:
            part["label"] = unit["label"]
        if piece["mid_line"]:
            part["mid_line"] = True
        if piece["header"]:
            part["header"] = piece["header"]
        parts.append(part)
        if view and start == 0:
            made_list, left = document.view(n, VIEW_UNITS - len(images))
            for number, made in made_list:
                if isinstance(made, str):
                    skipped.append({"unit": n, "picture": number, "reason": made})
                else:
                    images.append((n, number, made))
            more += left
        if piece["length"] is None or piece["to"] < piece["length"]:
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
