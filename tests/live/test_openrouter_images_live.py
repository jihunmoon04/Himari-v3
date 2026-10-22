"""Live check of image input (V3-P67 §7.1, §14): opt-in, ``HIMARI_LIVE=1``
plus the key. For each model in ``HIMARI_LIVE_IMAGE_MODELS``: ① an image in a
user message, ② an image in the user message after a Tool group. A model
gets ``image_input`` in the declared list only when both pass."""

from __future__ import annotations

import asyncio
import io
import os
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image
from testkit import image_model_input

from himari.bootstrap import read_env_file
from himari.common import AttachmentRef, CallId, EventId, FileId, ModelToolResult, RunId, TextBlock, ToolCall, ToolId
from himari.prompt.api import AttachmentInfo, ToolGroupTurn, UserTurn
from himari.provider.api import NativeReceived, NormalizedFinal, ProviderCapabilities
from himari.provider.openrouter import KNOWN_CAPABILITIES, OpenRouterAdapter
from himari.tool.api import EffectProfile, ToolDefinition, WorkspaceRequirement

ROOT = Path(__file__).resolve().parents[2]
KEY = os.environ.get("OPENROUTER_API_KEY") or read_env_file(str(ROOT / ".env")).get("OPENROUTER_API_KEY", "")
MODELS = [m for m in os.environ.get("HIMARI_LIVE_IMAGE_MODELS", "openai/gpt-6-luna").split(",") if m]

pytestmark = pytest.mark.skipif(
    os.environ.get("HIMARI_LIVE") != "1" or not KEY, reason="live tests are opt-in (HIMARI_LIVE=1 + key)")


def square(color: tuple[int, int, int]) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (256, 256), color).save(out, "JPEG", quality=90)
    return out.getvalue()


IMAGES = {"r" * 64: square((220, 20, 20)), "b" * 64: square((20, 40, 220))}


def info(key: str) -> AttachmentInfo:
    return AttachmentInfo("square.jpg", "/workspace/square.jpg", "image/jpeg", len(IMAGES[key]), view_state="ready",
                          view_key=key, view_media_type="image/jpeg", view_width=256, view_height=256,
                          view_bytes=len(IMAGES[key]))


def answer(model: str, model_input: object) -> str:
    caps = replace(KNOWN_CAPABILITIES.get(model, ProviderCapabilities(model)), image_input=True)
    adapter = OpenRouterAdapter(KEY, capabilities={model: caps}, blobs=IMAGES.__getitem__)
    prepared = adapter.prepare_native_request(model_input)  # type: ignore[arg-type]
    outcome = asyncio.run(adapter.invoke_prepared(prepared))
    assert isinstance(outcome, NativeReceived), outcome
    output = adapter.normalize(outcome)
    assert isinstance(output, NormalizedFinal), output
    return " ".join(b.text for b in output.blocks if isinstance(b, TextBlock)).lower()


@pytest.mark.parametrize("model", MODELS)
def test_an_image_in_a_user_message(model: str) -> None:
    turn = UserTurn(EventId("e"), (TextBlock("What is the main colour of the attached image? Answer in one word."),
                                   AttachmentRef(FileId("f"), 1)))
    said = answer(model, image_model_input([turn], {"f#1": info("r" * 64)}, image_input=True, model=model))
    assert "red" in said, said


@pytest.mark.parametrize("model", MODELS)
def test_an_image_after_a_tool_group(model: str) -> None:
    chart = ToolDefinition(ToolId("x/chart"), "Draws a chart.", {"type": "object", "properties": {}},
                           WorkspaceRequirement.NONE, EffectProfile.READ, external_source="x")
    turn = UserTurn(EventId("e"), (TextBlock("Draw the chart, then tell me its main colour in one word."),))
    group = ToolGroupTurn(RunId("r1"), (), (ToolCall(CallId("call_1"), 0, ToolId("x/chart"), {}),),
                          {CallId("call_1"): ModelToolResult((TextBlock("drawn"), AttachmentRef(FileId("g"), 1)))})
    model_input = image_model_input([turn, group], {"g#1": info("b" * 64)}, image_input=True, model=model)
    model_input = replace(model_input, tool_catalog=(chart,))
    said = answer(model, model_input)
    assert "blue" in said, said
