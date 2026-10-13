from __future__ import annotations

from collections.abc import Callable
from types import ModuleType

import pytest

from himari.common import CallId, RawRepresentation, TextBlock, ToolCall, ToolId
from himari.prompt.api import ModelInput
from himari.provider import api
from himari.provider.api import (
    NativeInvocationOutcome,
    NativePreparedRequest,
    NativeReceived,
    NativeUncertain,
    NormalizedFinal,
    NormalizedModelOutput,
    ProviderAdapter,
    ProviderCapabilities,
    ProviderError,
    ProviderErrorCode,
    validate_tool_calls,
)
from himari.tool.api import ASK_USER_TOOL_ID


def call(call_id: str, ordinal: int, tool_id: str = "echo") -> ToolCall:
    return ToolCall(CallId(call_id), ordinal, ToolId(tool_id), {})


class FakeAdapter:
    @property
    def provider_key(self) -> str:
        return "fake"

    def describe_capabilities(self, model_id: str) -> ProviderCapabilities:
        return ProviderCapabilities(model_id=model_id)

    def prepare_native_request(self, model_input: ModelInput) -> NativePreparedRequest:
        return NativePreparedRequest(
            b"{}", "application/json", RawRepresentation.ADAPTER_SERIALIZED, "fake://", "0"
        )

    async def invoke_prepared(self, request: NativePreparedRequest) -> NativeInvocationOutcome:
        return NativeUncertain("not_implemented")

    def normalize(self, received: NativeReceived) -> NormalizedModelOutput:
        return NormalizedFinal(blocks=(TextBlock(received.body.decode()),))


def test_dtos_are_frozen(assert_api_dataclasses_frozen: Callable[[ModuleType], None]) -> None:
    assert_api_dataclasses_frozen(api)


def test_fake_adapter_satisfies_spi() -> None:
    assert isinstance(FakeAdapter(), ProviderAdapter)


@pytest.mark.parametrize(
    "calls",
    [
        [call("a", 0)],
        [call("a", 0), call("b", 1)],
        [call("q", 0, ASK_USER_TOOL_ID)],
    ],
)
def test_valid_tool_call_groups(calls: list[ToolCall]) -> None:
    validate_tool_calls(calls)


@pytest.mark.parametrize(
    "calls",
    [
        [],
        [call("a", 0), call("a", 1)],
        [call("a", 0), call("b", 0)],
        [call("q", 0, ASK_USER_TOOL_ID), call("b", 1)],  # V3-P15
    ],
)
def test_invalid_tool_call_groups(calls: list[ToolCall]) -> None:
    with pytest.raises(ProviderError) as exc:
        validate_tool_calls(calls)
    assert exc.value.code is ProviderErrorCode.INVALID_MODEL_OUTPUT
