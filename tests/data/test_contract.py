"""Data port contract, run against every backend (tests/data/conftest.py)."""

from __future__ import annotations

import asyncio

import pytest
from testkit import SCOPE, data_for, forget_unknown_policy, new_store, text_result, user

from himari.common import ASK_USER_TOOL_ID, ProviderContinuation, RunId, TextBlock, UnknownOutcomePolicy, ArtifactId, CallId, CallRef, RawRepresentation, ToolCall, ToolId
from himari.data.api import (
    ArtifactMutation,
    AssistantFinal,
    AssistantToolCalls,
    DataError,
    DataErrorCode,
    PromptBasis,
    RawReceived,
    RunPreparation,
    RunPurpose,
    RunStatus,
    ToolCompletion,
    ToolExecutionStatus,
)
from himari.data.memory import InMemoryData
from himari.data.sqlite import SqliteData


def fresh() -> InMemoryData | SqliteData:
    store = new_store()
    store.create_conversation(SCOPE)
    return data_for(store)


async def projected_group(data: InMemoryData | SqliteData, *tool_ids: str) -> CallRef:
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

        await data.claim_tool_execution(SCOPE, call, unknown_outcome=UnknownOutcomePolicy.REPORT_TO_MODEL, record_event=True)
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
            await data.claim_tool_execution(SCOPE, call, unknown_outcome=UnknownOutcomePolicy.REQUIRE_USER, record_event=True)
            forget_unknown_policy(data._store)  # simulate a pre-V3-047 record
        else:
            await data.claim_tool_execution(SCOPE, call, unknown_outcome=policy, record_event=True)
        await data.recover_interrupted(SCOPE)
        with pytest.raises(DataError) as exc:
            await data.commit_unknown_outcome(SCOPE, call, text_result("outcome unknown", is_error=True))
        assert exc.value.code is DataErrorCode.INVALID_STATE_TRANSITION

    asyncio.run(scenario())


def test_projection_stamps_continuation_with_the_runs_provider_and_model() -> None:
    async def scenario() -> None:
        data = fresh()
        run_id = await data.prepare_run(
            SCOPE,
            RunPreparation(b"{}", "a", RawRepresentation.WIRE_EXACT, (), PromptBasis(0, 0), "openrouter", "model-x"),
        )
        await data.mark_dispatch(SCOPE, run_id)
        await data.record_raw_outcome(SCOPE, run_id, RawReceived(b"{}", "a"))
        await data.project_model_output(SCOPE, run_id, AssistantFinal((TextBlock("hi"),)),
                                        continuation={"reasoning_details": [{"type": "x"}]})
        run = await data.read_run(SCOPE, run_id)
        assert run.continuation == ProviderContinuation("openrouter", "model-x", {"reasoning_details": [{"type": "x"}]})

    asyncio.run(scenario())


# --- compaction Runs (PROMPT-04, T9c) -----------------------------------------


async def received_compaction(data: InMemoryData | SqliteData) -> RunId:
    run_id = await data.prepare_run(
        SCOPE,
        RunPreparation(b"{}", "application/json", RawRepresentation.ADAPTER_SERIALIZED, (), PromptBasis(0, 0), "p", "m",
                       purpose=RunPurpose.COMPACTION),
    )
    await data.mark_dispatch(SCOPE, run_id)
    await data.record_raw_outcome(SCOPE, run_id, RawReceived(b"{}", "application/json"))
    return run_id


def test_compaction_commit_replaces_the_snapshot_and_projects_the_run_atomically() -> None:
    async def scenario() -> None:
        data = fresh()
        for text in ("a", "b", "c"):
            await data.append_event(SCOPE, user(text))
        run_id = await received_compaction(data)
        assert (await data.read_run(SCOPE, run_id)).purpose is RunPurpose.COMPACTION
        with pytest.raises(DataError) as turn_only:
            await data.project_model_output(SCOPE, run_id, AssistantFinal((TextBlock("no"),)))
        assert turn_only.value.code is DataErrorCode.INVALID_STATE_TRANSITION
        assert await data.read_events(SCOPE, after_sequence=3) == ()

        revision = await data.commit_compaction(SCOPE, run_id, expected_revision=None, schema_version=1,
                                                body={"summary": "s"}, covers_through_sequence=2, basis=PromptBasis(2, 0))
        snapshot = await data.get_latest_snapshot(SCOPE)
        assert revision == 1 and snapshot is not None and snapshot.covers_through_sequence == 2
        assert (await data.read_run(SCOPE, run_id)).status is RunStatus.PROJECTED
        assert len(await data.read_events(SCOPE)) == 3
        with pytest.raises(DataError) as twice:
            await data.commit_compaction(SCOPE, run_id, expected_revision=1, schema_version=1,
                                         body={"summary": "s"}, covers_through_sequence=2, basis=PromptBasis(2, 0))
        assert twice.value.code is DataErrorCode.INVALID_STATE_TRANSITION

    asyncio.run(scenario())


@pytest.mark.parametrize(("expected", "covers", "code"), [
    (1, 9, DataErrorCode.STALE_BASIS),  # past the last Event
    (1, 1, DataErrorCode.STALE_BASIS),  # backwards
    (None, 3, DataErrorCode.VERSION_CONFLICT),  # another snapshot is current
])
def test_compaction_commit_refuses_stale_or_conflicting_coverage(expected: int | None, covers: int,
                                                                 code: DataErrorCode) -> None:
    async def scenario() -> None:
        data = fresh()
        for text in ("a", "b", "c"):
            await data.append_event(SCOPE, user(text))
        first = await received_compaction(data)
        await data.commit_compaction(SCOPE, first, expected_revision=None, schema_version=1,
                                     body={"summary": "s"}, covers_through_sequence=2, basis=PromptBasis(2, 0))
        run_id = await received_compaction(data)
        with pytest.raises(DataError) as refused:
            await data.commit_compaction(SCOPE, run_id, expected_revision=expected, schema_version=1,
                                         body={"summary": "t"}, covers_through_sequence=covers, basis=PromptBasis(3, 0))
        assert refused.value.code is code
        snapshot = await data.get_latest_snapshot(SCOPE)
        assert snapshot is not None and snapshot.body == {"summary": "s"}
        assert (await data.read_run(SCOPE, run_id)).status is RunStatus.RECEIVED

    asyncio.run(scenario())


def test_a_turn_run_cannot_commit_a_compaction() -> None:
    async def scenario() -> None:
        data = fresh()
        await data.append_event(SCOPE, user("a"))
        run_id = await data.prepare_run(
            SCOPE, RunPreparation(b"{}", "t", RawRepresentation.ADAPTER_SERIALIZED, (), PromptBasis(0, 0), "p", "m"))
        await data.mark_dispatch(SCOPE, run_id)
        await data.record_raw_outcome(SCOPE, run_id, RawReceived(b"{}", "t"))
        with pytest.raises(DataError) as refused:
            await data.commit_compaction(SCOPE, run_id, expected_revision=None, schema_version=1,
                                         body={"summary": "s"}, covers_through_sequence=1, basis=PromptBasis(1, 0))
        assert refused.value.code is DataErrorCode.INVALID_STATE_TRANSITION

    asyncio.run(scenario())


def test_restart_discards_an_uncommitted_compaction_instead_of_projecting_it() -> None:
    async def scenario() -> None:
        data = fresh()
        await data.append_event(SCOPE, user("a"))
        run_id = await received_compaction(data)
        report = await data.recover_interrupted(SCOPE)
        assert report.compactions_discarded == (run_id,) and report.runs_received_unprojected == ()
        run = await data.read_run(SCOPE, run_id)
        assert run.status is RunStatus.PROJECTION_FAILED and run.error_code == "interrupted"

    asyncio.run(scenario())


def test_a_committed_compaction_does_not_hide_a_waiting_question() -> None:
    async def scenario() -> None:
        data = fresh()
        call = await projected_group(data, ASK_USER_TOOL_ID)
        run_id = await received_compaction(data)
        await data.commit_compaction(SCOPE, run_id, expected_revision=None, schema_version=1,
                                     body={"summary": "s"}, covers_through_sequence=0, basis=PromptBasis(0, 0))
        assert await data.find_waiting_call(SCOPE) == call

    asyncio.run(scenario())


def test_a_compaction_step_is_settled_without_touching_the_snapshot() -> None:
    async def scenario() -> None:
        data = fresh()
        await data.append_event(SCOPE, user("a"))
        run_id = await received_compaction(data)
        await data.record_compaction_step(SCOPE, run_id)
        assert (await data.read_run(SCOPE, run_id)).status is RunStatus.PROJECTED
        assert await data.get_latest_snapshot(SCOPE) is None
        with pytest.raises(DataError) as again:
            await data.record_compaction_step(SCOPE, run_id)
        assert again.value.code is DataErrorCode.INVALID_STATE_TRANSITION
        turn = await data.prepare_run(
            SCOPE, RunPreparation(b"{}", "t", RawRepresentation.ADAPTER_SERIALIZED, (), PromptBasis(0, 0), "p", "m"))
        await data.mark_dispatch(SCOPE, turn)
        await data.record_raw_outcome(SCOPE, turn, RawReceived(b"{}", "t"))
        with pytest.raises(DataError) as not_compaction:
            await data.record_compaction_step(SCOPE, turn)
        assert not_compaction.value.code is DataErrorCode.INVALID_STATE_TRANSITION

    asyncio.run(scenario())
