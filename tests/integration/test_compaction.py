"""Context compaction end to end (PROMPT-04, Q-14) on both Data backends."""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable, Coroutine
from typing import Any

import pytest
from testkit import FAKE, SCOPE, RecordingTool, calls, final, restart, runtime, text_result, user

from himari.common import TextBlock
from himari.control.api import DriveState
from himari.data.api import RunPurpose, RunStatus
from himari.prompt.api import CompressedHistory, ContextBudget, ToolGroupTurn, UserTurn
from himari.prompt.compaction import COMPACTION_INSTRUCTIONS
from himari.provider.api import NativeNoResponse
from himari.provider.scripted import Measured
from himari.tool.api import ToolOutcome, ToolStatus

BUDGET = ContextBudget(limit_tokens=1000)


def sync(case: Callable[..., Coroutine[Any, Any, None]]) -> Callable[..., None]:
    @functools.wraps(case)
    def run(*args: Any, **kwargs: Any) -> None:
        asyncio.run(case(*args, **kwargs))

    return run


def _is_compaction(model_input: object) -> bool:
    return getattr(model_input, "stable_prefix", ()) == (COMPACTION_INSTRUCTIONS,)


@sync
async def test_below_the_threshold_nothing_is_compacted() -> None:
    rt, adapter = runtime([Measured(final("one"), 100), final("two")], context_budget=BUDGET)
    await rt.driver.drive(SCOPE, user("hi"))
    result = await rt.driver.drive(SCOPE, user("again"))
    assert result.state is DriveState.COMPLETED and result.compaction_run_ids == ()
    assert not any(_is_compaction(i) for i in adapter.prepared_inputs)
    assert [type(p) for p in adapter.prepared_inputs[-1].conversation][0] is UserTurn


@sync
async def test_a_measured_size_over_the_threshold_compacts_everything_but_the_tail() -> None:
    rt, adapter = runtime(
        [Measured(final("first answer"), 900), final("SUMMARY of the start"), final("second answer")],
        context_budget=BUDGET,
    )
    await rt.driver.drive(SCOPE, user("first question"))
    result = await rt.driver.drive(SCOPE, user("second question"))

    assert result.state is DriveState.COMPLETED
    assert len(result.compaction_run_ids) == 1 and len(result.run_ids) == 1
    summarizer, turn = adapter.prepared_inputs[-2], adapter.prepared_inputs[-1]
    assert _is_compaction(summarizer) and summarizer.tool_catalog == ()
    (request,) = summarizer.conversation
    assert isinstance(request, UserTurn)
    text = request.blocks[0].text  # type: ignore[union-attr]
    assert "first question" in text and "first answer" in text and "second question" not in text

    head, *tail = turn.conversation
    assert isinstance(head, CompressedHistory) and head.summary == (TextBlock("SUMMARY of the start"),)
    assert [p.blocks for p in tail if isinstance(p, UserTurn)] == [(TextBlock("second question"),)]
    assert len(tail) == 1
    assert turn.prompt_basis.snapshot_revision == 1

    run = await rt.data.read_run(SCOPE, result.compaction_run_ids[0])
    assert run.purpose is RunPurpose.COMPACTION and run.status is RunStatus.PROJECTED
    snapshot = await rt.data.get_latest_snapshot(SCOPE)
    assert snapshot is not None and snapshot.body == {"summary": "SUMMARY of the start"}
    # Exact history is untouched: every Event is still there.
    texts = [e.payload.blocks[0].text for e in await rt.data.read_events(SCOPE)]  # type: ignore[union-attr]
    assert texts == ["first question", "first answer", "second question", "second answer"]


@sync
async def test_without_usage_the_utf8_estimate_triggers_compaction() -> None:
    big = "가" * 900  # 2700 bytes → 900 tokens at 3 bytes per token
    rt, adapter = runtime([final("ok"), final("SUMMARY"), final("done")], context_budget=BUDGET)
    await rt.driver.drive(SCOPE, user(big))
    result = await rt.driver.drive(SCOPE, user("next"))
    assert result.state is DriveState.COMPLETED and len(result.compaction_run_ids) == 1
    assert isinstance(adapter.prepared_inputs[-1].conversation[0], CompressedHistory)


@sync
async def test_the_next_compaction_folds_the_previous_summary_and_moves_forward() -> None:
    rt, adapter = runtime(
        [
            Measured(final("a1"), 900), final("S1"), Measured(final("a2"), 100),
            Measured(final("a3"), 950), final("S2"), final("a4"),
        ],
        context_budget=BUDGET,
    )
    for text in ("q1", "q2", "q3", "q4"):
        await rt.driver.drive(SCOPE, user(text))
    second = [i for i in adapter.prepared_inputs if _is_compaction(i)][1]
    payload = second.conversation[0].blocks[0].text  # type: ignore[union-attr]
    assert '"previous_summary": "S1"' in payload and "q3" in payload and "q4" not in payload
    snapshot = await rt.data.get_latest_snapshot(SCOPE)
    assert snapshot is not None and snapshot.revision == 2 and snapshot.body == {"summary": "S2"}
    head, *tail = adapter.prepared_inputs[-1].conversation
    assert isinstance(head, CompressedHistory) and head.summary == (TextBlock("S2"),)
    assert [p.blocks[0].text for p in tail if isinstance(p, UserTurn)] == ["q4"]  # type: ignore[union-attr]


@sync
async def test_a_measurement_after_the_compaction_is_used_again() -> None:
    rt, adapter = runtime(
        [Measured(final("a1"), 900), final("S1"), Measured(final("a2"), 100), final("a3")],
        context_budget=BUDGET,
    )
    for text in ("q1", "q2", "q3"):
        result = await rt.driver.drive(SCOPE, user(text))
    assert result.compaction_run_ids == ()  # measured 100 after the compaction: far below 800


@sync
async def test_a_tool_group_is_never_split_from_its_results() -> None:
    tool = RecordingTool("fake/probe", environment_kind=FAKE,
                         behavior=lambda i, c: ToolOutcome(ToolStatus.SUCCEEDED, text_result("x" * 1500)))
    rt, adapter = runtime(
        [calls(("fake/probe", {})), Measured(final("looked"), 900), final("S"), final("done")],
        env_tools=[tool], context_budget=BUDGET,
    )
    await rt.driver.drive(SCOPE, user("look"))
    await rt.driver.drive(SCOPE, user("and now?"))
    summarizer = next(i for i in adapter.prepared_inputs if _is_compaction(i))
    text = summarizer.conversation[0].blocks[0].text  # type: ignore[union-attr]
    assert '"tool_calls"' in text and '"role": "tool"' in text
    tail = adapter.prepared_inputs[-1].conversation[1:]
    assert not any(isinstance(p, ToolGroupTurn) for p in tail)


@sync
async def test_a_failed_compaction_still_runs_a_request_that_fits() -> None:
    rt, adapter = runtime(
        [Measured(final("a1"), 850), NativeNoResponse("http_500"), final("a2")], context_budget=BUDGET
    )
    await rt.driver.drive(SCOPE, user("q1"))
    result = await rt.driver.drive(SCOPE, user("q2"))
    assert result.state is DriveState.COMPLETED and len(result.compaction_run_ids) == 1
    run = await rt.data.read_run(SCOPE, result.compaction_run_ids[0])
    assert run.status is RunStatus.FAILED_NO_RESPONSE
    assert await rt.data.get_latest_snapshot(SCOPE) is None
    assert not isinstance(adapter.prepared_inputs[-1].conversation[0], CompressedHistory)


@sync
async def test_a_request_over_the_limit_is_never_sent() -> None:
    rt, adapter = runtime([Measured(final("a1"), 1200), NativeNoResponse("http_500")], context_budget=BUDGET)
    await rt.driver.drive(SCOPE, user("q1"))
    sent = len(adapter.sent)
    result = await rt.driver.drive(SCOPE, user("q2"))
    assert result.state is DriveState.REQUEST_REJECTED
    assert "context_budget_exceeded" in result.detail and "compaction failed" in result.detail
    assert len(adapter.sent) == sent + 1  # only the compaction call
    assert result.run_ids == ()


@pytest.mark.parametrize("output", [final("   "), calls(("ask_user", {"question": "?"}))])
@sync
async def test_an_unusable_summary_is_not_committed(output: object) -> None:
    rt, _ = runtime([Measured(final("a1"), 850), output, final("a2")], context_budget=BUDGET)  # type: ignore[list-item]
    await rt.driver.drive(SCOPE, user("q1"))
    result = await rt.driver.drive(SCOPE, user("q2"))
    assert result.state is DriveState.COMPLETED
    run = await rt.data.read_run(SCOPE, result.compaction_run_ids[0])
    assert run.status is RunStatus.PROJECTION_FAILED and run.error_code == "invalid_summary"
    assert await rt.data.get_latest_snapshot(SCOPE) is None


@sync
async def test_the_summary_survives_a_restart() -> None:
    rt, _ = runtime([Measured(final("a1"), 900), final("S1"), final("a2")], context_budget=BUDGET)
    await rt.driver.drive(SCOPE, user("q1"))
    await rt.driver.drive(SCOPE, user("q2"))
    rt2, adapter2 = runtime([final("a3")], store=restart(rt.store), context_budget=BUDGET)
    result = await rt2.driver.drive(SCOPE, user("q3"))
    assert result.state is DriveState.COMPLETED
    head = adapter2.prepared_inputs[-1].conversation[0]
    assert isinstance(head, CompressedHistory) and head.summary == (TextBlock("S1"),)
