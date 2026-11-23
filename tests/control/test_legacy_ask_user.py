"""V3-P106: ``ask_user`` is gone. A question an older server left open is a
never-started call of the latest group: the next message closes it as
CANCELLED (the redirect of V3-P55) and the turn goes on; a resume leaves it
and starts nothing."""

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


def test_a_resume_leaves_it_for_the_user_and_starts_nothing() -> None:
    """Codex review of V3-P106: Continue on an older wake's question must not run a Run
    outside the wake's own Tool rules; the question is the user's to answer."""
    async def scenario() -> None:
        rt, adapter = runtime([final("Seoul it is.")])
        question = await legacy_question(rt)
        for _ in range(2):
            assert (await rt.driver.resume(SCOPE)).state is DriveState.IDLE
        (record,) = await rt.data.read_tool_executions(SCOPE, question.run_id)
        assert record.status is ToolExecutionStatus.PENDING
        assert adapter.prepared_inputs == []
        assert (await rt.driver.drive(SCOPE, user("Seoul"))).state is DriveState.COMPLETED  # the answer closes it
        (record,) = await rt.data.read_tool_executions(SCOPE, question.run_id)
        assert record.status is ToolExecutionStatus.CANCELLED

    asyncio.run(scenario())
