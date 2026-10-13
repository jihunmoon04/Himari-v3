"""Environment public contract: the EnvironmentPlugin SPI (ENV-06).

Source documents: docs/contracts.md §10, docs/decisions.md V3-040/041, V3-P19.

- One plugin per kind, registered at bootstrap. ``tools()`` returns the kind's
  fixed ToolHandlers; execution mechanics (e.g. the Inside Sandbox) stay inside
  the plugin package (ENV-04/05 are its internal contracts).
- Instances and Bindings live in Data (``WorkspaceState``). A ToolHandler gets
  the resolved Binding through ``ToolContext`` and re-validates before effects.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol, runtime_checkable

from himari.common import BindingId, EnvironmentKind, TrustedScope
from himari.data.api import BindingAuthority
from himari.tool.api import ToolHandler


class ConnectionStatus(Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    UNKNOWN = "unknown"  # default; never inferred from a stored "active" Binding


@dataclass(frozen=True)
class BindingObservation:
    """Time-scoped, best-effort observation. Not authorization."""

    binding_id: BindingId
    status: ConnectionStatus
    observed_at: datetime


@runtime_checkable
class EnvironmentPlugin(Protocol):
    @property
    def kind(self) -> EnvironmentKind: ...

    def tools(self) -> Sequence[ToolHandler]:
        """Fixed for the process lifetime; each ``definition.environment_kind``
        equals ``kind`` and ``tool_id`` is ``"<kind>/<name>"``."""
        ...

    async def observe(
        self, scope: TrustedScope, bindings: Sequence[BindingAuthority]
    ) -> Sequence[BindingObservation]:
        """ENV-01: side-effect free, bounded in time; UNKNOWN on timeout."""
        ...
