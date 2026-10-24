"""Live streaming checks against OpenRouter (V3-P73 §9, §12): what a model
must pass before it declares ``streaming``.

Opt-in like test_openrouter_live.py: ``HIMARI_LIVE=1`` plus the key. Each
model is checked with ``streaming`` declared here, so a candidate can be
tried before it is declared in ``KNOWN_CAPABILITIES``:
- a long answer arrives in many pieces and the stored stream normalizes to it;
- a tool turn's merged reasoning continuation is replayed unmodified in the
  next request, and the turn completes (none of it redacted);
- a cancel mid-stream ends the call and keeps what arrived.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from typing import Any

import pytest
from livekit import KEY, LIVE, MODELS, AddTool, RecordingTransport, final_text
from testkit import SCOPE, assert_chat_structure, user

from himari.bootstrap import build_runtime
from himari.common import RunId, thaw
from himari.control.api import DriveState, StopSignal, TurnUsage
from himari.control.driver import CANCELLED_BY_USER
from himari.data.api import RunStatus
from himari.provider.api import OutputDelta
from himari.provider.openrouter import OpenRouterAdapter

pytestmark = LIVE


class Shown:
    def __init__(self) -> None:
        self.deltas: list[OutputDelta] = []

    def report(self, usage: TurnUsage) -> None:
        pass

    def output_started(self, run_id: RunId) -> None:
        pass

    def output(self, run_id: RunId, delta: OutputDelta) -> None:
        self.deltas.append(delta)

    def output_ended(self, run_id: RunId) -> None:
        pass

    def text(self) -> str:
        return "".join(d.text for d in self.deltas)


def adapter(model: str, transport: Any = None) -> OpenRouterAdapter:
    probe = OpenRouterAdapter(KEY)
    declared = dataclasses.replace(probe.describe_capabilities(model), streaming=True)
    return OpenRouterAdapter(KEY, transport=transport, capabilities={model: declared})


@pytest.mark.parametrize("model", MODELS)
def test_live_a_long_answer_streams_and_its_stored_stream_normalizes_to_it(model: str) -> None:
    rt = build_runtime(adapter(model), model_id=model, streaming=True)
    rt.store.create_conversation(SCOPE)
    shown = Shown()
    result = asyncio.run(rt.driver.drive(SCOPE, user("Write five short numbered sentences about the sea."),
                                         progress=shown))
    assert result.state is DriveState.COMPLETED, result.detail
    raw = asyncio.run(rt.data.read_raw_response(SCOPE, result.run_ids[0]))
    assert raw.content_type.startswith("text/event-stream") and b"data: [DONE]" in raw.body
    assert len([d for d in shown.deltas if d.text]) > 5  # it came in pieces
    assert shown.text() == final_text(result)
    run = asyncio.run(rt.data.read_run(SCOPE, result.run_ids[0]))
    assert run.usage is not None and (run.usage.output_tokens or 0) > 0
    print(f"\n{model}: {len(shown.deltas)} pieces, {len(raw.body)} bytes stored, usage={run.usage}")


@pytest.mark.parametrize("model", MODELS)
def test_live_a_streamed_tool_turn_replays_its_merged_reasoning(model: str) -> None:
    """§3.3.1: the continuation merged from the stream goes back unmodified
    on the same assistant message, and the provider takes it (the turn ends
    with the answer). Tried up to three times, as the model may skip
    reasoning before a tool call (test_openrouter_live)."""
    for _ in range(3):
        transport = RecordingTransport()
        add = AddTool()
        rt = build_runtime(adapter(model, transport), tools=[add], model_id=model, reasoning_effort="high",
                           streaming=True)
        rt.store.create_conversation(SCOPE)
        shown = Shown()
        result = asyncio.run(rt.driver.drive(SCOPE, user(
            "How many positive integers below 1000 are divisible by 7 or 11 but not both? Work out the partial "
            "counts, use the add tool at least once to combine them, then answer with the number."), progress=shown))
        assert result.state is DriveState.COMPLETED, result.detail
        assert add.calls and "208" in final_text(result)
        first = asyncio.run(rt.data.read_run(SCOPE, result.run_ids[0]))
        if first.continuation is not None:
            break
    assert first.continuation is not None, "no tool turn returned reasoning to replay in three tries"
    assert any(d.thinking for d in shown.deltas) and "add" in [d.tool for d in shown.deltas]
    second = json.loads(transport.sent[1])["messages"]
    assert_chat_structure(second)
    turn = next(m for m in second if m["role"] == "assistant" and m.get("tool_calls"))
    stored = thaw(first.continuation.payload)
    replayed = {k: turn[k] for k in ("reasoning_details", "reasoning") if k in turn}
    assert replayed == stored
    details = stored.get("reasoning_details", [])
    assert not any(d.get("data") == "[REDACTED]" for d in details)
    print(f"\n{model}: replayed {[(d.get('type'), d.get('index'), bool(d.get('signature'))) for d in details]}")


@pytest.mark.parametrize("model", MODELS)
def test_live_a_cancel_mid_stream_keeps_what_arrived(model: str) -> None:
    async def scenario() -> tuple[Any, Any, Any, str]:
        rt = build_runtime(adapter(model), model_id=model, streaming=True)
        rt.store.create_conversation(SCOPE)
        shown, stop = Shown(), StopSignal()
        job = asyncio.create_task(rt.driver.drive(SCOPE, user("Count from 1 to 400, one number per line."),
                                                  stop=stop, progress=shown))
        for _ in range(600):
            if len(shown.text()) > 40:
                break
            await asyncio.sleep(0.1)
        stop.cancel()
        result = await asyncio.wait_for(job, 10)  # the connection is cut, not waited out
        run = await rt.data.read_run(SCOPE, result.run_ids[0])
        raw = await rt.data.read_raw_response(SCOPE, result.run_ids[0])
        return result, run, raw, shown.text()

    result, run, raw, text = asyncio.run(scenario())
    assert result.state is DriveState.CANCELLED
    assert (run.status, run.error_code) == (RunStatus.OUTCOME_UNKNOWN, CANCELLED_BY_USER)
    assert raw.body and b"data: [DONE]" not in raw.body
    print(f"\n{model}: cancelled after {len(text)} characters, {len(raw.body)} bytes kept")
