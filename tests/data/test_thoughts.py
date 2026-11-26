"""HIMARI's kept thoughts (character-mind v0.1 §5.1, §5.2): a memory pass keeps what HIMARI left unsaid as a life
entry of kind ``thought`` (what it is about, when it expires) and retires the ones it has now said; they are listed
until they expire, at most MAX_THOUGHTS a persona; the user only deletes them; a purge takes the ones its conversation
kept. On every backend."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from testkit import SCOPE, data_for, new_store, restart, user

from himari.data.api import (
    INTENT_DAYS,
    MAX_THOUGHT_CHARS,
    MAX_THOUGHTS,
    THOUGHT_DAYS,
    DataError,
    DataErrorCode,
    LifeKind,
    LifeOrigin,
    ThoughtAbout,
    ThoughtChange,
    ThoughtOp,
)

ALICE = SCOPE.principal_id
CONV = SCOPE.conversation_id
NOW = datetime(2026, 11, 25, 13, 0, tzinfo=UTC)


def keep(text: str, about: ThoughtAbout = ThoughtAbout.USER, *seqs: int) -> ThoughtChange:
    return ThoughtChange(ThoughtOp.KEEP, text, about, sequences=seqs or (2,))


def refused(code: DataErrorCode) -> pytest.RaisesExc[DataError]:
    return pytest.raises(DataError, check=lambda e: e.code is code)


def test_a_kept_thought_is_listed_until_it_expires_and_a_said_one_retires() -> None:
    async def scenario() -> None:
        store = new_store()
        data = data_for(store)
        outcomes = await data.apply_thought_changes(ALICE, 1, [
            keep("요즘 지쳐 보인다. 잔소리는 참았다.", ThoughtAbout.USER, 2, 4),
            keep("다음엔 스트레칭 얘기 해야지.", ThoughtAbout.INTENT),
            keep("요즘 지쳐 보인다. 잔소리는 참았다."),  # the same text again
            ThoughtChange(ThoughtOp.SAID, entry_id=99),
            keep("x" * (MAX_THOUGHT_CHARS + 1)),
            ThoughtChange(ThoughtOp.KEEP, "about nothing"),
        ], conversation=CONV, source="mpass_1", now=NOW)
        assert outcomes == ("applied", "applied", "duplicate", "unknown", "invalid", "invalid")
        data = data_for(restart(store))
        intent, tired = await data.list_life(ALICE, 1, now=NOW + timedelta(hours=1))  # newest first
        assert (tired.kind, tired.about, tired.origin, tired.source_conversation, tired.source_sequences,
                tired.source, tired.day) == (LifeKind.THOUGHT, ThoughtAbout.USER, LifeOrigin.CONVERSATION, CONV,
                                             (2, 4), "mpass_1", None)
        assert tired.expires_at == NOW + THOUGHT_DAYS and intent.expires_at == NOW + INTENT_DAYS
        assert await data.list_life(ALICE, 2, now=NOW) == ()  # another persona's are its own
        later = NOW + INTENT_DAYS + timedelta(minutes=1)
        assert await data.list_life(ALICE, 1, now=later) == (tired,)  # the intent expired
        assert await data.list_life(ALICE, 1, since="2026-11-30", now=later) == (tired,)  # whatever the date
        assert await data.list_life(ALICE, 1, now=NOW + THOUGHT_DAYS) == ()
        assert await data.apply_thought_changes(ALICE, 1, [ThoughtChange(ThoughtOp.SAID, entry_id=tired.entry_id),
                                                           ThoughtChange(ThoughtOp.SAID, entry_id=tired.entry_id)],
                                                conversation=CONV, source="mpass_2", now=later) == (
            "applied", "unknown")
        assert await data.list_life(ALICE, 1, now=later) == ()

    asyncio.run(scenario())


def test_past_the_limit_the_oldest_kept_thought_retires() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        for n in range(MAX_THOUGHTS + 1):
            await data.apply_thought_changes(ALICE, 1, [keep(f"생각 {n}")], conversation=CONV, source=f"mpass_{n}",
                                             now=NOW + timedelta(minutes=n))
        listed = await data.list_life(ALICE, 1, now=NOW + timedelta(hours=1))
        assert len(listed) == MAX_THOUGHTS and listed[0].text == f"생각 {MAX_THOUGHTS}"
        assert "생각 0" not in {e.text for e in listed}

    asyncio.run(scenario())


def test_the_user_deletes_a_kept_thought_but_never_rewrites_it() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        await data.apply_thought_changes(ALICE, 1, [keep("서운했지만 티 내지 않았다.", ThoughtAbout.SELF)],
                                         conversation=CONV, source="mpass_1", now=datetime.now(UTC))
        (thought,) = await data.list_life(ALICE, 1)
        with refused(DataErrorCode.INVALID_ARGUMENT):
            await data.edit_life(ALICE, thought.entry_id, "고쳐 봄", expected_revision=thought.revision)
        await data.retire_life(ALICE, thought.entry_id, expected_revision=thought.revision)
        assert await data.list_life(ALICE, 1) == ()

    asyncio.run(scenario())


def test_a_purge_takes_the_thoughts_its_conversation_kept() -> None:
    async def scenario() -> None:
        store = new_store()
        store.create_conversation(SCOPE)
        data = data_for(store)
        await data.append_event(SCOPE, user("오늘 좀 피곤해"))
        now = datetime.now(UTC)
        await data.apply_thought_changes(ALICE, 1, [keep("피곤해 보였다.")], conversation=CONV, source="mpass_1",
                                         now=now)
        fact = await data.add_life_fact(ALICE, 1, "고양이를 무서워한다.")
        await data.trash_conversation(SCOPE)
        await data.begin_purge(SCOPE)
        await data.purge_conversation(SCOPE)
        assert await data.list_life(ALICE, 1) == (fact,)

    asyncio.run(scenario())
