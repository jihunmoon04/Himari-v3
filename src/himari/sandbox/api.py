"""Sandbox public contract (ENV-04/05, V3-032, V3-036/037, V3-050/051, V3-P59 §3, §4).

The isolation mechanism (bubblewrap + Landlock) is one module; what it is
used for is a profile. Bootstrap builds the backends and hands them to their
consumers, which see only this module (or, for the MCP connector, a Protocol
of their own with the same shape: V3-059).

- **Server** (V3-P59 §4): a process with pipes, its ``read_only`` and
  ``writable`` directories bound at their host paths, no network. It has
  its own self-test and state. Its users are the image copies (V3-P67) and
  the document reader (V3-P70); stdio MCP servers, its first user, are no
  longer run (V3-P109).

States::

    UNAVAILABLE       the host lacks a capability; nothing can run
    CAPABLE_INACTIVE  capable, not started (or self-test not yet run)
    ENFORCED          the real-spawn self-test passed (V3-050)
    SETUP_FAILED      the self-test or a later setup failed; latched

There is no unsandboxed fallback in any state.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

# V3-051
MAX_ARGV = 64
MAX_ARG_CHARS = 4096

# V3-P59 §4
MAX_LINE_BYTES = 4 * 1024 * 1024  # one stdout line of a server process (the MCP HTTP response bound)
STDERR_TAIL_BYTES = 64 * 1024  # what is kept of a server process's stderr
MAX_READ_ONLY = 16
MAX_WRITABLE = 4
MAX_IDENTITY_ENTRIES = 200_000  # the protected set and every bound tree together (V3-P41's admission bound)


class SandboxState(Enum):
    UNAVAILABLE = "unavailable"
    CAPABLE_INACTIVE = "capable_inactive"
    ENFORCED = "enforced"
    SETUP_FAILED = "setup_failed"


class SandboxUnavailableError(RuntimeError):
    """Not ENFORCED. Nothing was spawned."""


class SandboxSetupError(RuntimeError):
    """Sandbox construction failed; the requested argv never executed."""


class SandboxAdmissionError(RuntimeError):
    """A bound tree failed the identity checks (V3-P59 §4.2: M0, R0, R1, R2,
    or the entry bound). Nothing was spawned."""


class SandboxProfileError(ValueError):
    """A server profile the sandbox refuses (V3-P59 §4.2, checked when the
    launcher is made: paths, overlaps, ownership, counts)."""


class ProcessGone(RuntimeError):
    """The isolated process has ended or was shut down (or its stdout broke
    the line bound); no more lines go in or come out."""


def validate_argv(argv: object) -> list[str]:
    if (
        not isinstance(argv, (list, tuple))
        or not 1 <= len(argv) <= MAX_ARGV
        or not all(isinstance(a, str) and a and len(a) <= MAX_ARG_CHARS and "\0" not in a for a in argv)
    ):
        raise ValueError(f"argv must be 1..{MAX_ARGV} non-empty strings of at most {MAX_ARG_CHARS} characters")
    return list(argv)


@dataclass(frozen=True)
class ServerProfile:
    """What one server-profile process may reach (V3-P59 §4.1). Paths are
    absolute host directories, bound at the same paths inside. The roots
    whose contents must not be reachable are fixed by the server sandbox
    itself (HIMARI's data root, which holds the Workspaces) and checked by
    identity at every launch; ``protected`` only adds more of them."""

    read_only: tuple[str, ...] = ()
    writable: tuple[str, ...] = ()
    protected: tuple[str, ...] = ()
    tmp_bytes: int | None = None  # the size of /tmp (bwrap --size); None: not set (V3-P70 §5)


class IsolatedProcess(Protocol):
    """A long-lived process in the server profile, spoken to by lines."""

    async def write_line(self, data: bytes) -> None:
        """``data`` (no newline) and a newline to stdin; ProcessGone if it ended."""
        ...

    async def read_line(self) -> bytes:
        """The next stdout line without its newline. ProcessGone at EOF, and
        when a line passes MAX_LINE_BYTES (the process is then shut down)."""
        ...

    @property
    def returncode(self) -> int | None: ...

    def stderr_tail(self) -> bytes:
        """The last STDERR_TAIL_BYTES the process wrote to stderr."""
        ...

    async def shutdown(self, grace: float) -> None:
        """Close stdin, wait ``grace``; SIGTERM, wait ``grace``; SIGKILL. The
        whole sandbox ends with it. Safe to call more than once."""
        ...


class ProcessLauncher(Protocol):
    """Launches processes in one server profile."""

    async def launch(self, argv: Sequence[str], env: Mapping[str, str], cwd: str) -> IsolatedProcess:
        """SandboxUnavailableError unless the server profile is ENFORCED,
        SandboxAdmissionError when a bound tree fails the identity checks,
        SandboxSetupError when the sandbox could not be built: in every case
        nothing ran. ``env`` is added to the fixed sandbox environment;
        ``cwd`` is an absolute path inside a bound tree, or /tmp."""
        ...

