"""Provider construction for entry points (the composition root may import
implementations; interfaces may not)."""

from __future__ import annotations

from himari.provider.api import ProviderAdapter
from himari.provider.openrouter import OpenRouterAdapter


def openrouter(api_key: str) -> ProviderAdapter:
    return OpenRouterAdapter(api_key)
