"""What the machine Tools' results look like to the model (FILE-03, V3-051).

Listing, reading and giving a file are formatted here
from what the runner answered. Every result is at most
MAX_OBSERVATION_BYTES of UTF-8, whatever produced it.
"""

from __future__ import annotations

import posixpath
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from himari.common import JsonValue, ModelToolResult, TextBlock
from himari.data.api import (
    DEFAULT_FILE_MAX_BYTES,
    IMAGE_HEAD_BYTES,
    PRESENT_LIMIT_BYTES,
    PRESENT_WINDOW,
    BlobStore,
    DataError,
    DataErrorCode,
    FileCatalog,
    PresentedFile,
    clean_file_name,
    clean_media_type,
    sniff_image,
)
from himari.tool.api import ResultImage, ToolContext, ToolOutcome, ToolStatus

MAX_READ_BYTES = 256 * 1024  # acquisition (V3-051)
MAX_OBSERVATION_BYTES = 64 * 1024  # shown to the model (V3-051)
MAX_WRITE_BYTES = 256 * 1024
MAX_LIST_ENTRIES = 1000


@dataclass(frozen=True)
class ToolFiles:
    """What ``present_file`` needs from Data (V3-P66 §7.1)."""

    blobs: BlobStore
    catalog: FileCatalog
    max_bytes: int = DEFAULT_FILE_MAX_BYTES


@dataclass(frozen=True)
class Entry:
    name: str
    kind: str  # "file" | "directory" | "symlink" | "other"
    size: int | None


def _schema(properties: dict[str, JsonValue], required: list[str]) -> dict[str, JsonValue]:
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }


_CUT_MARK = "\n[cut to the result size limit]"


def _fit(text: str, budget: int) -> str:
    """The longest prefix of ``text`` whose UTF-8 encoding fits ``budget``
    bytes (never splits a character)."""
    data = text.encode("utf-8")
    if len(data) <= budget:
        return text
    return data[: max(budget, 0)].decode("utf-8", errors="ignore")


def _cap(message: str) -> str:
    """Last line of defense: every result is at most MAX_OBSERVATION_BYTES of UTF-8."""
    if len(message.encode("utf-8")) <= MAX_OBSERVATION_BYTES:
        return message
    return _fit(message, MAX_OBSERVATION_BYTES - len(_CUT_MARK.encode())) + _CUT_MARK


def _text(message: str, *, error: bool = False) -> ModelToolResult:
    return ModelToolResult((TextBlock(_cap(message)),), is_error=error)


PARTIAL = (
    f"[partial listing: the directory has more entries than shown (limit {MAX_LIST_ENTRIES} entries, "
    f"{MAX_OBSERVATION_BYTES // 1024} KiB); entries are not the first by name]"
)


def listing_outcome(shown_path: str, entries: list[Entry], more: bool) -> ToolOutcome:
    """A directory listing as the model reads it."""
    # Budget the final text: the header (with the widest possible count)
    # and the partial-listing footer are reserved before any entry.
    widest_header = f"{shown_path} ({MAX_LIST_ENTRIES} entries shown)"
    budget = MAX_OBSERVATION_BYTES - len(widest_header.encode()) - len(PARTIAL.encode()) - 2
    lines: list[str] = []
    for e in entries:
        if e.kind == "directory":
            line = f"{e.name}/"
        elif e.kind == "file":
            line = f"{e.name}  ({e.size} bytes)"
        else:
            line = f"{e.name}  [{e.kind}]"
        cost = len(line.encode()) + 1
        if cost > budget:
            more = True
            break
        budget -= cost
        lines.append(line)
    header = f"{shown_path} ({len(lines)} entries shown)"
    body = "\n".join([header, *lines, *([PARTIAL] if more else [])])
    return ToolOutcome(
        ToolStatus.SUCCEEDED,
        _text(body),
        raw_result={"path": shown_path, "count": len(lines), "truncated": more},
    )


def _visible(data: bytes, budget: int) -> tuple[str, int]:
    """The longest prefix of ``data`` whose shown text (invalid UTF-8 bytes
    as U+FFFD) fits ``budget`` bytes, and how many bytes of ``data`` it
    covers — so the next ``offset`` continues exactly where it stopped."""
    shown: list[str] = []
    used = covered = 0
    for char in data.decode("utf-8", errors="surrogateescape"):
        if "\udc80" <= char <= "\udcff":
            text, raw = "�", 1
        else:
            text = char
            raw = len(char.encode("utf-8"))
        cost = len(text.encode("utf-8"))
        if used + cost > budget:
            break
        shown.append(text)
        used += cost
        covered += raw
    return "".join(shown), covered


def read_offset(args: Mapping[str, Any]) -> int:
    offset = args.get("offset", 0)
    return offset if isinstance(offset, int) and not isinstance(offset, bool) and offset > 0 else 0


def _read_notice(offset: int, end: int, size: int) -> str:
    if offset == 0:
        return f"\n[truncated: only the first {end} bytes of this {size}-byte file are shown; read on with offset={end}]"
    return f"\n[truncated: shown bytes {offset}-{end} of {size}; read on with offset={end}]"


def read_outcome(shown: str, data: bytes, size: int, offset: int, whole: Callable[[], bytes], limit: int, *,
                 documents: bool) -> ToolOutcome:
    """``read_file``'s result from up to MAX_READ_BYTES read at ``offset``.
    ``whole`` reads the file from its start, up to ``limit`` bytes, for an image."""
    if offset >= size and size:
        return ToolOutcome(ToolStatus.SUCCEEDED, _text(f"{shown} ({size} bytes): offset {offset} is past the end"),
                           raw_result={"path": shown, "size": size, "offset": offset, "chars_shown": 0})
    sniffed = sniff_image(data[:IMAGE_HEAD_BYTES]) if offset == 0 else None
    if sniffed is not None:  # V3-P67 §5 (Q3): the image itself, for a model that sees images
        line = f"{shown} ({size} bytes): {sniffed.media_type} {sniffed.width}×{sniffed.height}"
        raw: dict[str, JsonValue] = {"path": shown, "size": size, "image": sniffed.media_type,
                                     "width": sniffed.width, "height": sniffed.height}
        if size > limit:
            return ToolOutcome(ToolStatus.SUCCEEDED, _text(f"{line}; too large to show"), raw_result=raw)
        return ToolOutcome(ToolStatus.SUCCEEDED, _text(line), raw_result=raw,
                           result_images=(ResultImage(whole(), sniffed.media_type),))
    if b"\0" in data:
        return ToolOutcome(
            ToolStatus.SUCCEEDED,
            _text(f"{shown} ({size} bytes): binary file, content not shown"
                  + ("; read PDF, Office and HWPX documents with read_document" if documents else "")),
            raw_result={"path": shown, "size": size, "binary": True},
        )
    # Invalid UTF-8 policy: show invalid bytes as U+FFFD (which can triple
    # the byte count) and say so; the budget applies to the final encoded
    # text, header and notices included. Judged on the part shown, so a
    # read window ending inside a character is not "invalid" (PR #59 C12).
    def header(invalid: bool) -> str:
        return (f"{shown} ({size} bytes" + (f", from byte {offset}" if offset else "")
                + (", not valid UTF-8: invalid bytes shown as U+FFFD" if invalid else "") + ")\n")

    budget = MAX_OBSERVATION_BYTES - len(header(True).encode()) - len(_read_notice(offset, size, size).encode())
    visible, covered = _visible(data, budget)
    part = data[:covered]
    invalid_utf8 = "�" in part.decode("utf-8", errors="replace") and "�".encode() not in part
    end = offset + covered
    truncated = end < size
    notice = _read_notice(offset, end, size) if truncated else ""
    return ToolOutcome(
        ToolStatus.SUCCEEDED,
        _text(header(invalid_utf8) + visible + notice),
        raw_result={
            "path": shown,
            "size": size,
            "offset": offset,
            "next_offset": end if truncated else None,
            "chars_shown": len(visible),
            "truncated": truncated,
            "invalid_utf8": invalid_utf8,
        },
    )


def _size(n: int) -> str:
    for unit, scale in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= scale:
            return f"{n / scale:.1f} {unit}"
    return f"{n} bytes"


async def _once(data: bytes) -> AsyncIterator[bytes]:
    yield data


def present_refusal(shown: str, size: int, max_bytes: int, allowance: int) -> ToolOutcome | None:
    if size > max_bytes:
        return ToolOutcome(ToolStatus.FAILED, _text(
            f"{shown}: {_size(size)}, larger than the {_size(max_bytes)} a file may be", error=True),
            raw_result={"refused": "too_large", "size": size})
    if size > allowance:
        return ToolOutcome(ToolStatus.FAILED, _text(
            f"{shown}: too much was given in the last hour (limit {_size(PRESENT_LIMIT_BYTES)}); "
            "nothing was given", error=True), raw_result={"refused": "limit", "size": size})
    return None


def present_changed(shown: str) -> ToolOutcome:
    return ToolOutcome(ToolStatus.FAILED, _text(
        f"{shown}: the file changed while it was being read; nothing was given", error=True),
        raw_result={"refused": "changed"})


PresentFetch = Callable[[object, int, int], Awaitable["tuple[str, bytes] | ToolOutcome"]]


async def present(files: ToolFiles, ctx: ToolContext, raw_path: object, fetch: PresentFetch) -> ToolOutcome:
    """``present_file`` once the bytes are loaded by ``fetch(path, max bytes, allowance)``."""
    # Checked before anything is stored (review N1): one Conversation's
    # Tools run one at a time, so the sum cannot change before the commit.
    given = await files.catalog.presented_bytes_since(ctx.scope, datetime.now(UTC) - PRESENT_WINDOW)
    loaded = await fetch(raw_path, files.max_bytes, PRESENT_LIMIT_BYTES - given)
    if isinstance(loaded, ToolOutcome):
        return loaded
    shown, data = loaded
    try:
        blob = await files.blobs.put(ctx.scope.principal_id, _once(data), files.max_bytes)
    except DataError as error:
        if error.code is DataErrorCode.STORE_FULL:
            return ToolOutcome(ToolStatus.FAILED, _text(f"{shown}: the file store is full; nothing was given",
                                                        error=True), raw_result={"refused": "store_full"})
        if error.code is DataErrorCode.TOO_LARGE:
            return ToolOutcome(ToolStatus.FAILED, _text(f"{shown}: larger than {_size(files.max_bytes)}",
                                                        error=True), raw_result={"refused": "too_large"})
        raise
    name = clean_file_name(posixpath.basename(shown))
    presented = PresentedFile(blob, name, clean_media_type(None, name), shown)
    return ToolOutcome(ToolStatus.SUCCEEDED, _text(f"Prepared {name} in the conversation ({_size(blob.size)})."),
                       raw_result={"path": shown, "size": blob.size, "sha256": blob.sha256},
                       presented_files=(presented,))
