"""The world in the prompt (character-world v0.1 §2, §4, §5): the runtime state's world (the weather on the user's
side) and himari_life's open arcs, the principle naming the world, and the persona's hidden lines."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

from himari.data.api import (
    BondStage,
    InterfaceCapabilities,
    InterfaceKind,
    InterfaceSnapshot,
    LifeEntry,
    LifeKind,
    LifeOrigin,
    PersistedStateBasis,
)
from himari.prompt.api import (
    LIFE_ARCS_CHARS,
    LIFE_RULE,
    WORLD_ABOUT,
    PromptInputs,
    RequestOptions,
    SystemSpec,
    himari_life_data,
    persona_for_stage,
    state_data,
    world_data,
)
from himari.prompt.compaction import context_key, current_state, state_key
from himari.prompt.system import COMPANION_BASE, principle_section, system_text

SEOUL = timezone(timedelta(hours=9))
NOW = datetime(2026, 11, 25, 14, 30, tzinfo=SEOUL)
AT = datetime(2026, 11, 20, tzinfo=UTC)
LIVED = SystemSpec(companion=True, life=True, proactive=True)
RAIN = "light rain, 3 to 9°C, rain 2.1 mm"


def arc(entry_id: int, title: str, goal: str, end_day: str | None = None) -> LifeEntry:
    return LifeEntry(entry_id, 1, LifeKind.ARC, "2026-11-10", goal, LifeOrigin.WRITER, 1, AT, AT, title=title,
                     end_day=end_day, ending=None if end_day is None else "끝났다.")


def inputs(spec: SystemSpec, **more: object) -> PromptInputs:
    base = PromptInputs(instructions=COMPANION_BASE, tool_catalog=(), events=(), tool_executions=(), snapshot=None,
                        state_basis=PersistedStateBasis(0, 0, ()), observations={},
                        request_options=RequestOptions("m", speaks_first=True, free_wake=True), trigger_refs=(),
                        system=spec, now=NOW)
    return replace(base, **more)  # type: ignore[arg-type]


PERSONA = ("이름: 루시\n"
           "[familiar] 고양이를 키웠었다.\n"
           "  - [CLOSE] 사실 예전에 해커였다.\n"
           "[close]이건 붙여 쓴 줄.\n"
           "말투: 반말 [close] 줄 가운데의 표시는 숨기지 않는다.")


def test_a_hidden_line_waits_for_its_stage_and_is_told_without_its_mark() -> None:
    assert persona_for_stage(PERSONA, BondStage.NEW) == "이름: 루시\n말투: 반말 [close] 줄 가운데의 표시는 숨기지 않는다."
    assert persona_for_stage(PERSONA, BondStage.FAMILIAR) == (
        "이름: 루시\n고양이를 키웠었다.\n말투: 반말 [close] 줄 가운데의 표시는 숨기지 않는다.")
    assert persona_for_stage(PERSONA, BondStage.CLOSE) == (
        "이름: 루시\n고양이를 키웠었다.\n  - 사실 예전에 해커였다.\n이건 붙여 쓴 줄.\n"
        "말투: 반말 [close] 줄 가운데의 표시는 숨기지 않는다.")
    plain = "이름: 루시\n\n말투: 반말\n"
    assert persona_for_stage(plain, BondStage.NEW) == plain  # no marked line: as written


def test_the_open_arcs_are_in_himari_life_by_title_and_goal() -> None:
    life = himari_life_data((arc(1, "그림 연습", "풍경화 한 장"), arc(2, "옛 일", "끝난 것", "2026-11-20")), NOW)
    assert life is not None and life["arcs"] == [{"title": "그림 연습", "goal": "풍경화 한 장"}]  # open ones only
    assert himari_life_data((arc(2, "옛 일", "끝난 것", "2026-11-20"),), NOW) is None
    many = tuple(arc(n, "제목" * 5, "목표" * 90) for n in range(5))
    told = himari_life_data(many, NOW)
    assert told is not None
    arcs = told["arcs"]
    assert isinstance(arcs, list) and 0 < len(arcs) < 5
    assert sum(len(a["title"]) + len(a["goal"]) for a in arcs) <= LIFE_ARCS_CHARS
    assert "(arcs)" in LIFE_RULE


def test_the_weather_is_the_worlds_entry_named_as_the_users_side_and_as_information() -> None:
    told = inputs(LIVED, weather=RAIN)
    assert state_data(current_state(told))["world"] == world_data(RAIN) == {"about": WORLD_ABOUT, "weather": RAIN}
    assert "user's side" in WORLD_ABOUT and "may be another" in WORLD_ABOUT
    assert "world" not in state_data(current_state(replace(told, weather=None)))
    sunny = replace(told, weather="clear sky, 1 to 8°C")
    assert state_key(told) != state_key(sunny) and context_key(told) == context_key(sunny)  # the state, not the text
    assert "the weather on the user's side (world)" in principle_section(False, life=True)
    assert "(world)" not in principle_section(False)
    assert "(world)" in "\n\n".join(system_text(told))


def test_the_system_text_is_the_same_on_every_interface_with_the_world() -> None:
    web = inputs(LIVED, weather=RAIN, life=(arc(1, "그림 연습", "풍경화 한 장"),), companion_persona="이름: 루시",
                 interface=InterfaceSnapshot(InterfaceKind.WEB, InterfaceCapabilities(True)))
    desk = replace(web, interface=InterfaceSnapshot(InterfaceKind.WINDOWS_COMPANION))
    cli = replace(web, interface=InterfaceSnapshot(InterfaceKind.CLI, InterfaceCapabilities(True)))
    assert system_text(web) == system_text(desk) == system_text(cli)
    assert context_key(web) == context_key(desk) == context_key(cli)
    for key in ("world", "himari_life"):
        assert state_data(current_state(web))[key] == state_data(current_state(desk))[key]
