"""OpenRouter streaming (V3-P73 §3): the stream request, the SSE body kept
as received, what the sink is shown, and normalization of a stored stream
— judged as the JSON response is, failing closed where it cannot be."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from himari.common import EventId, TextBlock, thaw
from himari.data.api import PersistedStateBasis, PromptBasis
from himari.prompt.api import CurrentStateProjection, ModelInput, RequestOptions, UserTurn
from himari.provider import api as provider_api
from himari.provider.api import (
    NativeNoResponse,
    NativeReceived,
    NativeUncertain,
    NormalizedFinal,
    NormalizedToolCalls,
    OutputDelta,
    ProviderCapabilities,
    ProviderError,
    RawCapture,
)
from himari.provider.openrouter import OpenRouterAdapter
from himari.provider.openrouter_stream import SseReader, assemble, payloads

KEY = "sk-or-test-SECRET"
STREAMING = "test/streaming"  # a model declared to stream
PLAIN = "test/plain"  # one that is not
CAPABILITIES = {STREAMING: ProviderCapabilities(STREAMING, streaming=True), PLAIN: ProviderCapabilities(PLAIN)}
SSE = "text/event-stream"
USAGE = {"prompt_tokens": 12, "completion_tokens": 5, "completion_tokens_details": {"reasoning_tokens": 2}}


def model_input(model: str = STREAMING, *, stream: bool = True) -> ModelInput:
    return ModelInput(stable_prefix=("You are HIMARI.",), tool_catalog=(),
                      conversation=(UserTurn(EventId("e1"), (TextBlock("hi"),)),),
                      current_state=CurrentStateProjection(basis=PersistedStateBasis(0, 0, (), (), ())),
                      request_options=RequestOptions(model, stream=stream), trigger_refs=(),
                      prompt_basis=PromptBasis(0, 0))


def sse(*chunks: Any, done: bool = True, comment: bool = True) -> bytes:
    lines = [": OPENROUTER PROCESSING\n\n"] if comment else []
    lines += [f"data: {json.dumps(c)}\n\n" for c in chunks]
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


def delta(**fields: Any) -> dict[str, Any]:
    finish = fields.pop("finish_reason", None)
    return {"id": "gen-1", "choices": [{"index": 0, "delta": fields, "finish_reason": finish}]}


ANSWER = sse(delta(role="assistant", content=""), delta(content="Hel"), delta(content="lo, "),
             delta(content="Seoul.", finish_reason="stop"),
             {"id": "gen-1", "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "stop"}],
              "usage": USAGE})


def adapter(handler: Any) -> OpenRouterAdapter:
    return OpenRouterAdapter(KEY, transport=httpx.MockTransport(handler), capabilities=CAPABILITIES)


def streamed(body: bytes, *, status: int = 200, content_type: str = SSE) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body, headers={"content-type": content_type})
    return handler


def invoke(handler: Any, *, sink: Any = None) -> tuple[Any, RawCapture, list[OutputDelta]]:
    shown: list[OutputDelta] = []
    capture = RawCapture()
    a = adapter(handler)
    prepared = a.prepare_native_request(model_input())
    outcome = asyncio.run(a.invoke_prepared(prepared, sink or shown.append, capture))
    return outcome, capture, shown


def normalize(body: bytes) -> Any:
    return adapter(streamed(b"")).normalize(NativeReceived(body, SSE))


# --- the request ---------------------------------------------------------------------


def test_only_a_declared_model_asked_to_stream_gets_a_stream_request() -> None:
    a = adapter(streamed(b""))
    asked = a.prepare_native_request(model_input())
    assert asked.streamed and json.loads(asked.body)["stream"] is True  # in the stored, wire-exact body
    for prepared in (a.prepare_native_request(model_input(PLAIN)), a.prepare_native_request(model_input(stream=False))):
        assert not prepared.streamed and "stream" not in json.loads(prepared.body)


# --- receiving -----------------------------------------------------------------------


def test_a_streamed_answer_is_kept_as_received_and_shown_while_it_arrives() -> None:
    outcome, capture, shown = invoke(streamed(ANSWER))
    assert isinstance(outcome, NativeReceived) and outcome.body == ANSWER == capture.body()
    assert outcome.content_type == capture.content_type == SSE
    assert outcome.usage is not None and (outcome.usage.input_tokens, outcome.usage.reasoning_tokens) == (12, 2)
    assert "".join(d.text for d in shown) == "Hello, Seoul."
    normalized = adapter(streamed(b"")).normalize(outcome)
    assert normalized == NormalizedFinal((TextBlock("Hello, Seoul."),))


def test_tool_calls_arrive_in_pieces_and_their_names_are_shown() -> None:
    body = sse(delta(content="Let me look."),
               delta(tool_calls=[{"index": 0, "id": "call_a", "type": "function",
                                  "function": {"name": "inside__read_file", "arguments": ""}}]),
               delta(tool_calls=[{"index": 0, "function": {"arguments": "{\"path\": "}}]),
               delta(tool_calls=[{"index": 0, "function": {"arguments": "\"a.txt\"}"}},
                                 {"index": 1, "id": "call_b", "function": {"name": "echo", "arguments": "{}"}}]),
               delta(finish_reason="tool_calls"))
    outcome, _, shown = invoke(streamed(body))
    assert [d.tool for d in shown if d.tool] == ["inside/read_file", "echo"]
    normalized = adapter(streamed(b"")).normalize(outcome)
    assert isinstance(normalized, NormalizedToolCalls)
    assert [(c.call_id, c.tool_id, dict(c.arguments)) for c in normalized.calls] == [
        ("call_a", "inside/read_file", {"path": "a.txt"}), ("call_b", "echo", {})]
    assert normalized.commentary == (TextBlock("Let me look."),)


def test_reasoning_is_shown_only_as_thinking() -> None:
    body = sse(delta(reasoning="secret plan", reasoning_details=[{"type": "reasoning.text", "text": "secret plan",
                                                                   "index": 0}]),
               delta(content="Done.", finish_reason="stop"))
    _, _, shown = invoke(streamed(body))
    assert shown[0] == OutputDelta(thinking=True) and all("secret" not in d.text for d in shown)


def test_an_error_chunk_is_a_provider_error_and_its_body_is_kept() -> None:
    body = sse(delta(content="Par"), {"id": "gen-1", "error": {"code": "server_error", "message": "gone"},
                                      "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "error"}]},
               done=False)
    outcome, capture, _ = invoke(streamed(body))
    assert isinstance(outcome, NativeNoResponse) and outcome.error_code == "provider_error_server_error"
    assert outcome.body == body == capture.body()


def test_a_stream_that_stops_without_done_is_uncertain_and_keeps_what_arrived() -> None:
    body = sse(delta(content="Half an ans"), done=False)
    outcome, capture, _ = invoke(streamed(body))
    assert outcome == NativeUncertain("stream_incomplete") and capture.body() == body


def test_a_broken_connection_keeps_the_bytes_before_it() -> None:
    first = sse(delta(content="Before the break"), done=False)

    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield first
            raise httpx.ReadError("connection reset")

    outcome, capture, shown = invoke(lambda request: httpx.Response(200, stream=Broken(),
                                                                    headers={"content-type": SSE}))
    assert outcome == NativeUncertain("transport:ReadError") and capture.body() == first
    assert "".join(d.text for d in shown) == "Before the break"


def test_an_error_status_is_a_definite_failure_with_its_body() -> None:
    error = b'{"error": {"code": 429, "message": "slow down"}}'
    outcome, _, _ = invoke(streamed(error, status=429, content_type="application/json"))
    assert outcome == NativeNoResponse("http_429", error, "application/json")


def test_a_whole_json_answer_to_a_stream_request_is_read_as_before() -> None:
    body = json.dumps({"choices": [{"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
                       "usage": USAGE}).encode()
    outcome, capture, shown = invoke(streamed(body, content_type="application/json"))
    assert isinstance(outcome, NativeReceived) and outcome.body == body == capture.body() and shown == []


def test_a_response_past_the_limit_is_cut_off_as_uncertain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provider_api, "MAX_CAPTURE_BYTES", len(ANSWER) - 1)
    outcome, capture, _ = invoke(streamed(ANSWER))
    assert outcome == NativeUncertain("response_too_large") and (capture.body() or b"") != ANSWER


def test_a_sink_that_fails_changes_nothing() -> None:
    def broken(_: OutputDelta) -> None:
        raise RuntimeError("display bug")

    outcome, _, _ = invoke(streamed(ANSWER), sink=broken)
    assert isinstance(outcome, NativeReceived) and outcome.body == ANSWER


# --- reading SSE -----------------------------------------------------------------------


@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r"])
def test_events_are_read_however_the_body_is_chunked(ending: str) -> None:
    body = ANSWER.decode().replace("\n", ending).encode()
    whole = payloads(body)
    reader = SseReader()
    pieces = [event for i in range(len(body)) for event in reader.feed(body[i:i + 1])]  # one byte at a time
    assert pieces == whole and whole[-1] == "[DONE]" and len(whole) == 6


def test_multi_line_data_joins_and_an_unclosed_last_event_is_dropped() -> None:
    assert payloads(b"data: a\ndata: b\nid: 7\n\n: note\ndata: c") == ["a\nb"]


# --- normalization (§3.3) --------------------------------------------------------------


def test_a_stream_normalizes_as_the_same_json_response_does() -> None:
    details = [{"type": "reasoning.text", "text": "step one, step two", "signature": "sig", "id": "r1",
                "format": "f", "index": 0},
               {"type": "reasoning.encrypted", "data": "QUJD", "id": "r2", "format": "f", "index": 1}]
    message = {"role": "assistant", "content": "Answer.", "reasoning_details": details}
    json_body = json.dumps({"choices": [{"message": message, "finish_reason": "stop"}]}).encode()
    stream = sse(delta(reasoning_details=[{"type": "reasoning.text", "text": "step one, ", "id": "r1",
                                           "format": "f", "index": 0}]),
                 delta(reasoning_details=[{"type": "reasoning.text", "text": "step two", "format": "f", "index": 0}]),
                 delta(reasoning_details=[{"type": "reasoning.text", "signature": "sig", "index": 0}]),
                 delta(reasoning_details=[{"type": "reasoning.encrypted", "data": "QUJD", "id": "r2", "format": "f",
                                           "index": 1}]),
                 delta(content="Answer.", finish_reason="stop"))
    a = adapter(streamed(b""))
    assert normalize(stream) == a.normalize(NativeReceived(json_body, "application/json"))


def test_empty_reasoning_details_stay_apart_from_absent_ones() -> None:
    kept = normalize(sse(delta(reasoning_details=[]), delta(content="x", finish_reason="stop")))
    assert thaw(kept.continuation) == {"reasoning_details": []}  # as a JSON response's [] (V3-049)
    fallback = normalize(sse(delta(reasoning="th"), delta(reasoning="ought"), delta(content="x", finish_reason="stop")))
    assert thaw(fallback.continuation) == {"reasoning": "thought"}
    assert normalize(sse(delta(content="x", finish_reason="stop"))).continuation is None


@pytest.mark.parametrize("pieces", [
    # one index, two types
    [[{"type": "reasoning.text", "text": "a", "index": 0}], [{"type": "reasoning.summary", "summary": "b", "index": 0}]],
    # one index, two ids
    [[{"type": "reasoning.text", "text": "a", "id": "r1", "index": 0}],
     [{"type": "reasoning.text", "text": "b", "id": "r2", "index": 0}]],
    # two signatures
    [[{"type": "reasoning.text", "signature": "s1", "index": 0}], [{"type": "reasoning.text", "signature": "s2",
                                                                    "index": 0}]],
    # an index that came back
    [[{"type": "reasoning.text", "text": "a", "index": 0}], [{"type": "reasoning.text", "text": "b", "index": 1}],
     [{"type": "reasoning.text", "text": "c", "index": 0}]],
    # redacted encrypted reasoning
    [[{"type": "reasoning.encrypted", "data": "[REDACTED]", "index": 0}]],
])
def test_reasoning_details_that_cannot_be_joined_without_guessing_are_invalid(pieces: list[Any]) -> None:
    body = sse(*(delta(reasoning_details=p) for p in pieces), delta(content="x", finish_reason="stop"))
    with pytest.raises(ProviderError):
        normalize(body)


@pytest.mark.parametrize("body", [
    sse(delta(content="no end", finish_reason="stop"), done=False),
    sse(delta(content="x", finish_reason="stop")) + b"data: {}\n\n",  # data after [DONE]
    b"data: not json\n\ndata: [DONE]\n\n",
    sse({"choices": [{"index": 0, "delta": {"content": "a"}}, {"index": 1, "delta": {"content": "b"}}]}),
    sse(delta(tool_calls=[{"index": 1, "id": "c", "function": {"name": "echo"}}], finish_reason="tool_calls")),
    sse(delta(content="x"), {"error": {"code": "server_error"}, "choices": []}),
    sse(delta(role="user", content="x", finish_reason="stop")),
    sse(delta(content="cut", finish_reason="length")),
])
def test_streams_that_cannot_be_trusted_fail_closed(body: bytes) -> None:
    with pytest.raises(ProviderError):
        normalize(body)


def test_assemble_gives_the_shape_of_a_json_response() -> None:
    doc = assemble(payloads(ANSWER))
    assert doc == {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "Hello, Seoul."}}],
                   "usage": USAGE}


def test_only_the_models_checked_live_declare_streaming() -> None:
    # V3-P73 §9: a model is added here only after tests/live/test_openrouter_stream_live.py passes on it.
    from himari.provider.openrouter import KNOWN_CAPABILITIES

    assert {m for m, c in KNOWN_CAPABILITIES.items() if c.streaming} == {"openai/gpt-6-luna",
                                                                         "anthropic/claude-haiku-5.5"}
