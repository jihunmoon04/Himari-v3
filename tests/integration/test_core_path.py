"""End-to-end P0 core path over the in-memory stack (CPP §9 acceptance)."""

from __future__ import annotations

import asyncio
import time
import dataclasses
import json

import httpx
from typing import Any

import pytest
from testkit import (
    forget_unknown_policy,
    restart,
    FAKE,
    FakeEnvironment,
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

from himari.common import (
    ASK_USER_TOOL_ID,
    CallId,
    CallRef,
    JsonValue,
    ProviderContinuation,
    TextBlock,
    UnknownOutcomePolicy,
    WorkspaceId,
    thaw,
)
from himari.control.api import ControlError, ControlErrorCode, DriveState
from himari.data.api import (
    AssistantToolCalls,
    CompletedGroupRef,
    DataError,
    EventRef,
    DataErrorCode,
    EventKind,
    RunStatus,
    ToolExecutionStatus,
    UserMessage,
)
from himari.prompt.api import AssistantTurn, ToolGroupTurn, UserTurn
from himari.provider.api import (
    NativeInvocationOutcome,
    NativeNoResponse,
    NativePreparedRequest,
    NativeReceived,
    NativeUncertain,
    NormalizedModelOutput,
    OutputSink,
    RawCapture,
)
from himari.provider.openrouter import OpenRouterAdapter
from himari.provider.scripted import RawBody, ScriptedAdapter
from himari.tool.dispatcher import UNKNOWN_OUTCOME_RESULT
from himari.bootstrap import build_runtime
from himari.tool.api import ConversationRecord, EffectProfile, ToolOutcome, ToolStatus, WorkspaceRequirement


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
    rt2, _ = runtime(store=restart(rt.store), provider=provider2)
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
    rt2, _ = runtime(store=restart(rt.store), provider=provider2)
    result = asyncio.run(rt2.driver.drive(SCOPE, user("hello again")))

    assert result.state is DriveState.COMPLETED
    assert result.recovery is not None and len(result.recovery.runs_outcome_unknown) == 1
    unknown_run = result.recovery.runs_outcome_unknown[0]
    assert asyncio.run(rt2.data.read_run(SCOPE, unknown_run)).status is RunStatus.OUTCOME_UNKNOWN
    assert len(provider2.sent) == 1 and unknown_run not in result.run_ids


def test_never_claimed_pending_calls_resume_exactly_once() -> None:
    read = RecordingTool("fake/read", environment_kind=FAKE, workspace=WorkspaceRequirement.DEFAULT)
    write = RecordingTool("fake/write", effect=EffectProfile.SCOPED_WRITE, environment_kind=FAKE,
                          workspace=WorkspaceRequirement.DEFAULT)
    read.crash_next = True  # dies before any claim in this group
    rt, _ = runtime([calls(("fake/read", {}), ("fake/write", {}))], env_tools=[read, write])
    rt.store.activate_environment(SCOPE, FAKE, WorkspaceId("ws_1"))
    with pytest.raises(SimulatedCrash):
        asyncio.run(rt.driver.drive(SCOPE, user("go")))

    rt2, provider2 = runtime([final("done")], store=restart(rt.store), env_tools=[read, write])
    result = asyncio.run(rt2.driver.resume(SCOPE))

    assert result.state is DriveState.COMPLETED
    assert len(read.invocations) == 2  # READ, never claimed: safe to run again
    assert len(write.invocations) == 1


def test_a_new_message_closes_never_started_calls_instead_of_running_them() -> None:
    # V3-P55: a new message redirects; the group left behind (by a pause or,
    # as here, a crash) is closed with CANCELLED results, never run.
    read = RecordingTool("fake/read", environment_kind=FAKE, workspace=WorkspaceRequirement.DEFAULT)
    write = RecordingTool("fake/write", effect=EffectProfile.SCOPED_WRITE, environment_kind=FAKE,
                          workspace=WorkspaceRequirement.DEFAULT)
    read.crash_next = True
    rt, _ = runtime([calls(("fake/read", {}), ("fake/write", {}))], env_tools=[read, write])
    rt.store.activate_environment(SCOPE, FAKE, WorkspaceId("ws_1"))
    with pytest.raises(SimulatedCrash):
        asyncio.run(rt.driver.drive(SCOPE, user("go")))

    rt2, provider2 = runtime([final("ok, something else")], store=restart(rt.store), env_tools=[read, write])
    result = asyncio.run(rt2.driver.drive(SCOPE, user("never mind, do something else")))

    assert result.state is DriveState.COMPLETED
    assert len(read.invocations) == 1 and write.invocations == []  # only the attempt before the crash
    (group, message) = provider2.prepared_inputs[-1].conversation[-2:]
    assert isinstance(group, ToolGroupTurn) and isinstance(message, UserTurn)
    assert all(r.is_error and "sent a new message" in r.blocks[0].text  # type: ignore[union-attr]
               for r in group.results.values())


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
        async def invoke_prepared(self, request: NativePreparedRequest, sink: OutputSink | None = None,
                                  capture: RawCapture | None = None) -> NativeInvocationOutcome:
            await gate.wait()
            return await super().invoke_prepared(request, sink, capture)

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

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))  # fail, never hang


def test_foreign_principal_cannot_drive() -> None:
    rt, provider = runtime([final("x")])
    with pytest.raises(DataError) as exc:
        asyncio.run(rt.driver.drive(FOREIGN, user("hi")))
    assert exc.value.code is DataErrorCode.NOT_AUTHORIZED
    assert provider.sent == []


# --- Review follow-ups: B3-1 stored-response recovery ------------------------


class CrashAfterReceive(ScriptedAdapter):
    """Dies right after the raw response was stored (RECEIVED), before projection."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.crashed = False

    def normalize(self, received: NativeReceived) -> NormalizedModelOutput:
        if not self.crashed:
            self.crashed = True
            raise SimulatedCrash("after RECEIVED")
        return super().normalize(received)


def test_stored_final_response_is_projected_after_restart() -> None:
    rt, provider = runtime(provider=CrashAfterReceive([final("first answer")]))
    with pytest.raises(SimulatedCrash):
        asyncio.run(rt.driver.drive(SCOPE, user("q1")))

    provider2 = ScriptedAdapter([final("second answer")])
    rt2, _ = runtime(store=restart(rt.store), provider=provider2)
    result = asyncio.run(rt2.driver.drive(SCOPE, user("q2")))

    assert result.state is DriveState.COMPLETED
    assert result.recovery is not None and len(result.recovery.runs_received_unprojected) == 1
    assert len(provider.sent) == 1 and len(provider2.sent) == 1  # no Provider call to recover
    kinds_ = [e.kind for e in asyncio.run(rt2.data.read_events(SCOPE))]
    assert kinds_ == [EventKind.USER_MESSAGE, EventKind.ASSISTANT_FINAL, EventKind.USER_MESSAGE, EventKind.ASSISTANT_FINAL]
    history = provider2.prepared_inputs[0].conversation
    assert [type(p).__name__ for p in history] == ["UserTurn", "AssistantTurn", "UserTurn"]


def test_stored_tool_calls_are_projected_and_run_once_after_restart() -> None:
    echo = RecordingTool("echo")
    rt, _ = runtime(provider=CrashAfterReceive([calls(("echo", {}))]), tools=[echo])
    with pytest.raises(SimulatedCrash):
        asyncio.run(rt.driver.drive(SCOPE, user("run echo")))
    assert echo.invocations == []

    provider2 = ScriptedAdapter([final("echo done")])
    rt2, _ = runtime(store=restart(rt.store), provider=provider2, tools=[echo])
    result = asyncio.run(rt2.driver.resume(SCOPE))

    assert result.state is DriveState.COMPLETED
    assert len(echo.invocations) == 1
    history = provider2.prepared_inputs[0].conversation
    assert [type(p).__name__ for p in history] == ["UserTurn", "ToolGroupTurn"]


def test_stored_response_from_another_provider_blocks_but_keeps_input() -> None:
    class OtherProvider(ScriptedAdapter):
        @property
        def provider_key(self) -> str:
            return "other"

    rt, _ = runtime(provider=CrashAfterReceive([final("x")]))
    with pytest.raises(SimulatedCrash):
        asyncio.run(rt.driver.drive(SCOPE, user("q1")))
    provider2 = OtherProvider([final("never")])
    rt2, _ = runtime(store=restart(rt.store), provider=provider2)
    result = asyncio.run(rt2.driver.drive(SCOPE, user("q2")))

    assert result.state is DriveState.RECOVERY_REQUIRED
    assert provider2.sent == []
    assert [e.kind for e in asyncio.run(rt2.data.read_events(SCOPE))][-1] is EventKind.USER_MESSAGE


# --- Review follow-ups: B3-2 activation gate, schema --------------------------


def test_environment_tool_without_workspace_is_gated_on_activation() -> None:
    ping = RecordingTool("fake/ping", environment_kind=FAKE)
    rt, provider = runtime(
        [calls(("fake/ping", {})), final("a"), calls(("fake/ping", {})), final("b"),
         calls(("fake/ping", {})), final("c")],
        env_tools=[ping],
    )
    asyncio.run(rt.driver.drive(SCOPE, user("never activated")))
    assert ping.invocations == []
    rt.store.activate_environment(SCOPE, FAKE, WorkspaceId("ws"))
    asyncio.run(rt.driver.drive(SCOPE, user("active")))
    assert len(ping.invocations) == 1
    rt.store.deactivate_environment(SCOPE, FAKE)
    asyncio.run(rt.driver.drive(SCOPE, user("deactivated")))
    assert len(ping.invocations) == 1
    group = provider.prepared_inputs[-1].conversation[-1]
    assert isinstance(group, ToolGroupTurn) and group.results[CallId("c0")].is_error


def test_arguments_failing_schema_are_denied_without_running() -> None:
    strict = RecordingTool("strict")
    strict.definition = dataclasses.replace(
        strict.definition,
        input_schema={"type": "object", "properties": {"n": {"type": "integer"}},
                      "required": ["n"], "additionalProperties": False},
    )
    rt, provider = runtime([calls(("strict", {"n": "1", "x": 1})), final("ok")], tools=[strict])
    asyncio.run(rt.driver.drive(SCOPE, user("go")))

    assert strict.invocations == []
    group = provider.prepared_inputs[-1].conversation[-1]
    assert isinstance(group, ToolGroupTurn)
    text = group.results[CallId("c0")].blocks[0]
    assert isinstance(text, TextBlock) and text.text == (
        "invalid arguments: $: Additional properties are not allowed ('x' was unexpected); "
        "$.n: '1' is not of type 'integer'"
    )


# --- Review follow-ups: resume without a user message --------------------------


def test_resume_after_uncertain_send_starts_a_new_run_for_the_same_input() -> None:
    rt, provider = runtime([NativeUncertain("timeout"), final("answer")])
    first = asyncio.run(rt.driver.drive(SCOPE, user("question")))
    assert first.state is DriveState.OUTCOME_UNKNOWN

    resumed = asyncio.run(rt.driver.resume(SCOPE))
    assert resumed.state is DriveState.COMPLETED
    assert len(provider.sent) == 2 and resumed.run_ids[0] != first.run_ids[0]
    user_events = [e for e in asyncio.run(rt.data.read_events(SCOPE)) if e.kind is EventKind.USER_MESSAGE]
    assert len(user_events) == 1  # no synthetic user message
    assert provider.prepared_inputs[1].trigger_refs == (EventRef(user_events[0].event_id),)


def test_resume_finishes_unclaimed_calls_then_continues() -> None:
    read = RecordingTool("fake/read", environment_kind=FAKE, workspace=WorkspaceRequirement.DEFAULT)
    read.crash_next = True
    rt, _ = runtime([calls(("fake/read", {}))], env_tools=[read])
    rt.store.activate_environment(SCOPE, FAKE, WorkspaceId("ws"))
    with pytest.raises(SimulatedCrash):
        asyncio.run(rt.driver.drive(SCOPE, user("go")))

    provider2 = ScriptedAdapter([final("done")])
    rt2, _ = runtime(store=restart(rt.store), provider=provider2, env_tools=[read])
    resumed = asyncio.run(rt2.driver.resume(SCOPE))

    assert resumed.state is DriveState.COMPLETED
    assert len(read.invocations) == 2
    (trigger,) = provider2.prepared_inputs[0].trigger_refs
    assert isinstance(trigger, CompletedGroupRef)


def test_resume_is_idle_after_final_and_waits_while_asking() -> None:
    rt, provider = runtime([final("hi"), calls((ASK_USER_TOOL_ID, {"question": "?"}))])
    asyncio.run(rt.driver.drive(SCOPE, user("hello")))
    assert asyncio.run(rt.driver.resume(SCOPE)).state is DriveState.IDLE
    asyncio.run(rt.driver.drive(SCOPE, user("ask me")))
    waiting = asyncio.run(rt.driver.resume(SCOPE))
    assert waiting.state is DriveState.WAITING_FOR_USER and waiting.waiting_call is not None
    assert len(provider.sent) == 2


# --- Re-review B2: raw tool result is preserved, never shown to the model ------


def test_raw_tool_result_is_stored_separately_from_model_result() -> None:
    raw: dict[str, JsonValue] = {"exit_code": 0, "stdout": "RAW-ONLY-MARKER", "argv": ["ls", "-la"]}
    tool = RecordingTool(
        "lister",
        behavior=lambda inv, ctx: ToolOutcome(ToolStatus.SUCCEEDED, text_result("2 files"), raw_result=raw),
    )
    rt, provider = runtime([calls(("lister", {})), final("ok")], tools=[tool])
    result = asyncio.run(rt.driver.drive(SCOPE, user("list")))
    raw["stdout"] = "mutated after return"  # the record must not follow

    (record,) = asyncio.run(rt.data.read_tool_executions(SCOPE, result.run_ids[0]))
    assert record.model_result == text_result("2 files")
    assert thaw(record.raw_result) == {"exit_code": 0, "stdout": "RAW-ONLY-MARKER", "argv": ["ls", "-la"]}
    assert all(b"RAW-ONLY-MARKER" not in sent.body for sent in provider.sent)


# --- V3-047: uncertain effects are reported to the model, never re-run --------


def writer(
    policy: UnknownOutcomePolicy = UnknownOutcomePolicy.REPORT_TO_MODEL, name: str = "write"
) -> RecordingTool:
    tool = RecordingTool(f"fake/{name}", effect=EffectProfile.UNKNOWN, environment_kind=FAKE,
                         workspace=WorkspaceRequirement.DEFAULT)
    tool.definition = dataclasses.replace(tool.definition, unknown_outcome=policy)
    return tool


def test_crash_during_effectful_tool_is_reported_to_model_after_restart() -> None:
    write = writer()
    write.crash_next = True
    rt, _ = runtime([calls(("fake/write", {}))], env_tools=[write])
    rt.store.activate_environment(SCOPE, FAKE, WorkspaceId("ws_1"))
    with pytest.raises(SimulatedCrash):
        asyncio.run(rt.driver.drive(SCOPE, user("write")))

    rt2, provider2 = runtime([final("I checked; it was applied")], store=restart(rt.store), env_tools=[write])
    result = asyncio.run(rt2.driver.drive(SCOPE, user("continue")))

    assert result.state is DriveState.COMPLETED
    assert result.recovery is not None and len(result.recovery.tools_unknown) == 1
    assert len(write.invocations) == 1  # the runtime never re-runs it
    call = result.recovery.tools_unknown[0]
    (record,) = asyncio.run(rt2.data.read_tool_executions(SCOPE, call.run_id))
    assert record.status is ToolExecutionStatus.UNKNOWN  # still truthfully unknown
    assert record.model_result == UNKNOWN_OUTCOME_RESULT
    group = provider2.prepared_inputs[0].conversation[1]
    assert isinstance(group, ToolGroupTurn) and group.results[CallId("c0")] == UNKNOWN_OUTCOME_RESULT


def test_crash_during_external_tool_is_unknown_and_never_rerun() -> None:
    """V3-P52: an External Tool (e.g. MCP) with effects is claimed like an
    Environment Tool, so a crash mid-call is never silently re-run on restart.
    It needs no active Environment or Workspace."""
    send = RecordingTool("remote/send", effect=EffectProfile.UNKNOWN, external_source="remote")
    send.crash_next = True
    rt, _ = runtime([calls(("remote/send", {}))], tools=[send])
    with pytest.raises(SimulatedCrash):
        asyncio.run(rt.driver.drive(SCOPE, user("send")))

    rt2, provider2 = runtime([final("I checked; it was sent")], store=restart(rt.store), tools=[send])
    result = asyncio.run(rt2.driver.drive(SCOPE, user("continue")))

    assert result.state is DriveState.COMPLETED
    assert result.recovery is not None and len(result.recovery.tools_unknown) == 1
    assert len(send.invocations) == 1  # never re-run
    group = provider2.prepared_inputs[0].conversation[1]
    assert isinstance(group, ToolGroupTurn) and group.results[CallId("c0")] == UNKNOWN_OUTCOME_RESULT


def test_external_tool_arguments_are_validated_in_a_killable_child(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR #19 review B1: a pathological pattern from an external server must
    not stall the server; the call is denied and the handler never runs."""
    monkeypatch.setattr("himari.tool.schema.ISOLATED_SECONDS", 1.0)
    remote = RecordingTool("remote/match", external_source="remote")
    remote.definition = dataclasses.replace(
        remote.definition,
        input_schema={"type": "object", "properties": {"s": {"type": "string", "pattern": "^(a+)+$"}}},
    )
    rt, provider = runtime([calls(("remote/match", {"s": "a" * 60 + "!"})), final("ok")], tools=[remote])
    started = time.monotonic()
    result = asyncio.run(rt.driver.drive(SCOPE, user("go")))

    assert result.state is DriveState.COMPLETED and time.monotonic() - started < 5
    assert remote.invocations == []
    group = provider.prepared_inputs[-1].conversation[-1]
    assert isinstance(group, ToolGroupTurn)
    assert group.results[CallId("c0")].blocks == (TextBlock("invalid arguments: $: validating the arguments took too long"),)


def test_live_exception_after_claim_is_unknown_not_failed_and_loop_continues() -> None:
    write = writer()
    write.raise_next = RuntimeError("connection reset mid-command")
    rt, provider = runtime([calls(("fake/write", {})), final("will verify")], env_tools=[write])
    rt.store.activate_environment(SCOPE, FAKE, WorkspaceId("ws_1"))
    result = asyncio.run(rt.driver.drive(SCOPE, user("write")))

    assert result.state is DriveState.COMPLETED and len(provider.sent) == 2
    (record,) = asyncio.run(rt.data.read_tool_executions(SCOPE, result.run_ids[0]))
    assert record.status is ToolExecutionStatus.UNKNOWN
    assert record.model_result == UNKNOWN_OUTCOME_RESULT  # no exception text, no fake failure
    assert len(write.invocations) == 1


def test_model_may_verify_then_retry_with_a_new_call() -> None:
    write = writer()
    check = RecordingTool("fake/check", environment_kind=FAKE, workspace=WorkspaceRequirement.DEFAULT)
    write.raise_next = RuntimeError("lost")
    rt, provider = runtime(
        [calls(("fake/write", {})), calls(("fake/check", {})), calls(("fake/write", {})), final("done")],
        env_tools=[write, check],
    )
    rt.store.activate_environment(SCOPE, FAKE, WorkspaceId("ws_1"))
    result = asyncio.run(rt.driver.drive(SCOPE, user("write")))

    assert result.state is DriveState.COMPLETED
    assert len(check.invocations) == 1 and len(write.invocations) == 2  # retry was the model's new call
    first, retry = (asyncio.run(rt.data.read_tool_executions(SCOPE, r)) for r in (result.run_ids[0], result.run_ids[2]))
    assert first[0].status is ToolExecutionStatus.UNKNOWN
    assert retry[0].status is ToolExecutionStatus.SUCCEEDED


@pytest.mark.parametrize("crash", [True, False])
def test_require_user_policy_still_blocks(crash: bool) -> None:
    write = writer(UnknownOutcomePolicy.REQUIRE_USER)
    if crash:
        write.crash_next = True
    else:
        write.raise_next = RuntimeError("lost")
    rt, provider = runtime([calls(("fake/write", {})), final("never")], env_tools=[write])
    rt.store.activate_environment(SCOPE, FAKE, WorkspaceId("ws_1"))
    if crash:
        with pytest.raises(SimulatedCrash):
            asyncio.run(rt.driver.drive(SCOPE, user("write")))
        rt, provider = runtime([final("never")], store=restart(rt.store), env_tools=[write])
        result = asyncio.run(rt.driver.drive(SCOPE, user("continue")))
    else:
        result = asyncio.run(rt.driver.drive(SCOPE, user("write")))

    assert result.state is DriveState.RECOVERY_REQUIRED and len(result.unknown_calls) == 1
    assert len(write.invocations) == 1
    assert len(provider.sent) == (0 if crash else 1)  # no Run with an incomplete group
    (record,) = asyncio.run(rt.data.read_tool_executions(SCOPE, result.unknown_calls[0].run_id))
    assert record.model_result is None


# --- PR #4 review: REQUIRE_USER isolation (B1) and claim-time policy (B2) ------


def test_require_user_uncertainty_halts_the_rest_of_the_group_live() -> None:
    pay, confirm = writer(UnknownOutcomePolicy.REQUIRE_USER, "pay"), writer(name="confirm")
    pay.raise_next = RuntimeError("network")
    rt, _ = runtime([calls(("fake/pay", {}), ("fake/confirm", {}))], env_tools=[pay, confirm])
    rt.store.activate_environment(SCOPE, FAKE, WorkspaceId("ws"))
    result = asyncio.run(rt.driver.drive(SCOPE, user("buy")))

    assert result.state is DriveState.RECOVERY_REQUIRED
    assert confirm.invocations == []  # no further effect after the blocking uncertainty
    records = asyncio.run(rt.data.read_tool_executions(SCOPE, result.run_ids[0]))
    assert [r.status for r in records] == [ToolExecutionStatus.EXECUTION_CLAIMED, ToolExecutionStatus.PENDING]
    assert result.unknown_calls == (records[0].call,)


def test_require_user_uncertainty_halts_the_rest_of_the_group_after_restart() -> None:
    pay, confirm = writer(UnknownOutcomePolicy.REQUIRE_USER, "pay"), writer(name="confirm")
    pay.crash_next = True
    rt, _ = runtime([calls(("fake/pay", {}), ("fake/confirm", {}))], env_tools=[pay, confirm])
    rt.store.activate_environment(SCOPE, FAKE, WorkspaceId("ws"))
    with pytest.raises(SimulatedCrash):
        asyncio.run(rt.driver.drive(SCOPE, user("buy")))

    store = rt.store
    for entry in ("drive", "resume"):
        store = restart(store)  # each attempt is another process start
        rt2, provider2 = runtime([final("never")], store=store, env_tools=[pay, confirm])
        result = (
            asyncio.run(rt2.driver.drive(SCOPE, user("again")))
            if entry == "drive"
            else asyncio.run(rt2.driver.resume(SCOPE))
        )
        assert result.state is DriveState.RECOVERY_REQUIRED, entry
        assert confirm.invocations == [] and provider2.sent == [], entry


@pytest.mark.parametrize(
    "new_build_tools",
    [
        pytest.param([], id="tool-removed"),
        pytest.param([writer(UnknownOutcomePolicy.REPORT_TO_MODEL, "pay")], id="policy-relaxed"),
    ],
)
def test_claim_time_policy_survives_redeploy(new_build_tools: list[RecordingTool]) -> None:
    pay = writer(UnknownOutcomePolicy.REQUIRE_USER, "pay")
    pay.crash_next = True
    rt, _ = runtime([calls(("fake/pay", {}))], env_tools=[pay])
    rt.store.activate_environment(SCOPE, FAKE, WorkspaceId("ws"))
    with pytest.raises(SimulatedCrash):
        asyncio.run(rt.driver.drive(SCOPE, user("buy")))

    rt2, provider2 = runtime([final("never")], store=restart(rt.store), env_tools=new_build_tools)
    result = asyncio.run(rt2.driver.drive(SCOPE, user("again")))

    assert result.state is DriveState.RECOVERY_REQUIRED
    assert provider2.sent == []
    (record,) = asyncio.run(rt2.data.read_tool_executions(SCOPE, result.unknown_calls[0].run_id))
    assert record.unknown_outcome is UnknownOutcomePolicy.REQUIRE_USER and record.model_result is None


def test_report_to_model_survives_tool_removal() -> None:
    write = writer()
    write.crash_next = True
    rt, _ = runtime([calls(("fake/write", {}))], env_tools=[write])
    rt.store.activate_environment(SCOPE, FAKE, WorkspaceId("ws"))
    with pytest.raises(SimulatedCrash):
        asyncio.run(rt.driver.drive(SCOPE, user("write")))
    rt2, _ = runtime([final("checked")], store=restart(rt.store))  # Tool no longer registered
    assert asyncio.run(rt2.driver.drive(SCOPE, user("again"))).state is DriveState.COMPLETED


def test_uncertain_record_with_unknown_policy_blocks_cleanly() -> None:
    """A record whose claim-time policy is unknown (e.g. written before
    V3-047) is never relaxed into a model report: it blocks, without errors."""
    write = writer()
    write.crash_next = True
    rt, _ = runtime([calls(("fake/write", {}))], env_tools=[write])
    rt.store.activate_environment(SCOPE, FAKE, WorkspaceId("ws"))
    with pytest.raises(SimulatedCrash):
        asyncio.run(rt.driver.drive(SCOPE, user("write")))
    (ref,) = forget_unknown_policy(rt.store)

    rt2, provider2 = runtime([final("never")], store=restart(rt.store), env_tools=[write])
    result = asyncio.run(rt2.driver.drive(SCOPE, user("again")))

    assert result.state is DriveState.RECOVERY_REQUIRED and result.unknown_calls == (ref,)
    assert provider2.sent == []



# --- PR #4 follow-ups: C1 claim-time record policy, C2 REQUIRE_USER gate ------


@pytest.mark.parametrize("new_build", ["tool-removed", "policy-changed-to-full"])
def test_unknown_result_event_follows_claim_time_record_policy(new_build: str) -> None:
    quiet = writer()
    quiet.definition = dataclasses.replace(quiet.definition, conversation_record=ConversationRecord.OMIT)
    quiet.crash_next = True
    rt, _ = runtime([calls(("fake/write", {}))], env_tools=[quiet])
    rt.store.activate_environment(SCOPE, FAKE, WorkspaceId("ws"))
    with pytest.raises(SimulatedCrash):
        asyncio.run(rt.driver.drive(SCOPE, user("write")))

    loud = writer()  # same tool_id, conversation_record=FULL in the new build
    env_tools = [] if new_build == "tool-removed" else [loud]
    rt2, provider2 = runtime([final("checked")], store=restart(rt.store), env_tools=env_tools)
    assert asyncio.run(rt2.driver.drive(SCOPE, user("again"))).state is DriveState.COMPLETED

    kinds_ = [e.kind for e in asyncio.run(rt2.data.read_events(SCOPE))]
    assert EventKind.TOOL_RESULT not in kinds_  # OMIT applied at claim time still holds
    group = provider2.prepared_inputs[0].conversation[1]
    assert isinstance(group, ToolGroupTurn) and group.results[CallId("c0")] == UNKNOWN_OUTCOME_RESULT


def test_p0_runtime_refuses_require_user_tools_by_default() -> None:
    pay = writer(UnknownOutcomePolicy.REQUIRE_USER, "pay")
    with pytest.raises(ValueError, match="REQUIRE_USER Tools are disabled"):
        build_runtime(ScriptedAdapter(), environments=[FakeEnvironment(pay)])
    build_runtime(ScriptedAdapter(), environments=[FakeEnvironment(pay)], allow_require_user_tools=True)



# --- PR #5 review: provider error bodies and malformed stored responses --------


def openrouter_runtime(handler: Any, store: Any = None) -> Any:
    rt, _ = runtime(
        store=store,
        provider=OpenRouterAdapter("test-key", transport=httpx.MockTransport(handler)),  # type: ignore[arg-type]
    )
    return rt


OK = {"choices": [{"message": {"role": "assistant", "content": "fine"}, "finish_reason": "stop"}]}


def test_provider_error_response_body_is_kept_on_the_failed_run() -> None:
    error = {"error": {"code": 429, "message": "rate limited"}}
    rt = openrouter_runtime(lambda r: httpx.Response(429, json=error))
    result = asyncio.run(rt.driver.drive(SCOPE, user("hi")))

    assert result.state is DriveState.PROVIDER_FAILED and result.detail == "http_429"
    run = asyncio.run(rt.data.read_run(SCOPE, result.run_ids[0]))
    assert run.status is RunStatus.FAILED_NO_RESPONSE
    assert json.loads(asyncio.run(rt.data.read_raw_response(SCOPE, result.run_ids[0])).body) == error


def test_error_finish_with_tool_calls_never_runs_the_tool() -> None:
    echo = RecordingTool("echo")
    doc = {"choices": [{"finish_reason": "error", "message": {"content": None, "tool_calls": [
        {"id": "c", "type": "function", "function": {"name": "echo", "arguments": "{}"}}]}}]}
    rt, _ = runtime(tools=[echo], provider=OpenRouterAdapter(  # type: ignore[arg-type]
        "k", transport=httpx.MockTransport(lambda r: httpx.Response(200, json=doc))))
    result = asyncio.run(rt.driver.drive(SCOPE, user("go")))

    assert result.state is DriveState.INVALID_MODEL_OUTPUT and "finish_reason='error'" in result.detail
    assert echo.invocations == []
    assert asyncio.run(rt.data.read_run(SCOPE, result.run_ids[0])).status is RunStatus.PROJECTION_FAILED


def test_malformed_stored_response_is_handled_on_restart() -> None:
    class CrashOnFirstNormalize(OpenRouterAdapter):
        crashed = False

        def normalize(self, received: NativeReceived) -> NormalizedModelOutput:
            if not CrashOnFirstNormalize.crashed:
                CrashOnFirstNormalize.crashed = True
                raise SimulatedCrash("after RECEIVED")
            return super().normalize(received)

    bad = {"choices": [{"message": None, "finish_reason": "stop"}]}
    first, _ = runtime(provider=CrashOnFirstNormalize(  # type: ignore[arg-type]
        "k", transport=httpx.MockTransport(lambda r: httpx.Response(200, json=bad))))
    with pytest.raises(SimulatedCrash):
        asyncio.run(first.driver.drive(SCOPE, user("q1")))

    rt2 = openrouter_runtime(lambda r: httpx.Response(200, json=OK), store=restart(first.store))
    result = asyncio.run(rt2.driver.drive(SCOPE, user("q2")))  # no AttributeError loop

    assert result.state is DriveState.COMPLETED
    assert result.recovery is not None
    (stuck,) = result.recovery.runs_received_unprojected
    assert asyncio.run(rt2.data.read_run(SCOPE, stuck)).status is RunStatus.PROJECTION_FAILED


# --- PR B: continuation flows from the producing Run to the next request ------


def test_continuation_is_stored_on_its_run_and_attached_to_its_turn() -> None:
    first_call = dataclasses.replace(calls(("echo", {})), continuation={"reasoning_details": [{"sig": "A"}]})
    answer = dataclasses.replace(final("done"), continuation={"reasoning": "B"})
    rt, provider = runtime([first_call, answer, final("again")], tools=[RecordingTool("echo")])
    asyncio.run(rt.driver.drive(SCOPE, user("go")))
    asyncio.run(rt.driver.drive(SCOPE, user("next")))

    run2_input, run3_input = provider.prepared_inputs[1], provider.prepared_inputs[2]
    group = run2_input.conversation[1]
    assert isinstance(group, ToolGroupTurn)
    assert group.continuation == ProviderContinuation("scripted", "scripted-model", {"reasoning_details": [{"sig": "A"}]})
    turns = [p for p in run3_input.conversation if isinstance(p, AssistantTurn)]
    assert [t.continuation for t in turns] == [ProviderContinuation("scripted", "scripted-model", {"reasoning": "B"})]
    assert [p.continuation for p in run3_input.conversation if isinstance(p, ToolGroupTurn)] == [group.continuation]


def test_continuation_survives_restart_projection_of_a_stored_response() -> None:
    with_continuation = dataclasses.replace(calls(("echo", {})), continuation={"reasoning_details": [{"sig": "C"}]})
    rt, _ = runtime(provider=CrashAfterReceive([with_continuation]), tools=[RecordingTool("echo")])
    with pytest.raises(SimulatedCrash):
        asyncio.run(rt.driver.drive(SCOPE, user("go")))

    provider2 = ScriptedAdapter([final("done")])
    rt2, _ = runtime(store=restart(rt.store), provider=provider2, tools=[RecordingTool("echo")])
    asyncio.run(rt2.driver.drive(SCOPE, user("status")))

    group = provider2.prepared_inputs[0].conversation[1]
    assert isinstance(group, ToolGroupTurn)
    assert group.continuation == ProviderContinuation("scripted", "scripted-model", {"reasoning_details": [{"sig": "C"}]})


@pytest.mark.parametrize("details", [[], [{"type": "reasoning.text", "text": "t", "signature": "s"}]])
def test_reasoning_details_survive_the_full_path_into_the_next_request(details: list[Any]) -> None:
    """normalize → Data (RunRecord) → Prompt (turn) → prepare: the follow-up
    request carries exactly what the model returned — including an explicit
    empty array, which some models require (PR #6 review B1)."""
    tool_turn = {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None, "reasoning_details": details,
        "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "echo", "arguments": "{}"}}]}}]}
    responses = iter([tool_turn, OK])
    sent: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.content)
        return httpx.Response(200, json=next(responses))

    rt, _ = runtime(tools=[RecordingTool("echo")],
                    provider=OpenRouterAdapter("k", transport=httpx.MockTransport(handler)))  # type: ignore[arg-type]
    result = asyncio.run(rt.driver.drive(SCOPE, user("go")))

    assert result.state is DriveState.COMPLETED and len(sent) == 2
    stored = asyncio.run(rt.data.read_run(SCOPE, result.run_ids[0])).continuation
    assert stored is not None and thaw(stored.payload) == {"reasoning_details": details}
    assistant = next(m for m in json.loads(sent[1])["messages"] if m.get("tool_calls"))
    assert assistant["reasoning_details"] == details


# --- PR #12 review B1: accept + resume ----------------------------------------------------


def test_input_accepted_after_an_unfinished_group_closes_it_and_triggers_the_run() -> None:
    read = RecordingTool("fake/read", environment_kind=FAKE, workspace=WorkspaceRequirement.DEFAULT)
    read.crash_next = True  # the group stays PENDING
    rt, _ = runtime([calls(("fake/read", {}))], env_tools=[read])
    rt.store.activate_environment(SCOPE, FAKE, WorkspaceId("ws_1"))
    with pytest.raises(SimulatedCrash):
        asyncio.run(rt.driver.drive(SCOPE, user("go")))

    provider2 = ScriptedAdapter([final("done")])
    rt2, _ = runtime(store=restart(rt.store), provider=provider2, env_tools=[read])
    asyncio.run(rt2.driver.accept(SCOPE, user("and also this")))  # V3-P55: redirects, the call is closed
    result = asyncio.run(rt2.driver.resume(SCOPE))

    assert result.state is DriveState.COMPLETED and len(read.invocations) == 1
    run = asyncio.run(rt2.data.read_run(SCOPE, result.run_ids[0]))
    accepted = [e for e in asyncio.run(rt2.data.read_events(SCOPE)) if e.payload == user("and also this")][0]
    assert EventRef(accepted.event_id) in run.trigger_refs
    assert any(isinstance(t, CompletedGroupRef) for t in run.trigger_refs)
    conversation = provider2.prepared_inputs[0].conversation
    assert isinstance(conversation[-2], ToolGroupTurn) and isinstance(conversation[-1], UserTurn)  # results stay with their group
