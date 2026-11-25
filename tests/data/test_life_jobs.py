"""A life write's job (character-mind v0.1 §14.2): each write names its job (review, settle, world, work_start,
work_end) and the model's reason; attempts are counted per job; a self run's thought is kept with its write; a settle
write keeps the facts its diary holds. On every backend."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from testkit import data_for, new_store, restart

from himari.common import PrincipalId, TokenUsage
from himari.data.api import (
    DataError,
    DataErrorCode,
    LifeJob,
    LifeKind,
    LifeOrigin,
    LifePlan,
    LifeWriteStart,
    LifeWriteStatus,
    RawReceived,
    WorldEvent,
)

ALICE = PrincipalId("alice")
NOW = datetime(2026, 11, 25, 13, 0, tzinfo=UTC)


def refused(code: DataErrorCode) -> pytest.RaisesExc[DataError]:
    return pytest.raises(DataError, check=lambda e: e.code is code)


def start(job: LifeJob | None, plan_id: int | None = None, reason: str | None = None) -> LifeWriteStart:
    return LifeWriteStart(1, "2026-11-25", "openrouter", "m", b"{}", "application/json", 100, plan_id=plan_id,
                          job=job, model_reason=reason)


def test_a_write_names_its_job_and_attempts_are_counted_per_job() -> None:
    async def scenario() -> None:
        store = new_store()
        data = data_for(store)
        review = await data.begin_life_write(ALICE, start(LifeJob.REVIEW, reason="the latest conversation's model"))
        await data.begin_life_write(ALICE, start(None))  # REVIEW without a plan
        await data.begin_life_write(ALICE, start(LifeJob.SETTLE))
        await data.begin_life_write(ALICE, start(None, plan_id=7))  # WORK_END with one
        await data.begin_life_write(ALICE, start(LifeJob.WORK_START, plan_id=7))
        for bad in (start(LifeJob.WORK_START), start(LifeJob.REVIEW, plan_id=7)):
            with refused(DataErrorCode.INVALID_ARGUMENT):
                await data.begin_life_write(ALICE, bad)
        await data.note_life_thought(review, " 오늘은 좀 지쳤다. ")
        with refused(DataErrorCode.NOT_FOUND):
            await data.note_life_thought("lwrite_none", "x")
        data = data_for(restart(store))
        assert await data.life_attempts(ALICE, 1, "2026-11-25") == 2
        assert await data.life_attempts(ALICE, 1, "2026-11-25", job=LifeJob.SETTLE) == 1
        assert await data.life_attempts(ALICE, 1, "2026-11-25", job=LifeJob.WORLD) == 0
        assert await data.work_attempts(ALICE, 7) == 1
        assert await data.work_attempts(ALICE, 7, job=LifeJob.WORK_START) == 1
        written = {w.write_id: w for w in await data.list_life_writes(ALICE)}
        assert (written[review].job, written[review].model_reason, written[review].thought) == (
            LifeJob.REVIEW, "the latest conversation's model", "오늘은 좀 지쳤다.")
        assert sorted(w.job.value for w in written.values()) == [
            "review", "review", "settle", "work_end", "work_start"]

    asyncio.run(scenario())


async def received(data: object, job: LifeJob) -> str:
    write_id: str = await data.begin_life_write(ALICE, start(job))  # type: ignore[attr-defined]
    await data.mark_life_write_dispatched(write_id)  # type: ignore[attr-defined]
    await data.record_life_write(write_id, RawReceived(b"{}", "application/json", TokenUsage(10, 10)))  # type: ignore[attr-defined]
    return write_id


def test_a_settle_write_keeps_the_new_facts_once_and_only_a_settle_write_may() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        known = await data.add_life_fact(ALICE, 1, "고양이를 무서워한다.")
        review = await received(data, LifeJob.REVIEW)
        assert not await data.life_done(ALICE, 1, "2026-11-25", LifeJob.REVIEW)
        await data.apply_life_write(review, LifePlan("유나랑 레코드 가게에 갔다."), now=NOW)
        assert await data.life_done(ALICE, 1, "2026-11-25", LifeJob.REVIEW)
        assert not await data.life_done(ALICE, 1, "2026-11-25", LifeJob.SETTLE)
        with refused(DataErrorCode.INVALID_ARGUMENT):
            await data.apply_settle_write(await received(data, LifeJob.REVIEW), ["x"], now=NOW)
        settle = await received(data, LifeJob.SETTLE)
        with refused(DataErrorCode.INVALID_ARGUMENT):
            await data.apply_life_write(settle, LifePlan("일기"), now=NOW)  # not a review
        kept = await data.apply_settle_write(settle, ["친구 유나가 있다.", "고양이를 무서워한다."], now=NOW)
        assert [(e.kind, e.text, e.origin, e.source) for e in kept] == [
            (LifeKind.FACT, "친구 유나가 있다.", LifeOrigin.WRITER, settle)]
        with refused(DataErrorCode.INVALID_STATE_TRANSITION):
            await data.apply_settle_write(settle, [], now=NOW)
        assert await data.life_done(ALICE, 1, "2026-11-25", LifeJob.SETTLE)
        (record,) = [w for w in await data.list_life_writes(ALICE) if w.write_id == settle]
        assert record.status is LifeWriteStatus.APPLIED
        assert [e.text for e in await data.list_life(ALICE, 1) if e.kind is LifeKind.FACT] == [
            known.text, "친구 유나가 있다."]

    asyncio.run(scenario())


def test_a_world_write_keeps_at_most_two_events_a_date_which_the_user_edits_and_deletes() -> None:
    """character-mind §7: events of the write's date with their times; the user edits the text and deletes."""
    async def scenario() -> None:
        store = new_store()
        data = data_for(store)
        with refused(DataErrorCode.INVALID_ARGUMENT):
            await data.add_events(await received(data, LifeJob.REVIEW), [WorldEvent("15:00", "x")], now=NOW)
        first = await received(data, LifeJob.WORLD)
        for bad in (WorldEvent("3pm", "x"), WorldEvent("15:00", "두 줄\n글"), WorldEvent("15:00", "x" * 201)):
            with pytest.raises(DataError):
                await data.add_events(first, [bad], now=NOW)
        made = await data.add_events(first, [WorldEvent("15:00", "택배가 늦게 왔다.")], now=NOW)
        assert [(e.kind, e.day, e.time_of_day, e.text, e.source) for e in made] == [
            (LifeKind.EVENT, "2026-11-25", "15:00", "택배가 늦게 왔다.", first)]
        more = await data.add_events(await received(data, LifeJob.WORLD),
                                     [WorldEvent("18:00", "유나가 왔다."), WorldEvent("20:00", "셋째.")], now=NOW)
        assert [e.text for e in more] == ["유나가 왔다."]  # two a date at most
        data = data_for(restart(store))
        listed = [e for e in await data.list_life(ALICE, 1, since="2026-11-25") if e.kind is LifeKind.EVENT]
        assert [e.time_of_day for e in listed] == ["18:00", "15:00"]
        assert await data.list_life(ALICE, 1, since="2026-11-26") == ()
        edited = await data.edit_life(ALICE, listed[0].entry_id, "유나가 앨범을 들고 왔다.", expected_revision=1)
        assert (edited.text, edited.origin, edited.time_of_day) == ("유나가 앨범을 들고 왔다.", LifeOrigin.USER, "18:00")
        await data.retire_life(ALICE, edited.entry_id, expected_revision=2)
        assert [e.text for e in await data.list_life(ALICE, 1) if e.kind is LifeKind.EVENT] == ["택배가 늦게 왔다."]

    asyncio.run(scenario())
