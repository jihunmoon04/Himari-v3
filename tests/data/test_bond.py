"""BondStore (character-bond v0.1 §2–§4) contract, on every backend, and its pure day rules."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from testkit import SCOPE, data_for, new_store, restart, user

from himari.common import ConversationId, PrincipalId, TrustedScope
from himari.data import codec, rules
from himari.data.api import (
    MAX_BOND_CHARS,
    MAX_BOND_ENTRIES,
    Anniversary,
    BondChange,
    BondStage,
    BondStanding,
    BondTag,
    DataError,
    DataErrorCode,
    LifeChangeOp,
    LifeEntry,
    LifeKind,
    LifeOrigin,
    WakeKind,
    WakeReason,
    anniversaries_on,
    together_days,
)

ALICE, BOB = PrincipalId("alice"), PrincipalId("bob")
NOW = datetime(2026, 11, 25, 13, 0, tzinfo=UTC)
CONV = ConversationId("conv_1")


def refused(code: DataErrorCode) -> pytest.RaisesExc[DataError]:
    return pytest.raises(DataError, check=lambda e: e.code is code)


def add(tag: BondTag, text: str, month_day: str | None = None, seqs: tuple[int, ...] = (1,)) -> BondChange:
    return BondChange(LifeChangeOp.ADD, tag, text, month_day, sequences=seqs)


def test_the_users_bond_is_kept_per_persona_in_its_order_and_apart_from_the_life() -> None:
    async def scenario() -> None:
        store = new_store()
        data = data_for(store)
        assert await data.list_bond(ALICE, 1) == ()
        joke = await data.add_bond(ALICE, 1, BondTag.JOKE, " 감자 사건 ")
        name = await data.add_bond(ALICE, 1, BondTag.NAME, "나를 대장이라고 부른다.")
        birthday = await data.add_bond(ALICE, 1, BondTag.DATE, "사용자 생일", "03-02")
        promise = await data.add_bond(ALICE, 1, BondTag.PROMISE, "주말마다 영화 얘기 하기.")
        fact = await data.add_life_fact(ALICE, 1, "고양이를 무서워한다.")
        assert (joke.kind, joke.text, joke.origin, joke.tag, joke.month_day, joke.revision) == (
            LifeKind.BOND, "감자 사건", LifeOrigin.USER, BondTag.JOKE, None, 1)
        assert birthday.month_day == "03-02"
        assert await data.list_bond(ALICE, 1) == (name, promise, birthday, joke)  # names, promises, then newest
        assert await data.list_life(ALICE, 1) == (fact,)  # the bond is not the life
        assert await data.list_bond(ALICE, 2) == () and await data.list_bond(BOB, 1) == ()

        with refused(DataErrorCode.INVALID_ARGUMENT):  # a date needs its day
            await data.add_bond(ALICE, 1, BondTag.DATE, "처음 만난 날")
        with refused(DataErrorCode.INVALID_ARGUMENT):
            await data.add_bond(ALICE, 1, BondTag.DATE, "없는 날", "02-30")
        with refused(DataErrorCode.INVALID_ARGUMENT):  # only a date has one
            await data.add_bond(ALICE, 1, BondTag.NAME, "x", "03-02")
        with refused(DataErrorCode.TOO_LARGE):
            await data.add_bond(ALICE, 1, BondTag.JOKE, "x" * (MAX_BOND_CHARS + 1))
        assert (await data.add_bond(ALICE, 1, BondTag.DATE, "윤일", "02-29")).month_day == "02-29"

        edited = await data.edit_bond(ALICE, joke.entry_id, BondTag.FIRST, "처음으로 같이 밤샘", expected_revision=1)
        assert (edited.tag, edited.text, edited.revision, edited.created_at) == (
            BondTag.FIRST, "처음으로 같이 밤샘", 2, joke.created_at)
        with refused(DataErrorCode.VERSION_CONFLICT):
            await data.edit_bond(ALICE, joke.entry_id, BondTag.JOKE, "lost", expected_revision=1)
        with refused(DataErrorCode.NOT_FOUND):  # a fact is not a bond entry
            await data.edit_bond(ALICE, fact.entry_id, BondTag.JOKE, "x", expected_revision=1)

        data = data_for(restart(store))
        assert edited in await data.list_bond(ALICE, 1)
        await data.retire_life(ALICE, edited.entry_id, expected_revision=2)  # the life's delete retires it
        assert edited not in await data.list_bond(ALICE, 1)

    asyncio.run(scenario())


def test_a_passes_bond_decisions_apply_each_on_its_own() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        mine = await data.add_bond(ALICE, 1, BondTag.NAME, "대장")
        outcomes = await data.apply_bond_changes(ALICE, 1, [
            add(BondTag.PROMISE, "주말마다 영화 얘기 하기.", seqs=(1, 2)),
            add(BondTag.PROMISE, "주말마다 영화 얘기 하기."),  # the same again
            add(BondTag.DATE, "사용자 생일", "03-02"),
            add(BondTag.DATE, "no day"),
            BondChange(LifeChangeOp.UPDATE, BondTag.NAME, "선장", entry_id=mine.entry_id, expected_revision=1),
            BondChange(LifeChangeOp.RETIRE, entry_id=99, expected_revision=1),
        ], conversation=CONV, source="mpass_1", now=NOW)
        assert outcomes == ("applied", "duplicate", "applied", "invalid", "user_owned", "unknown")
        name, promise, birthday = await data.list_bond(ALICE, 1)
        assert name == mine
        assert (promise.origin, promise.source_conversation, promise.source_sequences, promise.source,
                promise.created_at) == (LifeOrigin.CONVERSATION, CONV, (1, 2), "mpass_1", NOW)
        assert (birthday.tag, birthday.month_day) == (BondTag.DATE, "03-02")

        later = NOW + timedelta(hours=1)
        outcomes = await data.apply_bond_changes(ALICE, 1, [
            BondChange(LifeChangeOp.UPDATE, BondTag.PROMISE, "토요일마다 영화 얘기 하기.", entry_id=promise.entry_id,
                       expected_revision=2),  # read at another revision
            BondChange(LifeChangeOp.UPDATE, BondTag.PROMISE, "토요일마다 영화 얘기 하기.", entry_id=promise.entry_id,
                       expected_revision=1, sequences=(5,)),
            BondChange(LifeChangeOp.RETIRE, entry_id=birthday.entry_id, expected_revision=1),
        ], conversation=CONV, source="mpass_2", now=later)
        assert outcomes == ("conflict", "applied", "applied")
        name, promise = await data.list_bond(ALICE, 1)
        assert (promise.text, promise.revision, promise.updated_at, promise.source_sequences) == (
            "토요일마다 영화 얘기 하기.", 2, later, (5,))
        assert await data.apply_bond_changes(ALICE, 1, ["not a change"], conversation=CONV,  # type: ignore[list-item]
                                             source="mpass_3", now=later) == ("invalid",)

    asyncio.run(scenario())


def test_past_the_limit_the_oldest_from_a_conversation_retires_and_the_users_stay() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        await data.apply_bond_changes(ALICE, 1, [add(BondTag.JOKE, "제일 오래된 농담")], conversation=CONV,
                                      source="mpass_1", now=NOW)
        for n in range(MAX_BOND_ENTRIES - 1):
            await data.add_bond(ALICE, 1, BondTag.JOKE, f"농담 {n}")
        assert await data.apply_bond_changes(ALICE, 1, [add(BondTag.JOKE, "새 농담")], conversation=CONV,
                                             source="mpass_2", now=NOW) == ("applied",)
        texts = [e.text for e in await data.list_bond(ALICE, 1)]
        assert len(texts) == MAX_BOND_ENTRIES and "제일 오래된 농담" not in texts and "새 농담" in texts
        mine = await data.add_bond(ALICE, 1, BondTag.JOKE, "내 농담")  # the conversation's last one gives way
        assert mine.origin is LifeOrigin.USER
        assert await data.apply_bond_changes(ALICE, 1, [add(BondTag.JOKE, "또 다른 농담")], conversation=CONV,
                                             source="mpass_3", now=NOW) == ("full",)
        with refused(DataErrorCode.LIMIT_EXCEEDED):
            await data.add_bond(ALICE, 1, BondTag.JOKE, "하나 더")

    asyncio.run(scenario())


def test_the_bond_counts_days_from_the_first_conversation_and_moves_up_by_what_happened() -> None:
    async def scenario() -> None:
        store = new_store()
        data = data_for(store)
        empty = await data.read_bond(ALICE, 1, now=NOW, zone="UTC")
        assert (empty.standing, empty.met_on, empty.together_days, empty.entries, empty.today) == (
            BondStanding(1), None, 0, (), ())
        store.create_conversation(SCOPE)
        met = datetime.now(UTC)
        first = await data.read_bond(ALICE, 1, now=met, zone="UTC")
        assert (first.met_on, first.together_days, first.standing.stage) == (met.date().isoformat(), 1, BondStage.NEW)

        # a name settled: familiar, because of it
        name = await data.add_bond(ALICE, 1, BondTag.NAME, "대장")
        seen = await data.read_bond(ALICE, 1, now=met + timedelta(days=1), zone="UTC")
        assert (seen.standing.stage, seen.standing.reason, seen.standing.since, seen.standing.revision) == (
            BondStage.FAMILIAR, f"b{name.entry_id}", name.created_at, 1)
        assert seen.together_days == 2 and seen.entries == (name,)
        assert await data.bond_stage(ALICE, 1) == seen.standing
        assert (await data.bond_stage(ALICE, 2)).stage is BondStage.NEW  # each persona its own

        # a promise is not enough before 90 days; on day 90 it is close
        await data.add_bond(ALICE, 1, BondTag.PROMISE, "주말마다 영화 얘기 하기.")
        day89 = met + timedelta(days=88)
        assert (await data.read_bond(ALICE, 1, now=day89, zone="UTC")).standing.stage is BondStage.FAMILIAR
        day90 = met + timedelta(days=89)
        close = await data.read_bond(ALICE, 1, now=day90, zone="UTC")
        assert (close.together_days, close.standing.stage, close.standing.reason, close.standing.since) == (
            90, BondStage.CLOSE, "90d", day90)
        data = data_for(restart(store))
        assert await data.bond_stage(ALICE, 1) == close.standing

        # the user takes it back to new (on day 1, as the clock says): what happened before does not move it
        # again, what happens after does
        with refused(DataErrorCode.VERSION_CONFLICT):
            await data.set_bond_stage(ALICE, 1, BondStage.NEW, expected_revision=1)
        chosen = await data.set_bond_stage(ALICE, 1, BondStage.NEW, expected_revision=close.standing.revision)
        assert (chosen.stage, chosen.reason, chosen.revision) == (BondStage.NEW, "user", close.standing.revision + 1)
        soon = datetime.now(UTC) + timedelta(minutes=1)
        assert (await data.read_bond(ALICE, 1, now=soon, zone="UTC")).standing == chosen
        joke = await data.add_bond(ALICE, 1, BondTag.JOKE, "감자 사건")
        again = await data.read_bond(ALICE, 1, now=soon, zone="UTC")
        assert (again.standing.stage, again.standing.reason) == (BondStage.FAMILIAR, f"b{joke.entry_id}")

    asyncio.run(scenario())


def test_thirty_days_together_make_it_familiar_and_a_purge_takes_what_its_conversation_settled() -> None:
    async def scenario() -> None:
        store = new_store()
        store.create_conversation(SCOPE)
        data = data_for(store)
        met = datetime.now(UTC)
        assert (await data.read_bond(ALICE, 1, now=met + timedelta(days=28), zone="UTC")).standing.stage \
            is BondStage.NEW
        familiar = (await data.read_bond(ALICE, 1, now=met + timedelta(days=29), zone="UTC")).standing
        assert (familiar.stage, familiar.reason) == (BondStage.FAMILIAR, "30d")

        await data.append_event(SCOPE, user("앞으로 대장이라고 불러"))
        await data.apply_bond_changes(ALICE, 1, [add(BondTag.NAME, "대장"), add(BondTag.JOKE, "감자 사건")],
                                      conversation=SCOPE.conversation_id, source="mpass_1", now=NOW)
        name, joke = await data.list_bond(ALICE, 1)
        mine = await data.edit_bond(ALICE, joke.entry_id, BondTag.JOKE, "감자 대사건", expected_revision=1)
        await data.trash_conversation(SCOPE)
        await data.begin_purge(SCOPE)
        await data.purge_conversation(SCOPE)
        (kept,) = await data.list_bond(ALICE, 1)  # the user's own stays, unlinked; the rest goes with its words
        assert (kept.entry_id, kept.text, kept.source_conversation) == (mine.entry_id, "감자 대사건", None)

    asyncio.run(scenario())


def test_the_days_together_and_the_days_to_mark() -> None:
    met = date(2026, 3, 1)
    assert together_days(met, met) == 1 and together_days(met, date(2026, 6, 8)) == 100
    assert anniversaries_on(date(2026, 6, 8), met, ()) == (Anniversary("100 days together", days=100),)
    assert anniversaries_on(date(2026, 6, 9), met, ()) == ()
    assert anniversaries_on(date(2027, 3, 1), met, ()) == (Anniversary("1 year together", years=1),)
    assert anniversaries_on(date(2028, 3, 1), met, ())[0].years == 2
    leap = date(2024, 2, 29)
    assert anniversaries_on(date(2025, 2, 28), leap, ())[0].years == 1  # kept on the 28th
    entry = LifeEntry(7, 1, LifeKind.BOND, None, "사용자 생일", LifeOrigin.USER, 1, datetime.now(UTC),
                      datetime.now(UTC), tag=BondTag.DATE, month_day="02-29")
    assert anniversaries_on(date(2027, 2, 28), None, (entry,)) == (Anniversary("사용자 생일", entry_id=7),)
    assert anniversaries_on(date(2028, 2, 28), None, (entry,)) == ()  # a leap year has its own day
    assert [a.key for a in anniversaries_on(date(2027, 3, 1), met, (replace(entry, month_day="03-01"),))] == [
        "y1", "b7"]


def test_an_anniversary_reason_is_stored_and_read_back() -> None:
    reasons = (WakeReason(WakeKind.ANNIVERSARY, days=100), WakeReason(WakeKind.ANNIVERSARY, years=2),
               WakeReason(WakeKind.ANNIVERSARY, entry_id=7))
    assert codec.wake_reasons(reasons) == [{"kind": "anniversary", "days": 100}, {"kind": "anniversary", "years": 2},
                                           {"kind": "anniversary", "entry_id": 7}]
    assert codec.wake_reasons_from(codec.wake_reasons(reasons)) == reasons


def bond(entry_id: int, tag: BondTag, at: datetime) -> LifeEntry:
    return LifeEntry(entry_id, 1, LifeKind.BOND, None, tag.value, LifeOrigin.CONVERSATION, 1, at, at, tag=tag)


def test_the_stage_rules_never_move_it_down_and_after_the_users_choice_count_only_what_comes_after() -> None:
    zone = rules.check_zone("Asia/Seoul")
    met = date(2026, 1, 1)
    close = BondStanding(1, BondStage.CLOSE, NOW, "90d", 3)
    assert rules.settle_stage(close, (), met, date(2026, 11, 25), zone, NOW) is close
    new = BondStanding(1)
    assert rules.settle_stage(new, (), None, date(2026, 11, 25), zone, NOW) is new
    with pytest.raises(DataError):
        rules.check_zone("Not/AZone")

    # all at once: a name, a first and 100 days — familiar then close, by the first made after day 90
    day100 = datetime(2026, 4, 10, 3, 0, tzinfo=UTC)
    name, first = bond(1, BondTag.NAME, datetime(2026, 1, 5, tzinfo=UTC)), bond(2, BondTag.FIRST, day100)
    moved = rules.settle_stage(new, (name, first), met, date(2026, 4, 10), zone, day100)
    assert (moved.stage, moved.reason, moved.since, moved.revision) == (BondStage.CLOSE, "b2", day100, 1)

    # the user chose new on day 45: the old name and the 30 days do not count, nor the 90 days with an old first
    chosen = BondStanding(1, BondStage.NEW, datetime(2026, 2, 14, tzinfo=UTC), "user", 4)
    old_first = bond(3, BondTag.FIRST, datetime(2026, 1, 20, tzinfo=UTC))
    assert rules.settle_stage(chosen, (name, old_first), met, date(2026, 4, 10), zone, day100) is chosen
    # chosen familiar on day 45: day 90 comes after it, so with the old first it becomes close then
    familiar = replace(chosen, stage=BondStage.FAMILIAR)
    assert rules.settle_stage(familiar, (old_first,), met, date(2026, 3, 30), zone, day100) is familiar  # day 89
    day90 = rules.settle_stage(familiar, (old_first,), met, date(2026, 3, 31), zone, day100)
    assert (day90.stage, day90.reason) == (BondStage.CLOSE, "90d")
    assert TrustedScope(ALICE, CONV).principal_id == ALICE
