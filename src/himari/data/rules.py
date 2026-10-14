"""Small value rules shared by every Data backend (one behaviour, two stores)."""

from __future__ import annotations

from collections.abc import Sequence

from himari.common import ContentBlock, TextBlock
from himari.data.api import (
    MAX_LIST_LIMIT,
    MAX_TITLE_CHARS,
    PREVIEW_CHARS,
    DataError,
    DataErrorCode,
    InitialActivation,
)


def normalize_title(title: str | None) -> str | None:
    if title is None:
        return None
    if not isinstance(title, str):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "title must be a string")
    title = " ".join(title.split())  # one line, no control whitespace
    if len(title) > MAX_TITLE_CHARS:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, f"title longer than {MAX_TITLE_CHARS} characters")
    return title or None


def check_page(limit: int, offset: int) -> None:
    if not 1 <= limit <= MAX_LIST_LIMIT or offset < 0:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, f"limit must be 1..{MAX_LIST_LIMIT} and offset >= 0")


def check_activations(activations: Sequence[InitialActivation]) -> None:
    kinds = [a.environment_kind for a in activations]
    if len(set(kinds)) != len(kinds):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "one activation per Environment kind (P0)")


def preview(blocks: Sequence[ContentBlock]) -> str:
    text = " ".join(" ".join(b.text.split()) for b in blocks if isinstance(b, TextBlock)).strip()
    return text if len(text) <= PREVIEW_CHARS else text[: PREVIEW_CHARS - 1] + "…"
