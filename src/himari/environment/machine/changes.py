"""The review record of an agent's file changes, as the file Tools see it
(V3-P76 §5, §7, §8): a port, implemented by Bootstrap (``bootstrap/changes.py``)
with the previous-version store; Environment does not import Bootstrap.

A Tool call that writes or deletes a file:

1. ``begin``: its intent recorded before the runner is asked; if that
   fails (it raises), the Tool writes nothing.
2. the runner's write with ``keep``; then exactly one of
   - ``dropped``: the runner refused (nothing was changed);
   - ``unknown``: no answer (whether it changed the file is unknown);
   - ``kept``: changed, with the copy of what the file held.

``kept``, ``dropped``, ``unknown`` and ``commands_ran`` never raise: a
failure stays a state of the record (no previous version, or unknown),
shown to the user, never the Tool's failure.

Identities are the runner's ``[dev, ino, size, mtime_ns]`` as tuples;
``ABSENT`` (empty) is "no file".
"""

from __future__ import annotations

from typing import Protocol

from himari.common import CallId, TrustedScope

Identity = tuple[int, ...]
ABSENT: Identity = ()


class ChangeLog(Protocol):
    async def begin(self, scope: TrustedScope, call: CallId, path: str) -> object:
        """A token for this call's intent on ``path`` (the real path the
        write lands on). Raises if it could not be recorded."""
        ...

    async def kept(self, token: object, path: str, copy: int | None, prior: Identity, after: Identity | None,
                   complete: bool) -> None:
        """The runner changed ``path`` (its answer's path). ``copy``: an fd
        of what it held (taken over), None if none was made; ``prior``: the
        file it found (ABSENT: none); ``after``: what is there now (ABSENT:
        deleted; None: unknown); ``complete``: False when the write's end
        is not known (the file may be partial, or was not seen after)."""
        ...

    async def dropped(self, token: object) -> None:
        """The runner refused: nothing was changed."""
        ...

    async def unknown(self, token: object) -> None:
        """No answer: the file may or may not have been changed."""
        ...

    async def commands_ran(self, scope: TrustedScope) -> None:
        """A command ran in the Conversation's Workspaces (§9): it may have changed files."""
        ...
