"""Reading documents in the documents sandbox (V3-P70 §5, §6).

``open_toolkit`` checks the documents toolkit once at startup: the folder
``current`` names under ``HIMARI_DOCUMENTS_DIR``, opened and walked on its
descriptor (owners, write bits, no links), apart from HIMARI's data. The
server keeps that descriptor to the end: ``run_command`` binds it, and the
reader makes sure the path it launches with is still that folder.

``DocumentReader`` runs ``worker.py`` in its own server sandbox (not the
images' one, so neither's setup failure turns the other off), with the
toolkit and the worker's folder read-only, nothing writable but a 64 MiB
/tmp. One process per call; a wall-clock bound on top of the worker's own
limits; at most ``MAX_WORKERS`` at once. Every answer is checked again here
— units, positions, marks, text size, images by their bytes — and anything
off is "a bad answer", never passed on.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import re
import stat
import zipfile
from collections.abc import Callable, Iterator
from itertools import islice
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from himari.data.api import IMAGE_HEAD_BYTES, sniff_image
from himari.documents.api import (
    LABEL_CHARS,
    MAX_UNITS,
    PART_OVERHEAD,
    UNIT_FLAGS,
    VIEW_LIMIT,
    DocumentRefused,
    DocumentsUnavailable,
    Part,
    Picture,
    Piece,
    Reading,
    Skipped,
)
from himari.sandbox.api import ProcessLauncher, SandboxAdmissionError, SandboxUnavailableError

log = logging.getLogger(__name__)

WORKER = Path(__file__).with_name("worker.py")
PYTHON = "/usr/bin/python3"  # the interpreter the toolkit is installed for (§6.2)
WALL_SECONDS = 60.0
MAX_WORKERS = 2  # each may take 1 GiB
TMP_BYTES = 64 * 1024 * 1024  # the worker's /tmp (§5, re-review N1)
MAX_TOOLKIT_ENTRIES = 100_000
LONG_EDGE = 1568
_REASON = re.compile(r"[a-z_]{1,40}(:[a-z]{1,10})?")
_LABEL = re.compile(r"[^\s\x00-\x1f\x7f-\x9f\u2028\u2029]{1,%d}" % LABEL_CHARS)
_FORMATS = {"pdf": "page", "docx": "part", "xlsx": "sheet", "pptx": "slide", "hwpx": "part"}
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class ToolkitRefused(Exception):
    """The documents toolkit is missing or cannot be trusted; the message says why."""


@dataclass
class Toolkit:
    path: str  # the version folder (``current`` resolved once, at startup)
    fd: int  # that folder, open until close()
    version: str
    manifest: dict[str, Any] = field(default_factory=dict)

    @property
    def site_packages(self) -> str:
        return os.path.join(self.path, "site-packages")

    def same(self) -> bool:
        """Whether the path still names the folder checked at startup."""
        try:
            now, then = os.stat(self.path), os.fstat(self.fd)
        except OSError:
            return False
        return (now.st_dev, now.st_ino) == (then.st_dev, then.st_ino)

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


def _trusted(info: os.stat_result, owners: frozenset[int], what: str, *, ancestor: bool = False) -> None:
    if info.st_uid != 0 and info.st_uid not in owners:
        raise ToolkitRefused(f"{what} does not belong to root")
    # Only an ancestor may be a root-owned sticky folder such as /tmp: nobody
    # else can rename or remove what is ours in it (review N1).
    sticky = ancestor and stat.S_ISDIR(info.st_mode) and info.st_mode & stat.S_ISVTX and info.st_uid == 0
    if info.st_mode & 0o022 and not sticky:
        raise ToolkitRefused(f"{what} can be written by other users")


def _entries(root_fd: int) -> Iterator[tuple[str, os.stat_result]]:
    """Every entry below ``root_fd``, links not followed, each directory
    opened by name under its parent with O_NOFOLLOW."""
    pending: list[tuple[str, int]] = [("", os.dup(root_fd))]
    seen = 0
    while pending:
        relative, fd = pending.pop()
        try:
            with os.scandir(fd) as items:
                for item in items:
                    seen += 1
                    if seen > MAX_TOOLKIT_ENTRIES:
                        raise ToolkitRefused("the toolkit has too many entries")
                    name = f"{relative}/{item.name}" if relative else item.name
                    info = item.stat(follow_symlinks=False)
                    yield name, info
                    if stat.S_ISDIR(info.st_mode):
                        pending.append((name, os.open(item.name, _DIRECTORY, dir_fd=fd)))
        finally:
            os.close(fd)


def open_toolkit(base: str, *, owners: frozenset[int] = frozenset({0})) -> Toolkit:
    """The checked version folder ``base/current`` names (§6.2): ``base``,
    its ancestors, the link and every entry of the folder owned by root (or
    ``owners``: tests) and not writable by others; no link inside. Whether
    it is apart from HIMARI's data is Bootstrap's check, by identity.
    ToolkitRefused says why not."""
    if not os.path.isabs(base) or os.path.normpath(base) != base:
        raise ToolkitRefused("HIMARI_DOCUMENTS_DIR must be an absolute, normalized path")
    try:
        place = "/"
        for name in ["", *base.strip("/").split("/")]:
            place = os.path.join(place, name) if name else "/"
            info = os.lstat(place)
            if stat.S_ISLNK(info.st_mode):
                raise ToolkitRefused(f"{place} is a symbolic link")
            _trusted(info, owners, place, ancestor=place != base)
        link = os.path.join(base, "current")
        info = os.lstat(link)
        if not stat.S_ISLNK(info.st_mode):
            raise ToolkitRefused(f"{link} is not a link to a version folder")
        if info.st_uid != 0 and info.st_uid not in owners:
            raise ToolkitRefused(f"{link} does not belong to root")
        version = os.readlink(link)
        if "/" in version or version in ("", ".", ".."):
            raise ToolkitRefused(f"{link} must name a folder next to it")
        base_fd = os.open(base, _DIRECTORY)
        try:
            fd = os.open(version, _DIRECTORY, dir_fd=base_fd)
        finally:
            os.close(base_fd)
    except FileNotFoundError as error:
        raise ToolkitRefused(f"{error.filename or base} does not exist") from None
    except OSError as error:
        raise ToolkitRefused(f"the toolkit could not be opened ({type(error).__name__})") from None
    toolkit = Toolkit(os.path.join(base, version), fd, version)
    try:
        _trusted(os.fstat(fd), owners, toolkit.path)
        for name, entry in _entries(fd):
            if stat.S_ISLNK(entry.st_mode):
                raise ToolkitRefused(f"{name} in the toolkit is a symbolic link")
            _trusted(entry, owners, f"{name} in the toolkit")
        with open(os.open("MANIFEST.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd),
                  encoding="utf-8") as handle:
            manifest = json.load(handle)
        if not isinstance(manifest, dict):
            raise ToolkitRefused("MANIFEST.json is not an object")
        toolkit.manifest = manifest
    except ToolkitRefused:
        toolkit.close()
        raise
    except (OSError, ValueError) as error:
        toolkit.close()
        raise ToolkitRefused(f"the toolkit could not be checked ({type(error).__name__})") from None
    return toolkit


def probe_pdf(text: str) -> bytes:
    """A one-page PDF showing ``text`` (ASCII): what the startup check reads."""
    stream = f"BT /F1 12 Tf 10 50 Td ({text}) Tj ET".encode()
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] /Contents 4 0 R "
               b"/Resources << /Font << /F1 5 0 R >> >> >>",
               b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def probe_docx(text: str) -> bytes:
    """A one-paragraph DOCX holding ``text``."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("[Content_Types].xml", (
            '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/'
            'content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
            'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName='
            '"/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.'
            'document.main+xml"/></Types>'))
        archive.writestr("_rels/.rels", (
            '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/'
            '2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
            '2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'))
        archive.writestr("word/document.xml", (
            '<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/'
            f'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>'))
    return out.getvalue()


def worker_roots(toolkit: Toolkit) -> tuple[str, ...]:
    """What the worker reads: the toolkit and the worker's folder (unless
    under /usr, which the sandbox shows anyway, review N4)."""
    folder = str(WORKER.parent.resolve())
    usr = folder == "/usr" or folder.startswith("/usr/")
    return (toolkit.path,) if usr else (toolkit.path, folder)


def _one_line(text: object, limit: int = 80) -> str:
    return " ".join(str(text).split())[:limit]


class DocumentReader:
    """A ``DocumentReading`` over a launcher of the documents profile."""

    def __init__(self, launcher: ProcessLauncher, toolkit: Toolkit, *, python: str = PYTHON,
                 wall_seconds: float = WALL_SECONDS) -> None:
        self._launcher = launcher
        self.toolkit = toolkit
        self._python = python
        self._wall = wall_seconds
        self._places = asyncio.Semaphore(MAX_WORKERS)
        self.available = False
        self.versions: dict[str, str] = {}
        self.split_rule = 0
        self.scripts = False  # set by Bootstrap once run_command has the toolkit (§6.4)

    @property
    def salt(self) -> bytes:
        return f"{self.toolkit.version}\0{self.split_rule}".encode()

    async def check(self) -> str | None:
        """At startup: the worker reads a tiny PDF and DOCX and names the
        toolkit's libraries. None when it works, else why not."""
        self.available = True
        try:
            lines = await self._run(lambda: {"op": "probe",
                                             "pdf": base64.b64encode(probe_pdf("HIMARI-PROBE")).decode(),
                                             "docx": base64.b64encode(probe_docx("HIMARI-PROBE")).decode()}, 0)
            head = json.loads(lines[0])
            texts, versions, rule = head["texts"], head["versions"], head["split_rule"]
            if not head.get("ok"):
                raise DocumentRefused(_one_line(head.get("reason")))
            if not all("HIMARI-PROBE" in str(texts.get(kind)) for kind in ("pdf", "docx")):
                raise DocumentRefused("the probe documents were not read")
            if not (isinstance(rule, int) and rule > 0 and isinstance(versions, dict)):
                raise DocumentRefused("a bad answer")
        except Exception as error:  # noqa: BLE001 — any odd answer is "no documents", never a failed startup
            self.available = False
            return _one_line(getattr(error, "reason", None) or error or type(error).__name__)
        self.versions = {str(k)[:40]: str(v)[:40] for k, v in versions.items()}
        self.split_rule = rule
        return None

    async def read(self, data: bytes, *, pages: tuple[Piece, ...] | None,
                   pictures: tuple[tuple[int, int | None], ...] | None, view: bool, text_budget: int) -> Reading:
        if not self.available:
            raise DocumentsUnavailable("not available")

        def request() -> dict[str, Any]:  # built once a worker place is free, off the event loop (review C4)
            return {"op": "read", "data": base64.b64encode(data).decode("ascii"),
                    "pages": None if pages is None else [{"first": p.first, "last": p.last, "start": p.start}
                                                         for p in pages],
                    "pictures": None if pictures is None else [list(p) for p in pictures],
                    "view": view, "text_budget": text_budget}

        lines = await self._run(request, None)
        try:
            return await asyncio.to_thread(_checked, lines, pages, pictures, view, text_budget)
        except (ValueError, KeyError, TypeError, AttributeError, AssertionError):
            raise DocumentRefused("a bad answer") from None

    async def _run(self, request: Callable[[], dict[str, Any]], images: int | None) -> list[bytes]:
        """The worker's answer lines: the first, then as many image lines as
        it announces (``images`` None) or ``images``."""
        if not self.toolkit.same():
            raise DocumentsUnavailable("the toolkit is not the folder checked at startup")
        async with self._places:
            try:
                process = await self._launcher.launch(
                    [self._python, "-I", "-S", str(WORKER), self.toolkit.site_packages], {}, "/tmp")
            except (SandboxUnavailableError, SandboxAdmissionError) as error:
                raise DocumentsUnavailable(type(error).__name__) from None
            except Exception as error:  # noqa: BLE001 — refused or setup failed: nothing ran
                raise DocumentsUnavailable(type(error).__name__) from None
            try:
                async with asyncio.timeout(self._wall):
                    line = await asyncio.to_thread(lambda: json.dumps(request()).encode("ascii"))
                    await process.write_line(line)
                    del line
                    first = await process.read_line()
                    head = json.loads(first)
                    if not isinstance(head, dict):
                        raise DocumentRefused("a bad answer")
                    if not head.get("ok"):
                        reason = str(head.get("reason"))
                        raise DocumentRefused(reason if _REASON.fullmatch(reason) else "broken")
                    count = head.get("images", 0) if images is None else images
                    if not isinstance(count, int) or not 0 <= count <= VIEW_LIMIT:
                        raise DocumentRefused("a bad answer")
                    return [first, *[await process.read_line() for _ in range(count)]]
            except TimeoutError:
                raise DocumentRefused("timed_out") from None
            except DocumentRefused:
                raise
            except Exception as error:  # noqa: BLE001 — a crash, a line too long, bad JSON
                raise DocumentRefused(f"worker_{type(error).__name__.lower()}"[:40]) from None
            finally:
                await process.shutdown(0.5)


def _selection(pages: tuple[Piece, ...], units: int) -> Iterator[tuple[int, int]]:
    """(unit, start) in the order read, past the end left out; lazily, so a
    huge range costs only what is looked at (review B1, C1)."""
    for piece in pages:
        last = units if piece.last is None else min(piece.last, units)
        for n in range(piece.first, last + 1):
            yield n, piece.start if n == piece.first else 0


def _wanted(pictures: tuple[tuple[int, int | None], ...], total: int) -> Callable[[int], bool]:
    def wanted(k: int) -> bool:
        return 1 <= k <= total and any(first <= k and (last is None or k <= last) for first, last in pictures)
    return wanted


def _int(value: object, low: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < low:
        raise ValueError("not a count")
    return value


def _checked(lines: list[bytes], pages: tuple[Piece, ...] | None, pictures: tuple[tuple[int, int | None], ...] | None,
             view: bool, text_budget: int) -> Reading:
    """The worker's answer as a Reading, or ValueError (§5's re-check)."""
    head = json.loads(lines[0])
    format_name = head["format"]
    if format_name not in _FORMATS or head["unit_name"] != _FORMATS[format_name]:
        raise ValueError("format")
    units, total = _int(head["units"]), _int(head["picture_total"])
    if units > MAX_UNITS or total > MAX_UNITS:
        raise ValueError("too many units")
    raw_parts = head["parts"]
    if not isinstance(raw_parts, list) or len(raw_parts) > text_budget // PART_OVERHEAD + 1:
        raise ValueError("parts")  # each part costs the worker PART_OVERHEAD of its budget (review B2)
    parts: list[Part] = []
    if pages is not None:
        order = list(islice(_selection(pages, units), len(raw_parts) + 1))
        if len(raw_parts) > len(order) or (view and len(raw_parts) > VIEW_LIMIT):
            raise ValueError("too many parts")
        used = 0
        for index, (raw, (n, start)) in enumerate(zip(raw_parts, order, strict=False)):
            text, flags, label = raw["text"], raw["flags"], raw.get("label")
            begin, end, length = _int(raw["from"]), _int(raw["to"]), raw["length"]
            if raw["n"] != n or begin != start or not isinstance(text, str) or len(text) != end - begin:
                raise ValueError("position")
            if length is not None:
                _int(length)
                if end > length or (begin < length and begin == end):
                    raise ValueError("position")
            elif begin == end:
                raise ValueError("no progress")
            if not isinstance(flags, list) or not set(flags) <= UNIT_FLAGS:
                raise ValueError("flags")
            if label is not None and not (isinstance(label, str) and _LABEL.fullmatch(label)):
                raise ValueError("label")
            header = raw.get("header")
            if header is not None and not (
                    isinstance(header, str) and begin > 0 and raw.get("mid_line") is not True
                    and len(heading := header.splitlines(keepends=True)) == 2
                    and all(line.startswith("|") and line.endswith("|\n") for line in heading)):
                raise ValueError("header")  # the column row and its rule, only where a sheet resumes at a row
            part = Part(n, tuple(flags), _int(raw["pictures"]), begin, end, length, text, label,
                        raw.get("mid_line") is True, header)
            if part.cut and index != len(raw_parts) - 1:
                raise ValueError("a cut part is not last")
            used += len(text.encode("utf-8")) + len((header or "").encode("utf-8"))
            parts.append(part)
        if used > text_budget:
            raise ValueError("text over budget")
    elif raw_parts:
        raise ValueError("parts in pictures mode")
    wanted = _wanted(pictures if pictures is not None else (), total)
    viewed = {p.n for p in parts if p.start == 0} if view else set()
    images: list[Picture] = []
    for line in lines[1:]:
        raw = json.loads(line)
        unit, number, data = _int(raw["unit"], 1), raw["picture"], base64.b64decode(raw["data"], validate=True)
        sniffed = sniff_image(data[:IMAGE_HEAD_BYTES])
        if (sniffed is None or sniffed.media_type not in ("image/jpeg", "image/png")
                or sniffed.media_type != raw["media_type"] or max(sniffed.width, sniffed.height) > LONG_EDGE):
            raise ValueError("image")
        if pictures is not None:
            if not isinstance(number, int) or not wanted(number) or (images and images[-1].number is not None
                                                                       and number <= images[-1].number):
                raise ValueError("picture number")
        elif view:
            if unit not in viewed or (number is None) != (format_name == "pdf"):
                raise ValueError("image unit")
            if number is not None:
                _int(number, 1)
        else:
            raise ValueError("an image nobody asked for")
        images.append(Picture(unit, number, sniffed.media_type, data))
    skipped = tuple(Skipped(s.get("unit") if isinstance(s.get("unit"), int) else None,
                            s.get("picture") if isinstance(s.get("picture"), int) else None,
                            _one_line(s.get("reason"))) for s in head["skipped"][:50])
    more = tuple(_int(k, 1) for k in head["more"][:1000])
    return Reading(format_name, units, head["unit_name"], total, tuple(parts), tuple(images), skipped,
                   _int(head["examined"]), more)
