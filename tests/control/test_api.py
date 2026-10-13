from __future__ import annotations

from collections.abc import Callable
from types import ModuleType

from testkit import runtime

from himari.control import api
from himari.control.api import ConversationDriver


def test_dtos_are_frozen(assert_api_dataclasses_frozen: Callable[[ModuleType], None]) -> None:
    assert_api_dataclasses_frozen(api)


def test_driver_implements_the_port() -> None:
    rt, _ = runtime()
    driver: ConversationDriver = rt.driver  # checked by mypy
    assert callable(driver.drive) and callable(driver.resume)
