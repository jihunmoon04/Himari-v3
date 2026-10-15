"""Sandbox public contract (ENV-04/05, V3-032, V3-036/037, V3-050/051, V3-P59 §3).

The isolation mechanism (bubblewrap + Landlock) is one module; what it is
used for is a profile. Step 1 of V3-P59 has one profile, the Workspace
(``inside/run_command``): one bounded command with a principal's Workspace
bound at ``WORKSPACE_MOUNT``. Bootstrap builds the backend and hands it to
its consumer, which sees only this module.

States::

    UNAVAILABLE       the host lacks a capability; nothing can run
    CAPABLE_INACTIVE  capable, not started (or self-test not yet run)
    ENFORCED          the real-spawn self-test passed (V3-050)
    SETUP_FAILED      the self-test or a later setup failed; latched

There is no unsandboxed fallback in any state.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

WORKSPACE_MOUNT = "/workspace"

# V3-051
MAX_ARGV = 64
MAX_ARG_CHARS = 4096
DEFAULT_TIMEOUT_SECONDS = 120
MAX_TIMEOUT_SECONDS = 600
MAX_OUTPUT_BYTES = 256 * 1024  # acquisition per stream


class SandboxState(Enum):
    UNAVAILABLE = "unavailable"
    CAPABLE_INACTIVE = "capable_inactive"
    ENFORCED = "enforced"
    SETUP_FAILED = "setup_failed"


class SandboxUnavailableError(RuntimeError):
    """Not ENFORCED. Nothing was spawned."""


class SandboxSetupError(RuntimeError):
    """Sandbox construction failed; the requested argv never executed."""


@dataclass(frozen=True)
class CommandResult:
    exit_code: int | None  # None when killed on timeout
    timed_out: bool
    stdout: bytes  # at most MAX_OUTPUT_BYTES
    stderr: bytes
    stdout_total: int  # bytes the command actually wrote
    stderr_total: int
    elapsed_ms: int


def validate_argv(argv: object) -> list[str]:
    if (
        not isinstance(argv, (list, tuple))
        or not 1 <= len(argv) <= MAX_ARGV
        or not all(isinstance(a, str) and a and len(a) <= MAX_ARG_CHARS and "\0" not in a for a in argv)
    ):
        raise ValueError(f"argv must be 1..{MAX_ARGV} non-empty strings of at most {MAX_ARG_CHARS} characters")
    return list(argv)


class WorkspaceSandbox(Protocol):
    """The Workspace profile: one bounded command per call, the verified
    principal root bound at ``WORKSPACE_MOUNT``, network isolated."""

    @property
    def state(self) -> SandboxState: ...

    async def start(self, *, protected_paths: Sequence[str] = ()) -> SandboxState:
        """Once per process: ENFORCED only if the real-spawn self-test passes.
        ``protected_paths`` are host roots the self-test proves invisible."""
        ...

    async def run(self, argv: list[str], *, root_fd: int, cwd: str, timeout: float) -> CommandResult:
        """Refuses (SandboxUnavailableError) unless ENFORCED; a setup failure
        raises SandboxSetupError and latches SETUP_FAILED. ``cwd`` is under
        ``WORKSPACE_MOUNT``."""
        ...
