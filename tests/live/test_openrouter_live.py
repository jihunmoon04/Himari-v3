"""Live smoke tests against OpenRouter, through the full stack.

Opt-in only: ``HIMARI_LIVE=1`` plus ``OPENROUTER_API_KEY`` in the environment
or in the repository's git-ignored ``.env``. Models: ``HIMARI_LIVE_MODELS``
(comma-separated). Each test makes a few small requests.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest
from testkit import SCOPE, assert_chat_structure, user

from himari.bootstrap import build_in_memory_runtime, read_env_file
from himari.common import JsonValue, ModelToolResult, RawRepresentation, TextBlock, ToolId
from himari.control.api import DriveState
from himari.provider.openrouter import OpenRouterAdapter
from himari.tool.api import (
    EffectProfile,
    ToolContext,
    ToolDefinition,
    ToolInvocation,
    ToolOutcome,
    ToolStatus,
    WorkspaceRequirement,
)

ROOT = Path(__file__).resolve().parents[2]
KEY = os.environ.get("OPENROUTER_API_KEY") or read_env_file(str(ROOT / ".env")).get("OPENROUTER_API_KEY", "")
MODELS = os.environ.get("HIMARI_LIVE_MODELS", "openai/gpt-5-nano,anthropic/claude-haiku-5.5").split(",")

pytestmark = pytest.mark.skipif(
    os.environ.get("HIMARI_LIVE") != "1" or not KEY, reason="live tests are opt-in (HIMARI_LIVE=1 + key)"
)


class RecordingTransport(httpx.AsyncBaseTransport):
    """Delegates to the real transport and keeps the exact bytes sent."""

    def __init__(self) -> None:
        self._inner = httpx.AsyncHTTPTransport()
        self.sent: list[bytes] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request.content)
        return await self._inner.handle_async_request(request)


class AddTool:
    definition = ToolDefinition(
        tool_id=ToolId("add"),
        description="Add two integers and return the sum.",
        input_schema={
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
            "additionalProperties": False,
        },
        workspace_requirement=WorkspaceRequirement.NONE,
        effect_profile=EffectProfile.READ,
    )

    def __init__(self) -> None:
        self.calls: list[dict[str, JsonValue]] = []

    async def run(self, invocation: ToolInvocation, ctx: ToolContext) -> ToolOutcome:
        a, b = invocation.arguments["a"], invocation.arguments["b"]
        assert isinstance(a, int) and isinstance(b, int)
        self.calls.append(dict(invocation.arguments))
        return ToolOutcome(ToolStatus.SUCCEEDED, ModelToolResult((TextBlock(str(a + b)),)))


def final_text(result: Any) -> str:
    payload = result.final_event.payload
    return " ".join(b.text for b in payload.blocks if isinstance(b, TextBlock))


@pytest.mark.parametrize("model", MODELS)
def test_live_final_answer_with_reasoning(model: str) -> None:
    transport = RecordingTransport()
    rt = build_in_memory_runtime(
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
    rt = build_in_memory_runtime(OpenRouterAdapter(KEY, transport=transport), tools=[add], model_id=model)
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
