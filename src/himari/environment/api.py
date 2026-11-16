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

from himari.common import DeepFrozen, BindingId, EnvironmentKind, TrustedScope
from himari.data.api import BindingAuthority
from himari.tool.api import ToolHandler


class ConnectionStatus(Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    UNKNOWN = "unknown"  # default; never inferred from a stored "active" Binding


@dataclass(frozen=True)
class ObservedWorkspace(DeepFrozen):
    """A folder a Conversation may write, as the model is told of it (V3-P74
    §10.5): its path, the label and note the user gave it, and what the
    Environment knows of it (``notice``, e.g. that it is cloud-synced)."""

    path: str
    label: str
    note: str | None = None
    notice: str | None = None


@dataclass(frozen=True)
class BindingObservation(DeepFrozen):
    """Time-scoped, best-effort observation. Not authorization."""

    binding_id: BindingId
    status: ConnectionStatus
    observed_at: datetime
    # What the model is told of the Binding's place, when the Environment
    # knows it (V3-P74 §10.5): the working directory as the model writes
    # paths, and the folders this Conversation may write. None/(): not given.
    working_directory: str | None = None
    writable: tuple[str, ...] = ()
    # Given instead of ``writable`` when the folders have labels (V3-P74 §10.5),
    # with what the Conversation may read.
    workspaces: tuple[ObservedWorkspace, ...] = ()
    readable: str | None = None
    network: str | None = None  # what a command can reach (V3-P74 §11)


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
