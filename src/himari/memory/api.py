"""Memory passes (V3-P85): the ports the composition root fills in.

The memory module reads a Conversation's settled part after it went quiet
and keeps notes, open loops and episodes from it (``worker.MemoryPasses``).
It never imports Control: whether a Conversation's job is running comes from
an ``ActivityProbe`` the composition root makes from the Runner, and the
quiet starts with the Runner's job events, passed on by the root.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from typing import Protocol

from himari.common import DeepFrozen, TrustedScope

DEFAULT_IDLE = timedelta(minutes=10)  # §4.1: HIMARI_MEMORY_IDLE_MINUTES
DEFAULT_DAILY_TOKENS = 1_000_000  # §4.8: HIMARI_MEMORY_DAILY_TOKENS, per principal and day


class ActivityProbe(Protocol):
    def running(self, scope: TrustedScope) -> bool:
        """This process runs a job of the Conversation (or takes an input for it)."""
        ...


@dataclass(frozen=True)
class MemoryPassSettings(DeepFrozen):
    """Read once at startup (§12): changing them takes a restart."""

    model_id: str  # one whose Provider declares memory_passes (§4.8, Q6)
    reasoning_effort: str | None = None  # "low" when the model takes it
    idle: timedelta = DEFAULT_IDLE
    daily_tokens: int = DEFAULT_DAILY_TOKENS
    time_zone: str = "UTC"  # the server's: the day of the budget, and the zone a pass is told


class PassOutcome(Enum):
    """What one attempt to read a Conversation came to."""

    APPLIED = "applied"
    DISCARDED = "discarded"  # it changed while the model was asked (§4.4)
    FAILED = "failed"
    SKIPPED = "skipped"  # the part's last allowed failure (§4.5)
    WAITING = "waiting"  # not quiet yet, a short part, or before its retry time (§4.1)
    BUSY = "busy"  # its job runs, work is unfinished, or another pass of the principal runs
    DEFERRED = "deferred"  # the principal's daily budget would be passed (§4.8)
    NOTHING = "nothing"  # nothing unread, or the Conversation is gone or trashed
