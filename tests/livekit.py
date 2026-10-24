"""Shared helpers of the live OpenRouter tests (tests/live): the key, the
models, a transport that keeps what was sent, and a small Tool."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from himari.bootstrap import read_env_file
from himari.common import JsonValue, ModelToolResult, TextBlock, ToolId
from himari.tool.api import (
    EffectProfile,
    ToolContext,
    ToolDefinition,
    ToolInvocation,
    ToolOutcome,
    ToolStatus,
    WorkspaceRequirement,
)

ROOT = Path(__file__).resolve().parents[1]
KEY = os.environ.get("OPENROUTER_API_KEY") or read_env_file(str(ROOT / ".env")).get("OPENROUTER_API_KEY", "")
MODELS = os.environ.get("HIMARI_LIVE_MODELS", "openai/gpt-6-luna,anthropic/claude-haiku-5.5").split(",")

LIVE = pytest.mark.skipif(
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
