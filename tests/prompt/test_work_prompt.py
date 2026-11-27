"""HIMARI's work in the prompt (character-work v0.1 §6, §7.1): himari_life.work and the projects' so far, the
life rule's clause, the work_done wake's line and how that wake is drawn."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from himari.data.api import LifeEntry, LifeKind, LifeOrigin, PersistedStateBasis, Wake, WakeKind, WakeReason
from himari.prompt.api import (
    ANNIVERSARY_RULE,
    LIFE_RULE,
    WORK_RULE,
    WORK_TEXT_CHARS,
    PromptInputs,
    RequestOptions,
    SystemSpec,
    himari_life_data,
    wake_text,
)
from himari.prompt.system import COMPANION_BASE, principle_section, system_text

SEOUL = timezone(timedelta(hours=9))
NOW = datetime(2026, 11, 25, 22, 0, tzinfo=SEOUL)


def entry(entry_id: int, kind: LifeKind, text: str, day: str | None = None, made: datetime = NOW,
          **more: object) -> LifeEntry:
    return LifeEntry(entry_id, 1, kind, day, text, LifeOrigin.WRITER, 1, made, made, **more)  # type: ignore[arg-type]


ARC = entry(1, LifeKind.ARC, "설정집 한 권", "2026-11-20", title="블랙홀 전자포", so_far="포신과 냉각부까지.")


def piece(entry_id: int, hours_ago: float, text: str = "본문") -> LifeEntry:
    return entry(entry_id, LifeKind.WORK, text, "2026-11-25", NOW - timedelta(hours=hours_ago), title=f"조각 {entry_id}",
                 summary=f"요약 {entry_id}", arc_id=1, plan_id=100 + entry_id)


def test_the_state_carries_the_projects_so_far_and_the_newest_two_pieces_of_two_days() -> None:
    long = "가" * (WORK_TEXT_CHARS + 50)
    life = himari_life_data((ARC, piece(3, 50), piece(4, 30), piece(5, 2, long), piece(6, 1)), NOW)
    assert life is not None
    assert life["arcs"] == [{"id": 1, "title": "블랙홀 전자포", "goal": "설정집 한 권", "started": "2026-11-20",
                             "so_far": "포신과 냉각부까지."}]
    assert life["work"] == [
        {"title": "조각 6", "project": "블랙홀 전자포", "date": "2026-11-25", "text": "본문"},  # whole: no summary
        {"title": "조각 5", "summary": "요약 5", "project": "블랙홀 전자포", "date": "2026-11-25"}]
    first = himari_life_data((ARC, piece(5, 2, long)), NOW)
    assert first is not None and first["work"][0]["text"] == "가" * WORK_TEXT_CHARS + "…"  # type: ignore[index]
    assert first["work"][0]["summary"] == "요약 5"  # type: ignore[index]  # cut: the summary tells the rest
    assert himari_life_data((piece(3, 49),), NOW) is None  # older than two days: nothing to show
    assert "himari_life.work" in LIFE_RULE and "do not read it out at length unless they ask" in LIFE_RULE


def inputs(spec: SystemSpec, **more: object) -> PromptInputs:
    base = PromptInputs(instructions=COMPANION_BASE, tool_catalog=(), events=(), tool_executions=(), snapshot=None,
                        state_basis=PersistedStateBasis(0, 0, ()), observations={},
                        request_options=RequestOptions("m", speaks_first=True, free_wake=True), trigger_refs=(),
                        system=spec, now=NOW)
    return replace(base, **more)  # type: ignore[arg-type]


def test_the_work_done_line_follows_the_anniversary_line_for_a_model_checked_for_free_wakes() -> None:
    text = "\n".join(system_text(inputs(SystemSpec(companion=True, life=True, proactive=True))))
    assert f"{ANNIVERSARY_RULE}\n{WORK_RULE}" in text
    assert "[[pass]]" in WORK_RULE and "do not read it out" in WORK_RULE
    unchecked = inputs(SystemSpec(companion=True, life=True, proactive=True),
                       request_options=RequestOptions("m", speaks_first=True))
    assert WORK_RULE not in "\n".join(system_text(unchecked))
    assert WORK_RULE not in "\n".join(system_text(inputs(SystemSpec(companion=True, proactive=True))))
    assert "your own life and work (himari_life)" in principle_section(False, life=True)


def test_a_work_done_wake_is_drawn_with_its_piece_and_project_read_now() -> None:
    wake = Wake("w1", (WakeReason(WakeKind.WORK_DONE, entry_id=6),), "d1")
    life = {e.entry_id: e for e in (ARC, piece(6, 1))}
    drawn = wake_text(wake, {}, {}, life)
    assert '"kind":"work_done"' in drawn.replace(" ", "")
    assert "블랙홀 전자포" in drawn and "조각 6" in drawn and "요약 6" in drawn and "본문" not in drawn
    gone = wake_text(wake, {}, {}, {})
    assert '"deleted":true' in gone.replace(" ", "")
