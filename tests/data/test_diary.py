"""The diary slot and the diary's amend in Data (character-diary v0.1 §3, §5, §16.2): the keeper makes the night's
diary slot; HIMARI and the user only move it (never make, cancel, delete or change its kind), from 18:00 of its night
to before that night's sleep slot, 10 to 60 minutes; a sleep moved earlier may cover it. An amend rewrites the
writer's diary in one step and leaves the user's alone. On every backend."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from testkit import SCOPE, data_for, new_store

from himari.data.api import (
    DataError,
    DataErrorCode,
    FellBy,
    LifeJob,
    LifeKind,
    LifeOrigin,
    LifePlan,
    LifeWriteStart,
    LifeWriteStatus,
    PlanChange,
    PlanKind,
    PlanOp,
    PlanWrite,
    RawReceived,
    WokeBy,
)

ALICE = SCOPE.principal_id
ZONE = "Asia/Seoul"
NOW = datetime(2026, 11, 26, 3, 0, tzinfo=UTC)  # 12:00 in Seoul, a Thursday


def refused(code: DataErrorCode, match: str = "") -> pytest.RaisesExc[DataError]:
    return pytest.raises(DataError, check=lambda e: e.code is code and match in e.message)


def slot(day: str, at: str, minutes: int, kind: PlanKind = PlanKind.DIARY, text: str = "일기 쓰기", *,
         entry_id: int | None = None, reason: str | None = None) -> PlanWrite:
    return PlanWrite(day, at, minutes, kind, text, entry_id, None, reason)


async def night(data: Any) -> tuple[int, int]:
    """Tonight's sleep slot (00:30–08:10) and its diary slot (22:00–22:30), placed by the keeper."""
    sleep = await data.write_plan(ALICE, 1, slot("2026-11-27", "00:30", 460, PlanKind.SLEEP, "잠"),
                                  origin=LifeOrigin.WRITER, zone=ZONE, now=NOW)
    diary = await data.write_plan(ALICE, 1, slot("2026-11-26", "22:00", 30),
                                  origin=LifeOrigin.WRITER, zone=ZONE, now=NOW)
    return sleep.entry_id, diary.entry_id


def test_only_the_keeper_makes_a_diary_slot_one_a_night_before_the_sleep() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        sleep_id, diary_id = await night(data)
        made = next(e for e in await data.list_life(ALICE, 1, now=NOW) if e.entry_id == diary_id)
        assert (made.plan_kind, made.minutes, made.origin, made.diary_slot) == (
            PlanKind.DIARY, 30, LifeOrigin.WRITER, True)
        for origin in (LifeOrigin.HIMARI, LifeOrigin.USER):
            with refused(DataErrorCode.PLAN_REFUSED, f"only moved, never made: change that night's (id {diary_id})"):
                await data.write_plan(ALICE, 1, slot("2026-11-26", "21:00", 30), origin=origin, zone=ZONE, now=NOW)
        with refused(DataErrorCode.PLAN_REFUSED, "has its diary slot already"):
            await data.write_plan(ALICE, 1, slot("2026-11-26", "21:00", 30), origin=LifeOrigin.WRITER, zone=ZONE,
                                  now=NOW)
        await data.write_plan(ALICE, 1, slot("2026-11-27", "23:30", 480, PlanKind.SLEEP, "잠"),
                              origin=LifeOrigin.WRITER, zone=ZONE, now=NOW)
        with refused(DataErrorCode.PLAN_REFUSED, "ends before that night's sleep slot"):
            await data.write_plan(ALICE, 1, slot("2026-11-27", "23:10", 30), origin=LifeOrigin.WRITER, zone=ZONE,
                                  now=NOW)
        assert sleep_id

    asyncio.run(scenario())


def test_himari_and_the_user_only_move_the_diary_slot_within_its_limits() -> None:
    """§3, §16.2: a move keeps its kind and text; from 18:00 of its night, 10 to 60 minutes, ending before the sleep
    slot, never into another night; no other slot becomes one, and it becomes no other."""
    async def scenario() -> None:
        data = data_for(new_store())
        sleep_id, diary_id = await night(data)
        moved = await data.write_plan(ALICE, 1, slot("2026-11-26", "23:20", 60, text="다른 글", entry_id=diary_id,
                                                     reason="영화 보고 쓸래"),
                                      origin=LifeOrigin.HIMARI, zone=ZONE, now=NOW)
        assert (moved.time_of_day, moved.minutes, moved.text, moved.plan_kind, moved.origin, moved.reason) == (
            "23:20", 60, "일기 쓰기", PlanKind.DIARY, LifeOrigin.HIMARI, "영화 보고 쓸래")
        by_user = await data.write_plan(ALICE, 1, slot("2026-11-26", "18:00", 10, entry_id=diary_id),
                                        origin=LifeOrigin.USER, zone=ZONE, now=NOW)
        assert (by_user.time_of_day, by_user.minutes, by_user.origin) == ("18:00", 10, LifeOrigin.USER)
        for bad, why in ((slot("2026-11-26", "17:50", 30, entry_id=diary_id), "starts between"),
                         (slot("2026-11-26", "21:00", 9, entry_id=diary_id), "10 to 60 minutes"),
                         (slot("2026-11-26", "21:00", 61, entry_id=diary_id), "10 to 60 minutes"),
                         (slot("2026-11-27", "00:10", 30, entry_id=diary_id), "ends before that night's sleep"),
                         (slot("2026-11-27", "21:00", 30, entry_id=diary_id), "stays in its night"),
                         (slot("2026-11-26", "21:00", 30, PlanKind.OWN, entry_id=diary_id), "stays a diary slot")):
            with refused(DataErrorCode.PLAN_REFUSED, why):
                await data.write_plan(ALICE, 1, bad, origin=LifeOrigin.HIMARI, zone=ZONE, now=NOW)
        own = await data.write_plan(ALICE, 1, slot("2026-11-26", "15:00", 60, PlanKind.OWN, "산책"),
                                    origin=LifeOrigin.HIMARI, zone=ZONE, now=NOW)
        with refused(DataErrorCode.PLAN_REFUSED, "stays a diary slot"):  # no other slot becomes one
            await data.write_plan(ALICE, 1, slot("2026-11-26", "19:00", 30, entry_id=own.entry_id),
                                  origin=LifeOrigin.USER, zone=ZONE, now=NOW)
        assert sleep_id

    asyncio.run(scenario())


def test_the_diary_slot_is_never_cancelled_deleted_or_edited_as_text() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        _, diary_id = await night(data)
        for origin in (LifeOrigin.HIMARI, LifeOrigin.USER):
            with refused(DataErrorCode.PLAN_REFUSED, "never skipped"):
                await data.cancel_plan(ALICE, diary_id, "귀찮아", origin=origin, zone=ZONE, now=NOW)
        with refused(DataErrorCode.PLAN_REFUSED, "never skipped"):
            await data.retire_life(ALICE, diary_id, expected_revision=1)
        with refused(DataErrorCode.PLAN_REFUSED, "only moved"):
            await data.edit_life(ALICE, diary_id, "다른 글", expected_revision=1)
        assert any(e.entry_id == diary_id and e.diary_slot for e in await data.list_life(ALICE, 1, now=NOW))

    asyncio.run(scenario())


def test_a_sleep_moved_earlier_may_cover_the_diary_slot() -> None:
    """§3, §16.2: the overlap check is the diary's own — a sleep slot moved over it (or go_to_sleep, or the keeper)
    is never held back; the diary slot stays as it is."""
    async def scenario() -> None:
        data = data_for(new_store())
        sleep_id, diary_id = await night(data)
        moved = await data.write_plan(ALICE, 1, slot("2026-11-26", "21:30", 600, PlanKind.SLEEP, "잠",
                                                     entry_id=sleep_id, reason="피곤해"),
                                      origin=LifeOrigin.HIMARI, zone=ZONE, now=NOW)
        assert moved.time_of_day == "21:30"
        assert any(e.entry_id == diary_id and e.diary_slot for e in await data.list_life(ALICE, 1, now=NOW))
        with refused(DataErrorCode.PLAN_REFUSED, "ends before that night's sleep"):  # the diary slot never goes over
            await data.write_plan(ALICE, 1, slot("2026-11-26", "21:20", 30, entry_id=diary_id),
                                  origin=LifeOrigin.HIMARI, zone=ZONE, now=NOW)
        other = data_for(new_store())  # go_to_sleep at 21:40 over a 22:00 diary slot
        await night(other)
        at = datetime(2026, 11, 26, 12, 40, tzinfo=UTC)  # 21:40
        from himari.data.sqlite import SqliteData
        change = PlanChange(PlanOp.SLEEP_NOW, 1, ZONE, at, reason="잘 자")
        if isinstance(other, SqliteData):
            with other._tx() as tx:  # noqa: SLF001 — the change as the call's commit applies it
                other._apply_plan_change(tx, ALICE, change)
        else:
            other._stage_plan_change(ALICE, change)()
        assert (await other.read_day_state(ALICE, 1)).sleep_requested_at == at

    asyncio.run(scenario())


async def amend_write(data: Any, day: str = "2026-11-26") -> str:
    write = await data.begin_life_write(ALICE, LifeWriteStart(
        1, day, "p", "m", b"{}", "application/json", 10, job=LifeJob.AMEND))
    await data.record_life_write(write, RawReceived(b"{}", "application/json"))
    return str(write)


async def diary(data: Any, text: str = "오늘은 산책을 했다.") -> int:
    write = await data.begin_life_write(ALICE, LifeWriteStart(
        1, "2026-11-26", "p", "m", b"{}", "application/json", 10))
    await data.record_life_write(write, RawReceived(b"{}", "application/json"))
    written = await data.apply_life_write(write, LifePlan(text), now=NOW)
    return int(next(e.entry_id for e in written if e.kind is LifeKind.DIARY))


def test_an_amend_rewrites_the_writers_diary_and_its_mood_in_one_step() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        diary_id = await diary(data)
        write = await amend_write(data)
        at = NOW + timedelta(hours=14)
        kept = await data.apply_life_amend(write, "2026-11-26", "오늘은 산책을 했고, 밤엔 면접 얘기를 했다.",
                                           mood="happy", mood_reason="응원받아서", now=at)
        assert kept is not None and (kept.entry_id, kept.text, kept.origin, kept.revision, kept.source) == (
            diary_id, "오늘은 산책을 했고, 밤엔 면접 얘기를 했다.", LifeOrigin.WRITER, 2, write)
        mood = await data.read_mood(ALICE, 1)
        assert mood is not None and (mood.mood, mood.reason) == ("happy", "응원받아서")
        (record,) = [w for w in await data.list_life_writes(ALICE) if w.write_id == write]
        assert record.status is LifeWriteStatus.APPLIED and await data.life_done(ALICE, 1, "2026-11-26",
                                                                                 LifeJob.AMEND)
        nothing = await amend_write(data)
        assert await data.apply_life_amend(nothing, "2026-11-26", None, now=at) is None  # nothing to add
        with pytest.raises(DataError, check=lambda e: e.code is DataErrorCode.INVALID_STATE_TRANSITION):
            await data.apply_life_amend(nothing, "2026-11-26", "다시", now=at)

    asyncio.run(scenario())


def test_an_amend_leaves_the_users_diary_and_mood_alone_and_checks_its_write() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        diary_id = await diary(data)
        edited = await data.edit_life(ALICE, diary_id, "내가 고친 일기.", expected_revision=1)
        write = await amend_write(data)
        assert await data.apply_life_amend(write, "2026-11-26", "덧붙임", mood="sad", now=NOW) is None
        found = next(e for e in await data.list_life(ALICE, 1, now=NOW) if e.entry_id == diary_id)
        assert found == edited and await data.read_mood(ALICE, 1) is None
        assert await data.life_done(ALICE, 1, "2026-11-26", LifeJob.AMEND)  # applied all the same
        other = await amend_write(data, "2026-11-25")
        with pytest.raises(DataError, check=lambda e: e.code is DataErrorCode.INVALID_ARGUMENT):
            await data.apply_life_amend(other, "2026-11-26", "x", now=NOW)  # another date's
        review = await data.begin_life_write(ALICE, LifeWriteStart(1, "2026-11-26", "p", "m", b"{}",
                                                                   "application/json", 10))
        await data.record_life_write(review, RawReceived(b"{}", "application/json"))
        with pytest.raises(DataError, check=lambda e: e.code is DataErrorCode.INVALID_ARGUMENT):
            await data.apply_life_amend(review, "2026-11-26", "x", now=NOW)  # not an amend
        with pytest.raises(DataError, check=lambda e: e.code is DataErrorCode.TOO_LARGE):
            await data.apply_life_amend(other, "2026-11-25", "가" * 1_501, now=NOW)

    asyncio.run(scenario())



def test_a_diarys_write_keeps_what_its_request_was_made_of() -> None:
    """character-diary §5 (review P1-6, v30): the session's last event its request held and the moment its data was
    read up to, kept with the write (None for a write without them)."""
    async def scenario() -> None:
        data = data_for(new_store())
        at = datetime(2026, 11, 25, 13, 40, tzinfo=UTC)
        write = await data.begin_life_write(ALICE, LifeWriteStart(
            1, "2026-11-25", "p", "m", b"{}", "application/json", 10, basis_sequence=7, basis_at=at))
        bare = await data.begin_life_write(ALICE, LifeWriteStart(1, "2026-11-25", "p", "m", b"{}",
                                                                 "application/json", 10))
        found = {w.write_id: w for w in await data.list_life_writes(ALICE)}
        assert (found[write].basis_sequence, found[write].basis_at) == (7, at)
        assert (found[bare].basis_sequence, found[bare].basis_at) == (None, None)

    asyncio.run(scenario())



def test_a_skipped_write_is_final_and_tries_are_counted_from_a_moment() -> None:
    """character-diary §5 (review 2, 1, 2, 5): a write never sent may be SKIPPED with why — final as APPLIED, charged
    nothing; the tries of a job and date may be counted from a moment (``basis_at``)."""
    async def scenario() -> None:
        data = data_for(new_store())
        at = datetime(2026, 11, 25, 18, 30, tzinfo=UTC)
        write = await data.begin_life_write(ALICE, LifeWriteStart(
            1, "2026-11-25", "p", "", b"", "", 0, job=LifeJob.AMEND, basis_at=at))
        assert not await data.life_done(ALICE, 1, "2026-11-25", LifeJob.AMEND)
        await data.skip_life_write(write, "nothing new after it")
        (record,) = await data.list_life_writes(ALICE)
        assert (record.status, record.error, record.charged_tokens) == (
            LifeWriteStatus.SKIPPED, "skipped: nothing new after it", 0)
        assert record.finished_at is not None and await data.life_done(ALICE, 1, "2026-11-25", LifeJob.AMEND)
        with pytest.raises(DataError, check=lambda e: e.code is DataErrorCode.INVALID_STATE_TRANSITION):
            await data.skip_life_write(write, "again")
        for basis in (at - timedelta(hours=1), at + timedelta(hours=1), None):
            await data.begin_life_write(ALICE, LifeWriteStart(1, "2026-11-25", "p", "m", b"{}", "j", 0,
                                                              basis_at=basis))
        assert await data.life_attempts(ALICE, 1, "2026-11-25") == 3
        assert await data.life_attempts(ALICE, 1, "2026-11-25", since=at) == 1
        assert await data.life_attempts(ALICE, 1, "2026-11-25", job=LifeJob.AMEND, since=at) == 1

    asyncio.run(scenario())


def test_the_closing_names_the_day_session_it_closed_on_its_sleep() -> None:
    """character-diary §5 (review 3, 1, v30): the sleep the day was closed in names the session it closed, so the
    amend's cutoff is that sleep's fall however late the closing was looked at; none when no session was closed."""
    async def scenario() -> None:
        data = data_for(new_store())
        session = await data.open_day_session(ALICE, "2026-11-25", "11월 25일 (수)")
        fell = datetime(2026, 11, 25, 15, 30, tzinfo=UTC)
        sleep = await data.open_sleep(ALICE, 1, at=fell, how=FellBy.SCHEDULE)
        assert await data.note_closing(ALICE, 1, sleep.sleep_id, at=fell + timedelta(hours=9), before=None,
                                       conversation_id=session)
        closed = await data.current_sleep(ALICE, 1)
        assert closed is not None and closed.closed_day and closed.closed_session == session
        await data.end_sleep(ALICE, sleep.sleep_id, at=fell + timedelta(hours=8), how=WokeBy.SCHEDULE)
        other = await data.open_sleep(ALICE, 1, at=fell + timedelta(days=1), how=FellBy.SCHEDULE)
        assert not await data.note_closing(ALICE, 1, other.sleep_id, at=fell + timedelta(days=1, hours=3),
                                           before=None, conversation_id=session)  # closed already
        again = await data.current_sleep(ALICE, 1)
        assert again is not None and again.closed_day and again.closed_session is None

    asyncio.run(scenario())
