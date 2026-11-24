"""LifeStore and LifeWriteLog (character-life v0.1 §2, §3) contract, on every backend."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from testkit import SCOPE, data_for, new_store, restart, user

from himari.common import ConversationId, PrincipalId, TokenUsage, TrustedScope
from himari.data import rules
from himari.data.api import (
    MAX_DIARY_CHARS,
    MAX_LIFE_CHARS,
    NO_PERSONA,
    DataError,
    DataErrorCode,
    LifeChange,
    LifeChangeOp,
    LifeKind,
    LifeOrigin,
    LifePlan,
    LifeWriteStart,
    LifeWriteStatus,
    RawNoResponse,
    RawReceived,
    RawUncertain,
)
from himari.data.memory import InMemoryData
from himari.data.sqlite import SqliteData

ALICE, BOB = PrincipalId("alice"), PrincipalId("bob")
NOW = datetime(2026, 11, 25, 13, 0, tzinfo=UTC)
CONV = ConversationId("conv_1")


def refused(code: DataErrorCode) -> pytest.RaisesExc[DataError]:
    return pytest.raises(DataError, check=lambda e: e.code is code)


def start(persona_id: int = 1, day: str = "2026-11-25", reserve: int = 5_000) -> LifeWriteStart:
    return LifeWriteStart(persona_id, day, "openrouter", "m", b'{"messages": []}', "application/json", reserve)


async def received(data: InMemoryData | SqliteData, principal: PrincipalId = ALICE, persona_id: int = 1,
                   day: str = "2026-11-25", usage: TokenUsage | None = TokenUsage(1_000, 200)) -> str:
    """A write whose answer is stored, ready to apply."""
    write_id = await data.begin_life_write(principal, start(persona_id, day))
    await data.mark_life_write_dispatched(write_id)
    await data.record_life_write(write_id, RawReceived(b"{}", "application/json", usage))
    return write_id


def test_the_users_facts_are_kept_per_persona_edited_and_retired() -> None:
    async def scenario() -> None:
        store = new_store()
        data = data_for(store)
        assert await data.list_life(ALICE, 1) == ()
        mine = await data.add_life_fact(ALICE, 1, "  고양이를\r\n무서워한다. ")
        assert (mine.entry_id, mine.kind, mine.text, mine.origin, mine.revision, mine.day) == (
            1, LifeKind.FACT, "고양이를\n무서워한다.", LifeOrigin.USER, 1, None)
        other = await data.add_life_fact(ALICE, NO_PERSONA, "비 오는 날을 좋아한다.")  # L2: no persona's own life
        assert other.entry_id == 2  # ids per principal, across personas
        assert await data.list_life(ALICE, 1) == (mine,)
        assert await data.list_life(ALICE, NO_PERSONA) == (other,)
        assert await data.list_life(BOB, 1) == ()
        edited = await data.edit_life(ALICE, 1, "고양이를 조금 무서워한다.", expected_revision=1)
        assert (edited.revision, edited.origin, edited.created_at) == (2, LifeOrigin.USER, mine.created_at)
        with refused(DataErrorCode.VERSION_CONFLICT):
            await data.edit_life(ALICE, 1, "lost", expected_revision=1)
        with refused(DataErrorCode.NOT_FOUND):
            await data.edit_life(BOB, 1, "not bob's", expected_revision=2)

        data = data_for(restart(store))
        assert await data.list_life(ALICE, 1) == (edited,)
        await data.retire_life(ALICE, 1, expected_revision=2)
        assert await data.list_life(ALICE, 1) == ()
        with refused(DataErrorCode.NOT_FOUND):  # retired: gone for the user too
            await data.retire_life(ALICE, 1, expected_revision=3)
        assert (await data.add_life_fact(ALICE, 1, "새 사실.")).entry_id == 3  # never given out again

    asyncio.run(scenario())


def test_a_life_entrys_text_is_checked() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        with refused(DataErrorCode.INVALID_ARGUMENT):
            await data.add_life_fact(ALICE, 1, " \n ")
        with refused(DataErrorCode.INVALID_ARGUMENT):
            await data.add_life_fact(ALICE, 1, "a\x00b")
        with refused(DataErrorCode.TOO_LARGE):
            await data.add_life_fact(ALICE, 1, "x" * (MAX_LIFE_CHARS + 1))
        with refused(DataErrorCode.INVALID_ARGUMENT):
            await data.add_life_fact(ALICE, -1, "no such persona")
        assert (await data.add_life_fact(ALICE, 1, "x" * MAX_LIFE_CHARS)).text == "x" * MAX_LIFE_CHARS

    asyncio.run(scenario())


def test_a_write_is_recorded_charged_and_applied_as_one_day() -> None:
    async def scenario() -> None:
        store = new_store()
        data = data_for(store)
        day_start = datetime.now(UTC) - timedelta(minutes=1)
        write_id = await data.begin_life_write(ALICE, start(reserve=5_000))
        assert await data.life_usage(ALICE, day_start) == 5_000  # its reservation until it reports
        await data.mark_life_write_dispatched(write_id)
        with refused(DataErrorCode.INVALID_STATE_TRANSITION):
            await data.mark_life_write_dispatched(write_id)
        with refused(DataErrorCode.INVALID_STATE_TRANSITION):  # nothing applies before the answer is stored
            await data.apply_life_write(write_id, LifePlan("오늘은 그림을 그렸다."), now=NOW)
        await data.record_life_write(write_id, RawReceived(b"{}", "application/json", TokenUsage(1_000, 200)))
        assert await data.life_usage(ALICE, day_start) == 1_200
        assert await data.life_attempts(ALICE, 1, "2026-11-25") == 1
        assert await data.life_attempts(ALICE, 2, "2026-11-25") == 0
        written = await data.apply_life_write(write_id, LifePlan(
            "오늘은 그림을 그렸다. 저녁엔 차를 마셨다.", (("2026-11-26", "전시를 보러 간다."),),
            ("수채화를 배우기 시작했다.",)), now=NOW)
        assert [(e.kind, e.day, e.origin, e.source) for e in written] == [
            (LifeKind.DIARY, "2026-11-25", LifeOrigin.WRITER, write_id),
            (LifeKind.PLAN, "2026-11-26", LifeOrigin.WRITER, write_id),
            (LifeKind.FACT, None, LifeOrigin.WRITER, write_id)]
        with refused(DataErrorCode.INVALID_STATE_TRANSITION):  # once
            await data.apply_life_write(write_id, LifePlan("again"), now=NOW)

        data = data_for(restart(store))
        (record,) = await data.list_life_writes(ALICE)
        assert (record.status, record.input_tokens, record.output_tokens, record.charged_tokens, record.day) == (
            LifeWriteStatus.APPLIED, 1_000, 200, 1_200, "2026-11-25")
        # the same date again: the writer's diary is rewritten, a fact it holds is not added twice
        again = await received(data)
        rewritten = await data.apply_life_write(again, LifePlan("다시 쓴 일기.", (), ("수채화를 배우기 시작했다.",)),
                                                now=NOW)
        assert [(e.entry_id, e.text, e.revision) for e in rewritten] == [(1, "다시 쓴 일기.", 2)]
        listed = await data.list_life(ALICE, 1)
        assert [(e.kind, e.day) for e in listed] == [
            (LifeKind.FACT, None), (LifeKind.PLAN, "2026-11-26"), (LifeKind.DIARY, "2026-11-25")]
        assert [e.kind for e in await data.list_life(ALICE, 1, since="2026-11-26")] == [LifeKind.FACT, LifeKind.PLAN]
        # the user's diary of that date stays as the user wrote it
        await data.edit_life(ALICE, 1, "내가 고친 일기.", expected_revision=2)
        third = await received(data)
        assert await data.apply_life_write(third, LifePlan("작가의 일기."), now=NOW) == ()
        assert [e.text for e in await data.list_life(ALICE, 1) if e.kind is LifeKind.DIARY] == ["내가 고친 일기."]

    asyncio.run(scenario())


def test_a_write_without_an_answer_or_cut_short_is_recorded_as_such() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        since = datetime.now(UTC) - timedelta(minutes=1)
        never_sent = await data.begin_life_write(ALICE, start(reserve=100))
        sent = await data.begin_life_write(ALICE, start(reserve=200))
        await data.mark_life_write_dispatched(sent)
        failed = await data.begin_life_write(ALICE, start(reserve=400))
        await data.mark_life_write_dispatched(failed)
        await data.record_life_write(failed, RawNoResponse("http_500", b"{}", "application/json"))
        unknown = await data.begin_life_write(ALICE, start(reserve=800))
        await data.mark_life_write_dispatched(unknown)
        await data.record_life_write(unknown, RawUncertain("timeout"))
        malformed = await received(data, usage=None)  # charged at its reservation: no usage reported
        await data.fail_life_write(malformed, "malformed answer: x")
        assert await data.interrupt_life_writes() == 2  # the one never sent and the one in flight
        by_id = {w.write_id: w for w in await data.list_life_writes(ALICE)}
        assert [(by_id[w].status, by_id[w].charged_tokens) for w in (never_sent, sent, failed, unknown, malformed)] == [
            (LifeWriteStatus.INTERRUPTED, 0), (LifeWriteStatus.INTERRUPTED, 200), (LifeWriteStatus.FAILED, 400),
            (LifeWriteStatus.FAILED, 800), (LifeWriteStatus.FAILED, 5_000)]
        assert by_id[failed].error == "http_500" and by_id[malformed].error == "malformed answer: x"
        assert await data.life_usage(ALICE, since) == 6_400
        assert await data.life_attempts(ALICE, 1, "2026-11-25") == 5
        assert [w.write_id for w in await data.list_life_writes(ALICE, limit=2)] == [malformed, unknown]

    asyncio.run(scenario())


def test_facts_past_the_limit_retire_the_writers_oldest_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rules, "MAX_LIFE_FACTS", 3)

    async def scenario() -> None:
        data = data_for(new_store())
        write_id = await received(data)
        await data.apply_life_write(write_id, LifePlan("일기.", (), ("작가의 사실.",)), now=NOW)
        said = await data.apply_life_changes(ALICE, 1, [LifeChange(LifeChangeOp.ADD, "대화의 사실.", sequences=(2,))],
                                             conversation=CONV, source="mpass_1", now=NOW)
        assert said == ("applied",)
        await data.add_life_fact(ALICE, 1, "사용자의 사실 1.")
        await data.add_life_fact(ALICE, 1, "사용자의 사실 2.")  # the writer's goes first
        assert sorted(e.text for e in await data.list_life(ALICE, 1) if e.kind is LifeKind.FACT) == [
            "대화의 사실.", "사용자의 사실 1.", "사용자의 사실 2."]
        await data.add_life_fact(ALICE, 1, "사용자의 사실 3.")  # then the conversation's
        with refused(DataErrorCode.LIMIT_EXCEEDED):  # all the user's
            await data.add_life_fact(ALICE, 1, "사용자의 사실 4.")
        assert await data.apply_life_changes(ALICE, 1, [LifeChange(LifeChangeOp.ADD, "또 다른 사실.", sequences=(2,))],
                                             conversation=CONV, source="mpass_2", now=NOW) == ("full",)
        assert len(await data.list_life(ALICE, 2)) == 0  # another persona's room is its own
        await data.add_life_fact(ALICE, 2, "다른 페르소나의 사실.")

    asyncio.run(scenario())


def test_a_memory_pass_changes_only_the_personas_facts_not_the_users() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        users = await data.add_life_fact(ALICE, 1, "사용자가 쓴 사실.")
        (added,) = await data.apply_life_changes(ALICE, 1, [
            LifeChange(LifeChangeOp.ADD, "고양이를 무서워한다.", sequences=(2, 4))],
            conversation=CONV, source="mpass_1", now=NOW)
        assert added == "applied"
        cat = next(e for e in await data.list_life(ALICE, 1) if e.origin is LifeOrigin.CONVERSATION)
        assert (cat.source_conversation, cat.source_sequences, cat.source) == (CONV, (2, 4), "mpass_1")
        outcomes = await data.apply_life_changes(ALICE, 1, [
            LifeChange(LifeChangeOp.ADD, "고양이를 무서워한다.", sequences=(6,)),  # the same as a live fact
            LifeChange(LifeChangeOp.UPDATE, "x", users.entry_id, users.revision, (6,)),
            LifeChange(LifeChangeOp.RETIRE, None, cat.entry_id, cat.revision + 5, (6,)),
            LifeChange(LifeChangeOp.UPDATE, "x", 99, 1, (6,)),
            LifeChange(LifeChangeOp.ADD, " ", sequences=(6,)),
            LifeChange(LifeChangeOp.UPDATE, "사용자가 쓴 사실.", cat.entry_id, cat.revision, (6,)),
            LifeChange(LifeChangeOp.UPDATE, "이제 고양이가 좋다.", cat.entry_id, cat.revision, (8,)),
        ], conversation=CONV, source="mpass_2", now=NOW)
        assert outcomes == ("duplicate", "user_owned", "conflict", "unknown", "invalid", "duplicate", "applied")
        changed = next(e for e in await data.list_life(ALICE, 1) if e.entry_id == cat.entry_id)
        assert (changed.text, changed.revision, changed.source, changed.source_sequences) == (
            "이제 고양이가 좋다.", 2, "mpass_2", (8,))
        assert await data.apply_life_changes(ALICE, 2, [LifeChange(LifeChangeOp.RETIRE, None, cat.entry_id, 2)],
                                             conversation=CONV, source="mpass_3", now=NOW) == ("unknown",)
        assert await data.apply_life_changes(ALICE, 1, [LifeChange(LifeChangeOp.RETIRE, None, cat.entry_id, 2)],
                                             conversation=CONV, source="mpass_3", now=NOW) == ("applied",)
        assert [e.entry_id for e in await data.list_life(ALICE, 1)] == [users.entry_id]

    asyncio.run(scenario())


def test_the_principals_with_a_recent_message_and_a_purge_keeps_the_facts_unlinked() -> None:
    async def scenario() -> None:
        store = new_store()
        bob = TrustedScope(BOB, ConversationId("conv_bob"))
        quiet = TrustedScope(PrincipalId("carol"), ConversationId("conv_carol"))
        for scope in (SCOPE, bob, quiet):
            store.create_conversation(scope)
        data = data_for(store)
        await data.append_event(SCOPE, user("안녕"))
        await data.append_event(bob, user("hi"))
        since = datetime.now(UTC) - timedelta(minutes=1)
        assert await data.life_principals(since) == (ALICE, BOB)  # carol sent nothing
        assert await data.life_principals(datetime.now(UTC) + timedelta(minutes=1)) == ()
        await data.trash_conversation(bob)
        assert await data.life_principals(since) == (ALICE,)

        await data.apply_life_changes(ALICE, 1, [LifeChange(LifeChangeOp.ADD, "바다를 좋아한다.", sequences=(2,))],
                                      conversation=SCOPE.conversation_id, source="mpass_1", now=NOW)
        await data.trash_conversation(SCOPE)
        await data.begin_purge(SCOPE)
        await data.purge_conversation(SCOPE)
        (kept,) = await data.list_life(ALICE, 1)
        assert (kept.text, kept.source_conversation, kept.source_sequences, kept.source) == ("바다를 좋아한다.", None,
                                                                                             (), None)

    asyncio.run(scenario())


def test_a_diary_may_be_longer_and_a_plans_date_is_checked() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        write_id = await received(data)
        with refused(DataErrorCode.INVALID_ARGUMENT):
            await data.apply_life_write(write_id, LifePlan("일기.", (("26-11-2026", "x"),)), now=NOW)
        with refused(DataErrorCode.TOO_LARGE):
            await data.apply_life_write(write_id, LifePlan("x" * (MAX_DIARY_CHARS + 1)), now=NOW)
        (diary,) = await data.apply_life_write(write_id, LifePlan("x" * MAX_DIARY_CHARS), now=NOW)
        assert len(diary.text) == MAX_DIARY_CHARS
        with refused(DataErrorCode.TOO_LARGE):  # the user's rewrite is held to a diary's limit too
            await data.edit_life(ALICE, diary.entry_id, "y" * (MAX_DIARY_CHARS + 1), expected_revision=1)
        assert (await data.edit_life(ALICE, diary.entry_id, "y" * MAX_LIFE_CHARS * 2, expected_revision=1)).revision == 2

    asyncio.run(scenario())
