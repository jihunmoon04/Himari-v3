"""One drive's accounting (V3-P62): what it used, and how many Runs its limit
still allows.

Process memory, owned by the drive: a drive runs in one process, and what is
counted here is never needed after it (a Continue starts a new count). The
tokens are what the Provider reported, for display only; a Run whose usage
was not reported adds none.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from himari.common import TokenUsage
from himari.control.api import TurnProgress, TurnUsage
from himari.prompt.api import TurnState

log = logging.getLogger(__name__)

LOW_RUNS_LEFT = 3  # the turn is "low" from here (§8)


@dataclass
class Turn:
    max_runs: int
    progress: TurnProgress | None = None
    runs: int = 0  # every Run of the drive, summarizing calls too
    steps: int = 0  # model Runs only: what the Run limit counts
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0

    def charge(self, usage: TokenUsage | None, *, step: bool) -> None:
        """One Run sent, with its reported usage."""
        self.runs += 1
        self.steps += 1 if step else 0
        if usage is not None:
            self.input_tokens += usage.input_tokens or 0
            self.output_tokens += usage.output_tokens or 0
            self.cached_input_tokens += usage.cached_input_tokens or 0
            self.cache_write_tokens += usage.cache_write_tokens or 0
        self.report()

    def usage(self) -> TurnUsage:
        return TurnUsage(self.runs, self.input_tokens, self.output_tokens, self.cached_input_tokens,
                         self.cache_write_tokens, self.max_runs)

    def report(self) -> None:
        if self.progress is None:
            return
        try:
            self.progress.report(self.usage())
        except Exception:  # noqa: BLE001 — a report never changes the drive
            log.exception("a turn progress report failed")

    def state(self) -> TurnState | None:
        """What the model is told (§8), only once few Runs are left: ``last``
        when this is the last one the limit allows."""
        runs_left = self.max_runs - self.steps
        if runs_left > LOW_RUNS_LEFT:
            return None
        return TurnState(runs_left, last=runs_left <= 1)
