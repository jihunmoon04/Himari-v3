from __future__ import annotations

import asyncio
import dataclasses

import pytest
from testkit import SCOPE, text_result, user

from himari.common import ASK_USER_TOOL_ID, UnknownOutcomePolicy, ArtifactId, CallId, CallRef, RawRepresentation, ToolCall, ToolId
from himari.data.api import (
    ArtifactMutation,
    AssistantToolCalls,
    DataError,
    DataErrorCode,
    PromptBasis,
    RawReceived,
    RunPreparation,
    ToolCompletion,
    ToolExecutionStatus,
)
from himari.data.memory import InMemoryData, InMemoryStore


def fresh() -> InMemoryData:
    store = InMemoryStore()
    store.create_conversation(SCOPE)
    return InMemoryData(store)


async def projected_group(data: InMemoryData, *tool_ids: str) -> CallRef:
    run_id = await data.prepare_run(
        SCOPE,
        RunPreparation(b"{}", "application/json", RawRepresentation.ADAPTER_SERIALIZED, (), PromptBasis(0, 0), "p", "m"),
    )
    await data.mark_dispatch(SCOPE, run_id)
    await data.record_raw_outcome(SCOPE, run_id, RawReceived(b"{}", "application/json"))
    group = tuple(ToolCall(CallId(f"c{i}"), i, ToolId(t), {}) for i, t in enumerate(tool_ids))
    await data.project_model_output(SCOPE, run_id, AssistantToolCalls(group))
    return CallRef(run_id, CallId("c0"))


def test_send_mark_is_once_only() -> None:
    async def scenario() -> None:
        data = fresh()
        run_id = await data.prepare_run(
            SCOPE,
            RunPreparation(b"x", "t", RawRepresentation.ADAPTER_SERIALIZED, (), PromptBasis(0, 0), "p", "m"),
        )
        await data.mark_dispatch(SCOPE, run_id)
        with pytest.raises(DataError) as exc:
            await data.mark_dispatch(SCOPE, run_id)
        assert exc.value.code is DataErrorCode.INVALID_STATE_TRANSITION

    asyncio.run(scenario())


def test_waiting_call_forces_answer_path() -> None:
    async def scenario() -> None:
        data = fresh()
        call = await projected_group(data, ASK_USER_TOOL_ID)
        assert await data.find_waiting_call(SCOPE) == call
        with pytest.raises(DataError) as exc:
            await data.append_event(SCOPE, user("not via T1"))
        assert exc.value.code is DataErrorCode.INVALID_STATE_TRANSITION

        commit = await data.answer_pending_call(SCOPE, call, user("yes"))
        assert commit.user_event.payload.answers_call == call  # type: ignore[union-attr]
        assert await data.find_waiting_call(SCOPE) is None
        with pytest.raises(DataError) as again:
            await data.answer_pending_call(SCOPE, call, user("yes again"))
        assert again.value.code is DataErrorCode.ALREADY_COMMITTED

    asyncio.run(scenario())


def test_artifact_conflict_rolls_back_the_whole_commit() -> None:
    async def scenario() -> None:
        data = fresh()
        first = await projected_group(data, "note", "note")
        await data.commit_tool_outcome(
            SCOPE, first, ToolCompletion(ToolExecutionStatus.SUCCEEDED, text_result("ok"),
                                         artifact_mutations=(ArtifactMutation("note", "v1"),))
        )
        (note,) = await data.list_artifacts(SCOPE)
        second = CallRef(first.run_id, CallId("c1"))
        events_before = len(await data.read_events(SCOPE))
        stale = ArtifactMutation("note", "v2", artifact_id=note.artifact_id, expected_revision=99)
        with pytest.raises(DataError) as exc:
            await data.commit_tool_outcome(
                SCOPE, second, ToolCompletion(ToolExecutionStatus.SUCCEEDED, text_result("ok"),
                                              artifact_mutations=(stale,))
            )
        assert exc.value.code is DataErrorCode.VERSION_CONFLICT
        assert (await data.read_artifact(SCOPE, ArtifactId(note.artifact_id))).content == "v1"
        records = await data.read_tool_executions(SCOPE, first.run_id)
        assert records[1].status is ToolExecutionStatus.PENDING
        assert len(await data.read_events(SCOPE)) == events_before

    asyncio.run(scenario())


def test_outcome_commits_once() -> None:
    async def scenario() -> None:
        data = fresh()
        call = await projected_group(data, "echo")
        done = ToolCompletion(ToolExecutionStatus.SUCCEEDED, text_result("ok"))
        await data.commit_tool_outcome(SCOPE, call, done)
        with pytest.raises(DataError) as exc:
            await data.commit_tool_outcome(SCOPE, call, done)
        assert exc.value.code is DataErrorCode.ALREADY_COMMITTED

    asyncio.run(scenario())


def test_record_event_false_keeps_result_out_of_conversation() -> None:
    async def scenario() -> None:
        data = fresh()
        call = await projected_group(data, "echo")
        event = await data.commit_tool_outcome(
            SCOPE, call, ToolCompletion(ToolExecutionStatus.SUCCEEDED, text_result("ok"), record_event=False)
        )
        assert event is None
        (record,) = await data.read_tool_executions(SCOPE, call.run_id)
        assert record.model_result == text_result("ok")

    asyncio.run(scenario())


def test_idempotency_key_reuse_with_different_message_is_rejected() -> None:
    async def scenario() -> None:
        data = fresh()
        first = await data.append_event(SCOPE, user("A"), idempotency_key="k")
        assert await data.append_event(SCOPE, user("A"), idempotency_key="k") == first
        with pytest.raises(DataError) as exc:
            await data.append_event(SCOPE, user("B"), idempotency_key="k")
        assert exc.value.code is DataErrorCode.DUPLICATE_IDENTITY
        assert len(await data.read_events(SCOPE)) == 1

    asyncio.run(scenario())


def test_stored_records_are_isolated_from_caller_objects() -> None:
    async def scenario() -> None:
        data = fresh()
        call = await projected_group(data, "echo")
        (record,) = await data.read_tool_executions(SCOPE, call.run_id)
        with pytest.raises(TypeError):
            record.arguments["x"] = 1  # type: ignore[index]
        assert dict((await data.read_tool_executions(SCOPE, call.run_id))[0].arguments) == {}

    asyncio.run(scenario())


def test_unknown_outcome_keeps_status_and_commits_once() -> None:
    async def scenario() -> None:
        data = fresh()
        call = await projected_group(data, "fake/write")
        notice = text_result("outcome unknown", is_error=True)
        with pytest.raises(DataError) as not_claimed:
            await data.commit_unknown_outcome(SCOPE, call, notice)
        assert not_claimed.value.code is DataErrorCode.INVALID_STATE_TRANSITION  # PENDING: nothing happened yet

        await data.claim_tool_execution(SCOPE, call, unknown_outcome=UnknownOutcomePolicy.REPORT_TO_MODEL)
        event = await data.commit_unknown_outcome(SCOPE, call, notice)
        assert event is not None
        (record,) = await data.read_tool_executions(SCOPE, call.run_id)
        assert record.status is ToolExecutionStatus.UNKNOWN and record.model_result == notice
        with pytest.raises(DataError) as again:
            await data.commit_unknown_outcome(SCOPE, call, notice)
        assert again.value.code is DataErrorCode.ALREADY_COMMITTED
        with pytest.raises(DataError):
            await data.commit_tool_outcome(SCOPE, call, ToolCompletion(ToolExecutionStatus.SUCCEEDED, notice))

    asyncio.run(scenario())


@pytest.mark.parametrize("policy", [UnknownOutcomePolicy.REQUIRE_USER, None])
def test_data_refuses_to_report_unless_policy_allows(policy: UnknownOutcomePolicy | None) -> None:
    """Defense in depth: even a buggy Dispatcher cannot relax REQUIRE_USER or
    an unknown (legacy) policy into a model report."""

    async def scenario() -> None:
        data = fresh()
        call = await projected_group(data, "fake/pay")
        if policy is None:
            await data.claim_tool_execution(SCOPE, call, unknown_outcome=UnknownOutcomePolicy.REQUIRE_USER)
            conv = data._store._conversations[SCOPE.conversation_id]  # simulate a pre-V3-047 record
            conv.executions[call] = dataclasses.replace(conv.executions[call], unknown_outcome=None)
        else:
            await data.claim_tool_execution(SCOPE, call, unknown_outcome=policy)
        await data.recover_interrupted(SCOPE)
        with pytest.raises(DataError) as exc:
            await data.commit_unknown_outcome(SCOPE, call, text_result("outcome unknown", is_error=True))
        assert exc.value.code is DataErrorCode.INVALID_STATE_TRANSITION

    asyncio.run(scenario())
