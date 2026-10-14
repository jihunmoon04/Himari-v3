"""``ExternalAccess`` implementations (V3-P53).

P0 has one principal, the operator, who configured the sources; so every
configured source is available to it, and a Conversation's own choice
(Data ``ExternalSourceSettings``) overrides the source's default.
"""

from __future__ import annotations

from collections.abc import Mapping

from himari.common import TrustedScope
from himari.data.api import ExternalSourceSettings


class NoExternalAccess:
    """No External Tool source is ever enabled."""

    async def enabled_sources(self, scope: TrustedScope) -> frozenset[str]:
        return frozenset()


class ConfiguredExternalAccess:
    """``defaults``: every configured source → on/off by default. A choice
    for a source that is not configured (any more) is ignored."""

    def __init__(self, defaults: Mapping[str, bool], settings: ExternalSourceSettings) -> None:
        self._defaults = dict(defaults)
        self._settings = settings

    async def enabled_sources(self, scope: TrustedScope) -> frozenset[str]:
        chosen = {s.source: s.enabled for s in await self._settings.read_external_settings(scope)}
        return frozenset(source for source, default in self._defaults.items() if chosen.get(source, default))
