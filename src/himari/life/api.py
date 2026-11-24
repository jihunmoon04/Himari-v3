"""HIMARI's life (character-life v0.1 §3): the settings the composition root fills in.

The story writer (``worker.LifeWriter``) writes, once a day after the write
time in the server's zone, the diary of the persona each active principal has
chosen, with plans ahead and the facts the diary settles. It runs from the
server's periodic jobs (``bootstrap.schedule``), never as a Conversation's Run,
and it never imports Control. Its model is the memory passes' (one the Provider
declares checked for them, H-60), with its own daily token budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time, timedelta
from enum import Enum

from himari.common import DeepFrozen

DEFAULT_WRITE_AT = time(22, 0)  # §3: HIMARI_LIFE_WRITE_AT, in the server's zone
DEFAULT_DAILY_TOKENS = 200_000  # HIMARI_LIFE_DAILY_TOKENS, per principal and day
ATTEMPTS_PER_DAY = 3  # §3: writes tried for one persona and date, whatever came of them
ACTIVE_DAYS = timedelta(days=7)  # a principal who sent no message this long gets no new days


@dataclass(frozen=True)
class LifeSettings(DeepFrozen):
    """Read once at startup: changing them takes a restart."""

    model_id: str  # the memory passes' model (memory_passes declared)
    reasoning_effort: str | None = None  # "low" when the model takes it
    write_at: time = DEFAULT_WRITE_AT
    daily_tokens: int = DEFAULT_DAILY_TOKENS
    time_zone: str = "UTC"  # the server's: the day written, the write time and the budget's day


class WriteOutcome(Enum):
    """What one look at a principal's day came to."""

    WRITTEN = "written"
    NOTHING = "nothing"  # the day's diary is there
    GAVE_UP = "gave_up"  # ATTEMPTS_PER_DAY were made for this date
    DEFERRED = "deferred"  # the principal's daily budget would be passed
    FAILED = "failed"  # no usable answer; tried again at the next round
