"""Live smoke tests against OpenRouter, through the full stack.

Opt-in only: ``HIMARI_LIVE=1`` plus ``OPENROUTER_API_KEY`` in the environment
or in the repository's git-ignored ``.env``. Models: ``HIMARI_LIVE_MODELS``
(comma-separated). Each test makes a few small requests.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

import pytest
from livekit import KEY, LIVE, MODELS, AddTool, RecordingTransport, final_text
from testkit import SCOPE, assert_chat_structure, user

from himari.bootstrap import build_runtime
from himari.common import RawRepresentation
from himari.control.api import DriveState
from himari.data.api import RunPurpose, RunStatus
from himari.prompt.api import ContextBudget
from himari.provider.openrouter import OpenRouterAdapter
pytestmark = LIVE


@pytest.mark.parametrize("model", MODELS)
def test_live_final_answer_with_reasoning(model: str) -> None:
    transport = RecordingTransport()
    rt = build_runtime(
        OpenRouterAdapter(KEY, transport=transport), model_id=model, reasoning_effort="low"
    )
    rt.store.create_conversation(SCOPE)
    result = asyncio.run(rt.driver.drive(SCOPE, user("Reply with exactly one word: pong")))

    assert result.state is DriveState.COMPLETED, result.detail
    assert "pong" in final_text(result).lower()
    run = asyncio.run(rt.data.read_run(SCOPE, result.run_ids[0]))
    assert run.raw_representation is RawRepresentation.WIRE_EXACT
    assert rt.store.raw_request_bodies(SCOPE) == transport.sent  # stored == sent, byte for byte
    assert run.usage is not None and (run.usage.output_tokens or 0) > 0
    print(f"\n{model}: usage={run.usage}")


@pytest.mark.parametrize("model", MODELS)
def test_live_tool_round_trip(model: str) -> None:
    transport = RecordingTransport()
    add = AddTool()
    rt = build_runtime(OpenRouterAdapter(KEY, transport=transport), tools=[add], model_id=model)
    rt.store.create_conversation(SCOPE)
    result = asyncio.run(
        rt.driver.drive(SCOPE, user("Use the add tool to compute 2 + 3, then reply with just the number."))
    )

    assert result.state is DriveState.COMPLETED, result.detail
    assert add.calls and add.calls[0] == {"a": 2, "b": 3}
    assert "5" in final_text(result)
    assert len(result.run_ids) >= 2
    for raw in rt.store.raw_request_bodies(SCOPE):
        assert_chat_structure(json.loads(raw)["messages"])
    print(f"\n{model}: runs={len(result.run_ids)} calls={add.calls}")


@pytest.mark.parametrize("model", MODELS)
def test_live_declared_efforts_are_accepted(model: str) -> None:
    """Keeps KNOWN_CAPABILITIES honest: every declared effort must be accepted."""
    adapter = OpenRouterAdapter(KEY)
    efforts = sorted(adapter.describe_capabilities(model).reasoning_efforts)
    if not efforts:
        pytest.skip(f"no declared efforts for {model}")

    async def one(effort: str) -> str:
        rt = build_runtime(adapter, model_id=model, reasoning_effort=effort)
        rt.store.create_conversation(SCOPE)
        result = await rt.driver.drive(SCOPE, user("What is 17*3? Answer with the number."))
        return f"{effort}:{result.state.value}:{result.detail}"

    async def sweep() -> list[str]:
        return list(await asyncio.gather(*(one(e) for e in efforts)))

    outcomes = asyncio.run(sweep())
    assert all(":completed:" in o for o in outcomes), outcomes
    print(f"\n{model}: {outcomes}")


@pytest.mark.parametrize("model", MODELS)
def test_live_tool_round_trip_replays_reasoning(model: str) -> None:
    """V3-049 end to end: the tool turn's reasoning_details come back on the
    same assistant message in the next request, unmodified.

    The task must need reasoning: on a trivial one claude-haiku-5.5 skips
    thinking even at "max" (observed live), leaving nothing to replay. Even on
    a harder one gpt-6-luna sometimes calls the tool without reasoning first
    (observed live, about one in two), so a fresh conversation is tried up to
    three times."""
    for _ in range(3):
        transport = RecordingTransport()
        add = AddTool()
        rt = build_runtime(
            OpenRouterAdapter(KEY, transport=transport), tools=[add], model_id=model, reasoning_effort="high"
        )
        rt.store.create_conversation(SCOPE)
        result = asyncio.run(
            rt.driver.drive(
                SCOPE,
                user(
                    "How many positive integers below 1000 are divisible by 7 or 11 but not both? "
                    "Work out the partial counts, use the add tool at least once to combine them, "
                    "then answer with the number."
                ),
            )
        )

        assert result.state is DriveState.COMPLETED, result.detail
        assert add.calls and "208" in final_text(result)
        first_run = asyncio.run(rt.data.read_run(SCOPE, result.run_ids[0]))
        if first_run.continuation is not None:
            break
    assert first_run.continuation is not None, "no tool turn returned reasoning to replay in three tries"
    second = json.loads(transport.sent[1])["messages"]
    assert_chat_structure(second)
    tool_turn = next(m for m in second if m["role"] == "assistant" and m.get("tool_calls"))
    first_response = json.loads(asyncio.run(rt.data.read_raw_response(SCOPE, result.run_ids[0])).body)
    assert tool_turn["reasoning_details"] == first_response["choices"][0]["message"]["reasoning_details"]
    kinds = [(d.get("type"), "signature" in d, "data" in d) for d in tool_turn["reasoning_details"]]
    print(f"\n{model}: runs={len(result.run_ids)} replayed {kinds}")


@pytest.mark.parametrize("model", MODELS)
def test_live_compaction_keeps_what_matters(model: str) -> None:
    """A small budget forces one compaction: the Provider-measured size of
    the first request plus the second message crosses 80%, the model writes
    the summary, and a later answer still knows a fact that now lives only
    in that summary (possibly a summary of a summary)."""
    transport = RecordingTransport()
    rt = build_runtime(OpenRouterAdapter(KEY, transport=transport), model_id=model, reasoning_effort="low",
                       context_budget=ContextBudget(limit_tokens=3000))
    rt.store.create_conversation(SCOPE)
    filler = "The quick brown fox jumps over the lazy dog. " * 100

    async def say(text: str) -> Any:
        result = await rt.driver.drive(SCOPE, user(text))
        assert result.state is DriveState.COMPLETED, result.detail
        return result

    async def scenario() -> None:  # one event loop: the transport's connections belong to it
        first = await say(f"My project codename is BLUE-HERON-42. Remember it. Ignore this filler: {filler}\nReply: ok")
        measured = (await rt.data.read_run(SCOPE, first.run_ids[0])).usage
        assert measured is not None and (measured.input_tokens or 0) > 900  # the token count the trigger uses
        second = await say(f"More filler to ignore: {filler}\nReply: ok")
        assert len(second.compaction_run_ids) == 1
        compaction = await rt.data.read_run(SCOPE, second.compaction_run_ids[0])
        assert compaction.purpose is RunPurpose.COMPACTION and compaction.status is RunStatus.PROJECTED
        assert "BLUE-HERON-42" not in json.dumps(json.loads(transport.sent[-1])["messages"][2:])

        third = await say("What is my project codename? Reply with only the codename.")
        # A tokenizer that counts the filler higher (claude-haiku: ~2x) is still
        # over 80% here and compacts again, folding the first summary in.
        assert len(third.compaction_run_ids) <= 1
        last_request = json.loads(transport.sent[-1])
        assert "earlier_conversation_summary" in last_request["messages"][1]["content"]
        assert "BLUE-HERON-42" in final_text(third)
        snapshot = await rt.data.get_latest_snapshot(SCOPE)
        assert snapshot is not None and isinstance(snapshot.body, Mapping)
        usage = [(await rt.data.read_run(SCOPE, r.run_ids[0])).usage for r in (first, second, third)]
        print(f"\n{model}: inputs={[u.input_tokens if u else None for u in usage]} compaction={compaction.usage}\n"
              f"summary={snapshot.body['summary']!r}")

    asyncio.run(scenario())


@pytest.mark.parametrize("model", MODELS)
def test_live_a_long_message_is_summarized_in_a_chain(model: str) -> None:
    """A message too long for one summarizing request is summarized in
    labelled pieces; a fact from its middle survives the chain. The first
    request is over the working budget but within the hard limit, so it is
    sent rather than refused."""
    rt = build_runtime(OpenRouterAdapter(KEY), model_id=model, reasoning_effort="low",
                       context_budget=ContextBudget(limit_tokens=2500, hard_limit_tokens=20_000))
    rt.store.create_conversation(SCOPE)
    filler = "The quick brown fox jumps over the lazy dog. " * 120
    message = f"Ignore this filler. {filler} The project label is ORCHID-7731. {filler} Reply: ok"

    async def scenario() -> None:
        first = await rt.driver.drive(SCOPE, user(message))
        assert first.state is DriveState.COMPLETED, first.detail
        second = await rt.driver.drive(SCOPE, user("Reply: ok"))
        assert second.state is DriveState.COMPLETED, second.detail
        chained = [*first.compaction_run_ids, *second.compaction_run_ids]
        assert len(chained) >= 2, chained
        third = await rt.driver.drive(SCOPE, user("What is the project label? Reply with only the label."))
        assert third.state is DriveState.COMPLETED, third.detail
        assert "ORCHID-7731" in final_text(third)
        snapshot = await rt.data.get_latest_snapshot(SCOPE)
        assert snapshot is not None and isinstance(snapshot.body, Mapping)
        print(f"\n{model}: calls={len(chained)} summary={snapshot.body['summary']!r}"[:600])

    asyncio.run(scenario())
