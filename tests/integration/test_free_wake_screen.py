"""PR #173: a screen look stays private across streaming Runs and compaction."""

from __future__ import annotations

import asyncio

import pytest
from testkit import SCOPE, RecordingTool, calls, final, runtime, user

from himari.common import RunId
from himari.control.api import DriveState, TurnUsage
from himari.data.api import Wake, WakeKind, WakeReason, WakeStatus
from himari.prompt.api import ContextBudget, UserTurn, blocks_text
from himari.prompt.compaction import COMPACTION_INSTRUCTIONS
from himari.provider.api import NativeNoResponse, OutputDelta
from himari.provider.scripted import Measured, ScriptedAdapter

FREE = (WakeReason(WakeKind.FREE, group="work", minutes=90, step=2),)


class Progress:
    def __init__(self) -> None:
        self.started: list[RunId] = []
        self.deltas: list[OutputDelta] = []
        self.ended: list[RunId] = []

    def report(self, usage: TurnUsage) -> None: ...

    def output_started(self, run_id: RunId) -> None:
        self.started.append(run_id)

    def output(self, run_id: RunId, delta: OutputDelta) -> None:
        self.deltas.append(delta)

    def output_ended(self, run_id: RunId) -> None:
        self.ended.append(run_id)


@pytest.mark.parametrize("commentary", ["[[pass]]", "[[mood:neutral]] [[pass]] \n"])
@pytest.mark.parametrize("speak", [False, True])
def test_a_streaming_screen_look_and_a_second_pass_stay_hidden(commentary: str, speak: bool) -> None:
    async def scenario() -> None:
        answer = "[[pass]] 괄호 하나 빠졌어" if speak else "[[pass]]"
        adapter = ScriptedAdapter([calls(("look_at_screen", {}), commentary=commentary), final(answer)],
                                  streaming=True)
        look = RecordingTool("look_at_screen")
        rt, _ = runtime(provider=adapter, streaming=True, tools=[look])
        event = await rt.driver.accept_wake(SCOPE, FREE, "d1")
        assert isinstance(event.payload, Wake)
        progress = Progress()
        done = await rt.driver.resume_wake(SCOPE, event.payload.wake_id, free=True, progress=progress, max_runs=2)
        wake = await rt.data.read_wake(SCOPE.principal_id, event.payload.wake_id)
        assert done.state is DriveState.COMPLETED and len(look.invocations) == 1
        assert len(adapter.sent) == 2 and all(r.streamed for r in adapter.sent)
        if speak:
            assert wake.status is WakeStatus.SPEAKING
            # Revealing the second Run replays the first Run's held screen progress too.
            assert progress.started == progress.ended == list(done.run_ids)
            assert [d.tool for d in progress.deltas if d.tool] == ["look_at_screen"]
            text = "".join(d.text for d in progress.deltas)
            assert "괄호 하나 빠졌어" in text and "[[pass]]" not in text
        else:
            assert wake.status is WakeStatus.PASSED and wake.held_run_id == done.run_ids[0]
            assert progress.started == progress.ended == [] and progress.deltas == []
            assert await rt.data.read_events(SCOPE) == ()
        assert await rt.data.unshown_wakes(SCOPE) == frozenset()

    asyncio.run(scenario())


@pytest.mark.parametrize("ending", ["pass", "drop", "speak"])
def test_compaction_between_the_look_and_the_verdict_never_summarizes_the_hidden_wake(ending: str) -> None:
    async def scenario() -> None:
        end = NativeNoResponse("failed") if ending == "drop" else final(
            "[[pass]]" if ending == "pass" else "괄호 하나 빠졌어")
        adapter = ScriptedAdapter([
            final("old answer"),
            Measured(calls(("look_at_screen", {}), commentary="[[pass]]"), 3500),
            final("old conversation summary"), end, final("next answer"),
        ])
        rt, _ = runtime(provider=adapter, tools=[RecordingTool("look_at_screen")],
                        instructions=("short",), context_budget=ContextBudget(limit_tokens=4000))
        await rt.driver.drive(SCOPE, user("old question"))
        event = await rt.driver.accept_wake(SCOPE, FREE, "d1")
        assert isinstance(event.payload, Wake)
        done = await rt.driver.resume_wake(SCOPE, event.payload.wake_id, free=True, max_runs=2)
        assert done.state is (DriveState.PROVIDER_FAILED if ending == "drop" else DriveState.COMPLETED)
        if ending == "drop":
            await rt.driver.withdraw_wake(SCOPE, event.payload.wake_id)
        wake = await rt.data.read_wake(SCOPE.principal_id, event.payload.wake_id)
        assert wake.status is {"pass": WakeStatus.PASSED, "drop": WakeStatus.DROPPED,
                               "speak": WakeStatus.SPEAKING}[ending]
        summaries = [i for i in adapter.prepared_inputs if i.stable_prefix == (COMPACTION_INSTRUCTIONS,)]
        assert summaries  # Compaction really happened between the two answering Runs.
        for summary in summaries:
            part = summary.conversation[0]
            assert isinstance(part, UserTurn)
            text = blocks_text(part.blocks, {})
            assert "old question" in text and "old answer" in text
            assert "wake" not in text and "look_at_screen" not in text and "[[pass]]" not in text
        snapshot = await rt.data.get_latest_snapshot(SCOPE)
        assert snapshot is not None and snapshot.covers_through_sequence < event.sequence
        assert snapshot.body == {"summary": "old conversation summary"}
        assert len(adapter.prepared_inputs) == 4
        if ending != "speak":
            events = await rt.data.read_events(SCOPE)
            assert all(e.sequence < event.sequence for e in events)
            assert await rt.data.unshown_wakes(SCOPE) == frozenset()
            await rt.driver.drive(SCOPE, user("next question"))
            future = repr(adapter.prepared_inputs[-1].conversation)
            assert "old conversation summary" in future and "next question" in future
            assert not any(isinstance(p, UserTurn) and p.wake for p in adapter.prepared_inputs[-1].conversation)
            assert "look_at_screen" not in future and "[[pass]]" not in future

    asyncio.run(scenario())
