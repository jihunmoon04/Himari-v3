"""V3-P106: ``ask_user`` is gone. A question an older server left open is a
never-started call of the latest group: the next message closes it as
CANCELLED (the redirect of V3-P55) and the turn goes on; a cancel closes it
too; a resume runs it, and the Dispatcher refuses it as an unknown Tool."""

from __future__ import annotations

import asyncio

from testkit import SCOPE, final, runtime, user

from himari.bootstrap import Runtime
from himari.common import CallId, CallRef, RawRepresentation, ToolCall, ToolId
from himari.control.api import DriveState
from himari.control.driver import REDIRECTED_RESULT
from himari.control.runner import BackgroundRunner
from himari.data.api import (AssistantToolCalls, PromptBasis, RawReceived, RunPreparation, ToolExecutionStatus,
                             UserMessage)
from himari.prompt.api import ToolGroupTurn, UserTurn


async def legacy_question(rt: Runtime) -> CallRef:
    """What an older server left: the model asked with ``ask_user``, and nobody answered."""
    data = rt.data
    await data.append_event(SCOPE, user("plan my trip"))
    run_id = await data.prepare_run(SCOPE, RunPreparation(b"{}", "application/json", RawRepresentation.ADAPTER_SERIALIZED,
                                                          (), PromptBasis(0, 0), "scripted", "scripted-model"))
    await data.mark_dispatch(SCOPE, run_id)
    await data.record_raw_outcome(SCOPE, run_id, RawReceived(b"{}", "application/json"))
    asked = ToolCall(CallId("q"), 0, ToolId("ask_user"), {"question": "Which city?"})
    await data.project_model_output(SCOPE, run_id, AssistantToolCalls((asked,)))
    return CallRef(run_id, CallId("q"))


def test_the_next_message_closes_an_open_question_and_the_turn_goes_on() -> None:
    async def scenario() -> None:
        rt, adapter = runtime([final("Seoul it is.")])
        question = await legacy_question(rt)
        runner = BackgroundRunner(rt.driver)
        await runner.submit(SCOPE, user("Seoul"))  # accept, then resume: as the Web and the app send
        status = await runner.wait(SCOPE)

        assert status.result is not None and status.result.state is DriveState.COMPLETED
        (record,) = await rt.data.read_tool_executions(SCOPE, question.run_id)
        assert record.status is ToolExecutionStatus.CANCELLED
        assert record.model_result is not None and record.model_result.blocks[0].text == REDIRECTED_RESULT  # type: ignore[union-attr]
        group, message = adapter.prepared_inputs[-1].conversation[-2:]
        assert isinstance(group, ToolGroupTurn) and [c.tool_id for c in group.calls] == ["ask_user"]
        assert group.results[CallId("q")].is_error
        assert isinstance(message, UserTurn)  # an ordinary message, not an answer
        messages = [e.payload for e in await rt.data.read_events(SCOPE) if isinstance(e.payload, UserMessage)]
        assert all(m.answers_call is None for m in messages)

    asyncio.run(scenario())


def test_a_drive_closes_it_too() -> None:
    async def scenario() -> None:
        rt, _ = runtime([final("ok")])
        question = await legacy_question(rt)
        assert (await rt.driver.drive(SCOPE, user("never mind"))).state is DriveState.COMPLETED
        (record,) = await rt.data.read_tool_executions(SCOPE, question.run_id)
        assert record.status is ToolExecutionStatus.CANCELLED

    asyncio.run(scenario())


def test_a_cancel_closes_it_and_a_resume_refuses_it_as_an_unknown_tool() -> None:
    async def scenario() -> None:
        rt, _ = runtime()
        question = await legacy_question(rt)
        assert (await rt.driver.cancel(SCOPE)).state is DriveState.CANCELLED
        (record,) = await rt.data.read_tool_executions(SCOPE, question.run_id)
        assert record.status is ToolExecutionStatus.CANCELLED

        rt2, adapter = runtime([final("I'll just ask: which city?")])
        question = await legacy_question(rt2)
        assert (await rt2.driver.resume(SCOPE)).state is DriveState.COMPLETED
        (record,) = await rt2.data.read_tool_executions(SCOPE, question.run_id)
        assert record.status is ToolExecutionStatus.DENIED and record.model_result is not None
        assert record.model_result.blocks[0].text == "unknown tool ask_user"  # type: ignore[union-attr]
        assert "ask_user" not in {str(d.tool_id) for d in adapter.prepared_inputs[-1].tool_catalog}

    asyncio.run(scenario())
