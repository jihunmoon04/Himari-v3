"""HIMARI's life in the prompt (character-life v0.1 §5): the runtime state's himari_life, the companion
section's rule, the free wake's added phrase and the principle naming it as information."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

from himari.data.api import LifeEntry, LifeKind, LifeOrigin, PersistedStateBasis
from himari.prompt.api import (
    FREE_LIFE_NOTE,
    FREE_RULE,
    LIFE_ABOUT,
    LIFE_DAYS_CHARS,
    LIFE_FACTS_CHARS,
    LIFE_RULE,
    PERSONA_RULE,
    TALKING_RULE,
    CurrentStateProjection,
    PromptInputs,
    RequestOptions,
    SystemSpec,
    himari_life_data,
    state_data,
)
from himari.prompt.compaction import current_state, state_key
from himari.prompt.system import COMPANION_BASE, principle_section, system_text

SEOUL = timezone(timedelta(hours=9))
NOW = datetime(2026, 11, 25, 10, 0, tzinfo=SEOUL)
AT = datetime(2026, 11, 20, tzinfo=UTC)


def entry(entry_id: int, kind: LifeKind, text: str, day: str | None = None,
          origin: LifeOrigin = LifeOrigin.WRITER) -> LifeEntry:
    return LifeEntry(entry_id, 1, kind, day, text, origin, 1, AT, AT)


LIFE = (entry(1, LifeKind.FACT, "고양이를 무서워한다.", origin=LifeOrigin.USER),
        entry(2, LifeKind.FACT, "수채화를 배운다."),
        entry(10, LifeKind.PLAN, "전시를 보러 간다.", "2026-11-27"),
        entry(9, LifeKind.PLAN, "오늘 장을 본다.", "2026-11-25"),
        entry(8, LifeKind.PLAN, "지난 일정.", "2026-11-24"),
        entry(7, LifeKind.DIARY, "어제 그림을 그렸다.", "2026-11-24"),
        entry(6, LifeKind.DIARY, "그제 산책했다.", "2026-11-23"),
        entry(5, LifeKind.DIARY, "사흘 전.", "2026-11-22"),
        entry(4, LifeKind.DIARY, "나흘 전.", "2026-11-21"))


def test_the_state_carries_the_facts_the_last_three_days_and_the_plans_from_today() -> None:
    assert himari_life_data(LIFE, NOW) == {
        "about": LIFE_ABOUT,
        "facts": ["고양이를 무서워한다.", "수채화를 배운다."],
        "days": [{"date": "2026-11-24", "diary": "어제 그림을 그렸다."}, {"date": "2026-11-23", "diary": "그제 산책했다."},
                 {"date": "2026-11-22", "diary": "사흘 전."}],
        "plans": [{"date": "2026-11-25", "text": "오늘 장을 본다."}, {"date": "2026-11-27", "text": "전시를 보러 간다."}],
    }
    assert himari_life_data((), NOW) is None
    assert himari_life_data((entry(8, LifeKind.PLAN, "지난 일정.", "2026-11-24"),), NOW) is None  # nothing to show
    many = tuple(entry(i, LifeKind.FACT, "x" * 400) for i in range(1, 10))
    shown = himari_life_data(many, NOW)
    assert shown is not None and len(shown["facts"]) == LIFE_FACTS_CHARS // 400  # type: ignore[arg-type]
    long_days = tuple(entry(i, LifeKind.DIARY, "y" * 1_000, f"2026-11-{10 + i}") for i in range(1, 4))
    shown = himari_life_data(long_days, NOW)
    assert shown is not None and len(shown["days"]) == LIFE_DAYS_CHARS // 1_000  # type: ignore[arg-type]


def inputs(spec: SystemSpec, **more: object) -> PromptInputs:
    base = PromptInputs(instructions=COMPANION_BASE, tool_catalog=(), events=(), tool_executions=(), snapshot=None,
                        state_basis=PersistedStateBasis(0, 0, ()), observations={},
                        request_options=RequestOptions("m", speaks_first=True, free_wake=True), trigger_refs=(),
                        system=spec, now=NOW)
    return replace(base, **more)  # type: ignore[arg-type]


def test_the_runtime_state_has_himari_life_only_when_it_is_told() -> None:
    told = inputs(SystemSpec(companion=True, life=True), life=LIFE)
    assert state_data(current_state(told))["himari_life"] == himari_life_data(LIFE, NOW)
    assert "himari_life" not in state_data(current_state(replace(told, life=None)))
    assert "himari_life" not in state_data(CurrentStateProjection(PersistedStateBasis(0, 0, ())))
    changed = replace(told, life=(*LIFE[:1], replace(LIFE[1], text="유화를 배운다.")))
    assert state_key(told) != state_key(changed)  # a changed fact is measured again


def test_the_companion_section_has_the_life_rule_after_the_persona_and_it_is_free() -> None:
    life = "\n\n".join(system_text(inputs(SystemSpec(companion=True, life=True, proactive=True))))
    assert f"{PERSONA_RULE}\n{LIFE_RULE}\n{TALKING_RULE}" in life
    assert f"{FREE_RULE} {FREE_LIFE_NOTE}" in life
    assert "your own life (himari_life)" in life
    # owner 2026-11-25: she brings up her own life freely; no daily limit
    assert "freely" in LIFE_RULE and "once a day" not in LIFE_RULE + FREE_LIFE_NOTE
    assert "never make up things you did together" in LIFE_RULE
    without = "\n\n".join(system_text(inputs(SystemSpec(companion=True, proactive=True))))
    assert LIFE_RULE not in without and FREE_LIFE_NOTE not in without and "himari_life" not in without
    off = "\n\n".join(system_text(inputs(SystemSpec(companion=False, life=True))))  # only with the companion
    assert LIFE_RULE not in off and "himari_life" not in off
    assert principle_section(False) == principle_section(False, life=False)
    assert "himari_life" in principle_section(False, life=True)
