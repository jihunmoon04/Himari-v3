"""Small value rules shared by every Data backend (one behaviour, two stores)."""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Sequence

from himari.common import ContentBlock, TextBlock, thaw, tool_id_problem
from himari.data.api import (
    MAX_APP_PAYLOAD_BYTES,
    MAX_EFFORT_CHARS,
    MAX_EXTERNAL_SETTINGS,
    MAX_INSTRUCTIONS_CHARS,
    MAX_LIST_LIMIT,
    MAX_MODEL_ID_CHARS,
    MAX_TITLE_CHARS,
    MAX_UI_HTML_BYTES,
    PREVIEW_CHARS,
    AppView,
    DataError,
    DataErrorCode,
    InitialActivation,
    InstructionScope,
    ModelSetting,
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


def _visible_ascii(value: object, limit: int) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= limit and all("!" <= c <= "~" for c in value)


def check_model_setting(setting: object) -> None:
    """Shape only: whether the model is offered is Control's to decide."""
    if setting is None:
        return
    if not isinstance(setting, ModelSetting) or not _visible_ascii(setting.model_id, MAX_MODEL_ID_CHARS):
        raise DataError(DataErrorCode.INVALID_ARGUMENT,
                        f"a model id is 1..{MAX_MODEL_ID_CHARS} visible ASCII characters")
    effort = setting.reasoning_effort
    if effort is not None and not _visible_ascii(effort, MAX_EFFORT_CHARS):
        raise DataError(DataErrorCode.INVALID_ARGUMENT,
                        f"a reasoning effort is 1..{MAX_EFFORT_CHARS} visible ASCII characters")


def check_instruction_scope(scope: object) -> None:
    if scope is not InstructionScope.USER:
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "only the user's own instructions are offered")


def check_instructions(scope: object, text: object) -> str:
    """V3-P69 §6: the text as stored, or why not."""
    check_instruction_scope(scope)
    if not isinstance(text, str):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "instructions are text")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Before the blank check: str.strip() would take some control characters as blanks. A lone
    # surrogate (Cs) is not text either, and SQLite could not store it.
    if any(unicodedata.category(c) in ("Cc", "Cs") and c not in "\n\t" for c in text):
        raise DataError(DataErrorCode.INVALID_ARGUMENT, "instructions may not contain control characters")
    if not text.strip():
        return ""
    if len(text) > MAX_INSTRUCTIONS_CHARS:
        raise DataError(DataErrorCode.TOO_LARGE, f"instructions are at most {MAX_INSTRUCTIONS_CHARS} characters")
    return text


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
