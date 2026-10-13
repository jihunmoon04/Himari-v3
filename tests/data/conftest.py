from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _every_data_backend(data_backend: object) -> None:
    """Each test here runs on the in-memory store and on SQLite."""
