"""The trash (V3-P65): ACTIVE → TRASHED → PURGING → gone, and restore, on every backend."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from testkit import FAKE, data_for, new_store, restart, text_result, user

from himari.common import (
    CallId,
    CallRef,
    ConversationId,
    PrincipalId,
    RawRepresentation,
    ToolCall,
    ToolId,
    TrustedScope,
    UnknownOutcomePolicy,
)
from himari.data.api import (
    AssistantToolCalls,
    DataError,
    DataErrorCode,
    ModelSetting,
    PromptBasis,
    RawReceived,
    RunPreparation,
    ToolCompletion,
    ToolExecutionStatus,
)
from himari.data.memory import InMemoryData
from himari.data.sqlite import SqliteData

ALICE, BOB = PrincipalId("alice"), PrincipalId("bob")

Data = InMemoryData | SqliteData


async def conversation(data: Data, owner: PrincipalId = ALICE) -> TrustedScope:
    cid = await data.create_conversation(owner, title="t", environments=(FAKE,))
    return TrustedScope(owner, cid)


async def called(data: Data, scope: TrustedScope, *tool_ids: str) -> CallRef:
    """A Run whose Tool calls are recorded; the first one done."""
    run_id = await data.prepare_run(
        scope, RunPreparation(b"{}", "application/json", RawRepresentation.ADAPTER_SERIALIZED, (),
                              PromptBasis(0, 0), "p", "m"))
    await data.mark_dispatch(scope, run_id)
    await data.record_raw_outcome(scope, run_id, RawReceived(b"{}", "application/json"))
    group = tuple(ToolCall(CallId(f"c{i}"), i, ToolId(t), {}) for i, t in enumerate(tool_ids))
    await data.project_model_output(scope, run_id, AssistantToolCalls(group))
    call = CallRef(run_id, CallId("c0"))
    await data.commit_tool_outcome(scope, call, ToolCompletion(
        ToolExecutionStatus.SUCCEEDED, text_result("ok")))
    return call


def code(error: pytest.ExceptionInfo[DataError]) -> DataErrorCode:
    return error.value.code


Port = Callable[[Data, TrustedScope], Coroutine[Any, Any, object]]

# Every other port a trashed Conversation must refuse as if it were missing.
OTHER_PORTS: list[tuple[str, Port]] = [
    ("append_event", lambda d, s: d.append_event(s, user("late"))),
    ("read_events", lambda d, s: d.read_events(s)),
    ("prepare_run", lambda d, s: d.prepare_run(s, RunPreparation(
        b"{}", "application/json", RawRepresentation.ADAPTER_SERIALIZED, (), PromptBasis(0, 0), "p", "m"))),
    ("recover_interrupted", lambda d, s: d.recover_interrupted(s)),
    ("get_latest_snapshot", lambda d, s: d.get_latest_snapshot(s)),
    ("has_environment", lambda d, s: d.has_environment(s, FAKE)),
    ("read_conversation", lambda d, s: d.read_conversation(s)),
    ("rename_conversation", lambda d, s: d.rename_conversation(s, "x")),
    ("read_external_settings", lambda d, s: d.read_external_settings(s)),
    ("set_external_setting", lambda d, s: d.set_external_setting(s, "calliope", False)),
    ("read_model_setting", lambda d, s: d.read_model_setting(s)),
    ("set_model_setting", lambda d, s: d.set_model_setting(s, ModelSetting("m", None))),
    ("read_current_state_basis", lambda d, s: d.read_current_state_basis(s)),
    ("used_tool_ids", lambda d, s: d.used_tool_ids(s)),
    ("count_staged", lambda d, s: d.count_staged(s)),
    ("read_files", lambda d, s: d.read_files(s)),
    ("read_file_records", lambda d, s: d.read_file_records(s, [])),
    ("read_call_files", lambda d, s: d.read_call_files(s, [])),
    ("check_attachments", lambda d, s: d.check_attachments(s, [])),
    ("presented_bytes_since", lambda d, s: d.presented_bytes_since(s, datetime.now(UTC))),
]


@pytest.mark.parametrize("name, port", OTHER_PORTS, ids=[n for n, _ in OTHER_PORTS])
def test_a_trashed_conversation_is_a_missing_one_to_every_other_port(name: str, port: Port) -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        scope = await conversation(data)
        await data.trash_conversation(scope)
        with pytest.raises(DataError) as exc:
            await port(data, scope)
        assert code(exc) is DataErrorCode.NOT_AUTHORIZED

    asyncio.run(scenario())


def test_trash_and_restore_keep_everything() -> None:
    async def scenario() -> None:
        store = new_store()
        data = data_for(store)
        scope = await conversation(data)
        await data.append_event(scope, user("hello"))
        await data.set_model_setting(scope, ModelSetting("m", "high"))
        before = await data.read_current_state_basis(scope), await data.read_events(scope)

        moved = await data.trash_conversation(scope)
        assert abs(moved - datetime.now(UTC)) < timedelta(seconds=5)
        assert await data.list_conversations(ALICE) == ()
        (listed,) = await data.list_conversations(ALICE, trashed=True)
        assert listed.conversation_id == scope.conversation_id and listed.trashed_at == moved
        assert listed.preview == "hello" and listed.title == "t"

        data = data_for(restart(store))  # the trash is durable
        await data.restore_conversation(scope)
        assert (await data.read_current_state_basis(scope), await data.read_events(scope)) == before
        assert await data.read_model_setting(scope) == ModelSetting("m", "high")
        (back,) = await data.list_conversations(ALICE)
        assert back.trashed_at is None and await data.list_conversations(ALICE, trashed=True) == ()

    asyncio.run(scenario())


def test_wrong_transitions_are_refused() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        scope = await conversation(data)
        transitions: tuple[Callable[[TrustedScope], Coroutine[Any, Any, object]], ...] = (
            data.restore_conversation, data.begin_purge, data.purge_conversation)
        for wrong in transitions:
            with pytest.raises(DataError) as exc:
                await wrong(scope)  # still active
            assert code(exc) is DataErrorCode.INVALID_STATE_TRANSITION
        await data.trash_conversation(scope)
        transitions = (data.trash_conversation, data.purge_conversation)  # purge needs begin_purge first
        for wrong in transitions:
            with pytest.raises(DataError) as exc:
                await wrong(scope)
            assert code(exc) is DataErrorCode.INVALID_STATE_TRANSITION

        first = await data.begin_purge(scope)
        again = await data.begin_purge(scope)  # idempotent: a purge cut short resumes
        assert first == again and first.environments == (FAKE,)  # whose folder goes with it
        with pytest.raises(DataError) as exc:
            await data.restore_conversation(scope)  # its files may already be gone
        assert code(exc) is DataErrorCode.INVALID_STATE_TRANSITION
        assert [s.conversation_id for s in await data.list_conversations(ALICE, trashed=True)] == [scope.conversation_id]

        await data.purge_conversation(scope)
        assert await data.list_conversations(ALICE, trashed=True) == ()
        for gone in (data.trash_conversation, data.restore_conversation, data.begin_purge, data.purge_conversation,
                     data.read_events):
            with pytest.raises(DataError) as exc:
                await gone(scope)
            assert code(exc) is DataErrorCode.NOT_AUTHORIZED

    asyncio.run(scenario())


def test_only_the_owner_may_move_restore_or_purge() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        scope = await conversation(data)
        foreign = TrustedScope(BOB, scope.conversation_id)
        missing = TrustedScope(ALICE, ConversationId("conv_999"))
        for intruder in (foreign, missing):
            for method in (data.trash_conversation, data.restore_conversation, data.begin_purge,
                           data.purge_conversation, data.used_tool_ids):
                with pytest.raises(DataError) as exc:
                    await method(intruder)
                assert code(exc) is DataErrorCode.NOT_AUTHORIZED  # the same for foreign and missing
        assert (await data.read_conversation(scope)).trashed_at is None
        await data.trash_conversation(scope)
        assert await data.list_conversations(BOB, trashed=True) == ()

    asyncio.run(scenario())


def test_used_tool_ids_are_what_the_conversation_called() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        scope = await conversation(data)
        assert await data.used_tool_ids(scope) == frozenset()
        await called(data, scope, "calliope/board", "inside/read_file")
        await called(data, scope, "ask_user")
        assert await data.used_tool_ids(scope) == {"calliope/board", "inside/read_file", "ask_user"}

    asyncio.run(scenario())


def test_due_purges_are_old_trash_and_every_purge_cut_short() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        old, cut_short = await conversation(data), await conversation(data, BOB)
        recent, active = await conversation(data), await conversation(data, BOB)
        await data.trash_conversation(old)
        await data.trash_conversation(cut_short)
        await data.begin_purge(cut_short)
        cutoff = datetime.now(UTC)
        await asyncio.sleep(0.01)
        await data.trash_conversation(recent)
        assert await data.due_purges(cutoff) == (old, cut_short)  # oldest first, every principal
        assert await data.due_purges(cutoff - timedelta(days=1)) == (cut_short,)  # PURGING whatever its age
        assert active not in await data.due_purges(datetime.now(UTC) + timedelta(days=1))

    asyncio.run(scenario())


def test_trash_lists_most_recently_trashed_first() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        first, second = await conversation(data), await conversation(data)
        await data.trash_conversation(second)
        await asyncio.sleep(0.01)
        await data.trash_conversation(first)
        assert [s.conversation_id for s in await data.list_conversations(ALICE, trashed=True)] == [
            first.conversation_id, second.conversation_id]
        assert [s.conversation_id for s in await data.list_conversations(ALICE, trashed=True, limit=1, offset=1)] == [
            second.conversation_id]

    asyncio.run(scenario())


def test_a_trashed_conversation_takes_no_late_tool_results() -> None:
    async def scenario() -> None:
        data = data_for(new_store())
        scope = await conversation(data)
        run_id = await data.prepare_run(scope, RunPreparation(
            b"{}", "application/json", RawRepresentation.ADAPTER_SERIALIZED, (), PromptBasis(0, 0), "p", "m"))
        await data.mark_dispatch(scope, run_id)
        await data.record_raw_outcome(scope, run_id, RawReceived(b"{}", "application/json"))
        await data.project_model_output(scope, run_id, AssistantToolCalls((ToolCall(CallId("c"), 0, ToolId("x/y"), {}),)))
        call = CallRef(run_id, CallId("c"))
        await data.claim_tool_execution(scope, call, unknown_outcome=UnknownOutcomePolicy.REPORT_TO_MODEL,
                                        record_event=True)
        await data.trash_conversation(scope)
        with pytest.raises(DataError) as exc:
            await data.commit_tool_outcome(scope, call, ToolCompletion(ToolExecutionStatus.SUCCEEDED, text_result("late")))
        assert code(exc) is DataErrorCode.NOT_AUTHORIZED
        await data.restore_conversation(scope)
        (record,) = await data.read_tool_executions(scope, run_id)
        assert record.status is ToolExecutionStatus.EXECUTION_CLAIMED  # nothing was written meanwhile

    asyncio.run(scenario())


def test_a_purge_takes_the_mcp_views_only_its_calls_named() -> None:
    """V3-P97 keeps ui_resources for the views stored before; a purge still takes the ones only its calls named."""
    async def scenario() -> None:
        data = data_for(new_store())
        if not isinstance(data, SqliteData):
            pytest.skip("ui_resources is SQLite's")
        scope, other = await conversation(data), await conversation(data)
        mine = await called(data, scope, "calliope/board", "calliope/board")  # calls c0 and c1
        theirs = await called(data, other, "calliope/board")
        with data._tx() as tx:  # as an earlier version stored them
            for digest in ("d_mine", "d_shared"):
                tx.execute("INSERT INTO ui_resources (digest, source, uri, html, meta) VALUES (?, ?, ?, ?, ?)",
                           digest, "calliope", "ui://board", "<p>board</p>", "{}")
            for run_id, call_id, digest in ((mine.run_id, "c0", "d_mine"), (mine.run_id, "c1", "d_shared"),
                                            (theirs.run_id, "c0", "d_shared")):
                tx.execute("UPDATE tool_executions SET ui_digest=? WHERE run_id=? AND call_id=?", digest, run_id,
                           call_id)
        await data.trash_conversation(scope)
        await data.begin_purge(scope)
        await data.purge_conversation(scope)
        with data._tx() as tx:
            left = {r["digest"] for r in tx.all("SELECT digest FROM ui_resources")}
        assert left == {"d_shared"}  # another conversation's call still names it

    asyncio.run(scenario())
