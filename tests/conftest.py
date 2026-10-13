"""Shared pytest fixtures. Module suites live in tests/<module>/ (V3-038)."""

from __future__ import annotations

import dataclasses
import inspect
from collections.abc import Callable, Iterator
from types import ModuleType

import pytest
import testkit

from himari.common import DeepFrozen
from himari.data.sqlite import SqliteStore


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


@pytest.fixture(params=["memory", "sqlite"])
def data_backend(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory,
                 monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Run the requesting test once per Data backend (tests/data and
    tests/integration use it for every test)."""
    if request.param == "sqlite":
        folder = tmp_path_factory.mktemp("db")
        counter = iter(range(1000))
        monkeypatch.setattr(testkit, "STORE_FACTORY", lambda: SqliteStore(folder / f"himari-{next(counter)}.db"))
    yield request.param
    while testkit.OPEN_STORES:
        testkit.OPEN_STORES.pop().close()
