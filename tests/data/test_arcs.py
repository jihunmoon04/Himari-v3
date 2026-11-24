"""HIMARI's arcs (character-world v0.1 §4, W3): life entries of kind ``arc`` the story writer starts, carries
on and closes, one move a day and at most two open per persona; the user edits and retires them. On every backend."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from testkit import data_for, new_store, restart

from himari.common import PrincipalId, TokenUsage
from himari.data.api import (
    MAX_ARC_TITLE_CHARS,
    ArcMove,
    ArcOp,
    DataError,
    DataErrorCode,
    LifeEntry,
    LifeKind,
    LifeOrigin,
    LifePlan,
    LifeWriteStart,
    RawReceived,
)
from himari.data.memory import InMemoryData
from himari.data.sqlite import SqliteData

ALICE = PrincipalId("alice")
NOW = datetime(2026, 11, 25, 13, 0, tzinfo=UTC)


def refused(code: DataErrorCode) -> pytest.RaisesExc[DataError]:
    return pytest.raises(DataError, check=lambda e: e.code is code)


async def write(data: InMemoryData | SqliteData, day: str, arc: ArcMove | None, *, persona_id: int = 1,
                diary: str | None = None) -> tuple[LifeEntry, ...]:
    """One writer's day for ``persona_id``, applied; the entries written."""
    write_id = await data.begin_life_write(ALICE, LifeWriteStart(persona_id, day, "openrouter", "m", b"{}",
                                                                 "application/json", 100))
    await data.mark_life_write_dispatched(write_id)
    await data.record_life_write(write_id, RawReceived(b"{}", "application/json", TokenUsage(10, 10)))
    return await data.apply_life_write(write_id, LifePlan(diary or f"{day}의 일기.", arc=arc), now=NOW)


def start(title: str, goal: str = "천천히 해 본다.") -> ArcMove:
    return ArcMove(ArcOp.START, title=title, goal=goal)


def test_an_arc_is_started_carried_on_and_closed_by_the_days_diaries() -> None:
    async def scenario() -> None:
        store = new_store()
        data = data_for(store)
        arc, diary = await write(data, "2026-11-10", start("그림 연습", "한 달 안에 풍경화 한 장"))
        assert (arc.kind, arc.title, arc.text, arc.day, arc.end_day, arc.origin, arc.arc_open) == (
            LifeKind.ARC, "그림 연습", "한 달 안에 풍경화 한 장", "2026-11-10", None, LifeOrigin.WRITER, True)
        assert diary.arc_id == arc.entry_id
        (carried,) = await write(data, "2026-11-12", ArcMove(ArcOp.ADVANCE, entry_id=arc.entry_id))
        assert carried.kind is LifeKind.DIARY and carried.arc_id == arc.entry_id
        (plain,) = await write(data, "2026-11-13", None)  # most days move no arc
        assert plain.arc_id is None
        listed = await data.list_life(ALICE, 1, since="2026-11-12")
        assert [(e.kind, e.day) for e in listed] == [  # the open arc first, whenever it started
            (LifeKind.ARC, "2026-11-10"), (LifeKind.DIARY, "2026-11-13"), (LifeKind.DIARY, "2026-11-12")]
        closed, last = await write(data, "2026-11-24", ArcMove(ArcOp.CLOSE, entry_id=arc.entry_id,
                                                               ending="풍경화를 완성해 창가에 걸었다."))
        assert (closed.entry_id, closed.end_day, closed.ending, closed.revision, closed.arc_open) == (
            arc.entry_id, "2026-11-24", "풍경화를 완성해 창가에 걸었다.", 2, False)
        assert last.arc_id == arc.entry_id

        data = data_for(restart(store))
        listed = await data.list_life(ALICE, 1, since="2026-11-20")
        assert [(e.kind, e.day, e.end_day) for e in listed] == [  # closed since: still listed
            (LifeKind.ARC, "2026-11-10", "2026-11-24"), (LifeKind.DIARY, "2026-11-24", None)]
        assert [e.kind for e in await data.list_life(ALICE, 1, since="2026-11-25")] == []  # closed before: not

    asyncio.run(scenario())


def test_a_move_the_store_cannot_make_is_left_out_and_the_day_is_written() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        first, _ = await write(data, "2026-11-10", start("그림 연습"))
        second, _ = await write(data, "2026-11-11", start("우쿨렐레"))
        (diary,) = await write(data, "2026-11-12", start("세 번째"))  # W3: at most two open
        assert diary.kind is LifeKind.DIARY and diary.arc_id is None
        await write(data, "2026-11-13", ArcMove(ArcOp.CLOSE, entry_id=second.entry_id, ending="그만뒀다."))
        (diary,) = await write(data, "2026-11-14", start(" 그림 연습 "))  # titled as an open one
        assert diary.arc_id is None
        moves = (ArcMove(ArcOp.ADVANCE, entry_id=second.entry_id),  # closed
                 ArcMove(ArcOp.CLOSE, entry_id=99, ending="없는 것"),  # no such arc
                 ArcMove(ArcOp.ADVANCE, entry_id=diary.entry_id))  # not an arc
        for day, move in enumerate(moves, start=15):
            (written,) = await write(data, f"2026-11-{day}", move)
            assert written.arc_id is None
        (other,) = await write(data, "2026-11-20", ArcMove(ArcOp.ADVANCE, entry_id=first.entry_id),
                               persona_id=2)  # another persona's arc
        assert other.arc_id is None
        await data.retire_life(ALICE, first.entry_id, expected_revision=1)
        (gone,) = await write(data, "2026-11-21", ArcMove(ArcOp.ADVANCE, entry_id=first.entry_id))
        assert gone.arc_id is None
        restarted, _ = await write(data, "2026-11-22", start("그림 연습"))  # room again, the title free again
        assert restarted.title == "그림 연습"

    asyncio.run(scenario())


def test_a_move_out_of_shape_refuses_the_write() -> None:
    """The life module checks a move's shape; one that reaches the store out of shape is a bug, refused whole."""
    async def scenario() -> None:
        data = data_for(new_store())
        for move in (ArcMove(ArcOp.START, title="제목만"), ArcMove(ArcOp.START, title="x\ny", goal="g"),
                     ArcMove(ArcOp.ADVANCE), ArcMove(ArcOp.CLOSE, entry_id=1)):
            with refused(DataErrorCode.INVALID_ARGUMENT):
                await write(data, "2026-11-10", move)
        assert await data.list_life(ALICE, 1) == ()

    asyncio.run(scenario())


def test_the_user_edits_an_arcs_title_and_goal_and_only_an_arc_has_a_title() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        arc, diary = await write(data, "2026-11-10", start("그림 연습", "풍경화 한 장"))
        edited = await data.edit_life(ALICE, arc.entry_id, " 인물화 한 장 ", expected_revision=1,
                                      title=" 수채화 연습 ")
        assert (edited.title, edited.text, edited.origin, edited.revision, edited.arc_open) == (
            "수채화 연습", "인물화 한 장", LifeOrigin.USER, 2, True)
        kept = await data.edit_life(ALICE, arc.entry_id, "인물화 두 장", expected_revision=2)
        assert (kept.title, kept.text) == ("수채화 연습", "인물화 두 장")  # no title given: kept
        with refused(DataErrorCode.INVALID_ARGUMENT):
            await data.edit_life(ALICE, diary.entry_id, "일기", expected_revision=1, title="제목")
        with refused(DataErrorCode.INVALID_ARGUMENT):
            await data.edit_life(ALICE, arc.entry_id, "goal", expected_revision=3, title="두\n줄")
        with refused(DataErrorCode.TOO_LARGE):
            await data.edit_life(ALICE, arc.entry_id, "goal", expected_revision=3,
                                 title="x" * (MAX_ARC_TITLE_CHARS + 1))
        with refused(DataErrorCode.TOO_LARGE):
            await data.edit_life(ALICE, arc.entry_id, "x" * 201, expected_revision=3)
        # the writer still carries on and closes an arc the user edited
        closed, _ = await write(data, "2026-11-20", ArcMove(ArcOp.CLOSE, entry_id=arc.entry_id, ending="끝냈다."))
        assert (closed.title, closed.text, closed.origin, closed.end_day) == (
            "수채화 연습", "인물화 두 장", LifeOrigin.USER, "2026-11-20")

    asyncio.run(scenario())
