"""One drive's accounting (V3-P62): what it used, whether the next request fits
its token budget, and whether its last Runs brought anything new.

Process memory, owned by the drive: a drive runs in one process, and what is
counted here is never needed after it (a Continue starts a new count).

- **Budget (§3).** Before each request — a model Run or a summarizing call —
  ``fits(estimate, reserve)`` asks whether what was used, the request's input
  estimate and room kept for its output stay within the budget. The reserve
  is twice the largest output a Run of this drive gave, at least
  ``MIN_OUTPUT_RESERVE`` and at most the model's own maximum. It is a budget
  on estimates, not a billing guarantee: the last Run may pass it by its
  estimate's error. A Run whose usage was not reported is charged its
  estimate and reserve; only reported outputs size later reserves.
- **Progress (§4).** After each group, ``observe`` compares every call with
  the earlier calls of the drive: same tool, same arguments (canonical JSON),
  same record status and the same whole ``ModelToolResult`` is a repeat
  (told in the runtime state). A Run is "nothing new" only if every call is
  a repeat of a ``READ`` tool or a refusal the input alone decides
  (``RefusalRule.by_input``) seen before; anything else — a write or unknown
  effect, a state-dependent refusal, an unknown outcome, a new result —
  breaks the streak. Nothing here ever stops a call from running.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from himari.common import CallId, ModelToolResult, TokenUsage, thaw
from himari.control.api import TurnProgress, TurnUsage
from himari.data.api import ToolExecutionRecord, ToolExecutionStatus
from himari.prompt.api import RepeatedCall, TurnBudget, TurnState
from himari.tool.api import EffectProfile, RefusalRule

log = logging.getLogger(__name__)

MIN_OUTPUT_RESERVE = 8_192
RECENT_REPEATS = 10  # repeats carried in the runtime state
LOW_RUNS_LEFT = 3  # under a Run limit, the turn is "low" from here (§8)
# Outcomes that say nothing about progress: neither counted nor compared.
_UNSETTLED = frozenset({ToolExecutionStatus.PENDING, ToolExecutionStatus.EXECUTION_CLAIMED,
                        ToolExecutionStatus.UNKNOWN, ToolExecutionStatus.CANCELLED})

type _Call = tuple[str, str]  # tool id, canonical arguments


def _canonical(arguments: object) -> str:
    return json.dumps(thaw(arguments), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass
class Turn:
    budget: int | None
    max_runs: int | None
    progress: TurnProgress | None = None
    model_max_output: int | None = None
    runs: int = 0  # every Run of the drive, summarizing calls too
    steps: int = 0  # model Runs only: what a Run limit counts
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    largest_output: int = 0
    last_input: int = 0
    empty_streak: int = 0
    _seen: dict[_Call, list[tuple[ToolExecutionStatus, ModelToolResult | None]]] = field(default_factory=dict)
    _refused: set[tuple[_Call, RefusalRule]] = field(default_factory=set)
    _repeated: list[RepeatedCall] = field(default_factory=list)

    # -- the budget ------------------------------------------------------------------

    @property
    def used(self) -> int:
        return self.input_tokens + self.output_tokens

    def reserve(self) -> int:
        reserve = max(MIN_OUTPUT_RESERVE, 2 * self.largest_output)
        return reserve if self.model_max_output is None else min(reserve, self.model_max_output)

    def fits(self, estimate: int, reserve: int) -> bool:
        return self.budget is None or self.used + estimate + reserve <= self.budget

    def charge(self, usage: TokenUsage | None, estimate: int, reserve: int, *, step: bool) -> None:
        """One Run sent: its reported usage, or its estimate and reserve."""
        self.runs += 1
        self.steps += 1 if step else 0
        given_in = usage.input_tokens if usage is not None else None
        given_out = usage.output_tokens if usage is not None else None
        self.last_input = given_in if given_in is not None else estimate
        self.input_tokens += self.last_input
        if given_out is not None:
            self.output_tokens += given_out
            self.largest_output = max(self.largest_output, given_out)  # only reported outputs size the reserve
        else:
            self.output_tokens += reserve  # charged what was kept for it, never fed back into the reserve
        if usage is not None:
            self.cached_input_tokens += usage.cached_input_tokens or 0
            self.cache_write_tokens += usage.cache_write_tokens or 0
        self.report()

    def usage(self) -> TurnUsage:
        return TurnUsage(self.runs, self.input_tokens, self.output_tokens, self.cached_input_tokens,
                         self.cache_write_tokens, self.budget, self.max_runs)

    def report(self) -> None:
        if self.progress is None:
            return
        try:
            self.progress.report(self.usage())
        except Exception:  # noqa: BLE001 — a report never changes the drive
            log.exception("a turn progress report failed")

    # -- what the model is told (§8) ------------------------------------------------

    def state(self) -> TurnState | None:
        """``low``: fewer than about three more requests fit; ``last``: this one
        fits but the next likely will not (a request that does not fit is
        never sent, so ``last`` is seen on the last request sent; review C1).
        A request is expected to cost the previous one's input and the reserve."""
        budget: TurnBudget | None = None
        if self.budget is not None and self.runs:
            left = self.budget - self.used
            request = self.last_input + self.reserve()
            last = left < 2 * request
            if left < 3 * request:
                budget = TurnBudget(low=True, last=last, used=self.used, budget=self.budget, left=max(left, 0))
        if self.max_runs is not None:
            runs_left = self.max_runs - self.steps
            if runs_left <= LOW_RUNS_LEFT:
                last = runs_left <= 1 or (budget is not None and budget.last)
                budget = TurnBudget(low=True, last=last, used=budget.used if budget else None,
                                    budget=budget.budget if budget else None,
                                    left=budget.left if budget else None, runs_left=runs_left)
        repeated = tuple(self._repeated[-RECENT_REPEATS:])
        if budget is None and not repeated:
            return None
        return TurnState(budget, repeated)

    # -- progress (§4) ----------------------------------------------------------------------

    def observe(self, records: Sequence[ToolExecutionRecord], refusals: Mapping[CallId, RefusalRule],
                effects: Mapping[str, EffectProfile]) -> None:
        """One group's settled calls; then whether this Run brought anything new."""
        nothing_new = bool(records)
        for record in records:
            if record.status in _UNSETTLED:
                nothing_new = False
                continue
            call: _Call = (str(record.tool_id), _canonical(record.arguments))
            result = (record.status, record.model_result)
            earlier = self._seen.setdefault(call, [])
            same = result in earlier  # the whole ModelToolResult: blocks, attachment refs, is_error
            if same:
                effect = effects.get(str(record.tool_id))
                self._repeated.append(RepeatedCall(str(record.tool_id), earlier.count(result) + 1, record.status.value,
                                                   effect.value if effect is not None else "unknown"))
            rule = refusals.get(record.call.call_id)
            if rule is not None:  # the Dispatcher's own refusal: state-dependent ones never count
                repeat = rule.by_input and (call, rule) in self._refused
                self._refused.add((call, rule))
            else:
                repeat = same and effects.get(str(record.tool_id)) is EffectProfile.READ
            earlier.append(result)
            nothing_new = nothing_new and repeat
        self.empty_streak = self.empty_streak + 1 if nothing_new else 0
