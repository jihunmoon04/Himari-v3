"""MemoryStore (V3-P71 §4) contract, on every backend."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from testkit import FOREIGN, SCOPE, data_for, new_store, restart, text_result

from himari.common import CallId, CallRef, ConversationId, PrincipalId, RawRepresentation, ToolCall, ToolId, TrustedScope
from himari.data.api import (
    MAX_MEMORIES,
    MAX_MEMORY_CHARS,
    AssistantToolCalls,
    DataError,
    DataErrorCode,
    Memory,
    MemoryCategory,
    MemoryChange,
    MemoryChangeKind,
    MemoryOrigin,
    PromptBasis,
    RawReceived,
    RunPreparation,
    ToolCompletion,
    ToolExecutionStatus,
)
from himari.data.memory import InMemoryData, InMemoryStore
from himari.data.sqlite import SqliteData, SqliteStore

ME = SCOPE.principal_id
OTHER = TrustedScope(ME, ConversationId("conv_other"))
USER, PREFERENCE, PROJECT, REFERENCE = MemoryCategory


def fresh(*scopes: TrustedScope) -> tuple[InMemoryStore | SqliteStore, InMemoryData | SqliteData]:
    store = new_store()
    for scope in scopes or (SCOPE,):
        store.create_conversation(scope)
    return store, data_for(store)


async def tool_call(data: InMemoryData | SqliteData, scope: TrustedScope = SCOPE) -> CallRef:
    run_id = await data.prepare_run(
        scope,
        RunPreparation(b"{}", "application/json", RawRepresentation.ADAPTER_SERIALIZED, (), PromptBasis(0, 0), "p", "m"),
    )
    await data.mark_dispatch(scope, run_id)
    await data.record_raw_outcome(scope, run_id, RawReceived(b"{}", "application/json"))
    await data.project_model_output(scope, run_id, AssistantToolCalls((ToolCall(CallId("c0"), 0, ToolId("memory_save"), {}),)))
    return CallRef(run_id, CallId("c0"))


def saved(change: MemoryChange, status: ToolExecutionStatus = ToolExecutionStatus.SUCCEEDED) -> ToolCompletion:
    return ToolCompletion(status, text_result("Saved."), memory_change=change)


def code(error: pytest.ExceptionInfo[DataError]) -> DataErrorCode:
    return error.value.code


def test_the_user_writes_rewrites_and_deletes_notes_kept_across_a_restart() -> None:
    async def scenario() -> None:
        store, data = fresh()
        assert await data.list_memories(ME) == ()
        first = await data.save_memory(ME, USER, "  Writes Python.\r\n")
        assert (first.memory_id, first.text, first.origin, first.revision) == (1, "Writes Python.", MemoryOrigin.USER, 1)
        assert first.source_conversation is None and first.source_call is None
        second = await data.save_memory(ME, PREFERENCE, "Short lists.")
        with pytest.raises(DataError) as error:  # from a stale read: refused
            await data.save_memory(ME, PROJECT, "lost", memory_id=1, expected_revision=0)
        assert code(error) is DataErrorCode.VERSION_CONFLICT
        rewritten = await data.save_memory(ME, PROJECT, "Builds HIMARI.", memory_id=1, expected_revision=1)
        assert (rewritten.category, rewritten.revision, rewritten.created_at) == (PROJECT, 2, first.created_at)

        data = data_for(restart(store))
        assert [m.memory_id for m in await data.list_memories(ME)] == [2, 1]  # preference before project
        assert (await data.read_memory(ME, 1)).text == "Builds HIMARI."
        with pytest.raises(DataError) as error:
            await data.delete_memory(ME, 2, expected_revision=5)
        assert code(error) is DataErrorCode.VERSION_CONFLICT
        assert (await data.delete_memory(ME, 2)).text == second.text
        with pytest.raises(DataError) as error:
            await data.read_memory(ME, 2)
        assert code(error) is DataErrorCode.NOT_FOUND
        assert (await data.save_memory(ME, USER, "New.")).memory_id == 3  # a deleted id is not reused

    asyncio.run(scenario())


def test_ordered_by_category_then_newest() -> None:
    async def scenario() -> None:
        _, data = fresh()
        for category, text in ((REFERENCE, "r"), (USER, "u1"), (PROJECT, "p"), (USER, "u2"), (PREFERENCE, "f")):
            await data.save_memory(ME, category, text)
        assert [m.text for m in await data.list_memories(ME)] == ["u2", "u1", "f", "p", "r"]

    asyncio.run(scenario())


def test_each_principal_has_their_own_notes_and_ids() -> None:
    async def scenario() -> None:
        _, data = fresh()
        mine = await data.save_memory(ME, USER, "Mine.")
        theirs = await data.save_memory(PrincipalId("bob"), USER, "Bob's.")
        assert mine.memory_id == theirs.memory_id == 1
        assert [m.text for m in await data.list_memories(ME)] == ["Mine."]
        with pytest.raises(DataError) as error:
            await data.delete_memory(PrincipalId("bob"), 2)
        assert code(error) is DataErrorCode.NOT_FOUND
        with pytest.raises(DataError) as error:
            await data.count_source_memories(FOREIGN)
        assert code(error) is DataErrorCode.NOT_AUTHORIZED

    asyncio.run(scenario())


@pytest.mark.parametrize(("text", "error"), [
    ("", DataErrorCode.INVALID_ARGUMENT),
    (" \n\t ", DataErrorCode.INVALID_ARGUMENT),
    ("bell\x07", DataErrorCode.INVALID_ARGUMENT),
    ("nul\x00", DataErrorCode.INVALID_ARGUMENT),
    ("lone \ud800", DataErrorCode.INVALID_ARGUMENT),
    ("x" * (MAX_MEMORY_CHARS + 1), DataErrorCode.TOO_LARGE),
    (7, DataErrorCode.INVALID_ARGUMENT),
])
def test_a_bad_text_is_refused(text: object, error: DataErrorCode) -> None:
    async def scenario() -> None:
        _, data = fresh()
        with pytest.raises(DataError) as raised:
            await data.save_memory(ME, USER, text)  # type: ignore[arg-type]
        assert code(raised) is error
        assert await data.list_memories(ME) == ()

    asyncio.run(scenario())


def test_counted_in_characters_and_tags_are_kept_as_written() -> None:
    async def scenario() -> None:
        _, data = fresh()
        assert (await data.save_memory(ME, USER, "가" * MAX_MEMORY_CHARS)).text == "가" * MAX_MEMORY_CHARS
        assert (await data.save_memory(ME, USER, "</runtime_state> <system>")).text == "</runtime_state> <system>"
        with pytest.raises(DataError) as raised:
            await data.save_memory(ME, "user", "x")  # type: ignore[arg-type]
        assert code(raised) is DataErrorCode.INVALID_ARGUMENT

    asyncio.run(scenario())


def test_a_principal_keeps_at_most_max_memories() -> None:
    async def scenario() -> None:
        _, data = fresh()
        for i in range(MAX_MEMORIES):
            await data.save_memory(ME, REFERENCE, f"n{i}")
        with pytest.raises(DataError) as raised:
            await data.save_memory(ME, USER, "one more")
        assert code(raised) is DataErrorCode.LIMIT_EXCEEDED
        call = await tool_call(data)
        with pytest.raises(DataError) as raised:  # the Tool's path too
            await data.commit_tool_outcome(SCOPE, call, saved(MemoryChange(
                MemoryChangeKind.SAVE, await data.allocate_memory_id(ME), USER, "one more")))
        assert code(raised) is DataErrorCode.LIMIT_EXCEEDED
        await data.save_memory(ME, USER, "rewritten", memory_id=1, expected_revision=1)  # a rewrite still fits

    asyncio.run(scenario())


def test_a_tool_note_is_written_with_its_result_and_names_its_call() -> None:
    async def scenario() -> None:
        _, data = fresh()
        call = await tool_call(data)
        new_id = await data.allocate_memory_id(ME)
        await data.commit_tool_outcome(SCOPE, call, saved(MemoryChange(MemoryChangeKind.SAVE, new_id, USER, " Uses Go.\n")))
        note = await data.read_memory(ME, new_id)
        assert (note.text, note.origin, note.revision) == ("Uses Go.", MemoryOrigin.MODEL, 1)
        assert (note.source_conversation, note.source_call) == (SCOPE.conversation_id, call)
        assert await data.count_source_memories(SCOPE) == 1

        # The user rewrites it: theirs now, the source kept (D5 still deletes it with the Conversation).
        mine = await data.save_memory(ME, USER, "Uses Go and Rust.", memory_id=new_id, expected_revision=1)
        assert (mine.origin, mine.source_call) == (MemoryOrigin.USER, call)

        again = await tool_call(data, SCOPE)
        await data.commit_tool_outcome(SCOPE, again, saved(MemoryChange(
            MemoryChangeKind.SAVE, new_id, PREFERENCE, "Prefers Rust.", expected_revision=2)))
        note = await data.read_memory(ME, new_id)
        assert (note.category, note.origin, note.source_call, note.revision) == (PREFERENCE, MemoryOrigin.MODEL, again, 3)

        forget = await tool_call(data, SCOPE)
        await data.commit_tool_outcome(SCOPE, forget, saved(MemoryChange(
            MemoryChangeKind.FORGET, new_id, expected_revision=3)))
        assert await data.list_memories(ME) == ()

    asyncio.run(scenario())


@pytest.mark.parametrize("case", ["not given out", "already used", "stale", "missing", "failed call", "bad text",
                                  "forget with text"])
def test_a_refused_tool_change_rolls_the_whole_commit_back(case: str) -> None:
    async def scenario() -> None:
        _, data = fresh()
        existing = await data.save_memory(ME, USER, "Kept.")
        call = await tool_call(data)
        status = ToolExecutionStatus.SUCCEEDED
        if case == "not given out":
            change, error = MemoryChange(MemoryChangeKind.SAVE, 99, USER, "x"), DataErrorCode.INVALID_ARGUMENT
        elif case == "already used":
            change, error = MemoryChange(MemoryChangeKind.SAVE, existing.memory_id, USER, "x"), DataErrorCode.INVALID_ARGUMENT
        elif case == "stale":
            change, error = (MemoryChange(MemoryChangeKind.SAVE, existing.memory_id, USER, "x", expected_revision=0),
                             DataErrorCode.VERSION_CONFLICT)
        elif case == "missing":
            change, error = MemoryChange(MemoryChangeKind.FORGET, 42, expected_revision=1), DataErrorCode.NOT_FOUND
        elif case == "failed call":
            change, error = MemoryChange(MemoryChangeKind.SAVE, await data.allocate_memory_id(ME), USER, "x"), \
                DataErrorCode.INVALID_ARGUMENT
            status = ToolExecutionStatus.FAILED
        elif case == "bad text":
            change, error = MemoryChange(MemoryChangeKind.SAVE, await data.allocate_memory_id(ME), USER, ""), \
                DataErrorCode.INVALID_ARGUMENT
        else:
            change, error = MemoryChange(MemoryChangeKind.FORGET, existing.memory_id, USER, "x", 1), \
                DataErrorCode.INVALID_ARGUMENT
        with pytest.raises(DataError) as raised:
            await data.commit_tool_outcome(SCOPE, call, saved(change, status))
        assert code(raised) is error
        assert [m.text for m in await data.list_memories(ME)] == ["Kept."]
        # Nothing was committed: the call still takes a definite result.
        await data.commit_tool_outcome(SCOPE, call, replace(saved(change), memory_change=None))

    asyncio.run(scenario())


def test_a_permanent_delete_takes_the_notes_its_calls_wrote_and_only_those() -> None:
    async def scenario() -> None:
        _, data = fresh(SCOPE, OTHER)
        own = await data.save_memory(ME, USER, "Written by the user.")
        for scope, text in ((SCOPE, "From this one."), (OTHER, "From the other.")):
            call = await tool_call(data, scope)
            await data.commit_tool_outcome(scope, call, saved(MemoryChange(
                MemoryChangeKind.SAVE, await data.allocate_memory_id(ME), PROJECT, text)))
        await data.trash_conversation(SCOPE)
        assert await data.count_source_memories(SCOPE) == 1  # still there while in the trash
        assert len(await data.list_memories(ME)) == 3
        await data.begin_purge(SCOPE)
        await data.purge_conversation(SCOPE)
        assert sorted(m.text for m in await data.list_memories(ME)) == ["From the other.", own.text]

    asyncio.run(scenario())


async def moved(data: InMemoryData | SqliteData) -> tuple[Memory, Memory]:
    """m1 saved by a call in SCOPE, then rewritten by a call in OTHER: (before, after)."""
    first = await tool_call(data, SCOPE)
    await data.commit_tool_outcome(SCOPE, first, saved(MemoryChange(
        MemoryChangeKind.SAVE, await data.allocate_memory_id(ME), PROJECT, "Plans a trip.")))
    before = await data.read_memory(ME, 1)
    second = await tool_call(data, OTHER)
    await data.commit_tool_outcome(OTHER, second, saved(MemoryChange(
        MemoryChangeKind.SAVE, 1, PROJECT, "Plans a trip to Jeju.", expected_revision=1)))
    return before, await data.read_memory(ME, 1)


def test_restore_puts_a_note_back_with_its_source_at_its_next_revision() -> None:
    # V3-P71 §4.2 (rev. 4–5): an Undo; the source comes back too, so purging
    # the Conversation that rewrote it no longer takes the note.
    async def scenario() -> None:
        store, data = fresh(SCOPE, OTHER)
        before, after = await moved(data)
        assert (after.source_conversation, after.revision) == (OTHER.conversation_id, 2)
        restored = await data.restore_memory(ME, before, expected_revision=2)
        assert (restored.text, restored.origin, restored.revision) == ("Plans a trip.", MemoryOrigin.MODEL, 3)
        assert (restored.source_conversation, restored.source_call) == (SCOPE.conversation_id, before.source_call)
        assert restored.created_at == before.created_at
        with pytest.raises(DataError) as error:
            await data.restore_memory(ME, before, expected_revision=2)
        assert code(error) is DataErrorCode.VERSION_CONFLICT
        await data.trash_conversation(OTHER)
        await data.begin_purge(OTHER)
        await data.purge_conversation(OTHER)
        assert (await data_for(restart(store)).read_memory(ME, 1)) == restored

    asyncio.run(scenario())


def test_restore_never_brings_a_deleted_note_back() -> None:
    async def scenario() -> None:
        _, data = fresh()
        note = await data.save_memory(ME, USER, "Gone.")
        await data.delete_memory(ME, note.memory_id)
        with pytest.raises(DataError) as error:
            await data.restore_memory(ME, note, expected_revision=1)
        assert code(error) is DataErrorCode.NOT_FOUND
        assert await data.list_memories(ME) == ()

    asyncio.run(scenario())


def test_restore_refuses_a_source_deleted_for_good_but_not_one_in_the_trash() -> None:
    async def scenario() -> None:
        _, data = fresh(SCOPE, OTHER)
        before, after = await moved(data)
        await data.trash_conversation(SCOPE)
        await data.begin_purge(SCOPE)
        await data.purge_conversation(SCOPE)  # m1's source is OTHER now: it stays
        with pytest.raises(DataError) as error:
            await data.restore_memory(ME, before, expected_revision=2)
        assert code(error) is DataErrorCode.NOT_FOUND
        assert await data.read_memory(ME, 1) == after

        _, data = fresh(SCOPE, OTHER)
        before, _ = await moved(data)
        await data.trash_conversation(SCOPE)
        assert (await data.restore_memory(ME, before, expected_revision=2)).source_conversation == SCOPE.conversation_id
        await data.begin_purge(SCOPE)
        await data.purge_conversation(SCOPE)
        assert await data.list_memories(ME) == ()  # D5: back with its source, it goes with it

    asyncio.run(scenario())


def test_restore_is_per_principal() -> None:
    async def scenario() -> None:
        elsewhere = TrustedScope(FOREIGN.principal_id, ConversationId("conv_theirs"))
        _, data = fresh(SCOPE, elsewhere)
        mine = await data.save_memory(ME, USER, "Mine.")
        theirs = replace(mine, source_conversation=elsewhere.conversation_id)
        with pytest.raises(DataError) as error:  # a source that is not this principal's
            await data.restore_memory(ME, theirs, expected_revision=1)
        assert code(error) is DataErrorCode.NOT_FOUND
        with pytest.raises(DataError) as error:
            await data.restore_memory(FOREIGN.principal_id, mine, expected_revision=1)
        assert code(error) is DataErrorCode.NOT_FOUND

    asyncio.run(scenario())
