"""ConversationSearch (V3-P72 §3–§5) contract, on every backend."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from testkit import FOREIGN, SCOPE, data_for, new_store, restart, text_result, user

from himari.common import CallId, CallRef, ConversationId, RawRepresentation, RunId, TextBlock, ToolCall, ToolId, TrustedScope
from himari.data.api import (
    MAX_SEARCH_WORDS,
    AssistantFinal,
    AssistantToolCalls,
    DataError,
    DataErrorCode,
    EventKind,
    EventSearch,
    PromptBasis,
    RawReceived,
    RunPreparation,
    ToolCompletion,
    ToolExecutionStatus,
)
from himari.data import sqlite as sqlite_backend
from himari.data.memory import InMemoryData
from himari.data.sqlite import SqliteData, SqliteStore

ME = SCOPE.principal_id
OTHER = TrustedScope(ME, ConversationId("conv_other"))
NOWHERE = ConversationId("conv_none")  # a current Conversation that holds nothing


def fresh(*scopes: TrustedScope) -> InMemoryData | SqliteData:
    store = new_store()
    for scope in scopes or (SCOPE,):
        store.create_conversation(scope)
    return data_for(store)


async def run_of(data: InMemoryData | SqliteData, scope: TrustedScope) -> RunId:
    run_id = await data.prepare_run(
        scope, RunPreparation(b"{}", "application/json", RawRepresentation.ADAPTER_SERIALIZED, (), PromptBasis(0, 0),
                              "p", "m"))
    await data.mark_dispatch(scope, run_id)
    await data.record_raw_outcome(scope, run_id, RawReceived(b"{}", "application/json"))
    return run_id


async def answer(data: InMemoryData | SqliteData, scope: TrustedScope, text: str) -> None:
    await data.project_model_output(scope, await run_of(data, scope), AssistantFinal((TextBlock(text),)))


async def tool_step(data: InMemoryData | SqliteData, scope: TrustedScope, commentary: str, result: str) -> None:
    run_id = await run_of(data, scope)
    await data.project_model_output(scope, run_id, AssistantToolCalls(
        (ToolCall(CallId("c0"), 0, ToolId("inside/read_file"), {}),), commentary=(TextBlock(commentary),)))
    await data.commit_tool_outcome(scope, CallRef(run_id, CallId("c0")),
                                   ToolCompletion(ToolExecutionStatus.SUCCEEDED, text_result(result)))


async def search(data: InMemoryData | SqliteData, *words: str, current: ConversationId = NOWHERE,
                 through: int | None = None, only: ConversationId | None = None, per: int = 3,
                 limit: int = 10) -> EventSearch:
    return await data.search_events(ME, list(words), current=current, current_through=through, conversation=only,
                                    per_conversation=per, limit=limit)


def texts(found: EventSearch) -> list[str]:
    return [h.text for h in found.hits]


def test_user_messages_and_final_answers_are_searched_and_nothing_else() -> None:
    async def scenario() -> None:
        data = fresh()
        await data.append_event(SCOPE, user("배포 순서는\n데이터 먼저"))
        await tool_step(data, SCOPE, "PROGRESS line", "TOOLRESULT text")
        await answer(data, SCOPE, "그다음 웹으로 정했다")
        found = await search(data, "순서")  # two Korean characters: a substring, not a whole word
        assert texts(found) == ["배포 순서는\n데이터 먼저"]
        (hit,) = found.hits
        assert (hit.conversation_id, hit.sequence, hit.kind, hit.matched) == (SCOPE.conversation_id, 1,
                                                                             EventKind.USER_MESSAGE, 1)
        assert [c.conversation_id for c in found.conversations] == [SCOPE.conversation_id]
        assert texts(await search(data, "웹으로")) == ["그다음 웹으로 정했다"]
        assert (await search(data, "progress")).hits == ()  # the progress line of a tool step
        assert (await search(data, "toolresult")).hits == ()  # a tool's result

    asyncio.run(scenario())


def test_words_are_folded_like_memory_search() -> None:
    async def scenario() -> None:
        data = fresh()
        await data.append_event(SCOPE, user("Fix the ＳＱＬ Index"))
        assert texts(await search(data, "sql", "INDEX")) == ["Fix the ＳＱＬ Index"]  # NFKC, case-folded
        assert (await search(data, "sql", "sql")).hits[0].matched == 1  # a repeated word counts once

    asyncio.run(scenario())


def test_most_words_first_then_newest_at_most_per_conversation_and_limit() -> None:
    async def scenario() -> None:
        data = fresh(SCOPE, OTHER)
        for n in range(5):
            await data.append_event(SCOPE, user(f"deploy note {n}"))
        await data.append_event(OTHER, user("deploy and rollback"))
        await data.append_event(OTHER, user("deploy only"))
        found = await search(data, "deploy", "rollback")
        assert texts(found) == ["deploy and rollback", "deploy only", "deploy note 4", "deploy note 3",
                                "deploy note 2"]
        assert found.total == 7
        assert len((await search(data, "deploy", limit=2)).hits) == 2
        assert texts(await search(data, "note", per=1)) == ["deploy note 4"]

    asyncio.run(scenario())


def test_only_the_principals_conversations_outside_the_trash() -> None:
    async def scenario() -> None:
        theirs = TrustedScope(FOREIGN.principal_id, ConversationId("conv_theirs"))
        data = fresh(SCOPE, OTHER, theirs)
        await data.append_event(SCOPE, user("shared word here"))
        await data.append_event(OTHER, user("shared word there"))
        await data.append_event(theirs, user("shared word theirs"))
        assert sorted(texts(await search(data, "shared"))) == ["shared word here", "shared word there"]
        await data.trash_conversation(OTHER)
        assert texts(await search(data, "shared")) == ["shared word here"]
        with pytest.raises(DataError) as error:  # as any read of a trashed one
            await search(data, "shared", only=OTHER.conversation_id)
        assert error.value.code is DataErrorCode.NOT_AUTHORIZED
        await data.restore_conversation(OTHER)
        assert texts(await search(data, "shared", only=OTHER.conversation_id)) == ["shared word there"]
        with pytest.raises(DataError) as error:
            await search(data, "shared", only=theirs.conversation_id)
        assert error.value.code is DataErrorCode.NOT_AUTHORIZED

    asyncio.run(scenario())


def test_the_current_conversation_only_up_to_its_summary() -> None:
    # §3.2: what the model already sees exactly is not searched; what a compaction summary replaced is.
    async def scenario() -> None:
        data = fresh()
        for n in range(1, 5):
            await data.append_event(SCOPE, user(f"path {n}"))
        here = SCOPE.conversation_id
        assert (await search(data, "path", current=here)).hits == ()  # never compacted: none of it
        assert texts(await search(data, "path", current=here, through=2)) == ["path 2", "path 1"]
        assert len((await search(data, "path")).hits) == 3  # from another conversation: all of it (3 at most)

    asyncio.run(scenario())


@pytest.mark.parametrize("words", [[], ["  "], [f"w{n}" for n in range(MAX_SEARCH_WORDS + 1)], ["x" * 201], "word"])
def test_bad_words_are_refused(words: object) -> None:
    async def scenario() -> None:
        data = fresh()
        with pytest.raises(DataError) as error:
            await data.search_events(ME, words, current=NOWHERE, current_through=None,  # type: ignore[arg-type]
                                     per_conversation=3, limit=10)
        assert error.value.code is DataErrorCode.INVALID_ARGUMENT

    asyncio.run(scenario())


def test_read_goes_on_from_a_sequence() -> None:
    async def scenario() -> None:
        data = fresh(SCOPE, OTHER)
        await data.append_event(OTHER, user("q1"))
        await tool_step(data, OTHER, "looking", "result")  # sequences 2 and 3: not read
        await answer(data, OTHER, "a1")
        await data.append_event(OTHER, user("q2"))
        page = await data.read_event_texts(ME, OTHER.conversation_id, from_sequence=1, current=NOWHERE,
                                           current_through=None, limit=2)
        assert [(i.sequence, i.text) for i in page.items] == [(1, "q1"), (4, "a1")] and page.more
        assert page.conversation.conversation_id == OTHER.conversation_id
        rest = await data.read_event_texts(ME, OTHER.conversation_id, from_sequence=5, current=NOWHERE,
                                           current_through=None, limit=2)
        assert [i.text for i in rest.items] == ["q2"] and not rest.more

    asyncio.run(scenario())


def test_read_refuses_what_it_may_not_show() -> None:
    async def scenario() -> None:
        theirs = TrustedScope(FOREIGN.principal_id, ConversationId("conv_theirs"))
        data = fresh(SCOPE, theirs)
        for n in range(1, 4):
            await data.append_event(SCOPE, user(f"m{n}"))
        here = SCOPE.conversation_id
        page = await data.read_event_texts(ME, here, from_sequence=1, current=here, current_through=2, limit=10)
        assert [i.text for i in page.items] == ["m1", "m2"] and not page.more  # up to the summary's end
        for start, through in ((3, 2), (1, None)):
            with pytest.raises(DataError) as error:  # already in front of the model
                await data.read_event_texts(ME, here, from_sequence=start, current=here, current_through=through,
                                            limit=10)
            assert error.value.code is DataErrorCode.INVALID_ARGUMENT
        with pytest.raises(DataError) as error:
            await data.read_event_texts(ME, theirs.conversation_id, from_sequence=1, current=here,
                                        current_through=None, limit=10)
        assert error.value.code is DataErrorCode.NOT_AUTHORIZED

    asyncio.run(scenario())


def test_a_permanent_delete_takes_the_text_and_a_restart_keeps_it() -> None:
    async def scenario() -> None:
        store = new_store()
        store.create_conversation(SCOPE)
        store.create_conversation(OTHER)
        data = data_for(store)
        await data.append_event(SCOPE, user("kept text"))
        await data.append_event(OTHER, user("gone text"))
        data = data_for(restart(store))
        assert sorted(texts(await search(data, "text"))) == ["gone text", "kept text"]
        await data.trash_conversation(OTHER)
        await data.begin_purge(OTHER)
        await data.purge_conversation(OTHER)
        assert texts(await search(data, "text")) == ["kept text"]
        if isinstance(store, SqliteStore):  # its rows are gone, not only hidden
            conn = sqlite3.connect(store.path)
            try:
                assert conn.execute("SELECT count(*) FROM event_text WHERE conversation_id=?",
                                    (OTHER.conversation_id,)).fetchone()[0] == 0
            finally:
                conn.close()

    asyncio.run(scenario())


def test_an_upgrade_fills_in_batches_without_missing_or_repeating_one(tmp_path: Path,
                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    # PR #79 review B1: the fill reads FILL_BATCH Events at a time, in key order.
    path = tmp_path / "h.db"
    store = SqliteStore(path)
    scopes = [TrustedScope(ME, ConversationId(f"conv_{name}")) for name in ("a", "b", "c")]
    for scope in scopes:
        store.create_conversation(scope)
    data = SqliteData(store)

    async def fill() -> None:
        for n in range(7):
            for scope in scopes:
                await data.append_event(scope, user(f"note {scope.conversation_id} {n}"))
        await answer(data, scopes[0], "an answer note")

    asyncio.run(fill())
    store.close()
    conn = sqlite3.connect(path)
    try:
        conn.execute("DROP INDEX bindings_by_workspace")  # v13 (V3-P74)
        conn.execute("DROP TABLE workspaces")
        conn.execute("DROP TABLE event_text")
        conn.execute("PRAGMA user_version=11")
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(sqlite_backend, "FILL_BATCH", 2)
    store = SqliteStore(path)
    try:
        rows = store.connection.execute("SELECT conversation_id, sequence FROM event_text").fetchall()
        assert len(rows) == len(set(rows)) == 22  # 21 messages and one answer, each once
        found = asyncio.run(SqliteData(store).search_events(ME, ["answer"], current=NOWHERE, current_through=None,
                                                            per_conversation=3, limit=10))
        assert texts(found) == ["an answer note"]
    finally:
        store.close()
