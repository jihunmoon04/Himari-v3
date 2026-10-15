"""Small value rules shared by every Data backend (one behaviour, two stores)."""

from __future__ import annotations

import json
from collections.abc import Sequence

from himari.common import ContentBlock, TextBlock, thaw, tool_id_problem
from himari.data.api import (
    MAX_APP_PAYLOAD_BYTES,
    MAX_EXTERNAL_SETTINGS,
    MAX_LIST_LIMIT,
    MAX_TITLE_CHARS,
    MAX_UI_HTML_BYTES,
    PREVIEW_CHARS,
    AppView,
    DataError,
    DataErrorCode,
    InitialActivation,
    ToolCompletion,
    ToolExecutionStatus,
    UiResource,
    ui_digest,
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


def check_external_setting(source: object, enabled: object, count_after: int) -> None:
    """A source is one Tool id segment: the namespace of its Tools' ids."""
    if not isinstance(source, str) or "/" in source or tool_id_problem(source) is not None:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "an external source is one Tool id segment")
    if enabled is not None and not isinstance(enabled, bool):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "enabled must be true, false or null")
    if count_after > MAX_EXTERNAL_SETTINGS:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, f"at most {MAX_EXTERNAL_SETTINGS} external source settings")


def preview(blocks: Sequence[ContentBlock]) -> str:
    text = " ".join(" ".join(b.text.split()) for b in blocks if isinstance(b, TextBlock)).strip()
    return text if len(text) <= PREVIEW_CHARS else text[: PREVIEW_CHARS - 1] + "…"


_VIEW_STATUSES = frozenset({ToolExecutionStatus.SUCCEEDED, ToolExecutionStatus.FAILED})


def check_app_view(completion: ToolCompletion) -> AppView | None:
    """V3-P58: a view goes only with a call that ran, within the size
    limits, and its digest must be its content's."""
    view = completion.app_view
    if view is None:
        return None
    if completion.status not in _VIEW_STATUSES:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, f"a view goes only with a call that ran, not {completion.status.value}")
    check_ui_resource(view.resource)
    if len(json.dumps(thaw(view.payload), ensure_ascii=False).encode("utf-8")) > MAX_APP_PAYLOAD_BYTES:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, f"a view payload is at most {MAX_APP_PAYLOAD_BYTES} bytes")
    return view


def check_ui_resource(resource: UiResource) -> None:
    if "/" in resource.source or tool_id_problem(resource.source) is not None:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a view's source is one Tool id segment")
    if not resource.uri.startswith("ui://"):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a view's uri starts with ui://")
    if len(resource.html.encode("utf-8")) > MAX_UI_HTML_BYTES:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, f"a view's HTML is at most {MAX_UI_HTML_BYTES} bytes")
    expected = ui_digest(resource.source, resource.uri, resource.html, resource.csp, resource.prefers_border)
    if resource.digest != expected:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "a view's digest does not match its content")
