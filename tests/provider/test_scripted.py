from __future__ import annotations

import pytest
from testkit import calls, final

from himari.provider.api import NativeReceived, ProviderAdapter, ProviderError, ProviderErrorCode
from himari.provider.scripted import ScriptedAdapter, encode_output


@pytest.mark.parametrize("output", [final("hi"), calls(("echo", {"x": 1}), ("echo", {}), commentary="c")])
def test_encode_normalize_roundtrip(output: object) -> None:
    adapter = ScriptedAdapter()
    assert isinstance(adapter, ProviderAdapter)
    body = encode_output(output)  # type: ignore[arg-type]
    assert adapter.normalize(NativeReceived(body, "application/json")) == output


def test_malformed_body_is_invalid_model_output() -> None:
    with pytest.raises(ProviderError) as exc:
        ScriptedAdapter().normalize(NativeReceived(b'{"kind": "poem"}', "application/json"))
    assert exc.value.code is ProviderErrorCode.INVALID_MODEL_OUTPUT
