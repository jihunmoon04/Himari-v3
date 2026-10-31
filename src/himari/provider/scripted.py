"""Scripted ProviderAdapter for development and tests (no network).

Each ``invoke_prepared`` consumes one script step:
- ``NormalizedFinal`` / ``NormalizedToolCalls`` → a received JSON body
- ``NativeNoResponse`` / ``NativeUncertain`` → returned as the outcome
- ``RawBody`` → received verbatim (e.g. to exercise invalid output)
- ``Measured`` → its output, with a Provider-reported input token count
- a ``BaseException`` instance → raised (e.g. to simulate a process crash)
- ``StreamCut`` → a stream that stops part way (V3-P73 tests)

With ``streaming=True`` the adapter declares the ``streaming`` capability;
a request prepared with ``stream`` is then invoked with a sink: the answer
(or commentary) goes to it a few characters at a time, ``piece_delay``
apart (``first_delay`` before the first, as a model thinking), then the
tool ids, and the body goes into the capture.

The request body is ``ModelInput`` serialized to JSON, labelled
``ADAPTER_SERIALIZED`` — it is not a wire capture of any real Provider.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from himari.common import (
    AttachmentRef,
    CallId,
    ContentBlock,
    RawRepresentation,
    TextBlock,
    TokenUsage,
    ToolCall,
    ToolId,
    thaw,
)
from himari.prompt.api import ModelInput, file_reference_line
from himari.provider.api import (
    NativeInvocationOutcome,
    OutputDelta,
    OutputSink,
    RawCapture,
    NativeNoResponse,
    NativePreparedRequest,
    NativeReceived,
    NativeUncertain,
    NormalizedFinal,
    NormalizedModelOutput,
    NormalizedToolCalls,
    ProviderCapabilities,
    ProviderError,
    ProviderErrorCode,
    validate_tool_calls,
)


@dataclass(frozen=True)
class RawBody:
    body: bytes


@dataclass(frozen=True)
class Measured:
    output: NormalizedModelOutput
    input_tokens: int
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_tokens: int | None = None


@dataclass(frozen=True)
class StreamCut:
    """A streamed answer that stops after ``pieces``: each goes to the sink
    and, as bytes, to the capture; then ``end`` is returned or raised, or —
    an ``asyncio.Event`` — waited for (to be cancelled meanwhile)."""

    pieces: tuple[str, ...]
    end: NativeUncertain | BaseException | asyncio.Event


type ScriptStep = (NormalizedModelOutput | NativeNoResponse | NativeUncertain | RawBody | Measured | BaseException
                   | StreamCut)
PIECE_CHARS = 4  # of one streamed piece


def _json_default(value: object) -> object:
    if isinstance(value, (Mapping, frozenset)):
        return thaw(value)  # FrozenMap is deliberately not a dict
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)  # DTOs nested inside a FrozenMap
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    raise TypeError(type(value).__name__)


def _blocks_to_json(blocks: Iterable[ContentBlock]) -> list[str]:
    return [b.text if isinstance(b, TextBlock) else f"[attachment {b.file_id}@{b.version}]"
            if isinstance(b, AttachmentRef) else file_reference_line(b) for b in blocks]


def encode_output(output: NormalizedModelOutput) -> bytes:
    if isinstance(output, NormalizedFinal):
        doc: dict[str, Any] = {"kind": "final", "blocks": _blocks_to_json(output.blocks)}
    else:
        doc = {
            "kind": "tool_calls",
            "commentary": _blocks_to_json(output.commentary),
            "calls": [
                {"call_id": c.call_id, "ordinal": c.ordinal, "tool_id": c.tool_id, "arguments": thaw(c.arguments)}
                for c in output.calls
            ],
        }
    if output.continuation is not None:
        doc["continuation"] = thaw(output.continuation)
    return json.dumps(doc, sort_keys=True).encode()


class ScriptedAdapter:
    def __init__(self, script: Iterable[ScriptStep] = (), *, streaming: bool = False,
                 piece_delay: float = 0.0, first_delay: float = 0.0, image_input: bool = False,
                 screen_points: bool = False, memory_passes: bool = False) -> None:
        self._script = list(script)
        self._streaming = streaming
        self._image_input = image_input
        self._screen_points = screen_points
        self._memory_passes = memory_passes
        self._piece_delay = piece_delay
        self._first_delay = first_delay
        self.prepared_inputs: list[ModelInput] = []
        self.sent: list[NativePreparedRequest] = []

    def extend(self, steps: Iterable[ScriptStep]) -> None:
        self._script.extend(steps)

    @property
    def provider_key(self) -> str:
        return "scripted"

    def describe_capabilities(self, model_id: str) -> ProviderCapabilities:
        return ProviderCapabilities(model_id=model_id, parallel_tool_calls=True, streaming=self._streaming,
                                    image_input=self._image_input, screen_points=self._screen_points,
                                    memory_passes=self._memory_passes)

    def prepare_native_request(self, model_input: ModelInput) -> NativePreparedRequest:
        body = json.dumps(
            dataclasses.asdict(model_input), default=_json_default, sort_keys=True
        ).encode()
        self.prepared_inputs.append(model_input)
        return NativePreparedRequest(
            body=body,
            content_type="application/json",
            raw_representation=RawRepresentation.ADAPTER_SERIALIZED,
            endpoint="scripted://local",
            digest=hashlib.sha256(body).hexdigest(),
            streamed=model_input.request_options.stream and self._streaming,
        )

    async def invoke_prepared(self, request: NativePreparedRequest, sink: OutputSink | None = None,
                              capture: RawCapture | None = None) -> NativeInvocationOutcome:
        self.sent.append(request)
        if not self._script:
            return NativeNoResponse("script_exhausted")
        step = self._script.pop(0)
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, StreamCut):
            return await self._cut(step, sink, capture)
        if isinstance(step, (NativeNoResponse, NativeUncertain)):
            return step
        if isinstance(step, RawBody):
            return NativeReceived(step.body, "application/json")
        output = step.output if isinstance(step, Measured) else step
        body = encode_output(output)
        if request.streamed:
            if capture is not None:
                capture.content_type = "application/json"
                capture.append(body)
            await self._stream(output, sink)
        if isinstance(step, Measured):
            return NativeReceived(body, "application/json",
                                  TokenUsage(input_tokens=step.input_tokens, output_tokens=step.output_tokens,
                                             cached_input_tokens=step.cached_input_tokens,
                                             cache_write_tokens=step.cache_write_tokens))
        return NativeReceived(body, "application/json")

    async def _show(self, sink: OutputSink | None, delta: OutputDelta) -> None:
        if self._piece_delay:
            await asyncio.sleep(self._piece_delay)
        if sink is not None:
            try:
                sink(delta)
            except Exception:  # noqa: BLE001 — as a real adapter: display never fails the Run
                pass

    async def _stream(self, output: NormalizedModelOutput, sink: OutputSink | None) -> None:
        if output.continuation is not None:
            await self._show(sink, OutputDelta(thinking=True))
        if self._first_delay:
            await asyncio.sleep(self._first_delay)
        blocks = output.blocks if isinstance(output, NormalizedFinal) else output.commentary
        text = "".join(_blocks_to_json(blocks))
        for start in range(0, len(text), PIECE_CHARS):
            await self._show(sink, OutputDelta(text=text[start:start + PIECE_CHARS]))
        if isinstance(output, NormalizedToolCalls):
            for call in output.calls:
                await self._show(sink, OutputDelta(tool=str(call.tool_id)))

    async def _cut(self, step: StreamCut, sink: OutputSink | None,
                   capture: RawCapture | None) -> NativeInvocationOutcome:
        if capture is not None:
            capture.content_type = "text/event-stream"
        if self._first_delay:
            await asyncio.sleep(self._first_delay)
        for piece in step.pieces:
            if capture is not None:
                capture.append(piece.encode())
            await self._show(sink, OutputDelta(text=piece))
        if isinstance(step.end, asyncio.Event):
            await step.end.wait()
            return NativeUncertain("stream_incomplete")
        if isinstance(step.end, BaseException):
            raise step.end
        return step.end

    def normalize(self, received: NativeReceived) -> NormalizedModelOutput:
        try:
            doc = json.loads(received.body)
            continuation = doc.get("continuation")
            if doc["kind"] == "final":
                return NormalizedFinal(tuple(TextBlock(t) for t in doc["blocks"]), continuation)
            if doc["kind"] == "tool_calls":
                calls = tuple(
                    ToolCall(CallId(c["call_id"]), int(c["ordinal"]), ToolId(c["tool_id"]), c["arguments"])
                    for c in doc["calls"]
                )
                commentary = tuple(TextBlock(t) for t in doc.get("commentary", ()))
            else:
                raise ValueError(doc["kind"])
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderError(ProviderErrorCode.INVALID_MODEL_OUTPUT, str(exc)) from exc
        validate_tool_calls(calls)
        return NormalizedToolCalls(calls, commentary, continuation)
