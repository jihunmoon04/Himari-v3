"""HIMARI's work (character-work v0.1 §3, §4): a plan may name a project (an open arc, or the one its write
starts) when it has a time; the worker's call names its plan and its piece is a life entry of kind ``work`` that
rewrites the project's "so far" and may close it; the user edits and retires pieces. On every backend."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from testkit import data_for, new_store, restart

from himari.common import PrincipalId, TokenUsage
from himari.data.api import (
    MAX_SO_FAR_CHARS,
    MAX_WORK_CHARS,
    MAX_WORK_SUMMARY_CHARS,
    NEW_ARC,
    ArcMove,
    ArcOp,
    DataError,
    DataErrorCode,
    LifeEntry,
    LifeJob,
    LifeKind,
    LifeOrigin,
    LifePlan,
    LifeWriteStart,
    LifeWriteStatus,
    RawReceived,
    WorkDraft,
    WorkPiece,
)
from himari.data.memory import InMemoryData
from himari.data.sqlite import SqliteData

ALICE = PrincipalId("alice")
NOW = datetime(2026, 11, 25, 13, 0, tzinfo=UTC)
PIECE = WorkPiece("냉각부 설계. 블랙홀 증발열을 포신 뒤로 뺀다. " * 10, "냉각부", "냉각부 설계 끝냈어.",
                  "포신과 냉각부까지 정했다.")


def refused(code: DataErrorCode) -> pytest.RaisesExc[DataError]:
    return pytest.raises(DataError, check=lambda e: e.code is code)


async def received(data: InMemoryData | SqliteData, day: str, plan_id: int | None = None, persona_id: int = 1) -> str:
    write_id = await data.begin_life_write(ALICE, LifeWriteStart(persona_id, day, "openrouter", "m", b"{}",
                                                                 "application/json", 100, plan_id=plan_id))
    await data.mark_life_write_dispatched(write_id)
    await data.record_life_write(write_id, RawReceived(b"{}", "application/json", TokenUsage(10, 10)))
    return write_id


async def planned(data: InMemoryData | SqliteData) -> tuple[LifeEntry, LifeEntry]:
    """A project started with a work plan for it the next evening (NEW_ARC); the arc and the plan."""
    written = await data.apply_life_write(await received(data, "2026-11-24"), LifePlan(
        "전자포를 구상했다.", (("2026-11-25", "내일 밤엔 냉각부 도면 그려야지."),), times=("20:00",),
        arc=ArcMove(ArcOp.START, title="블랙홀 전자포", goal="설정집 한 권"), plan_arcs=(NEW_ARC,)), now=NOW)
    arc, _diary, plan = written
    return arc, plan


def test_a_work_plan_names_its_project_only_with_a_time_and_an_open_arc() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        arc, plan = await planned(data)
        assert (arc.kind, plan.kind, plan.arc_id, plan.time_of_day) == (LifeKind.ARC, LifeKind.PLAN, arc.entry_id,
                                                                         "20:00")
        written = await data.apply_life_write(await received(data, "2026-11-25"), LifePlan(
            "또 하루.", (("2026-11-26", "시각 없는 일정."), ("2026-11-26", "없는 프로젝트."),
                        ("2026-11-27", "열린 프로젝트.")), times=(None, "10:00", "11:00"),
            plan_arcs=(arc.entry_id, 99, arc.entry_id)), now=NOW)
        assert [(e.text, e.arc_id) for e in written if e.kind is LifeKind.PLAN] == [
            ("시각 없는 일정.", None), ("없는 프로젝트.", None), ("열린 프로젝트.", arc.entry_id)]
        new = await data.apply_life_write(await received(data, "2026-11-26"), LifePlan(
            "새 프로젝트 없이.", (("2026-11-27", "새 것."),), times=("09:00",), plan_arcs=(NEW_ARC,)), now=NOW)
        assert [e.arc_id for e in new if e.kind is LifeKind.PLAN] == [None]  # no start: no new arc
        with refused(DataErrorCode.INVALID_ARGUMENT):
            await data.apply_life_write(await received(data, "2026-11-27"), LifePlan(
                "x", (("2026-11-28", "y"),), plan_arcs=(-1,)), now=NOW)
        with refused(DataErrorCode.INVALID_ARGUMENT):
            await data.apply_life_write(await received(data, "2026-11-27"), LifePlan("x", plan_arcs=(1,)), now=NOW)

    asyncio.run(scenario())


def test_a_piece_is_kept_with_its_plan_and_project_and_rewrites_the_so_far() -> None:
    async def scenario() -> None:
        store = new_store()
        data = data_for(store)
        arc, plan = await planned(data)
        write_id = await received(data, "2026-11-25", plan.entry_id)
        assert await data.work_attempts(ALICE, plan.entry_id) == 1
        assert await data.life_attempts(ALICE, 1, "2026-11-25") == 0  # a worker's call is not a day's write
        piece, project = await data.apply_work_write(write_id, PIECE, now=NOW)
        assert (piece.kind, piece.day, piece.time_of_day, piece.title, piece.summary, piece.arc_id, piece.plan_id,
                piece.origin, piece.source) == (LifeKind.WORK, "2026-11-25", "20:00", "냉각부", "냉각부 설계 끝냈어.",
                                                arc.entry_id, plan.entry_id, LifeOrigin.WRITER, write_id)
        assert piece.text == PIECE.text.strip()
        assert (project.entry_id, project.so_far, project.revision, project.arc_open) == (
            arc.entry_id, "포신과 냉각부까지 정했다.", 2, True)
        (record, *_) = await data.list_life_writes(ALICE)
        assert (record.status, record.plan_id) == (LifeWriteStatus.APPLIED, plan.entry_id)
        # every piece of an open project is listed, whatever its date
        listed = await data.list_life(ALICE, 1, since="2026-12-20")
        assert [e.kind for e in listed] == [LifeKind.ARC, LifeKind.WORK]
        data = data_for(restart(store))
        assert [e for e in await data.list_life(ALICE, 1) if e.kind is LifeKind.WORK] == [piece]
        with refused(DataErrorCode.INVALID_ARGUMENT):  # one piece a plan
            await data.apply_work_write(await received(data, "2026-11-25", plan.entry_id), PIECE, now=NOW)
        with refused(DataErrorCode.INVALID_ARGUMENT):  # a story writer's call makes no piece
            await data.apply_work_write(await received(data, "2026-11-25"), PIECE, now=NOW)
        with refused(DataErrorCode.INVALID_ARGUMENT):  # and a worker's call writes no day
            await data.apply_life_write(await received(data, "2026-11-25", plan.entry_id), LifePlan("x"), now=NOW)

    asyncio.run(scenario())


def test_a_piece_may_close_its_project_and_then_its_pieces_are_dated_ones() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        arc, plan = await planned(data)
        piece, project = await data.apply_work_write(
            await received(data, "2026-11-25", plan.entry_id),
            WorkPiece(PIECE.text, "마지막 장", "다 됐어!", "설정집 완성.", done="설정집을 끝냈다."), now=NOW)
        assert (project.end_day, project.ending, project.arc_open) == ("2026-11-25", "설정집을 끝냈다.", False)
        assert [e.kind for e in await data.list_life(ALICE, 1, since="2026-12-20")] == []  # closed: by date only
        assert piece in await data.list_life(ALICE, 1, since="2026-11-25")

    asyncio.run(scenario())


def test_a_piece_for_no_open_work_plan_or_out_of_shape_is_refused() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        arc, plan = await planned(data)
        await data.retire_life(ALICE, arc.entry_id, expected_revision=1)
        with refused(DataErrorCode.INVALID_ARGUMENT):  # the project is gone
            await data.apply_work_write(await received(data, "2026-11-25", plan.entry_id), PIECE, now=NOW)
        with refused(DataErrorCode.INVALID_ARGUMENT):  # no such plan
            await data.apply_work_write(await received(data, "2026-11-25", 999), PIECE, now=NOW)
        with refused(DataErrorCode.INVALID_ARGUMENT):
            await data.begin_life_write(ALICE, LifeWriteStart(1, "2026-11-25", "p", "m", b"{}", "j", 1, plan_id=0))
        arc2, plan2 = await planned(data)
        write_id = await received(data, "2026-11-25", plan2.entry_id)
        for bad, code in ((WorkPiece("x" * (MAX_WORK_CHARS + 1), "t", "s", "f"), DataErrorCode.TOO_LARGE),
                          (WorkPiece("x", "t", "s" * (MAX_WORK_SUMMARY_CHARS + 1), "f"), DataErrorCode.TOO_LARGE),
                          (WorkPiece("x", "t", "s", "f" * (MAX_SO_FAR_CHARS + 1)), DataErrorCode.TOO_LARGE),
                          (WorkPiece("x", "두\n줄", "s", "f"), DataErrorCode.INVALID_ARGUMENT),
                          (WorkPiece("x", "t", "두\n줄", "f"), DataErrorCode.INVALID_ARGUMENT),
                          (WorkPiece(" ", "t", "s", "f"), DataErrorCode.INVALID_ARGUMENT)):
            with refused(code):
                await data.apply_work_write(write_id, bad, now=NOW)
        await data.apply_work_write(write_id, PIECE, now=NOW)  # nothing was written meanwhile
        assert [e.kind for e in await data.list_life(ALICE, 1) if e.kind is LifeKind.WORK] == [LifeKind.WORK]
        assert arc2.entry_id != arc.entry_id

    asyncio.run(scenario())


def test_the_user_edits_a_pieces_text_title_and_summary_and_retires_it() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        _, plan = await planned(data)
        piece, _ = await data.apply_work_write(await received(data, "2026-11-25", plan.entry_id), PIECE, now=NOW)
        edited = await data.edit_life(ALICE, piece.entry_id, "고친 조각.", expected_revision=1, title="냉각부 2",
                                      summary="고쳤어.")
        assert (edited.text, edited.title, edited.summary, edited.origin, edited.revision) == (
            "고친 조각.", "냉각부 2", "고쳤어.", LifeOrigin.USER, 2)
        kept = await data.edit_life(ALICE, piece.entry_id, "또.", expected_revision=2)
        assert (kept.title, kept.summary) == ("냉각부 2", "고쳤어.")
        with refused(DataErrorCode.INVALID_ARGUMENT):  # only a piece has a summary
            await data.edit_life(ALICE, plan.entry_id, "일정.", expected_revision=1, summary="요약")
        with refused(DataErrorCode.TOO_LARGE):
            await data.edit_life(ALICE, piece.entry_id, "x" * (MAX_WORK_CHARS + 1), expected_revision=3)
        await data.retire_life(ALICE, piece.entry_id, expected_revision=3)
        assert [e for e in await data.list_life(ALICE, 1) if e.kind is LifeKind.WORK] == []

    asyncio.run(scenario())


def test_a_start_step_leaves_a_draft_its_end_fills_and_the_draft_never_counts_as_made() -> None:
    """character-mind §4.4: one draft a plan, from a work_start write; the work_end write fills the same entry."""
    async def scenario() -> None:
        store = new_store()
        data = data_for(store)
        arc, plan = await planned(data)

        async def begun(job: LifeJob) -> str:
            write_id = await data.begin_life_write(ALICE, LifeWriteStart(1, "2026-11-25", "openrouter", "m", b"{}",
                                                                         "application/json", 100,
                                                                         plan_id=plan.entry_id, job=job))
            await data.mark_life_write_dispatched(write_id)
            await data.record_life_write(write_id, RawReceived(b"{}", "application/json", TokenUsage(10, 10)))
            return write_id

        sketch = WorkDraft("냉각부 스케치", "방열판 세 겹으로.", "첫 스케치. " * 10)
        with refused(DataErrorCode.INVALID_ARGUMENT):
            await data.apply_work_start(await begun(LifeJob.WORK_END), sketch, now=NOW)  # not a start
        start = await begun(LifeJob.WORK_START)
        with refused(DataErrorCode.INVALID_ARGUMENT):
            await data.apply_work_write(start, PIECE, now=NOW)  # not an end
        draft = await data.apply_work_start(start, sketch, now=NOW)
        assert (draft.kind, draft.draft, draft.title, draft.summary, draft.text, draft.plan_id, draft.arc_id,
                draft.day, draft.time_of_day, draft.source) == (
            LifeKind.WORK, True, "냉각부 스케치", "방열판 세 겹으로.", ("첫 스케치. " * 10).strip(), plan.entry_id,
            arc.entry_id, "2026-11-25", "20:00", start)
        with refused(DataErrorCode.INVALID_ARGUMENT):
            await data.apply_work_start(await begun(LifeJob.WORK_START), sketch, now=NOW)  # one a plan
        later = NOW + timedelta(hours=1)
        end = await begun(LifeJob.WORK_END)
        piece, project = await data.apply_work_write(end, PIECE, now=later)
        assert (piece.entry_id, piece.draft, piece.text, piece.summary, piece.created_at, piece.source) == (
            draft.entry_id, False, PIECE.text.strip(), PIECE.summary, later, end)
        assert project.so_far == PIECE.so_far
        data = data_for(restart(store))
        (kept,) = [e for e in await data.list_life(ALICE, 1) if e.kind is LifeKind.WORK]
        assert kept == piece
        with refused(DataErrorCode.INVALID_ARGUMENT):
            await data.apply_work_write(await begun(LifeJob.WORK_END), PIECE, now=later)  # finished already

    asyncio.run(scenario())
