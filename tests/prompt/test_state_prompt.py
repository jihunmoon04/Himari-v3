"""HIMARI's state in the prompt (character-state v0.1 §2, §3): the runtime state's himari_mood and himari_now,
the companion section's mood rule, the life rule's phrase for sleep and the principle naming both."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, time, timedelta, timezone

from himari.data.api import (
    HimariMood,
    InterfaceCapabilities,
    InterfaceKind,
    InterfaceSnapshot,
    LifeEntry,
    LifeKind,
    LifeOrigin,
    MoodSource,
    PersistedStateBasis,
)
from himari.prompt.api import (
    BOND_RULE,
    LIFE_RULE,
    MOOD_ABOUT,
    MOOD_RULE,
    NOW_ABOUT,
    TALKING_RULE,
    HimariNow,
    PromptInputs,
    RequestOptions,
    SystemSpec,
    doing_now,
    himari_life_data,
    himari_mood_data,
    himari_now_data,
    state_data,
)
from himari.prompt.compaction import context_key, current_state, state_key
from himari.prompt.system import COMPANION_BASE, principle_section, system_text

SEOUL = timezone(timedelta(hours=9))
NOW = datetime(2026, 11, 25, 14, 30, tzinfo=SEOUL)
AT = datetime(2026, 11, 20, tzinfo=UTC)
QUIET = (time(23, 0), time(8, 0))


def plan(entry_id: int, day: str, text: str, at: str | None) -> LifeEntry:
    return LifeEntry(entry_id, 1, LifeKind.PLAN, day, text, LifeOrigin.WRITER, 1, AT, AT, time_of_day=at)


MOOD = HimariMood(1, "sad", NOW - timedelta(hours=2, minutes=10), MoodSource.WRITER, "비 오는 날이라서")


def test_the_mood_is_told_by_name_hours_and_reason() -> None:
    assert himari_mood_data(MOOD, NOW) == {"about": MOOD_ABOUT, "mood": "sad", "hours_ago": 2,
                                           "reason": "비 오는 날이라서"}
    reply = HimariMood(1, "happy", NOW - timedelta(minutes=5), MoodSource.REPLY)
    assert himari_mood_data(reply, NOW) == {"about": MOOD_ABOUT, "mood": "happy", "hours_ago": 0}
    assert himari_mood_data(reply) == {"about": MOOD_ABOUT, "mood": "happy"}


def test_what_himari_is_doing_is_a_timed_plan_under_way_else_asleep_in_the_quiet_hours() -> None:
    plans = (plan(1, "2026-11-25", "도서관에서 공부", "13:30"), plan(2, "2026-11-25", "빵집 들르기", "14:00"),
             plan(3, "2026-11-25", "저녁 산책", "19:00"), plan(4, "2026-11-25", "언젠가 청소", None),
             plan(5, "2026-11-24", "어제 일", "14:00"))
    assert doing_now(plans, NOW, QUIET) == HimariNow(doing="빵집 들르기", since="14:00")  # the latest start wins
    assert doing_now(plans, NOW + timedelta(minutes=59), QUIET) == HimariNow(doing="빵집 들르기", since="14:00")
    assert doing_now(plans, NOW + timedelta(minutes=60), QUIET) is None  # 90 minutes after 14:00 have passed
    assert doing_now(plans, NOW.replace(hour=12), QUIET) is None  # not started yet
    assert doing_now((), NOW.replace(hour=23, minute=30), QUIET) == HimariNow(asleep=True)
    assert doing_now((), NOW.replace(hour=7, minute=59), QUIET) == HimariNow(asleep=True)
    assert doing_now((), NOW.replace(hour=8, minute=0), QUIET) is None
    assert doing_now((), NOW.replace(hour=23, minute=30)) is None  # no quiet hours: never asleep
    late = (plan(6, "2026-11-25", "별 보기", "23:30"),)
    midnight = datetime(2026, 11, 26, 0, 30, tzinfo=SEOUL)
    assert doing_now(late, midnight, QUIET) == HimariNow(doing="별 보기", since="23:30")  # a plan wins over sleep
    assert doing_now(late, midnight.astimezone(UTC), QUIET) is None  # read in now's zone: 15:30 UTC
    assert himari_now_data(HimariNow(asleep=True)) == {"about": NOW_ABOUT, "asleep": True}
    assert himari_now_data(HimariNow(doing="빵집 들르기", since="14:00")) == {
        "about": NOW_ABOUT, "doing": "빵집 들르기", "since": "14:00"}


def test_a_plans_time_is_in_himari_life() -> None:
    life = himari_life_data((plan(1, "2026-11-26", "산책", "07:30"), plan(2, "2026-11-27", "빵 굽기", None)), NOW)
    assert life is not None and life["plans"] == [{"date": "2026-11-26", "time": "07:30", "text": "산책"},
                                                  {"date": "2026-11-27", "text": "빵 굽기"}]


def inputs(spec: SystemSpec, **more: object) -> PromptInputs:
    base = PromptInputs(instructions=COMPANION_BASE, tool_catalog=(), events=(), tool_executions=(), snapshot=None,
                        state_basis=PersistedStateBasis(0, 0, ()), observations={},
                        request_options=RequestOptions("m", speaks_first=True, free_wake=True), trigger_refs=(),
                        system=spec, now=NOW)
    return replace(base, **more)  # type: ignore[arg-type]


LIVED = SystemSpec(companion=True, life=True, proactive=True)
DOING = HimariNow(doing="빵집 들르기", since="14:00")


def test_the_runtime_state_has_the_mood_and_now_only_when_told_and_a_change_is_measured_again() -> None:
    told = inputs(LIVED, mood=MOOD, himari_now=DOING)
    data = state_data(current_state(told))
    assert data["himari_mood"] == himari_mood_data(MOOD, NOW) and data["himari_now"] == himari_now_data(DOING)
    bare = state_data(current_state(replace(told, mood=None, himari_now=None)))
    assert "himari_mood" not in bare and "himari_now" not in bare
    asleep = replace(told, himari_now=HimariNow(asleep=True))
    assert state_key(told) != state_key(asleep) and context_key(told) == context_key(asleep)  # the state, not the text


def test_the_mood_rule_follows_the_bond_rule_and_the_life_rule_tells_of_sleep() -> None:
    composed = "\n\n".join(system_text(inputs(LIVED)))
    assert f"{BOND_RULE}\n{MOOD_RULE}\n{TALKING_RULE}" in composed
    assert "do not announce it unasked" in MOOD_RULE and "user's matters come first" in MOOD_RULE
    assert "himari_now" in LIFE_RULE and "as if just woken" in LIFE_RULE
    without = "\n\n".join(system_text(inputs(SystemSpec(companion=True, proactive=True))))
    assert MOOD_RULE not in without and "himari_mood" not in without and "himari_now" not in without
    told = principle_section(False, life=True)
    assert "your mood (himari_mood)" in told and "what you are doing now (himari_now)" in told
    assert "himari_mood" not in principle_section(False)


def test_the_system_text_is_the_same_on_every_interface_with_the_state() -> None:
    web = inputs(LIVED, mood=MOOD, himari_now=DOING,
                 interface=InterfaceSnapshot(InterfaceKind.WEB, InterfaceCapabilities(True)))
    desk = replace(web, interface=InterfaceSnapshot(InterfaceKind.WINDOWS_COMPANION))
    cli = replace(web, interface=InterfaceSnapshot(InterfaceKind.CLI, InterfaceCapabilities(True)))
    assert system_text(web) == system_text(desk) == system_text(cli)
    assert context_key(web) == context_key(desk) == context_key(cli)
    for key in ("himari_mood", "himari_now"):
        assert state_data(current_state(web))[key] == state_data(current_state(desk))[key]
