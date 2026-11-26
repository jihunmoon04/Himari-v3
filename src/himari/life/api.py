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

from himari.common import ConversationId, DeepFrozen, PrincipalId
from himari.prompt.api import PromptInputs

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
    # character-day §10, §18.7: the model a call falls back to after the same job's last try got no answer at all
    # (a retryable failure); None: none
    backup_model_id: str | None = None
    backup_effort: str | None = None
    default_model_id: str | None = None  # §10: a self run's model with no day session (None: the memory model)
    default_effort: str | None = None


@dataclass(frozen=True)
class SessionInput(DeepFrozen):
    """character-day §7, §18.7: the day session a diary is written in the context of — its next turn's inputs and
    the most its model's request may hold (tokens; None: no limit known)."""

    conversation_id: ConversationId
    inputs: PromptInputs
    limit_tokens: int | None = None


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
        """character-day §10, §18.7: the model of the principal's day session (where a wake goes), with its
        effort; with no day session ``model_id`` None with why (the default model)."""
        ...

    async def remarks(self, principal: PrincipalId, since: datetime, until: datetime) -> tuple[str, ...]:
        """§4.4: the user's messages (their text) in the day session from ``since`` to ``until``, oldest first:
        what they said while HIMARI worked."""
        ...

    async def session_input(self, principal: PrincipalId) -> SessionInput | None:
        """character-day §7, §18.7: the open day session's next turn as it would be composed now; None: no day
        session open (or it cannot be read now)."""
        ...
