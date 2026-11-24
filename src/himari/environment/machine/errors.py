"""Why a file operation in a Conversation's folder was refused (V3-P109 §4.1).

Each message is safe to show the model. ``OUTSIDE`` and ``ACCESS`` are
refusals about the boundary (the Tools report them DENIED); the others are
failures (FAILED). Host error text never leaves :mod:`.folders`.
"""

from __future__ import annotations

NOT_FOUND = "not_found"
OUTSIDE = "outside"
IS_DIRECTORY = "is_directory"
NOT_DIRECTORY = "not_directory"
NOT_REGULAR = "not_regular"
EXISTS = "exists"
ACCESS = "access"
TOO_LARGE = "too_large"
CHANGED = "changed"
INVALID = "invalid"
LINKS = "links"
OTHER = "other"

DENIED = frozenset({OUTSIDE, ACCESS})

MESSAGES = {
    NOT_FOUND: "no such file or directory",
    OUTSIDE: "outside this conversation's folder: you can read and write only there",
    IS_DIRECTORY: "is a directory",
    NOT_DIRECTORY: "not a directory",
    NOT_REGULAR: "not a regular file",
    EXISTS: "already exists",
    ACCESS: "permission denied",
    TOO_LARGE: "file is too large",
    CHANGED: "file changed while it was being edited; nothing written",
    INVALID: "invalid path",
    LINKS: "a link: links in this conversation's folder are not followed",
    OTHER: "file operation failed",
}


class FolderError(Exception):
    """A refused or failed operation; ``str()`` is safe to show the model."""

    def __init__(self, category: str, message: str | None = None) -> None:
        super().__init__(message or MESSAGES.get(category, MESSAGES[OTHER]))
        self.category = category
