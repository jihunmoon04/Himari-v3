"""The life's record by date (character-routine v0.1 §9.1): a date range's plans, diaries, work pieces and events, and
the sleeps that fell in a time range, for the Web's Records and its "Show older". On every backend."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from testkit import data_for, new_store, restart

from himari.common import PrincipalId, TokenUsage
from himari.data.api import (
    FellBy,
    LifeJob,
    LifeKind,
    LifePlan,
    LifeWriteStart,
    RawReceived,
    WokeBy,
    WorldEvent,
)
from himari.data.memory import InMemoryData
from himari.data.sqlite import SqliteData

ALICE, BOB = PrincipalId("alice"), PrincipalId("bob")
NOW = datetime(2026, 11, 25, 13, 0, tzinfo=UTC)


async def received(data: InMemoryData | SqliteData, day: str, job: LifeJob = LifeJob.REVIEW,
                   principal: PrincipalId = ALICE) -> str:
    write_id = await data.begin_life_write(principal, LifeWriteStart(1, day, "openrouter", "m", b"{}",
                                                                     "application/json", 100, job=job))
    await data.mark_life_write_dispatched(write_id)
    await data.record_life_write(write_id, RawReceived(b"{}", "application/json", TokenUsage(10, 10)))
    return write_id


def test_a_date_range_lists_the_dated_entries_newest_first_and_never_a_fact_or_one_outside_it() -> None:
    async def scenario() -> None:
        store = new_store()
        data = data_for(store)
        await data.add_life_fact(ALICE, 1, "고양이를 무서워한다.")
        for day, plan_day in (("2026-10-01", "2026-10-02"), ("2026-10-20", "2026-10-21"), ("2026-11-10", None)):
            await data.apply_life_write(await received(data, day), LifePlan(
                f"{day}의 일기.", ((plan_day, f"{plan_day} 산책."),) if plan_day else ()), now=NOW)
        await data.add_events(await received(data, "2026-10-20", LifeJob.WORLD), [WorldEvent("15:00", "택배.")],
                              now=NOW)
        await data.apply_life_write(await received(data, "2026-10-05", principal=BOB), LifePlan("남의 일기."),
                                    now=NOW)
        data = data_for(restart(store))

        window = await data.list_life_days(ALICE, 1, since="2026-10-02", until="2026-11-10")
        assert [(e.kind, e.day) for e in window] == [
            (LifeKind.PLAN, "2026-10-21"), (LifeKind.DIARY, "2026-10-20"), (LifeKind.EVENT, "2026-10-20"),
            (LifeKind.PLAN, "2026-10-02")]  # "until" is not in it, "since" is; a date's diary first, as list_life
        older = await data.list_life_days(ALICE, 1, until="2026-10-02")
        assert [(e.kind, e.day) for e in older] == [(LifeKind.DIARY, "2026-10-01")]
        assert [e.day for e in await data.list_life_days(ALICE, 1, until="2026-12-01", limit=2)] == [
            "2026-11-10", "2026-10-21"]
        assert await data.list_life_days(ALICE, 1, until="2026-10-01") == ()
        assert await data.list_life_days(ALICE, 2, until="2026-12-01") == ()  # another persona's
        assert [e.text for e in await data.list_life_days(BOB, 1, until="2026-12-01")] == ["남의 일기."]
        # a retired entry is gone from it
        (diary,) = [e for e in window if e.kind is LifeKind.DIARY]
        await data.retire_life(ALICE, diary.entry_id, expected_revision=diary.revision)
        assert LifeKind.DIARY not in {e.kind for e in await data.list_life_days(ALICE, 1, since="2026-10-02",
                                                                                until="2026-11-10")}

    asyncio.run(scenario())


def test_the_sleeps_are_listed_by_when_they_fell_newest_first() -> None:
    async def scenario() -> None:
        store = new_store()
        data = data_for(store)
        fell = [NOW - timedelta(days=days) for days in (40, 20, 1)]
        for at in fell:
            sleep = await data.open_sleep(ALICE, 1, at=at, how=FellBy.SCHEDULE)
            await data.end_sleep(ALICE, sleep.sleep_id, at=at + timedelta(hours=7), how=WokeBy.SCHEDULE)
        await data.open_sleep(BOB, 1, at=NOW, how=FellBy.TOOL)
        data = data_for(restart(store))
        assert [s.fell_at for s in await data.list_sleeps(ALICE, 1)] == fell[::-1]
        assert [s.fell_at for s in await data.list_sleeps(ALICE, 1, since=fell[1])] == [fell[2], fell[1]]
        assert [s.fell_at for s in await data.list_sleeps(ALICE, 1, until=fell[1])] == [fell[0]]  # before it
        assert [s.fell_at for s in await data.list_sleeps(ALICE, 1, since=fell[0], until=fell[2], limit=1)] == [
            fell[1]]
        assert await data.list_sleeps(ALICE, 2) == ()
        assert [s.how_fell for s in await data.list_sleeps(BOB, 1)] == [FellBy.TOOL]

    asyncio.run(scenario())
