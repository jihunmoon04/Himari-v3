"""MoodStore and the plans' times (character-state v0.1 §2, §3) contract, on every backend."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from testkit import data_for, new_store, restart

from himari.common import PrincipalId, TokenUsage
from himari.data.api import (
    MAX_MOOD_REASON_CHARS,
    MOOD_HOURS,
    DataError,
    DataErrorCode,
    HimariMood,
    LifeKind,
    LifePlan,
    LifeWriteStart,
    MoodSource,
    RawReceived,
    live_mood,
)
from himari.data.memory import InMemoryData
from himari.data.sqlite import SqliteData

ALICE, BOB = PrincipalId("alice"), PrincipalId("bob")
NOW = datetime(2026, 11, 25, 13, 0, tzinfo=UTC)


def refused(code: DataErrorCode) -> pytest.RaisesExc[DataError]:
    return pytest.raises(DataError, check=lambda e: e.code is code)


async def received(data: InMemoryData | SqliteData, persona_id: int = 1, day: str = "2026-11-25") -> str:
    write_id = await data.begin_life_write(ALICE, LifeWriteStart(persona_id, day, "openrouter", "m", b"{}",
                                                                 "application/json", 5_000))
    await data.mark_life_write_dispatched(write_id)
    await data.record_life_write(write_id, RawReceived(b"{}", "application/json", TokenUsage(10, 10)))
    return write_id


def test_a_mood_is_kept_per_persona_and_a_later_one_wins() -> None:
    async def scenario() -> None:
        store = new_store()
        data = data_for(store)
        assert await data.read_mood(ALICE, 1) is None
        happy = await data.set_mood(ALICE, 1, "happy", source=MoodSource.REPLY, at=NOW)
        assert happy == HimariMood(1, "happy", NOW, MoodSource.REPLY)
        assert await data.read_mood(ALICE, 2) is None and await data.read_mood(BOB, 1) is None
        older = await data.set_mood(ALICE, 1, "sad", source=MoodSource.WRITER, at=NOW - timedelta(hours=1),
                                    reason="  비가 와서  ")
        assert older == happy  # one set before the stored one does not replace it
        later = await data.set_mood(ALICE, 1, "sad", source=MoodSource.WRITER, at=NOW + timedelta(minutes=1),
                                    reason="  비가 와서  ")
        assert later.reason == "비가 와서" and later.source is MoodSource.WRITER
        data = data_for(restart(store))
        assert await data.read_mood(ALICE, 1) == later

    asyncio.run(scenario())


def test_a_mood_is_checked() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        for mood in ("neutral", "angry", "", None):
            with refused(DataErrorCode.INVALID_ARGUMENT):
                await data.set_mood(ALICE, 1, mood, source=MoodSource.REPLY, at=NOW)  # type: ignore[arg-type]
        with refused(DataErrorCode.INVALID_ARGUMENT):  # only the writer says why
            await data.set_mood(ALICE, 1, "happy", source=MoodSource.REPLY, at=NOW, reason="좋아서")
        with refused(DataErrorCode.INVALID_ARGUMENT):
            await data.set_mood(ALICE, 1, "happy", source=MoodSource.WRITER, at=NOW,
                                reason="x" * (MAX_MOOD_REASON_CHARS + 1))
        with refused(DataErrorCode.INVALID_ARGUMENT):
            await data.set_mood(ALICE, 1, "happy", source=MoodSource.WRITER, at=NOW, reason="a\x00b")
        with refused(DataErrorCode.INVALID_ARGUMENT):
            await data.set_mood(ALICE, 1, "happy", source=MoodSource.REPLY, at=NOW.replace(tzinfo=None))
        with refused(DataErrorCode.INVALID_ARGUMENT):
            await data.set_mood(ALICE, -1, "happy", source=MoodSource.REPLY, at=NOW)
        assert await data.read_mood(ALICE, 1) is None

    asyncio.run(scenario())


def test_a_mood_is_told_for_six_hours() -> None:
    mood = HimariMood(1, "happy", NOW, MoodSource.REPLY)
    assert live_mood(mood, NOW) == mood
    assert live_mood(mood, NOW + timedelta(hours=MOOD_HOURS) - timedelta(seconds=1)) == mood
    assert live_mood(mood, NOW + timedelta(hours=MOOD_HOURS)) is None
    assert live_mood(mood, NOW - timedelta(minutes=1)) is None  # not yet
    assert live_mood(None, NOW) is None


def test_a_write_keeps_the_plans_times_and_the_days_mood() -> None:
    async def scenario() -> None:
        store = new_store()
        data = data_for(store)
        write_id = await received(data)
        plan = LifePlan("오늘은 서점에 갔다.", (("2026-11-26", "산책"), ("2026-11-27", "빵 굽기")), (),
                        ("07:30",), "happy", "새 책을 찾았다", NOW - timedelta(hours=2))
        written = await data.apply_life_write(write_id, plan, now=NOW)
        plans = [e for e in written if e.kind is LifeKind.PLAN]
        assert [(e.text, e.time_of_day) for e in plans] == [("산책", "07:30"), ("빵 굽기", None)]
        assert await data.read_mood(ALICE, 1) == HimariMood(1, "happy", NOW - timedelta(hours=2), MoodSource.WRITER,
                                                            "새 책을 찾았다")
        data = data_for(restart(store))
        listed = [e for e in await data.list_life(ALICE, 1) if e.kind is LifeKind.PLAN]
        assert sorted((e.text, e.time_of_day) for e in listed) == [("빵 굽기", None), ("산책", "07:30")]
        # a reply's mood set later stays over the next writer's earlier one
        await data.set_mood(ALICE, 1, "excited", source=MoodSource.REPLY, at=NOW)
        later = await received(data, day="2026-11-26")
        await data.apply_life_write(later, LifePlan("하루.", mood="sad", mood_at=NOW - timedelta(minutes=5)),
                                    now=NOW)
        assert (await data.read_mood(ALICE, 1)).mood == "excited"  # type: ignore[union-attr]
        plain = await received(data, day="2026-11-27")
        await data.apply_life_write(plain, LifePlan("평범한 하루."), now=NOW + timedelta(hours=1))
        assert (await data.read_mood(ALICE, 1)).mood == "excited"  # type: ignore[union-attr]  # none given: kept

    asyncio.run(scenario())


def test_a_writes_times_and_mood_are_checked_before_anything_is_written() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        for bad in (LifePlan("하루.", (("2026-11-26", "산책"),), (), ("25:00",)),
                    LifePlan("하루.", (("2026-11-26", "산책"),), (), ("07:30", "08:00")),  # a time for no plan
                    LifePlan("하루.", mood="neutral"),
                    LifePlan("하루.", mood="happy", mood_reason="x" * (MAX_MOOD_REASON_CHARS + 1))):
            write_id = await received(data)
            with refused(DataErrorCode.INVALID_ARGUMENT):
                await data.apply_life_write(write_id, bad, now=NOW)
        assert await data.list_life(ALICE, 1) == () and await data.read_mood(ALICE, 1) is None

    asyncio.run(scenario())
