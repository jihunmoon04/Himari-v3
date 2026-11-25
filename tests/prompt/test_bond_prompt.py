"""The bond in the prompt (character-bond v0.1 §5, §6): the runtime state's bond, the companion section's rule,
the anniversary wake's line and how a wake for a day is drawn."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

from himari.common import EventId
from himari.data.api import (
    Anniversary,
    BondSnapshot,
    BondStage,
    BondStanding,
    BondTag,
    ConversationEvent,
    EventKind,
    InterfaceCapabilities,
    InterfaceKind,
    InterfaceSnapshot,
    LifeEntry,
    LifeKind,
    LifeOrigin,
    PersistedStateBasis,
    Wake,
    WakeKind,
    WakeReason,
)
from himari.prompt.api import (
    ANNIVERSARY_RULE,
    BOND_ABOUT,
    BOND_ENTRY_CHARS,
    BOND_RULE,
    BOND_STAGE_WORDS,
    FREE_RULE,
    LIFE_RULE,
    PromptInputs,
    RequestOptions,
    SystemSpec,
    himari_bond_data,
    state_data,
    wake_text,
)
from himari.prompt.compaction import context_key, current_state, state_key
from himari.prompt.composer import Composer
from himari.prompt.system import COMPANION_BASE, principle_section, system_text

SEOUL = timezone(timedelta(hours=9))
NOW = datetime(2026, 11, 25, 10, 0, tzinfo=SEOUL)
AT = datetime(2026, 11, 20, tzinfo=UTC)


def entry(entry_id: int, tag: BondTag, text: str, month_day: str | None = None) -> LifeEntry:
    return LifeEntry(entry_id, 1, LifeKind.BOND, None, text, LifeOrigin.CONVERSATION, 1, AT, AT, tag=tag,
                     month_day=month_day)


ENTRIES = (entry(3, BondTag.NAME, "사용자를 대장이라고 부른다."), entry(5, BondTag.PROMISE, "주말마다 영화 얘기 하기."),
           entry(7, BondTag.DATE, "사용자 생일", "11-25"), entry(2, BondTag.JOKE, "감자 사건"))
BOND = BondSnapshot(BondStanding(1, BondStage.FAMILIAR, AT, "b3", 1), "2026-08-18", 100, ENTRIES,
                    (Anniversary("100 days together", days=100), Anniversary("사용자 생일", entry_id=7)))


def test_the_state_tells_the_stage_in_words_the_days_the_entries_and_todays_days() -> None:
    assert himari_bond_data(BOND) == {
        "about": BOND_ABOUT,
        "stage": BOND_STAGE_WORDS[BondStage.FAMILIAR],
        "together_days": 100,
        "since": "2026-08-18",
        "entries": [{"tag": "name", "text": "사용자를 대장이라고 부른다."}, {"tag": "promise", "text": "주말마다 영화 얘기 하기."},
                    {"tag": "date", "text": "사용자 생일", "date": "11-25"}, {"tag": "joke", "text": "감자 사건"}],
        "today": ["100 days together", "사용자 생일"],
    }
    assert all(not re.search(r"\d", words) for words in BOND_STAGE_WORDS.values())  # D5: never a number
    alone = himari_bond_data(BondSnapshot(BondStanding(1), None, 0))
    assert alone == {"about": BOND_ABOUT, "stage": BOND_STAGE_WORDS[BondStage.NEW]}
    many = tuple(entry(i, BondTag.JOKE, "x" * 300) for i in range(1, 10))
    shown = himari_bond_data(replace(BOND, entries=many, today=()))
    assert len(shown["entries"]) == BOND_ENTRY_CHARS // 300 and "today" not in shown  # type: ignore[arg-type]
    skipped = (entry(1, BondTag.JOKE, "x" * (BOND_ENTRY_CHARS + 1)), entry(2, BondTag.JOKE, "감자 사건"))
    shown = himari_bond_data(replace(BOND, entries=skipped, today=()))
    assert shown["entries"] == [{"tag": "joke", "text": "감자 사건"}]  # one too long does not hide the rest


def inputs(spec: SystemSpec, **more: object) -> PromptInputs:
    base = PromptInputs(instructions=COMPANION_BASE, tool_catalog=(), events=(), tool_executions=(), snapshot=None,
                        state_basis=PersistedStateBasis(0, 0, ()), observations={},
                        request_options=RequestOptions("m", speaks_first=True, free_wake=True), trigger_refs=(),
                        system=spec, now=NOW)
    return replace(base, **more)  # type: ignore[arg-type]


LIVED = SystemSpec(companion=True, life=True, proactive=True)


def test_the_runtime_state_has_the_bond_only_when_it_is_told_and_a_change_is_measured_again() -> None:
    told = inputs(LIVED, bond=BOND)
    assert state_data(current_state(told))["bond"] == himari_bond_data(BOND)
    assert "bond" not in state_data(current_state(replace(told, bond=None)))
    closer = replace(told, bond=replace(BOND, standing=replace(BOND.standing, stage=BondStage.CLOSE)))
    assert state_key(told) != state_key(closer) and context_key(told) == context_key(closer)


def test_the_bond_rule_follows_the_life_rule_and_the_anniversary_line_the_free_wakes() -> None:
    composed = "\n\n".join(system_text(inputs(LIVED)))
    assert f"{LIFE_RULE}\n{BOND_RULE}\n" in composed
    assert "persona sets comes first" in BOND_RULE and "once that day" in BOND_RULE
    assert ANNIVERSARY_RULE in composed and composed.index(FREE_RULE) < composed.index(ANNIVERSARY_RULE)
    assert "[[pass]]" in ANNIVERSARY_RULE
    no_free = "\n\n".join(system_text(inputs(LIVED, request_options=RequestOptions("m", speaks_first=True))))
    assert BOND_RULE in no_free and ANNIVERSARY_RULE not in no_free  # no wake that may pass: no line for it
    without = "\n\n".join(system_text(inputs(SystemSpec(companion=True, proactive=True))))
    assert BOND_RULE not in without and ANNIVERSARY_RULE not in without and "(bond)" not in without
    assert "your bond with the user (bond)" in principle_section(False, life=True)
    assert "(bond)" not in principle_section(False)


def test_the_system_text_is_the_same_on_every_interface_with_the_bond() -> None:
    web = inputs(LIVED, bond=BOND, interface=InterfaceSnapshot(InterfaceKind.WEB, InterfaceCapabilities(True)))
    desk = replace(web, interface=InterfaceSnapshot(InterfaceKind.WINDOWS_COMPANION))
    cli = replace(web, interface=InterfaceSnapshot(InterfaceKind.CLI, InterfaceCapabilities(True)))
    assert system_text(web) == system_text(desk) == system_text(cli)
    assert context_key(web) == context_key(desk) == context_key(cli)
    assert state_data(current_state(web))["bond"] == state_data(current_state(desk))["bond"]


def test_an_anniversary_wake_is_drawn_with_its_day_read_now() -> None:
    wake = Wake("wake_1", (WakeReason(WakeKind.ANNIVERSARY, days=100),
                           WakeReason(WakeKind.ANNIVERSARY, entry_id=7), WakeReason(WakeKind.ANNIVERSARY, years=1),
                           WakeReason(WakeKind.ANNIVERSARY, entry_id=99)), "dev_1")
    drawn = wake_text(wake, {}, {e.entry_id: e for e in ENTRIES})
    assert drawn.startswith("<wake>") and drawn.endswith("</wake>")
    assert json.loads(drawn[len("<wake>"):-len("</wake>")]) == {"reasons": [
        {"kind": "anniversary", "together_days": 100},
        {"kind": "anniversary", "day": {"text": "사용자 생일", "date": "11-25"}},
        {"kind": "anniversary", "years_together": 1},
        {"kind": "anniversary", "day": {"deleted": True}}]}
    event = ConversationEvent(EventId("ev_1"), "conv_1", 1, EventKind.WAKE, wake, 1, AT)  # type: ignore[arg-type]
    composed = Composer().compose(inputs(LIVED, events=(event,), bond=BOND))
    assert "사용자 생일" in composed.conversation[0].blocks[0].text  # type: ignore[union-attr]
