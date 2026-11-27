"""HIMARI's projects in Data (character-routine v0.1 §11.1): one change — start, update, end — that the project Tools
(origin HIMARI, kept with the call's result), the diary's arc (origin WRITER, left out when refused), a piece's
``done`` and the Web's end (origin USER) all make, with the same limits: at most two open, none titled as an open one,
never ended on the day it started (but by the user), the user's title and goal kept from HIMARI, and HIMARI's changes
on the schedule Tools' day count. On every backend."""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from testkit import SCOPE, data_for, new_store, restart

from himari.common import TokenUsage
from himari.data import rules
from himari.data.api import (
    MAX_HIMARI_PLAN_WRITES,
    ArcMove,
    ArcOp,
    DataError,
    DataErrorCode,
    LifeEntry,
    LifeKind,
    LifeOrigin,
    LifePlan,
    LifeWriteStart,
    PlanChange,
    PlanKind,
    PlanOp,
    PlanWrite,
    ProjectWrite,
    RawReceived,
    WorkPiece,
    life_day,
)
from himari.data.memory import InMemoryData
from himari.data.sqlite import SqliteData

ALICE = SCOPE.principal_id
ZONE = "Asia/Seoul"
SEOUL = ZoneInfo(ZONE)
NOW = datetime(2026, 11, 26, 5, 0, tzinfo=UTC)  # 14:00 in Seoul: the day of 11-26


def refused(code: DataErrorCode, match: str = "") -> pytest.RaisesExc[DataError]:
    return pytest.raises(DataError, check=lambda e: e.code is code and match in e.message)


def change(data: InMemoryData | SqliteData, op: PlanOp, write: ProjectWrite, *, at: datetime = NOW,
           persona_id: int = 1) -> LifeEntry | None:
    """A project Tool's change as the call's commit applies it (origin HIMARI)."""
    planned = PlanChange(op, persona_id, ZONE, at, project=write)
    if isinstance(data, SqliteData):
        with data._tx() as tx:  # noqa: SLF001
            return data._apply_plan_change(tx, ALICE, planned)  # noqa: SLF001
    return data._stage_plan_change(ALICE, planned)()  # noqa: SLF001


def start(data: InMemoryData | SqliteData, title: str, *, at: datetime = NOW) -> LifeEntry:
    made = change(data, PlanOp.PROJECT_START, ProjectWrite(title=title, goal="천천히 해 본다.", reason="해 보고 싶어서"),
                  at=at)
    assert made is not None
    return made


async def diary(data: InMemoryData | SqliteData, day: str, arc: ArcMove | None) -> tuple[LifeEntry, ...]:
    """One diary write of ``day`` for persona 1, applied; the entries written."""
    write_id = await data.begin_life_write(ALICE, LifeWriteStart(1, day, "openrouter", "m", b"{}",
                                                                 "application/json", 100))
    await data.mark_life_write_dispatched(write_id)
    await data.record_life_write(write_id, RawReceived(b"{}", "application/json", TokenUsage(10, 10)))
    return await data.apply_life_write(write_id, LifePlan(f"{day}의 일기.", arc=arc), now=NOW)


def test_the_day_of_a_moment_runs_to_five_in_the_morning() -> None:
    assert life_day(datetime(2026, 11, 26, 14, 59, tzinfo=UTC), SEOUL) == "2026-11-26"  # 11-26 23:59 in Seoul
    assert life_day(datetime(2026, 11, 26, 19, 59, tzinfo=UTC), SEOUL) == "2026-11-26"  # 11-27 04:59: still 11-26
    assert life_day(datetime(2026, 11, 26, 20, 0, tzinfo=UTC), SEOUL) == "2026-11-27"  # 11-27 05:00


def test_himari_starts_changes_and_ends_a_project_with_its_reasons() -> None:
    async def scenario() -> None:
        store = new_store()
        data = data_for(store)
        made = start(data, "그림 연습")
        assert (made.kind, made.title, made.text, made.day, made.origin, made.reason, made.arc_open) == (
            LifeKind.ARC, "그림 연습", "천천히 해 본다.", "2026-11-26", LifeOrigin.HIMARI, "해 보고 싶어서", True)
        late = start(data, "우쿨렐레", at=datetime(2026, 11, 26, 16, 30, tzinfo=UTC))  # 01:30 on 11-27: still 11-26
        assert late.day == "2026-11-26"
        changed = change(data, PlanOp.PROJECT_UPDATE, ProjectWrite(made.entry_id, goal="풍경화 한 장", reason="목표를 좁혔어"))
        assert changed is not None
        assert (changed.title, changed.text, changed.revision, changed.reason) == (
            "그림 연습", "풍경화 한 장", 2, "목표를 좁혔어")
        tomorrow = NOW + timedelta(days=1)
        ended = change(data, PlanOp.PROJECT_END, ProjectWrite(made.entry_id, ending="한 장 다 그렸다.", reason="끝났어"),
                       at=tomorrow)
        assert ended is not None
        assert (ended.end_day, ended.ending, ended.reason, ended.origin, ended.arc_open) == (
            "2026-11-27", "한 장 다 그렸다.", "끝났어", LifeOrigin.HIMARI, False)
        data = data_for(restart(store))
        kept = {e.entry_id: e for e in await data.list_life(ALICE, 1, since="2026-11-01")}
        assert (kept[made.entry_id].end_day, kept[made.entry_id].reason) == ("2026-11-27", "끝났어")
        assert kept[late.entry_id].arc_open
        with refused(DataErrorCode.NOT_FOUND, f"no open project {made.entry_id}"):  # a closed one is never changed
            change(data, PlanOp.PROJECT_UPDATE, ProjectWrite(made.entry_id, title="다시", reason="다시"), at=tomorrow)

    asyncio.run(scenario())


def test_the_limits_are_said_in_sentences() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        first = start(data, "그림 연습")
        with refused(DataErrorCode.PLAN_REFUSED, f"A project titled \"그림 연습\" is open already (id {first.entry_id})"):
            start(data, " 그림 연습 ")
        start(data, "우쿨렐레")
        with refused(DataErrorCode.PLAN_REFUSED, "You have 2 open projects already"):
            start(data, "세 번째")
        with refused(DataErrorCode.PLAN_REFUSED, f"Project {first.entry_id} started today"):
            change(data, PlanOp.PROJECT_END, ProjectWrite(first.entry_id, ending="끝.", reason="질렸어"))
        with refused(DataErrorCode.NOT_FOUND, "There is no open project 99"):
            change(data, PlanOp.PROJECT_END, ProjectWrite(99, ending="끝.", reason="질렸어"),
                   at=NOW + timedelta(days=1))
        with refused(DataErrorCode.NOT_FOUND):  # another persona's
            change(data, PlanOp.PROJECT_UPDATE, ProjectWrite(first.entry_id, title="남의 것", reason="왜"), persona_id=2)
        with refused(DataErrorCode.INVALID_ARGUMENT, "says why"):  # HIMARI's change always says why
            change(data, PlanOp.PROJECT_START, ProjectWrite(title="이유 없이", goal="그냥"))
        with refused(DataErrorCode.INVALID_ARGUMENT, "title or the goal"):
            change(data, PlanOp.PROJECT_UPDATE, ProjectWrite(first.entry_id, reason="아무것도"))
        with refused(DataErrorCode.TOO_LARGE):
            change(data, PlanOp.PROJECT_START, ProjectWrite(title="가" * 61, goal="g", reason="r"))

    asyncio.run(scenario())


def test_himari_keeps_the_users_title_and_goal_but_may_end_the_project() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        made = start(data, "그림 연습")
        mine = await data.edit_life(ALICE, made.entry_id, "수채화 한 장", expected_revision=1, title="수채화")
        with refused(DataErrorCode.PLAN_REFUSED, "The user wrote this project's title and goal"):
            change(data, PlanOp.PROJECT_UPDATE, ProjectWrite(made.entry_id, title="유화", reason="바꾸고 싶어"))
        ended = change(data, PlanOp.PROJECT_END, ProjectWrite(made.entry_id, ending="다 그렸다.", reason="끝"),
                       at=NOW + timedelta(days=2))
        assert ended is not None
        assert (ended.title, ended.text, ended.origin, ended.revision) == ("수채화", "수채화 한 장", LifeOrigin.USER,
                                                                           mine.revision + 1)

    asyncio.run(scenario())


def test_the_user_ends_a_project_any_day_from_the_revision_read() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        made = start(data, "그림 연습")
        with refused(DataErrorCode.VERSION_CONFLICT):
            await data.end_project(ALICE, made.entry_id, "끝.", expected_revision=2, zone=ZONE, now=NOW)
        ended = await data.end_project(ALICE, made.entry_id, " 그만뒀다. ", expected_revision=1, zone=ZONE, now=NOW)
        assert (ended.end_day, ended.ending, ended.origin, ended.reason) == (  # the day it started: fine for the user
            "2026-11-26", "그만뒀다.", LifeOrigin.USER, "해 보고 싶어서")
        with refused(DataErrorCode.NOT_FOUND):
            await data.end_project(ALICE, made.entry_id, "또.", expected_revision=2, zone=ZONE, now=NOW)
        with refused(DataErrorCode.INVALID_ARGUMENT):
            await data.end_project(ALICE, start(data, "우쿨렐레").entry_id, " ", expected_revision=1, zone=ZONE,
                                   now=NOW)

    asyncio.run(scenario())


def test_the_diary_makes_the_same_change_and_a_refused_move_is_left_out() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        made = start(data, "그림 연습")  # by the Tool on 11-26
        (written,) = await diary(data, "2026-11-26", ArcMove(ArcOp.CLOSE, entry_id=made.entry_id, ending="끝."))
        assert written.arc_id is None  # never closed the day it started: left out, the diary kept
        start(data, "우쿨렐레")
        (third,) = await diary(data, "2026-11-26", ArcMove(ArcOp.START, title="세 번째", goal="g"))
        assert third.arc_id is None  # two open already
        closed, last = await diary(data, "2026-11-27", ArcMove(ArcOp.CLOSE, entry_id=made.entry_id, ending="다 그렸다."))
        assert (closed.entry_id, closed.end_day, closed.ending, closed.origin, closed.arc_open) == (
            made.entry_id, "2026-11-27", "다 그렸다.", LifeOrigin.WRITER, False)
        assert last.arc_id == made.entry_id
        began, _ = await diary(data, "2026-11-28", ArcMove(ArcOp.START, title="작곡 노트", goal="매일 한 곡"))
        assert (began.day, began.origin, began.reason) == ("2026-11-28", LifeOrigin.WRITER, None)

    asyncio.run(scenario())


def test_a_piece_never_finishes_a_project_on_the_day_it_started() -> None:
    project = LifeEntry(3, 1, LifeKind.ARC, "2026-11-26", "goal", LifeOrigin.HIMARI, 1, NOW, NOW, title="t")
    piece = WorkPiece("piece", "title", "summary", "so far", done="끝났다.")
    plan = LifeEntry(4, 1, LifeKind.PLAN, "2026-11-26", "work", LifeOrigin.HIMARI, 1, NOW, NOW, arc_id=3,
                     plan_kind=PlanKind.WORK, minutes=60, time_of_day="20:00")
    same_day = rules.worked_on(project, plan, piece, NOW)
    assert (same_day.so_far, same_day.end_day, same_day.ending, same_day.arc_open) == ("so far", None, None, True)
    later = rules.worked_on(project, dataclasses.replace(plan, day="2026-11-27"), piece, NOW)
    assert (later.end_day, later.ending) == ("2026-11-27", "끝났다.")


def test_himaris_project_changes_count_with_its_slots() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        made = start(data, "그림 연습")
        change(data, PlanOp.PROJECT_UPDATE, ProjectWrite(made.entry_id, title="수채화", reason="r"))  # counted once
        for i in range(MAX_HIMARI_PLAN_WRITES - 1):
            await data.write_plan(ALICE, 1, PlanWrite("2026-11-27", f"{10 + i // 6:02d}:{i % 6 * 10:02d}", 10,
                                                      PlanKind.OWN, f"일 {i}"), origin=LifeOrigin.HIMARI, zone=ZONE,
                                  now=NOW)
        with refused(DataErrorCode.PLAN_REFUSED, f"changed {MAX_HIMARI_PLAN_WRITES} slots and projects today"):
            start(data, "우쿨렐레")
        with refused(DataErrorCode.PLAN_REFUSED, f"changed {MAX_HIMARI_PLAN_WRITES} slots and projects today"):
            await data.write_plan(ALICE, 1, PlanWrite("2026-11-27", "20:00", 10, PlanKind.OWN, "하나 더"),
                                  origin=LifeOrigin.HIMARI, zone=ZONE, now=NOW)
        again = change(data, PlanOp.PROJECT_UPDATE, ProjectWrite(made.entry_id, goal="한 장", reason="r"))
        assert again is not None and again.revision == 3  # the same project again: already counted

    asyncio.run(scenario())
