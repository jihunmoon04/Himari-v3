from __future__ import annotations

from collections.abc import Callable
from types import ModuleType

import pytest
from testkit import runtime

from himari.control import api
from himari.control.api import ConversationDriver, ModelCatalog, ModelOffer, ResolvedModel
from himari.data.api import ModelSetting


def test_dtos_are_frozen(assert_api_dataclasses_frozen: Callable[[ModuleType], None]) -> None:
    assert_api_dataclasses_frozen(api)


def test_driver_implements_the_port() -> None:
    rt, _ = runtime()
    driver: ConversationDriver = rt.driver  # checked by mypy
    assert callable(driver.drive) and callable(driver.resume)


# --- ModelCatalog (V3-P64 §3, §4.2) -------------------------------------------

A = ModelOffer("a", "low", frozenset({"low", "high"}))
B = ModelOffer("b")
CATALOG = ModelCatalog((A, B), "b")


@pytest.mark.parametrize("offers, default, message", [
    ((A, A), "a", "offered twice"),
    ((A,), "b", "not offered"),
    ((A, ModelOffer("c", "max", frozenset({"low"}))), "a", "'max' is not supported by c"),
    ((ModelOffer("d", "low"),), "d", "'low' is not supported by d"),  # an undeclared model has no efforts
])
def test_a_catalog_is_refused_at_start(offers: tuple[ModelOffer, ...], default: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        ModelCatalog(offers, default)


@pytest.mark.parametrize("setting, expected", [
    (None, ResolvedModel(B, None)),
    (ModelSetting("a"), ResolvedModel(A, "low")),
    (ModelSetting("a", "high"), ResolvedModel(A, "high")),
    (ModelSetting("a", "max"), ResolvedModel(A, "low", "the reasoning effort 'max' is no longer offered for a; "
                                                        "using its default")),
    (ModelSetting("b", "low"), ResolvedModel(B, None, "the reasoning effort 'low' is no longer offered for b; "
                                                       "using its default")),
    (ModelSetting("gone", "high"), ResolvedModel(B, None, "gone is no longer offered; using the default model b")),
])
def test_resolve(setting: ModelSetting | None, expected: ResolvedModel) -> None:
    assert CATALOG.resolve(setting) == expected
    assert CATALOG.default is B and CATALOG.offer("a") is A and CATALOG.offer("gone") is None
