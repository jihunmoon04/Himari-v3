"""HIMARI's life (character-life v0.1 §3, character-mind v0.1 §4): the settings the composition root fills in.

The life runs (``worker.LifeWriter``) look, each round, at each active principal's chosen persona: the work at its
work plans (character-work §4), the day's review once a day after the write time in the server's zone (the diary,
plans, a project's start or end and the mood: HIMARI's own self run, character-mind §4.3) and the facts its diary
settles (§5.3). They run from the server's periodic jobs (``bootstrap.schedule``), never as a Conversation's Run,
and never import Control. A self run uses the model of the conversation a wake would go to when it is checked for
free wakes (``MindLink``, given by the composition root), else the memory passes' model (one the Provider declares
checked for them, H-60); every other call uses the memory model. All share one daily token budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import Enum
from typing import Protocol

from himari.common import DeepFrozen, PrincipalId

DEFAULT_WRITE_AT = time(22, 0)  # §3: HIMARI_LIFE_WRITE_AT, in the server's zone
DEFAULT_DAILY_TOKENS = 200_000  # HIMARI_LIFE_DAILY_TOKENS, per principal and day
ATTEMPTS_PER_DAY = 3  # §3: calls tried for one job, persona and date (a work plan's: the plan), whatever came of them
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
    """What one look at a principal's job came to."""

    WRITTEN = "written"
    NOTHING = "nothing"  # nothing to do (the day's diary is there, no work plan due…)
    GAVE_UP = "gave_up"  # ATTEMPTS_PER_DAY were made for this job and date (or plan)
    DEFERRED = "deferred"  # the principal's daily budget would be passed
    FAILED = "failed"  # no usable answer; tried again at the next round


@dataclass(frozen=True)
class MindModel(DeepFrozen):
    """character-mind §4.1: the model a self run uses and why (recorded with the write)."""

    model_id: str | None  # None: the memory model (``reason`` says why)
    reasoning_effort: str | None = None
    reason: str = ""


class MindLink(Protocol):
    """character-mind §4.1, §4.4: what only the composition root can tell a self run."""

    async def mind_model(self, principal: PrincipalId) -> MindModel:
        """The model of the principal's latest conversation (where a wake goes) when it is checked for
        free wakes, with its effort; else ``model_id`` None with why."""
        ...

    async def remarks(self, principal: PrincipalId, since: datetime, until: datetime) -> tuple[str, ...]:
        """§4.4: the user's messages (their text) in that latest conversation from ``since`` to ``until``,
        oldest first: what they said while HIMARI worked."""
        ...
