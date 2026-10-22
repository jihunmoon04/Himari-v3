"""OpenRouter and images (V3-P67 §7): content parts, the message after a
Tool group, markers in the stored body and their expansion when sent."""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import replace
from typing import Any

import httpx
from testkit import image_model_input

from himari.common import (
    AttachmentRef,
    CallId,
    EventId,
    FileId,
    ModelToolResult,
    RawRepresentation,
    RunId,
    TextBlock,
    ToolCall,
    ToolId,
)
from himari.prompt.api import AttachmentInfo, ToolGroupTurn, UserTurn
from himari.provider.api import NativeNoResponse, NativeReceived, ProviderCapabilities
from himari.provider.openrouter import OpenRouterAdapter

KEY = "a" * 64
READY = AttachmentInfo("p.png", "/workspace/u/p.png", "image/png", 9000, view_state="ready", view_key=KEY,
                       view_media_type="image/jpeg", view_width=20, view_height=10, view_bytes=4)
SEES = ProviderCapabilities("sees", image_input=True)


def adapter(read: Any = None, transport: httpx.AsyncBaseTransport | None = None) -> OpenRouterAdapter:
    return OpenRouterAdapter("k", capabilities={"sees": SEES}, blobs=read, transport=transport)


def user(*blocks: Any) -> UserTurn:
    return UserTurn(EventId("e"), tuple(blocks))


def body(prepared: Any) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(prepared.body)
    return parsed


def test_a_shown_image_follows_its_line_as_a_marker() -> None:
    model_input = image_model_input([user(TextBlock("look"), AttachmentRef(FileId("f"), 1))], {"f#1": READY},
                                    image_input=True, model="sees")
    prepared = adapter().prepare_native_request(model_input)
    assert prepared.raw_representation is RawRepresentation.WIRE_EXACT_BLOB_REFS
    content = body(prepared)["messages"][1]["content"]
    assert [p["type"] for p in content] == ["text", "image_url"]
    assert content[0]["text"].startswith("look\n<attachment>") and "not_shown" not in content[0]["text"]
    assert content[1]["image_url"]["url"] == f"himari-blob:{KEY}:image/jpeg"


def test_a_model_without_images_gets_the_line_and_a_reason() -> None:
    model_input = image_model_input([user(AttachmentRef(FileId("f"), 1))], {"f#1": READY}, model="text-only")
    prepared = adapter().prepare_native_request(model_input)
    assert prepared.raw_representation is RawRepresentation.WIRE_EXACT
    content = body(prepared)["messages"][1]["content"]
    assert isinstance(content, str) and '"not_shown": "this model does not take images"' in content


def test_tool_images_come_after_the_whole_group() -> None:
    group = ToolGroupTurn(RunId("r"), (), (ToolCall(CallId("a"), 0, ToolId("x/chart"), {}),
                                           ToolCall(CallId("b"), 1, ToolId("x/other"), {})),
                          {CallId("a"): ModelToolResult((TextBlock("chart"), AttachmentRef(FileId("f"), 1))),
                           CallId("b"): ModelToolResult((TextBlock("plain"),))})
    model_input = image_model_input([user(TextBlock("go")), group], {"f#1": READY}, image_input=True, model="sees")
    roles = [(m["role"], m.get("tool_call_id")) for m in body(adapter().prepare_native_request(model_input))["messages"]]
    assert roles[2:] == [("assistant", None), ("tool", "a"), ("tool", "b"), ("user", None)]
    last = body(adapter().prepare_native_request(model_input))["messages"][-1]["content"]
    assert last[0]["text"] == "Images from the tool results above:"
    assert last[1]["text"].startswith("[x/chart call 1] <attachment>") and last[2]["type"] == "image_url"


def test_sending_expands_markers_only_in_image_urls() -> None:
    sent: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]})

    typed = f"himari-blob:{KEY}:image/jpeg"  # someone typed a marker: it stays text
    model_input = image_model_input([user(TextBlock(typed), AttachmentRef(FileId("f"), 1))], {"f#1": READY},
                                    image_input=True, model="sees")
    reads: list[str] = []
    def read(key: str) -> bytes:
        reads.append(key)
        return b"JPEG"

    provider = adapter(read, httpx.MockTransport(handler))
    prepared = provider.prepare_native_request(model_input)
    outcome = asyncio.run(provider.invoke_prepared(prepared))
    assert isinstance(outcome, NativeReceived) and reads == [KEY]
    content = json.loads(sent[0])["messages"][1]["content"]
    assert content[0]["text"].startswith(typed)
    assert content[1]["image_url"]["url"] == "data:image/jpeg;base64," + base64.b64encode(b"JPEG").decode()
    first = sent.pop()
    asyncio.run(adapter(lambda key: b"JPEG", httpx.MockTransport(handler)).invoke_prepared(prepared))
    assert sent == [first]  # the same stored body sends the same bytes


def test_a_copy_gone_before_sending_sends_nothing() -> None:
    def gone(key: str) -> bytes:
        raise RuntimeError("expired")

    model_input = image_model_input([user(AttachmentRef(FileId("f"), 1))], {"f#1": READY}, image_input=True,
                                    model="sees")
    called: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(1)
        return httpx.Response(200)

    provider = adapter(gone, httpx.MockTransport(handler))
    outcome = asyncio.run(provider.invoke_prepared(provider.prepare_native_request(model_input)))
    assert isinstance(outcome, NativeNoResponse) and outcome.error_code == "image_missing" and not called


def test_an_expired_copy_is_a_line_from_then_on() -> None:
    expired = replace(READY, view_state="view_expired")
    model_input = image_model_input([user(AttachmentRef(FileId("f"), 1))], {"f#1": expired}, image_input=True,
                                    model="sees")
    prepared = adapter().prepare_native_request(model_input)
    assert prepared.raw_representation is RawRepresentation.WIRE_EXACT
    assert '"not_shown": "expired"' in body(prepared)["messages"][1]["content"]
