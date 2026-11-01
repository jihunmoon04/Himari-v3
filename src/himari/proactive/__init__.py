"""When HIMARI speaks first (V3-P87 §5–§7): the triggers, the throttling rules,
choosing and merging — pure functions of a clock and a snapshot.

Layer 3: ``common``, ``data`` (its types) and ``presence`` (its types) only.
Bootstrap reads Data, Presence and the Runner into a ``Situation``, asks
``decide`` and does what it says.
"""

from himari.proactive.rules import (
    ACTIVITY_FOCUS,
    CHECK_IN_WINDOW,
    GAME_RECENT,
    MERGE_ORDER,
    RETURN_FRESH,
    RETURN_WAIT,
    TALKING,
    Candidate,
    Decision,
    ProactiveSettings,
    Return,
    Situation,
    activity_candidates,
    candidate_keys,
    candidates,
    decide,
    in_quiet_hours,
    local_midnight,
    loop_candidates,
    loop_moment,
    reminder_candidates,
    return_candidate,
    spoke,
)

__all__ = [
    "ACTIVITY_FOCUS",
    "CHECK_IN_WINDOW",
    "GAME_RECENT",
    "MERGE_ORDER",
    "RETURN_FRESH",
    "RETURN_WAIT",
    "TALKING",
    "Candidate",
    "Decision",
    "ProactiveSettings",
    "Return",
    "Situation",
    "activity_candidates",
    "candidate_keys",
    "candidates",
    "decide",
    "in_quiet_hours",
    "local_midnight",
    "loop_candidates",
    "loop_moment",
    "reminder_candidates",
    "return_candidate",
    "spoke",
]
