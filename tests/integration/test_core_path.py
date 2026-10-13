"""End-to-end P0 core path over the in-memory stack (CPP §9 acceptance)."""

from __future__ import annotations

import asyncio
import json

import pytest
from testkit import (
    FAKE,
    FOREIGN,
    SCOPE,
    RecordingTool,
    SimulatedCrash,
    calls,
    final,
    note_writer,
    runtime,
    text_result,
    user,
)

from himari.common import ASK_USER_TOOL_ID, CallId, CallRef, TextBlock, WorkspaceId
from himari.control.api import ControlError, ControlErrorCode, DriveState
from himari.data.api import (
    AssistantToolCalls,
    CompletedGroupRef,
    DataError,
    DataErrorCode,
    EventKind,
    RunStatus,
    ToolExecutionStatus,
    UserMessage,
)
from himari.prompt.api import ToolGroupTurn, UserTurn
from himari.provider.api import NativeNoResponse, NativePreparedRequest, NativeUncertain
from himari.provider.scripted import RawBody, ScriptedAdapter
from himari.tool.api import EffectProfile, WorkspaceRequirement


async def kinds(rt_data: object) -> list[EventKind]:
    events = await rt_data.read_events(SCOPE)  # type: ignore[attr-defined]
    return [e.kind for e in events]


def test_final_answer_completes_in_one_run() -> None:
    rt, provider = runtime([final("hello")])
    result = asyncio.run(rt.driver.drive(SCOPE, user("hi")))

    assert result.state is DriveState.COMPLETED
    assert len(result.run_ids) == 1
    assert asyncio.run(kinds(rt.data)) == [EventKind.USER_MESSAGE, EventKind.ASSISTANT_FINAL]
    # Raw request persisted before send, and it is what was sent.
    assert rt.store.raw_request_bodies(SCOPE) == [provider.sent[0].body]
    run = asyncio.run(rt.data.read_run(SCOPE, result.run_ids[0]))
    assert run.status is RunStatus.PROJECTED


def test_tool_group_feeds_next_run_in_ordinal_order() -> None:
    echo = RecordingTool("echo")
    rt, provider = runtime(
        [calls(("echo", {"n": 0}), ("echo", {"n": 1}), commentary="checking"), final("done")],
        tools=[echo],
    )
    result = asyncio.run(rt.driver.drive(SCOPE, user("go")))

    assert result.state is DriveState.COMPLETED
    assert len(result.run_ids) == 2 and len(echo.invocations) == 2
    second = provider.prepared_inputs[1]
    assert second.trigger_refs == (CompletedGroupRef(result.run_ids[0]),)
    group = second.conversation[-1]
    assert isinstance(group, ToolGroupTurn)
    assert [c.call_id for c in group.calls] == [CallId("c0"), CallId("c1")]
    assert group.commentary == (TextBlock("checking"),)
    assert set(group.results) == {CallId("c0"), CallId("c1")}


def test_tool_failure_is_returned_to_the_model() -> None:
    flaky = RecordingTool("flaky")
    flaky.raise_next = RuntimeError("boom")
    rt, provider = runtime([calls(("flaky", {})), final("handled")], tools=[flaky])
    result = asyncio.run(rt.driver.drive(SCOPE, user("go")))

    assert result.state is DriveState.COMPLETED
    group = provider.prepared_inputs[1].conversation[-1]
    assert isinstance(group, ToolGroupTurn)
    assert group.results[CallId("c0")].is_error


def test_internal_tool_artifact_commits_with_result() -> None:
    note = RecordingTool("write_note", effect=EffectProfile.SCOPED_WRITE, behavior=note_writer)
    rt, _ = runtime([calls(("write_note", {"text": "remember"})), final("ok")], tools=[note])
    asyncio.run(rt.driver.drive(SCOPE, user("note it")))

    notes = asyncio.run(rt.data.list_artifacts(SCOPE, kind="note"))
    assert [n.content for n in notes] == ["remember"]


def test_ask_user_waits_survives_restart_and_replays_answer_once() -> None:
    rt, provider = runtime([calls((ASK_USER_TOOL_ID, {"question": "Which file?"}))])
    first = asyncio.run(rt.driver.drive(SCOPE, user("edit it")))
    assert first.state is DriveState.WAITING_FOR_USER
    assert first.waiting_call == CallRef(first.run_ids[0], CallId("c0"))

    # Restart: new runtime over the same store. The wait is derived from Data.
    provider2 = ScriptedAdapter([final("editing main.py")])
    rt2, _ = runtime(store=rt.store, provider=provider2)
    assert asyncio.run(rt2.data.find_waiting_call(SCOPE)) == first.waiting_call

    second = asyncio.run(rt2.driver.drive(SCOPE, user("main.py")))
    assert second.state is DriveState.COMPLETED

    events = asyncio.run(rt2.data.read_events(SCOPE))
    answer = [e for e in events if isinstance(e.payload, UserMessage) and e.payload.answers_call]
    assert len(answer) == 1 and answer[0].payload.answers_call == first.waiting_call  # type: ignore[union-attr]
    assert EventKind.TOOL_RESULT in [e.kind for e in events]

    model_input = provider2.prepared_inputs[0]
    assert model_input.trigger_refs == (CompletedGroupRef(first.run_ids[0]),)
    group = model_input.conversation[-1]
    assert isinstance(group, ToolGroupTurn)
    assert group.results[CallId("c0")] == text_result("main.py")
    user_turns = [p for p in model_input.conversation if isinstance(p, UserTurn)]
    assert [t.blocks for t in user_turns] == [(TextBlock("edit it"),)]
    assert provider2.sent[0].body.count(b'"main.py"') == 1  # answer appears exactly once


def test_input_after_answer_is_an_ordinary_message() -> None:
    rt, _ = runtime([calls((ASK_USER_TOOL_ID, {"question": "?"})), final("a"), final("b")])
    asyncio.run(rt.driver.drive(SCOPE, user("start")))
    asyncio.run(rt.driver.drive(SCOPE, user("answer")))
    third = asyncio.run(rt.driver.drive(SCOPE, user("thanks")))

    assert third.state is DriveState.COMPLETED
    events = asyncio.run(rt.data.read_events(SCOPE))
    results = [e for e in events if e.kind is EventKind.TOOL_RESULT]
    assert len(results) == 1  # never a second ToolResult for the same call


def test_crash_after_dispatch_is_outcome_unknown_and_never_resent() -> None:
    rt, provider = runtime([SimulatedCrash("provider call")])
    with pytest.raises(SimulatedCrash):
        asyncio.run(rt.driver.drive(SCOPE, user("hi")))
    crashed_run = asyncio.run(rt.data.read_events(SCOPE))  # only the user event
    assert len(crashed_run) == 1
    assert len(provider.sent) == 1

    provider2 = ScriptedAdapter([final("back")])
    rt2, _ = runtime(store=rt.store, provider=provider2)
    result = asyncio.run(rt2.driver.drive(SCOPE, user("hello again")))

    assert result.state is DriveState.COMPLETED
    assert result.recovery is not None and len(result.recovery.runs_outcome_unknown) == 1
    unknown_run = result.recovery.runs_outcome_unknown[0]
    assert asyncio.run(rt2.data.read_run(SCOPE, unknown_run)).status is RunStatus.OUTCOME_UNKNOWN
    assert len(provider2.sent) == 1 and unknown_run not in result.run_ids


def test_crash_during_effectful_tool_becomes_unknown_and_blocks() -> None:
    write = RecordingTool("fake/write", effect=EffectProfile.UNKNOWN, environment_kind=FAKE,
                          workspace=WorkspaceRequirement.DEFAULT)
    write.crash_next = True
    rt, provider = runtime([calls(("fake/write", {}))], env_tools=[write])
    rt.store.activate_environment(SCOPE, FAKE, WorkspaceId("ws_1"))
    with pytest.raises(SimulatedCrash):
        asyncio.run(rt.driver.drive(SCOPE, user("write")))

    rt2, provider2 = runtime([final("never")], store=rt.store, env_tools=[write])
    result = asyncio.run(rt2.driver.drive(SCOPE, user("continue")))

    assert result.state is DriveState.RECOVERY_REQUIRED
    assert result.recovery is not None and len(result.recovery.tools_unknown) == 1
    assert len(write.invocations) == 1  # handler never re-invoked
    assert provider2.sent == []  # no next Run with an incomplete group
    records = asyncio.run(rt2.data.read_tool_executions(SCOPE, result.recovery.tools_unknown[0].run_id))
    assert records[0].status is ToolExecutionStatus.UNKNOWN


def test_effectful_tool_exception_is_uncertain_not_failed() -> None:
    write = RecordingTool("fake/write", effect=EffectProfile.UNKNOWN, environment_kind=FAKE,
                          workspace=WorkspaceRequirement.DEFAULT)
    write.raise_next = RuntimeError("connection reset mid-command")
    rt, provider = runtime([calls(("fake/write", {})), final("never")], env_tools=[write])
    rt.store.activate_environment(SCOPE, FAKE, WorkspaceId("ws_1"))
    result = asyncio.run(rt.driver.drive(SCOPE, user("write")))

    assert result.state is DriveState.RECOVERY_REQUIRED
    assert len(provider.sent) == 1  # no Run with a fabricated failure result
    call = result.unknown_calls[0]
    records = asyncio.run(rt.data.read_tool_executions(SCOPE, call.run_id))
    assert records[0].status is ToolExecutionStatus.EXECUTION_CLAIMED
    assert records[0].model_result is None

    rt2, _ = runtime([final("never")], store=rt.store, env_tools=[write])
    again = asyncio.run(rt2.driver.drive(SCOPE, user("retry?")))
    assert again.state is DriveState.RECOVERY_REQUIRED
    assert len(write.invocations) == 1


def test_never_claimed_pending_calls_resume_exactly_once() -> None:
    read = RecordingTool("fake/read", environment_kind=FAKE, workspace=WorkspaceRequirement.DEFAULT)
    write = RecordingTool("fake/write", effect=EffectProfile.SCOPED_WRITE, environment_kind=FAKE,
                          workspace=WorkspaceRequirement.DEFAULT)
    read.crash_next = True  # dies before any claim in this group
    rt, _ = runtime([calls(("fake/read", {}), ("fake/write", {}))], env_tools=[read, write])
    rt.store.activate_environment(SCOPE, FAKE, WorkspaceId("ws_1"))
    with pytest.raises(SimulatedCrash):
        asyncio.run(rt.driver.drive(SCOPE, user("go")))

    rt2, provider2 = runtime([final("done")], store=rt.store, env_tools=[read, write])
    result = asyncio.run(rt2.driver.drive(SCOPE, user("resume")))

    assert result.state is DriveState.COMPLETED
    assert len(read.invocations) == 2  # READ, never claimed: safe to run again
    assert len(write.invocations) == 1


def test_default_binding_resolution_and_revocation() -> None:
    write = RecordingTool("fake/write", effect=EffectProfile.SCOPED_WRITE, environment_kind=FAKE,
                          workspace=WorkspaceRequirement.DEFAULT)
    rt, provider = runtime(
        [calls(("fake/write", {})), final("ok"), calls(("fake/write", {})), final("denied seen")],
        env_tools=[write],
    )
    binding = rt.store.activate_environment(SCOPE, FAKE, WorkspaceId("ws_1"))
    asyncio.run(rt.driver.drive(SCOPE, user("one")))
    assert write.contexts[0].binding == binding

    rt.store.revoke_binding(SCOPE, binding.binding_id)
    asyncio.run(rt.driver.drive(SCOPE, user("two")))
    assert len(write.invocations) == 1  # not run without a current binding
    group = provider.prepared_inputs[-1].conversation[-1]
    assert isinstance(group, ToolGroupTurn) and group.results[CallId("c0")].is_error


def test_environment_tools_hidden_until_activated() -> None:
    write = RecordingTool("fake/write", environment_kind=FAKE, workspace=WorkspaceRequirement.DEFAULT)
    rt, provider = runtime([final("a"), final("b")], env_tools=[write])
    asyncio.run(rt.driver.drive(SCOPE, user("one")))
    rt.store.activate_environment(SCOPE, FAKE, WorkspaceId("ws_1"))
    asyncio.run(rt.driver.drive(SCOPE, user("two")))

    before = {d.tool_id for d in provider.prepared_inputs[0].tool_catalog}
    after = {d.tool_id for d in provider.prepared_inputs[1].tool_catalog}
    assert "fake/write" not in before and "fake/write" in after
    assert ASK_USER_TOOL_ID in before


@pytest.mark.parametrize(
    ("step", "state", "status"),
    [
        (NativeNoResponse("503"), DriveState.PROVIDER_FAILED, RunStatus.FAILED_NO_RESPONSE),
        (NativeUncertain("timeout"), DriveState.OUTCOME_UNKNOWN, RunStatus.OUTCOME_UNKNOWN),
        (RawBody(b"not json"), DriveState.INVALID_MODEL_OUTPUT, RunStatus.PROJECTION_FAILED),
    ],
)
def test_provider_failures_are_recorded_not_retried(step: object, state: DriveState, status: RunStatus) -> None:
    rt, provider = runtime([step])  # type: ignore[list-item]
    result = asyncio.run(rt.driver.drive(SCOPE, user("hi")))

    assert result.state is state
    assert len(provider.sent) == 1
    assert asyncio.run(rt.data.read_run(SCOPE, result.run_ids[0])).status is status


def test_ask_user_mixed_with_other_calls_is_rejected_without_effects() -> None:
    echo = RecordingTool("echo")
    body = json.dumps({
        "kind": "tool_calls",
        "calls": [
            {"call_id": "q", "ordinal": 0, "tool_id": "ask_user", "arguments": {}},
            {"call_id": "e", "ordinal": 1, "tool_id": "echo", "arguments": {}},
        ],
    }).encode()
    rt, _ = runtime([RawBody(body)], tools=[echo])
    result = asyncio.run(rt.driver.drive(SCOPE, user("hi")))

    assert result.state is DriveState.INVALID_MODEL_OUTPUT
    assert echo.invocations == []
    assert not any(
        isinstance(e.payload, AssistantToolCalls) for e in asyncio.run(rt.data.read_events(SCOPE))
    )


def test_concurrent_drive_on_same_conversation_is_busy() -> None:
    gate = asyncio.Event()

    class SlowAdapter(ScriptedAdapter):
        async def invoke_prepared(self, request: NativePreparedRequest):  # type: ignore[no-untyped-def]
            await gate.wait()
            return await super().invoke_prepared(request)

    rt, _ = runtime(provider=SlowAdapter([final("done")]))

    async def scenario() -> None:
        first = asyncio.create_task(rt.driver.drive(SCOPE, user("one")))
        await asyncio.sleep(0)
        while not rt.store.raw_request_bodies(SCOPE):
            await asyncio.sleep(0)
        with pytest.raises(ControlError) as exc:
            await rt.driver.drive(SCOPE, user("two"))
        assert exc.value.code is ControlErrorCode.BUSY
        gate.set()
        assert (await first).state is DriveState.COMPLETED

    asyncio.run(scenario())


def test_foreign_principal_cannot_drive() -> None:
    rt, provider = runtime([final("x")])
    with pytest.raises(DataError) as exc:
        asyncio.run(rt.driver.drive(FOREIGN, user("hi")))
    assert exc.value.code is DataErrorCode.NOT_AUTHORIZED
    assert provider.sent == []
