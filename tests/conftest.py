"""Shared pytest fixtures. Module suites live in tests/<module>/ (V3-038)."""

from __future__ import annotations

import dataclasses
import inspect
from collections.abc import Callable
from types import ModuleType

import pytest

from himari.common import DeepFrozen


def _assert_api_dataclasses_frozen(module: ModuleType) -> None:
    defined = [
        obj
        for _, obj in inspect.getmembers(module, inspect.isclass)
        if dataclasses.is_dataclass(obj) and obj.__module__ == module.__name__
    ]
    assert defined, f"{module.__name__} defines no dataclasses"
    mutable = [cls.__name__ for cls in defined if not cls.__dataclass_params__.frozen]  # type: ignore[attr-defined]
    assert not mutable, f"public DTOs must be frozen: {mutable}"
    shallow = [cls.__name__ for cls in defined if not issubclass(cls, DeepFrozen)]
    assert not shallow, f"public DTOs must inherit DeepFrozen: {shallow}"


@pytest.fixture
def assert_api_dataclasses_frozen() -> Callable[[ModuleType], None]:
    return _assert_api_dataclasses_frozen
