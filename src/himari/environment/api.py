"""Environment public contract: the EnvironmentPlugin SPI (ENV-06).

Source documents: docs/contracts.md §10, docs/archive/decisions-v3.md V3-040/041, V3-P19.

- One plugin per kind, registered at bootstrap. ``tools()`` returns the kind's
  fixed ToolHandlers; execution mechanics stay inside the plugin package.
- Which Conversations have a kind lives in Data (``EnvironmentState``, set when
  the Conversation is created). The Dispatcher runs a kind's Tools only there;
  a handler finds its places (e.g. the Conversation's folder) from the scope.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol, runtime_checkable

from himari.common import DeepFrozen, EnvironmentKind, TrustedScope
from himari.tool.api import ToolHandler


class ConnectionStatus(Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    UNKNOWN = "unknown"  # default; never inferred from the stored kind alone


@dataclass(frozen=True)
class EnvironmentObservation(DeepFrozen):
    """Time-scoped, best-effort observation of a Conversation's Environment. Not authorization."""

    status: ConnectionStatus
    observed_at: datetime
    # What the model is told of the Environment's place, when it knows it
    # (V3-P74 §10.5): the working directory as the model writes
    # paths, the folders this Conversation may write, and what it may read. None/(): not given.
    working_directory: str | None = None
    writable: tuple[str, ...] = ()
    readable: str | None = None


@runtime_checkable
class EnvironmentPlugin(Protocol):
    @property
    def kind(self) -> EnvironmentKind: ...

    def tools(self) -> Sequence[ToolHandler]:
        """Fixed for the process lifetime; each ``definition.environment_kind``
        equals ``kind`` and ``tool_id`` is ``"<kind>/<name>"``."""
        ...

    async def observe(self, scope: TrustedScope) -> EnvironmentObservation:
        """ENV-01: this Conversation's place; side-effect free, bounded in time."""
        ...
