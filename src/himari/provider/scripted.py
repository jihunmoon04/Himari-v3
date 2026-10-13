"""Scripted ProviderAdapter for development and tests (no network).

Each ``invoke_prepared`` consumes one script step:
- ``NormalizedFinal`` / ``NormalizedToolCalls`` → a received JSON body
- ``NativeNoResponse`` / ``NativeUncertain`` → returned as the outcome
- ``RawBody`` → received verbatim (e.g. to exercise invalid output)
- a ``BaseException`` instance → raised (e.g. to simulate a process crash)

The request body is ``ModelInput`` serialized to JSON, labelled
``ADAPTER_SERIALIZED`` — it is not a wire capture of any real Provider.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from himari.common import CallId, ContentBlock, RawRepresentation, TextBlock, ToolCall, ToolId
from himari.prompt.api import ModelInput
from himari.provider.api import (
    NativeInvocationOutcome,
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


type ScriptStep = NormalizedModelOutput | NativeNoResponse | NativeUncertain | RawBody | BaseException


def _json_default(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    raise TypeError(type(value).__name__)


def _blocks_to_json(blocks: Iterable[ContentBlock]) -> list[str]:
    return [b.text if isinstance(b, TextBlock) else f"[attachment {b.file_id}@{b.version}]" for b in blocks]


def encode_output(output: NormalizedModelOutput) -> bytes:
    if isinstance(output, NormalizedFinal):
        doc: dict[str, Any] = {"kind": "final", "blocks": _blocks_to_json(output.blocks)}
    else:
        doc = {
            "kind": "tool_calls",
            "commentary": _blocks_to_json(output.commentary),
            "calls": [
                {"call_id": c.call_id, "ordinal": c.ordinal, "tool_id": c.tool_id, "arguments": dict(c.arguments)}
                for c in output.calls
            ],
        }
    return json.dumps(doc, sort_keys=True).encode()


class ScriptedAdapter:
    def __init__(self, script: Iterable[ScriptStep] = ()) -> None:
        self._script = list(script)
        self.prepared_inputs: list[ModelInput] = []
        self.sent: list[NativePreparedRequest] = []

    def extend(self, steps: Iterable[ScriptStep]) -> None:
        self._script.extend(steps)

    @property
    def provider_key(self) -> str:
        return "scripted"

    def describe_capabilities(self, model_id: str) -> ProviderCapabilities:
        return ProviderCapabilities(model_id=model_id, parallel_tool_calls=True)

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
        )

    async def invoke_prepared(self, request: NativePreparedRequest) -> NativeInvocationOutcome:
        self.sent.append(request)
        if not self._script:
            return NativeNoResponse("script_exhausted")
        step = self._script.pop(0)
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, (NativeNoResponse, NativeUncertain)):
            return step
        if isinstance(step, RawBody):
            return NativeReceived(step.body, "application/json")
        return NativeReceived(encode_output(step), "application/json")

    def normalize(self, received: NativeReceived) -> NormalizedModelOutput:
        try:
            doc = json.loads(received.body)
            if doc["kind"] == "final":
                return NormalizedFinal(tuple(TextBlock(t) for t in doc["blocks"]))
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
        return NormalizedToolCalls(calls, commentary)
